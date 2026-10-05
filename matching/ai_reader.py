"""ИИ читает описания объявлений — то, чего нет в полях карточки.

Локальная Ollama на Маке (или любой OpenAI-совместимый сервер, см. config.py)
получает пачку объявлений и возвращает факты: учёт, где машина, в пути ли,
растаможена ли, битая / на запчасти / не на ходу, полная ли цена (а не взнос
или аренда), обмен, и короткую сводку для перекупщика.

Результат хранится в listings_archive.ai_facts вместе с хэшем текста —
перечитываем, только если описание изменилось. Если ИИ выключен или
недоступен, всё работает как без него: факты просто отсутствуют.
"""

import hashlib
import json
import logging
import re
import time
from difflib import SequenceMatcher

import httpx
from sqlalchemy import select

from config import AI_API_KEY, AI_BASE_URL, AI_ENABLED, AI_MODEL
from db.models import ArchivedListing
from db.session import get_session
from parsers import mashina
from parsers.base import RawListing

log = logging.getLogger(__name__)

BATCH = 5            # объявлений в одном запросе: маленькой модели так надёжнее
DESC_LIMIT = 1000    # символов описания на объявление
TIMEOUT_SEC = 300    # первый запрос грузит модель в память — это долго
PAUSE_SEC = 600      # сервер не ответил — не долбим его 10 минут
NUM_CTX = 8192       # окно контекста Ollama: пачка из 5 описаний + инструкция
KEEP_ALIVE = "30m"   # не выгружать модель между запросами (по умолчанию 5 минут)

SYSTEM_PROMPT = """Ты читаешь объявления о продаже автомобилей в Кыргызстане (mashina.kg, lalafo.kg).
Для каждого объявления извлеки факты ТОЛЬКО из текста. Не сказано — null (для да/нет — false).

Ответ — строго JSON:
{"items": [{"id": "...", "reg_country": null, "in_kg": null, "in_transit": false,
 "customs_cleared": null, "severe_damage": false, "for_parts": false, "not_running": false,
 "price_not_full": false, "exchange": false, "summary": ""}]}

Поля:
- reg_country: страна учёта (регистрации), если прямо сказано: «учёт КР», «на казахском учёте», «армянский учёт». «Из Кореи», «с Америки» — это откуда пригнали, НЕ учёт.
- in_kg: true — машина сейчас в Кыргызстане; false — за границей (стоит в Корее/Китае/Дубае, продаётся под заказ); null — не ясно.
- in_transit: true — в пути, под заказ, ещё не пришла.
- customs_cleared: false — «не растаможена», true — «растаможена», иначе null.
- severe_damage: true — битая, после серьёзного ДТП, нужен кузовной ремонт, утопленник, после пожара. Царапины, подкрас, мелкие косяки — false.
- for_parts: true — на запчасти, на разбор, без документов.
- not_running: true — не на ходу, не заводится, мотор или коробка требуют ремонта.
- price_not_full: true — цена не полная: первый взнос, рассрочка, кредит, аренда, цена за месяц, символическая цена.
- exchange: true — рассматривают обмен.
- summary: до 120 символов по-русски — главное для перекупщика (учёт, ДТП/покраска, срочность, обмен/рассрочка, минусы), без марки и цены. Нечего сказать — пустая строка."""

_model: str | None = AI_MODEL or None
_ollama: bool | None = None  # None — ещё не проверяли, Ollama ли это
_paused_until = 0.0


def available() -> bool:
    return AI_ENABLED and time.time() >= _paused_until


