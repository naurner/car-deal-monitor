"""Парсер mashina.kg. Детали протокола: docs/PARSING_NOTES.md."""

import asyncio
import logging
import re

import httpx
from bs4 import BeautifulSoup

from config import USER_AGENT
from parsers.base import RawListing

log = logging.getLogger(__name__)

BASE = "https://mashina.kg"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"}
REQUEST_DELAY_SEC = 1.5
PAGE_RETRY_PAUSES = (5, 20, 60)  # пауза после 1-й, 2-й, 3-й неудачи страницы
MAX_PAGES = 5
BATCH_PAGES = 50

# "Toyota Camry VIII (XV70) Japan Market 2.5hyb CVT" -> ("VIII", "XV70")
GEN_RE = re.compile(r"\b([IVXLC]+)\s*\(([^)]+)\)")
YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")
PRICE_USD_RE = re.compile(r"\$\s?([\d\s  ]+)")


# Спец-коды поколений для марок без кодов в заголовке (напр. LiXiang):
# различаем дорестайлинг и рестайлинг как отдельные "поколения"
GEN_BASE = "base"
GEN_RESTYLING = "restyling"

# Римская цифра в начале остатка заголовка (после "Марка Модель"),
# напр. "III 2.4 AT" у Honda CR-V — поколение без кода в скобках
GEN_ROMAN_RE = re.compile(r"^([IVXLC]+)\b\s*(Рестайлинг)?")

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def translit(s: str) -> str:
    """ru→lat нижним регистром — для сопоставления заголовков со слагами URL."""
    return "".join(_TRANSLIT.get(ch, ch) for ch in s.lower())


def _strip_prefix(title: str, brand: str | None, model: str | None) -> str | None:
    if not brand or not model:
        return None
    prefix = f"{brand} {model}".lower()
    if title.lower().startswith(prefix):
        return title[len(prefix):].strip()
    return None


def parse_generation(
    title: str, brand: str | None = None, model: str | None = None
) -> tuple[str | None, str | None]:
    """Возвращает (имя, код) поколения из заголовка.

    "Camry VIII (XV70) ..."            -> ("VIII (XV70)", "XV70")
    "Camry VIII (XV70) Рестайлинг ..." -> ("VIII (XV70) Рестайлинг", "XV70-R")
    "Honda CR-V III 2.4 ..."           -> ("III", "III")        # нужен brand+model
    "Honda CR-V I Рестайлинг ..."      -> ("I Рестайлинг", "I-R")
    "LiXiang L7 Рестайлинг ..."        -> ("Рестайлинг", "restyling")
    "LiXiang L7 Max ..."               -> ("База (дорестайлинг)", "base")
    """
    rest = _strip_prefix(title, brand, model)
    haystack = rest if rest is not None else title
    m = GEN_RE.search(haystack)
    if m:
        name, code = f"{m.group(1)} ({m.group(2).strip()})", m.group(2).strip()
        if haystack[m.end():].lstrip().startswith("Рестайлинг"):
            return name + " Рестайлинг", code + "-R"
        return name, code
    if rest is not None:
        m2 = GEN_ROMAN_RE.match(rest)
        if m2:
            code = m2.group(1)
            if m2.group(2):
                return f"{code} Рестайлинг", f"{code}-R"
            return code, code
    if "Рестайлинг" in title:
        return "Рестайлинг", GEN_RESTYLING
    return "База (дорестайлинг)", GEN_BASE


def _slugify(name: str) -> str:
    # В URL mashina.kg марка/модель идут с Заглавной буквы как есть
    return name.strip()


async def _fetch(client: httpx.AsyncClient, url: str, params: dict | None = None) -> str:
    for attempt in (1, 2):
        resp = await client.get(url, params=params, headers=HEADERS, follow_redirects=True)
        if resp.status_code >= 500 and attempt == 1:
            await asyncio.sleep(5)  # транзиентные 503 у площадки
            continue
        resp.raise_for_status()
        return resp.text
    raise AssertionError("unreachable")


