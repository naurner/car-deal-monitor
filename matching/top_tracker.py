"""Топ-3 самых дешёвых объявлений по марка+модель+поколение (весь рынок).

Логика на каждый цикл (после full_scan.run(), см. main.py):
1. Группируем все АКТИВНЫЕ объявления архива с известной ценой по
   (brand, model, generation_code). Машины с иностранным учётом, «в пути»,
   «на заказ», не растаможенные или находящиеся за границей в топ и среднюю
   цену не попадают (is_local_market). Самых дешёвых в каждой группе
   дочитывает ИИ (matching/ai_reader.py): битые, на запчасти, цена за взнос
   или аренду, учёт/нахождение не в КР по тексту описания — тоже вне топа.
2. Для новой группы (ещё не было ModelTop) — молча сохраняем базовую линию
   (без уведомления, иначе первый прогон на всём рынке = тысячи сообщений).
3. Подозрительные цены («8$», «320 сом» — цена в тысячах, опечатки, сомы
   вместо долларов: ниже 400$ или в 2.5 раза дальше медианы группы) в топ и
   среднюю цену не попадают — молча, без уведомлений (market.split_suspicious).
4. Если состав топ-3 изменился — уведомление "новый топ" с вошедшими
   объявлениями, средней ценой и прошлым топом; фото новых лотов
   скачиваются на диск, фото выбывших — удаляются.
5. Иначе — тихо обновляем среднюю цену/дату.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
from sqlalchemy import select

from config import AI_MAX_PER_CYCLE, DATA_DIR, USER_AGENT
from db.models import ArchivedListing, ModelTop
from db.session import get_session
from matching import ai_reader, market, sellers
from matching import generations as gen_dict

log = logging.getLogger(__name__)

TOP_N = 3
# разовая тихая пересборка всех топов: после перехода на полный рынок lalafo
# (в 3 раза больше объявлений) и чистки подозрительных цен каждое изменение
# иначе пришло бы отдельным «новым минимумом»
REBASELINE_KEY = "tops_rebaseline_v2"
PHOTOS_PER_LISTING = 1   # фото на лот в топе (рынок целиком — тысячи групп)
PHOTOS_DIR = DATA_DIR / "top_photos"
PHOTOS_DIR.mkdir(exist_ok=True)


def _safe(s: str | None) -> str:
    return re.sub(r"[^\w-]", "_", s or "none", flags=re.UNICODE)


def _group_dir(brand: str, model: str, gen_code: str) -> Path:
    return PHOTOS_DIR / f"{_safe(brand)}_{_safe(model)}_{_safe(gen_code)}"


@dataclass
class TopEvent:
    kind: str  # "new_top" ("anomaly" больше не шлём — подозрительные цены отсекаются)
    brand: str
    model: str
    generation_name: str | None
    entered: list[ArchivedListing] = field(default_factory=list)  # для new_top
    top3: list[ArchivedListing] = field(default_factory=list)
    avg_price: float | None = None
    prev_top: list[dict] | None = None
    prev_avg: float | None = None
    suspicious_listing: ArchivedListing | None = None  # для anomaly
    prev_top1_price: float | None = None


async def _download_photo(url: str, dest: Path) -> bool:
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
        return True
    except Exception:
        log.warning("Не удалось скачать фото %s", url)
        return False


async def _save_photos_for(listing: ArchivedListing, group_dir: Path) -> list[str]:
    group_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for i, url in enumerate((listing.photos or [])[:PHOTOS_PER_LISTING]):
        ext = ".jpg"
        for cand in (".jpg", ".jpeg", ".png", ".webp"):
            if cand in url.lower():
                ext = cand
                break
        dest = group_dir / f"{listing.id}_{i}{ext}"
        if await _download_photo(url, dest):
            saved.append(str(dest))
    return saved


def _delete_photos(paths: list[str]) -> None:
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass


async def _entry_for(listing: ArchivedListing, group_dir: Path) -> dict:
    photos = await _save_photos_for(listing, group_dir)
    return {
        "archive_id": listing.id,
        "price": listing.price_usd,
        "photo_paths": photos,
        "url": listing.url,
        "title": listing.title,
        "source_site": listing.source_site,
    }


def _generation_display_name(brand: str, model: str, gen_code: str) -> str | None:
    if not gen_code:
        return None
    for g in gen_dict.get_generations(brand, model):
        if g.generation_code == gen_code:
            return g.generation_name
    return gen_code


# Машины за границей: в поле города mashina ставит страну/регион вместо города.
FOREIGN_LOCATIONS = {
    "китай", "южная корея", "корея", "япония", "сша", "канада", "оаэ", "дубай",
    "европа", "германия", "литва", "польша", "грузия", "армения", "россия",
    "казахстан", "узбекистан", "беларусь", "турция",
}
NOT_IN_KG = {"в пути", "на заказ", "не растаможен"}


def is_local_market(row: ArchivedListing) -> bool:
    """Машина на кыргызском рынке: не на иностранном учёте и физически в КР.
    Иначе её цена несопоставима с местными и ломает топ/среднюю цену.
    Продавцы из чёрного списка (matching/sellers.py) — тоже не рынок."""
    if sellers.is_banned(row):
        return False
    reg = (row.country or "").lower()
    if reg and "кыргыз" not in reg and "не стоит" not in reg:
        return False
    if (row.availability or "").lower() in NOT_IN_KG:
        return False
    if (row.city or "").lower() in FOREIGN_LOCATIONS:
        return False
    return True


def _fetch_active_groups() -> tuple[dict[tuple[str, str, str], list[ArchivedListing]], set[int]]:
    """(группы местных объявлений, id исключённых — для очистки старых топов)."""
    with get_session() as s:
        rows = list(
            s.execute(
                select(ArchivedListing).where(
                    ArchivedListing.is_active.is_(True),
                    ArchivedListing.price_usd.isnot(None),
                    ArchivedListing.brand.isnot(None),
                    ArchivedListing.model.isnot(None),
                )
            ).scalars()
        )
    groups: dict[tuple[str, str, str], list[ArchivedListing]] = {}
    excluded_ids: set[int] = set()
    for r in rows:
        if not is_local_market(r):
            excluded_ids.add(r.id)
            continue
        key = (r.brand, r.model, r.generation_code or "")
        groups.setdefault(key, []).append(r)
    for key in list(groups):
        ok, bad = market.split_suspicious(groups[key])
        excluded_ids.update(r.id for r in bad)
        if ok:
            groups[key] = ok
        else:
            del groups[key]
    return groups, excluded_ids


def _plausible_cheapest(listings: list[ArchivedListing], excluded_ids: set[int]) -> list[ArchivedListing]:
    """В маленькой группе медианы нет — самых дешёвых сверяем с похожими
    машинами того же года по всей модели."""
    if len(listings) >= market.MIN_SAMPLE:
        return listings
    out = []
    for l in sorted(listings, key=lambda l: l.price_usd):
        if len(out) < TOP_N + 1 and market.price_check(l):
            excluded_ids.add(l.id)
            continue
        out.append(l)
    return out


def _silent_top(existing: ModelTop, candidate_top3: list[ArchivedListing]) -> None:
    """Новый состав топа без уведомления и без скачивания фото."""
    new_ids = {l.id for l in candidate_top3}
    _delete_photos([p for e in existing.top_json if e["archive_id"] not in new_ids
                    for p in e.get("photo_paths", [])])
    old = {e["archive_id"]: e for e in existing.top_json}
    existing.top_json = [
        old.get(l.id) or {"archive_id": l.id, "price": l.price_usd, "photo_paths": [],
                          "url": l.url, "title": l.title, "source_site": l.source_site}
        for l in candidate_top3
    ]
    existing.last_anomaly_archive_id = None


async def _rebuild_top(existing: ModelTop, candidate_top3: list[ArchivedListing],
                       group_dir: Path) -> list[ArchivedListing]:
    """Переписывает сохранённый топ под новый состав; возвращает вошедшие лоты."""
    prev_ids = {e["archive_id"] for e in existing.top_json}
    new_ids = {l.id for l in candidate_top3}
    _delete_photos([
        p for e in existing.top_json if e["archive_id"] not in new_ids
        for p in e.get("photo_paths", [])
    ])
    new_top_json = []
    for l in candidate_top3:
        old_entry = next((e for e in existing.top_json if e["archive_id"] == l.id), None)
        new_top_json.append(old_entry or await _entry_for(l, group_dir))
    existing.top_json = new_top_json
    existing.last_anomaly_archive_id = None
    return [l for l in candidate_top3 if l.id not in prev_ids]


async def evaluate_all() -> list[TopEvent]:
    from bot.settings_store import get_setting, set_setting

    rebaseline = get_setting(REBASELINE_KEY) != "done"
    groups, excluded_ids = _fetch_active_groups()
    events: list[TopEvent] = []

    # ИИ читает описания самых дешёвых: «битая», «на запчасти», «цена за
    # первый взнос», «учёт Казахстан» в тексте — такие тоже не рынок КР
    with get_session() as s:
        stored_ids = {e["archive_id"] for t in s.execute(select(ModelTop)).scalars()
                      for e in t.top_json}
    await ai_reader.review_cheapest(groups, stored_ids, TOP_N + 3, AI_MAX_PER_CYCLE)
    for key in list(groups):
        good = []
        for l in groups[key]:
            if ai_reader.market_ok(l):
                good.append(l)
            else:
                excluded_ids.add(l.id)
        good = _plausible_cheapest(good, excluded_ids)
        if good:
            groups[key] = good
        else:
            del groups[key]
    log.info("Топ-трекер: %d групп марка+модель+поколение%s", len(groups),
             " (тихая пересборка всех топов)" if rebaseline else "")

    with get_session() as s:
        for (brand, model, gen_code), listings in groups.items():
            # фиксируем накопленное: дальше группа может качать фото, а
            # открытая транзакция всё это время блокировала бы другие задачи
            s.commit()
            listings.sort(key=lambda l: l.price_usd)
            candidate_top3 = listings[:TOP_N]
            avg_price = sum(l.price_usd for l in listings) / len(listings)
            gen_name = _generation_display_name(brand, model, gen_code)

            existing = s.execute(
                select(ModelTop).where(
                    ModelTop.brand == brand,
                    ModelTop.model == model,
                    ModelTop.generation_code == gen_code,
                )
            ).scalar_one_or_none()

            group_dir = _group_dir(brand, model, gen_code)

            if existing is None:
                # базовая линия — молча, без уведомления и без скачивания фото
                # (фото качаем только с момента, когда трекер реально начал следить)
                top_json = [
                    {
                        "archive_id": l.id, "price": l.price_usd, "photo_paths": [],
                        "url": l.url, "title": l.title, "source_site": l.source_site,
                    }
                    for l in candidate_top3
                ]
                s.add(ModelTop(
                    brand=brand, model=model, generation_code=gen_code,
                    generation_name=gen_name, top_json=top_json,
                    avg_price=avg_price, sample_count=len(listings),
                ))
                continue

            if rebaseline:
                _silent_top(existing, candidate_top3)
                existing.generation_name = gen_name
                existing.avg_price = avg_price
                existing.sample_count = len(listings)
                existing.updated_at = datetime.utcnow()
                continue

            if any(e["archive_id"] in excluded_ids for e in existing.top_json):
                # в сохранённый топ раньше попала машина не с рынка КР (учёт,
                # локация или то, что ИИ вычитал в описании) —
                # молча пересобираем без неё, иначе её уход выглядел
                # бы как «новый минимум», а её цена — как повод для аномалии
                await _rebuild_top(existing, candidate_top3, group_dir)
                existing.avg_price = avg_price
                existing.sample_count = len(listings)
                existing.updated_at = datetime.utcnow()
                continue

            prev_top1_price = min((e["price"] for e in existing.top_json if e.get("price")),
                                  default=None)
            prev_ids = {e["archive_id"] for e in existing.top_json}
            new_ids = {l.id for l in candidate_top3}
            if new_ids != prev_ids and any(
                ai_reader.pending(l) for l in candidate_top3 if l.id not in prev_ids
            ):
                # новичка в топе ИИ ещё не прочитал (кончился лимит на цикл) —
                # не объявляем, пока не прочитает: в следующем цикле он первый
                continue
            if new_ids != prev_ids:
                prev_top = existing.top_json
                prev_avg = existing.avg_price
                # сообщаем только о НОВОМ МИНИМУМЕ: дешевле прошлой топ-1.
                # Ушла дешёвая машина и в топ поднялась дороже — молча
                cheaper = [l for l in candidate_top3 if l.id not in prev_ids
                           and (prev_top1_price is None or l.price_usd < prev_top1_price)]
                if cheaper:
                    await _rebuild_top(existing, candidate_top3, group_dir)
                    events.append(TopEvent(
                        kind="new_top", brand=brand, model=model,
                        generation_name=gen_name, entered=cheaper, top3=candidate_top3,
                        avg_price=avg_price, prev_top=prev_top, prev_avg=prev_avg,
                    ))
                else:
                    _silent_top(existing, candidate_top3)

            existing.avg_price = avg_price
            existing.sample_count = len(listings)
            existing.updated_at = datetime.utcnow()

        # топы групп, где местных объявлений не осталось вовсе, а в топе сидят
        # иностранные машины — удаляем; появится местная — начнётся новая
        # базовая линия без уведомления
        for top in list(s.execute(select(ModelTop)).scalars()):
            key = (top.brand, top.model, top.generation_code)
            if key not in groups and any(e["archive_id"] in excluded_ids for e in top.top_json):
                _delete_photos([p for e in top.top_json for p in e.get("photo_paths", [])])
                s.delete(top)

        s.commit()

    if rebaseline:
        set_setting(REBASELINE_KEY, "done")
    if events:
        log.info("Топ-трекер: %d новых минимумов", len(events))
    return events
