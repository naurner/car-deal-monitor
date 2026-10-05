"""Рыночная аналитика по архиву объявлений.

- fair_price / deal_line: «на 18% ниже рынка» — медиана похожих машин
  (модель + поколение + год; мало данных — соседние годы);
- liquidity: за сколько дней модель уходит с продажи;
- history_lines: признаки для торга — цену снижали, машину перевыставляли,
  висит дольше обычного;
- weekly_report / liquidity_report: сводки для раздела «Рынок» и
  еженедельной рассылки.

Цифры приблизительные: «пропало с площадки» ≠ «продано», а одну и ту же
машину часто снимают и выставляют заново — такие объявления склеиваем по
подписи описания (desc_sig): марка + модель + год + текст.
"""

import hashlib
import logging
import re
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy import func, select, text, update

from db.models import ArchivedListing
from db.session import get_session

log = logging.getLogger(__name__)

FRESH_DAYS = 7          # «сейчас в продаже» = активно и видели за неделю
MIN_SAMPLE = 5          # меньше похожих — оценку не показываем
NEUTRAL_PCT = 3         # в пределах ±3% — «по рынку»
OUTLIER_LOW, OUTLIER_HIGH = 0.4, 2.5  # цены в сомах, «цена за взнос» и т.п.
LIQ_WINDOW_DAYS = 180   # ликвидность — по снятым за полгода
LIQ_MIN_SAMPLE = 5
# момент снятия известен, только если пропажу заметили вскоре после последнего
# появления: иначе объявление могло пропасть когда угодно в этом промежутке
LIQ_MAX_GAP = timedelta(days=2)
CACHE_SEC = 600
BACKFILL_KEY = "backfill_market_v1"

_SIG_CLEAN = re.compile(r"[^0-9a-zа-яё]")
KOREA_RE = re.compile(r"коре[яиюйе]|korea")
IN_TRANSIT = {"в пути", "на заказ"}


def fmt_price(p: float | None) -> str:
    return f"{int(p):,}".replace(",", " ") + "$" if p is not None else "?"


# ---------- подозрительные цены ----------
# Полный рынок lalafo — это и «8$», и «320 сом»: продавцы пишут цену в
# тысячах, ставят 1 ради показа, путают валюту. В топы, средние и оценки
# такие не попадают; в уведомлениях цена помечается и дублируется в сомах.

MIN_CAR_PRICE_USD = 400  # дешевле машин не бывает
SUSPICIOUS_LOW, SUSPICIOUS_HIGH = OUTLIER_LOW, OUTLIER_HIGH


def fmt_kgs(v: float | None) -> str:
    return f"{int(v):,}".replace(",", " ") + " сом" if v is not None else "?"


def kgs_price(price_usd: float | None, price_kgs: float | None = None) -> float | None:
    """Цена в сомах: как указана на площадке, иначе по курсу НБКР."""
    if price_kgs is not None:
        return price_kgs
    if price_usd is None:
        return None
    from config import KGS_PER_USD
    from matching import currency

    return price_usd * (currency._cache.get("rate") or KGS_PER_USD)


def suspicious_reason(price: float | None, median: float | None = None) -> str | None:
    if price is None:
        return None
    if price < MIN_CAR_PRICE_USD:
        return "слишком низкая для машины"
    if median and price < SUSPICIOUS_LOW * median:
        return f"в {median / price:.0f} раз ниже рынка" if price else "ниже рынка"
    if median and price > SUSPICIOUS_HIGH * median:
        return f"в {price / median:.0f} раз выше рынка"
    return None