def _parse_card(
    card, brand: str | None = None, model: str | None = None,
    known_makes: list[str] | None = None,
) -> RawListing | None:
    """card — <article class="ad-card-item"> (2026-09: сайт сменил обёртку
    карточки с <a> на <article>; сама ссылка теперь вложена внутри).

    known_makes — при сканировании без фильтра по марке (brand=None):
    список марок (sort_makes_by_slug_length) для определения марки/модели
    по каждой карточке индивидуально."""
    link = card.find("a", href=True)
    href = link.get("href", "") if link else ""
    if not href.startswith("/details/"):
        return None
    external_id = href.rstrip("/").rsplit("-", 1)[-1]
    h3 = card.find("h3")
    title = h3.get_text(strip=True) if h3 else ""

    if not brand and known_makes:
        resolved = resolve_brand_model_from_card(href, title, known_makes)
        if resolved:
            brand, model = resolved
    sub = card.find("p")
    subtitle = sub.get_text(" ", strip=True) if sub else ""

    year = None
    ym = YEAR_RE.search(subtitle)
    if ym:
        year = int(ym.group(1))

    price_usd = None
    pm = PRICE_USD_RE.search(card.get_text(" ", strip=True))
    if pm:
        digits = re.sub(r"\D", "", pm.group(1))
        if digits:
            price_usd = float(digits)

    _, gen_code = parse_generation(title, brand, model)
    img = card.find("img")
    photos = [img["src"]] if img and img.get("src", "").startswith("http") else []

    extra = {"subtitle": subtitle, **_subtitle_specs(subtitle)}
    city = _card_city(card)
    if city:
        extra["city"] = city
    availability = _card_availability(card)
    if availability:
        extra["availability"] = availability

    return RawListing(
        source_site="mashina",
        external_id=external_id,
        url=BASE + href,
        title=title,
        price_usd=price_usd,
        year=year,
        brand=brand,
        model=model,
        generation_code=gen_code,
        photos=photos,
        extra=extra,
    )


_TRANSMISSIONS = {"автомат", "механика", "вариатор", "робот"}
MILEAGE_RE = re.compile(r"([\d\s]+)\s*(km|км|миль|miles)", re.I)


def _subtitle_specs(subtitle: str) -> dict:
    """"2019 ‧ 58000 km ‧ 2.5 ‧ автомат ‧ Слева ‧ седан" -> поля.

    2026-09: город переехал в отдельный <span> вне подзаголовка (см.
    _card_city), поэтому здесь его больше не ищем — последний токен теперь
    тип кузова."""
    specs: dict = {}
    tokens = [t.strip() for t in subtitle.split("‧")]
    for t in tokens:
        low = t.lower()
        m = MILEAGE_RE.match(t)
        if m:
            km = int(re.sub(r"\D", "", m.group(1)) or 0)
            if "миль" in low or "miles" in low:
                km = int(km * 1.609)
            specs["mileage_km"] = km
        elif low in _TRANSMISSIONS:
            specs["transmission"] = low
        elif low and not t[0].isdigit() and low not in ("слева", "справа"):
            specs.setdefault("body_type", low)
    return specs


_CITY_RE = re.compile(r"^г\.\s*(.+)")


def _card_city(card) -> str | None:
    """Город теперь в <span class="...truncate">г. Бишкек</span>,
    отдельно от подзаголовка."""
    for span in card.find_all("span"):
        if span.find_all():  # интересуют только "листовые" span без вложенных тегов
            continue
        text = span.get_text(strip=True)
        m = _CITY_RE.match(text)
        if m:
            return m.group(1).strip()
    return None


# Бейдж «Наличие в Кыргызстане» на карточке. У машин за границей в поле
# города при этом стоит страна: «г. Китай», «г. Южная Корея», «г. Европа».
AVAILABILITY_VALUES = ("В наличии", "В пути", "На заказ")


def _card_availability(card) -> str | None:
    for span in card.find_all("span"):
        if span.find_all():
            continue
        text = span.get_text(strip=True)
        if text in AVAILABILITY_VALUES:
            return text
    return None


