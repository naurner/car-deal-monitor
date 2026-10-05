"""Ключевое слово в контексте: «из России» ≠ «не проходной на Россию».

1. Правило: каждое вхождение слова смотрим в его части предложения. Рядом
   нет отрицания («не», «нет», «без», «кроме»…) — подходит сразу, без ИИ.
2. Отрицание рядом есть («не РФ», «не россиянин», «на Россию не проходит»)
   — решает ИИ по тексту объявления. Вердикт кэшируется в ai_tags
   («k:<слова слежения>», с хэшем текста — поменялось описание, перечитаем).
3. ИИ недоступен — по правилу: все вхождения в отрицании → не подходит.

Слова ищутся с начала слова: «РФ» не находится внутри «шарф».
"""

import asyncio
import logging
import re

import httpx
from sqlalchemy import delete, select

from db.models import ArchivedListing, KeywordSeen, KeywordWatch
from db.session import get_session
from matching import ai_reader

log = logging.getLogger(__name__)

NEGATORS = {"не", "нет", "нету", "без", "ни", "кроме", "not", "no", "non"}
WORDS_BEFORE, WORDS_AFTER = 3, 2
SNIPPET = 90                 # символов вокруг вхождения — подсказка ИИ
REVIEW_LIMIT = 5000          # кандидатов из архива на одно слежение
_CLAUSE_END = re.compile(r"[.!?;\n,()«»\"]")
_WORD = re.compile(r"[a-zа-яё0-9]+")
_CHAR = "a-zа-яё0-9"


def _pattern(kw: str) -> re.Pattern:
    k = kw.lower().strip()
    pat = rf"(?<![{_CHAR}]){re.escape(k)}"
    if len(k) <= 3:  # «РФ», «РК» — только целым словом
        pat += rf"(?![{_CHAR}])"
    return re.compile(pat)


def text_of(row) -> str:
    return f"{row.title or ''}\n{row.description or ''}"


def occurrences(text: str, keywords: list[str]) -> list[tuple[bool, str]]:
    """[(в отрицании?, фрагмент вокруг)] по всем вхождениям всех слов."""
    t = text.lower()
    out = []
    for kw in keywords:
        if not kw.strip():
            continue
        for m in _pattern(kw).finditer(t):
            left = t[:m.start()]
            cut = max((x.end() for x in _CLAUSE_END.finditer(left)), default=0)
            right = t[m.end():]
            end = next((x.start() for x in _CLAUSE_END.finditer(right)), len(right))
            near = (_WORD.findall(left[cut:])[-WORDS_BEFORE:]
                    + _WORD.findall(right[:end])[:WORDS_AFTER])
            snippet = text[max(0, m.start() - SNIPPET):m.end() + SNIPPET].replace("\n", " ")
            out.append((any(w in NEGATORS for w in near), snippet))
    return out


def rule_verdict(text: str, keywords: list[str]) -> str | None:
    """None — слова нет; 'yes' — есть без отрицаний; 'check' — нужен ИИ."""
    occ = occurrences(text, keywords)
    if not occ:
        return None
    return "check" if any(neg for neg, _ in occ) else "yes"


def _cache_key(keywords: list[str]) -> str:
    return "k:" + "|".join(sorted({k.lower().strip() for k in keywords if k.strip()}))


def _cached(row, keywords: list[str]) -> str | None:
    e = ai_reader._tag_entry(row, _cache_key(keywords))
    return e.get("v") if e else None


def matches(row, keywords: list[str]) -> bool:
    """Подходит ли объявление (по кэшу ИИ, без него — по правилу)."""
    text = text_of(row)
    rule = rule_verdict(text, keywords)
    if rule is None:
        return False
    if rule == "yes":
        return True
    cached = _cached(row, keywords)
    if cached in ("yes", "no"):
        return cached == "yes"
    # ИИ ещё не читал: подходит, только если есть вхождение без отрицания
    return any(not neg for neg, _ in occurrences(text, keywords))


def _system(keywords: list[str]) -> str:
    topic = " / ".join(keywords)
    return (
        "Ты читаешь объявления о продаже автомобилей в Кыргызстане. Пользователь "
        f"следит за объявлениями по теме: «{topic}». Для каждого объявления реши match:\n"
        "true — объявление действительно про это, это утверждается (например «из "
        "России», «учёт РФ», «проходной на Россию»);\n"
        "false — слово упомянуто в отрицании или в другом смысле (например «не "
        "проходной на Россию», «не РФ», «не россиянин», «кроме России», «в Россию "
        "не проходит»).\n"
        "fragments — места, где встречается слово. Отвечай только по тексту, ничего "
        'не додумывай. Ответ — JSON {"items": [{"id": "...", "match": true|false}]}, '
        "по элементу на каждое объявление."
    )


