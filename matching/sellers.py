"""Чёрный список продавцов.

Объявления забаненного продавца не приходят в уведомлениях (поиски, ключевые
слова) и не считаются рынком: топы, оценка «ниже рынка», /pricecheck,
отчёты (через top_tracker.is_local_market).

Продавец у объявления — archive.seller_id: lalafo отдаёт user_id прямо в
выдаче, mashina — в публичной карточке /ads/<slug>/detail (подтягиваем для
находок по поискам). При бане сразу помечаем ВСЕ объявления продавца — у
обеих площадок есть выдача по продавцу.
"""

import logging
import re
import time

import httpx
from sqlalchemy import select, update

from config import USER_AGENT
from db.models import ArchivedListing, BannedSeller
from db.session import get_session

log = logging.getLogger(__name__)

LALAFO_FEED = "https://lalafo.kg/api/search/v3/feed/search"
LALAFO_DETAILS = "https://lalafo.kg/api/search/v3/feed/details/{}"
MASHINA_LISTINGS = "https://mashina.kg/api/mbank-proxy/v1/ads/listings"
MASHINA_HDR = {"User-Agent": USER_AGENT, "X-Project-Id": "1", "Accept": "application/json"}
CACHE_SEC = 60

_cache: tuple[float, set[tuple[str, str]]] = (0.0, set())


def banned_keys() -> set[tuple[str, str]]:
    global _cache
    if time.time() - _cache[0] > CACHE_SEC:
        with get_session() as s:
            keys = {(b.site, b.seller_id) for b in s.execute(select(BannedSeller)).scalars()}
        _cache = (time.time(), keys)
    return _cache[1]


def _reset() -> None:
    global _cache
    _cache = (0.0, set())


def is_banned(row) -> bool:
    """row — ArchivedListing, строка-кортеж с seller_id или RawListing."""
    sid = getattr(row, "seller_id", None)
    if sid is None and hasattr(row, "extra"):
        sid = (row.extra or {}).get("seller_id")
    site = getattr(row, "source_site", None)
    return bool(sid) and (site, str(sid)) in banned_keys()


def all_banned() -> list[BannedSeller]:
    with get_session() as s:
        return list(s.execute(select(BannedSeller).order_by(BannedSeller.created_at)).scalars())


# ---------- кто продавец ----------

async def _lalafo_ad_seller(ad_id: str) -> tuple[str | None, str | None]:
    from parsers import lalafo

    async with httpx.AsyncClient(timeout=20) as c:
        try:
            d = (await c.get(LALAFO_DETAILS.format(ad_id), headers=lalafo.HEADERS)).json()
        except (httpx.HTTPError, ValueError):
            return None, None
    uid = d.get("user_id")
    return (str(uid) if uid else None), (d.get("user") or {}).get("username")


async def resolve(text: str) -> tuple[str, str, str | None] | None:
    """(площадка, id продавца, имя) по ссылке на объявление/профиль или по
    объявлению из архива. None — не понял."""
    t = text.strip()
    m = re.search(r"lalafo\.kg/.*?(?:user|profile)[/=](\d+)", t)
    if m:
        return "lalafo", m.group(1), None
    m = re.search(r"lalafo\.kg/.*-id-(\d+)", t)
    if m:
        sid, name = await _lalafo_ad_seller(m.group(1))
        return ("lalafo", sid, name) if sid else None
    m = re.search(r"mashina\.kg/details/([\w-]+)", t)
    if m:
        from parsers import mashina

        sid, name = await mashina.seller_of(m.group(1))
        return ("mashina", sid, name) if sid else None
    return None


async def seller_of_row(row: ArchivedListing) -> tuple[str | None, str | None]:
    if row.seller_id:
        return row.seller_id, row.seller_name
    found = await resolve(row.url)
    return (found[1], found[2]) if found else (None, None)


# ---------- бан ----------

async def _mark_all(site: str, seller_id: str, name: str | None) -> int:
    """Проставляет продавца всем его объявлениям в архиве (по выдаче площадки)."""
    ids: list[str] = []
    async with httpx.AsyncClient(timeout=30) as c:
        if site == "lalafo":
            from parsers import lalafo

            for page in range(1, 30):
                d = (await c.get(LALAFO_FEED, headers=lalafo.HEADERS, params={
                    "user_id": seller_id, "per-page": 40, "page": page})).json()
                ids += [str(i["id"]) for i in d.get("items") or []
                        if str(i.get("user_id")) == str(seller_id)]
                if page >= (d.get("_meta") or {}).get("pageCount", 1):
                    break
        else:
            for page in range(1, 30):
                d = (await c.get(MASHINA_LISTINGS, headers=MASHINA_HDR, params={
                    "user_id": seller_id, "page": page, "size": 100})).json()
                # external_id mashina — хвост slug после последнего «-»
                ids += [str(i["slug"]).rsplit("-", 1)[-1] for i in d.get("items") or []
                        if str(i.get("user_id")) == str(seller_id) and i.get("slug")]
                if page >= (d.get("pages") or 1):
                    break
    if not ids:
        return 0
    with get_session() as s:
        n = 0
        for i in range(0, len(ids), 500):
            n += s.execute(update(ArchivedListing).where(
                ArchivedListing.source_site == site, ArchivedListing.external_id.in_(ids[i:i + 500]),
            ).values(seller_id=str(seller_id), seller_name=name)).rowcount
        s.commit()
    return n


async def ban(site: str, seller_id: str, name: str | None) -> tuple[bool, int]:
    """(новый ли бан, сколько его объявлений в архиве помечено)."""
    with get_session() as s:
        exists = s.execute(select(BannedSeller).where(
            BannedSeller.site == site, BannedSeller.seller_id == str(seller_id))).scalar_one_or_none()
        if exists is None:
            s.add(BannedSeller(site=site, seller_id=str(seller_id), name=name))
            s.commit()
    _reset()
    try:
        marked = await _mark_all(site, str(seller_id), name)
    except (httpx.HTTPError, ValueError) as e:
        log.warning("бан %s:%s — объявления не подтянулись: %s", site, seller_id, e)
        marked = 0
    log.info("Бан продавца %s:%s (%s): помечено объявлений %d", site, seller_id, name, marked)
    return exists is None, marked


def unban(ban_id: int) -> BannedSeller | None:
    with get_session() as s:
        b = s.get(BannedSeller, ban_id)
        if b:
            s.delete(b)
            s.commit()
    _reset()
    return b


async def refresh_all() -> None:
    """После полного скана: новые объявления забаненных продавцов (mashina в
    скане продавца не показывает — подтягиваем по выдаче продавца)."""
    for b in all_banned():
        try:
            await _mark_all(b.site, b.seller_id, b.name)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("чёрный список %s:%s: %s", b.site, b.seller_id, e)


DEFAULT_BANS = [("lalafo", "7313102", "Lotte Auto Global")]  # авто в Корее под видом «в наличии»
DEFAULT_KEY = "banned_sellers_default_v1"


async def ensure_defaults() -> None:
    from bot.settings_store import get_setting, set_setting

    if get_setting(DEFAULT_KEY) == "done":
        return
    for site, sid, name in DEFAULT_BANS:
        await ban(site, sid, name)
    set_setting(DEFAULT_KEY, "done")