def _pause(reason) -> None:
    global _paused_until
    _paused_until = time.time() + PAUSE_SEC
    log.warning("ИИ недоступен (%s) — пауза %d мин, работаю без него", reason, PAUSE_SEC // 60)


def status_line() -> str:
    if not AI_ENABLED:
        return "🤖 ИИ по описаниям: выключен (AI_ENABLED=false)"
    state = "недоступен, повторю позже" if not available() else "включён"
    return f"🤖 ИИ по описаниям: {state} — {_model or 'модель выберу сам'} ({AI_BASE_URL})"


# ---------- факты и вердикты ----------

def _text(row: ArchivedListing) -> str:
    return f"{row.title or ''}\n{row.description or ''}".strip()


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def facts_of(row: ArchivedListing) -> dict | None:
    """Факты, если они прочитаны из ТЕКУЩЕГО описания."""
    if row.ai_facts is None or row.ai_hash != _hash(_text(row)):
        return None
    return row.ai_facts


def pending(row: ArchivedListing) -> bool:
    """ИИ доступен, а объявление ещё не прочитано."""
    readable = bool((row.description or "").strip()) or row.source_site == "mashina"
    return available() and readable and facts_of(row) is None


def _condition_problem(f: dict) -> str | None:
    if f.get("for_parts"):
        return "на запчасти"
    if f.get("not_running"):
        return "не на ходу"
    if f.get("severe_damage"):
        return "битая"
    return None


def _price_problem(f: dict) -> str | None:
    return "цена не полная (взнос / рассрочка / аренда)" if f.get("price_not_full") else None


def _location_problem(f: dict) -> str | None:
    if f.get("in_transit"):
        return "в пути / под заказ"
    if f.get("in_kg") is False:
        return "не в Кыргызстане"
    if f.get("customs_cleared") is False:
        return "не растаможена"
    reg = (f.get("reg_country") or "").lower()
    if reg and "кыргыз" not in reg and "кр" != reg and "не стоит" not in reg:
        return f"учёт: {f['reg_country']}"
    return None


def market_problem(row: ArchivedListing) -> str | None:
    """Почему машину нельзя сравнивать с рынком КР (топ, средняя цена, /pricecheck)."""
    f = facts_of(row)
    if not f:
        return None
    return _condition_problem(f) or _price_problem(f) or _location_problem(f)


def market_ok(row: ArchivedListing) -> bool:
    return market_problem(row) is None


def request_problem(row: ArchivedListing, req_reg_country: str | None) -> str | None:
    """Почему находку не стоит присылать по поисковому запросу."""
    f = facts_of(row)
    if not f:
        return None
    problem = _condition_problem(f) or _price_problem(f)
    if problem or not req_reg_country:
        return problem
    reg = f.get("reg_country")
    if reg and req_reg_country.lower() not in reg.lower():
        return f"учёт: {reg}"
    if "кыргыз" in req_reg_country.lower():
        return _location_problem(f)
    return None


def summary_of(row: ArchivedListing) -> str | None:
    f = facts_of(row)
    return (f or {}).get("summary") or None


# ---------- чтение ----------

def _as_bool(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "да", "yes")


def _as_tristate(v) -> bool | None:
    if v is None or str(v).strip().lower() in ("null", "none", ""):
        return None
    return _as_bool(v)


def _clean(item: dict) -> dict:
    reg = item.get("reg_country")
    reg = reg.strip() if isinstance(reg, str) else None
    if reg and reg.lower() in ("null", "none", "нет", "неизвестно"):
        reg = None
    facts = {"reg_country": reg or None}
    for key in ("in_kg", "customs_cleared"):
        facts[key] = _as_tristate(item.get(key))
    for key in ("in_transit", "severe_damage", "for_parts", "not_running", "price_not_full", "exchange"):
        facts[key] = _as_bool(item.get(key))
    summary = item.get("summary")
    facts["summary"] = summary.strip()[:160] if isinstance(summary, str) else ""
    return facts


def _root_url() -> str:
    base = AI_BASE_URL.rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_SEC,
                             headers={"Authorization": f"Bearer {AI_API_KEY}"})


