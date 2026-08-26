"""News source adapters.

Every source is best-effort: a failure (network, auth, unexpected schema,
rate limit) is caught and logged inside the source itself, never allowed to
break the overall aggregation in `news.news_engine` - a bot that stops
scanning for opportunities because one RSS feed is down would be a worse
outcome than just missing that one source for a cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import aiohttp
import feedparser

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RawNewsItem:
    source: str
    title: str
    url: str
    published_at: datetime
    body: str = ""


class NewsSource(Protocol):
    name: str

    async def fetch(self, session: aiohttp.ClientSession) -> list[RawNewsItem]: ...


class CryptoPanicSource:
    """https://cryptopanic.com/ public API. Recent plans require an API
    token; with none configured this source simply yields nothing rather
    than failing the aggregation run."""

    name = "cryptopanic"
    _URL = "https://cryptopanic.com/api/v1/posts/"

    def __init__(self, api_token: str | None) -> None:
        self._token = api_token

    async def fetch(self, session: aiohttp.ClientSession) -> list[RawNewsItem]:
        if not self._token:
            return []
        params = {"auth_token": self._token, "public": "true", "kind": "news"}
        try:
            async with session.get(self._URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.warning("CryptoPanic returned HTTP %s", resp.status)
                    return []
                data = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001 - any source failure degrades gracefully
            logger.warning("CryptoPanic fetch failed: %r", exc)
            return []

        items: list[RawNewsItem] = []
        for entry in data.get("results", []):
            try:
                published = datetime.fromisoformat(entry["published_at"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            items.append(
                RawNewsItem(source="cryptopanic", title=entry.get("title", ""), url=entry.get("url", ""), published_at=published)
            )
        return items


def _parse_feed_time(entry: Any) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            # `value` is a `time.struct_time` (feedparser has no type stubs,
            # so it's `Any` here) - index explicitly rather than star-unpack
            # a slice, which mypy can't verify won't collide with `tzinfo=`.
            year, month, day, hour, minute, second = value[0], value[1], value[2], value[3], value[4], value[5]
            return datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    return datetime.now(UTC)


class RssSource:
    """Generic RSS/Atom feed source (CoinDesk, CoinTelegraph, The Block,
    etc). `feedparser` handles both formats and tolerates malformed feeds
    rather than raising."""

    def __init__(self, name: str, feed_url: str) -> None:
        self.name = name
        self._feed_url = feed_url

    async def fetch(self, session: aiohttp.ClientSession) -> list[RawNewsItem]:
        try:
            async with session.get(self._feed_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.warning("%s RSS returned HTTP %s", self.name, resp.status)
                    return []
                raw_bytes = await resp.read()
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s RSS fetch failed: %r", self.name, exc)
            return []

        parsed = feedparser.parse(raw_bytes)
        items: list[RawNewsItem] = []
        for entry in parsed.entries:
            items.append(
                RawNewsItem(
                    source=self.name, title=entry.get("title", ""), url=entry.get("link", ""),
                    published_at=_parse_feed_time(entry), body=entry.get("summary", ""),
                )
            )
        return items


class CoinGeckoStatusUpdatesSource:
    """CoinGecko's free API has no general news endpoint; this reads the
    per-coin `status_updates` endpoint for a small watch-list as a
    best-effort supplementary signal. It commonly returns nothing for any
    given coin, and that's an expected, non-error outcome."""

    name = "coingecko"

    def __init__(self, coin_ids: list[str]) -> None:
        self._coin_ids = coin_ids

    async def fetch(self, session: aiohttp.ClientSession) -> list[RawNewsItem]:
        items: list[RawNewsItem] = []
        for coin_id in self._coin_ids:
            url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/status_updates"
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json(content_type=None)
            except Exception as exc:  # noqa: BLE001
                logger.debug("CoinGecko status_updates fetch failed for %s: %r", coin_id, exc)
                continue
            for update in data.get("status_updates", []) or []:
                description = update.get("description", "")
                if not description:
                    continue
                try:
                    published = datetime.fromisoformat(str(update["created_at"]).replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    published = datetime.now(UTC)
                items.append(
                    RawNewsItem(
                        source="coingecko", title=description[:200],
                        url=(update.get("project") or {}).get("public_url", ""),
                        published_at=published, body=description,
                    )
                )
        return items


class BinanceAnnouncementsSource:
    """Unofficial (undocumented) CMS endpoint that powers binance.com's own
    announcements page. Best-effort: Binance can change this without
    notice - any failure here just means this source yields nothing for
    that cycle, never a crash."""

    name = "binance_announcements"
    _URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"

    async def fetch(self, session: aiohttp.ClientSession) -> list[RawNewsItem]:
        params = {"type": "1", "catalogId": "48", "pageNo": "1", "pageSize": "20"}
        try:
            async with session.get(self._URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Binance announcements fetch failed: %r", exc)
            return []

        articles = self._extract_articles(data)
        items: list[RawNewsItem] = []
        for article in articles:
            try:
                published = datetime.fromtimestamp(article["releaseDate"] / 1000, tz=UTC)
                code = article.get("code", "")
                items.append(
                    RawNewsItem(
                        source="binance_announcements", title=article.get("title", ""),
                        url=f"https://www.binance.com/en/support/announcement/{code}", published_at=published,
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue
        return items

    @staticmethod
    def _extract_articles(data: Any) -> list[dict[str, Any]]:
        """The exact shape of this undocumented endpoint has shifted before
        and may shift again; try the known variants and fall back to empty
        rather than raising."""
        payload = data.get("data") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            return []
        if isinstance(payload.get("articles"), list):
            return payload["articles"]
        catalogs = payload.get("catalogs")
        if isinstance(catalogs, list) and catalogs and isinstance(catalogs[0], dict):
            return catalogs[0].get("articles", []) or []
        return []
