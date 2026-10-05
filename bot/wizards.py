"""FSM-мастера: единый движок шагов с автозаполнением и мультивыбором.

Потоки: search (поисковый запрос), sold (проданное авто),
archive (поиск по архиву объявлений), keyword (слежение за словами).

На options-шагах с multi=True можно выбрать несколько значений и
переключить режим «искать эти» / «все, кроме этих».
"""

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot import texts
from bot.settings_store import get_setting
from config import DATA_DIR, OWNER_TELEGRAM_ID
from db.models import ArchivedListing, KeywordWatch, PostedAd, SoldCar, WatchRequest
from db.session import get_session
from matching import ai_reader, archive, catalog, generations, market, seller_questions, top_tracker
from parsers import lalafo, mashina

log = logging.getLogger(__name__)
router = Router()


def _home_kb() -> InlineKeyboardMarkup:
    """Кнопка «В меню» на финальных сообщениях мастеров — тот же смысл,
    что и в bot/commands.py (не импортируем оттуда, чтобы не городить
    циклический импорт: commands.py сам вызывает wizards.start_flow)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🏠 В меню", callback_data="menu:home"),
    ]])


def _with_home(kb: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[*kb.inline_keyboard, [
            InlineKeyboardButton(text="🏠 В меню", callback_data="menu:home"),
        ]]
    )


class Wizard(StatesGroup):
    active = State()


# Типы шагов: brand, model, generation, years, range, number, percent,
# options (single или multi=True), text. required=True — без кнопки skip.
FLOWS = {
    "search": [
        {"key": "brand", "kind": "brand", "prompt": "Марка авто? (например: Toyota)", "required": True},
        {"key": "model", "kind": "model", "prompt": "Модель? (выберите или введите)", "required": True},
        {"key": "generation", "kind": "generation", "prompt": "Поколение:", "required": True},
        {"key": "years", "kind": "years", "prompt": "Годы выпуска: диапазон (2018-2023) или один год (2019):"},
        {"key": "price", "kind": "range", "prompt": "Цена от–до в $ (например: 15000-25000):", "required": True},
        {"key": "reg_country", "kind": "options", "prompt": "Страна учета/нахождения (строгий фильтр):", "options": "reg_country"},
        {"key": "country", "kind": "options", "prompt": "Страна производителя (информационно):", "options": "country"},
        {"key": "label", "kind": "text", "prompt": "Метка для отличия похожих запросов (например «кожа/4WD»):"},
        {"key": "color", "kind": "options", "prompt": "Цвет (фильтр):", "options": "color", "multi": True},
        {"key": "body_type", "kind": "options", "prompt": "Тип кузова (фильтр):", "options": "body_type", "multi": True},
        {"key": "transmission", "kind": "options", "prompt": "Коробка (фильтр):", "options": "transmission", "multi": True},
        {"key": "drive", "kind": "options", "prompt": "Привод (фильтр):", "options": "drive", "multi": True},
        {"key": "engine_volume", "kind": "text", "prompt": "Объём двигателя (информационно, например: 2.5):"},
        {"key": "fuel", "kind": "options", "prompt": "Топливо (фильтр):", "options": "fuel", "multi": True},
        {"key": "mileage_max", "kind": "number", "prompt": "Пробег до, км (фильтр, например: 150000):"},
        {"key": "equipment", "kind": "text", "prompt": "Комплектация (информационно):"},
        {"key": "interior", "kind": "options", "prompt": "Салон (информационно):", "options": "interior", "multi": True},
    ],
    "sold": [
        {"key": "brand", "kind": "brand", "prompt": "Марка проданного авто?", "required": True},
        {"key": "model", "kind": "model", "prompt": "Модель? (выберите или введите)", "required": True},
        {"key": "generation", "kind": "generation", "prompt": "Поколение:", "required": True},
        {"key": "year", "kind": "number", "prompt": "Год выпуска проданного авто?", "required": True},
        {"key": "sale_price", "kind": "number", "prompt": "Цена продажи в $?", "required": True},
        {"key": "margin", "kind": "margin", "prompt": "Маржа-вычет из цены продажи — сумма (1000) или процент (10%)"},
        {"key": "tolerance", "kind": "percent", "prompt": "Допуск цены для поиска, %"},
        {"key": "country", "kind": "options", "prompt": "Страна производителя авто? (обязательно)", "options": "country", "required": True},
        {"key": "label", "kind": "text", "prompt": "Метка конфигурации (например «кожа/4WD»):"},
        {"key": "note", "kind": "text", "prompt": "Заметка (кто продал, детали сделки):"},
    ],
    "archive": [
        {"key": "brand", "kind": "brand", "prompt": "Марка? (поиск по архиву объявлений)", "required": True},
        {"key": "model", "kind": "model", "prompt": "Модель? (или пропустите)"},
        {"key": "price_max", "kind": "number", "prompt": "Цена до, $ (или пропустите):"},
        {"key": "status", "kind": "options", "prompt": "Какие объявления показать?", "options": "archive_status", "required": True},
        {"key": "keyword", "kind": "text", "prompt": "Ключевое слово в тексте (или пропустите):"},
    ],
    "keyword": [
        {"key": "keywords", "kind": "text",
         "prompt": "Ключевые слова через запятую — любое совпадение = уведомление.\nНапример: из РФ, из России, растаможен", "required": True},
    ],
    # разовая проверка цены по модели — НЕ создаёт поисковый запрос,
    # просто показывает самые дешёвые актуальные варианты (живой поиск + архив)
    "pricecheck": [
        {"key": "brand", "kind": "brand", "prompt": "Марка авто?", "required": True},
        {"key": "model", "kind": "model", "prompt": "Модель? (выберите или введите)", "required": True},
        {"key": "generation", "kind": "generation", "prompt": "Поколение:", "required": True},
        {"key": "years", "kind": "years", "prompt": "Годы выпуска: диапазон (2018-2023) или один год (2019):"},
        {"key": "tags", "kind": "text",
         "prompt": "Теги через запятую — ИИ найдёт их в описаниях (или пропустите).\n"
                   "• «корея, limited» — ищу по описаниям.\n"
                   "• «страна: корея, комплектация: limited» — если из описания не "
                   "ясно, сам напишу продавцу на lalafo и пришлю его ответ.\n"
                   "Неясные покажу отдельно — в средней цене их не учитываю."},
    ],
    # кросс-платформенная анкета объявления: каждое поле вводится один раз,
    # используется всеми площадками
    "post": [
        {"key": "brand", "kind": "brand", "prompt": "Марка авто?", "required": True},
        {"key": "model", "kind": "model", "prompt": "Модель?", "required": True},
        {"key": "year", "kind": "number", "prompt": "Год выпуска?", "required": True},
        {"key": "price_usd", "kind": "number", "prompt": "Цена в $?", "required": True},
        {"key": "mileage", "kind": "number", "prompt": "Пробег, км?", "required": True},
        {"key": "color", "kind": "options", "prompt": "Цвет:", "options": "color", "required": True},
        {"key": "body_type", "kind": "options", "prompt": "Тип кузова:", "options": "body_type", "required": True},
        {"key": "transmission", "kind": "options", "prompt": "Коробка:", "options": "transmission", "required": True},
        {"key": "drive", "kind": "options", "prompt": "Привод:", "options": "drive", "required": True},
        {"key": "fuel", "kind": "options", "prompt": "Топливо:", "options": "fuel", "required": True},
        {"key": "engine_volume", "kind": "text", "prompt": "Объём двигателя (например 2.5):", "required": True},
        {"key": "steering", "kind": "options", "prompt": "Руль:", "options": "steering", "required": True},
        {"key": "condition", "kind": "options", "prompt": "Состояние:", "options": "condition", "required": True},
        {"key": "reg_country", "kind": "options", "prompt": "Учет (страна регистрации):", "options": "reg_country", "required": True},
        {"key": "city", "kind": "text", "prompt": "Город?", "required": True},
        {"key": "phone", "kind": "text", "prompt": "Телефон для объявления?", "required": True},
        {"key": "description", "kind": "text", "prompt": "Описание объявления (один текст для всех площадок):", "required": True},
        {"key": "photos", "kind": "photos", "prompt": "Пришлите фото авто (несколько сообщений подряд), затем нажмите «Готово»:", "required": True},
    ],
    # шины / диски / колёса в сборе
    "post_part": [
        {"key": "part_type", "kind": "options", "prompt": "Что продаёте?",
         "choices": ["Шины", "Диски", "Колёса в сборе"], "required": True},
        {"key": "title", "kind": "text", "required": True,
         "prompt": "Заголовок, например «Зимние шины Bridgestone 225/60 R17, комплект»:"},
        {"key": "state", "kind": "options", "prompt": "Состояние:", "choices": ["Б/у", "Новый"], "required": True},
        {"key": "diameter", "kind": "text", "prompt": "Диаметр R, например 17:", "required": True},
        {"key": "width", "kind": "text", "prompt": "Ширина шины, например 225 (для дисков — пропустите):"},
        {"key": "profile", "kind": "text", "prompt": "Профиль шины, например 60 (для дисков — пропустите):"},
        {"key": "season", "kind": "options", "prompt": "Сезон (для дисков — пропустите):",
         "choices": ["Лето", "Зима", "Зимняя шипованная", "Всесезонная"]},
        {"key": "rim_type", "kind": "options", "prompt": "Вид диска (для шин — пропустите):",
         "choices": ["Литые", "Железные", "Другие"]},
        {"key": "bolts", "kind": "text", "prompt": "Сколько крепёжных отверстий, например 5 (для шин — пропустите):"},
        {"key": "pcd", "kind": "text",
         "prompt": "Сверловка (PCD), например 5x114.3 — нужна mashina.kg для дисков (для шин — пропустите):"},
        {"key": "quantity", "kind": "options", "prompt": "Количество:",
         "choices": ["Комплект", "Пара", "1 шт", "Запаска"], "required": True},
        {"key": "brand", "kind": "text", "prompt": "Бренд (или пропустите):"},
        {"key": "price_usd", "kind": "number", "prompt": "Цена в $?", "required": True},
        {"key": "description", "kind": "text", "prompt": "Описание объявления:", "required": True},
        {"key": "photos", "kind": "photos", "prompt": "Пришлите фото (несколько сообщений подряд), затем нажмите «Готово»:", "required": True},
    ],
}

SEARCH_EXTRA_KEYS = {
    "color", "body_type", "transmission", "drive",
    "engine_volume", "fuel", "mileage_max", "equipment", "interior",
}

ARCHIVE_STATUS_OPTIONS = ["Все", "Только активные", "Только проданные"]


# ---------- клавиатуры ----------

def _kb(buttons: list[str], skip: bool, prefix: str = "w:opt",
        selected: list[str] | None = None, multi: bool = False,
        mode: str = "include", back: bool = False) -> InlineKeyboardMarkup:
    rows, row = [], []
    sel = set(selected or [])
    for i, b in enumerate(buttons):
        label = ("✅ " if b in sel else "") + b
        row.append(InlineKeyboardButton(text=label, callback_data=f"{prefix}:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if multi:
        mode_label = "🚫 Режим: КРОМЕ выбранных" if mode == "exclude" else "🔍 Режим: искать выбранные"
        rows.append([InlineKeyboardButton(text=mode_label, callback_data="w:mode")])
        rows.append([InlineKeyboardButton(text="✔️ Готово", callback_data="w:done")])
    tail = []
    if back:
        tail.append(InlineKeyboardButton(text="⬅️ Назад", callback_data="w:back"))
    if skip:
        tail.append(InlineKeyboardButton(text="Пропустить ➡️", callback_data="w:skip"))
    tail.append(InlineKeyboardButton(text="❌ Отмена", callback_data="w:cancel"))
    rows.append(tail)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _gen_kb(labels: list[str], selected: list[int], back: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура мультивыбора поколений. Последний пункт — «Любое поколение»."""
    rows, row = [], []
    sel = set(selected)
    for i, b in enumerate(labels):
        text = ("✅ " if i in sel else "") + b
        row.append(InlineKeyboardButton(text=text, callback_data=f"w:gen:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✔️ Готово", callback_data="w:gendone")])
    tail = []
    if back:
        tail.append(InlineKeyboardButton(text="⬅️ Назад", callback_data="w:back"))
    tail.append(InlineKeyboardButton(text="❌ Отмена", callback_data="w:cancel"))
    rows.append(tail)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _step_all_options(step: dict) -> list[str]:
    if step.get("choices"):
        return step["choices"]
    if step.get("options") == "archive_status":
        return ARCHIVE_STATUS_OPTIONS
    return await catalog.get_options(step["options"])


