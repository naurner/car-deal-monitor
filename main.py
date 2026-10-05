import asyncio
import atexit
import logging
import os
import sys
from datetime import datetime, timedelta

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.types import BotCommand, BotCommandScopeChat, Message, TelegramObject
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from sqlalchemy import select as sa_select

from bot import commands, host_update, texts, wizards
from bot.settings_store import get_setting, set_setting
from config import BASE_DIR, BOT_TOKEN, OWNER_TELEGRAM_ID, RUNNING_IN_DOCKER
from db.models import AllowedUser, PostedAd
from db.session import get_session, init_db
from matching import (archive, full_scan, keyword_context, keywords, market, matcher,
                      seller_questions, sellers, top_tracker, vehicle_ident)
from posting import manage as posting_manage

LOCK_FILE = BASE_DIR / "data" / "bot.lock"


def _acquire_single_instance_lock() -> None:
    """Не даёт запустить второй экземпляр бота одновременно с первым —
    два процесса одновременно лезут в один Chrome-профиль (посадка/логин)
    и портят сессию площадок. См. docs/PROJECT_LOG.md."""
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text().strip())
        except (ValueError, OSError):
            old_pid = None
        # в контейнере бот всегда PID 1: после жёсткой остановки (перезагрузка
        # Мака) в lock-файле остаётся "1" — это мы сами, а не второй экземпляр
        if old_pid and old_pid != os.getpid() and _pid_alive(old_pid):
            sys.exit(
                f"Бот уже запущен (PID {old_pid}). Закройте то окно перед "
                f"новым запуском — два одновременных процесса портят сессии "
                f"площадок и конфликтуют в Telegram."
            )
    LOCK_FILE.write_text(str(os.getpid()))
    atexit.register(lambda: LOCK_FILE.unlink(missing_ok=True))


