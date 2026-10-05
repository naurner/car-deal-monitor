"""Команды, главное меню и инлайн-кнопки уведомлений."""

import asyncio
import shutil
import tempfile
import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
    WebAppInfo,
)
from sqlalchemy import select

from bot import db_backup, host_update, texts, wizards, xlsx
from bot.settings_store import get_setting, set_setting
from config import OWNER_TELEGRAM_ID, RUNNING_IN_DOCKER
from db.models import (
    AllowedUser,
    ArchivedListing,
    BannedSeller,
    KeywordWatch,
    ListingSeen,
    PostedAd,
    SoldCar,
    WatchRequest,
)
from db.session import get_session
from matching import ai_reader, archive, keyword_context, market, matcher
from posting import manage as posting_manage
from posting import post as posting_post
from posting import remote_login
from posting import sessions as posting_sessions

FIND_PAGE = 8  # объявлений на страницу в /find

log = logging.getLogger(__name__)
router = Router()

# Структура меню: несколько категорий верхнего уровня вместо одной длинной
# простыни кнопок. Категория со списком действий → подменю с кнопкой "Назад".

def _menu_kb(is_owner: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🔍 Поиск и запросы", callback_data="cat:search"),
            InlineKeyboardButton(text="💰 Рынок и цены", callback_data="cat:market"),
        ],
        [
            InlineKeyboardButton(text="🔑 Ключевые слова", callback_data="menu:keywords"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu:settings"),
        ],
    ]
    if is_owner:
        # постинг/поднятия управляют аккаунтами владельца на площадках —
        # видно только владельцу, остальные не должны знать об этой функции
        rows.append([
            InlineKeyboardButton(text="📤 Мои объявления", callback_data="cat:posting"),
            InlineKeyboardButton(text="🔄 Обновить с GitHub", callback_data="host:update"),
        ])
        rows.append([
            InlineKeyboardButton(text="💾 Скачать базу данных", callback_data="host:backup"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _back_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:home")]


def _submenu_kb(category: str) -> InlineKeyboardMarkup:
    if category == "search":
        rows = [
            [InlineKeyboardButton(text="➕ Новый поисковый запрос", callback_data="menu:add_search")],
            [InlineKeyboardButton(text="📋 Мои запросы", callback_data="menu:my_requests")],
            [InlineKeyboardButton(text="💵 Записать проданное авто", callback_data="menu:add_sold")],
            [InlineKeyboardButton(text="🔄 Проверить сейчас", callback_data="menu:check")],
            [InlineKeyboardButton(text="🚫 Чёрный список продавцов", callback_data="menu:banned")],
        ]
    elif category == "market":
        rows = [
            [InlineKeyboardButton(text="💰 Цена по модели", callback_data="menu:pricecheck")],
            [InlineKeyboardButton(text="📈 Отчёт по рынку за неделю", callback_data="menu:market_report")],
            [InlineKeyboardButton(text="💧 Ликвидность моделей", callback_data="menu:liquidity")],
            [InlineKeyboardButton(text="📦 Архив объявлений", callback_data="menu:archive")],
            [InlineKeyboardButton(text="🔎 Поиск по слову", callback_data="menu:find")],
        ]
    elif category == "posting":
        rows = [
            [InlineKeyboardButton(text="➕ Выставить авто", callback_data="menu:post")],
            [InlineKeyboardButton(text="🛞 Выставить шины / диски / колёса", callback_data="menu:post_part")],
            [InlineKeyboardButton(text="🗂 Мои объявления и поднятия", callback_data="menu:my_ads")],
            [InlineKeyboardButton(text="🔄 Синхронизировать с площадками", callback_data="menu:adopt")],
            [InlineKeyboardButton(text="🔐 Вход на площадки", callback_data="menu:login")],
            [InlineKeyboardButton(text="🔐 Статус входа", callback_data="menu:login_status")],
            [InlineKeyboardButton(text="👑 Пользователи бота", callback_data="menu:users")],
        ]
    else:
        rows = []
    rows.append(_back_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


SUBMENU_TITLES = {
    "search": "🔍 Поиск и запросы",
    "market": "💰 Рынок и цены",
    "posting": "📤 Мои объявления",
}


def _menu_text(is_owner: bool) -> str:
    text = """🚗 CarDeal — бот для перепродажи авто

Выберите категорию кнопкой ниже."""
    if is_owner:
        text += (
            "\n\n👑 «📤 Мои объявления» и обновление видны только вам."
            f"\nВерсия бота: {host_update.VERSION}"
        )
    return text


def _home_kb() -> InlineKeyboardMarkup:
    """Кнопка «В меню» — добавляется почти к любому финальному ответу,
    чтобы из любого места можно было одним кликом вернуться в /menu."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🏠 В меню", callback_data="menu:home"),
    ]])


def _with_home(kb: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Добавляет строку «В меню» под уже существующей клавиатурой."""
    return InlineKeyboardMarkup(
        inline_keyboard=[*kb.inline_keyboard, [
            InlineKeyboardButton(text="🏠 В меню", callback_data="menu:home"),
        ]]
    )


def notification_kb(req: WatchRequest, listing=None) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="📋 Параметры запроса", callback_data=f"req:{req.id}"),
        InlineKeyboardButton(
            text=f"🚗 Проданные {req.brand}", callback_data=f"sold:{req.brand}"
        ),
    ]]
    if listing is not None and req.user_id in (None, OWNER_TELEGRAM_ID):
        rows.append(ban_row(listing))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ban_row(listing) -> list[InlineKeyboardButton]:
    """Кнопка «в чёрный список» под объявлением (только у владельца)."""
    return [InlineKeyboardButton(text="🚫 Бан продавца",
                                 callback_data=f"ban:{listing.source_site}:{listing.external_id}"[:64])]


def ban_kb(listing) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[ban_row(listing)])


@router.message(Command("start", "help", "menu"))
async def cmd_start(message: Message) -> None:
    is_owner = message.from_user.id == OWNER_TELEGRAM_ID
    await message.answer(_menu_text(is_owner), reply_markup=_menu_kb(is_owner))


# ---------- главное меню ----------

@router.callback_query(F.data.startswith("cat:"))
async def cb_category(cb: CallbackQuery) -> None:
    category = cb.data.split(":", 1)[1]
    is_owner = cb.from_user.id == OWNER_TELEGRAM_ID
    if category == "posting" and not is_owner:
        await cb.answer()
        return
    await cb.answer()
    await cb.message.answer(SUBMENU_TITLES.get(category, ""), reply_markup=_submenu_kb(category))


