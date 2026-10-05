"""Парсер lalafo.kg через их публичный JSON API. Детали: docs/PARSING_NOTES.md.

Поколений в данных нет — generation_code остаётся None, matcher фильтрует
по году через справочник поколений.
"""

import asyncio
import json
import logging
import re
from datetime import datetime

import httpx

from config import USER_AGENT
from matching import currency
from parsers.base import RawListing

log = logging.getLogger(__name__)

API = "https://lalafo.kg/api/search/v3/feed/search"
BASE = "https://lalafo.kg"
CATEGORY_CARS = 1502  # «Легковые автомобили с пробегом»
HEADERS = {
    "User-Agent": USER_AGENT,
    "device": "pc",
    "language": "ru_RU",
    "Country-Id": "12",
    "Accept": "application/json",
}
PER_PAGE = 40
MAX_PAGES = 3
REQUEST_DELAY_SEC = 1.0
PAGE_RETRY_PAUSES = (5, 20, 60)
BATCH_PAGES = 50

# "Toyota Camry: 2018 г., 2.5 л, Автомат, Бензин, Седан"
TITLE_YEAR_RE = re.compile(r":\s*(\d{4})\s*г")
YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")


def _price_usd(item: dict, rate: float) -> tuple[float | None, float | None]:
    """(цена в $, курс если конвертировали из сомов)."""
    price, curr = item.get("price"), item.get("currency")
    if price is None:
        return None, None
    if curr == "USD":
        return float(price), None
    if curr == "KGS":
        return round(float(price) / rate), rate
    return None, None


def _price_kgs(item: dict) -> float | None:
    """Цена в сомах как на площадке: при фильтре по цене в USD lalafo
    пересчитывает price в доллары, а сомы кладёт в national_price."""
    if item.get("currency") == "KGS" and item.get("price") is not None:
        return float(item["price"])
    nat = item.get("national_price") or {}
    if (nat.get("currency") or {}).get("code") == "KGS" and nat.get("price") is not None:
        return float(nat["price"])
    return None


def _year(item: dict) -> int | None:
    title = item.get("title") or ""
    m = TITLE_YEAR_RE.search(title) or YEAR_RE.search(title)
    return int(m.group(1)) if m else None


_TRANSMISSIONS = {"автомат", "механика", "вариатор", "робот"}
_FUELS = {"бензин", "дизель", "газ", "гибрид", "электро"}


def _title_specs(title: str) -> dict:
    """"...: 2018 г., 2.5 л, Автомат, Бензин, Седан" -> кпп/топливо/кузов."""
    specs: dict = {}
    if ":" not in title:
        return specs
    for seg in title.split(":", 1)[1].split(","):
        seg = seg.strip().lower()
        if seg in _TRANSMISSIONS:
            specs["transmission"] = seg
        elif seg in _FUELS:
            specs["fuel"] = seg
        elif seg.endswith(" л") or seg.endswith("л."):
            specs["engine_volume"] = seg.rstrip(" л.")
        elif seg and not seg.endswith("г.") and not seg.endswith("г"):
            specs.setdefault("body_type", seg)
    return specs


# Параметры объявления приходят прямо в выдаче поиска: tracking_info →
# {"name": "params", "value": "{\"<param_id>\": <value_id>, ...}"}.
# Расшифровка — через /api/search/v3/feed/details/<id> (проверено 2026-09).
PARAM_REG_COUNTRY = "5151"  # «Страна учета»
PARAM_CUSTOMS = "1157"      # «Растаможка»
REG_COUNTRIES = {
    74096: "Кыргызстан", 74102: "Казахстан", 74093: "Россия", 74097: "Грузия",
    74099: "США", 74100: "Южная Корея", 74104: "Китай", 74107: "Германия",
    70425: "Япония",
}
CUSTOMS_NOT_CLEARED = 31948  # «Не растаможен»


# Марка и модель без фильтра по марке (полный скан): категория объявления
# внутри «Продажа авто» — это марка, параметр 49 — код модели. Названия
# моделей по кодам — публичный /catalog/v3/params/filter (по одному запросу
# на марку, кэшируется на время жизни процесса).
TREE = "https://lalafo.kg/api/proxy/catalog/v3/categories/tree"
PARAMS_FILTER = "https://lalafo.kg/api/catalog/v3/params/filter"
PARAM_MODEL = "49"
_brand_by_cat: dict[int, str] = {}
_models_by_cat: dict[int, dict[int, str]] = {}


async def _ensure_brands(client: httpx.AsyncClient) -> None:
    if _brand_by_cat:
        return
    try:
        tree = (await client.get(TREE, headers=HEADERS)).json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("lalafo: дерево категорий недоступно: %s", e)
        return

    def walk(nodes):
        for n in nodes:
            if n.get("id") == CATEGORY_CARS:
                for b in n.get("children") or []:
                    _brand_by_cat[b["id"]] = b["name"]
                return
            walk(n.get("children") or [])
    walk(tree)


