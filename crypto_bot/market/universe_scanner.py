"""Dynamic universe scanner (2-stage, per project rule: never run full
indicator analysis over thousands of coins).

Stage 1 (this module): cheap, batched checks - liquidity, spread proxy via
24h stats, listing age, blacklist/stablecoin/leveraged-token exclusion -
narrowed with two REST calls (`exchangeInfo`, all-symbol `ticker/24hr`) plus
a bounded number of listing-age lookups. Produces a ranked shortlist.

Stage 2 (full multi-timeframe indicators + SignalEngine scoring) runs only
on that shortlist, driven by the caller (StrategyEngine), not here.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from decimal import Decimal

from config.settings import Settings, UniverseConfig
from exchange.binance_client import BinanceClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanCandidate:
    symbol: str
    quote_volume_24h: Decimal
    price_change_24h_percent: Decimal
    last_price: Decimal
    trades_24h: int
    opportunity_score: float


def _opportunity_score(quote_volume: float, change_pct: float) -> float:
    """Liquidity dominates the score (log scale keeps mega-caps from
    permanently drowning out everything else); a moderate pullback earns a
    bonus so genuine dip candidates surface instead of only ever ranking
    the biggest-volume pairs. This is a shortlist heuristic only - it never
    decides BUY, it only decides what gets the expensive stage-2 analysis.
    """
    base = math.log10(max(quote_volume, 1.0))
    if -15.0 <= change_pct <= 0.0:
        pullback_bonus = (1.0 - (change_pct / -15.0)) * 3.0
    elif change_pct > 0:
        pullback_bonus = max(0.0, 0.3 - change_pct / 100.0) * 3.0
    else:
        pullback_bonus = 0.3  # dropped >15%: still eligible, deprioritized (structure likely broken)
    return base + pullback_bonus


class UniverseScanner:
    def __init__(self, client: BinanceClient, settings: Settings, universe_config: UniverseConfig) -> None:
        self._client = client
        self._settings = settings
        self._universe = universe_config

    def _exclusion_reason(self, symbol: str, base_asset: str, quote_asset: str, status: str) -> str | None:
        if quote_asset != self._universe.quote_asset:
            return "wrong quote asset"
        if status != "TRADING":
            return "not TRADING"
        if base_asset in self._universe.stablecoin_assets:
            return "stablecoin pair"
        if any(base_asset.endswith(suffix) for suffix in self._universe.leveraged_token_suffixes):
            return "leveraged token"
        if symbol in self._universe.blacklist_symbols:
            return "blacklisted"
        return None

    async def _passes_listing_age(self, symbol: str) -> bool:
        min_days = self._settings.min_listing_age_days
        if min_days <= 0:
            return True
        df = await self._client.get_klines(symbol, "1d", limit=min_days + 1)
        return len(df) >= min_days

    async def scan(self) -> list[ScanCandidate]:
        exchange_info = await self._client.get_exchange_info()
        tickers = await self._client.get_ticker_24h()
        ticker_by_symbol = {t["symbol"]: t for t in tickers}

        stage1: list[ScanCandidate] = []
        for symbol, filters in exchange_info.items():
            if self._exclusion_reason(symbol, filters.base_asset, filters.quote_asset, filters.status):
                continue
            ticker = ticker_by_symbol.get(symbol)
            if ticker is None:
                continue
            try:
                quote_volume = Decimal(ticker["quoteVolume"])
                change_pct = Decimal(ticker["priceChangePercent"])
                last_price = Decimal(ticker["lastPrice"])
                trades = int(ticker.get("count", 0))
            except (KeyError, ValueError, ArithmeticError):
                continue
            if quote_volume < self._settings.min_quote_volume_24h_usdt:
                continue
            if trades < self._universe.min_trades_24h:
                continue
            if last_price <= 0:
                continue

            score = _opportunity_score(float(quote_volume), float(change_pct))
            stage1.append(
                ScanCandidate(
                    symbol=symbol, quote_volume_24h=quote_volume, price_change_24h_percent=change_pct,
                    last_price=last_price, trades_24h=trades, opportunity_score=score,
                )
            )

        stage1.sort(key=lambda c: c.opportunity_score, reverse=True)

        top_n = self._settings.scanner_top_n
        pool = stage1[: max(top_n * 3, top_n)]
        confirmed: list[ScanCandidate] = []
        for candidate in pool:
            try:
                if await self._passes_listing_age(candidate.symbol):
                    confirmed.append(candidate)
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not kill the scan
                logger.warning("Listing-age check failed for %s: %r", candidate.symbol, exc)
                continue
            if len(confirmed) >= top_n:
                break

        logger.info(
            "Universe scan: %s symbols passed stage-1 filters, %s confirmed after listing-age check",
            len(stage1), len(confirmed),
        )
        return confirmed