_SCHEMA = ai_reader._items_schema({"match": {"type": "boolean"}})


async def judge(rows: list[ArchivedListing], keywords: list[str], progress=None) -> int:
    """Дочитывает ИИ спорные (слово рядом с отрицанием). Возвращает, сколько
    прочитано. progress(done, total) — для сообщения о ходе работы."""
    key = _cache_key(keywords)
    todo = [r for r in rows if rule_verdict(text_of(r), keywords) == "check"
            and _cached(r, keywords) is None]
    if not todo or not ai_reader.available():
        return 0
    done = 0
    system = _system(keywords)
    async with ai_reader._client() as client:
        try:
            model = await ai_reader._pick_model(client)
        except Exception as e:
            ai_reader._pause(e)
            return 0
        for i in range(0, len(todo), ai_reader.BATCH):
            chunk = todo[i:i + ai_reader.BATCH]
            posts = [{"id": str(r.id), "title": r.title or "",
                      "fragments": [s for _, s in occurrences(text_of(r), keywords)][:4],
                      "description": (r.description or "")[:ai_reader.DESC_LIMIT]}
                     for r in chunk]
            try:
                data = await ai_reader._complete(client, model, system, posts, _SCHEMA, len(chunk))
                verdicts = {int(it["id"]): bool(it.get("match")) for it in data.get("items") or []
                            if isinstance(it, dict) and "id" in it}
            except httpx.HTTPError as e:
                ai_reader._pause(e)
                break
            except (KeyError, TypeError, ValueError) as e:
                log.warning("ИИ: не разобрал ответ по ключевым словам: %s", e)
                continue
            for r in chunk:
                if r.id in verdicts:
                    tags = dict(r.ai_tags or {})
                    tags[key] = {"v": "yes" if verdicts[r.id] else "no", "src": "ai",
                                 "h": ai_reader._hash(ai_reader._text(r))}
                    r.ai_tags = tags
            ai_reader._save(chunk)
            done += len(chunk)
            if progress:
                await progress(min(i + ai_reader.BATCH, len(todo)), len(todo))
    log.info("Ключевые слова «%s»: ИИ прочитал %d спорных объявлений", "/".join(keywords), done)
    return done


def pending_count(rows: list[ArchivedListing], keywords: list[str]) -> int:
    return sum(1 for r in rows if rule_verdict(text_of(r), keywords) == "check"
               and _cached(r, keywords) is None)


def candidates(keywords: list[str]) -> list[ArchivedListing]:
    """Объявления архива, где слова встречаются (с проверкой границы слова)."""
    from matching import archive

    seen: dict[int, ArchivedListing] = {}
    for kw in keywords:
        for r in archive.search_archive(keyword=kw, limit=REVIEW_LIMIT):
            seen.setdefault(r.id, r)
    return [r for r in seen.values() if rule_verdict(text_of(r), keywords) is not None]


async def review_watch(watch: KeywordWatch, progress=None,
                       rows: list[ArchivedListing] | None = None) -> tuple[list[ArchivedListing], int]:
    """Перепроверка всего найденного по слежению: ИИ дочитывает спорные,
    из отправленных (KeywordSeen) убираются неподходящие.
    Возвращает (подходящие объявления, сколько отсеяно)."""
    if rows is None:
        rows = await asyncio.to_thread(candidates, watch.keywords)
    await judge(rows, watch.keywords, progress)
    good = [r for r in rows if matches(r, watch.keywords)]
    bad_ids = [r.id for r in rows if r.id not in {g.id for g in good}]
    if bad_ids:
        with get_session() as s:
            s.execute(delete(KeywordSeen).where(KeywordSeen.watch_id == watch.id,
                                                KeywordSeen.archive_id.in_(bad_ids)))
            s.commit()
    good.sort(key=lambda r: (not r.is_active, -r.last_seen_at.timestamp()))
    return good, len(bad_ids)


async def review_all() -> None:
    """При старте: перепроверить найденное по всем слежениям (чистка старых
    ложных совпадений). Вердикты кэшируются — повторно ИИ не читает."""
    with get_session() as s:
        watches = list(s.execute(select(KeywordWatch)).scalars())
    for w in watches:
        try:
            good, removed = await review_watch(w)
            log.info("Слежение #%s: подходит %d, отсеяно %d", w.id, len(good), removed)
        except Exception:
            log.exception("перепроверка слежения %s", w.id)