async def _ensure_models(client: httpx.AsyncClient, cat_id: int) -> None:
    if cat_id in _models_by_cat:
        return
    _models_by_cat[cat_id] = {}
    try:
        resp = await client.get(PARAMS_FILTER, params={"category_id": cat_id},
                                headers={**HEADERS, "X-Cache-Bypass": "yes"})
        params = resp.json()
    except (httpx.HTTPError, ValueError):
        return
    for p in params if isinstance(params, list) else []:
        if str(p.get("id")) == PARAM_MODEL:
            _models_by_cat[cat_id] = {v["id"]: v.get("value") or v.get("name") for v in p.get("values") or []}


def _identify(item: dict) -> tuple[str | None, str | None]:
    from matching import vehicle_ident as vi  # вне модуля: matching импортирует parsers

    cat = item.get("category_id")
    raw_brand = _brand_by_cat.get(cat)
    if not raw_brand:
        return vi.split_title(item.get("title"))
    brand = vi.canonical_brand(raw_brand)
    model = _models_by_cat.get(cat, {}).get(_item_params(item).get(PARAM_MODEL))
    if model and not model.lower().startswith("друг"):  # «Другая модель»
        return brand, vi.canonical_model(brand, model)
    title_brand, title_model = vi.split_title(item.get("title"))
    return brand, title_model if title_brand == brand else None


def _item_params(item: dict) -> dict:
    for t in item.get("tracking_info") or []:
        if t.get("name") == "params":
            try:
                return json.loads(t.get("value") or "{}")
            except ValueError:
                return {}
    return {}


def _posted_at(item: dict) -> datetime | None:
    ts = item.get("created_time") or item.get("created_at")
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.utcfromtimestamp(ts)
    return None


def _relevant(item: dict, brand: str, model: str | None) -> bool:
    """q= на lalafo — полнотекстовый и шумный; проверяем заголовок."""
    hay = (item.get("title") or "").lower()
    if brand.lower() not in hay:
        return False
    if model and model.lower() not in hay:
        return False
    return True