def split_suspicious(rows: list) -> tuple[list, list]:
    """(нормальные, подозрительные) — для объявлений одной модели. Каждое
    сравниваем с медианой той же модели ±2 года (2000-й за 3 000$ рядом с
    2020-ми за 15 000$ — не подозрительный), год неизвестен — со всеми."""
    by_year: dict[int | None, list[float]] = defaultdict(list)
    for r in rows:
        if r.price_usd and r.price_usd >= MIN_CAR_PRICE_USD:
            by_year[r.year].append(r.price_usd)
    everything = [p for ps in by_year.values() for p in ps]
    medians: dict[int | None, float | None] = {}

    def median_near(year: int | None) -> float | None:
        if year not in medians:
            near = everything if year is None else [
                p for y, ps in by_year.items() if y and abs(y - year) <= 2 for p in ps]
            medians[year] = statistics.median(near) if len(near) >= MIN_SAMPLE else None
        return medians[year]

    ok, bad = [], []
    for r in rows:
        (bad if suspicious_reason(r.price_usd, median_near(r.year)) else ok).append(r)
    return ok, bad


def price_check(row) -> str | None:
    """Почему цена объявления подозрительна (None — нормальная). Сравнение
    с похожими машинами того же года, без них — только нижняя граница."""
    fair = fair_price(row.brand, row.model, row.generation_code, row.year,
                      exclude_id=getattr(row, "id", None),
                      exclude_sig=getattr(row, "desc_sig", None))
    return suspicious_reason(row.price_usd, fair.price if fair else None)


def price_text(price_usd: float | None, price_kgs: float | None = None,
               reason: str | None = None) -> str:
    """«15 000$», а подозрительная — «8$ (700 сом) ⚠️ цена подозрительная»."""
    if not reason:
        return fmt_price(price_usd)
    return (f"{fmt_price(price_usd)} ({fmt_kgs(kgs_price(price_usd, price_kgs))}) "
            f"⚠️ цена подозрительная: {reason}")


# ---------- подпись описания и перевыставления ----------

def desc_sig(brand: str | None, model: str | None, year: int | None,
             description: str | None) -> str | None:
    """Одинаковая у объявлений одной машины, выставленной заново."""
    body = _SIG_CLEAN.sub("", (description or "").lower())
    if not brand or len(body) < 60:
        return None
    return hashlib.sha1(f"{brand}|{model}|{year}|{body[:500]}".encode()).hexdigest()[:16]


def relist_root(s, site: str, sig: str | None, self_id: int | None = None) -> int | None:
    """Самое раннее объявление той же машины на той же площадке."""
    if not sig:
        return None
    q = select(func.min(ArchivedListing.id)).where(
        ArchivedListing.source_site == site, ArchivedListing.desc_sig == sig)
    if self_id is not None:
        q = q.where(ArchivedListing.id < self_id)
    return s.execute(q).scalar()


def backfill_history(batch: int = 2000) -> int:
    """Подписи описаний и связи «перевыставлено» у уже накопленного архива."""
    done, last_id = 0, 0
    while True:
        with get_session() as s:
            rows = s.execute(
                select(ArchivedListing.id, ArchivedListing.brand, ArchivedListing.model,
                       ArchivedListing.year, ArchivedListing.description)
                .where(ArchivedListing.desc_sig.is_(None), ArchivedListing.id > last_id)
                .order_by(ArchivedListing.id).limit(batch)
            ).all()
            if not rows:
                break
            for rid, brand, model, year, desc in rows:
                sig = desc_sig(brand, model, year, desc)
                if sig:
                    s.execute(update(ArchivedListing).where(ArchivedListing.id == rid)
                              .values(desc_sig=sig))
                    done += 1
            last_id = rows[-1][0]
            s.commit()
    with get_session() as s:
        s.execute(text(
            "UPDATE listings_archive SET relist_of = ("
            " SELECT MIN(b.id) FROM listings_archive b"
            " WHERE b.source_site = listings_archive.source_site"
            " AND b.desc_sig = listings_archive.desc_sig"
            " AND b.id < listings_archive.id)"
            " WHERE desc_sig IS NOT NULL AND relist_of IS NULL"
        ))
        s.commit()
    log.info("Рынок: подписи описаний у %d объявлений архива", done)
    return done


# ---------- справедливая цена ----------

class _Lite(NamedTuple):
    id: int
    gen: str | None
    year: int | None
    price: float
    sig: str | None