@router.callback_query(F.data.startswith("menu:"))
async def cb_menu(cb: CallbackQuery, state: FSMContext) -> None:
    action = cb.data.split(":", 1)[1]
    await cb.answer()
    if action == "home":
        is_owner = cb.from_user.id == OWNER_TELEGRAM_ID
        await cb.message.answer(_menu_text(is_owner), reply_markup=_menu_kb(is_owner))
    elif action == "add_search":
        await wizards.start_flow("search", cb.message, state, cb.from_user.id)
    elif action == "add_sold":
        await wizards.start_flow("sold", cb.message, state, cb.from_user.id)
    elif action == "archive":
        await wizards.start_flow("archive", cb.message, state, cb.from_user.id)
    elif action == "my_requests":
        await _send_my_requests(cb.message, cb.from_user.id)
    elif action == "check":
        await _run_check(cb.message, cb.from_user.id)
    elif action == "keywords":
        await _send_keywords(cb.message, cb.from_user.id)
    elif action == "pricecheck":
        await wizards.start_flow("pricecheck", cb.message, state, cb.from_user.id)
    elif action == "find":
        await cb.message.answer(
            "🔎 Отправьте: /find <слово>\n"
            "Найду ВСЕ спаршенные объявления (вкл. проданные), где это слово "
            "есть в названии или описании.\n"
            "Примеры: /find пневмо | /find из Кореи | /find панорама",
            reply_markup=_home_kb(),
        )
    elif action in ("post", "post_part"):
        await wizards.start_flow(action, cb.message, state, cb.from_user.id)
    elif action == "my_ads":
        await _send_my_ads(cb.message, cb.from_user.id)
    elif action == "adopt":
        await _do_adopt(cb.message, cb.from_user.id)
    elif action == "login":
        await cmd_login(cb.message)
    elif action == "login_status":
        await cmd_login_status(cb.message)
    elif action == "users":
        await _send_users(cb.message)
    elif action == "settings":
        await _send_settings(cb.message)
    elif action == "banned":
        await _send_banned(cb.message)
    elif action in ("market_report", "liquidity"):
        await send_market_report(cb.message, action)


async def send_market_report(message: Message, kind: str = "market_report") -> None:
    """Отчёт по рынку / ликвидность — считается по всему архиву (секунды)."""
    status = await message.answer("⏳ Считаю по архиву…")
    build = market.weekly_report if kind == "market_report" else market.liquidity_report
    try:
        parts = await asyncio.to_thread(build)
    except Exception as e:
        log.exception("отчёт по рынку")
        await status.edit_text(f"❌ Не удалось посчитать: {e}", reply_markup=_home_kb())
        return
    await status.delete()
    chunks = texts.batch_listings(parts, lambda p: p, sep="\n\n")
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        await message.answer(chunk, disable_web_page_preview=True,
                             reply_markup=_home_kb() if last else None)


@router.message(Command("backup"))
async def cmd_backup(message: Message) -> None:
    await _send_db_backup(message)


@router.callback_query(F.data == "host:backup")
async def cb_backup(cb: CallbackQuery) -> None:
    await cb.answer()
    await _send_db_backup(cb.message)


_backup_running = False


async def _send_db_backup(message: Message) -> None:
    global _backup_running
    if _backup_running:
        await message.answer("Копия базы уже готовится — дождитесь файлов.")
        return
    _backup_running = True
    status = await message.answer("⏳ Готовлю копию базы…")
    tmp = Path(tempfile.mkdtemp(prefix="cardeal_backup_"))
    try:
        parts, raw_size = await asyncio.to_thread(db_backup.make_parts, tmp)
        total = len(parts)
        await status.edit_text(
            f"📤 Отправляю базу: {db_backup.fmt_size(raw_size)}, в архиве "
            f"{db_backup.fmt_size(sum(p.stat().st_size for p in parts))}"
            + (f", частей: {total}" if total > 1 else "") + "…"
        )
        for i, part in enumerate(parts, 1):
            caption = f"💾 База CarDealTG — часть {i} из {total}" if total > 1 else "💾 База CarDealTG"
            # 25 МБ с домашнего интернета грузятся дольше стандартного таймаута
            await message.answer_document(FSInputFile(part), caption=caption, request_timeout=900)
        await status.edit_text(db_backup.instructions(parts), reply_markup=_home_kb())
    except Exception as e:
        log.exception("резервная копия базы")
        await status.edit_text(f"❌ Не удалось отправить базу: {e}", reply_markup=_home_kb())
    finally:
        _backup_running = False
        shutil.rmtree(tmp, ignore_errors=True)


@router.callback_query(F.data == "host:update")
async def cb_host_update(cb: CallbackQuery) -> None:
    await cb.answer()
    if not RUNNING_IN_DOCKER:
        await cb.message.answer(
            "Обновление кнопкой работает на Маке, где бот запущен в Docker. "
            "Здесь: git pull и перезапуск вручную.",
            reply_markup=_home_kb(),
        )
        return
    msg = await cb.message.answer(
        f"⏳ Прошу Мак обновиться с GitHub (сейчас версия {host_update.VERSION}).\n"
        "Если есть изменения, пересборка займёт несколько минут — пришлю итог."
    )
    if not host_update.request_update(msg.chat.id, msg.message_id):
        await msg.edit_text("Обновление уже идёт — дождитесь результата.")


# ---------- запросы ----------

async def _send_my_requests(message: Message, user_id: int) -> None:
    with get_session() as s:
        reqs = list(
            s.execute(
                select(WatchRequest).where(WatchRequest.user_id == user_id)
            ).scalars()
        )
    if not reqs:
        is_owner = user_id == OWNER_TELEGRAM_ID
        await message.answer(
            "Запросов пока нет. Создайте:", reply_markup=_menu_kb(is_owner)
        )
        return
    for i, req in enumerate(reqs):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏸ Пауза" if req.is_active else "▶️ Активировать",
                    callback_data=f"toggle:{req.id}",
                ),
                InlineKeyboardButton(text="✏️ Изменить", callback_data=f"editreq:{req.id}"),
            ],
            [
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:{req.id}"),
                InlineKeyboardButton(text="🔎 Найденное", callback_data=f"found:{req.id}"),
            ],
        ])
        if i == len(reqs) - 1:
            kb = _with_home(kb)
        await message.answer(texts.request_summary(req), reply_markup=kb)


@router.message(Command("my_requests"))
async def cmd_my_requests(message: Message) -> None:
    await _send_my_requests(message, message.from_user.id)


@router.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(cb: CallbackQuery) -> None:
    req_id = int(cb.data.split(":")[1])
    status = None
    with get_session() as s:
        req = s.get(WatchRequest, req_id)
        if req:
            req.is_active = not req.is_active
            s.commit()
            status = "активирован ▶️" if req.is_active else "на паузе ⏸"
    if status:  # ответ — вне сессии: она держит базу занятой
        await cb.message.answer(f"Запрос #{req_id} {status}")
    await cb.answer()


@router.callback_query(F.data.startswith("del:"))
async def cb_delete(cb: CallbackQuery) -> None:
    req_id = int(cb.data.split(":")[1])
    with get_session() as s:
        req = s.get(WatchRequest, req_id)
        if req:
            s.delete(req)
            s.commit()
    await cb.message.answer(f"Запрос #{req_id} удалён.")
    await cb.answer()


# ---------- изменение параметров уже созданного запроса ----------
# Точечная правка нескольких полей вместо повторного прохождения всего
# мастера — смену марки/модели/поколения проще сделать через новый запрос.

class EditRequest(StatesGroup):
    waiting_value = State()


EDIT_FIELDS = {
    "price": "💰 Цена",
    "years": "🗓 Годы",
    "label": "🏷 Метка",
    "reg_country": "📋 Учет/нахождение",
    "country": "🌍 Страна",
}

EDIT_PROMPTS = {
    "price": "Новая цена от–до в $ (например: 15000-25000):",
    "years": "Новые годы: диапазон (2018-2023), один год (2019), или «-» чтобы снять фильтр:",
    "label": "Новая метка (или «-» чтобы убрать):",
    "reg_country": "Новая страна учета/нахождения (или «-» чтобы убрать фильтр):",
    "country": "Новая страна производителя (или «-» чтобы убрать):",
}


