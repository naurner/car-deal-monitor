"""Публикация объявлений на mashina.kg через API сайта.

Формат разобран по JavaScript формы /ads/create (2026-10, chunk с
createAd/presignImages): форма открывается только залогиненному, но её код —
публичный.

1. Варианты полей — публичные GET /ads/<attr>/options?category_id=&parent_option_id=.
   У легковых — цепочка: марка → модель → год → кузов → поколение → топливо →
   привод → коробка → модификация (каждый следующий список зависит от
   выбранного варианта предыдущего); регион → город.
2. POST /ads/images/presign?count=N → {"images": [{upload_url, image_id}]};
   файл — PUT на upload_url (хранилище, без cookies).
3. POST /ads {category_id, image_ids: ["…"], attributes: {"<attr id>": "<id
   варианта>" | текст | {"value": пробег, "suffix": "км"}}, title,
   description, price, currency: "USD", phones: ["+996…"]}.
4. POST /ads/images/confirm {listing_id} — привязать фото.
Запросы 2–4 — из залогиненного браузера бота (cookies сессии), заголовок
X-Project-Id: 1, как у самого сайта. dry_run=True делает только шаг 1:
показывает, что и как заполнится, ничего не отправляя.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from config import USER_AGENT
from posting.lalafo_chat import SessionExpired
from posting.sessions import SiteBrowser

log = logging.getLogger(__name__)

API = "https://mashina.kg/api/mbank-proxy/v1"
HDR = {"X-Project-Id": "1", "Accept": "application/json", "Accept-Language": "ru"}

CAT_CARS, CAT_TIRES, CAT_RIMS = 1, 31, 32
# у mashina нет «колёс в сборе» — выставляем в диски
CAT_PARTS = {"Шины": CAT_TIRES, "Диски": CAT_RIMS, "Колёса в сборе": CAT_RIMS}

# id атрибутов (/categories/<id>/layout)
A_MAKE, A_MODEL, A_YEAR, A_BODY, A_GEN = 16, 17, 1, 18, 13
A_FUEL, A_DRIVE, A_GEARBOX, A_MOD = 19, 23, 21, 14
A_STEERING, A_COLOR, A_CONDITION, A_MILEAGE, A_MILEAGE_UNIT = 22, 24, 20, 2, 86
A_AVAIL, A_CUSTOMS, A_ACCOUNTING, A_REGION, A_CITY = 96, 5, 82, 51, 50
A_TIRE_TYPE, A_TIRE_WIDTH, A_TIRE_HEIGHT, A_TIRE_SIZE = 152, 138, 139, 140
A_RIMS_TYPE, A_PCD, A_WHEEL_SIZE, A_COND_PARTS = 151, 136, 137, 143

SYNONYMS = {
    "аварийное / не на ходу": "аварийное", "среднее": "хорошее",
    "корея": "южная корея", "кр": "кыргызстан",
    "лето": "летние", "зима": "зимние", "зимняя шипованная": "зимние",
    "всесезонная": "всесезонные",
    "литые": "литые", "железные": "штампованные", "другие": "другие",
    "б/у": "б/у", "новый": "новые",
}
PCD_RE = re.compile(r"\b([3-8])\s*[xхX*×]\s*(\d{2,3}(?:[.,]\d)?)\b")
PAYMENT_WORDS = ("оплат", "тариф", "лимит", "limit", "payment", "balance", "баланс")


@dataclass
class PostResult:
    ok: bool
    message: str
    url: str | None = None
    external_id: str | None = None
    payment_required: bool = False
    skipped: list[str] = field(default_factory=list)
    body: dict | None = None


def _norm(s) -> str:
    return re.sub(r"[^0-9a-zа-я]", "", str(s).lower().replace("ё", "е"))


def _label(o: dict) -> str:
    return str(o.get("label") or o.get("value") or "")


def pick(options: list[dict], wanted, number: bool = False) -> dict | None:
    """Вариант mashina по нашему значению: точное совпадение, по вхождению,
    по числу («225» ~ «225 мм», «17» ~ «R17», «2.5» ~ «2.5 AT (181 л.с.)»)."""
    if wanted in (None, "") or not options:
        return None
    w = SYNONYMS.get(str(wanted).strip().lower(), str(wanted).strip())
    nw = _norm(w)
    for o in options:
        if _norm(_label(o)) == nw or _norm(o.get("value") or "") == nw:
            return o
    if number:
        digits = re.sub(r"[^\d.]", "", str(w).replace(",", "."))
        for o in options:
            if re.sub(r"[^\d.]", "", _label(o).replace(",", ".")) == digits and digits:
                return o
        return None
    for o in options:
        nl = _norm(_label(o))
        if nw and nl and (nw in nl or nl in nw) and min(len(nw), len(nl)) >= 3:
            return o
    return None


class _Options:
    """Публичные справочники вариантов (без входа)."""

    def __init__(self, client: httpx.AsyncClient, category: int) -> None:
        self.c, self.category = client, category

    async def get(self, attr: int, parent: int | None = None) -> list[dict]:
        params = {"category_id": self.category}
        if parent:
            params["parent_option_id"] = parent
        resp = await self.c.get(f"{API}/ads/{attr}/options", params=params)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("results") or []


async def _location(opts: _Options, city: str | None) -> tuple[dict | None, dict | None]:
    """(регион, город) по названию города; по умолчанию — Бишкек."""
    city = (city or "Бишкек").strip()
    regions = await opts.get(A_REGION)
    # город-регион (Бишкек, Ош) — сначала регион с таким названием
    ordered = sorted(regions, key=lambda r: _norm(city) not in _norm(_label(r)))
    for region in ordered:
        found = pick(await opts.get(A_CITY, region["id"]), city)
        if found:
            return region, found
    return None, None


async def _car_attributes(opts: _Options, d: dict, skipped: list[str]) -> dict[int, object]:
    attrs: dict[int, object] = {}
    chain = [
        (A_MAKE, "Марка", d.get("brand"), False),
        (A_MODEL, "Модель", d.get("model"), False),
        (A_YEAR, "Год", d.get("year"), True),
        (A_BODY, "Кузов", d.get("body_type"), False),
        (A_GEN, "Поколение", d.get("generation"), False),
        (A_FUEL, "Топливо", d.get("fuel"), False),
        (A_DRIVE, "Привод", d.get("drive"), False),
        (A_GEARBOX, "Коробка", d.get("transmission"), False),
        (A_MOD, "Модификация", d.get("engine_volume"), False),
    ]
    parent = None
    for attr, name, wanted, number in chain:
        options = await opts.get(attr, parent)
        if attr == A_MOD and wanted:
            # «2.5» → «2.5 AT (181 л.с.)»: по объёму в начале названия
            vol = str(wanted).replace(",", ".").strip()
            hit = [o for o in options if _label(o).replace(",", ".").startswith(vol)]
            chosen = hit[0] if hit else None
        else:
            chosen = pick(options, wanted, number)
        if chosen is None and len(options) == 1:
            chosen = options[0]  # единственный вариант (одно поколение, один кузов…)
        if chosen is None and attr == A_GEN and options:
            chosen = options[0]  # поколение по году почти всегда одно
        if chosen is None:
            variants = ", ".join(_label(o) for o in options[:12])
            raise ValueError(f"{name}: «{wanted or '—'}» нет среди вариантов mashina"
                             + (f" ({variants})" if variants else ""))
        attrs[attr] = str(chosen["id"])
        parent = chosen["id"]

    for attr, name, wanted in ((A_STEERING, "Руль", d.get("steering") or "Слева"),
                               (A_COLOR, "Цвет", d.get("color")),
                               (A_CONDITION, "Состояние", d.get("condition") or "Хорошее"),
                               (A_AVAIL, "Наличие", d.get("availability") or "В наличии"),
                               (A_ACCOUNTING, "Учёт", d.get("reg_country") or "Кыргызстан")):
        options = await opts.get(attr)
        chosen = pick(options, wanted)
        if chosen is None and attr == A_ACCOUNTING:
            chosen = pick(options, "Другая страна")  # США, Корея, Грузия… — в списке mashina их нет
        if chosen is None:
            raise ValueError(f"{name}: «{wanted}» нет среди вариантов mashina")
        attrs[attr] = str(chosen["id"])
    if "кыргыз" in str(d.get("reg_country") or "кыргыз").lower():
        attrs[A_CUSTOMS] = "true"
    mileage = re.sub(r"\D", "", str(d.get("mileage") or ""))
    if mileage:
        attrs[A_MILEAGE] = {"value": mileage, "suffix": "км"}
    return attrs


async def _part_attributes(opts: _Options, d: dict, category: int) -> dict[int, object]:
    attrs: dict[int, object] = {}

    async def put(attr: int, name: str, wanted, number: bool = False) -> None:
        chosen = pick(await opts.get(attr), wanted, number)
        if chosen is None:
            raise ValueError(f"{name}: «{wanted or '—'}» нет среди вариантов mashina")
        attrs[attr] = str(chosen["id"])

    await put(A_COND_PARTS, "Состояние", d.get("state") or "Б/у")
    await put(A_AVAIL, "Наличие", "В наличии")
    if category == CAT_TIRES:
        await put(A_TIRE_TYPE, "Тип шин", d.get("season"))
        await put(A_TIRE_WIDTH, "Ширина шины", d.get("width"), number=True)
        await put(A_TIRE_HEIGHT, "Профиль шины", d.get("profile"), number=True)
        await put(A_TIRE_SIZE, "Диаметр шины", d.get("diameter"), number=True)
    else:
        await put(A_RIMS_TYPE, "Тип дисков", d.get("rim_type"))
        pcd = normalize_pcd(d.get("pcd"))
        chosen = pick(await opts.get(A_PCD), pcd) if pcd else None
        if chosen:
            attrs[A_PCD] = str(chosen["id"])
        else:
            # сверловку не знаем — выставляем без неё (решение пользователя:
            # «похуй на сверловку»); не примет сайт — покажем его ответ
            log.info("mashina: сверловка «%s» не распознана — без неё", d.get("pcd"))
        await put(A_WHEEL_SIZE, "Диаметр диска", d.get("diameter"), number=True)
    return attrs


def normalize_pcd(raw) -> str:
    """«5×114,3», «5Х114.3», «5*114.3» → «5x114.3» (как в списке mashina)."""
    s = str(raw or "").strip().lower().replace(" ", "").replace(",", ".")
    for ch in ("×", "х", "*"):
        s = s.replace(ch, "x")
    return s


async def build(ad_data: dict, description: str, price_usd: float | None) -> tuple[int, dict]:
    """(категория, тело POST /ads без фото и телефонов). ValueError — что не сопоставилось."""
    kind = ad_data.get("kind", "car")
    category = CAT_PARTS.get(ad_data.get("part_type")) if kind == "part" else CAT_CARS
    if not category:
        raise ValueError(f"нет категории для «{ad_data.get('part_type')}»")
    if kind == "part" and not ad_data.get("pcd"):
        # черновики, созданные до шага «Сверловка», — ищем «5x114.3» в тексте
        m = PCD_RE.search(f"{ad_data.get('title') or ''} {description or ''}")
        if m:
            ad_data = {**ad_data, "pcd": f"{m.group(1)}x{m.group(2).replace(',', '.')}"}
    async with httpx.AsyncClient(timeout=30, headers={**HDR, "User-Agent": USER_AGENT}) as c:
        opts = _Options(c, category)
        if kind == "part" and category == CAT_RIMS and ad_data.get("part_type") == "Колёса в сборе" \
                and not normalize_pcd(ad_data.get("pcd")) and ad_data.get("width") and ad_data.get("profile"):
            # колёса без сверловки — выставляем как шины: размер шины известен
            category = CAT_TIRES
            opts = _Options(c, category)
        if kind == "part":
            attrs = await _part_attributes(opts, ad_data, category)
        else:
            attrs = await _car_attributes(opts, ad_data, [])
        region, city = await _location(opts, ad_data.get("city"))
        if not city:
            raise ValueError(f"Город: «{ad_data.get('city') or 'Бишкек'}» нет среди городов mashina")
        attrs[A_REGION], attrs[A_CITY] = str(region["id"]), str(city["id"])
    body = {"category_id": category, "attributes": {str(k): v for k, v in attrs.items()},
            "description": description, "currency": "USD"}
    if price_usd:
        body["price"] = int(price_usd)
    if kind == "part" and ad_data.get("title"):
        body["title"] = ad_data["title"][:100]
    return category, body


class _Session:
    """Залогиненный браузер бота: запросы к API с cookies сессии."""

    def __init__(self) -> None:
        self._browser = SiteBrowser("mashina")

    async def __aenter__(self):
        self.ctx = await self._browser.__aenter__()
        try:
            page = await self.ctx.new_page()
            # страница кабинета заодно обновляет токен сессии, как у бампа
            await page.goto("https://mashina.kg/profile/my-ads", wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
            if "/auth" in page.url:
                raise SessionExpired("mashina: сессия истекла — войдите заново: /login")
        except BaseException:
            await self._browser.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc):
        await self._browser.__aexit__(*exc)

    async def call(self, method: str, path: str, body=None, params=None):
        resp = await self.ctx.request.fetch(
            API + path, method=method, params=params,
            headers={**HDR, "Content-Type": "application/json"},
            data=json.dumps(body) if body is not None else None)
        text = await resp.text()
        if resp.status == 401:
            raise SessionExpired("mashina: сессия истекла — войдите заново: /login")
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {"raw": text[:300]}
        if not resp.ok:
            raise _ApiError(resp.status, data)
        return data

    async def upload(self, path: Path, url: str) -> None:
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        resp = await self.ctx.request.put(url, data=path.read_bytes(), headers={"Content-Type": mime})
        if not resp.ok:
            raise RuntimeError(f"фото {path.name} не загрузилось: HTTP {resp.status}")


class _ApiError(Exception):
    def __init__(self, status: int, data) -> None:
        self.status, self.data = status, data
        super().__init__(f"HTTP {status}: {json.dumps(data, ensure_ascii=False)[:400]}")

    def text(self) -> str:
        d = self.data if isinstance(self.data, dict) else {}
        return str(d.get("message") or d.get("detail") or d.get("error_code") or d)[:300]


def _listing(resp) -> dict:
    if isinstance(resp, dict):
        for k in ("data", "listing"):
            if not resp.get("id") and isinstance(resp.get(k), dict):
                return resp[k]
    return resp if isinstance(resp, dict) else {}


async def post(ad_data: dict, description: str, price_usd: float | None,
               photos: list[str], dry_run: bool = False) -> PostResult:
    try:
        category, body = await build(ad_data, description, price_usd)
    except ValueError as e:
        return PostResult(False, f"mashina: {e}")
    if dry_run:
        return PostResult(True, "mashina: черновик собран (не отправлялся)", body=body)

    async with _Session() as s:
        me = await s.call("GET", "/profile/me")
        if "phones" not in me and isinstance(me.get("data"), dict):
            me = me["data"]
        raw = me.get("phones") or [me.get("contact") or me.get("phone")]
        raw = [p.get("number") if isinstance(p, dict) else p for p in raw]
        phones = [str(p) if str(p).startswith("+") else f"+{p}" for p in raw if p]
        wanted = re.sub(r"\D", "", str(ad_data.get("phone") or ""))
        chosen = [p for p in phones if wanted and re.sub(r"\D", "", p).endswith(wanted[-9:])]
        body["phones"] = (chosen or phones)[:1]
        if not body["phones"]:
            return PostResult(False, "mashina: в профиле mashina нет телефона — добавьте его на сайте")

        image_ids = []
        if photos:
            pres = await s.call("POST", "/ads/images/presign", {}, params={"count": len(photos)})
            slots = pres.get("images") or []
            for path, slot in zip(photos, slots):
                await s.upload(Path(path), slot["upload_url"])
                image_ids.append(str(slot["image_id"]))
        body["image_ids"] = image_ids

        try:
            created = _listing(await s.call("POST", "/ads", body))
        except _ApiError as e:
            low = json.dumps(e.data, ensure_ascii=False).lower()
            log.warning("mashina POST /ads: %s; тело: %s", e, json.dumps(body, ensure_ascii=False))
            pay = any(w in low for w in PAYMENT_WORDS)
            return PostResult(False, f"mashina: {'нужна оплата — ' if pay else ''}{e.text()}",
                              payment_required=pay, body=body)
        listing_id = created.get("id")
        if image_ids and listing_id:
            try:
                await s.call("POST", "/ads/images/confirm", {"listing_id": listing_id})
            except _ApiError as e:
                log.warning("mashina: фото не привязались к %s: %s", listing_id, e)
        slug = created.get("slug")
        url = f"https://mashina.kg/details/{slug}" if slug else "https://mashina.kg/profile/my-ads"
        status = str(created.get("moderation_status") or created.get("status") or "")
        note = " (на модерации)" if "moder" in status.lower() or "pending" in status.lower() else ""
        return PostResult(True, f"mashina: опубликовано{note}", url=url,
                          external_id=str(listing_id or slug or ""), body=body)


# ---------- замена фото у опубликованного объявления ----------
# Как форма редактирования сайта: GET /ads/<id>/edit (текущие фото), новые —
# presign + PUT, PATCH /ads/slug/<slug> с image_ids, confirm, затем DELETE
# /ads/images/<id> заменённых.

def _phones(raw) -> list[str]:
    raw = [p.get("number") if isinstance(p, dict) else p for p in raw or []]
    return [str(p) if str(p).startswith("+") else f"+{p}" for p in raw if p]


async def replace_photos(ad_data: dict, info: dict, description: str,
                         price_usd: float | None, photos: list[str]) -> PostResult:
    url = info.get("url") or ""
    if "/details/" not in url:
        return PostResult(False, "mashina: у объявления нет ссылки — не знаю, что менять")
    slug = url.split("/details/", 1)[1].strip("/")
    async with _Session() as s:
        listing_id = info.get("external_id")
        if not str(listing_id or "").isdigit():
            listing_id = _listing(await s.call("GET", f"/ads/slug/{slug}")).get("id")
        edit = _listing(await s.call("GET", f"/ads/{listing_id}/edit"))
        old_ids = [str(i.get("id")) for i in edit.get("images") or [] if isinstance(i, dict) and i.get("id")]

        pres = await s.call("POST", "/ads/images/presign", {}, params={"count": len(photos)})
        new_ids = []
        for path, slot in zip(photos, pres.get("images") or []):
            await s.upload(Path(path), slot["upload_url"])
            new_ids.append(str(slot["image_id"]))
        if len(new_ids) < len(photos):
            return PostResult(False, f"mashina: загрузилось {len(new_ids)} фото из {len(photos)}")

        # создано ботом — тело собираем заново, как при публикации; иначе
        # меняем только фото (PATCH)
        body: dict = {"image_ids": new_ids}
        if ad_data.get("brand") or ad_data.get("kind") == "part":
            try:
                _, body = await build(ad_data, description, price_usd)
                body["image_ids"] = new_ids
            except ValueError as e:
                log.warning("mashina: не собрал тело для замены фото (%s) — меняю только фото", e)
        phones = _phones(edit.get("phones"))
        if phones:
            body["phones"] = phones
        try:
            await s.call("PATCH", f"/ads/slug/{slug}", body)
        except _ApiError as e:
            log.warning("mashina PATCH /ads/slug/%s: %s; тело: %s", slug, e, json.dumps(body, ensure_ascii=False))
            return PostResult(False, f"mashina: сайт не принял новые фото — {e.text()}", body=body)
        try:
            await s.call("POST", "/ads/images/confirm", {"listing_id": listing_id})
        except _ApiError as e:
            log.warning("mashina: confirm фото %s: %s", listing_id, e)
        removed = 0
        for img in old_ids:
            if img in new_ids:
                continue
            try:
                await s.call("DELETE", f"/ads/images/{img}")
                removed += 1
            except _ApiError as e:
                log.warning("mashina: старое фото %s не удалилось: %s", img, e)
        return PostResult(True, f"mashina: фото заменены ({len(new_ids)} новых, старых убрано {removed})",
                          url=url if url.startswith("http") else "https://mashina.kg" + url)
