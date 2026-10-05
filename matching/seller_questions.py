"""Вопросы продавцам по тегам /pricecheck.

Если по тегу «ключ: значение» (страна, комплектация…) из описания не ясно,
бот сам пишет продавцу в чат площадки от аккаунта владельца, раз в 10 минут
проверяет ответы, ИИ вытаскивает из ответа значения, и пользователю приходит
уведомление: подходит объявление или нет.

Площадки банят за массовые одинаковые сообщения — а это аккаунт, через
который идут объявления и поднятия владельца. Поэтому: не больше
MAX_PER_SEARCH за один поиск и MAX_PER_DAY в сутки, паузы между
сообщениями, одному объявлению — никогда дважды.
Пока только lalafo: чат mashina ещё не разобран.
"""

import asyncio
import logging
import random
from datetime import datetime, timedelta

from aiogram import Bot
from sqlalchemy import func, select

from db.models import ArchivedListing, SellerAsk
from db.session import get_session
from matching import ai_reader
from posting.lalafo_chat import LalafoChat, SessionExpired, ad_id_from_url

log = logging.getLogger(__name__)

MAX_PER_SEARCH = 10
MAX_PER_DAY = 30
PAUSE_SEC = (25, 50)      # между сообщениями — не как бот
EXPIRE_HOURS = 72         # дольше не ждём ответа
SITES = ("lalafo",)

QUESTIONS = {
    "страна": "откуда пригнана машина",
    "откуда": "откуда пригнана машина",
    "комплектация": "какая комплектация",
    "учет": "на каком учёте машина",
    "учёт": "на каком учёте машина",
    "цвет": "какой цвет",
}

_send_lock = asyncio.Lock()


def _short_title(title: str | None) -> str:
    # lalafo: «Toyota Camry: 2018 г., 2.5 л, …» → «Toyota Camry 2018 г.»
    head, _, rest = (title or "").partition(":")
    year = rest.split(",")[0].strip()
    return f"{head.strip()} {year}".strip() if year else head.strip()


def question_text(title: str | None, keys: list[str]) -> str:
    parts = [QUESTIONS.get(k, k) for k in keys]
    asks = ", ".join(parts[:-1]) + (" и " if len(parts) > 1 else "") + parts[-1]
    return f"Здравствуйте! По вашему объявлению {_short_title(title)}: {asks}? Спасибо!"


def asked(archive_ids: list[int]) -> dict[int, SellerAsk]:
    with get_session() as s:
        return {a.archive_id: a for a in s.execute(
            select(SellerAsk).where(SellerAsk.archive_id.in_(archive_ids))).scalars()}


def _used_today() -> int:
    since = datetime.utcnow() - timedelta(hours=24)
    with get_session() as s:
        return s.execute(select(func.count()).select_from(SellerAsk).where(
            SellerAsk.status != "failed", SellerAsk.created_at >= since)).scalar_one()


def enqueue(candidates: list[tuple[ArchivedListing, list[str]]], wanted: dict[str, str],
            label: str, chat_id: int) -> tuple[list[int], str | None]:
    """Ставит вопросы в очередь (самые дешёвые первыми). Возвращает id объявлений
    в очереди и причину, почему не всем (лимит), если такая есть."""
    known = asked([r.id for r, _ in candidates])
    budget = min(MAX_PER_SEARCH, MAX_PER_DAY - _used_today())
    queued, note = [], None
    with get_session() as s:
        for r, missing in candidates:
            if r.source_site not in SITES or r.id in known:
                continue
            if len(queued) >= budget:
                note = ("лимит сообщений на сегодня" if budget < MAX_PER_SEARCH
                        else f"не больше {MAX_PER_SEARCH} за поиск")
                break
            s.add(SellerAsk(
                archive_id=r.id, site=r.source_site, ad_url=r.url, keys=missing,
                wanted={k: wanted[k] for k in missing}, label=label, chat_id=chat_id,
                question=question_text(r.title, missing),
            ))
            queued.append(r.id)
        s.commit()
    return queued, note


