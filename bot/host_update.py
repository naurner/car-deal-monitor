"""Кнопка «Обновить с GitHub».

Бот живёт в контейнере без git и без доступа к Docker, поэтому обновляется
руками Мака: бот кладёт в общую папку data/ файл-запрос, launchd на Маке
видит его (WatchPaths в com.cardealtg.autopull.plist) и сразу запускает
mac-autopull.sh — тот тянет код, пересобирает контейнер и пишет итог в
data/update_result.txt. Итог забирает уже новый (или старый, если менять
нечего/сборка упала) экземпляр бота — периодической задачей в main.py.
"""

import json
import logging
import os
import time

from aiogram import Bot

from config import DATA_DIR, OWNER_TELEGRAM_ID

log = logging.getLogger(__name__)

REQUEST = DATA_DIR / "update_request.json"    # пишет бот, забирает Мак
INFLIGHT = DATA_DIR / "update_inflight.json"  # Мак переименовал запрос — в работе
RESULT = DATA_DIR / "update_result.txt"       # итог от Мака: key=value по строкам
REQUEST_STALE_SEC = 420  # таймер автообновления — раз в 5 минут, плюс запас

VERSION = os.getenv("GIT_COMMIT", "unknown")


def request_update(chat_id: int, message_id: int) -> bool:
    """False — обновление уже запрошено и ещё не завершилось."""
    if REQUEST.exists() or INFLIGHT.exists():
        return False
    REQUEST.write_text(json.dumps({
        "chat_id": chat_id, "message_id": message_id, "requested_at": time.time(),
    }))
    return True


def _read_json(path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _read_result() -> dict | None:
    try:
        lines = RESULT.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    return dict(line.split("=", 1) for line in lines if "=" in line)


async def _say(bot: Bot, target: dict | None, text: str) -> None:
    chat_id = (target or {}).get("chat_id") or OWNER_TELEGRAM_ID
    message_id = (target or {}).get("message_id")
    if message_id:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
            return
        except Exception:
            pass  # сообщение слишком старое или удалено — пришлём новое
    await bot.send_message(chat_id, text)


async def report(bot: Bot) -> None:
    result = _read_result()
    if result is None:
        pending = _read_json(REQUEST)
        if pending and time.time() - pending.get("requested_at", 0) > REQUEST_STALE_SEC:
            REQUEST.unlink(missing_ok=True)
            await _say(bot, pending,
                       "⚠️ Мак не забрал запрос на обновление за 7 минут.\n"
                       "Похоже, автообновление на Маке выключено — запусти там "
                       "mac-autopull-install.command.")
        return

    target = _read_json(INFLIGHT)
    status = result.get("status")
    if status == "updated":
        text = f"✅ Бот обновлён с GitHub: {result.get('from')} → {result.get('to')}. Уже работаю."
    elif status == "nochange":
        text = f"Уже последняя версия ({result.get('to') or VERSION})."
    else:
        text = f"❌ Обновление не удалось: {result.get('detail') or 'см. autopull.log на Маке'}"
    try:
        await _say(bot, target, text)
    finally:
        RESULT.unlink(missing_ok=True)
        INFLIGHT.unlink(missing_ok=True)
