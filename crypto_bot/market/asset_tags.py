"""Binance product tags for the EXCLUDED_ASSET_TAGS no-buy list.

Binance puts a "Monitoring" tag on coins it may delist and a "Seed" tag on
new, very volatile projects; "bStocks" marks tokenized US stocks. The
trading API has no such field, so this reads the public product list behind
binance.com's markets page (`tags` per symbol). That endpoint is
undocumented, like the announcements feed in news/sources.py, so every
failure is soft: the last good list stays in use, and until the first
successful fetch nothing is excluded - the bot then trades exactly as it did
before this filter existed. A broken web page must never stop trading;
`problem()` puts a line in /status when the list is missing or old.

Refreshed by the universe scan (`UniverseScanner.scan`), at most hourly:
tags change a few times a month.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

PRODUCTS_URL = "https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-products?includeEtf=true"
_REFRESH_SECONDS = 3600
# ~1,400 products today; far fewer means a broken or cut-off answer, which
# must not silently empty the no-buy list.
_MIN_PRODUCTS = 200
_MISSING_PROBLEM_SECONDS = 3600
_STALE_PROBLEM_SECONDS = 24 * 3600
_TAG_NOTES = {
    "monitoring": "Binance may delist it",
    "seed": "new, very volatile project",
    "bstocks": "tokenized stock",
}


async def fetch_products() -> Any:
    async with aiohttp.ClientSession() as session:
        async with session.get(PRODUCTS_URL, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)


def parse_tagged(payload: Any, excluded: frozenset[str], quote_asset: str) -> dict[str, tuple[str, ...]]:
    """`quote_asset` pairs carrying an excluded tag -> those tags, spelled as
    Binance spells them. `excluded` is lower-case: matching ignores case.
    Raises ValueError on an answer that doesn't look like the full list."""
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list) or len(items) < _MIN_PRODUCTS:
        size = len(items) if isinstance(items, list) else type(items).__name__
        raise ValueError(f"unexpected product list from Binance ({size})")
    tagged: dict[str, tuple[str, ...]] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("q") != quote_asset:
            continue
        symbol, tags = item.get("s"), item.get("tags")
        if not isinstance(symbol, str) or not isinstance(tags, list):
            continue
        hits = tuple(tag for tag in tags if isinstance(tag, str) and tag.lower() in excluded)
        if hits:
            tagged[symbol] = hits
    return tagged


class AssetTags:
    def __init__(
        self,
        excluded: frozenset[str],
        *,
        quote_asset: str,
        fetch: Callable[[], Awaitable[Any]] = fetch_products,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._names = ", ".join(sorted(excluded))
        self._excluded = frozenset(tag.lower() for tag in excluded)
        self._quote_asset = quote_asset
        self._fetch = fetch
        self._clock = clock
        self._tagged: dict[str, tuple[str, ...]] = {}
        self._created_at = clock()
        self._last_attempt: float | None = None
        self._last_success: float | None = None
        self._failures = 0

    async def refresh_if_due(self) -> None:
        """Fetches the list when the last good one is an hour old; after a
        failure, on every call (each universe scan). Never raises."""
        now = self._clock()
        if self._failures == 0 and self._last_attempt is not None and now - self._last_attempt < _REFRESH_SECONDS:
            return
        self._last_attempt = now
        try:
            tagged = parse_tagged(await self._fetch(), self._excluded, self._quote_asset)
        except Exception as exc:  # noqa: BLE001 - the last good list stays in use
            self._failures += 1
            logger.warning(
                "Binance tags refresh failed (%s in a row), keeping the last list (%s pair(s)): %r",
                self._failures, len(self._tagged), exc,
            )
            return
        added = sorted(tagged.keys() - self._tagged.keys())
        removed = sorted(self._tagged.keys() - tagged.keys())
        if self._last_success is None or added or removed:
            logger.info(
                "Binance tags %s: %s %s pair(s) get no new buys or DCA (added: %s; removed: %s)",
                self._names, len(tagged), self._quote_asset, ", ".join(added) or "-", ", ".join(removed) or "-",
            )
        if not tagged:
            logger.warning("No %s pair carries the Binance tag(s) %s - renamed by Binance?", self._quote_asset, self._names)
        self._tagged = tagged
        self._failures = 0
        self._last_success = now

    def excluded_tags(self, symbol: str) -> tuple[str, ...]:
        return self._tagged.get(symbol, ())

    def exclusion_reason(self, symbol: str) -> str | None:
        tags = self.excluded_tags(symbol)
        if not tags:
            return None
        named = ", ".join(f"{tag} ({_TAG_NOTES.get(tag.lower(), 'excluded tag')})" for tag in tags)
        return f"Binance tag {named} - no new buys or DCA (EXCLUDED_ASSET_TAGS)"

    def problem(self) -> str | None:
        """Plain-language /status line when the no-buy list can't be trusted."""
        now = self._clock()
        if self._last_success is None:
            if now - self._created_at < _MISSING_PROBLEM_SECONDS:
                return None
            return f"позначки монет з Binance не завантажуються - заборона купівель ({self._names}) поки не діє"
        age = now - self._last_success
        if age < _STALE_PROBLEM_SECONDS:
            return None
        return (
            f"позначки монет з Binance не оновлювались {int(age // 3600)} год - "
            f"заборона купівель ({self._names}) діє за старим списком"
        )