# ---------- рендер шага ----------

async def _render_step(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow, idx = data["flow"], data["step"]
    steps = FLOWS[flow]

    if idx >= len(steps):
        await _finish(message, state)
        return

    step = steps[idx]
    kind = step["kind"]
    skip = not step.get("required")
    back = idx > 0

    if kind not in ("options", "model", "generation"):
        await state.update_data(options=[])

    if kind == "brand":
        await message.answer(step["prompt"], reply_markup=_kb([], skip=skip, back=back))
        return
    if kind == "model":
        await message.answer("Собираю список моделей…")
        models = await mashina.models_for_make(data["values"]["brand"])
        await state.update_data(model_options=models, options=models[:8])
        await message.answer(step["prompt"], reply_markup=_kb(models[:8], skip=skip, back=back))
        return
    if kind == "generation":
        await message.answer("Ищу поколения на mashina.kg…")
        gens = await generations.fetch_generations(
            data["values"]["brand"], data["values"]["model"]
        )
        labels = [
            f"{g.generation_name} ({g.year_from}–{g.year_to or 'н.в.'})" for g in gens[:12]
        ] + ["Любое поколение"]
        await state.update_data(
            gen_codes=[g.generation_code for g in gens[:12]] + ["any"],
            gen_labels=labels,
            gen_sel=[],
        )
        await message.answer(
            step["prompt"] + "\nМожно выбрать НЕСКОЛЬКО поколений, затем «Готово».",
            reply_markup=_gen_kb(labels, [], back=back),
        )
        return
    if kind == "options":
        options = await _step_all_options(step)
        multi = bool(step.get("multi"))
        await state.update_data(
            options=options[:8], all_options=options, sel=[], opt_mode="include"
        )
        hint = "\nМожно выбрать несколько и нажать «Готово». Текст фильтрует список." if multi \
            else "\nВыберите кнопкой или начните вводить текст:"
        await message.answer(
            step["prompt"] + hint,
            reply_markup=_kb(options[:8], skip=skip, multi=multi, back=back),
        )
        return
    if kind == "photos":
        await state.update_data(photos=[])
        tail = [InlineKeyboardButton(text="✔️ Готово", callback_data="w:done")]
        if back:
            tail.append(InlineKeyboardButton(text="⬅️ Назад", callback_data="w:back"))
        tail.append(InlineKeyboardButton(text="❌ Отмена", callback_data="w:cancel"))
        await message.answer(step["prompt"], reply_markup=InlineKeyboardMarkup(inline_keyboard=[tail]))
        return
    if kind == "percent":
        default_tol = get_setting("price_tolerance_pct")
        await message.answer(
            f"{step['prompt']} (по умолчанию {default_tol}%):",
            reply_markup=_kb([], skip=True, back=back),
        )
        return
    if kind == "margin":
        default_margin = get_setting("sale_margin")
        suffix = "" if default_margin.endswith("%") else "$"
        await message.answer(
            f"{step['prompt']} (по умолчанию {default_margin}{suffix}, "
            "изменить дефолт: /set_margin):",
            reply_markup=_kb([], skip=True, back=back),
        )
        return
    if kind == "years":
        values = data["values"]
        hint = ""
        if values.get("generation_code"):
            hint = (
                f"\nВыбрано поколение {values.get('generation_name')} "
                f"({values.get('year_from')}–{values.get('year_to')}) — "
                "можно сузить годы внутри него или пропустить."
            )
        await message.answer(step["prompt"] + hint, reply_markup=_kb([], skip=True, back=back))
        return
    await message.answer(step["prompt"], reply_markup=_kb([], skip=skip, back=back))


async def _save_value(state: FSMContext, key: str, value) -> None:
    data = await state.get_data()
    values = data["values"]
    values[key] = value
    await state.update_data(values=values)


async def _advance(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.update_data(step=data["step"] + 1, pending_text=None)
    await _render_step(message, state)


async def _go_back(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    step = max(0, data["step"] - 1)
    await state.update_data(step=step, pending_text=None)
    await _render_step(message, state)


# ---------- старт потоков ----------

async def start_flow(flow: str, message: Message, state: FSMContext, user_id: int) -> None:
    await state.clear()
    await state.set_state(Wizard.active)
    await state.update_data(flow=flow, step=0, values={}, user_id=user_id)
    await _render_step(message, state)


@router.message(Command("add_search"))
async def add_search_start(message: Message, state: FSMContext) -> None:
    await start_flow("search", message, state, message.from_user.id)


@router.message(Command("add_sold"))
async def add_sold_start(message: Message, state: FSMContext) -> None:
    await start_flow("sold", message, state, message.from_user.id)


@router.message(Command("archive"))
async def archive_start(message: Message, state: FSMContext) -> None:
    await start_flow("archive", message, state, message.from_user.id)


@router.message(Command("add_keyword"))
async def keyword_start(message: Message, state: FSMContext) -> None:
    await start_flow("keyword", message, state, message.from_user.id)


@router.message(Command("pricecheck"))
async def pricecheck_start(message: Message, state: FSMContext) -> None:
    await start_flow("pricecheck", message, state, message.from_user.id)


@router.message(Command("post"))
async def post_start(message: Message, state: FSMContext) -> None:
    await start_flow("post", message, state, message.from_user.id)


@router.message(Wizard.active, F.text == "/cancel")
async def wizard_cancel_cmd(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Мастер отменён.", reply_markup=_home_kb())


@router.message(Wizard.active, F.text.startswith("/"))
async def wizard_other_command(message: Message) -> None:
    await message.answer("Сначала завершите мастер или отмените его: /cancel")


# ---------- ввод текста ----------

# ---------- фото в мастере ----------
# Альбом — это 7–10 сообщений разом, и aiogram обрабатывает их ПАРАЛЛЕЛЬНО.
# Раньше каждое видело «0 фото», все писали в один 00.jpg и затирали список
# друг друга: в черновике оставалось одно фото, склеенное из кусков разных
# (верх целый, дальше цветной мусор). Теперь: замок на пользователя для
# состояния, своё имя файла у каждого фото, загрузка во временный файл с
# проверкой размера, одно подтверждение на весь альбом.

_photo_locks: dict[int, asyncio.Lock] = {}
_photo_notice: dict[int, asyncio.Task] = {}
_photos_in_flight: dict[int, int] = {}
PHOTO_NOTICE_DELAY = 1.5


async def _save_photo(message: Message, state: FSMContext) -> None:
    import time as _time
    from pathlib import Path as _Path

    uid = message.from_user.id
    lock = _photo_locks.setdefault(uid, asyncio.Lock())
    ph = message.photo[-1]
    _photos_in_flight[uid] = _photos_in_flight.get(uid, 0) + 1
    try:
        async with lock:
            data = await state.get_data()
            photos_dir = data.get("photos_dir") or str(
                DATA_DIR / "photos" / f"{data['user_id']}_{int(_time.time())}")
            _Path(photos_dir).mkdir(parents=True, exist_ok=True)
            await state.update_data(photos_dir=photos_dir)
        # имя по номеру сообщения — порядок фото как в альбоме
        dest = _Path(photos_dir) / f"{message.message_id:010d}_{ph.file_unique_id}.jpg"
        tmp = dest.with_suffix(".part")
        for _ in range(3):
            await message.bot.download(ph, destination=tmp)
            if tmp.exists() and (not ph.file_size or tmp.stat().st_size == ph.file_size):
                break
        else:
            tmp.unlink(missing_ok=True)
            await message.answer("⚠️ Одно фото не скачалось целиком — пришлите его ещё раз.")
            return
        tmp.replace(dest)
        async with lock:
            data = await state.get_data()
            await state.update_data(photos=[*data.get("photos", []), str(dest)])
    finally:
        _photos_in_flight[uid] -= 1
    old = _photo_notice.pop(uid, None)
    if old:
        old.cancel()
    _photo_notice[uid] = asyncio.create_task(_photo_notice_later(message, state))


async def wait_photos(user_id: int, state: FSMContext) -> dict:
    """«Готово» нажали, пока фото из альбома ещё качаются — дожидаемся."""
    for _ in range(120):
        if not _photos_in_flight.get(user_id):
            break
        await asyncio.sleep(0.5)
    return await state.get_data()


async def _photo_notice_later(message: Message, state: FSMContext) -> None:
    try:
        await asyncio.sleep(PHOTO_NOTICE_DELAY)
    except asyncio.CancelledError:
        return
    data = await state.get_data()
    if not data.get("flow"):
        return  # мастер уже закончен
    n = len(data.get("photos", []))
    done_cb = data.get("done_cb") or "w:done"
    await message.answer(
        f"📸 Сохранено фото: {n}. Пришлите ещё или нажмите «Готово».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Готово", callback_data=done_cb)]]),
    )


