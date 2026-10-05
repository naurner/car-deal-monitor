"""Глобальный архив объявлений: всё спаршенное сохраняется навсегда.

- upsert_listings: вызывается после каждого парсинга;
- search_archive: поиск по архиву (включая проданные/удалённые);
- actualize: проверка живости старых объявлений, простановка sold_at.
"""

import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select

from config import USER_AGENT
from db.models import ArchivedListing
from db.session import get_session
from parsers.base import RawListing

log = logging.getLogger(__name__)


def _resolve_generation_code(brand: str | None, model: str | None, year: int | None) -> str | None:
    """Доопределяет поколение по году через справочник (для lalafo, где
    поколения в данных нет) — нужно для группировки в топ-трекере."""
    if not brand or not model or not year:
        return None
    from matching import generations  # локальный импорт: избегаем цикла archive<->generations

    gen = generations.resolve_generation(brand, model, year)
    return gen.generation_code if gen else None

ACTUALIZE_AFTER_HOURS = 24   # не проверять чаще, чем раз в сутки
ACTUALIZE_BATCH = 30         # объявлений за один цикл


UPSERT_COMMIT_EVERY = 200  # порциями: полный скан иначе держит базу занятой
                           # минутами, и автопроверка не может записать находки


def upsert_listings(listings: list[RawListing]) -> None:
    from matching import market  # market -> top_tracker -> archive

    now = datetime.utcnow()
    with get_session() as s:
        for i, l in enumerate(listings, 1):
            if i % UPSERT_COMMIT_EVERY == 0:
                s.commit()
            row = s.execute(
                select(ArchivedListing).where(
                    ArchivedListing.source_site == l.source_site,
                    ArchivedListing.external_id == l.external_id,
                )
            ).scalar_one_or_none()
            posted = None
            if l.extra.get("posted_at"):
                try:
                    posted = datetime.fromisoformat(l.extra["posted_at"])
                except ValueError:
                    pass
            gen_code = l.generation_code or _resolve_generation_code(l.brand, l.model, l.year)
            sig = market.desc_sig(l.brand, l.model, l.year, l.description)
            if row is None:
                s.add(ArchivedListing(
                    first_price_usd=l.price_usd,
                    price_kgs=l.extra.get("price_kgs"),
                    seller_id=l.extra.get("seller_id"),
                    seller_name=l.extra.get("seller_name"),
                    desc_sig=sig,
                    relist_of=market.relist_root(s, l.source_site, sig),
                    source_site=l.source_site,
                    external_id=l.external_id,
                    url=l.url,
                    title=l.title,
                    brand=l.brand,
                    model=l.model,
                    generation_code=gen_code,
                    year=l.year,
                    price_usd=l.price_usd,
                    description=l.description,
                    photos=l.photos,
                    characteristics=l.extra.get("characteristics") or {},
                    country=l.country,
                    city=l.extra.get("city"),
                    availability=l.extra.get("availability"),
                    posted_at=posted,
                ))
            else:
                row.last_seen_at = now
                row.is_active = True
                row.sold_at = None
                if l.price_usd is not None:
                    row.price_usd = l.price_usd
                    row.price_kgs = l.extra.get("price_kgs")
                if l.description:
                    row.description = l.description
                if l.country:
                    row.country = l.country
                if l.extra.get("city"):
                    row.city = l.extra["city"]
                # наличие меняется (машина доехала / растаможили) — всегда
                # берём свежее значение, отсутствие = в наличии
                row.availability = l.extra.get("availability")
                if l.brand and not row.brand:
                    row.brand = l.brand
                if l.model and not row.model:
                    row.model = l.model
                if gen_code and not row.generation_code:
                    row.generation_code = gen_code
                if l.extra.get("seller_id"):
                    row.seller_id = l.extra["seller_id"]
                    row.seller_name = l.extra.get("seller_name") or row.seller_name
                if row.first_price_usd is None:
                    row.first_price_usd = l.price_usd
                if sig and not row.desc_sig:  # описание mashina приходит с деталями
                    row.desc_sig = sig
                    row.relist_of = market.relist_root(s, row.source_site, sig, row.id)
        s.commit()


MISSING_MIN_SEEN = 1000  # скан вернул подозрительно мало — не делаем выводов


