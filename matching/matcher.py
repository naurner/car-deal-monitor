"""Пайплайн проверки запроса: парсинг -> строгий фильтр -> дедуп -> результат."""

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

import re

from db.models import ListingSeen, WatchRequest
from db.session import get_session
from matching import ai_reader, archive, features, generations, market, sellers
from parsers import lalafo, mashina
from parsers.base import RawListing

log = logging.getLogger(__name__)


@dataclass
class MatchResult:
    listing: RawListing
    watch_request: WatchRequest
    is_price_drop: bool = False
    old_price: float | None = None
    generation_unknown: bool = False


def _match_one_generation(gen_code, year_from, year_to, l: RawListing) -> tuple[bool, bool]:
    """Совпадение объявления с ОДНИМ поколением. (проходит, поколение_не_определено)."""
    surrogate = {mashina.GEN_BASE, mashina.GEN_RESTYLING}
    if l.generation_code == gen_code:
        return True, False
    both_real = (
        l.generation_code
        and l.generation_code not in surrogate
        and gen_code not in surrogate
    )
    both_surrogate = l.generation_code in surrogate and gen_code in surrogate
    if both_real or both_surrogate:
        return False, False  # коды сопоставимы и не совпали
    # код объявления неизвестен (lalafo) — фильтруем по годам поколения
    if l.year is not None and year_from and year_to:
        return year_from <= l.year <= year_to, False
    # поколение не определить — показываем с пометкой (решение из спеки)
    return True, True


def _passes_strict(req: WatchRequest, l: RawListing) -> tuple[bool, bool]:
    """(проходит, поколение_не_определено). Строго: цена + поколение(год).

    Мультивыбор: объявление проходит, если совпадает с ЛЮБЫМ из поколений.
    """
    if l.price_usd is not None:
        if req.price_min is not None and l.price_usd < req.price_min:
            return False, False
        if req.price_max is not None and l.price_usd > req.price_max:
            return False, False

    # список поколений (мультивыбор) имеет приоритет над одиночным полем
    gens = req.generations or []
    if not gens and req.generation_code:
        gens = [{
            "code": req.generation_code,
            "year_from": req.year_from,
            "year_to": req.year_to,
        }]

    if gens:
        any_unknown = False
        for g in gens:
            ok, unknown = _match_one_generation(
                g.get("code"), g.get("year_from"), g.get("year_to"), l
            )
            if ok:
                return True, unknown
            any_unknown = any_unknown or unknown
        return False, any_unknown

    if req.year_from and l.year is not None and l.year < req.year_from:
        return False, False
    if req.year_to and l.year is not None and l.year > req.year_to:
        return False, False
    return True, False


PARSERS = [("mashina", mashina.search), ("lalafo", lalafo.search)]

# откуда брать значение параметра из объявления (extra карточки или
# характеристики страницы деталей mashina)
_CHAR_KEYS = {
    "color": "Цвет",
    "body_type": "Кузов",
    "transmission": "Коробка",
    "drive": "Привод",
    "fuel": "Топливо",
}


def _listing_param(l: RawListing, key: str) -> str | None:
    val = l.extra.get(key)
    if val:
        return str(val)
    chars = l.extra.get("characteristics") or {}
    char_key = _CHAR_KEYS.get(key)
    return chars.get(char_key) if char_key else None


def _listing_mileage(l: RawListing) -> int | None:
    if l.extra.get("mileage_km"):
        return int(l.extra["mileage_km"])
    chars = l.extra.get("characteristics") or {}
    raw = chars.get("Пробег")
    if raw:
        digits = re.sub(r"\D", "", raw)
        if digits:
            km = int(digits)
            return int(km * 1.609) if "миль" in raw.lower() else km
    return None


def _passes_extra(req: WatchRequest, l: RawListing) -> bool:
    """Фильтры по второстепенным параметрам: мультивыбор и исключение.

    Значение в extra_params: {"mode": "include"|"exclude", "values": [...]}
    (старый формат — строка — трактуется как include с одним значением).
    Если у объявления параметр неизвестен — пропускаем (не отсеиваем).
    """
    for key, cfg in (req.extra_params or {}).items():
        if key == "mileage_max":
            try:
                limit = float(str(cfg).replace(" ", ""))
            except ValueError:
                continue
            mileage = _listing_mileage(l)
            if mileage is not None and mileage > limit:
                return False
            continue
        if key not in _CHAR_KEYS:
            continue  # engine_volume/equipment/interior — информационные
        if isinstance(cfg, dict):
            mode, values = cfg.get("mode", "include"), cfg.get("values", [])
        else:
            mode, values = "include", [cfg]
        if not values:
            continue
        actual = _listing_param(l, key)
        if not actual:
            continue
        norm_actual = mashina.normalize_option(actual)
        norm_values = {mashina.normalize_option(str(v)) for v in values}
        hit = any(v in norm_actual or norm_actual in v for v in norm_values)
        if mode == "include" and not hit:
            return False
        if mode == "exclude" and hit:
            return False
    return True


def _reg_country_matches(req: WatchRequest, country: str | None) -> tuple[bool, bool]:
    """(проходит, учет_не_определён). Строгий фильтр по стране учета."""
    if not req.reg_country:
        return True, False
    if not country:
        return True, True  # данных нет — показываем с пометкой
    return req.reg_country.lower() in country.lower(), False


