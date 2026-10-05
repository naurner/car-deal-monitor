"""Публикация объявления на площадки.

lalafo (posting/lalafo_post.py) и mashina (posting/mashina_post.py) — через
API сайтов: машины, шины, диски, колёса в сборе. Площадки, где объявление
уже опубликовано, пропускаются — так повторное «Опубликовать» довыставляет
туда, где в прошлый раз не получилось.
"""

import logging
from datetime import datetime
from pathlib import Path

from db.models import PostedAd
from db.session import get_session
from posting import lalafo_post, mashina_post
from posting.lalafo_chat import SessionExpired
from posting.manage import ActionResult

log = logging.getLogger(__name__)

SITES = ("lalafo", "mashina")
POSTERS = {"lalafo": lalafo_post.post, "mashina": mashina_post.post}


def _photos(ad: PostedAd) -> list[str]:
    if not ad.photos_dir:
        return []
    return sorted(str(p) for p in Path(ad.photos_dir).iterdir()
                  if p.suffix.lower() in (".jpg", ".jpeg", ".png"))


def pending_sites(ad: PostedAd) -> list[str]:
    """Площадки, где объявление ещё не опубликовано."""
    return [s for s in SITES if (ad.sites or {}).get(s, {}).get("status") != "posted"]


async def post_all(ad: PostedAd) -> dict[str, ActionResult]:
    results: dict[str, ActionResult] = {}
    entries: dict[str, dict] = {}
    for site in pending_sites(ad):
        try:
            r = await POSTERS[site](ad.car_data or {}, ad.description or "", ad.price_usd, _photos(ad))
            msg = r.message
            if r.skipped:
                msg += f"\n   не нашлось на {site}: " + "; ".join(r.skipped)
            results[site] = ActionResult(r.ok, payment_required=r.payment_required, message=msg)
            entries[site] = {"url": r.url, "external_id": r.external_id}
        except SessionExpired as e:
            results[site] = ActionResult(False, message=str(e))
        except Exception as e:
            log.exception("%s post ad=%s", site, ad.id)
            results[site] = ActionResult(False, message=f"{site}: ошибка публикации — {e}")

    with get_session() as s:
        row = s.get(PostedAd, ad.id)
        sites = dict(row.sites or {})
        for site, res in results.items():
            entry = {**sites.get(site, {}), **entries.get(site, {})}
            entry["status"] = "posted" if res.ok else ("payment" if res.payment_required else "error")
            entry["error"] = None if res.ok else res.message
            if res.ok:
                entry["posted_at"] = datetime.utcnow().isoformat()
            sites[site] = entry
        row.sites = sites
        s.commit()
    return results


async def replace_photos(ad: PostedAd, photos: list[str], photos_dir: str) -> dict[str, ActionResult]:
    """Новые фото в уже опубликованные объявления (на тех площадках, где оно висит)."""
    results: dict[str, ActionResult] = {}
    for site in SITES:
        info = (ad.sites or {}).get(site) or {}
        if info.get("status") != "posted":
            continue
        try:
            if site == "lalafo":
                r = await lalafo_post.replace_photos(info, photos)
            else:
                r = await mashina_post.replace_photos(ad.car_data or {}, info, ad.description or "",
                                                      ad.price_usd, photos)
            results[site] = ActionResult(r.ok, message=r.message)
        except SessionExpired as e:
            results[site] = ActionResult(False, message=str(e))
        except Exception as e:
            log.exception("%s replace photos ad=%s", site, ad.id)
            results[site] = ActionResult(False, message=f"{site}: ошибка замены фото — {e}")
    if any(r.ok for r in results.values()):
        with get_session() as s:
            s.get(PostedAd, ad.id).photos_dir = photos_dir  # для будущих публикаций
            s.commit()
    return results
