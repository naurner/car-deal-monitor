"""Слежение за ключевыми словами в свежих объявлениях всех площадок.

Каждый цикл: тянем свежие ленты (без фильтров), кладём в архив, ищем
ключевые слова в объявлениях, появившихся за окно свежести.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from db.models import ArchivedListing, KeywordSeen, KeywordWatch
from db.session import get_session
from matching import archive, keyword_context, sellers
from parsers import lalafo, mashina

log = logging.getLogger(__name__)


@dataclass
class KeywordMatch:
    watch: KeywordWatch
    listing: ArchivedListing
    matched_keyword: str


async def check_keywords(window_min: int) -> list[KeywordMatch]:
    # свежие объявления архивируются ВСЕГДА (даже без активных слежений) —
    # это основной источник "все новые авто пишутся в базу"
    fresh_keys: set[tuple[str, str]] = set()
    for fetch in (mashina.fresh, lalafo.fresh):
        try:
            got = await fetch()
            archive.upsert_listings(got)
            fresh_keys |= {(l.source_site, l.external_id) for l in got}
        except Exception:
            log.exception("fresh feed failed: %s", fetch.__module__)

    with get_session() as s:
        watches = list(
            s.execute(
                select(KeywordWatch).where(KeywordWatch.is_active.is_(True))
            ).scalars()
        )
    if not watches:
        return []

    cutoff = datetime.utcnow() - timedelta(minutes=window_min)
    with get_session() as s:
        fresh_rows = list(
            s.execute(
                select(ArchivedListing).where(
                    ArchivedListing.first_seen_at >= cutoff
                )
            ).scalars()
        )
        # только из свежих лент: полный скан тоже впервые видит тысячи
        # СТАРЫХ объявлений — по first_seen_at они выглядели бы новыми
        fresh_rows = [
            r for r in fresh_rows
            if (r.source_site, r.external_id) in fresh_keys
            and (r.posted_at is None or r.posted_at >= cutoff - timedelta(days=1))
        ]
    # слово в контексте: «не проходной на Россию» — не про Россию.
    # Спорные (рядом отрицание) дочитывает ИИ — до записи «уже отправлено»
    for watch in watches:
        hits = [r for r in fresh_rows
                if keyword_context.rule_verdict(keyword_context.text_of(r), watch.keywords)]
        await keyword_context.judge(hits, watch.keywords)

    with get_session() as s:
        results: list[KeywordMatch] = []
        for watch in watches:
            for row in fresh_rows:
                if sellers.is_banned(row) or not keyword_context.matches(row, watch.keywords):
                    continue
                text = keyword_context.text_of(row).lower()
                hit = next((kw for kw in watch.keywords if kw.lower() in text), watch.keywords[0])
                dup = s.execute(
                    select(KeywordSeen).where(
                        KeywordSeen.watch_id == watch.id,
                        KeywordSeen.archive_id == row.id,
                    )
                ).scalar_one_or_none()
                if dup:
                    continue
                s.add(KeywordSeen(watch_id=watch.id, archive_id=row.id))
                results.append(KeywordMatch(watch, row, hit))
        s.commit()
    return results