def _edit_field_label(req: WatchRequest, field: str) -> str:
    current = {
        "price": f"{texts.fmt_price(req.price_min)}–{texts.fmt_price(req.price_max)}",
        "years": f"{req.year_from or '…'}–{req.year_to or 'н.в.'}" if (req.year_from or req.year_to) else "не задано",
        "label": req.label or "не задано",
        "reg_country": req.reg_country or "не задано",
        "country": req.country or "не задано",
    }[field]
    return f"{EDIT_FIELDS[field]} ({current})"


@router.callback_query(F.data.startswith("editreq:"))
async def cb_edit_request(cb: CallbackQuery) -> None:
    req_id = int(cb.data.split(":")[1])
    with get_session() as s:
        req = s.get(WatchRequest, req_id)
    if not req:
        await cb.answer("Запрос не найден")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_edit_field_label(req, f), callback_data=f"editfield:{req_id}:{f}")]
        for f in EDIT_FIELDS
    ])
    kb = _with_home(kb)
    await cb.message.answer(
        f"Что изменить у запроса #{req_id} ({req.brand} {req.model})?\n"
        "Марку/модель/поколение здесь не тронуть — для этого проще создать новый запрос.",
        reply_markup=kb,
    )
    await cb.answer()


@router.callback_query(F.data.startswith("editfield:"))
async def cb_edit_field(cb: CallbackQuery, state: FSMContext) -> None:
    _, req_id, field = cb.data.split(":")
    await state.set_state(EditRequest.waiting_value)
    await state.update_data(req_id=int(req_id), field=field)
    await cb.message.answer(EDIT_PROMPTS[field])
    await cb.answer()


@router.message(EditRequest.waiting_value)
async def edit_field_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    req_id, field = data["req_id"], data["field"]
    text = (message.text or "").strip()

    if field == "price":
        try:
            lo, hi = (float(x) for x in text.replace(" ", "").split("-"))
        except ValueError:
            await message.answer("Формат: 15000-25000. Попробуйте ещё раз.")
            return
    elif field == "years" and text != "-":
        cleaned = text.replace(" ", "")
        try:
            if "-" in cleaned:
                y1, y2 = (int(x) for x in cleaned.split("-"))
            else:
                y1 = y2 = int(cleaned)
        except ValueError:
            await message.answer("Формат: 2018-2023 или 2019, либо «-». Попробуйте ещё раз.")
            return

    summary = None
    with get_session() as s:
        req = s.get(WatchRequest, req_id)
        if not req:
            pass
        elif field == "price":
            req.price_min, req.price_max = lo, hi
        elif field == "years":
            if text == "-":
                req.year_from = req.year_to = None
            else:
                req.year_from, req.year_to = y1, y2
        else:
            setattr(req, field, None if text == "-" else text)
        if req:
            s.commit()
            summary = texts.request_summary(req)
    await state.clear()
    if summary is None:
        await message.answer("Запрос не найден (возможно, удалён).", reply_markup=_home_kb())
        return
    await message.answer(f"✅ Обновлено:\n\n{summary}", reply_markup=_home_kb())


@router.callback_query(F.data.startswith("found:"))
async def cb_found(cb: CallbackQuery) -> None:
    req_id = int(cb.data.split(":")[1])
    with get_session() as s:
        listings = list(
            s.execute(
                select(ListingSeen)
                .where(ListingSeen.watch_request_id == req_id)
                .order_by(ListingSeen.first_seen_at.desc())
                .limit(15)
            ).scalars()
        )
    if not listings:
        await cb.message.answer("По этому запросу пока ничего не найдено.", reply_markup=_home_kb())
    else:
        body = "\n\n".join(texts.listing_short(l) for l in listings)
        await cb.message.answer(f"Найдено по запросу #{req_id}:\n\n{body}", reply_markup=_home_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("req:"))
async def cb_request_params(cb: CallbackQuery) -> None:
    req_id = int(cb.data.split(":")[1])
    with get_session() as s:
        req = s.get(WatchRequest, req_id)
    if req:
        await cb.message.answer(texts.request_summary(req), reply_markup=_home_kb())
    else:
        await cb.message.answer("Запрос не найден (возможно, удалён).", reply_markup=_home_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("sold:"))
async def cb_sold_by_brand(cb: CallbackQuery) -> None:
    brand = cb.data.split(":", 1)[1]
    with get_session() as s:
        cars = list(
            s.execute(select(SoldCar).where(SoldCar.brand == brand)).scalars()
        )
    if not cars:
        await cb.message.answer(f"Проданных {brand} в базе нет.", reply_markup=_home_kb())
    else:
        chunks = texts.batch_listings(cars, texts.sold_summary)
        for chunk in chunks[:-1]:
            await cb.message.answer(chunk)
        await cb.message.answer(chunks[-1], reply_markup=_home_kb())
    await cb.answer()


# ---------- проверка ----------

async def _run_check(message: Message, user_id: int) -> None:
    await message.answer("🔍 Проверяю площадки…")
    results = await matcher.check_all_active()
    mine = [m for m in results if m.watch_request.user_id == user_id]
    if not mine:
        note = f" (у других пользователей: {len(results)})" if results else ""
        await message.answer(
            f"Новых совпадений по вашим запросам нет{note}.", reply_markup=_home_kb()
        )
        return
    for i, m in enumerate(mine):
        kb = notification_kb(m.watch_request, m.listing)
        if i == len(mine) - 1:
            kb = _with_home(kb)
        await message.answer(texts.match_notification(m), reply_markup=kb)


@router.message(Command("check"))
async def cmd_check(message: Message) -> None:
    await _run_check(message, message.from_user.id)


# ---------- ключевые слова ----------

async def _send_keywords(message: Message, user_id: int) -> None:
    with get_session() as s:
        watches = list(
            s.execute(
                select(KeywordWatch).where(KeywordWatch.user_id == user_id)
            ).scalars()
        )
    add_row = [InlineKeyboardButton(text="➕ Добавить слежение", callback_data="menu:kw_add")]
    if not watches:
        await message.answer(
            "Слежений за ключевыми словами нет.\n"
            "Бот может присылать свежие объявления по словам "
            "(например «из РФ» или «белый»).",
            reply_markup=_with_home(InlineKeyboardMarkup(inline_keyboard=[add_row])),
        )
        return
    for i, w in enumerate(watches):
        status = "🟢" if w.is_active else "⏸"
        # сколько подходит в архиве (с учётом контекста, по кэшу ИИ)
        found = await asyncio.to_thread(keyword_context.candidates, w.keywords)
        total = sum(1 for r in found if keyword_context.matches(r, w.keywords))
        rows = [[
            InlineKeyboardButton(
                text=f"📂 Найденные ({total})", callback_data=f"kwfind:{w.id}:0",
            ),
            InlineKeyboardButton(text="📊 Таблица Excel", callback_data=f"kwxls:{w.id}"),
        ], [
            InlineKeyboardButton(
                text="⏸ Пауза" if w.is_active else "▶️ Вкл",
                callback_data=f"kwtoggle:{w.id}",
            ),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"kwdel:{w.id}"),
        ]]
        if i == len(watches) - 1:
            rows.append(add_row)
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        if i == len(watches) - 1:
            kb = _with_home(kb)
        await message.answer(
            f"{status} Слежение #{w.id}: {', '.join(w.keywords)}",
            reply_markup=kb,
        )


