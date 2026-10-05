"""Формирование сообщений бота."""

from datetime import datetime

from db.models import ArchivedListing, ListingSeen, SoldCar, WatchRequest
from matching import ai_reader, market
from matching.matcher import MatchResult

SITE_NAMES = {"mashina": "mashina.kg", "lalafo": "lalafo.kg", "unaa": "unaa.kg"}

PARAM_LABELS = {
    "color": "Цвет", "body_type": "Кузов", "transmission": "Коробка",
    "drive": "Привод", "fuel": "Топливо", "engine_volume": "Объём",
    "mileage_max": "Пробег до", "equipment": "Комплектация", "interior": "Салон",
}


def fmt_param(key: str, val) -> str:
    label = PARAM_LABELS.get(key, key)
    if isinstance(val, dict):
        vals = ", ".join(str(v) for v in val.get("values", []))
        if val.get("mode") == "exclude":
            return f"{label}: любой, КРОМЕ {vals}"
        return f"{label}: {vals}"
    return f"{label}: {val}"


def fmt_price(p: float | None) -> str:
    return f"{int(p):,}".replace(",", " ") + "$" if p is not None else "?"


def request_summary(req: WatchRequest) -> str:
    parts = [f"🚗 {req.brand} {req.model}"]
    gens = getattr(req, "generations", None) or []
    if len(gens) > 1:
        names = ", ".join(g.get("name", g.get("code", "")) for g in gens)
        parts.append(f"Поколения ({len(gens)}): {names}")
    elif req.generation_name:
        parts.append(f"Поколение: {req.generation_name}")
    if req.year_from or req.year_to:
        parts.append(f"Годы: {req.year_from or '…'}–{req.year_to or 'н.в.'}")
    parts.append(f"💰 {fmt_price(req.price_min)} – {fmt_price(req.price_max)}")
    if req.reg_country:
        parts.append(f"📋 Учет/нахождение: {req.reg_country} (строгий фильтр)")
    if req.country:
        parts.append(f"🌍 Страна: {req.country}")
    if req.label:
        parts.append(f"🏷 {req.label}")
    for key, val in (req.extra_params or {}).items():
        parts.append(fmt_param(key, val))
    src = "создан из продажи" if req.source_type == "from_sale" else "ручной поиск"
    if req.created_at:  # None до сохранения в БД (предпросмотр в мастере)
        age = (datetime.utcnow() - req.created_at).days
        parts.append(f"🗓 Создан: {req.created_at:%d.%m.%Y} ({age} дн. назад), {src}")
        parts.append("▶️ Активен" if req.is_active else "⏸ На паузе")
    return "\n".join(parts)


def sold_summary(car: SoldCar) -> str:
    parts = [f"🚗 {car.brand} {car.model}"]
    if car.generation_name:
        parts.append(f"Поколение: {car.generation_name}")
    if car.year:
        parts.append(f"Год: {car.year}")
    margin_str = ""
    if car.margin and car.margin not in ("0", "0%"):
        disp = car.margin if car.margin.endswith("%") else f"{car.margin}$"
        margin_str = f", маржа −{disp}"
    parts.append(
        f"💵 Продано за: {fmt_price(car.sale_price)} "
        f"(допуск ±{car.price_tolerance_pct:g}%{margin_str})"
    )
    parts.append(f"🌍 Страна: {car.country}")
    if car.label:
        parts.append(f"🏷 {car.label}")
    if car.note:
        parts.append(f"📝 {car.note}")
    if car.created_at:  # None до сохранения в БД (предпросмотр в мастере)
        parts.append(f"🗓 {car.created_at:%d.%m.%Y}")
    return "\n".join(parts)


def match_notification(m: MatchResult) -> str:
    req = m.watch_request
    l = m.listing
    lines = []
    if m.is_price_drop:
        lines.append(f"📉 Цена снижена: {fmt_price(m.old_price)} → {fmt_price(l.price_usd)}")
    else:
        lines.append("🔔 Найдено похожее авто")
    title = l.title
    if req.label:
        title += f" 🏷 {req.label}"
    lines.append(title)
    price_line = (
        f"💰 {market.price_text(l.price_usd, l.extra.get('price_kgs'), l.extra.get('price_check'))}"
        f" (диапазон: {fmt_price(req.price_min)} – {fmt_price(req.price_max)})"
    )
    if l.extra.get("converted_from_kgs"):
        price_line += f"\n💱 Цена была в сомах, курс {l.extra['converted_from_kgs']:.2f} сом/$"
    lines.append(price_line)
    lines += l.extra.get("market_notes") or []
    site = SITE_NAMES.get(l.source_site, l.source_site)
    loc = f"📍 {site}"
    if l.country:
        loc += f" | 📋 Учет: {l.country}"
    city = l.extra.get("city") or (l.extra.get("characteristics") or {}).get("Город")
    if city:
        loc += f" | 🏙 {city}"
    lines.append(loc)
    feats = [k for k in (l.extra.get("features") or {})]
    if feats:
        lines.append("✅ " + " | ".join(feats))
    subtitle = l.extra.get("subtitle")
    if subtitle:
        lines.append(subtitle)
    if l.extra.get("ai_summary"):
        lines.append(f"🤖 Из описания: {l.extra['ai_summary']}")
    if m.generation_unknown:
        lines.append("⚠️ Поколение не определено — проверьте вручную")
    if l.extra.get("reg_unknown"):
        lines.append("⚠️ Страна учета не указана в объявлении — проверьте вручную")
    age = (datetime.utcnow() - req.created_at).days
    lines.append(f"🗓 Запрос создан: {req.created_at:%d.%m.%Y} ({age} дн. назад)")
    lines.append(l.url)
    return "\n".join(lines)