async def _pick_model(client: httpx.AsyncClient) -> str:
    """Модель из AI_MODEL или первая из Ollama; заодно узнаём, Ollama ли это."""
    global _model, _ollama
    if _model and _ollama is not None:
        return _model
    try:
        resp = await client.get(_root_url() + "/api/tags")  # список моделей Ollama
        resp.raise_for_status()
        names = [m.get("name") for m in resp.json().get("models", []) if m.get("name")]
        _ollama = True
    except (httpx.HTTPError, ValueError):
        if not _model:
            raise
        _ollama, names = False, []  # не Ollama (Groq и т.п.) — модель задана явно
    if not _model:
        names = [n for n in names if "embed" not in n.lower()]
        if not names:
            raise RuntimeError("в Ollama нет ни одной модели — ollama pull llama3.1")
        _model = names[0]
        log.info("ИИ: выбрал модель %s", _model)
    return _model


async def _complete(client: httpx.AsyncClient, model: str, system: str,
                    payload: list, schema: dict, n: int) -> dict:
    user = json.dumps(payload, ensure_ascii=False)
    started = time.monotonic()
    if _ollama:
        # Родной API Ollama, а не /v1: окно контекста по умолчанию 2–4 тыс.
        # токенов — пачка русских описаний с инструкцией туда не влезала,
        # модель теряла инструкцию и отвечала мусором (пачки делились,
        # запросов становилось в разы больше). Здесь окно задаём сами,
        # модель держим в памяти между запросами и требуем JSON по схеме.
        resp = await client.post(_root_url() + "/api/chat", json={
            "model": model,
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "format": schema,
            "options": {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": 300 * n},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        })
        resp.raise_for_status()
        content = resp.json()["message"]["content"]
    else:
        resp = await client.post(AI_BASE_URL.rstrip("/") + "/chat/completions", json={
            "model": model,
            "temperature": 0,
            "max_tokens": 300 * n,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        })
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
    log.info("ИИ: %d объявл. за %.0f с", n, time.monotonic() - started)
    return json.loads(content)


async def _read_items(client: httpx.AsyncClient, model: str, system: str,
                      rows: list[ArchivedListing], schema: dict, clean) -> dict[int, dict]:
    posts = [
        {"id": str(r.id), "title": r.title or "", "price_usd": r.price_usd,
         "description": (r.description or "")[:DESC_LIMIT]}
        for r in rows
    ]
    try:
        data = await _complete(client, model, system, posts, schema, len(rows))
        items = data.get("items") or []
        by_id = {int(i["id"]): clean(i) for i in items if isinstance(i, dict) and "id" in i}
    except (KeyError, IndexError, TypeError, ValueError) as e:
        if len(rows) == 1:
            log.warning("ИИ: не разобрал ответ по объявлению %s: %s", rows[0].id, e)
            return {}
        mid = len(rows) // 2  # ответ на пачку сломан — делим пополам
        return {**await _read_items(client, model, system, rows[:mid], schema, clean),
                **await _read_items(client, model, system, rows[mid:], schema, clean)}
    missing = [r for r in rows if r.id not in by_id]
    if missing and len(missing) < len(rows):
        by_id.update(await _read_items(client, model, system, missing, schema, clean))
    return by_id


def _items_schema(props: dict) -> dict:
    return {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, **props},
            "required": ["id", *props],
        }}},
        "required": ["items"],
    }


_NULLABLE_STR = {"type": ["string", "null"]}
_NULLABLE_BOOL = {"type": ["boolean", "null"]}
FACTS_SCHEMA = _items_schema({
    "reg_country": _NULLABLE_STR, "in_kg": _NULLABLE_BOOL,
    "in_transit": {"type": "boolean"}, "customs_cleared": _NULLABLE_BOOL,
    "severe_damage": {"type": "boolean"}, "for_parts": {"type": "boolean"},
    "not_running": {"type": "boolean"}, "price_not_full": {"type": "boolean"},
    "exchange": {"type": "boolean"}, "summary": {"type": "string"},
})


def _save(rows: list[ArchivedListing]) -> None:
    with get_session() as s:
        for r in rows:
            row = s.get(ArchivedListing, r.id)
            if row is None:
                continue
            for attr in ("description", "country", "availability", "characteristics",
                         "ai_facts", "ai_hash", "ai_tags"):
                setattr(row, attr, getattr(r, attr))
        s.commit()


