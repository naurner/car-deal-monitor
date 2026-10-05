"""Полное сканирование рынка: все активные объявления обеих площадок,
без фильтра по марке — для архива и топ-трекера цен (matching/top_tracker.py).

Тяжёлая операция (может занять десятки минут на mashina.kg из-за объёма
каталога), поэтому запускается редко (см. main.py — раз в несколько часов),
в отличие от лёгкого matching/keywords.py (свежая лента каждые ~15-30 мин).
"""

import logging
from datetime import datetime

from matching import archive, catalog, generations
from parsers import lalafo, mashina
from parsers.base import RawListing

log = logging.getLogger(__name__)

MASHINA_MAX_PAGES = 3000   # самоограничивается: search() останавливается,
                           # когда карточки закончились раньше этого


async def _scan_mashina(status: dict) -> list[RawListing]:
    makes = await catalog._all_makes()
    sorted_makes = mashina.sort_makes_by_slug_length(makes)
    listings = await mashina.search(
        brand=None, max_pages=MASHINA_MAX_PAGES, known_makes=sorted_makes,
        on_batch=archive.upsert_listings, status=status,
    )
    # заполняем справочник поколений по всему, что нашли (группируя по марка+модель)
    by_brand_model: dict[tuple[str, str], list[RawListing]] = {}
    for l in listings:
        if l.brand and l.model:
            by_brand_model.setdefault((l.brand, l.model), []).append(l)
    for (brand, model), group in by_brand_model.items():
        try:
            generations.update_dictionary(brand, model, group)
        except Exception:
            log.exception("update_dictionary failed for %s %s", brand, model)
    return listings


async def _scan_lalafo(status: dict) -> list[RawListing]:
    return await lalafo.scan_all(on_batch=archive.upsert_listings, status=status)


async def run() -> dict[str, int]:
    """Полный обход обеих площадок + запись в архив (по ходу скана, порциями).
    Если площадка пройдена до конца — пропавшие из неё объявления снимаем с
    продажи: так ликвидность и «сейчас в продаже» считаются по факту."""
    stats = {"mashina": 0, "lalafo": 0, "gone": 0}
    # lalafo первым: ~30 минут против ~2 часов у mashina
    for site, scan in (("lalafo", _scan_lalafo), ("mashina", _scan_mashina)):
        started = datetime.utcnow()
        status: dict = {}
        try:
            listings = await scan(status)
        except Exception:
            log.exception("full_scan: %s упал", site)
            continue
        stats[site] = len(listings)
        if status.get("complete"):
            stats["gone"] += archive.mark_missing(site, started, len(listings))
        else:
            log.warning("full_scan: %s пройден не до конца — пропавшие не отмечаю", site)

    log.info("Полное сканирование рынка: mashina=%d lalafo=%d, снято с продажи %d",
             stats["mashina"], stats["lalafo"], stats["gone"])
    return stats
