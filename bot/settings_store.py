from sqlalchemy import select

from config import DEFAULT_CHECK_INTERVAL_MIN, DEFAULT_PRICE_TOLERANCE_PCT
from db.models import Setting
from db.session import get_session

DEFAULTS = {
    "check_interval_min": str(DEFAULT_CHECK_INTERVAL_MIN),
    "price_tolerance_pct": str(DEFAULT_PRICE_TOLERANCE_PCT),
    "keyword_window_min": "60",
    # маржа-вычет из цены продажи для автопоиска: "1000" ($) или "10%"
    "sale_margin": "0",
    # интервал полного сканирования рынка (все марки/модели) для архива и топ-трекера цен
    "full_scan_interval_hours": "6",
    # когда редкие задачи в последний раз отработали — чтобы после перезапуска
    # (обновление бота, перезагрузка Мака) догнать пропущенное, а не ждать
    # полный интервал заново
    "last_full_scan_at": "",
    "last_actualize_at": "",
    "last_bumps_at": "",
    "last_weekly_report_at": "",
    # разовые переработки данных (matching/vehicle_ident.py)
    "backfill_brand_model_v1": "",
    "backfill_market_v1": "",
    "tops_rebaseline_v2": "",
    "banned_sellers_default_v1": "",
}


def parse_margin(raw: str) -> tuple[float, bool]:
    """"1000" -> (1000, False=сумма); "10%" -> (10, True=процент)."""
    raw = raw.strip().replace(" ", "")
    if raw.endswith("%"):
        return float(raw[:-1]), True
    return float(raw), False


def apply_margin(sale_price: float, raw: str) -> float:
    """Базовая цена поиска = цена продажи минус маржа."""
    try:
        value, is_pct = parse_margin(raw)
    except ValueError:
        return sale_price
    base = sale_price * (1 - value / 100) if is_pct else sale_price - value
    return max(base, 0)


def get_setting(key: str) -> str:
    with get_session() as s:
        row = s.get(Setting, key)
        return row.value if row else DEFAULTS[key]


def set_setting(key: str, value: str) -> None:
    with get_session() as s:
        row = s.get(Setting, key)
        if row:
            row.value = value
        else:
            s.add(Setting(key=key, value=value))
        s.commit()
