"""NewsEngine: aggregates configured sources, deduplicates, scores
sentiment, persists to the DB, and answers "what's the news score for this
symbol right now" for `StrategyEngine` (structurally satisfying its
`NewsProvider` protocol - no import of strategy internals needed there).

News is a *risk filter*, not a BUY trigger, per project rule: `StrategyEngine`
only ever uses this to apply a small, asymmetric score adjustment and to
hard-block on critical news. It never originates a BUY signal on its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta
from difflib import SequenceMatcher

import aiohttp

from config.settings import Settings
from database.repository import NewsRepository
from database.session import session_scope
from news.sentiment import extract_symbols, score_text
from news.sources import (
    BinanceAnnouncementsSource,
    CryptoPanicSource,
    NewsSource,
    RawNewsItem,
    RssSource,
)
from strategy.strategy_engine import NewsAssessment
from utils.time import utcnow

logger = logging.getLogger(__name__)

DEFAULT_RSS_FEEDS = [
    ("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("cointelegraph", "https://cointelegraph.com/rss"),
    ("theblock", "https://www.theblock.co/rss.xml"),
]

_TITLE_SIMILARITY_THRESHOLD = 0.85
_DEDUP_WINDOW_HOURS = 12
_NEWS_LOOKBACK_HOURS = 48


def _normalize_title(title: str) -> str:
    return " ".join(title.lower().split())


def _dedup_hash(title: str, url: str) -> str:
    return hashlib.sha256(f"{_normalize_title(title)}|{url}".encode()).hexdigest()


class NewsEngine:
    def __init__(
        self,
        settings: Settings,
        *,
        known_bases: set[str] | None = None,
        sources: list[NewsSource] | None = None,
    ) -> None:
        self._settings = settings
        self._known_bases = known_bases or set()
        self._sources: list[NewsSource] = sources if sources is not None else self._default_sources()
        # In-process recent-title cache for cross-source near-duplicate
        # detection (title similarity) within the dedup window; the DB
        # `dedup_hash` unique constraint handles exact repeats across restarts.
        self._recent_titles: list[tuple[str, datetime]] = []

    def _default_sources(self) -> list[NewsSource]:
        sources: list[NewsSource] = [RssSource(name, url) for name, url in DEFAULT_RSS_FEEDS]
        sources.append(BinanceAnnouncementsSource())
        token = self._settings.cryptopanic_api_token.get_secret_value()
        if token:
            sources.append(CryptoPanicSource(token))
        return sources

    def set_known_bases(self, bases: set[str]) -> None:
        self._known_bases = bases

    def _is_near_duplicate(self, title: str, published_at: datetime) -> bool:
        normalized = _normalize_title(title)
        cutoff = published_at - timedelta(hours=_DEDUP_WINDOW_HOURS)
        for seen_title, seen_at in self._recent_titles:
            if seen_at < cutoff:
                continue
            if SequenceMatcher(None, normalized, seen_title).ratio() >= _TITLE_SIMILARITY_THRESHOLD:
                return True
        return False

    async def refresh(self) -> int:
        """Fetch every configured source concurrently, dedup, score, and
        persist new items. Returns how many new (non-duplicate) items were
        stored. One failing source never blocks the others."""
        if not self._settings.news_enabled:
            return 0

        async with aiohttp.ClientSession() as http_session:
            results = await asyncio.gather(
                *(source.fetch(http_session) for source in self._sources), return_exceptions=True
            )

        all_items: list[RawNewsItem] = []
        for source, result in zip(self._sources, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("News source %s failed: %r", source.name, result)
                continue
            all_items.extend(result)

        all_items.sort(key=lambda item: item.published_at)

        stored = 0
        with session_scope() as db_session:
            repo = NewsRepository(db_session)
            for item in all_items:
                if not item.title:
                    continue
                dedup_hash = _dedup_hash(item.title, item.url)
                if repo.exists(dedup_hash) or self._is_near_duplicate(item.title, item.published_at):
                    continue

                sentiment = score_text(item.title, item.body)
                symbols = extract_symbols(item.title, item.body, self._known_bases)
                repo.add(
                    source=item.source, title=item.title[:500], url=item.url[:1000],
                    published_at=item.published_at, fetched_at=utcnow(),
                    sentiment_score=sentiment.score, symbols=symbols, dedup_hash=dedup_hash,
                    critical=sentiment.critical,
                )
                self._recent_titles.append((_normalize_title(item.title), item.published_at))
                stored += 1

        dedup_cutoff = utcnow() - timedelta(hours=_DEDUP_WINDOW_HOURS)
        self._recent_titles = [(t, ts) for t, ts in self._recent_titles if ts >= dedup_cutoff]

        if stored:
            logger.info("News refresh: %s new item(s) stored", stored)
        return stored

    async def get_symbol_news_score(self, symbol: str) -> NewsAssessment:
        """Implements `strategy.strategy_engine.NewsProvider`. The single
        worst critical item dominates the result; otherwise scores over the
        lookback window are averaged."""
        base_asset = symbol[:-4] if symbol.endswith("USDT") else symbol
        lookback = utcnow() - timedelta(hours=_NEWS_LOOKBACK_HOURS)
        with session_scope() as db_session:
            items = NewsRepository(db_session).recent_for_symbol(base_asset, lookback)

        if not items:
            return NewsAssessment(score=0, critical=False, headlines=[])

        critical_items = [item for item in items if item.critical]
        if critical_items:
            worst = min(critical_items, key=lambda item: item.sentiment_score)
            return NewsAssessment(
                score=worst.sentiment_score, critical=True, headlines=[item.title for item in critical_items[:3]]
            )

        avg_score = round(sum(item.sentiment_score for item in items) / len(items))
        headlines = [item.title for item in sorted(items, key=lambda item: abs(item.sentiment_score), reverse=True)[:3]]
        return NewsAssessment(score=avg_score, critical=False, headlines=headlines)