async def _fetch_mashina_description(r: ArchivedListing) -> bool:
    """У карточек mashina описания нет — берём со страницы объявления.
    Заодно там точные «Учёт» и «Наличие в Кыргызстане»."""
    raw = await mashina.fetch_details(
        RawListing(source_site="mashina", external_id=r.external_id, url=r.url, title=r.title or "")
    )
    chars = raw.extra.get("characteristics")
    if not chars:
        return False  # страница не загрузилась — попробуем в другой раз
    r.characteristics = chars
    r.description = raw.description or r.description
    r.country = raw.country or r.country
    r.availability = raw.extra.get("availability") or r.availability
    return True


async def analyze(rows: list[ArchivedListing], budget: int | None = None) -> int:
    """Читает описания ещё не прочитанных объявлений. Меняет и переданные
    объекты, и базу. Возвращает, сколько прочитано."""
    if not available():
        return 0
    todo = [r for r in rows if pending(r)]
    if budget is not None:
        todo = todo[:budget]
    if not todo:
        return 0

    ready = []
    for r in todo:
        if r.source_site == "mashina" and not r.characteristics:
            if not await _fetch_mashina_description(r):
                continue
        if not (r.description or "").strip():
            r.ai_facts, r.ai_hash = {"empty": True}, _hash(_text(r))
        ready.append(r)

    done = 0
    async with _client() as client:
        try:
            model = await _pick_model(client)
        except Exception as e:
            _pause(e)
            _save(ready)
            return 0
        to_read = [r for r in ready if r.ai_facts is None or r.ai_facts.get("empty") is not True]
        for i in range(0, len(to_read), BATCH):
            chunk = to_read[i:i + BATCH]
            try:
                facts = await _read_items(client, model, SYSTEM_PROMPT, chunk, FACTS_SCHEMA, _clean)
            except httpx.HTTPError as e:
                _pause(e)
                break
            for r in chunk:
                if r.id in facts:
                    r.ai_facts, r.ai_hash = facts[r.id], _hash(_text(r))
                    done += 1
            _save(chunk)
    _save([r for r in ready if (r.ai_facts or {}).get("empty")])
    if done:
        log.info("ИИ: прочитано описаний — %d", done)
    return done


async def review_cheapest(groups: dict, stored_ids: set[int], window: int, budget: int) -> int:
    """Перед пересчётом топов читаем самых дешёвых в каждой группе. Сначала
    тех, кто в топ ещё не входил (иначе о них ушло бы уведомление без проверки),
    потом уже сидящих в топе."""
    entering, sitting = [], []
    for listings in groups.values():
        for r in sorted(listings, key=lambda l: l.price_usd)[:window]:
            if pending(r):
                (sitting if r.id in stored_ids else entering).append(r)
    return await analyze(entering + sitting, budget)


# ---------- теги в /pricecheck ----------
# «корея, limited» — ИИ сам решает по описанию (да/нет/не сказано), неясные
# не подходят. «страна: корея, комплектация: limited» — ИИ достаёт значение
# поля; если в описании его нет, бот спрашивает продавца в чате площадки
# (matching/seller_questions.py). Всё хранится в listings_archive.ai_tags:
# вычитанное ИИ — до изменения описания, ответы продавца — навсегда.

KEY_HINTS = {
    "страна": "откуда пригнана машина — страна происхождения (не учёт), по-русски: Корея, Япония, США…",
    "откуда": "откуда пригнана машина — страна происхождения, по-русски",
    "учет": "страна учёта (регистрации)",
    "учёт": "страна учёта (регистрации)",
    "комплектация": "название комплектации / версии (Limited, Prestige, Base…)",
    "цвет": "цвет кузова",
}
def parse_tags(text: str | None) -> tuple[list[str], dict[str, str]]:
    """«корея, страна: корея, комплектация: limited» → (["корея"], {"страна": "корея", ...})."""
    plain, keyed = [], {}
    for part in re.split(r"[,;\n]", text or ""):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            key, value = (x.strip() for x in part.split(":", 1))
            if key and value:
                keyed[key.lower()] = value
                continue
        plain.append(part.lower())
    return plain, keyed