async def search(
    brand: str | None,
    model: str | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    max_pages: int = MAX_PAGES,
    on_batch=None,
    status: dict | None = None,
) -> list[RawListing]:
    """on_batch, status — как в parsers/mashina.py: запись в архив по ходу
    полного скана и признак «дошли до конца выдачи»."""
    params: dict = {
        "category_id": CATEGORY_CARS,
        "expand": "url",
        "per-page": PER_PAGE,
        # по умолчанию выдача — «по релевантности»: порядок плавает между
        # страницами (дубли и пропуски), свежие объявления не обязательно в
        # начале, а без фильтров она вообще охватывает только ~18 тыс. из
        # ~50 тыс. «Новые» — стабильный порядок и точный totalCount
        # (но не больше 10 000 на запрос — см. scan_all)
        "sort_by": "newest",
    }
    if brand:
        params["q"] = f"{brand} {model}" if model else brand
    if price_min is not None:
        params["price[from]"] = int(price_min)
    if price_max is not None:
        params["price[to]"] = int(price_max)
    if price_min is not None or price_max is not None:
        params["currency"] = "USD"

    complete = False
    rate = await currency.usd_rate()
    results: list[RawListing] = []
    flushed = 0
    async with httpx.AsyncClient(timeout=30) as client:
        for page in range(1, max_pages + 1):
            data = None
            for attempt, pause in enumerate(PAGE_RETRY_PAUSES):
                try:
                    resp = await client.get(
                        API, params={**params, "page": page}, headers=HEADERS
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except (httpx.HTTPError, ValueError) as e:
                    log.warning("lalafo fetch failed (page %s, попытка %d): %s", page, attempt + 1, e)
                    await asyncio.sleep(pause)
            if data is None:
                break
            items = data.get("items", [])
            if not items:
                complete = True
                break
            if not brand:
                await _ensure_brands(client)
                for cat in {i.get("category_id") for i in items} & _brand_by_cat.keys():
                    await _ensure_models(client, cat)
            for item in items:
                if brand and not _relevant(item, brand, model):
                    continue
                year = _year(item)
                if year_from and year and year < year_from:
                    continue
                if year_to and year and year > year_to:
                    continue
                results.append(_to_listing(item, brand, model, rate))
            if on_batch and page % BATCH_PAGES == 0:
                on_batch(results[flushed:])
                flushed = len(results)
            meta = data.get("_meta", {})
            if page >= meta.get("pageCount", 1):
                complete = True
                break
            await asyncio.sleep(REQUEST_DELAY_SEC)
    if on_batch and results[flushed:]:
        on_batch(results[flushed:])
    if status is not None:
        status["complete"] = complete

    seen: dict[str, RawListing] = {}
    for r in results:
        seen.setdefault(r.external_id, r)
    return list(seen.values())


FULL_SCAN_BAND_MAX = 9000  # «новые» отдают не больше 10 000 на запрос
FULL_SCAN_EDGES = (0, 2000, 4000, 6000, 8000, 11000, 15000, 20000, 30000, 60000, 10**8)


async def _band_total(client: httpx.AsyncClient, lo: int, hi: int) -> int | None:
    params = {"category_id": CATEGORY_CARS, "per-page": 1, "page": 1, "sort_by": "newest",
              "price[from]": lo, "price[to]": hi, "currency": "USD"}
    for pause in PAGE_RETRY_PAUSES:
        try:
            resp = await client.get(API, params=params, headers=HEADERS)
            resp.raise_for_status()
            return int(resp.json()["_meta"]["totalCount"])
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.warning("lalafo: размер диапазона %s–%s: %s", lo, hi, e)
            await asyncio.sleep(pause)
    return None


async def _bands(client: httpx.AsyncClient) -> list[tuple[int, int]] | None:
    """Ценовые диапазоны, в каждом меньше FULL_SCAN_BAND_MAX объявлений."""
    todo = list(zip(FULL_SCAN_EDGES, [e - 1 for e in FULL_SCAN_EDGES[1:]]))
    bands = []
    while todo:
        lo, hi = todo.pop(0)
        total = await _band_total(client, lo, hi)
        if total is None:
            return None
        if total >= FULL_SCAN_BAND_MAX and hi - lo > 10:
            mid = (lo + hi) // 2
            todo[:0] = [(lo, mid), (mid + 1, hi)]
        else:
            bands.append((lo, hi))
        await asyncio.sleep(0.3)
    return bands


async def scan_all(on_batch=None, status: dict | None = None) -> list[RawListing]:
    """Весь рынок lalafo (с ценой) — по ценовым диапазонам. Объявления без
    цены («договорная») в диапазоны не попадают — для аналитики они и не нужны."""
    status = status if status is not None else {}
    status["complete"] = False
    async with httpx.AsyncClient(timeout=30) as client:
        bands = await _bands(client)
    if bands is None:
        return []
    results: list[RawListing] = []
    complete = True
    for lo, hi in bands:
        band: dict = {}
        results += await search(None, price_min=lo, price_max=hi,
                                max_pages=FULL_SCAN_BAND_MAX // PER_PAGE + 10,
                                on_batch=on_batch, status=band)
        complete = complete and band.get("complete", False)
    status["complete"] = complete
    seen: dict[str, RawListing] = {}
    for r in results:
        seen.setdefault(r.external_id, r)
    log.info("lalafo: весь рынок — %d объявлений в %d диапазонах цен%s", len(seen), len(bands),
             "" if complete else " (не до конца)")
    return list(seen.values())


def _to_listing(item: dict, brand: str | None, model: str | None, rate: float) -> RawListing:
    title = item.get("title") or ""
    price, converted_rate = _price_usd(item, rate)
    extra = {"city": item.get("city"), **_title_specs(title)}
    if converted_rate:
        extra["converted_from_kgs"] = converted_rate
    price_kgs = _price_kgs(item)
    if price_kgs is not None:
        extra["price_kgs"] = price_kgs
    if item.get("user_id"):  # продавец — для чёрного списка (matching/sellers.py)
        extra["seller_id"] = str(item["user_id"])
        extra["seller_name"] = ((item.get("user") or {}).get("username") or "")[:128] or None
    posted = _posted_at(item)
    if posted:
        extra["posted_at"] = posted.isoformat()
    if not brand:
        brand, model = _identify(item)
    params = _item_params(item)
    reg_id = params.get(PARAM_REG_COUNTRY)
    country = REG_COUNTRIES.get(reg_id, "другая страна") if reg_id else None
    if params.get(PARAM_CUSTOMS) == CUSTOMS_NOT_CLEARED:
        extra["availability"] = "Не растаможен"
    return RawListing(
        source_site="lalafo",
        external_id=str(item["id"]),
        url=BASE + item.get("url", f"/ads/{item['id']}"),
        title=title,
        price_usd=price,
        year=_year(item),
        brand=brand,
        model=model,
        generation_code=None,
        description=(item.get("description") or "")[:4000] or None,
        country=country,
        photos=[
            img.get("original_url")
            for img in (item.get("images") or [])[:5]
            if img.get("original_url")
        ],
        extra=extra,
    )


async def fresh(max_pages: int = 2) -> list[RawListing]:
    """Свежие объявления всей категории (для слежения по ключевым словам)."""
    return await search(brand=None, model=None, max_pages=max_pages)