def _pid_alive(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            import ctypes

            h = ctypes.windll.kernel32.OpenProcess(1, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        os.kill(pid, 0)
        return True
    except OSError:
        return False


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("main")

# Видимый список команд ("/"-подсказка Telegram) — намеренно короткий.
# Всё остальное (поиск, архив, ключевые слова, цены, постинг...) открывается
# через /menu → категория → кнопка, а не запоминанием десятков команд.
# Сами обработчики команд (/add_search и т.п.) продолжают работать, если
# их набрать руками — просто не подсказываются в автодополнении.
PUBLIC_COMMANDS = [
    BotCommand(command="menu", description="🏠 Главное меню"),
    BotCommand(command="cancel", description="❌ Отменить текущий мастер"),
]

# Постинг/логин/управление пользователями работают ТОЛЬКО у владельца
# (управляет его аккаунтами на площадках; остальные не должны знать о функции),
# но в подсказку "/" не выводятся тоже — доступ через 📤 Мои объявления в /menu.
OWNER_COMMANDS = PUBLIC_COMMANDS

OWNER_ONLY_COMMANDS = (
    "/post", "/my_ads", "/adopt", "/login", "/login_status",
    "/users", "/adduser", "/deluser", "/backup", "/ban", "/banned",
)
OWNER_ONLY_CALLBACKS = (
    "cat:posting", "menu:post", "menu:my_ads", "menu:adopt", "menu:login",
    "menu:users", "adbump:", "addel:", "adup:", "login:", "host:", "pubad:", "pubcancel:",
    "adphoto", "ban:", "unban:", "menu:banned",
)

# Настройки — доступны владельцу и доверенным (/adduser), но не постинг/логин.
TRUSTED_COMMANDS = (
    "/set_interval", "/set_tolerance", "/set_margin", "/set_window",
    "/set_scan_interval",
)


def _is_owner(user_id: int) -> bool:
    return user_id == OWNER_TELEGRAM_ID


def _is_trusted(user_id: int) -> bool:
    if _is_owner(user_id):
        return True
    with get_session() as s:
        return s.get(AllowedUser, user_id) is not None


class AccessMiddleware(BaseMiddleware):
    """Открытый доступ всем; постинг/логин — только владельцу (скрыто и
    молча для остальных), настройки — доверенным пользователям."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user is None:
            return

        cmd = None
        if isinstance(event, Message) and event.text:
            cmd = event.text.split()[0].split("@")[0].lower()
        cb = event.data if hasattr(event, "data") and event.data else None

        owner_only = cmd in OWNER_ONLY_COMMANDS or (
            cb and any(cb.startswith(p) for p in OWNER_ONLY_CALLBACKS)
        )
        if owner_only and not _is_owner(user.id):
            # ничего не отвечаем — пользователь не должен даже заподозрить,
            # что такая функциональность существует
            return

        if cmd in TRUSTED_COMMANDS and not _is_trusted(user.id):
            await event.answer("⛔ Эта функция доступна только доверенным пользователям.")
            return

        return await handler(event, data)


async def scheduled_check(bot: Bot) -> None:
    log.info("Автопроверка площадок…")
    try:
        results = await matcher.check_all_active()
    except Exception:
        log.exception("Автопроверка упала")
        return
    for m in results:
        target = m.watch_request.user_id or OWNER_TELEGRAM_ID
        try:
            await bot.send_message(
                target,
                texts.match_notification(m),
                reply_markup=commands.notification_kb(m.watch_request, m.listing),
            )
        except Exception:
            log.exception("Не доставлено пользователю %s", target)
    log.info("Автопроверка завершена: %d новых совпадений", len(results))


async def scheduled_keywords(bot: Bot) -> None:
    window = int(get_setting("keyword_window_min"))
    try:
        matches = await keywords.check_keywords(window)
    except Exception:
        log.exception("Проверка ключевых слов упала")
        return
    for m in matches:
        target = m.watch.user_id or OWNER_TELEGRAM_ID
        try:
            await bot.send_message(target, texts.keyword_match(m.matched_keyword, m.listing),
                                   reply_markup=commands.ban_kb(m.listing) if target == OWNER_TELEGRAM_ID else None)
        except Exception:
            log.exception("Не доставлено пользователю %s", target)
    if matches:
        log.info("Ключевые слова: %d совпадений", len(matches))


async def scheduled_full_scan(bot: Bot) -> None:
    """Полное сканирование рынка (все марки/модели) + пересчёт топ-3 цен.
    Тяжёлая операция — интервал в часах (settings, /set_scan_interval)."""
    try:
        await full_scan.run()
    except Exception:
        log.exception("Полное сканирование рынка упало")
        return
    _mark_done("last_full_scan_at")
    try:  # новые объявления продавцов из чёрного списка — до пересчёта топов
        await sellers.refresh_all()
    except Exception:
        log.exception("чёрный список: обновление не удалось")
    try:
        events = await top_tracker.evaluate_all()
    except Exception:
        log.exception("Топ-трекер упал")
        return
    # самые выгодные минимумы — первыми; остальные одной строкой, а не
    # сотней сообщений (весь рынок — тысячи групп)
    events.sort(key=lambda ev: min(l.price_usd for l in ev.entered) / ev.avg_price
                if ev.entered and ev.avg_price else 1)
    shown, rest = events[:TOP_EVENTS_MAX], len(events) - TOP_EVENTS_MAX
    chunks = texts.batch_listings(shown, texts.top_event)
    if rest > 0 and chunks:
        chunks[-1] += f"\n\n…и ещё {rest} моделей с новым минимумом (не так сильно ниже средней)."
    for chunk in chunks:
        try:
            await bot.send_message(OWNER_TELEGRAM_ID, chunk, disable_web_page_preview=True)
        except Exception:
            log.exception("Не доставлено уведомление топ-трекера")
    if events:
        log.info("Топ-трекер: %d новых минимумов, отправлено %d сообщений", len(events), len(chunks))


TOP_EVENTS_MAX = 10  # «новых минимумов» за один полный скан


def _mark_done(key: str) -> None:
    set_setting(key, datetime.now().isoformat(timespec="seconds"))


def _first_run(key: str, hours: float) -> datetime:
    """Когда запустить редкую задачу после старта бота. Interval-триггер
    отсчитывает интервал от старта — а бот перезапускается при каждом
    обновлении, так что 6-часовой скан и суточные поднятия не случались бы
    никогда. Считаем от последнего реального запуска; просрочено — через
    пару минут после старта."""
    now = datetime.now()
    try:
        due = datetime.fromisoformat(get_setting(key)) + timedelta(hours=hours)
    except ValueError:
        due = now
    return due if due > now else now + timedelta(minutes=2)


async def scheduled_actualize() -> None:
    try:
        await archive.actualize()
    except Exception:
        log.exception("Актуализация архива упала")
        return
    _mark_done("last_actualize_at")


async def scheduled_bumps(bot: Bot) -> None:
    """Ежесуточные поднятия (джиттер задаётся в настройке job'а)."""
    # отмечаем в начале: перезапуск посреди поднятий не должен поднять дважды
    _mark_done("last_bumps_at")
    with get_session() as s:
        ads = list(
            s.execute(
                sa_select(PostedAd).where(
                    PostedAd.is_active.is_(True),
                    PostedAd.bump_enabled.is_(True),
                )
            ).scalars()
        )
    if not ads:
        return
    log.info("Поднятия: %d объявлений", len(ads))
    mashina_done = False
    for ad in ads:
        # mashina: кулдаун 5 минут между поднятиями — разносим по времени
        if mashina_done and posting_manage.has_mashina(ad):
            await asyncio.sleep(330)
        try:
            results = await posting_manage.bump_ad(ad)
        except Exception:
            log.exception("поднятие объявления %s упало", ad.id)
            continue
        mashina_done = mashina_done or any(site == "mashina" for site, _ in results)
        for site, res in results:
            if res.ok:
                log.info("bump %s ad=%s: %s", site, ad.id, res.message)
                continue
            if res.transient:
                log.info("bump %s ad=%s кулдаун: %s", site, ad.id, res.message)
                continue
            prefix = "💳 ТРЕБУЕТСЯ ОПЛАТА" if res.payment_required else "⚠️ Поднятие не удалось"
            try:
                await bot.send_message(ad.user_id or OWNER_TELEGRAM_ID,
                                       f"{prefix}\nОбъявление #{ad.id}\n{res.message}")
            except Exception:
                log.exception("Не доставлено %s", ad.user_id)


async def scheduled_weekly_report(bot: Bot) -> None:
    """Раз в неделю — сводка по рынку владельцу (та же, что по кнопке)."""
    try:
        parts = await asyncio.to_thread(market.weekly_report)
    except Exception:
        log.exception("Недельный отчёт по рынку упал")
        return
    _mark_done("last_weekly_report_at")
    for chunk in texts.batch_listings(parts, lambda p: p, sep="\n\n"):
        try:
            await bot.send_message(OWNER_TELEGRAM_ID, chunk, disable_web_page_preview=True)
        except Exception:
            log.exception("Не доставлен недельный отчёт")


def _run_backfills() -> None:
    """Разовые переработки накопленного архива (в фоне, после старта)."""
    for key, job in ((vehicle_ident.BACKFILL_KEY, vehicle_ident.backfill_archive),
                     (market.BACKFILL_KEY, market.backfill_history)):
        if get_setting(key) == "done":
            continue
        try:
            job()
            set_setting(key, "done")
        except Exception:
            log.exception("Переработка архива %s упала — повторю при следующем старте", key)
            return


async def main() -> None:
    _acquire_single_instance_lock()
    if not BOT_TOKEN or not OWNER_TELEGRAM_ID:
        raise SystemExit(
            "Заполните .env: BOT_TOKEN и OWNER_TELEGRAM_ID (образец в .env.example)"
        )
    init_db()

    bot = Bot(token=BOT_TOKEN)
    # у всех — только публичные команды; у владельца отдельно — полный список
    # (BotCommandScopeChat), чтобы постинг/логин не светились в чужом "/"-меню
    await bot.set_my_commands(PUBLIC_COMMANDS)
    await bot.set_my_commands(
        OWNER_COMMANDS, scope=BotCommandScopeChat(chat_id=OWNER_TELEGRAM_ID)
    )

    dp = Dispatcher()
    dp.message.middleware(AccessMiddleware())
    dp.callback_query.middleware(AccessMiddleware())
    dp.include_router(wizards.router)
    dp.include_router(commands.router)

    scheduler = AsyncIOScheduler()
    interval = int(get_setting("check_interval_min"))
    scheduler.add_job(scheduled_check, "interval", minutes=interval, args=[bot])
    scheduler.add_job(
        scheduled_keywords, "interval",
        minutes=max(15, int(get_setting("keyword_window_min")) // 2),
        args=[bot],
    )
    scheduler.add_job(
        scheduled_actualize, "interval", hours=6,
        next_run_time=_first_run("last_actualize_at", 6),
    )
    # поднятия: раз в сутки со случайным сдвигом до ±2 часов
    scheduler.add_job(
        scheduled_bumps, "interval", hours=24, jitter=7200, args=[bot],
        next_run_time=_first_run("last_bumps_at", 24),
    )
    scan_hours = int(get_setting("full_scan_interval_hours"))
    # полный скан тяжёлый — не одновременно с актуализацией/поднятиями
    # при старте: +5 минут к «догоняющему» запуску
    scan_first = _first_run("last_full_scan_at", scan_hours) + timedelta(minutes=5)
    scheduler.add_job(
        scheduled_full_scan, "interval", hours=scan_hours, args=[bot],
        next_run_time=scan_first,
    )
    # вопросы продавцам из /pricecheck: дослать очередь и проверить ответы
    scheduler.add_job(seller_questions.check_replies, "interval", minutes=10, args=[bot])
    if RUNNING_IN_DOCKER:
        # итог «Обновить с GitHub» от Мака (см. bot/host_update.py)
        scheduler.add_job(host_update.report, "interval", seconds=15, args=[bot])
        await host_update.report(bot)
    scheduler.add_job(
        scheduled_weekly_report, "interval", hours=168, args=[bot],
        next_run_time=_first_run("last_weekly_report_at", 168) + timedelta(minutes=10),
    )
    scheduler.start()
    backfill_task = asyncio.create_task(asyncio.to_thread(_run_backfills))  # ссылка: иначе задачу может собрать GC
    # чистка старых совпадений по ключевым словам с отрицанием («не РФ»)
    keyword_task = asyncio.create_task(keyword_context.review_all())  # noqa: F841
    sellers_task = asyncio.create_task(sellers.ensure_defaults())  # noqa: F841
    log.info("Автопроверка каждые %d мин, полное сканирование раз в %d ч (ближайшее — %s)",
             interval, scan_hours, scan_first.strftime("%d.%m %H:%M"))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