@dataclass
class Fair:
    price: float
    n: int
    scope: str


def _local_ok(r: ArchivedListing) -> bool:
    from matching import ai_reader, top_tracker  # top_tracker тянет archive

    return (r.price_usd is not None and r.price_usd >= MIN_CAR_PRICE_USD
            and top_tracker.is_local_market(r) and ai_reader.market_ok(r))


def _dedup(rows) -> list[_Lite]:
    """Одна машина — одна цена: дубли и перевыставления не должны перевешивать."""
    seen, out = set(), []
    for r in rows:
        key = r.desc_sig or (r.year, round(r.price_usd), (r.title or "").lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(_Lite(r.id, r.generation_code, r.year, r.price_usd, r.desc_sig))
    return out


_model_cache: dict[tuple[str, str], tuple[float, list[_Lite]]] = {}


def _model_rows(brand: str, model: str) -> list[_Lite]:
    hit = _model_cache.get((brand, model))
    if hit and time.time() - hit[0] < CACHE_SEC:
        return hit[1]
    since = datetime.utcnow() - timedelta(days=FRESH_DAYS)
    with get_session() as s:
        rows = list(s.execute(
            select(ArchivedListing).where(
                ArchivedListing.brand == brand, ArchivedListing.model == model,
                ArchivedListing.is_active.is_(True), ArchivedListing.price_usd.isnot(None),
                ArchivedListing.last_seen_at >= since,
            ).order_by(ArchivedListing.last_seen_at.desc())
        ).scalars())
    lites = _dedup(r for r in rows if _local_ok(r))
    _model_cache[(brand, model)] = (time.time(), lites)
    return lites


def _robust_median(prices: list[float]) -> tuple[float, int] | None:
    if len(prices) < MIN_SAMPLE:
        return None
    m = statistics.median(prices)
    kept = [p for p in prices if OUTLIER_LOW * m <= p <= OUTLIER_HIGH * m]
    if len(kept) < MIN_SAMPLE:
        return None
    return statistics.median(kept), len(kept)


def _fair_from(rows: list[_Lite], gen: str | None, year: int,
               exclude_id: int | None, exclude_sig: str | None) -> Fair | None:
    rows = [r for r in rows if r.year and r.id != exclude_id
            and not (exclude_sig and r.sig == exclude_sig)]
    tries = []
    if gen:
        tries.append((lambda r: r.gen == gen and r.year == year, f"{year} г."))
    tries.append((lambda r: r.year == year, f"{year} г."))
    if gen:
        tries.append((lambda r: r.gen == gen and abs(r.year - year) <= 1, f"{year - 1}–{year + 1} гг."))
    tries.append((lambda r: abs(r.year - year) <= 1, f"{year - 1}–{year + 1} гг."))
    for pred, scope in tries:
        got = _robust_median([r.price for r in rows if pred(r)])
        if got:
            return Fair(got[0], got[1], scope)
    return None


def fair_price(brand: str | None, model: str | None, gen: str | None, year: int | None,
               exclude_id: int | None = None, exclude_sig: str | None = None) -> Fair | None:
    if not (brand and model and year):
        return None
    return _fair_from(_model_rows(brand, model), gen, year, exclude_id, exclude_sig)


def deal_pct(price: float | None, fair: Fair | None) -> float | None:
    if not price or not fair:
        return None
    return (price - fair.price) / fair.price * 100


def deal_line(price: float | None, fair: Fair | None) -> str | None:
    pct = deal_pct(price, fair)
    if pct is None:
        return None
    ref = f"похожие ~{fmt_price(fair.price)}: {fair.n} объявл., {fair.scope}"
    if pct <= -NEUTRAL_PCT:
        return f"💰 На {abs(pct):.0f}% ниже рынка ({ref})"
    if pct >= NEUTRAL_PCT:
        return f"💰 На {pct:.0f}% выше рынка ({ref})"
    return f"💰 По рынку ({ref})"


# ---------- ликвидность ----------

@dataclass
class Liq:
    days: int
    n: int


_liq_cache: tuple[float, dict[tuple[str, str], Liq]] = (0.0, {})


def _site_starts(s) -> dict[str, datetime]:
    return dict(s.execute(
        select(ArchivedListing.source_site, func.min(ArchivedListing.first_seen_at))
        .group_by(ArchivedListing.source_site)
    ).all())


def _start_of(posted, first_seen, site_start) -> datetime | None:
    """Когда машину выставили. Объявления, найденные первым сканом без даты
    публикации (mashina), начало продаж не знаем — не учитываем."""
    if posted:
        return min(posted, first_seen) if first_seen else posted
    if first_seen and site_start and first_seen > site_start + timedelta(days=2):
        return first_seen
    return None


def liquidity_all() -> dict[tuple[str, str], Liq]:
    """Медиана дней от публикации до снятия, по моделям (кэш на час)."""
    global _liq_cache
    if time.time() - _liq_cache[0] < CACHE_SEC * 6:
        return _liq_cache[1]
    since = datetime.utcnow() - timedelta(days=LIQ_WINDOW_DAYS)
    cols = (ArchivedListing.id, ArchivedListing.source_site, ArchivedListing.brand,
            ArchivedListing.model, ArchivedListing.desc_sig, ArchivedListing.posted_at,
            ArchivedListing.first_seen_at, ArchivedListing.last_seen_at,
            ArchivedListing.sold_at, ArchivedListing.is_active,
            ArchivedListing.country, ArchivedListing.availability, ArchivedListing.city,
            ArchivedListing.seller_id)
    with get_session() as s:
        starts = _site_starts(s)
        rows = s.execute(select(*cols).where(
            ArchivedListing.brand.isnot(None), ArchivedListing.model.isnot(None),
            ArchivedListing.price_usd.isnot(None),
        )).all()
    from matching import top_tracker

    cars: dict[tuple, list] = defaultdict(list)
    for r in rows:
        if not top_tracker.is_local_market(r):
            continue
        cars[(r.source_site, r.desc_sig) if r.desc_sig else ("id", r.id)].append(r)
    days_by_model: dict[tuple[str, str], list[int]] = defaultdict(list)
    for group in cars.values():
        if any(r.is_active for r in group) or any(r.sold_at is None for r in group):
            continue
        last = max(group, key=lambda r: r.last_seen_at)
        gap = last.sold_at - last.last_seen_at
        if gap > LIQ_MAX_GAP or last.last_seen_at < since:
            continue
        end = last.last_seen_at + gap / 2
        begins = [b for r in group if (b := _start_of(r.posted_at, r.first_seen_at,
                                                      starts.get(r.source_site)))]
        if not begins:
            continue
        days = (end - min(begins)).days
        if days >= 0:
            days_by_model[(group[0].brand, group[0].model)].append(days)
    result = {k: Liq(int(statistics.median(v)), len(v))
              for k, v in days_by_model.items() if len(v) >= LIQ_MIN_SAMPLE}
    _liq_cache = (time.time(), result)
    return result


def liquidity(brand: str | None, model: str | None) -> Liq | None:
    return liquidity_all().get((brand, model)) if brand and model else None


def liquidity_line(brand: str | None, model: str | None) -> str | None:
    liq = liquidity(brand, model)
    if not liq:
        return None
    return f"💧 {brand} {model} обычно уходит с продажи за ~{liq.days} дн. (по {liq.n} снятым)"


# ---------- история объявления: признаки для торга ----------

def history_lines(row: ArchivedListing) -> list[str]:
    lines: list[str] = []
    with get_session() as s:
        root = s.get(ArchivedListing, row.relist_of) if row.relist_of else None
        starts = _site_starts(s)
    first = root or row
    start = _start_of(first.posted_at, first.first_seen_at, starts.get(first.source_site))
    first_price = first.first_price_usd or first.price_usd
    now = datetime.utcnow()
    if root:
        when = f" {start:%d.%m.%Y}" if start else ""
        lines.append(f"🔁 Перевыставлено: впервые выставляли{when} за {fmt_price(first_price)}")
    if first_price and row.price_usd and first_price > row.price_usd * 1.01:
        pct = (first_price - row.price_usd) / first_price * 100
        lines.append(f"📉 Цену уже снижали: {fmt_price(first_price)} → "
                     f"{fmt_price(row.price_usd)} (−{pct:.0f}%)")
    if start:
        days_on = (now - start).days
        liq = liquidity(row.brand, row.model)
        if liq and days_on > max(liq.days * 1.5, liq.days + 14):
            lines.append(f"⏳ В продаже {days_on} дн. — дольше обычного "
                         f"(такие уходят за ~{liq.days} дн.)")
        elif not liq and days_on >= 45:
            lines.append(f"⏳ В продаже {days_on} дн.")
    if any(l.startswith(("📉", "⏳")) for l in lines):
        lines.append("🤝 Похоже, продавец готов торговаться")
    return lines


def row_for(site: str, external_id: str) -> ArchivedListing | None:
    with get_session() as s:
        return s.execute(select(ArchivedListing).where(
            ArchivedListing.source_site == site,
            ArchivedListing.external_id == external_id,
        )).scalar_one_or_none()


def notes_for(row: ArchivedListing | None) -> list[str]:
    """Строки «оценка сделки + история» для карточки объявления."""
    if row is None:
        return []
    out = []
    fair = fair_price(row.brand, row.model, row.generation_code, row.year,
                      exclude_id=row.id, exclude_sig=row.desc_sig)
    if suspicious_reason(row.price_usd, fair.price if fair else None):
        # «на 99% ниже рынка» у цены «8$» — не находка, а опечатка продавца
        if fair:
            out.append(f"💰 Похожие стоят ~{fmt_price(fair.price)} ({fair.n} объявл., {fair.scope})")
        return out
    line = deal_line(row.price_usd, fair)
    if line:
        out.append(line)
    try:
        out += history_lines(row)
    except Exception:
        log.exception("history_lines failed for %s", row.id)
    return out


# ---------- сводки ----------

def _active_market(days: int = FRESH_DAYS) -> list[ArchivedListing]:
    since = datetime.utcnow() - timedelta(days=days)
    with get_session() as s:
        rows = list(s.execute(select(ArchivedListing).where(
            ArchivedListing.is_active.is_(True), ArchivedListing.price_usd.isnot(None),
            ArchivedListing.last_seen_at >= since,
        )).scalars())
    return rows


def _groups(rows: list[ArchivedListing]) -> dict[tuple[str, str], list[_Lite]]:
    by_model: dict[tuple[str, str], list] = defaultdict(list)
    for r in rows:
        if r.brand and r.model and _local_ok(r):
            by_model[(r.brand, r.model)].append(r)
    return {k: _dedup(v) for k, v in by_model.items()}


def best_deals(rows: list[ArchivedListing], groups, limit: int = 10,
               min_pct: float = 12, max_pct: float = 35,
               models: set | None = None, fresh_days: int = 3) -> list[tuple[ArchivedListing, float, Fair]]:
    """Самые дешёвые относительно рынка. Ниже рынка больше чем на max_pct —
    обычно ошибка цены или «битая/взнос», такие не показываем."""
    fresh = datetime.utcnow() - timedelta(days=fresh_days)
    out, seen_car = [], set()
    for r in rows:
        if not (r.brand and r.model and r.year) or r.last_seen_at < fresh:
            continue
        if models is not None and (r.brand, r.model) not in models:
            continue
        if not _local_ok(r):
            continue
        fair = _fair_from(groups.get((r.brand, r.model), []), r.generation_code, r.year,
                          r.id, r.desc_sig)
        pct = deal_pct(r.price_usd, fair)
        if pct is None or not (-max_pct <= pct <= -min_pct) or fair.n < 8:
            continue
        car = r.desc_sig or (r.brand, r.model, r.year, round(r.price_usd))
        if car in seen_car:
            continue
        seen_car.add(car)
        out.append((r, pct, fair))
    # по выгоде в долларах: −40% от 2 000$ — чаще хлам, чем находка
    out.sort(key=lambda t: t[2].price - t[0].price_usd, reverse=True)
    return out[:limit]


def _deal_item(r: ArchivedListing, pct: float, fair: Fair) -> str:
    return (f"• {r.brand} {r.model} {r.year} — {fmt_price(r.price_usd)} "
            f"({pct:.0f}%, дешевле рынка на {fmt_price(fair.price - r.price_usd)})\n  {r.url}")


def _median_change(now_rows, prev_rows) -> float | None:
    """Изменение цены модели с поправкой на состав: сравниваем медианы по
    годам выпуска и усредняем с весом по числу объявлений."""
    by_year_now, by_year_prev = defaultdict(list), defaultdict(list)
    for r in now_rows:
        by_year_now[r.year].append(r.price)
    for r in prev_rows:
        by_year_prev[r.year].append(r.price)
    num = den = 0.0
    for y, now_p in by_year_now.items():
        prev_p = by_year_prev.get(y, [])
        if y is None or len(now_p) < 3 or len(prev_p) < 3:
            continue
        w = min(len(now_p), len(prev_p))
        num += w * (statistics.median(now_p) / statistics.median(prev_p) - 1)
        den += w
    return num / den * 100 if den >= 10 else None


def weekly_report() -> list[str]:
    """Сводка по рынку за неделю — список частей (каждая в пределах сообщения)."""
    now = datetime.utcnow()
    week, week2 = now - timedelta(days=7), now - timedelta(days=14)
    with get_session() as s:
        recent = list(s.execute(select(ArchivedListing).where(
            ArchivedListing.last_seen_at >= week2,
            ArchivedListing.price_usd.isnot(None),
        )).scalars())
    # ушедшие — только с точно известным моментом: первый полный скан
    # находит тысячи давно пропавших, их в недельную статистику не пускаем
    gone = [r for r in recent if not r.is_active and r.sold_at
            and r.sold_at - r.last_seen_at <= LIQ_MAX_GAP]
    gone_week = sum(1 for r in gone if r.last_seen_at >= week)
    gone_prev = len(gone) - gone_week
    active = [r for r in recent if r.is_active and r.last_seen_at >= week]
    local = [r for r in active if _local_ok(r)]
    groups = _groups(active)
    by_site = Counter(r.source_site for r in active)

    parts = [
        f"📊 Рынок за неделю ({week:%d.%m}–{now:%d.%m})\n"
        f"В продаже: {len(active)} объявл. (lalafo {by_site.get('lalafo', 0)}, "
        f"mashina {by_site.get('mashina', 0)}), из них на местном рынке {len(local)}"
    ]
    # дата публикации (lalafo), иначе — когда впервые увидели
    new_week = [r for r in recent if (r.posted_at or r.first_seen_at) >= week]
    new_prev = [r for r in recent if week2 <= (r.posted_at or r.first_seen_at) < week]
    relisted = sum(1 for r in new_week if r.relist_of)
    parts[0] += (f"\nНовых за неделю: {len(new_week)} (неделей раньше {len(new_prev)})"
                 + (f", из них перевыставленных {relisted}" if relisted else "")
                 + f"\nУшло с продажи: {gone_week} (неделей раньше {gone_prev})")

    # цены популярных моделей
    now_lites: dict[tuple, list[_Lite]] = groups
    prev_by_model: dict[tuple, list] = defaultdict(list)
    for r in recent:
        if (r.brand and r.model and r.first_seen_at < week
                and week2 <= r.last_seen_at and _local_ok(r)):
            prev_by_model[(r.brand, r.model)].append(r)
    popular = sorted(now_lites.items(), key=lambda kv: -len(kv[1]))[:15]
    lines = ["📈 Популярные модели — медиана цены, изменение за неделю:"]
    for (brand, model), lites in popular:
        med = _robust_median([l.price for l in lites])
        if not med:
            continue
        change = _median_change(lites, _dedup(prev_by_model.get((brand, model), [])))
        ch = ""
        if change is not None:
            arrow = "▲" if change > 0.5 else "▼" if change < -0.5 else "≈"
            ch = f", {arrow}{abs(change):.1f}%"
        lines.append(f"• {brand} {model} — {fmt_price(med[0])}{ch} ({len(lites)} объявл.)")
    parts.append("\n".join(lines))

    # ввоз
    def transit(rows):
        return [r for r in rows if (r.availability or "").lower() in IN_TRANSIT]

    def korea(rows):
        return [r for r in rows if KOREA_RE.search(
            f"{r.title or ''} {r.description or ''} {r.country or ''} {r.city or ''}".lower())]

    tr_now = transit(active)
    k_week, k_prev = korea(new_week), korea(new_prev)
    lines = ["🚢 Ввоз",
             f"В пути / под заказ сейчас: {len(tr_now)} (новых за неделю "
             f"{len(transit(new_week))}, неделей раньше {len(transit(new_prev))})",
             f"🇰🇷 Новых объявлений с Кореей в тексте/учёте: {len(k_week)} "
             f"(неделей раньше {len(k_prev)})"]
    top_import = Counter(f"{r.brand} {r.model}" for r in tr_now + k_week
                         if r.brand and r.model).most_common(6)
    if top_import:
        lines.append("Чаще всего везут: " + ", ".join(f"{m} ({n})" for m, n in top_import))
    parts.append("\n".join(lines))

    liq = liquidity_all()
    fast = sorted(((k, v) for k, v in liq.items() if v.n >= 8 and k in groups),
                  key=lambda kv: kv[1].days)[:8]
    if fast:
        parts.append("💧 Быстрее всего уходят с продажи:\n" + "\n".join(
            f"• {b} {m} — ~{v.days} дн. (снято {v.n})" for (b, m), v in fast))

    deals = best_deals(active, groups, limit=8)
    if deals:
        parts.append("🔥 Лучшие предложения сейчас (ниже рынка):\n"
                     + "\n".join(_deal_item(*d) for d in deals))
    parts.append("ℹ️ Цены — медианы местного рынка без дублей и перевыставлений; "
                 "«ушло с продажи» — пропало с площадки, не обязательно продано.")
    return parts


def liquidity_report() -> list[str]:
    liq = liquidity_all()
    rows = _active_market()
    groups = _groups(rows)
    ranked = sorted(((k, v) for k, v in liq.items() if v.n >= 8),
                    key=lambda kv: kv[1].days)
    if not ranked:
        return ["💧 Пока мало данных: нужно, чтобы объявления успели появиться и "
                "уйти с продажи хотя бы за несколько недель наблюдений."]
    lines = ["💧 Ликвидность — за сколько дней модель уходит с продажи (медиана):"]
    for (b, m), v in ranked[:25]:
        on_sale = len(groups.get((b, m), []))
        lines.append(f"• {b} {m} — ~{v.days} дн. (снято {v.n}, в продаже {on_sale})")
    parts = ["\n".join(lines)]
    med_days = statistics.median(v.days for _, v in ranked)
    fast = {k for k, v in ranked if v.days <= med_days}
    deals = best_deals(rows, groups, limit=10, min_pct=10, models=fast)
    if deals:
        parts.append(f"⚡ Быстро уходят (≤{int(med_days)} дн.) и сейчас дешевле рынка:\n"
                     + "\n".join(_deal_item(*d) for d in deals))
    else:
        parts.append("⚡ Быстро уходящих моделей заметно ниже рынка сейчас нет.")
    parts.append("ℹ️ «Ушло с продажи» — пропало с площадки; перевыставленные "
                 "объявления склеены с исходными.")
    return parts