async def search(
    brand: str | None,
    model: str | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    max_pages: int = MAX_PAGES,
    known_makes: list[str] | None = None,
    on_batch=None,
    status: dict | None = None,
) -> list[RawListing]:
    """known_makes: при brand=None (полное сканирование) — список марок
    (sort_makes_by_slug_length) для определения марки/модели по карточке.
    on_batch(listings): вызывается каждые BATCH_PAGES страниц — полный скан
    пишет в архив по ходу, а не через 2 часа в самом конце (перезапуск
    контейнера посреди скана раньше терял всё).
    status["complete"]: дошёл ли поиск до конца выдачи (а не оборвался на
    ошибке или лимите страниц) — только тогда полный скан вправе считать
    пропавшие объявления снятыми. Не глобальный флаг: автопроверка вызывает
    search() параллельно с полным сканом."""
    params: dict = {}
    if brand:
        params["make"] = _slugify(brand)
    if model:
        params["model"] = _slugify(model)
    if price_min is not None:
        params["price__gte"] = int(price_min)
    if price_max is not None:
        params["price__lte"] = int(price_max)
    if price_min is not None or price_max is not None:
        params["currency"] = "USD"
    if year_from is not None:
        params["year__gte"] = year_from
    if year_to is not None:
        params["year__lte"] = year_to

    complete = False
    results: list[RawListing] = []
    flushed = 0
    async with httpx.AsyncClient(timeout=30) as client:
        for page in range(1, max_pages + 1):
            page_params = dict(params)
            if page > 1:
                page_params["page"] = page
            html = None
            # полный каталог — ~2700 страниц: без повторов один таймаут
            # обрывал весь скан, и до конца он не доходил ни разу
            for attempt, pause in enumerate(PAGE_RETRY_PAUSES):
                try:
                    html = await _fetch(client, f"{BASE}/catalog/passenger", page_params)
                    break
                except httpx.HTTPError as e:
                    log.warning("mashina.kg fetch failed (page %s, попытка %d): %s", page, attempt + 1, e)
                    await asyncio.sleep(pause)
            if html is None:
                break
            soup = BeautifulSoup(html, "html.parser")
            cards = soup.select("article.ad-card-item")
            if not cards:
                complete = True
                break
            for card in cards:
                listing = _parse_card(card, brand, model, known_makes)
                if listing:
                    results.append(listing)
            if on_batch and page % BATCH_PAGES == 0:
                on_batch(results[flushed:])
                flushed = len(results)
            if len(cards) < 20:  # последняя страница
                complete = True
                break
            await asyncio.sleep(REQUEST_DELAY_SEC)
    if on_batch and results[flushed:]:
        on_batch(results[flushed:])
    if status is not None:
        status["complete"] = complete

    # дедуп по external_id (одно объявление может встретиться дважды)
    seen: dict[str, RawListing] = {}
    for r in results:
        seen.setdefault(r.external_id, r)
    return list(seen.values())


_OPTION_VALUE_RE = re.compile(r'\\"value\\":\\"([^"\\]+)\\"')
_OPTION_ATTR_RE = re.compile(r'\\"attribute_id\\":(\d+)')
MAKE_ATTRIBUTE_ID = "16"
# attribute_id -> имя справочника (какие удалось найти в payload страницы)
FILTER_ATTRIBUTES = {"19": "fuel", "21": "transmission", "18": "body_type"}