@router.message(Wizard.active)
async def wizard_message(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    steps = FLOWS[data["flow"]]
    if data["step"] >= len(steps):
        await _finish(message, state)
        return
    step = steps[data["step"]]
    kind = step["kind"]
    text = (message.text or "").strip()

    if kind == "photos":
        if not message.photo:
            await message.answer("Пришлите фото или нажмите «Готово».")
            return
        await _save_photo(message, state)
        return

    if kind == "brand":
        exact, similar = await catalog.resolve_make(text)
        if exact:
            if exact != text:
                await message.answer(f"Марка распознана как: {exact}")
            await _save_value(state, "brand", exact)
            await _advance(message, state)
        elif similar:
            await state.update_data(options=similar)
            await message.answer(
                "Точного совпадения нет. Похожие марки:",
                reply_markup=_kb(similar, skip=not step.get("required"), back=data["step"] > 0),
            )
        else:
            await message.answer("Марка не найдена на mashina.kg. Введите ещё раз:")
        return

    if kind == "model":
        models = data.get("model_options", [])
        matched = catalog.suggest(models, text)
        if len(matched) == 1 and mashina.translit(matched[0]) == mashina.translit(text):
            await _save_value(state, "model", matched[0])
            await _advance(message, state)
            return
        if matched and any(mashina.translit(m) != mashina.translit(text) for m in matched):
            await state.update_data(options=matched)
            await message.answer("Уточните модель:", reply_markup=_kb(matched, skip=not step.get("required"), back=data["step"] > 0))
            return
        await message.answer("Проверяю модель на mashina.kg…")
        slug = await mashina.resolve_model(data["values"]["brand"], text)
        if slug is None:
            await message.answer(
                "Объявлений с такой моделью не нашлось — возможно, опечатка. Введите ещё раз:"
            )
            return
        if slug != text:
            await message.answer(f"Модель распознана как: {slug}")
        await _save_value(state, "model", slug)
        await _advance(message, state)
        return

    if kind == "generation":
        await message.answer("Выберите поколение кнопкой.")
        return

    if kind == "years":
        cleaned = text.replace(" ", "")
        try:
            if "-" in cleaned:
                y1, y2 = (int(x) for x in cleaned.split("-"))
            else:
                y1 = y2 = int(cleaned)
        except ValueError:
            await message.answer("Формат: 2018-2023 или один год 2019. Попробуйте ещё раз.")
            return
        gen_from = data["values"].get("year_from")
        gen_to = data["values"].get("year_to")
        if data["values"].get("generation_code") and gen_from and gen_to:
            if y2 < gen_from or y1 > gen_to:
                await message.answer(
                    f"Эти годы не пересекаются с выбранным поколением "
                    f"({gen_from}–{gen_to}). Введите годы внутри поколения или пропустите."
                )
                return
        await _save_value(state, "year_from", y1)
        await _save_value(state, "year_to", y2)
        # годы задал сам пользователь (иначе year_from/to — это диапазон поколения)
        await _save_value(state, "years_explicit", True)
        await _advance(message, state)
        return

    if kind == "range":
        try:
            lo, hi = (float(x) for x in text.replace(" ", "").split("-"))
        except ValueError:
            await message.answer("Формат: 15000-25000. Попробуйте ещё раз.")
            return
        await _save_value(state, step["key"] + "_min", lo)
        await _save_value(state, step["key"] + "_max", hi)
        await _advance(message, state)
        return

    if kind in ("number", "percent"):
        try:
            num = float(text.replace(" ", "").replace("%", ""))
        except ValueError:
            await message.answer("Введите число.")
            return
        await _save_value(state, step["key"], num)
        await _advance(message, state)
        return

    if kind == "margin":
        from bot.settings_store import parse_margin
        try:
            parse_margin(text)
        except ValueError:
            await message.answer("Формат: 1000 (сумма в $) или 10% (процент).")
            return
        await _save_value(state, "margin", text.replace(" ", ""))
        await _advance(message, state)
        return

    if kind == "options":
        multi = bool(step.get("multi"))
        all_opts = data.get("all_options", [])
        matched = catalog.suggest(all_opts, text)
        exact = [m for m in matched if mashina.translit(m) == mashina.translit(text)]
        if multi:
            sel = data.get("sel", [])
            value = exact[0] if exact else (matched[0] if len(matched) == 1 else None)
            if value is None and not matched:
                value = text  # свободное значение
            if value is not None:
                if value not in sel:
                    sel.append(value)
                await state.update_data(sel=sel, options=all_opts[:8])
                await message.answer(
                    f"Добавлено: {value}. Выбрано: {', '.join(sel)}",
                    reply_markup=_kb(all_opts[:8], skip=not step.get("required"),
                                     selected=sel, multi=True,
                                     mode=data.get("opt_mode", "include"),
                                     back=data["step"] > 0),
                )
                return
            await state.update_data(options=matched)
            await message.answer(
                "Уточните вариант:",
                reply_markup=_kb(matched, skip=not step.get("required"),
                                 selected=data.get("sel", []), multi=True,
                                 mode=data.get("opt_mode", "include"),
                                 back=data["step"] > 0),
            )
            return
        # одиночный выбор
        if exact:
            await _save_value(state, step["key"], exact[0])
            await _advance(message, state)
            return
        if data.get("pending_text") == text or not matched:
            await _save_value(state, step["key"], text)
            await _advance(message, state)
            return
        await state.update_data(options=matched, pending_text=text)
        await message.answer(
            "Выберите вариант (или отправьте тот же текст ещё раз, чтобы записать как есть):",
            reply_markup=_kb(matched, skip=not step.get("required"), back=data["step"] > 0),
        )
        return

    # text
    await _save_value(state, step["key"], text)
    await _advance(message, state)


# ---------- кнопки ----------

@router.callback_query(Wizard.active, F.data.startswith("w:opt:"))
async def wizard_option(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(cb.data.rsplit(":", 1)[1])
    options = data.get("options", [])
    if idx >= len(options) or data["step"] >= len(FLOWS[data["flow"]]):
        await cb.answer("Устаревшая кнопка")
        return
    step = FLOWS[data["flow"]][data["step"]]
    value = options[idx]

    if step.get("multi"):
        sel = data.get("sel", [])
        if value in sel:
            sel.remove(value)
        else:
            sel.append(value)
        await state.update_data(sel=sel)
        try:
            await cb.message.edit_reply_markup(
                reply_markup=_kb(options, skip=not step.get("required"),
                                 selected=sel, multi=True,
                                 mode=data.get("opt_mode", "include"),
                                 back=data["step"] > 0)
            )
        except Exception:
            pass
        await cb.answer(("✅ " if value in sel else "➖ ") + value)
        return

    await _save_value(state, step["key"], value)
    await cb.answer(value)
    await _advance(cb.message, state)


@router.callback_query(Wizard.active, F.data == "w:mode")
async def wizard_mode(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if data["step"] >= len(FLOWS[data["flow"]]):
        await cb.answer("Устаревшая кнопка")
        return
    step = FLOWS[data["flow"]][data["step"]]
    mode = "exclude" if data.get("opt_mode", "include") == "include" else "include"
    await state.update_data(opt_mode=mode)
    try:
        await cb.message.edit_reply_markup(
            reply_markup=_kb(data.get("options", []), skip=not step.get("required"),
                             selected=data.get("sel", []), multi=True, mode=mode,
                             back=data["step"] > 0)
        )
    except Exception:
        pass
    await cb.answer("Режим: " + ("все КРОМЕ выбранных" if mode == "exclude" else "искать выбранные"))


@router.callback_query(Wizard.active, F.data == "w:done")
async def wizard_done(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if data["step"] >= len(FLOWS[data["flow"]]):
        await cb.answer("Устаревшая кнопка")
        return
    step = FLOWS[data["flow"]][data["step"]]
    if step["kind"] == "photos":
        data = await wait_photos(cb.from_user.id, state)
        photos = data.get("photos", [])
        if step.get("required") and not photos:
            await cb.answer("Нужно хотя бы одно фото")
            return
        await _save_value(state, "photos", photos)
        await cb.answer(f"Фото: {len(photos)}")
        await _advance(cb.message, state)
        return
    sel = data.get("sel", [])
    if not sel:
        if step.get("required"):
            await cb.answer("Выберите хотя бы один вариант")
            return
        await cb.answer("Пропущено")
        await _advance(cb.message, state)
        return
    await _save_value(state, step["key"], {
        "mode": data.get("opt_mode", "include"),
        "values": sel,
    })
    await cb.answer("Сохранено: " + ", ".join(sel))
    await _advance(cb.message, state)


@router.callback_query(Wizard.active, F.data.startswith("w:gen:"))
async def wizard_generation(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    idx = int(cb.data.rsplit(":", 1)[1])
    labels = data.get("gen_labels", [])
    codes = data.get("gen_codes", [])
    if idx >= len(labels):
        await cb.answer("Устаревшая кнопка")
        return
    sel = data.get("gen_sel", [])
    any_idx = len(labels) - 1  # «Любое поколение» — последний
    if idx == any_idx:
        sel = [] if any_idx in sel else [any_idx]  # «любое» эксклюзивно
    else:
        sel = [i for i in sel if i != any_idx]  # снимаем «любое» при выборе конкретного
        if idx in sel:
            sel.remove(idx)
        else:
            sel.append(idx)
    await state.update_data(gen_sel=sel)
    try:
        await cb.message.edit_reply_markup(reply_markup=_gen_kb(labels, sel, back=data["step"] > 0))
    except Exception:
        pass
    await cb.answer(("✅ " if idx in sel else "➖ ") + labels[idx])


@router.callback_query(Wizard.active, F.data == "w:gendone")
async def wizard_generation_done(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    labels = data.get("gen_labels", [])
    codes = data.get("gen_codes", [])
    sel = data.get("gen_sel", [])
    any_idx = len(labels) - 1
    # «любое» или ничего не выбрано → без фильтра по поколению
    chosen = [i for i in sel if i != any_idx]
    if not chosen or any_idx in sel:
        await _save_value(state, "generations", [])
        await _save_value(state, "generation_code", None)
        await _save_value(state, "generation_name", None)
        await cb.answer("Поколение: любое")
        await _advance(cb.message, state)
        return
    all_gens = generations.get_generations(
        data["values"]["brand"], data["values"]["model"]
    )
    picked = []
    years_from, years_to = [], []
    for i in chosen:
        code = codes[i]
        gen = next((g for g in all_gens if g.generation_code == code), None)
        if gen:
            picked.append({
                "code": gen.generation_code,
                "name": gen.generation_name,
                "year_from": gen.year_from,
                "year_to": gen.year_to,
            })
            if gen.year_from:
                years_from.append(gen.year_from)
            if gen.year_to:
                years_to.append(gen.year_to)
    await _save_value(state, "generations", picked)
    # для обратной совместимости и отображения — первое поколение
    if picked:
        await _save_value(state, "generation_code", picked[0]["code"])
        await _save_value(state, "generation_name",
                          " + ".join(p["name"] for p in picked))
        # объединённый диапазон годов (для поиска и lalafo-фильтрации по году)
        await _save_value(state, "year_from", min(years_from) if years_from else None)
        await _save_value(state, "year_to", max(years_to) if years_to else None)
    await cb.answer(f"Выбрано поколений: {len(picked)}")
    await _advance(cb.message, state)


@router.callback_query(Wizard.active, F.data == "w:skip")
async def wizard_skip(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    steps = FLOWS[data["flow"]]
    if data["step"] < len(steps) and steps[data["step"]]["kind"] == "years":
        await _save_value(state, "years_explicit", False)
    await cb.answer("Пропущено")
    await _advance(cb.message, state)


@router.callback_query(Wizard.active, F.data == "w:back")
async def wizard_back(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer("Назад")
    await _go_back(cb.message, state)


@router.callback_query(Wizard.active, F.data == "w:cancel")
async def wizard_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.message.answer("Мастер отменён.", reply_markup=_home_kb())
    await cb.answer()


# ---------- завершение потоков ----------

def _build_request(values: dict, user_id: int) -> WatchRequest:
    extra = {k: v for k, v in values.items() if k in SEARCH_EXTRA_KEYS and v}
    return WatchRequest(
        user_id=user_id,
        source_type="manual",
        brand=values["brand"],
        model=values["model"],
        generation_code=values.get("generation_code"),
        generation_name=values.get("generation_name"),
        generations=values.get("generations") or [],
        year_from=values.get("year_from"),
        year_to=values.get("year_to"),
        price_min=values.get("price_min"),
        price_max=values.get("price_max"),
        country=_scalar(values.get("country")),
        reg_country=_scalar(values.get("reg_country")),
        label=values.get("label"),
        extra_params=extra,
    )


def _scalar(v):
    """options-шаги без multi могут отдать dict при старых данных."""
    if isinstance(v, dict):
        vals = v.get("values") or []
        return vals[0] if vals else None
    return v


def _build_sold(values: dict, user_id: int) -> SoldCar:
    return SoldCar(
        user_id=user_id,
        brand=values["brand"],
        model=values["model"],
        generation_code=values.get("generation_code"),
        generation_name=values.get("generation_name"),
        year=int(values["year"]) if values.get("year") else None,
        sale_price=values["sale_price"],
        price_tolerance_pct=values.get(
            "tolerance", float(get_setting("price_tolerance_pct"))
        ),
        margin=values.get("margin", get_setting("sale_margin")),
        country=_scalar(values["country"]),
        label=values.get("label"),
        note=values.get("note"),
    )


async def _finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    flow, values = data["flow"], data["values"]

    if flow == "archive":
        include_sold = values.get("status") != "Только активные"
        only_sold = values.get("status") == "Только проданные"
        rows = archive.search_archive(
            brand=values.get("brand"),
            model=values.get("model"),
            price_max=values.get("price_max"),
            keyword=values.get("keyword"),
            include_sold=include_sold,
            limit=10,
        )
        if only_sold:
            rows = [r for r in rows if not r.is_active][:10]
        await state.clear()
        if not rows:
            await message.answer(
                "В архиве ничего не найдено по этим условиям.", reply_markup=_home_kb()
            )
            return
        chunks = texts.batch_listings(rows, texts.archived_listing)
        chunks[0] = f"📦 Архив: найдено {len(rows)} (показаны свежие):\n\n{chunks[0]}"
        for chunk in chunks[:-1]:
            await message.answer(chunk)
        await message.answer(chunks[-1], reply_markup=_home_kb())
        return

    if flow in ("post", "post_part"):
        car_data = {k: _scalar(v) if not isinstance(v, list) else v
                    for k, v in values.items() if k not in ("photos",)}
        car_data["kind"] = "part" if flow == "post_part" else "car"
        ad = PostedAd(
            user_id=data["user_id"],
            car_data=car_data,
            photos_dir=data.get("photos_dir"),
            description=values.get("description"),
            price_usd=values.get("price_usd"),
            sites={},
            is_active=False,  # станет активным после публикации
        )
        with get_session() as s:
            s.add(ad)
            s.commit()
        await state.clear()
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"pubad:{ad.id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"pubcancel:{ad.id}"),
        ]])
        await message.answer(
            f"📝 Проверьте объявление перед публикацией:\n\n{texts.posted_ad_preview(ad)}\n\n"
            f"Фото: {len(values.get('photos') or [])}\n"
            "Опубликую на lalafo. На mashina автопубликация пока не подключена.",
            reply_markup=kb,
        )
        return

    if flow == "pricecheck":
        await state.clear()
        await _pricecheck(message, state, values)
        return

    if flow == "keyword":
        kws = [k.strip() for k in str(values["keywords"]).split(",") if k.strip()]
        watch = KeywordWatch(user_id=data["user_id"], keywords=kws)
        with get_session() as s:
            s.add(watch)
            s.commit()
        await state.clear()
        window = get_setting("keyword_window_min")
        await message.answer(
            f"🔔 Слежение #{watch.id} создано: {', '.join(kws)}\n"
            f"Окно свежести: {window} мин (настройка /settings).\n"
            f"Список: /keywords",
            reply_markup=_home_kb(),
        )
        return

    # search / sold — предпросмотр с подтверждением
    if flow == "search":
        preview = texts.request_summary(_build_request(values, data["user_id"]))
        head = "Проверьте запрос:"
    else:
        preview = texts.sold_summary(_build_sold(values, data["user_id"]))
        head = "Проверьте запись о продаже:"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Сохранить", callback_data="w:save"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="w:back"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="w:cancel"),
    ]])
    await message.answer(f"{head}\n\n{preview}", reply_markup=kb)


@router.callback_query(Wizard.active, F.data == "w:save")
async def wizard_save(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    values = data["values"]
    user_id = data.get("user_id") or cb.from_user.id
    if data["flow"] == "search":
        req = _build_request(values, user_id)
        with get_session() as s:
            s.add(req)
            s.commit()
        await cb.message.answer(
            f"✅ Запрос #{req.id} сохранён и активен.\n"
            "Проверка по расписанию, либо вручную: /check",
            reply_markup=_home_kb(),
        )
    else:
        from bot.settings_store import apply_margin

        car = _build_sold(values, user_id)
        tol = car.price_tolerance_pct / 100
        # база поиска = цена продажи минус маржа; диапазон = база ± допуск
        base = apply_margin(car.sale_price, car.margin or "0")
        req = WatchRequest(
            user_id=user_id,
            source_type="from_sale",
            brand=car.brand,
            model=car.model,
            generation_code=car.generation_code,
            generation_name=car.generation_name,
            generations=values.get("generations") or [],
            year_from=values.get("year_from"),
            year_to=values.get("year_to"),
            price_min=round(base * (1 - tol)),
            price_max=round(base * (1 + tol)),
            country=car.country,
            label=car.label,
        )
        with get_session() as s:
            s.add(req)
            s.flush()
            car.watch_request_id = req.id
            s.add(car)
            s.commit()
        margin_note = ""
        if base != car.sale_price:
            margin_note = (
                f"\nБаза поиска: {texts.fmt_price(car.sale_price)} − "
                f"маржа {car.margin} = {texts.fmt_price(base)}"
            )
        await cb.message.answer(
            f"✅ Продажа записана, автосоздан поисковый запрос #{req.id} "
            f"({texts.fmt_price(req.price_min)} – {texts.fmt_price(req.price_max)})."
            f"{margin_note}\nПроверить сейчас: /check",
            reply_markup=_home_kb(),
        )
    await state.clear()
    await cb.answer()


# ---------- публикация после предпросмотра ----------

_publishing: set[int] = set()


@router.callback_query(F.data.startswith("pubad:"))
async def publish_ad(cb: CallbackQuery) -> None:
    ad_id = int(cb.data.split(":")[1])
    if ad_id in _publishing:
        await cb.answer("Уже публикую")
        return
    with get_session() as s:
        ad = s.get(PostedAd, ad_id)
    if ad is None or ad.user_id != cb.from_user.id:
        await cb.answer("Объявление не найдено", show_alert=True)
        return
    from posting import post as posting_post

    todo = posting_post.pending_sites(ad)
    if not todo:
        await cb.answer("Уже опубликовано везде")
        return
    _publishing.add(ad_id)
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    names = ", ".join(texts.SITE_NAMES.get(s, s) for s in todo)
    status = await cb.message.answer(f"⏳ Публикую на {names} (до пары минут)…")
    try:
        results = await posting_post.post_all(ad)
    finally:
        _publishing.discard(ad_id)
    ok = any(r.ok for r in results.values())
    if ok:
        with get_session() as s:
            s.get(PostedAd, ad_id).is_active = True
            s.commit()
    lines = [f"{'✅' if ok else '⚠️'} Объявление #{ad_id}:"]
    for site, res in results.items():
        mark = "✅" if res.ok else ("💳" if res.payment_required else "▫️")
        lines.append(f"{mark} {res.message}")
    lines.append("\nУправление и поднятия: 📤 Мои объявления")
    await status.edit_text("\n".join(lines), reply_markup=_home_kb(), disable_web_page_preview=True)


@router.callback_query(F.data.startswith("pubcancel:"))
async def publish_cancel(cb: CallbackQuery) -> None:
    ad_id = int(cb.data.split(":")[1])
    with get_session() as s:
        ad = s.get(PostedAd, ad_id)
        if ad is not None and ad.user_id == cb.from_user.id and not (ad.sites or {}):
            s.delete(ad)
            s.commit()
    await cb.answer()
    await cb.message.edit_text("Публикация отменена.", reply_markup=_home_kb())


# ---------- /pricecheck: поиск, годы, теги, вопросы продавцам ----------

TAG_SCAN_LIMIT = 25  # сколько самых дешёвых проверять по тегам (ИИ локальный — не быстрый)


def _pc_label(values: dict) -> str:
    label = f"{values['brand']} {values['model']}"
    if values.get("generation_name"):
        label += f" {values['generation_name']}"
    if values.get("years_explicit"):
        yf, yt = values.get("year_from"), values.get("year_to")
        label += f" {yf}" if yf == yt else f" {yf}–{yt}"
    return label


def _load_row(row_id: int) -> ArchivedListing | None:
    with get_session() as s:
        return s.get(ArchivedListing, row_id)


async def _send_price_list(message: Message, header: str, rows: list[ArchivedListing],
                           home: bool = True) -> None:
    rows = sorted(rows, key=lambda r: r.price_usd)
    avg = sum(r.price_usd for r in rows) / len(rows)
    chunks = texts.batch_listings(rows[:5], texts.archived_listing)
    liq = market.liquidity_line(rows[0].brand, rows[0].model)
    head = f"{header}\nСредняя цена: {texts.fmt_price(avg)}" + (f"\n{liq}" if liq else "")
    chunks[0] = f"{head}\n\n{chunks[0]}"
    for chunk in chunks[:-1]:
        await message.answer(chunk)
    await message.answer(chunks[-1], reply_markup=_home_kb() if home else None)


async def _pricecheck(message: Message, state: FSMContext, values: dict) -> None:
    brand, model = values["brand"], values["model"]
    label = _pc_label(values)
    tags_text = (values.get("tags") or "").strip()
    status = await message.answer(f"🔍 Ищу актуальные {label}…")

    live = []
    try:
        live += await mashina.search(brand, model, max_pages=3)
    except Exception:
        log.exception("pricecheck: mashina search failed")
    try:
        live += await lalafo.search(brand, model, max_pages=2)
    except Exception:
        log.exception("pricecheck: lalafo search failed")
    if live:
        archive.upsert_listings(live)  # заодно пополняем общий архив

    rows = archive.search_archive(brand=brand, model=model, include_sold=False, limit=300)
    codes = [g.get("code") for g in values.get("generations") or [] if g.get("code")]
    if not codes and values.get("generation_code"):
        codes = [values["generation_code"]]
    if codes:
        rows = [r for r in rows if r.generation_code in codes]
    if values.get("years_explicit"):
        yf, yt = values["year_from"], values["year_to"]
        rows = [r for r in rows if r.year and yf <= r.year <= yt]
    rows = [r for r in rows if top_tracker.is_local_market(r) and r.price_usd is not None]
    # «8$», «320 сом» и прочие опечатки продавцов — не самые дешёвые машины
    rows, _odd_prices = market.split_suspicious(rows)
    rows.sort(key=lambda r: r.price_usd)
    if any(ai_reader.pending(r) for r in rows[:10]):
        await status.edit_text(f"🤖 Читаю описания самых дешёвых {label}…")
        await ai_reader.analyze(rows[:10])
    priced = [r for r in rows if ai_reader.market_ok(r)]
    if not priced:
        await status.edit_text(f"По {label} актуальных объявлений с ценой не нашлось.",
                               reply_markup=_home_kb())
        return

    if not tags_text:
        await status.edit_text(f"💵 {label}: найдено {len(priced)} актуальных объявлений")
        await _send_price_list(message, f"💵 {label}", priced)
        return

    if not ai_reader.available():
        await status.edit_text(
            "🤖 ИИ сейчас недоступен — поиск по тегам не получится. "
            "Проверьте, что на Маке запущена Ollama, и попробуйте позже.",
            reply_markup=_home_kb(),
        )
        return
    plain, keyed = ai_reader.parse_tags(tags_text)
    cands = priced[:TAG_SCAN_LIMIT]

    async def progress(done: int, total: int) -> None:
        try:
            await status.edit_text(f"🤖 Ищу по тегам «{tags_text}»: прочитано {done} из {total}…")
        except Exception:
            pass

    await status.edit_text(f"🤖 Ищу по тегам «{tags_text}» среди {len(cands)} самых дешёвых…")
    await ai_reader.read_tags(cands, plain, list(keyed), progress)

    matched, unclear = [], []
    for r in cands:
        verdict, missing = ai_reader.tag_match(r, plain, keyed)
        if verdict == "yes":
            matched.append(r)
        elif verdict == "unclear":
            unclear.append((r, missing))

    # по «ключ: значение», которых нет в описании, — пишем продавцу. Только в
    # поиске владельца: пишем с ЕГО аккаунта площадки
    to_ask = [(r, missing) for r, missing in unclear if missing]
    if message.chat.id != OWNER_TELEGRAM_ID:
        to_ask = []
    queued, limit_note = [], None
    if to_ask:
        queued, limit_note = seller_questions.enqueue(
            to_ask, keyed, f"{label} · «{tags_text}»", message.chat.id)
        if queued:
            asyncio.create_task(seller_questions.send_queued(message.bot))
    earlier = seller_questions.asked([r.id for r, _ in unclear])

    await status.edit_text(
        f"🤖 Прочитал {len(cands)} самых дешёвых: подходят {len(matched)}, "
        f"не ясно {len(unclear)}" + (f", пишу {len(queued)} продавцам" if queued else "")
    )
    title = f"💵 {label} · «{tags_text}»"
    if matched:
        await _send_price_list(message, f"{title}: подходят {len(matched)} из {len(cands)}",
                               matched, home=not unclear)
    else:
        await message.answer(f"{title}: точно подходящих среди {len(cands)} самых дешёвых нет.",
                             reply_markup=None if unclear else _home_kb())
    if not unclear:
        return

    def mark(r: ArchivedListing, missing: list[str]) -> str:
        if r.id in queued:
            return "✉️ спрашиваю продавца"
        ask = earlier.get(r.id)
        if ask is not None and ask.status in ("queued", "sent"):
            return "✉️ ждём ответа продавца"
        if ask is not None and ask.status == "answered":
            return "💬 продавец ответил неясно"
        if missing and message.chat.id != OWNER_TELEGRAM_ID:
            return "в описании не сказано"
        if missing and r.source_site not in seller_questions.SITES:
            return "не могу написать на mashina"
        if missing and limit_note:
            return f"не спросил — {limit_note}"
        return "в описании не сказано"

    lines = [f"❔ Не ясно из описания — показываю, но в средней цене не учитываю ({len(unclear)}):"]
    for r, missing in unclear:
        lines.append(f"\n• {r.title} — {texts.fmt_price(r.price_usd)}\n  {mark(r, missing)}\n  {r.url}")
    if queued:
        lines.append("\nКак продавцы ответят — пришлю, подходит машина или нет.")
    await message.answer("\n".join(lines)[:4000], reply_markup=_home_kb(),
                         disable_web_page_preview=True)