PART_FIELDS = (
    ("part_type", "Что"), ("state", "Состояние"), ("diameter", "Диаметр R"),
    ("width", "Ширина"), ("profile", "Профиль"), ("season", "Сезон"),
    ("rim_type", "Вид диска"), ("bolts", "Отверстий"), ("quantity", "Количество"),
    ("brand", "Бренд"),
)
CAR_FIELDS = (
    ("year", "Год"), ("mileage", "Пробег, км"), ("engine_volume", "Объём"),
    ("fuel", "Топливо"), ("transmission", "Коробка"), ("drive", "Привод"),
    ("body_type", "Кузов"), ("color", "Цвет"), ("steering", "Руль"),
    ("condition", "Состояние"), ("reg_country", "Учёт"),
)


def posted_ad_preview(ad) -> str:
    d = ad.car_data or {}
    if d.get("kind") == "part":
        head, fields = d.get("title") or d.get("part_type"), PART_FIELDS
    else:
        head, fields = f"{d.get('brand', '')} {d.get('model', '')}".strip(), CAR_FIELDS
    lines = [f"🏷 {head} — {fmt_price(ad.price_usd)}"]
    lines += [f"{label}: {d[k]}" for k, label in fields if d.get(k) not in (None, "")]
    if ad.description:
        lines.append(f"\n{ad.description[:600]}")
    return "\n".join(lines)


def batch_listings(items, formatter, sep: str = "\n\n➖➖➖\n\n", limit: int = 3500) -> list[str]:
    """Склеивает отформатированные элементы в минимум сообщений вместо
    одного сообщения на элемент (лимит Telegram ~4096 символов)."""
    chunks: list[str] = []
    current = ""
    for item in items:
        piece = formatter(item)
        candidate = piece if not current else current + sep + piece
        if len(candidate) > limit and current:
            chunks.append(current)
            current = piece
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def listing_short(l: ListingSeen) -> str:
    price = fmt_price(l.last_price_seen)
    return f"• {l.title} — {price}\n  {l.url}"


def archived_listing(r: ArchivedListing) -> str:
    site = SITE_NAMES.get(r.source_site, r.source_site)
    if r.is_active:
        status = "🟢 активно"
    else:
        sold = f" ~{r.sold_at:%d.%m.%Y}" if r.sold_at else ""
        status = f"🔴 продано/снято{sold}"
    lines = [
        f"{r.title} — {_row_price(r)}",
        f"📍 {site} | {status}",
    ]
    if r.city:
        lines.append(f"🏙 {r.city}")
    posted = r.posted_at or r.first_seen_at
    lines.append(f"🗓 Опубликовано: {posted:%d.%m.%Y} | Видели: {r.last_seen_at:%d.%m.%Y}")
    summary = ai_reader.summary_of(r)
    if summary:
        lines.append(f"🤖 {summary}")
    if r.is_active:
        lines += _market_notes(r)
    lines.append(r.url)
    return "\n".join(lines)


def _row_price(r: ArchivedListing) -> str:
    """Цена; подозрительная («8$») — с сомами в скобках и пометкой."""
    try:
        reason = market.price_check(r)
    except Exception:
        reason = market.suspicious_reason(r.price_usd)
    return market.price_text(r.price_usd, getattr(r, "price_kgs", None), reason)


def _market_notes(r: ArchivedListing) -> list[str]:
    try:
        return market.notes_for(r)
    except Exception:
        return []


def keyword_match(kw: str, r: ArchivedListing) -> str:
    site = SITE_NAMES.get(r.source_site, r.source_site)
    lines = [
        f"🔑 Совпадение по слову «{kw}»",
        f"{r.title} — {_row_price(r)}",
        f"📍 {site}" + (f" | 🏙 {r.city}" if r.city else ""),
    ]
    if r.description:
        lines.append(r.description[:200])
    lines.append(r.url)
    return "\n".join(lines)


def _model_title(brand: str, model: str, generation_name: str | None) -> str:
    return f"{brand} {model}" + (f" {generation_name}" if generation_name else "")


def top_event(event) -> str:
    """event: matching.top_tracker.TopEvent."""
    title = _model_title(event.brand, event.model, event.generation_name)

    if event.kind == "anomaly":
        l = event.suspicious_listing
        site = SITE_NAMES.get(l.source_site, l.source_site)
        return (
            f"⚠️ ПОДОЗРИТЕЛЬНАЯ ЦЕНА — {title}\n"
            f"{l.title} — {fmt_price(l.price_usd)} (было топ-1: {fmt_price(event.prev_top1_price)})\n"
            f"Цена ниже прошлой минимальной больше чем на 50% — похоже на "
            f"ошибку (например сомы вместо долларов). Топ НЕ обновлён, "
            f"проверьте объявление вручную:\n"
            f"📍 {site}\n{l.url}"
        )

    # new_top
    lines = [f"📉 Новый минимум — {title}"]
    for l in event.entered:
        site = SITE_NAMES.get(l.source_site, l.source_site)
        summary = ai_reader.summary_of(l)
        ai_line = f"\n🤖 {summary}" if summary else ""
        notes = "".join(f"\n{n}" for n in _market_notes(l))
        lines.append(f"\n🆕 {l.title} — {fmt_price(l.price_usd)}\n📍 {site}{ai_line}{notes}\n{l.url}")
    lines.append(f"\n💰 Средняя цена по рынку: {fmt_price(event.avg_price)}")
    if event.prev_avg is not None:
        lines.append(f"   (было: {fmt_price(event.prev_avg)})")
    if event.prev_top:
        lines.append("\n📊 Прошлый топ-3:")
        for i, e in enumerate(event.prev_top, 1):
            lines.append(f"  {i}. {e['title']} — {fmt_price(e['price'])}")
    return "\n".join(lines)
