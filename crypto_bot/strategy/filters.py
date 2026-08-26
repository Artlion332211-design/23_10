"""Decision-time filters applied after the technical score, but before a
BUY/DCA is allowed: AntiFOMO (don't chase an already-vertical move), a final
liquidity/spread freshness check, and a blacklist re-check.

Universe-scan-time filtering (quote volume, listing age, leveraged tokens,
stablecoin pairs) lives in `market/universe_scanner.py` - those properties
don't change bar-to-bar, so there's no need to repeat them here.
"""

from __future__ import annotations

from dataclasses import dataclass

from config.settings import AntiFomoConfig, Settings, UniverseConfig
from market.market_data import IndicatorSnapshot
from market.orderbook import OrderBookSnapshot


@dataclass(frozen=True)
class FilterResult:
    passed: bool
    reason: str | None = None


class AntiFOMOFilter:
    """Blocks chasing a vertical move: the bot should buy a normal dip
    setup, not jump on a pump already in progress. Requires at least two
    independent pump indicators before blocking - any single metric alone
    can have an innocent explanation."""

    def __init__(self, config: AntiFomoConfig) -> None:
        self._config = config

    def check(
        self,
        snapshot_1h: IndicatorSnapshot,
        *,
        change_1h_percent: float,
        change_4h_percent: float,
    ) -> FilterResult:
        cfg = self._config
        reasons: list[str] = []

        if change_1h_percent >= cfg.max_price_change_1h_percent:
            reasons.append(f"+{change_1h_percent:.1f}% in 1h (max {cfg.max_price_change_1h_percent:.1f}%)")
        if change_4h_percent >= cfg.max_price_change_4h_percent:
            reasons.append(f"+{change_4h_percent:.1f}% in 4h (max {cfg.max_price_change_4h_percent:.1f}%)")
        if snapshot_1h.distance_from_ema_fast_atr >= cfg.max_distance_from_ema20_atr:
            reasons.append(
                f"{snapshot_1h.distance_from_ema_fast_atr:.1f} ATR above EMA20 "
                f"(max {cfg.max_distance_from_ema20_atr:.1f})"
            )
        if snapshot_1h.rsi >= cfg.rsi_overbought:
            reasons.append(f"1h RSI {snapshot_1h.rsi:.0f} overbought (max {cfg.rsi_overbought:.0f})")
        if snapshot_1h.volume_ma > 0 and snapshot_1h.close > snapshot_1h.open:
            volume_ratio = snapshot_1h.volume / snapshot_1h.volume_ma
            if volume_ratio >= cfg.volume_spike_multiplier:
                reasons.append(f"volume spike {volume_ratio:.1f}x looks pump-like")

        if len(reasons) >= 2:
            return FilterResult(passed=False, reason="AntiFOMO: " + "; ".join(reasons))
        return FilterResult(passed=True)


def check_blacklist(symbol: str, universe: UniverseConfig) -> FilterResult:
    if symbol in universe.blacklist_symbols:
        return FilterResult(passed=False, reason=f"{symbol} is blacklisted")
    return FilterResult(passed=True)


def check_liquidity_fresh(order_book: OrderBookSnapshot, settings: Settings) -> FilterResult:
    """Re-check spread right before ordering: scan-time ticker data can be
    minutes stale by the time a decision is actually acted on."""
    if order_book.is_empty:
        return FilterResult(passed=False, reason="order book empty/unavailable")
    if order_book.spread_percent > settings.max_spread_percent:
        return FilterResult(
            passed=False, reason=f"spread {order_book.spread_percent:.2f}% > max {settings.max_spread_percent}%"
        )
    return FilterResult(passed=True)
