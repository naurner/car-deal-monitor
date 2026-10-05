"""Справочник поколений: самонаполнение из заголовков mashina.kg.

Поколения фильтром на сайте недоступны (закрытый API), но присутствуют
в заголовке каждой карточки. Стратегия: при парсинге make+model агрегируем
(roman, code) -> min/max год и пишем в generation_dictionary.
"""

import logging

from sqlalchemy import select

from db.models import GenerationEntry
from db.session import get_session
from parsers import mashina
from parsers.base import RawListing

log = logging.getLogger(__name__)


def update_dictionary(brand: str, model: str, listings: list[RawListing]) -> None:
    """Обновляет справочник по фактическим данным выдачи."""
    agg: dict[str, dict] = {}
    for l in listings:
        if not l.generation_code or not l.year:
            continue
        name, code = mashina.parse_generation(l.title, brand, model)
        entry = agg.setdefault(
            code, {"name": name or code, "y_min": l.year, "y_max": l.year}
        )
        entry["y_min"] = min(entry["y_min"], l.year)
        entry["y_max"] = max(entry["y_max"], l.year)

    # base/restyling — суррогатные бакеты для марок без кодов поколений;
    # если у модели есть настоящие коды (XV70 и т.п.), суррогаты — это просто
    # объявления с неполным заголовком, в справочник их не пишем
    surrogate = {mashina.GEN_BASE, mashina.GEN_RESTYLING}
    has_real_codes = bool(set(agg) - surrogate)
    if has_real_codes:
        for k in surrogate:
            agg.pop(k, None)

    if not agg:
        return
    with get_session() as s:
        if has_real_codes:
            # суррогаты могли попасть в БД раньше, пока парсер не знал модель
            for row in s.execute(
                select(GenerationEntry).where(
                    GenerationEntry.brand == brand,
                    GenerationEntry.model == model,
                    GenerationEntry.generation_code.in_(surrogate),
                )
            ).scalars():
                s.delete(row)
        for code, data in agg.items():
            row = s.execute(
                select(GenerationEntry).where(
                    GenerationEntry.brand == brand,
                    GenerationEntry.model == model,
                    GenerationEntry.generation_code == code,
                )
            ).scalar_one_or_none()
            if row is None:
                row = GenerationEntry(
                    brand=brand, model=model, generation_code=code,
                    generation_name=data["name"],
                    year_from=data["y_min"], year_to=data["y_max"],
                )
                s.add(row)
            else:
                row.year_from = min(row.year_from or data["y_min"], data["y_min"])
                row.year_to = max(row.year_to or data["y_max"], data["y_max"])
        s.commit()


def get_generations(brand: str, model: str) -> list[GenerationEntry]:
    with get_session() as s:
        return list(
            s.execute(
                select(GenerationEntry)
                .where(GenerationEntry.brand == brand, GenerationEntry.model == model)
                .order_by(GenerationEntry.year_from)
            ).scalars()
        )


async def fetch_generations(brand: str, model: str) -> list[GenerationEntry]:
    """Для мастера: если в справочнике пусто — парсим выдачу и наполняем."""
    gens = get_generations(brand, model)
    if gens:
        return gens
    listings = await mashina.search(brand, model, max_pages=3)
    update_dictionary(brand, model, listings)
    return get_generations(brand, model)


def resolve_generation(brand: str, model: str, year: int) -> GenerationEntry | None:
    """Поколение по году (для lalafo/unaa, где поколения в данных нет)."""
    for g in get_generations(brand, model):
        if g.year_from and g.year_from <= year <= (g.year_to or 2100):
            return g
    return None