@router.callback_query(F.data.startswith("kwfind:"))
async def cb_kw_find(cb: CallbackQuery) -> None:
    """Архив по словам конкретного слежения (все найденные авто)."""
    _, wid, offset_s = cb.data.split(":")
    await cb.answer()
    w = _own_watch(int(wid), cb.from_user.id)
    if not w:
        await cb.message.answer("Слежение не найдено.", reply_markup=_home_kb())
        return
    offset = int(offset_s)
    collected, removed = await _reviewed(cb.message, w)
    total = len(collected)
    if total == 0:
        await cb.message.answer(
            f"По словам «{', '.join(w.keywords)}» в архиве пока ничего нет.",
            reply_markup=_home_kb(),
        )
        return
    page = collected[offset:offset + FIND_PAGE]
    shown_to = offset + len(page)
    header = (
        f"📂 Слежение #{w.id} ({', '.join(w.keywords)}): найдено {total} "
        f"(показаны {offset + 1}–{shown_to})"
    )
    chunks = texts.batch_listings(page, texts.archived_listing) or [""]
    chunks[0] = f"{header}\n\n{chunks[0]}".rstrip()
    for chunk in chunks[:-1]:
        await cb.message.answer(chunk)
    if shown_to < total:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"⬇️ Ещё {min(FIND_PAGE, total - shown_to)}",
                callback_data=f"kwfind:{w.id}:{shown_to}",
            )
        ]])
        await cb.message.answer(chunks[-1], reply_markup=_with_home(kb))
    else:
        await cb.message.answer(chunks[-1], reply_markup=_home_kb())


def _own_watch(wid: int, user_id: int) -> KeywordWatch | None:
    with get_session() as s:
        w = s.get(KeywordWatch, wid)
    if w and (w.user_id == user_id or user_id == OWNER_TELEGRAM_ID):
        return w
    return None


async def _reviewed(message: Message, w: KeywordWatch):
    """Найденное по слежению с проверкой контекста. Спорные («не РФ», «не
    проходной на Россию») ИИ дочитывает здесь же — с сообщением о ходе."""
    found = await asyncio.to_thread(keyword_context.candidates, w.keywords)
    todo = keyword_context.pending_count(found, w.keywords)
    status = None
    if todo and ai_reader.available():
        status = await message.answer(
            f"🤖 Проверяю контекст: {todo} объявлений, где слово рядом с «не»/«без»…")

        async def progress(done: int, total: int) -> None:
            try:
                await status.edit_text(f"🤖 Проверяю контекст: {done} из {total}…")
            except Exception:
                pass
    else:
        progress = None
    good, removed = await keyword_context.review_watch(w, progress, found)
    if status:
        note = f", отсеяно {removed} с отрицанием" if removed else ""
        await status.edit_text(f"🤖 Контекст проверен: подходит {len(good)}{note}.")
    return good, removed


@router.callback_query(F.data.startswith("kwxls:"))
async def cb_kw_xlsx(cb: CallbackQuery) -> None:
    """Таблица всех найденных по слежению: ссылки, проданные/актуальные."""
    await cb.answer()
    w = _own_watch(int(cb.data.split(":")[1]), cb.from_user.id)
    if not w:
        await cb.message.answer("Слежение не найдено.", reply_markup=_home_kb())
        return
    rows, _ = await _reviewed(cb.message, w)
    if not rows:
        await cb.message.answer(f"По словам «{', '.join(w.keywords)}» пока ничего не найдено.",
                                reply_markup=_home_kb())
        return
    data = await asyncio.to_thread(_keyword_table, w, rows)
    active = sum(1 for r in rows if r.is_active)
    name = "_".join(w.keywords)[:40].replace(" ", "_").replace("/", "_")
    await cb.message.answer_document(
        BufferedInputFile(data, filename=f"слежение_{w.id}_{name}.xlsx"),
        caption=(f"📊 Слежение #{w.id} ({', '.join(w.keywords)}): {len(rows)} объявлений — "
                 f"🟢 актуальных {active}, 🔴 проданных/снятых {len(rows) - active}"),
        reply_markup=_home_kb(),
    )


def _keyword_table(w: KeywordWatch, rows) -> bytes:
    headers = ["Статус", "Название", "Марка", "Модель", "Год", "Цена, $", "Цена, сом",
               "Площадка", "Город", "Слово", "Опубликовано", "Последний раз видели",
               "Снято", "Ссылка"]
    out = []
    for r in rows:
        text = keyword_context.text_of(r).lower()
        word = next((k for k in w.keywords if k.lower() in text), "")
        posted = r.posted_at or r.first_seen_at
        out.append([
            xlsx.Styled("🟢 Актуально", xlsx.GREEN) if r.is_active
            else xlsx.Styled("🔴 Продано / снято", xlsx.RED),
            r.title, r.brand, r.model, r.year,
            round(r.price_usd) if r.price_usd is not None else None,
            round(market.kgs_price(r.price_usd, r.price_kgs)) if r.price_usd is not None else None,
            texts.SITE_NAMES.get(r.source_site, r.source_site), r.city, word,
            f"{posted:%d.%m.%Y}" if posted else "",
            f"{r.last_seen_at:%d.%m.%Y}",
            f"{r.sold_at:%d.%m.%Y}" if r.sold_at and not r.is_active else "",
            xlsx.Link(r.url),
        ])
    return xlsx.make_xlsx(headers, out, widths=[18, 45, 14, 14, 7, 10, 12, 12, 14, 14, 13, 13, 12, 60],
                          sheet=f"Слежение {w.id}")


@router.message(Command("keywords"))
async def cmd_keywords(message: Message) -> None:
    await _send_keywords(message, message.from_user.id)


