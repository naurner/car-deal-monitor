"""Справочники значений для автозаполнения в мастерах.

Марки/топливо/коробка/кузов берутся с mashina.kg (кэш в БД);
цвета/привод/страны в payload сайта отсутствуют — статические списки.
"""

import logging

from sqlalchemy import select

from db.models import CatalogMake, CatalogOption
from db.session import get_session
from parsers import mashina

log = logging.getLogger(__name__)

STATIC_OPTIONS = {
    "color": [
        "белый", "черный", "серый", "серебристый", "синий", "голубой",
        "красный", "бордовый", "зеленый", "желтый", "оранжевый",
        "коричневый", "бежевый", "золотистый", "фиолетовый",
    ],
    "drive": ["передний", "задний", "полный"],
    "country": ["Япония", "США", "Германия", "Корея", "Китай", "ОАЭ", "Россия"],
    # страна учета/нахождения (значения как в карточках mashina.kg)
    "reg_country": ["Кыргызстан", "Казахстан", "Россия", "США", "Корея", "Армения", "Грузия"],
    "interior": ["кожа", "ткань", "велюр", "комбинированный"],
    "steering": ["Слева", "Справа"],
    "condition": ["Идеальное", "Хорошее", "Среднее", "Аварийное / Не на ходу"],
}


async def resolve_make(user_input: str) -> tuple[str | None, list[str]]:
    """(точный слаг | None, похожие варианты для подсказки).

    Марки на mashina.kg бывают кириллическими ("УАЗ", "ГАЗ", "ВАЗ (Lada)") —
    сопоставление через translit, иначе латинский ввод "uaz" их не найдёт.
    """
    makes = await _all_makes()
    needle = user_input.strip().lower()
    needle_translit = mashina.translit(user_input.strip())
    for m in makes:
        low = m.lower()
        if low == needle or mashina.translit(m) == needle_translit:
            return m, []
    similar = [
        m for m in makes
        if needle in m.lower() or m.lower() in needle
        or needle_translit in mashina.translit(m) or mashina.translit(m) in needle_translit
    ]
    return None, similar[:8]


async def get_options(attribute: str) -> list[str]:
    """Список значений справочника для кнопок автозаполнения."""
    if attribute in STATIC_OPTIONS:
        return STATIC_OPTIONS[attribute]
    with get_session() as s:
        vals = list(
            s.execute(
                select(CatalogOption.value)
                .where(CatalogOption.attribute == attribute)
                .order_by(CatalogOption.value)
            ).scalars()
        )
    if vals:
        return vals
    await _refresh_catalogs()
    with get_session() as s:
        return list(
            s.execute(
                select(CatalogOption.value)
                .where(CatalogOption.attribute == attribute)
                .order_by(CatalogOption.value)
            ).scalars()
        )


def suggest(options: list[str], typed: str, limit: int = 8) -> list[str]:
    """Фильтр опций по введённому тексту (автозаполнение).

    Сопоставление через транслитерацию: ввод "e-klass" находит "E-Класс".
    """
    needle = mashina.translit(typed.strip())
    if not needle:
        return options[:limit]
    starts = [o for o in options if mashina.translit(o).startswith(needle)]
    contains = [
        o for o in options if needle in mashina.translit(o) and o not in starts
    ]
    return (starts + contains)[:limit]


async def _all_makes() -> list[str]:
    with get_session() as s:
        makes = list(s.execute(select(CatalogMake.value)).scalars())
    if makes:
        return makes
    await _refresh_catalogs()
    with get_session() as s:
        return list(s.execute(select(CatalogMake.value)).scalars())


async def _refresh_catalogs() -> None:
    catalogs = await mashina.fetch_catalogs()
    with get_session() as s:
        for v in catalogs.pop("make", set()):
            s.merge(CatalogMake(value=v))
        for attr, values in catalogs.items():
            for v in values:
                s.merge(CatalogOption(attribute=attr, value=v))
        s.commit()