def _words(s: str) -> list[str]:
    s = mashina.translit(s.replace("ё", "е"))
    return [w for w in re.split(r"[^\w]+", s) if w]


def _word_like(a: str, b: str) -> bool:
    return a in b or b in a or SequenceMatcher(None, a, b).ratio() >= 0.8


def same_value(wanted: str, actual: str) -> bool:
    """«корея» ~ «Южная Корея» ~ «Korea», «limited» ~ «Лимитед»: каждое слово
    искомого похоже на какое-то слово значения (после транслита, с допуском
    на разное написание — koreya/korea)."""
    want, have = _words(wanted), _words(actual)
    return bool(want and have) and all(any(_word_like(w, h) for h in have) for w in want)


PERMANENT_SOURCES = ("seller", "user")


def _tag_entry(row: ArchivedListing, name: str) -> dict | None:
    e = (row.ai_tags or {}).get(name)
    if not e:
        return None
    # ответ продавца — навсегда; вычитанное ИИ — пока не поменялось описание
    if e.get("src") in PERMANENT_SOURCES or e.get("h") == _hash(_text(row)):
        return e
    return None


def tag_match(row: ArchivedListing, plain: list[str], keyed: dict[str, str]) -> tuple[str, list[str]]:
    """('yes' | 'no' | 'unclear', поля «ключ: значение», которых нет в описании).
    unclear — показываем, но не считаем в статистике; по missing спрашиваем продавца."""
    unclear = False
    for t in plain:
        v = (_tag_entry(row, "t:" + t) or {}).get("v")
        if v == "no":
            return "no", []
        unclear = unclear or v != "yes"
    missing = []
    for key, wanted in keyed.items():
        value = (_tag_entry(row, "a:" + key) or {}).get("v")
        if not value:
            missing.append(key)
        elif not same_value(wanted, value):
            return "no", []
    return ("unclear", missing) if unclear or missing else ("yes", [])


def _tags_prompt(plain: list[str], keys: list[str]) -> str:
    lines = ["Ты читаешь объявления о продаже автомобилей. Отвечай ТОЛЬКО по тексту "
             "объявления (заголовок и описание), ничего не додумывай."]
    if plain:
        lines.append("\nПризнаки — для каждого yes (из текста ясно, что это так), "
                     "no (ясно, что нет) или unknown (не сказано):")
        lines += [f"t{i}: «{t}»" for i, t in enumerate(plain, 1)]
    if keys:
        lines.append("\nПоля — значение из текста или null, если об этом не сказано:")
        lines += [f"a{i}: {k} — {KEY_HINTS.get(k, k)}" for i, k in enumerate(keys, 1)]
    lines.append('\nОтвет — JSON {"items": [{"id": "...", ...}]}, по элементу на каждое объявление.')
    return "\n".join(lines)