async def send_queued(bot: Bot) -> None:
    """Отправляет очередь. Один отправитель за раз (общий браузерный профиль)."""
    if _send_lock.locked():
        return
    async with _send_lock:
        with get_session() as s:
            queue = list(s.execute(select(SellerAsk).where(SellerAsk.status == "queued")
                                   .order_by(SellerAsk.id)).scalars())
        if not queue:
            return
        failed: dict[int, list[str]] = {}
        try:
            async with LalafoChat() as chat:
                for i, ask in enumerate(queue):
                    if i:
                        await asyncio.sleep(random.uniform(*PAUSE_SEC))
                    try:
                        ok = await chat.send(ask.ad_url, ask.question)
                    except SessionExpired:
                        raise
                    except Exception as e:
                        log.exception("seller ask %s", ask.id)
                        ok, err = False, str(e)[:200]
                    else:
                        err = None if ok else "сообщение не отправилось (чат закрыт или изменился сайт)"
                    _update(ask.id, status="sent" if ok else "failed", error=err,
                            sent_at=datetime.utcnow() if ok else None)
                    if not ok:
                        failed.setdefault(ask.chat_id, []).append(ask.ad_url)
        except SessionExpired as e:
            with get_session() as s:
                for ask in s.execute(select(SellerAsk).where(SellerAsk.status == "queued")).scalars():
                    ask.status, ask.error = "failed", str(e)
                    failed.setdefault(ask.chat_id, []).append(ask.ad_url)
                s.commit()
            failed = {chat: [str(e)] for chat in failed}
        for chat_id, items in failed.items():
            await bot.send_message(chat_id, "⚠️ Не смог написать продавцам:\n" + "\n".join(items),
                                   disable_web_page_preview=True)


def _update(ask_id: int, **fields) -> None:
    with get_session() as s:
        ask = s.get(SellerAsk, ask_id)
        for k, v in fields.items():
            setattr(ask, k, v)
        s.commit()


async def check_replies(bot: Bot) -> None:
    """Раз в 10 минут: дослать очередь и проверить ответы продавцов."""
    await send_queued(bot)
    with get_session() as s:
        waiting = list(s.execute(select(SellerAsk).where(SellerAsk.status == "sent")).scalars())
    if not waiting:
        return
    now = datetime.utcnow()
    try:
        async with LalafoChat() as chat:
            threads = await chat.threads()
            for ask in waiting:
                replies = await chat.seller_replies(
                    ad_id_from_url(ask.ad_url), ask.sent_at.timestamp(), threads)
                if replies:
                    await _on_reply(bot, ask, "\n".join(replies))
                elif now - ask.sent_at > timedelta(hours=EXPIRE_HOURS):
                    _update(ask.id, status="expired")
    except SessionExpired as e:
        log.warning("проверка ответов продавцов: %s", e)


async def _on_reply(bot: Bot, ask: SellerAsk, reply: str) -> None:
    from bot import texts  # bot.texts импортирует matching — избегаем цикла

    with get_session() as s:
        row = s.get(ArchivedListing, ask.archive_id)
    values = await ai_reader.extract_from_reply(row.title if row else "", ask.question, reply, ask.keys)
    if values is None:
        return  # ИИ недоступен — разберём в следующий раз, ответ никуда не денется
    _update(ask.id, status="answered", answer=reply, answered_at=datetime.utcnow())
    if row is not None:
        ai_reader.save_answers(row, values)
    got = ", ".join(f"{k}: {values.get(k) or '—'}" for k in ask.keys)
    if all(values.get(k) for k in ask.keys):
        fits = all(ai_reader.same_value(ask.wanted[k], values[k]) for k in ask.keys)
        verdict = "✅ подходит под ваш поиск" if fits else "❌ не подходит"
    else:
        verdict = "❔ из ответа не понятно — посмотрите сами"
    card = texts.archived_listing(row) if row is not None else ask.ad_url
    await bot.send_message(
        ask.chat_id,
        f"💬 Продавец ответил ({ask.label})\n\n{card}\n\n"
        f"Вопрос: {ask.question}\nОтвет: «{reply[:500]}»\n→ {got} — {verdict}",
    )