async def fetch_catalogs() -> dict[str, set[str]]:
    """Справочники опций из RSC-payload страницы поиска.

    Возвращает {"make": {...}, "fuel": {...}, "transmission": {...},
    "body_type": {...}}. Цвета/привод в payload отсутствуют — они
    статические списки в matching/catalog.py.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        html = await _fetch(client, f"{BASE}/catalog/passenger")
    catalogs: dict[str, set[str]] = {}
    # payload — массив объектов-опций; режем по границе объектов
    for fragment in html.split("},{"):
        vm = _OPTION_VALUE_RE.search(fragment)
        am = _OPTION_ATTR_RE.search(fragment)
        if not (vm and am):
            continue
        aid = am.group(1)
        if aid == MAKE_ATTRIBUTE_ID:
            catalogs.setdefault("make", set()).add(vm.group(1))
        elif aid in FILTER_ATTRIBUTES:
            # кузова легковых: category_id=1 (грузовые/спец — другие категории)
            if aid == "18" and not re.search(r'\\"category_id\\":1[,}\\]', fragment):
                continue
            catalogs.setdefault(FILTER_ATTRIBUTES[aid], set()).add(vm.group(1).lower())
    for key in list(catalogs):
        if key != "make":
            catalogs[key] = _dedupe_options(catalogs[key])
    log.info("mashina.kg catalogs: %s", {k: len(v) for k, v in catalogs.items()})
    return catalogs


def normalize_option(value: str) -> str:
    """Каноническая форма значения фильтра для дедупликации и сравнения.

    'внедорожник_5_дв' == 'внедорожник 5 дв.' ; 'бензин / газ' == 'бензин/ газ'
    """
    v = value.lower().replace("_", " ").rstrip(".")
    v = re.sub(r"\s*/\s*", "/", v)
    return re.sub(r"\s+", " ", v).strip()


def _dedupe_options(values: set[str]) -> set[str]:
    by_norm: dict[str, str] = {}
    for v in sorted(values):
        norm = normalize_option(v)
        # предпочитаем вариант с пробелами и без подчёркиваний
        if norm not in by_norm or "_" in by_norm[norm]:
            by_norm[norm] = v
    return set(by_norm.values())


async def fetch_makes() -> list[str]:
    return sorted((await fetch_catalogs()).get("make", set()))


def _url_slug(name: str) -> str:
    """Слаг как в URL mashina.kg: транслит + только буквы/цифры/пробелы,
    пунктуация (скобки, точки) вырезается, а не заменяется дефисом.
    "ВАЗ (Lada)" -> "vaz-lada" (не "vaz-(lada)")."""
    t = translit(name)
    t = re.sub(r"[^\w\s-]", "", t)
    t = re.sub(r"\s+", "-", t.strip())
    return re.sub(r"-+", "-", t)


def _model_from_card(href: str, title: str, make: str) -> str | None:
    """Извлекает отображаемое имя модели из слага URL + заголовка карточки.

    Слаг транслитерирован ("mercedes-benz-e-klass"), а в заголовке модель
    может быть кириллицей ("E-Класс") — сопоставляем через translit.
    """
    slug = href[len("/details/"):].rsplit("-", 1)[0]
    make_slug = _url_slug(make)
    if not slug.startswith(make_slug + "-"):
        return None
    model_slug = slug[len(make_slug) + 1:]
    rest = title
    if rest.lower().startswith(make.lower()):
        rest = rest[len(make):].strip()
    acc: list[str] = []
    joined = ""
    for token in rest.split():
        piece = _url_slug(token)
        if not piece:
            continue  # токен состоял только из пунктуации — пропускаем
        candidate = (joined + "-" if joined else "") + piece
        if model_slug == candidate:
            return " ".join(acc + [token])
        if model_slug.startswith(candidate + "-"):
            acc.append(token)
            joined = candidate
            continue
        break
    return None


def sort_makes_by_slug_length(makes: list[str]) -> list[str]:
    """Сортировка марок по убыванию длины URL-слага — для перебора с самой
    длинной (значит наиболее специфичной) марки первой, напр. 'ГАЗ-САЗ'
    раньше 'ГАЗ', иначе короткая марка ложно "съест" длинную."""
    return sorted(makes, key=lambda m: -len(_url_slug(m)))


def resolve_brand_model_from_card(
    href: str, title: str, sorted_makes: list[str]
) -> tuple[str, str] | None:
    """Определяет марку и модель по URL-слагу+заголовку, когда марка заранее
    не известна (полное сканирование рынка без фильтра по марке).
    sorted_makes — из sort_makes_by_slug_length (самые длинные слаги первыми)."""
    if not href.startswith("/details/"):
        return None
    slug = href[len("/details/"):].rsplit("-", 1)[0]
    for make in sorted_makes:
        make_slug = _url_slug(make)
        if slug == make_slug or slug.startswith(make_slug + "-"):
            model = _model_from_card(href, title, make)
            if model:
                return make, model
    return None


async def models_for_make(make: str) -> list[str]:
    """Модели марки по свежим объявлениям (для кнопок автозаполнения)."""
    listings = await search(make, max_pages=2)
    counts: dict[str, int] = {}
    for l in listings:
        model = _model_from_card(l.url.replace(BASE, ""), l.title, make)
        if model:
            counts[model] = counts.get(model, 0) + 1
    return [m for m, _ in sorted(counts.items(), key=lambda x: -x[1])]


async def resolve_model(make: str, model_input: str) -> str | None:
    """Подбирает точный регистрозависимый слаг модели, пробуя варианты.

    Возвращает рабочий слаг или None, если ни один вариант не дал результатов.
    """
    tried = []
    for candidate in dict.fromkeys([
        model_input.strip(),
        model_input.strip().capitalize(),
        model_input.strip().title(),
        model_input.strip().upper(),
    ]):
        tried.append(candidate)
        found = await search(make, candidate, max_pages=1)
        if found:
            return candidate
    log.info("model not resolved for %s: tried %s", make, tried)
    return None


async def fresh(max_pages: int = 2) -> list[RawListing]:
    """Свежие объявления без фильтров (для слежения по ключевым словам)."""
    return await search(brand=None, max_pages=max_pages)


async def seller_of(slug: str) -> tuple[str | None, str | None]:
    """(id продавца, имя) — публичный API карточки объявления."""
    async with httpx.AsyncClient(timeout=20, headers={**HEADERS, "X-Project-Id": "1",
                                                      "Accept": "application/json"}) as client:
        try:
            resp = await client.get(f"{BASE}/api/mbank-proxy/v1/ads/{slug}/detail")
            d = resp.json() if resp.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return None, None
    d = d.get("data", d) if isinstance(d, dict) else {}
    name = (d.get("user") or {}).get("name") or (d.get("dealer") or {}).get("name")
    return (str(d["user_id"]) if d.get("user_id") else None), name


async def fetch_details(listing: RawListing) -> RawListing:
    """Дозагружает описание и характеристики (вкл. страну) со страницы деталей."""
    if "/details/" in listing.url and not listing.extra.get("seller_id"):
        sid, sname = await seller_of(listing.url.split("/details/", 1)[1].strip("/"))
        if sid:
            listing.extra["seller_id"], listing.extra["seller_name"] = sid, sname
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            html = await _fetch(client, listing.url)
        except httpx.HTTPError as e:
            log.warning("mashina.kg details failed %s: %s", listing.url, e)
            return listing
    # RSC-payload в <script> содержит те же строки, что и DOM — вырезаем,
    # чтобы BeautifulSoup видел только визуальную разметку
    html = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    soup = BeautifulSoup(html, "html.parser")

    h2 = soup.find(string=re.compile("Описание от продавца"))
    if h2:
        section = h2.find_parent()
        if section:
            nxt = section.find_next_sibling()
            if nxt:
                listing.description = nxt.get_text(" ", strip=True)[:4000]

    # характеристики: строки <div class="...min-h-7..."><span>Метка</span><div><span>Значение
    chars: dict[str, str] = {}
    for label_span in soup.select("div span.text-text-muted"):
        label = label_span.get_text(strip=True)
        value_div = label_span.find_next_sibling("div")
        if label and value_div:
            chars[label] = value_div.get_text(" ", strip=True)
    if chars:
        listing.extra["characteristics"] = chars
        for key, val in chars.items():
            if "страна" in key.lower():
                listing.country = val
                break
        if chars.get("Наличие в Кыргызстане"):
            listing.extra["availability"] = chars["Наличие в Кыргызстане"]
    return listing