@router.callback_query(F.data == "menu:kw_add")
async def cb_kw_add(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await wizards.start_flow("keyword", cb.message, state, cb.from_user.id)


@router.callback_query(F.data.startswith("kwtoggle:"))
async def cb_kw_toggle(cb: CallbackQuery) -> None:
    wid = int(cb.data.split(":")[1])
    with get_session() as s:
        w = s.get(KeywordWatch, wid)
        state = None
        if w:
            w.is_active = not w.is_active
            s.commit()
            state = "включено ▶️" if w.is_active else "на паузе ⏸"
    if state:
        await cb.message.answer(f"Слежение #{wid} {state}")
    await cb.answer()


@router.callback_query(F.data.startswith("kwdel:"))
async def cb_kw_del(cb: CallbackQuery) -> None:
    wid = int(cb.data.split(":")[1])
    with get_session() as s:
        w = s.get(KeywordWatch, wid)
        if w:
            s.delete(w)
            s.commit()
    await cb.message.answer(f"Слежение #{wid} удалено.")
    await cb.answer()


# ---------- мои объявления (постинг) ----------

_SITE_BASE = {"mashina": "https://mashina.kg", "lalafo": "https://lalafo.kg"}


def _full_url(site: str, url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("http"):
        return url
    return _SITE_BASE.get(site, "") + url


def _fmt_bump(iso: str | None) -> str:
    if not iso:
        return "поднятий ещё не было"
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return "поднято недавно"
    delta = datetime.utcnow() - dt
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        ago = "только что"
    elif mins < 60:
        ago = f"{mins} мин назад"
    elif mins < 1440:
        ago = f"{mins // 60} ч назад"
    else:
        ago = f"{mins // 1440} дн назад"
    return f"поднято {dt:%d.%m %H:%M} ({ago})"


async def _send_my_ads(message: Message, user_id: int) -> None:
    with get_session() as s:
        ads = list(
            s.execute(
                select(PostedAd).where(PostedAd.user_id == user_id)
            ).scalars()
        )
    if not ads:
        await message.answer(
            "Размещённых через бота объявлений нет.\n"
            "Разместить новое, синхронизировать с площадками или проверить "
            "вход — в разделе «📤 Мои объявления».",
            reply_markup=_home_kb(),
        )
        return
    for i, ad in enumerate(ads):
        car = ad.car_data or {}
        # заголовок: title (для /adopt) или марка/модель/год (для /post)
        title = car.get("title") or " ".join(
            str(car.get(k)) for k in ("brand", "model", "year") if car.get(k)
        ) or "объявление"
        head = f"🚗 #{ad.id} {title}"
        if ad.price_usd:
            head += f" — {texts.fmt_price(ad.price_usd)}"
        lines = [head]
        for site, info in (ad.sites or {}).items():
            status = info.get("status", "?")
            mark = {"posted": "✅", "pending": "⏳", "error": "❌"}.get(status, "▫️")
            bump = _fmt_bump(info.get("last_bump"))
            lines.append(f"{mark} {texts.SITE_NAMES.get(site, site)}: {status} | {bump}")
            url = _full_url(site, info.get("url"))
            if url:
                lines.append(url)
            if info.get("error"):
                lines.append(f"   ⚠️ {info['error']}")
        if not ad.sites:
            lines.append("📝 Черновик — ещё не опубликован")
        lines.append("Автоподнятие: " + ("🟢 вкл" if ad.bump_enabled else "⏸ выкл"))
        rows = [[
            InlineKeyboardButton(text="🔼 Поднять сейчас", callback_data=f"adup:{ad.id}"),
            InlineKeyboardButton(
                text="⏸ Стоп поднятия" if ad.bump_enabled else "▶️ Поднимать",
                callback_data=f"adbump:{ad.id}",
            ),
        ], [
            InlineKeyboardButton(text="🗑 Убрать из бота", callback_data=f"addel:{ad.id}"),
        ]]
        if any((info or {}).get("status") == "posted" for info in (ad.sites or {}).values()):
            rows.insert(1, [InlineKeyboardButton(text="📷 Заменить фото", callback_data=f"adphoto:{ad.id}")])
        # создано мастером бота (а не подтянуто с площадки) — можно
        # опубликовать черновик или довыставить туда, где ещё нет
        if car.get("brand") or car.get("kind") == "part":
            todo = posting_post.pending_sites(ad)
            if todo:
                names = " и ".join(texts.SITE_NAMES.get(s, s) for s in todo)
                text = "✅ Опубликовать" if not ad.sites else f"📤 Выставить на {names}"
                rows.insert(0, [InlineKeyboardButton(text=text, callback_data=f"pubad:{ad.id}")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        if i == len(ads) - 1:
            kb = _with_home(kb)
        await message.answer("\n".join(lines), reply_markup=kb,
                             disable_web_page_preview=True)


# ---------- чёрный список продавцов ----------

@router.callback_query(F.data.startswith("ban:"))
async def cb_ban(cb: CallbackQuery) -> None:
    from matching import sellers

    _, site, ext = cb.data.split(":", 2)
    await cb.answer("Баню продавца…")
    with get_session() as s:
        row = s.execute(select(ArchivedListing).where(
            ArchivedListing.source_site == site, ArchivedListing.external_id == ext,
        )).scalar_one_or_none()
    if row is None:
        await cb.message.answer("Объявление не нашлось в архиве.")
        return
    sid, name = await sellers.seller_of_row(row)
    if not sid:
        await cb.message.answer("Не смог узнать продавца этого объявления — "
                                "пришлите /ban <ссылка на объявление>.")
        return
    await _do_ban(cb.message, site, sid, name)


async def _do_ban(message: Message, site: str, sid: str, name: str | None) -> None:
    from matching import sellers

    new, marked = await sellers.ban(site, sid, name)
    who = f"«{name}»" if name else f"№{sid}"
    head = "🚫 В чёрном списке" if new else "Уже в чёрном списке"
    with get_session() as s:
        b = s.execute(select(BannedSeller).where(BannedSeller.site == site,
                                                 BannedSeller.seller_id == sid)).scalar_one()
    await message.answer(
        f"{head}: продавец {who} ({texts.SITE_NAMES.get(site, site)}).\n"
        f"Его объявлений найдено: {marked} — больше не присылаю и не считаю рынком "
        f"(топы, «ниже рынка», /pricecheck).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="↩️ Разбанить", callback_data=f"unban:{b.id}"),
            InlineKeyboardButton(text="🚫 Весь список", callback_data="menu:banned"),
        ]]),
    )


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject) -> None:
    from matching import sellers

    arg = (command.args or "").strip()
    found = await sellers.resolve(arg) if arg else None
    if not found:
        await message.answer(
            "Пришлите: /ban <ссылка на объявление или профиль продавца>\n"
            "Например: /ban https://lalafo.kg/bishkek/ads/...-id-123456\n"
            "Или нажмите «🚫 Бан продавца» под уведомлением.",
            reply_markup=_home_kb())
        return
    await _do_ban(message, *found)


async def _send_banned(message: Message) -> None:
    from matching import sellers

    bans = sellers.all_banned()
    if not bans:
        await message.answer(
            "Чёрный список продавцов пуст.\nДобавить: «🚫 Бан продавца» под уведомлением "
            "или /ban <ссылка на объявление>.", reply_markup=_home_kb())
        return
    lines = ["🚫 Чёрный список продавцов — их объявления не присылаю и не считаю рынком:"]
    rows = []
    for b in bans:
        who = b.name or f"№{b.seller_id}"
        lines.append(f"• {who} — {texts.SITE_NAMES.get(b.site, b.site)} (с {b.created_at:%d.%m.%Y})")
        rows.append([InlineKeyboardButton(text=f"↩️ Разбанить {who}"[:60], callback_data=f"unban:{b.id}")])
    lines.append("\nДобавить: /ban <ссылка на объявление> или кнопка под уведомлением.")
    await message.answer("\n".join(lines), reply_markup=_with_home(InlineKeyboardMarkup(inline_keyboard=rows)))


@router.message(Command("banned"))
async def cmd_banned(message: Message) -> None:
    await _send_banned(message)


@router.callback_query(F.data.startswith("unban:"))
async def cb_unban(cb: CallbackQuery) -> None:
    from matching import sellers

    b = sellers.unban(int(cb.data.split(":")[1]))
    await cb.answer()
    who = (b.name or f"№{b.seller_id}") if b else "продавец"
    await cb.message.answer(f"↩️ {who} убран из чёрного списка.", reply_markup=_home_kb())


class PhotoReplace(StatesGroup):
    waiting = State()