def mark_missing(site: str, scan_started: datetime, seen: int) -> int:
    """После ПОЛНОГО скана площадки: активные объявления, которых в нём не
    было, сняты с продажи (не ждём, пока actualize проверит их по 30 штук).
    sold_at — когда пропажу заметили; сняли где-то между last_seen_at и им."""
    if seen < MISSING_MIN_SEEN:
        log.warning("%s: скан вернул %d объявлений — пропавшие не отмечаю", site, seen)
        return 0
    from sqlalchemy import update

    with get_session() as s:
        res = s.execute(
            update(ArchivedListing)
            .where(ArchivedListing.source_site == site,
                   ArchivedListing.is_active.is_(True),
                   ArchivedListing.last_seen_at < scan_started)
            .values(is_active=False, sold_at=scan_started)
        )
        s.commit()
    log.info("%s: %d объявлений пропали с площадки — сняты с продажи", site, res.rowcount)
    return res.rowcount


def _archive_query(brand, model, price_max, keyword, include_sold):
    """Общий фильтр для поиска и подсчёта по архиву. Слово ищется в SQL
    (LIKE по названию/описанию), поэтому охватывает ВЕСЬ архив."""
    from sqlalchemy import or_

    q = select(ArchivedListing)
    if brand:
        q = q.where(ArchivedListing.brand == brand)
    if model:
        q = q.where(ArchivedListing.model == model)
    if price_max is not None:
        q = q.where(ArchivedListing.price_usd <= price_max)
    if not include_sold:
        q = q.where(ArchivedListing.is_active.is_(True))
    if keyword:
        # SQLite сравнивает без учёта регистра только латиницу: «россия» не
        # находило «Россия». Перебираем написания: как есть, строчными,
        # ПРОПИСНЫМИ, с заглавной
        kw = keyword.strip()
        variants = {kw, kw.lower(), kw.upper(), kw.capitalize(), kw[:1].upper() + kw[1:]}
        q = q.where(or_(*(
            col.like(f"%{v}%") for v in variants
            for col in (ArchivedListing.title, ArchivedListing.description)
        )))
    return q


def count_archive(
    brand: str | None = None,
    model: str | None = None,
    price_max: float | None = None,
    keyword: str | None = None,
    include_sold: bool = True,
) -> int:
    from sqlalchemy import func

    q = _archive_query(brand, model, price_max, keyword, include_sold)
    with get_session() as s:
        return s.execute(
            select(func.count()).select_from(q.subquery())
        ).scalar_one()


def search_archive(
    brand: str | None = None,
    model: str | None = None,
    price_max: float | None = None,
    keyword: str | None = None,
    include_sold: bool = True,
    limit: int = 10,
    offset: int = 0,
) -> list[ArchivedListing]:
    q = _archive_query(brand, model, price_max, keyword, include_sold)
    q = q.order_by(ArchivedListing.last_seen_at.desc()).limit(limit).offset(offset)
    with get_session() as s:
        return list(s.execute(q).scalars())


async def actualize() -> int:
    """Проверяет давно не виденные активные объявления; помечает проданные."""
    cutoff = datetime.utcnow() - timedelta(hours=ACTUALIZE_AFTER_HOURS)
    with get_session() as s:
        stale = list(
            s.execute(
                select(ArchivedListing)
                .where(
                    ArchivedListing.is_active.is_(True),
                    ArchivedListing.last_seen_at < cutoff,
                )
                .order_by(ArchivedListing.last_seen_at)
                .limit(ACTUALIZE_BATCH)
            ).scalars()
        )
    if not stale:
        return 0
    sold = 0
    async with httpx.AsyncClient(
        timeout=20, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        for row in stale:
            try:
                resp = await client.get(row.url)
                alive = resp.status_code == 200
                # mashina перенаправляет удалённые объявления на поиск
                if alive and "/details/" not in str(resp.url) and row.source_site == "mashina":
                    alive = False
            except httpx.HTTPError:
                continue  # сеть/таймаут — не делаем выводов
            with get_session() as s:
                fresh_row = s.get(ArchivedListing, row.id)
                if alive:
                    fresh_row.last_seen_at = datetime.utcnow()
                else:
                    fresh_row.is_active = False
                    fresh_row.sold_at = datetime.utcnow()
                    sold += 1
                s.commit()
    if sold:
        log.info("Архив: %d объявлений помечено проданными", sold)
    return sold