async def check_request(req: WatchRequest) -> list[MatchResult]:
    all_listings: list[RawListing] = []
    for site, search_fn in PARSERS:
        try:
            found = await search_fn(
                req.brand, req.model, req.price_min, req.price_max,
                req.year_from, req.year_to,
            )
            all_listings.extend(found)
            if site == "mashina":
                generations.update_dictionary(req.brand, req.model, found)
        except Exception:
            log.exception("%s search failed for request %s", site, req.id)
    # TODO: unaa — SPA с reCAPTCHA, потребует Playwright (см. PARSING_NOTES)
    archive.upsert_listings(all_listings)

    # Детали mashina тянем ДО открытия сессии: сеть внутри транзакции держит
    # блокировку базы, и параллельные задачи падают с «database is locked».
    with get_session() as s:
        known = {
            (x.source_site, x.external_id) for x in s.execute(
                select(ListingSeen).where(ListingSeen.watch_request_id == req.id)
            ).scalars()
        }
    detailed: list[RawListing] = []
    for i, l in enumerate(all_listings):
        if (l.source_site, l.external_id) in known or l.source_site != "mashina":
            continue
        if _passes_strict(req, l)[0]:
            all_listings[i] = await mashina.fetch_details(l)
            detailed.append(all_listings[i])
    if detailed:
        archive.upsert_listings(detailed)  # описание и учёт — для ИИ ниже

    results: list[MatchResult] = []
    with get_session() as s:
        for l in all_listings:
            ok, gen_unknown = _passes_strict(req, l)
            if not ok:
                continue
            seen = s.execute(
                select(ListingSeen).where(
                    ListingSeen.watch_request_id == req.id,
                    ListingSeen.source_site == l.source_site,
                    ListingSeen.external_id == l.external_id,
                )
            ).scalar_one_or_none()

            if seen is None:
                reg_ok, reg_unknown = _reg_country_matches(req, l.country)
                extra_ok = _passes_extra(req, l)
                extracted = features.extract(f"{l.title} {l.description or ''}")
                # записываем даже не прошедшие фильтр учета, чтобы не
                # перекачивать детали при каждой проверке
                s.add(ListingSeen(
                    watch_request_id=req.id,
                    source_site=l.source_site,
                    external_id=l.external_id,
                    url=l.url,
                    title=l.title,
                    price_at_found=l.price_usd,
                    last_price_seen=l.price_usd,
                    description=l.description,
                    photos=l.photos,
                    extracted_features=extracted,
                    country=l.country,
                ))
                if not reg_ok or not extra_ok:
                    continue
                l.extra["features"] = extracted
                if reg_unknown:
                    l.extra["reg_unknown"] = True
                results.append(MatchResult(l, req, generation_unknown=gen_unknown))
            elif (
                l.price_usd is not None
                and seen.last_price_seen is not None
                and l.price_usd < seen.last_price_seen
                # «8$» не запоминаем: иначе после исправления цены продавцом
                # настоящее снижение уже не заметим
                and not market.suspicious_reason(l.price_usd)
            ):
                old = seen.last_price_seen
                seen.last_price_seen = l.price_usd
                seen.last_seen_at = datetime.utcnow()
                results.append(MatchResult(
                    l, req, is_price_drop=True, old_price=old,
                    generation_unknown=gen_unknown,
                ))
            else:
                seen.last_seen_at = datetime.utcnow()
        s.commit()
    return await _ai_filter(req, results)


async def _ai_filter(req: WatchRequest, results: list[MatchResult]) -> list[MatchResult]:
    """ИИ дочитывает описания находок: неподходящие (битая, на запчасти, цена
    за взнос, чужой учёт при фильтре по учёту) не присылаем, остальным —
    сводка «🤖 Из описания». ListingSeen уже записан — повторно не всплывут."""
    if not results:
        return results
    rows = ai_reader.rows_for([(m.listing.source_site, m.listing.external_id) for m in results])
    await ai_reader.analyze(list(rows.values()))
    kept = []
    for m in results:
        row = rows.get((m.listing.source_site, m.listing.external_id))
        if sellers.is_banned(row) or sellers.is_banned(m.listing):
            log.info("запрос %s: пропускаю %s — продавец в чёрном списке", req.id, m.listing.url)
            continue
        if row is not None:
            problem = ai_reader.request_problem(row, req.reg_country)
            if problem:
                log.info("запрос %s: пропускаю %s — %s", req.id, m.listing.url, problem)
                continue
            summary = ai_reader.summary_of(row)
            if summary:
                m.listing.extra["ai_summary"] = summary
            try:  # оценка к рынку, подозрительная цена и признаки для торга
                m.listing.extra["price_check"] = market.price_check(row)
                m.listing.extra["market_notes"] = market.notes_for(row)
            except Exception:
                log.exception("market notes для %s", m.listing.url)
            if m.is_price_drop and m.listing.extra.get("price_check"):
                # «15 000$ → 8$» — не снижение, а опечатка продавца
                log.info("запрос %s: пропускаю «снижение» до подозрительной цены %s",
                         req.id, m.listing.url)
                continue
        kept.append(m)
    return kept


async def check_all_active() -> list[MatchResult]:
    with get_session() as s:
        requests = list(
            s.execute(
                select(WatchRequest).where(WatchRequest.is_active.is_(True))
            ).scalars()
        )
    results: list[MatchResult] = []
    for req in requests:
        results.extend(await check_request(req))
    return results