@router.callback_query(F.data.startswith("adphoto:"))
async def cb_ad_photo_start(cb: CallbackQuery, state: FSMContext) -> None:
    """Заменить фото в уже опубликованном объявлении — на всех площадках."""
    ad_id = int(cb.data.split(":")[1])
    with get_session() as s:
        ad = s.get(PostedAd, ad_id)
    if ad is None:
        await cb.answer("Объявление не найдено", show_alert=True)
        return
    await cb.answer()
    import time as _time

    from config import DATA_DIR
    await state.set_state(PhotoReplace.waiting)
    await state.set_data({
        "flow": "photo_replace", "ad_id": ad_id, "user_id": cb.from_user.id, "photos": [],
        "photos_dir": str(DATA_DIR / "photos" / f"{cb.from_user.id}_{int(_time.time())}"),
        "done_cb": "adphotodone",
    })
    sites = ", ".join(texts.SITE_NAMES.get(k, k) for k, v in (ad.sites or {}).items()
                      if v.get("status") == "posted")
    await cb.message.answer(
        f"📷 Пришлите новые фото для объявления #{ad_id} (можно альбомом), затем «Готово».\n"
        f"Заменю на: {sites}. Старые фото уберу, только когда новые загрузятся.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Готово", callback_data="adphotodone"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="adphotocancel"),
        ]]),
    )


@router.message(PhotoReplace.waiting)
async def ad_photo_receive(message: Message, state: FSMContext) -> None:
    if not message.photo:
        await message.answer("Пришлите фото или нажмите «Готово».")
        return
    await wizards._save_photo(message, state)


@router.callback_query(PhotoReplace.waiting, F.data == "adphotocancel")
async def cb_ad_photo_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await cb.message.answer("Замена фото отменена.", reply_markup=_home_kb())


@router.callback_query(PhotoReplace.waiting, F.data == "adphotodone")
async def cb_ad_photo_done(cb: CallbackQuery, state: FSMContext) -> None:
    data = await wizards.wait_photos(cb.from_user.id, state)
    photos = sorted(data.get("photos") or [])
    if not photos:
        await cb.answer("Сначала пришлите фото")
        return
    await state.clear()
    await cb.answer()
    with get_session() as s:
        ad = s.get(PostedAd, data["ad_id"])
    status = await cb.message.answer(f"⏳ Заменяю фото ({len(photos)} шт.) на площадках…")
    results = await posting_post.replace_photos(ad, photos, data["photos_dir"])
    if not results:
        text = "Объявление не опубликовано ни на одной площадке — менять нечего."
    else:
        lines = [f"Объявление #{ad.id}:"]
        lines += [f"{'✅' if r.ok else '⚠️'} {r.message}" for r in results.values()]
        text = "\n".join(lines)
    await status.edit_text(text, reply_markup=_home_kb(), disable_web_page_preview=True)


@router.message(Command("my_ads"))
async def cmd_my_ads(message: Message) -> None:
    await _send_my_ads(message, message.from_user.id)


_bumping: set[int] = set()


@router.callback_query(F.data.startswith("adup:"))
async def cb_ad_up_now(cb: CallbackQuery) -> None:
    ad_id = int(cb.data.split(":")[1])
    if ad_id in _bumping:
        await cb.answer("Уже поднимаю")
        return
    with get_session() as s:
        ad = s.get(PostedAd, ad_id)
    if ad is None:
        await cb.answer("Объявление не найдено", show_alert=True)
        return
    _bumping.add(ad_id)
    await cb.answer()
    status = await cb.message.answer(f"⏳ Поднимаю объявление #{ad_id} (до минуты)…")
    try:
        results = await posting_manage.bump_ad(ad)
    finally:
        _bumping.discard(ad_id)
    if not results:
        text = f"Объявление #{ad_id} не опубликовано ни на одной площадке — поднимать нечего."
    else:
        lines = [f"Объявление #{ad_id}:"]
        for site, res in results:
            mark = "✅" if res.ok else ("⏳" if res.transient else ("💳" if res.payment_required else "⚠️"))
            lines.append(f"{mark} {res.message}")
        text = "\n".join(lines)
    await status.edit_text(text, reply_markup=_home_kb())


@router.callback_query(F.data.startswith("adbump:"))
async def cb_ad_bump_toggle(cb: CallbackQuery) -> None:
    ad_id = int(cb.data.split(":")[1])
    with get_session() as s:
        ad = s.get(PostedAd, ad_id)
        bump = None
        if ad:
            ad.bump_enabled = not ad.bump_enabled
            s.commit()
            bump = "включено 🟢" if ad.bump_enabled else "выключено ⏸"
    if bump:
        await cb.message.answer(f"Объявление #{ad_id}: автоподнятие {bump}")
    await cb.answer()


@router.callback_query(F.data.startswith("addel:"))
async def cb_ad_delete(cb: CallbackQuery) -> None:
    ad_id = int(cb.data.split(":")[1])
    with get_session() as s:
        ad = s.get(PostedAd, ad_id)
        if ad:
            s.delete(ad)
            s.commit()
    await cb.message.answer(
        f"Объявление #{ad_id} убрано из бота (на площадках оно остаётся)."
    )
    await cb.answer()


_login_in_progress: set[str] = set()


@router.message(Command("login"))
async def cmd_login(message: Message) -> None:
    kb = _with_home(InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="mashina.kg", callback_data="login:mashina"),
        InlineKeyboardButton(text="lalafo.kg", callback_data="login:lalafo"),
    ]]))
    where = (
        "Браузер откроется прямо в Telegram — войдите в аккаунт как обычно."
        if RUNNING_IN_DOCKER else
        "На ЭТОМ компьютере откроется окно браузера — войдите в аккаунт как "
        "обычно и закройте окно."
    )
    await message.answer(
        f"На каком сайте войти? {where}\n\n"
        "ℹ️ unaa.kg работает только через мобильное приложение — "
        "ни вход, ни постинг, ни парсинг через браузер там невозможны.",
        reply_markup=kb,
    )


# ---------- вход из Telegram (бот в Docker, экрана нет) ----------

class LoginSessionState(StatesGroup):
    active = State()


def _login_session_kb(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Открыть браузер", web_app=WebAppInfo(url=url))],
        [
            InlineKeyboardButton(text="⏎ Enter", callback_data="login:enter"),
            InlineKeyboardButton(text="📸 Скриншот", callback_data="login:shot"),
        ],
        [
            InlineKeyboardButton(text="✅ Я вошёл", callback_data="login:done"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="login:cancel"),
        ],
        [InlineKeyboardButton(text="🔗 Открыть в обычном браузере", url=url)],
    ])


async def _start_remote_login(site: str, cb: CallbackQuery, state: FSMContext) -> None:
    status = await cb.message.answer(f"⏳ Запускаю браузер для {site} (до минуты)…")
    try:
        session = await remote_login.start(site)
    except Exception as e:
        log.exception("remote login start %s", site)
        await status.edit_text(f"❌ Не удалось запустить браузер: {e}", reply_markup=_home_kb())
        return
    await state.set_state(LoginSessionState.active)
    session.timeout_task = asyncio.create_task(
        _login_timeout(cb.bot, cb.message.chat.id, state)
    )
    await status.edit_text(
        f"🔐 Вход на {site}\n\n"
        "1. «🌐 Открыть браузер» — войдите как обычно. Если страница не "
        "загрузилась — подождите 10 секунд и откройте снова.\n"
        "2. Телефон и код из SMS можно просто написать сюда сообщением — "
        "впечатаю в поле, где стоит курсор (сначала нажмите на поле в браузере).\n"
        "3. Вошли — «✅ Я вошёл».\n\n"
        f"Браузер закроется сам через {remote_login.SESSION_TIMEOUT_SEC // 60} мин.",
        reply_markup=_login_session_kb(session.url),
    )


async def _check_after_login(site: str) -> str:
    ok = await posting_sessions.check_logged_in(site)
    return (
        f"🟢 {site}: вход выполнен, сессия сохранена!" if ok
        else f"🔴 {site}: похоже, вход не завершён — попробуйте ещё раз"
    )