async def read_tags(rows: list[ArchivedListing], plain: list[str], keys: list[str],
                    progress=None) -> None:
    """Дочитывает теги по объявлениям, где ответа ещё нет. progress(done, total)."""
    if not available() or not (plain or keys):
        return
    todo = [r for r in rows
            if any(_tag_entry(r, "t:" + t) is None for t in plain)
            or any(_tag_entry(r, "a:" + k) is None for k in keys)]
    if not todo:
        return
    for r in todo:
        if r.source_site == "mashina" and not r.characteristics:
            await _fetch_mashina_description(r)

    def store(r: ArchivedListing, item: dict) -> None:
        tags = dict(r.ai_tags or {})
        h = _hash(_text(r))
        for i, t in enumerate(plain, 1):
            v = str(item.get(f"t{i}") or "unknown").lower()
            tags["t:" + t] = {"v": v if v in ("yes", "no") else "unknown", "src": "ai", "h": h}
        for i, k in enumerate(keys, 1):
            if (tags.get("a:" + k) or {}).get("src") in PERMANENT_SOURCES:
                continue  # ответ продавца важнее
            v = item.get(f"a{i}")
            v = v.strip() if isinstance(v, str) and v.strip().lower() not in ("null", "none", "") else None
            tags["a:" + k] = {"v": v, "src": "ai", "h": h}
        r.ai_tags = tags

    no_text = [r for r in todo if not (r.description or "").strip()]
    for r in no_text:
        store(r, {})  # описания нет — признаки «не сказано», поля спросим
    readable = [r for r in todo if (r.description or "").strip()]
    schema = _items_schema({
        **{f"t{i}": {"type": "string", "enum": ["yes", "no", "unknown"]} for i in range(1, len(plain) + 1)},
        **{f"a{i}": _NULLABLE_STR for i in range(1, len(keys) + 1)},
    })
    system = _tags_prompt(plain, keys)
    async with _client() as client:
        try:
            model = await _pick_model(client)
        except Exception as e:
            _pause(e)
            _save(no_text)
            return
        for i in range(0, len(readable), BATCH):
            chunk = readable[i:i + BATCH]
            try:
                items = await _read_items(client, model, system, chunk, schema, lambda x: x)
            except httpx.HTTPError as e:
                _pause(e)
                break
            for r in chunk:
                if r.id in items:
                    store(r, items[r.id])
            _save(chunk)
            if progress:
                await progress(min(i + BATCH, len(readable)), len(readable))
    _save(no_text)


async def extract_from_reply(title: str, question: str, reply: str,
                             keys: list[str]) -> dict[str, str | None] | None:
    """Значения полей из ответа продавца. None — ИИ недоступен."""
    if not available():
        return None
    system = (
        "Покупатель спросил продавца автомобиля о машине, продавец ответил. "
        "Извлеки из ОТВЕТА ПРОДАВЦА значения полей. Не сказано — null.\n"
        + "\n".join(f"a{i}: {k} — {KEY_HINTS.get(k, k)}" for i, k in enumerate(keys, 1))
        + '\nОтвет — JSON {"items": [{"id": "1", ...}]}.'
    )
    payload = [{"id": "1", "title": title, "question": question, "seller_reply": reply[:DESC_LIMIT]}]
    schema = _items_schema({f"a{i}": _NULLABLE_STR for i in range(1, len(keys) + 1)})
    async with _client() as client:
        try:
            model = await _pick_model(client)
            data = await _complete(client, model, system, payload, schema, 1)
        except (httpx.HTTPError, RuntimeError) as e:
            _pause(e)
            return None
        except (KeyError, ValueError) as e:
            log.warning("ИИ: не разобрал ответ продавца: %s", e)
            return {k: None for k in keys}
    item = (data.get("items") or [{}])[0]
    out = {}
    for i, k in enumerate(keys, 1):
        v = item.get(f"a{i}")
        out[k] = v.strip() if isinstance(v, str) and v.strip().lower() not in ("null", "none", "") else None
    return out


def save_answers(row: ArchivedListing, answers: dict[str, str], src: str = "seller") -> None:
    tags = dict(row.ai_tags or {})
    for key, value in answers.items():
        if value:
            tags["a:" + key] = {"v": value, "src": src}
    row.ai_tags = tags
    with get_session() as s:
        db_row = s.get(ArchivedListing, row.id)
        if db_row is not None:
            db_row.ai_tags = tags
            s.commit()


def rows_for(keys: list[tuple[str, str]]) -> dict[tuple[str, str], ArchivedListing]:
    ext_ids = list({k[1] for k in keys})
    with get_session() as s:
        rows = s.execute(
            select(ArchivedListing).where(ArchivedListing.external_id.in_(ext_ids))
        ).scalars()
        wanted = set(keys)
        return {(r.source_site, r.external_id): r for r in rows
                if (r.source_site, r.external_id) in wanted}
