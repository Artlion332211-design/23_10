"""Correlation / concentration control.

Don't let the bot open several highly-correlated positions at once (e.g.
ETH + SOL + AVAX + LINK can all just be the same leveraged bet on one
BTC-driven move). Correlation is computed dynamically from recent returns -
no hardcoded symbol clusters - so it adapts as market relationships shift.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from config.settings import CorrelationConfig


@dataclass(frozen=True)
class CorrelationCheck:
    passed: bool
    reason: str | None
    max_correlation: float | None
    correlated_with: str | None


def compute_correlation(candidate_closes: pd.Series, other_closes: pd.Series, *, lookback_bars: int) -> float | None:
    a = candidate_closes.pct_change().tail(lookback_bars)
    b = other_closes.pct_change().tail(lookback_bars)
    aligned = pd.concat([a.reset_index(drop=True), b.reset_index(drop=True)], axis=1).dropna()
    if len(aligned) < max(10, lookback_bars // 3):
        return None
    corr = aligned.iloc[:, 0].corr(aligned.iloc[:, 1])
    return None if pd.isna(corr) else float(corr)


def check_correlation_limit(
    candidate_symbol: str,
    candidate_closes: pd.Series,
    open_position_symbols: list[str],
    closes_by_symbol: dict[str, pd.Series],
    config: CorrelationConfig,
) -> CorrelationCheck:
    """Blocks a new position if it is highly correlated with an existing
    open position AND the number of already-open positions in that
    correlated cluster has reached `max_correlated_positions`."""
    correlated_open: list[tuple[str, float]] = []
    for symbol in open_position_symbols:
        other = closes_by_symbol.get(symbol)
        if other is None:
            continue
        corr = compute_correlation(candidate_closes, other, lookback_bars=config.lookback_bars)
        if corr is not None and corr >= config.correlation_threshold:
            correlated_open.append((symbol, corr))

    if len(correlated_open) >= config.max_correlated_positions:
        worst_symbol, worst_corr = max(correlated_open, key=lambda x: x[1])
        return CorrelationCheck(
            passed=False,
            reason=(
                f"{candidate_symbol} correlates {worst_corr:.2f} with already-open {worst_symbol} "
                f"({len(correlated_open)} correlated positions open, max {config.max_correlated_positions})"
            ),
            max_correlation=worst_corr,
            correlated_with=worst_symbol,
        )
    return CorrelationCheck(passed=True, reason=None, max_correlation=None, correlated_with=None)