async def _login_timeout(bot, chat_id: int, state: FSMContext) -> None:
    await asyncio.sleep(remote_login.SESSION_TIMEOUT_SEC)
    session = remote_login.current()
    if session is None:
        return
    await state.clear()
    await remote_login.finish()
    result = await _check_after_login(session.site)
    await bot.send_message(chat_id, f"⏱ Время на вход вышло, браузер закрыт.\n{result}",
                           reply_markup=_home_kb())


@router.callback_query(F.data.in_({"login:enter", "login:shot", "login:done", "login:cancel"}))
async def cb_login_session(cb: CallbackQuery, state: FSMContext) -> None:
    action = cb.data.split(":")[1]
    session = remote_login.current()
    if session is None:
        await state.clear()
        await cb.answer("Сессия входа уже закрыта", show_alert=True)
        return
    try:
        if action == "enter":
            await remote_login.press_enter()
            await cb.answer("⏎ Нажал Enter")
        elif action == "shot":
            await cb.answer()
            png = await remote_login.screenshot()
            await cb.message.answer_photo(BufferedInputFile(png, "screen.png"))
        else:
            await cb.answer()
            await state.clear()
            await remote_login.finish()
            if action == "cancel":
                await cb.message.edit_text("Вход отменён.", reply_markup=_home_kb())
                return
            await cb.message.edit_text(f"⏳ Проверяю сессию {session.site}…")
            await cb.message.edit_text(await _check_after_login(session.site),
                                       reply_markup=_home_kb())
    except Exception as e:
        log.exception("remote login action %s", action)
        await cb.message.answer(f"⚠️ Не получилось: {e}")


@router.message(LoginSessionState.active, F.text, ~F.text.startswith("/"))
async def login_type_text(message: Message, state: FSMContext) -> None:
    if remote_login.current() is None:
        await state.clear()
        return
    try:
        await remote_login.type_text(message.text)
    except Exception as e:
        await message.answer(f"⚠️ Не смог впечатать: {e}")
        return
    try:
        await message.react([ReactionTypeEmoji(emoji="👌")])
    except Exception:
        await message.answer("⌨️ Впечатал")


async def _login_flow(site: str, cb: CallbackQuery) -> None:
    from posting.sessions import SITES, SiteBrowser, check_logged_in
    import asyncio as _asyncio

    try:
        async with SiteBrowser(site, headless=False) as ctx:
            page = await ctx.new_page()
            await page.goto(SITES[site])
            while ctx.pages:
                await _asyncio.sleep(1)
    except Exception as e:
        log.exception("login window %s", site)
        if "closed" not in str(e).lower():
            await cb.message.answer(f"⚠️ {site}: окно закрылось с ошибкой: {e}")
    finally:
        _login_in_progress.discard(site)
    await cb.message.answer(f"Окно закрыто, проверяю сессию {site}…")
    ok = await check_logged_in(site)
    await cb.message.answer(
        f"🟢 {site}: вход выполнен, сессия сохранена!" if ok
        else f"🔴 {site}: похоже, вход не завершён — попробуйте ещё раз",
        reply_markup=_home_kb(),
    )


@router.callback_query(F.data.in_({f"login:{s}" for s in posting_sessions.SITES}))
async def cb_login(cb: CallbackQuery, state: FSMContext) -> None:
    site = cb.data.split(":")[1]
    if RUNNING_IN_DOCKER:
        await cb.answer()
        await _start_remote_login(site, cb, state)
        return
    if site in _login_in_progress:
        await cb.answer("Окно этого сайта уже открыто")
        return
    _login_in_progress.add(site)
    await cb.answer()
    await cb.message.answer(
        f"🖥 Открываю окно {site} на компьютере, где запущен бот. "
        "Войдите в аккаунт и закройте окно браузера."
    )
    import asyncio as _asyncio
    _asyncio.create_task(_login_flow(site, cb))


@router.message(Command("login_status"))
async def cmd_login_status(message: Message) -> None:
    await message.answer("Проверяю сессии площадок (до минуты)…")
    lines = []
    for site in posting_sessions.SITES:
        if not posting_sessions.has_profile(site):
            lines.append(f"⚪ {site}: входа не было — /login")
            continue
        ok = await posting_sessions.check_logged_in(site)
        lines.append(
            f"🟢 {site}: сессия активна" if ok
            else f"🔴 {site}: сессия истекла — /login"
        )
    await message.answer("\n".join(lines), reply_markup=_home_kb())


@router.message(Command("adopt"))
async def cmd_adopt(message: Message) -> None:
    await _do_adopt(message, message.from_user.id)


async def _do_adopt(message: Message, uid: int) -> None:
    """Берёт объявления из кабинетов под управление, объединяя одно авто
    между площадками (BMW X6 на mashina и lalafo → одна запись, обе площадки)."""
    await message.answer("Ищу ваши объявления в кабинетах площадок…")

    # собираем объявления по всем площадкам, группируем по ключу авто
    by_key: dict[str, dict] = {}
    for site in posting_sessions.SITES:
        if not posting_sessions.has_profile(site):
            continue
        try:
            ads = await posting_manage.list_my_ads(site)
        except Exception as e:
            await message.answer(f"⚠️ {site}: не удалось получить кабинет ({e})")
            continue
        if not ads:
            await message.answer(f"▫️ {site}: объявлений в кабинете не видно")
            continue
        for a in ads:
            entry = by_key.setdefault(a["key"], {"title": "", "sites": {}})
            entry["sites"][site] = {
                "status": "posted",
                "external_id": a["external_id"],
                "url": a["url"],
            }
            # mashina-заголовок приоритетнее (есть поколение/комплектация)
            if site == "mashina" or not entry["title"]:
                entry["title"] = a["title"]

    if not by_key:
        await message.answer(
            "Объявлений не найдено. Проверьте вход: /login_status",
            reply_markup=_home_kb(),
        )
        return

    # пересобираем управляемые ботом объявления пользователя начисто,
    # сохраняя ручные настройки (bump_enabled) по совпадающему ключу
    created = merged = 0
    with get_session() as s:
        old = list(s.execute(
            select(PostedAd).where(PostedAd.user_id == uid)
        ).scalars())
        prev_flags = {}
        for p in old:
            k = (p.car_data or {}).get("merge_key")
            if k:
                prev_flags[k] = p.bump_enabled
            s.delete(p)
        for key, entry in by_key.items():
            if len(entry["sites"]) > 1:
                merged += 1
            s.add(PostedAd(
                user_id=uid,
                car_data={"title": entry["title"], "merge_key": key},
                sites=entry["sites"],
                bump_enabled=prev_flags.get(key, True),
            ))
            created += 1
        s.commit()

    note = f" (из них {merged} — на обеих площадках)" if merged else ""
    await message.answer(
        f"Готово: {created} объявлений под управлением{note}.\n"
        f"Список: /my_ads — автоподнятие по расписанию включено.",
        reply_markup=_home_kb(),
    )


# ---------- поиск по слову в архиве ----------

