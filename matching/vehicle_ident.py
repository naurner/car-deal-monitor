"""Марка и модель объявления в единых названиях (как на mashina.kg).

Полный скан lalafo сохранял объявления без марки и модели — 87% архива
выпадало из топов цен, оценки сделок и аналитики. Здесь:
- канонические названия: марки и модели mashina (они же в справочнике
  поколений), сравнение через транслит — «ВАЗ (ЛАДА)» == «ВАЗ (Lada)»,
  «Lixiang» == «LiXiang»;
- split_title: «Toyota Camry: 2018 г., …» → ("Toyota", "Camry");
- backfill_archive: разовое восстановление уже накопленного архива.
"""

import logging
import re
import time

from sqlalchemy import select

from db.models import ArchivedListing, CatalogMake, GenerationEntry
from db.session import get_session
from parsers import mashina

log = logging.getLogger(__name__)

BACKFILL_KEY = "backfill_brand_model_v1"
_REFRESH_SEC = 3600


def norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", mashina.translit(s or ""))


def norm_model(s: str | None) -> str:
    # lalafo пишет «E-Class», «5 series», mashina — «E-Класс», «5 серии»
    return norm(s).replace("class", "klass").replace("series", "serii")


class _Names:
    def __init__(self) -> None:
        self.loaded_at = 0.0
        self.brands: dict[str, str] = {}
        self.models: dict[str, dict[str, str]] = {}

    def load(self) -> None:
        brands: dict[str, str] = {}
        models: dict[str, dict[str, str]] = {}
        with get_session() as s:
            for (b,) in s.execute(select(CatalogMake.value)):
                brands.setdefault(norm(b), b)
            known = s.execute(
                select(ArchivedListing.brand, ArchivedListing.model)
                .where(ArchivedListing.source_site == "mashina",
                       ArchivedListing.brand.isnot(None), ArchivedListing.model.isnot(None))
                .distinct()
            ).all()
            known += s.execute(select(GenerationEntry.brand, GenerationEntry.model).distinct()).all()
        for b, m in known:
            brands[norm(b)] = b  # марки из реальных объявлений mashina важнее
            models.setdefault(b, {}).setdefault(norm_model(m), m)
        self.brands, self.models, self.loaded_at = brands, models, time.time()

    def ensure(self) -> None:
        if time.time() - self.loaded_at > _REFRESH_SEC:
            self.load()


names = _Names()


def canonical_brand(name: str | None) -> str | None:
    if not name:
        return None
    names.ensure()
    return names.brands.get(norm(name), name.strip())


def canonical_model(brand: str | None, model: str | None) -> str | None:
    if not model:
        return None
    names.ensure()
    return names.models.get(brand or "", {}).get(norm_model(model), model.strip())


def split_title(title: str | None) -> tuple[str | None, str | None]:
    """Марка — самое длинное известное начало заголовка (до «:»), модель — остаток."""
    names.ensure()
    head = (title or "").split(":")[0].strip()
    words = head.split()
    for n in (3, 2, 1):
        if len(words) < n:
            continue
        brand = names.brands.get(norm(" ".join(words[:n])))
        if brand:
            rest = " ".join(words[n:]).strip(" ,.-") or None
            # «Toyota Camry» — модель; длинный свободный текст моделью не считаем
            if rest and (":" not in (title or "") and len(rest.split()) > 2):
                rest = None
            return brand, canonical_model(brand, rest)
    return None, None


def backfill_archive(batch: int = 2000) -> int:
    """Восстанавливает марку/модель/поколение у объявлений архива без марки."""
    from matching.archive import _resolve_generation_code

    names.load()
    fixed = 0
    gen_cache: dict[tuple, str | None] = {}
    last_id = 0
    while True:
        with get_session() as s:
            rows = list(s.execute(
                select(ArchivedListing)
                .where(ArchivedListing.brand.is_(None), ArchivedListing.id > last_id)
                .order_by(ArchivedListing.id).limit(batch)
            ).scalars())
            if not rows:
                break
            for r in rows:
                brand, model = split_title(r.title)
                if not brand:
                    continue
                r.brand, r.model = brand, model
                if model and r.year and not r.generation_code:
                    key = (brand, model, r.year)
                    if key not in gen_cache:
                        gen_cache[key] = _resolve_generation_code(brand, model, r.year)
                    r.generation_code = gen_cache[key]
                fixed += 1
            last_id = rows[-1].id
            s.commit()
    log.info("Марка/модель восстановлены у %d объявлений архива", fixed)
    return fixed
