"""Актуальный курс USD/KGS с сайта Нацбанка КР, кэш на 12 часов."""

import logging
import re
import time

import httpx

from config import KGS_PER_USD, USER_AGENT

log = logging.getLogger(__name__)

NBKR_URL = "https://www.nbkr.kg/XML/daily.xml"
_cache: dict = {"rate": None, "ts": 0.0}
CACHE_TTL_SEC = 12 * 3600


async def usd_rate() -> float:
    """KGS за 1 USD. При недоступности НБКР — фолбэк из config."""
    now = time.time()
    if _cache["rate"] and now - _cache["ts"] < CACHE_TTL_SEC:
        return _cache["rate"]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(NBKR_URL, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
        m = re.search(
            r'ISOCode="USD".*?<Value>([\d,.]+)</Value>', resp.text, re.S
        )
        if m:
            rate = float(m.group(1).replace(",", "."))
            _cache.update(rate=rate, ts=now)
            log.info("Курс НБКР: 1 USD = %.2f KGS", rate)
            return rate
    except (httpx.HTTPError, ValueError) as e:
        log.warning("НБКР недоступен (%s), фолбэк %.1f", e, KGS_PER_USD)
    return _cache["rate"] or KGS_PER_USD