async def _send_find_page(message: Message, keyword: str, offset: int) -> None:
    total = archive.count_archive(keyword=keyword)
    if total == 0:
        await message.answer(
            f"🔎 По слову «{keyword}» в архиве ничего не найдено.\n"
            "Архив наполняется по мере парсинга — попробуйте позже или другое слово.",
            reply_markup=_home_kb(),
        )
        return
    rows = archive.search_archive(keyword=keyword, limit=FIND_PAGE, offset=offset)
    shown_to = offset + len(rows)
    header = (
        f"🔎 «{keyword}»: найдено {total} "
        f"(показаны {offset + 1}–{shown_to})"
    )
    chunks = texts.batch_listings(rows, texts.archived_listing) or [""]
    chunks[0] = f"{header}\n\n{chunks[0]}".rstrip()
    for chunk in chunks[:-1]:
        await message.answer(chunk)
    if shown_to < total:
        import urllib.parse
        token = urllib.parse.quote(keyword)[:48]
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"⬇️ Ещё {min(FIND_PAGE, total - shown_to)}",
                callback_data=f"find:{shown_to}:{token}",
            )
        ]])
        await message.answer(chunks[-1], reply_markup=_with_home(kb))
    else:
        await message.answer(chunks[-1], reply_markup=_home_kb())


@router.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject) -> None:
    keyword = (command.args or "").strip()
    if not keyword:
        await message.answer(
            "🔎 Формат: /find <слово>\n"
            "Найду все спаршенные объявления с этим словом в названии/описании.\n"
            "Примеры: /find пневмо | /find из Кореи | /find панорама"
        )
        return
    await _send_find_page(message, keyword, 0)


@router.callback_query(F.data.startswith("find:"))
async def cb_find_more(cb: CallbackQuery) -> None:
    import urllib.parse
    _, offset_s, token = cb.data.split(":", 2)
    keyword = urllib.parse.unquote(token)
    await cb.answer()
    await _send_find_page(cb.message, keyword, int(offset_s))


# ---------- настройки ----------

async def _send_settings(message: Message) -> None:
    interval = get_setting("check_interval_min")
    tol = get_setting("price_tolerance_pct")
    window = get_setting("keyword_window_min")
    margin = get_setting("sale_margin")
    margin_disp = margin if margin.endswith("%") else f"{margin}$"
    scan_hours = get_setting("full_scan_interval_hours")
    await message.answer(
        f"⚙️ Настройки:\n"
        f"Интервал автопроверки: {interval} мин — /set_interval 60\n"
        f"Допуск цены для «продано»: ±{tol}% — /set_tolerance 10\n"
        f"Маржа-вычет из цены продажи: {margin_disp} — /set_margin 1000 или /set_margin 10%\n"
        f"Окно свежести для ключевых слов: {window} мин — /set_window 60\n"
        f"Полное сканирование рынка (топ-3 цен): раз в {scan_hours} ч — /set_scan_interval 6\n"
        f"{ai_reader.status_line()}\n\n"
        f"Логика поиска по продаже: база = цена продажи − маржа, "
        f"диапазон = база ± допуск%",
        reply_markup=_home_kb(),
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    await _send_settings(message)


@router.message(Command("set_interval"))
async def cmd_set_interval(message: Message) -> None:
    try:
        val = int(message.text.split(maxsplit=1)[1])
        assert 5 <= val <= 1440
    except (IndexError, ValueError, AssertionError):
        await message.answer("Формат: /set_interval 60 (минуты, 5–1440)")
        return
    set_setting("check_interval_min", str(val))
    await message.answer(f"Интервал: {val} мин. Вступит в силу после перезапуска бота.")


@router.message(Command("set_tolerance"))
async def cmd_set_tolerance(message: Message) -> None:
    try:
        val = float(message.text.split(maxsplit=1)[1])
        assert 1 <= val <= 50
    except (IndexError, ValueError, AssertionError):
        await message.answer("Формат: /set_tolerance 10 (проценты, 1–50)")
        return
    set_setting("price_tolerance_pct", str(val))
    await message.answer(f"Допуск по умолчанию: ±{val:g}%")


@router.message(Command("set_margin"))
async def cmd_set_margin(message: Message) -> None:
    from bot.settings_store import parse_margin
    try:
        raw = message.text.split(maxsplit=1)[1].replace(" ", "")
        value, is_pct = parse_margin(raw)
        assert 0 <= value <= (90 if is_pct else 100000)
    except (IndexError, ValueError, AssertionError):
        await message.answer("Формат: /set_margin 1000 (сумма $) или /set_margin 10%")
        return
    set_setting("sale_margin", raw)
    disp = raw if raw.endswith("%") else f"{raw}$"
    await message.answer(
        f"Маржа-вычет по умолчанию: {disp}\n"
        f"Пример: продали за 18 000$ → база поиска "
        f"{'18 000 − ' + disp} — от неё строится диапазон ± допуск."
    )


@router.message(Command("set_window"))
async def cmd_set_window(message: Message) -> None:
    try:
        val = int(message.text.split(maxsplit=1)[1])
        assert 10 <= val <= 1440
    except (IndexError, ValueError, AssertionError):
        await message.answer("Формат: /set_window 60 (минуты, 10–1440)")
        return
    set_setting("keyword_window_min", str(val))
    await message.answer(f"Окно свежести: {val} мин")


@router.message(Command("set_scan_interval"))
async def cmd_set_scan_interval(message: Message) -> None:
    try:
        val = int(message.text.split(maxsplit=1)[1])
        assert 1 <= val <= 48
    except (IndexError, ValueError, AssertionError):
        await message.answer("Формат: /set_scan_interval 6 (часы, 1–48)")
        return
    set_setting("full_scan_interval_hours", str(val))
    await message.answer(
        f"Полное сканирование рынка: раз в {val} ч. "
        f"Вступит в силу после перезапуска бота."
    )


# ---------- пользователи (только владелец) ----------

@router.message(Command("users"))
async def cmd_users(message: Message) -> None:
    await _send_users(message)


async def _send_users(message: Message) -> None:
    with get_session() as s:
        users = list(s.execute(select(AllowedUser)).scalars())
    lines = [f"👑 Владелец: {OWNER_TELEGRAM_ID}"]
    for u in users:
        lines.append(f"• {u.user_id}" + (f" — {u.note}" if u.note else ""))
    lines.append("\nДобавить: /adduser <id> [заметка]\nУдалить: /deluser <id>")
    await message.answer("\n".join(lines), reply_markup=_home_kb())


@router.message(Command("adduser"))
async def cmd_adduser(message: Message, command: CommandObject) -> None:
    if message.from_user.id != OWNER_TELEGRAM_ID:
        return
    try:
        parts = (command.args or "").split(maxsplit=1)
        uid = int(parts[0])
        note = parts[1] if len(parts) > 1 else None
    except (ValueError, IndexError):
        await message.answer("Формат: /adduser 123456789 Азамат")
        return
    with get_session() as s:
        s.merge(AllowedUser(user_id=uid, note=note))
        s.commit()
    await message.answer(
        f"✅ Пользователь {uid} допущен к боту.\n"
        f"Пусть напишет боту /start — у него будут свои запросы и уведомления."
    )


@router.message(Command("deluser"))
async def cmd_deluser(message: Message, command: CommandObject) -> None:
    if message.from_user.id != OWNER_TELEGRAM_ID:
        return
    try:
        uid = int((command.args or "").strip())
    except ValueError:
        await message.answer("Формат: /deluser 123456789")
        return
    with get_session() as s:
        u = s.get(AllowedUser, uid)
        if u:
            s.delete(u)
            s.commit()
    await message.answer(f"Пользователь {uid} удалён из допуска.")
