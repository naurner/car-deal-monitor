"""Публикация объявлений на lalafo через API сайта (разведка 2026-10).

Порядок, как у самой формы lalafo.kg/ad/post:
1. POST /catalog/v32/posting-ads/temp — черновик (приватный, его видно только нам);
2. POST /upload/swoole-upload/v3/images/upload (image_file + ad_id) — фото сразу
   привязываются к черновику;
3. POST /catalog/v32/params/posting — какие параметры у категории и коды значений;
4. PUT /catalog/v32/posting-ads/temp/<id> — категория, тексты, цена, параметры
   в виде [{"id": <param>, "value_id": <value>}] (для полей ввода — "value");
5. POST /catalog/v32/posting-ads/temp/<id>/publish — публикация.
dry_run=True делает 1–4 и возвращает ссылку на черновик: так проверяем
заполнение, ничего не публикуя.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from parsers import mashina
from posting.lalafo_chat import SessionExpired
from posting.sessions import SiteBrowser

log = logging.getLogger(__name__)

API = "https://lalafo.kg/api/catalog"
UPLOAD = "https://lalafo.kg/api/upload/swoole-upload/v3/images/upload"
TREE = "https://lalafo.kg/api/proxy/catalog/v3/categories/tree"
HDR = {"device": "pc", "country-id": "12", "language": "ru_RU", "X-Cache-Bypass": "yes"}

CAT_CARS = 1502
CAT_PARTS = {"Шины": 6653, "Диски": 6654, "Колёса в сборе": 6655}

# наши поля объявления → id параметра lalafo
CAR_PARAMS = {
    "model": 49, "year": 62, "mileage": 56, "fuel": 65, "body_type": 63,
    "transmission": 64, "steering": 106, "drive": 244, "color": 105,
    "engine_volume": 66, "condition_tech": 1155, "reg_country": 5151,
    "customs": 1157, "availability": 242, "state": 29,
}
PART_PARAMS = {
    "state": 29, "diameter": 189, "width": 188, "profile": 187, "season": 184,
    "rim_type": 1963, "bolts": 1964, "quantity": 1962, "brand": (1819, 1965),
}
# как назвать наши значения словами lalafo
SYNONYMS = {
    "аварийное / не на ходу": "Аварийное", "среднее": "Хорошее",
    "корея": "Южная Корея", "кр": "Кыргызстан",
    "полный": "4WD, полный", "передний": "Передний", "задний": "Задний",
    "внедорожник": "Внедорожник", "электро": "Электромобиль",
    "колеса в сборе": "Колёса в сборе",
}


@dataclass
class PostResult:
    ok: bool
    message: str
    url: str | None = None
    external_id: str | None = None
    payment_required: bool = False
    skipped: list[str] = field(default_factory=list)  # что не удалось сопоставить


def _norm(s) -> str:
    s = mashina.translit(str(s).lower().replace("ё", "е"))
    return re.sub(r"[^\w.]", "", s.replace(",", "."))


def match_value(values: list[dict], wanted) -> int | None:
    """Код значения lalafo по нашему тексту: точное совпадение, затем по вхождению."""
    if wanted in (None, ""):
        return None
    w = SYNONYMS.get(str(wanted).strip().lower(), str(wanted).strip())
    nw = _norm(w)
    for v in values:
        if _norm(v.get("value") or v.get("name") or "") == nw:
            return v["id"]
    for v in values:
        nv = _norm(v.get("value") or v.get("name") or "")
        if nv and (nw in nv or nv in nw) and min(len(nw), len(nv)) >= 3:
            return v["id"]
    try:  # числа: «17» ~ «17.0», объём «2.5» ~ «2,5»
        f = float(nw)
        for v in values:
            try:
                if float(_norm(v.get("value") or "")) == f:
                    return v["id"]
            except ValueError:
                pass
    except ValueError:
        pass
    return None


async def car_category(brand: str) -> int | None:
    async with httpx.AsyncClient(timeout=30) as c:
        tree = (await c.get(TREE, headers=HDR)).json()

    def find(nodes):
        for n in nodes:
            if n["id"] == CAT_CARS:
                return n.get("children") or []
            r = find(n.get("children") or [])
            if r is not None:
                return r
        return None
    brands = find(tree) or []
    nb = _norm(brand)
    for n in brands:
        if _norm(n["name"]) == nb:
            return n["id"]
    for n in brands:
        if nb and (nb in _norm(n["name"]) or _norm(n["name"]) in nb):
            return n["id"]
    return None


def _car_values(d: dict) -> dict:
    mileage = d.get("mileage")
    reg = d.get("reg_country") or "Кыргызстан"
    return {
        "model": d.get("model"), "year": d.get("year") and int(d["year"]),
        "mileage": int(mileage) if mileage not in (None, "") else None,
        "fuel": d.get("fuel"), "body_type": d.get("body_type"),
        "transmission": d.get("transmission"), "steering": d.get("steering"),
        "drive": d.get("drive"), "color": d.get("color"),
        "engine_volume": d.get("engine_volume"), "condition_tech": d.get("condition"),
        "reg_country": reg,
        "customs": "Растаможен" if "кыргыз" in str(reg).lower() else None,
        "availability": "В наличии",
        "state": "Новый" if mileage in (0, "0") else "Б/у",
    }


def _part_values(d: dict) -> dict:
    return {k: d.get(k) for k in PART_PARAMS}


def _title(d: dict) -> str:
    if d.get("kind") == "part":
        return (d.get("title") or d.get("part_type") or "")[:70]
    return f"{d.get('brand', '')} {d.get('model', '')} {d.get('year') or ''}".strip()


class _Client:
    """Сессия владельца: токен берём из запросов открытой формы публикации."""

    def __init__(self) -> None:
        self._browser = SiteBrowser("lalafo")
        self.token = None
        self.temp: dict = {}

    async def __aenter__(self):
        self.ctx = await self._browser.__aenter__()
        try:
            page = await self.ctx.new_page()
            page.on("request", lambda r: setattr(self, "token", self.token or (
                r.headers.get("authorization") if r.headers.get("authorization", "").startswith("Bearer ") else None)))

            async def on_resp(r):
                if r.url.endswith("/v32/posting-ads/temp") and r.request.method == "POST" and r.ok:
                    self.temp = await r.json()
            page.on("response", on_resp)
            await page.goto("https://lalafo.kg/ad/post", wait_until="networkidle")
            if not self.token:
                raise SessionExpired("lalafo: сессия истекла — войдите заново: /login")
            if not self.temp:
                self.temp = await self.call("POST", "/v32/posting-ads/temp")
        except BaseException:
            await self._browser.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc):
        await self._browser.__aexit__(*exc)

    async def call(self, method: str, path: str, body=None) -> dict:
        resp = await self.ctx.request.fetch(
            API + path, method=method,
            headers={**HDR, "authorization": self.token, "content-type": "application/json"},
            data=json.dumps(body) if body is not None else None)
        text = await resp.text()
        if resp.status in (401, 403):
            raise SessionExpired("lalafo: сессия истекла — войдите заново: /login")
        if not resp.ok:
            raise RuntimeError(f"lalafo {method} {path}: HTTP {resp.status} {text[:300]}")
        return json.loads(text) if text else {}

    async def upload(self, path: Path, ad_id: int, n: int = 1) -> dict:
        return await _upload(self.ctx, {**HDR, "authorization": self.token}, path, ad_id, n)

    async def clear_temp_images(self) -> None:
        """Черновик lalafo живёт между попытками: фото прошлой неудачной
        публикации остались бы в объявлении вторым комплектом."""
        ids = [i["id"] for i in self.temp.get("images") or [] if i.get("id")]
        if not ids:
            return
        hdr = {**HDR, "authorization": self.token}
        query = "&".join(f"ids[]={i}" for i in ids)
        resp = await self.ctx.request.delete(f"{REMOVE_IMAGES}?{query}", headers=hdr)
        if not resp.ok:
            for i in ids:  # у черновика свой адрес удаления
                await self.ctx.request.delete(f"{API}/v33/temp-images/{i}", headers=hdr)
        log.info("lalafo: убрал %d фото прошлой попытки из черновика", len(ids))


UPLOAD_RETRY_PAUSES = (3, 10, 30)  # 520/502 у lalafo — обычно временные


async def _upload(ctx, hdr: dict, path: Path, ad_id: int, n: int) -> dict:
    """Загрузка фото с повторами: lalafo иногда отвечает 520/502 (2026-10-02,
    объявление #56 — упало на одном фото, весь пост сорвался). Имя файла —
    простое «photo_N.jpg», как у браузера, а не наше внутреннее."""
    import asyncio

    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    name = f"photo_{n}{'.png' if mime == 'image/png' else '.jpg'}"
    data = path.read_bytes()
    status, text = 0, ""
    for pause in (*UPLOAD_RETRY_PAUSES, None):
        resp = await ctx.request.post(UPLOAD, headers=hdr, multipart={
            "image_file": {"name": name, "mimeType": mime, "buffer": data}, "ad_id": str(ad_id)})
        if resp.ok:
            return await resp.json()
        status, text = resp.status, (await resp.text())[:200]
        if status < 500 or pause is None:
            break
        log.warning("lalafo: фото %d — HTTP %s, повтор через %s с", n, status, pause)
        await asyncio.sleep(pause)
    first_line = text.split("<title>", 1)[-1].split("</title>", 1)[0].strip() if "<title>" in text else text[:120]
    raise RuntimeError(f"lalafo: фото №{n} не загрузилось после повторов (HTTP {status}: {first_line})")


async def post(ad_data: dict, description: str, price_usd: float | None,
               photos: list[str], dry_run: bool = False) -> PostResult:
    kind = ad_data.get("kind", "car")
    if kind == "part":
        category = CAT_PARTS.get(ad_data.get("part_type"))
        values, param_map = _part_values(ad_data), PART_PARAMS
    else:
        category = await car_category(ad_data.get("brand", ""))
        values, param_map = _car_values(ad_data), CAR_PARAMS
    if not category:
        return PostResult(False, f"lalafo: не нашёл категорию для «{ad_data.get('brand') or ad_data.get('part_type')}»")

    async with _Client() as c:
        ad_id = c.temp["id"]
        await c.clear_temp_images()
        for n, p in enumerate(photos, 1):
            await c.upload(Path(p), ad_id, n)
        schema = await c.call("POST", "/v32/params/posting", {
            "id": ad_id, "category_id": category, "city_id": c.temp.get("city_id"), "type": None})
        by_id = {p.get("id"): p for p in schema if p.get("id")}
        params, skipped = [], []
        for key, pid in param_map.items():
            val = values.get(key)
            if val in (None, ""):
                continue
            pids = pid if isinstance(pid, tuple) else (pid,)
            p = next((by_id[x] for x in pids if x in by_id), None)
            if p is None:
                continue
            if p.get("kind") == "input":
                params.append({"id": p["id"], "value": str(val)})
                continue
            vid = match_value(p.get("values") or [], val)
            if vid is None:
                skipped.append(f"{p.get('name')}: {val}")
            else:
                params.append({"id": p["id"], "value_id": vid})
        missing = [p["name"] for p in schema if p.get("is_required") and p.get("id")
                   and p["id"] not in {x["id"] for x in params}]
        if missing:
            return PostResult(False, "lalafo: не заполнены обязательные поля — " + ", ".join(missing),
                              skipped=skipped)
        # lalafo берёт заголовок из первой строки описания, поле title игнорирует
        title = _title(ad_data)
        if title and not description.lstrip().lower().startswith(title.lower()):
            description = f"{title}\n\n{description}"
        body = {**c.temp, "category_id": category, "title": title,
                "description": description, "price": int(price_usd) if price_usd else None,
                "currency": "USD", "is_negotiable": not price_usd, "params": params}
        body.pop("images", None)  # фото уже привязаны при загрузке — не затираем
        saved = await c.call("PUT", f"/v32/posting-ads/temp/{ad_id}", body)
        got = {p["id"] for p in saved.get("params") or []
               if any(v.get("selected") for v in p.get("values") or []) or p.get("value")}
        lost = [p for p in params if p["id"] not in got and p["id"] in by_id and by_id[p["id"]].get("kind") != "input"]
        if lost:
            log.warning("lalafo: не сохранились параметры %s", lost)
        n_img = len(saved.get("images") or [])
        if dry_run:
            return PostResult(True, f"lalafo: черновик «{saved.get('title')}» заполнен ({len(params)} параметров, фото {n_img}), "
                                    "не опубликован", url=saved.get("url"), external_id=str(ad_id),
                              skipped=skipped)
        res = await c.call("POST", f"/v32/posting-ads/temp/{ad_id}/publish?expand=available_campaign_types")
        text = json.dumps(res, ensure_ascii=False).lower()
        if any(w in text for w in ("оплат", "payment", "limit", "лимит")) and not res.get("url"):
            return PostResult(False, f"lalafo: требуется оплата размещения — {text[:200]}",
                              payment_required=True, external_id=str(ad_id), skipped=skipped)
        return PostResult(True, "lalafo: опубликовано", url=res.get("url") or saved.get("url"),
                          external_id=str(ad_id), skipped=skipped)


# ---------- замена фото у опубликованного объявления ----------
# Как страница редактирования (<url объявления>/edit): текущие фото —
# GET /catalog/v3/feed/my-ad-details/<id>; новые — та же загрузка, что при
# публикации, с ad_id объявления; старые — DELETE /catalog/v3/images/remove-by-ids.
# Старые удаляем, только убедившись, что новые привязались: объявление не
# останется без фото.

MY_AD = "https://lalafo.kg/api/catalog/v3/feed/my-ad-details/{}"
REMOVE_IMAGES = "https://lalafo.kg/api/catalog/v3/images/remove-by-ids"


async def replace_photos(info: dict, photos: list[str]) -> PostResult:
    url = info.get("url") or ""
    if not url.startswith("http"):
        url = "https://lalafo.kg" + url
    m = re.search(r"-id-(\d+)", url) or re.search(r"(\d{6,})", str(info.get("external_id") or ""))
    if not m:
        return PostResult(False, "lalafo: не нашёл номер объявления")
    ad_id = int(m.group(1))
    token: list[str] = []
    async with SiteBrowser("lalafo") as ctx:
        page = await ctx.new_page()
        page.on("request", lambda r: token.append(r.headers["authorization"])
                if r.headers.get("authorization", "").startswith("Bearer ") and not token else None)
        await page.goto(url.rstrip("/") + "/edit", wait_until="networkidle")
        await page.wait_for_timeout(1500)
        if not token:
            raise SessionExpired("lalafo: сессия истекла — войдите заново: /login")
        hdr = {**HDR, "authorization": token[0]}

        async def current() -> list[int]:
            resp = await ctx.request.get(MY_AD.format(ad_id), headers=hdr)
            if not resp.ok:
                raise RuntimeError(f"lalafo: объявление {ad_id} не открылось: HTTP {resp.status}")
            return [i["id"] for i in (await resp.json()).get("images") or [] if i.get("id")]

        old = await current()
        for n, p in enumerate(photos, 1):
            try:
                await _upload(ctx, hdr, Path(p), ad_id, n)
            except RuntimeError as e:
                return PostResult(False, f"{e} — старые фото оставил")
        now = await current()
        added = [i for i in now if i not in old]
        if len(added) < len(photos):
            return PostResult(False, f"lalafo: новые фото не привязались к объявлению "
                                     f"({len(added)} из {len(photos)}) — старые оставил")
        if old:
            query = "&".join(f"ids[]={i}" for i in old)
            resp = await ctx.request.delete(f"{REMOVE_IMAGES}?{query}", headers=hdr)
            if not resp.ok:
                return PostResult(True, f"lalafo: новые фото добавлены ({len(added)}), но старые не "
                                        f"удалились (HTTP {resp.status}) — уберите их вручную", url=url)
        left = await current()
        return PostResult(True, f"lalafo: фото заменены — сейчас {len(left)} шт.", url=url)
