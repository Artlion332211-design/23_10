"""Backtest performance metrics.

Per project rule, Win Rate alone is not a reliable optimization target - a
high win rate can still hide a poor strategy (many tiny wins, one huge
loss). The primary metrics for judging a parameter set are Max Drawdown,
Profit Factor, Sharpe/Sortino, and Net Return; everything else here exists
to make those numbers inspectable, not to replace them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import numpy as np
import pandas as pd

# `close_reason` of the mark-to-market record the engine writes for a
# position still open when the data ends - a valuation, not a sale.
OPEN_AT_END_REASON = "OPEN_AT_END"


@dataclass(frozen=True)
class TradeRecord:
    """One sell slice of a position (a partial take-profit, the final exit,
    or a whole-position close) - or, with `close_reason=OPEN_AT_END_REASON`,
    the end-of-data mark of a position that never finished. A position can
    therefore span several records; `compute_metrics` nets them back into
    one outcome per position."""

    symbol: str
    opened_at: datetime
    closed_at: datetime
    avg_entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    cost_usdt: Decimal
    proceeds_usdt: Decimal
    net_pnl_usdt: Decimal
    net_pnl_percent: Decimal
    dca_count: int
    close_reason: str
    worst_drawdown_percent: float  # max adverse excursion while the position was open


@dataclass(frozen=True)
class EquityPoint:
    timestamp: datetime
    equity_usdt: Decimal


@dataclass(frozen=True)
class BacktestMetrics:
    start: datetime
    end: datetime
    starting_balance: Decimal
    ending_balance: Decimal
    total_return_percent: float
    net_profit_usdt: Decimal
    num_trades: int  # closed positions - partial-TP slices netted, still-open ones excluded
    win_rate: float
    avg_profit_percent: float
    avg_loss_percent: float
    profit_factor: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown_percent: float
    avg_holding_time_hours: float
    exposure_percent: float
    dca_frequency_percent: float
    avg_dca_count: float
    worst_position_drawdown_percent: float
    total_fees_usdt: Decimal
    open_at_end_count: int  # positions still open when the data ran out
    open_at_end_unrealized_pnl_usdt: Decimal  # their mark-to-market P&L (already inside ending_balance)


@dataclass(frozen=True)
class _ClosedPosition:
    net_pnl_usdt: Decimal
    net_pnl_percent: Decimal
    holding_hours: float
    dca_count: int


def _group_by_position(trades: list[TradeRecord]) -> list[list[TradeRecord]]:
    """TradeRecord carries no position id, but (symbol, opened_at) is one:
    the engine holds at most one position per symbol, and a new one always
    opens on a later bar than the one it replaces."""
    groups: dict[tuple[str, datetime], list[TradeRecord]] = {}
    for t in trades:
        groups.setdefault((t.symbol, t.opened_at), []).append(t)
    return list(groups.values())


def _net_closed_position(slices: list[TradeRecord]) -> _ClosedPosition:
    """A partial take-profit and the exit of its remainder are one trade:
    counted separately, one winning position would read as two wins (or a
    win and a loss), skewing trade count, win rate and profit factor. The
    percentage is re-derived over the combined cost basis, which for a
    single-slice position is exactly the slice's own figure."""
    net_pnl = sum((t.net_pnl_usdt for t in slices), Decimal(0))
    cost = sum((t.cost_usdt for t in slices), Decimal(0))
    closed_at = max(t.closed_at for t in slices)
    return _ClosedPosition(
        net_pnl_usdt=net_pnl,
        net_pnl_percent=(net_pnl / cost * 100) if cost > 0 else Decimal(0),
        holding_hours=(closed_at - slices[0].opened_at).total_seconds() / 3600,
        dca_count=max(t.dca_count for t in slices),
    )


def _annualized_sharpe(daily_returns: pd.Series, periods_per_year: int = 365) -> float:
    if len(daily_returns) < 2 or daily_returns.std() == 0:
        return 0.0
    return float(daily_returns.mean() / daily_returns.std() * np.sqrt(periods_per_year))


def _annualized_sortino(daily_returns: pd.Series, periods_per_year: int = 365) -> float:
    if len(daily_returns) < 2:
        return 0.0
    downside = daily_returns[daily_returns < 0]
    if len(downside) == 0:
        return 0.0 if daily_returns.mean() <= 0 else float("inf")
    downside_std = downside.std()
    if downside_std == 0:
        return 0.0
    return float(daily_returns.mean() / downside_std * np.sqrt(periods_per_year))


def _max_drawdown_percent(equity_series: pd.Series) -> float:
    if len(equity_series) == 0:
        return 0.0
    running_max = equity_series.cummax()
    drawdown = (equity_series - running_max) / running_max.replace(0, np.nan) * 100
    return float(drawdown.min()) if drawdown.notna().any() else 0.0


def _exposure_percent(trades: list[TradeRecord], equity_curve: list[EquityPoint]) -> float:
    """Time-weighted average of (capital deployed / starting balance) -
    "what fraction of the account was working, on average, over the whole
    backtest period"."""
    if not equity_curve or not trades:
        return 0.0
    total_duration = (equity_curve[-1].timestamp - equity_curve[0].timestamp).total_seconds()
    starting_balance = float(equity_curve[0].equity_usdt)
    if total_duration <= 0 or starting_balance <= 0:
        return 0.0
    capital_seconds = sum(
        float(t.cost_usdt) * max(0.0, (t.closed_at - t.opened_at).total_seconds()) for t in trades
    )
    return min(100.0, capital_seconds / (starting_balance * total_duration) * 100.0)


def compute_metrics(
    trades: list[TradeRecord],
    equity_curve: list[EquityPoint],
    starting_balance: Decimal,
    total_fees_usdt: Decimal,
) -> BacktestMetrics:
    """Trade statistics (count, win rate, avg profit/loss, profit factor,
    holding time, DCA) are per CLOSED position: a still-open position's
    mark is not a result yet, so it's reported only as `open_at_end_*`.
    Balance-, equity- and exposure-based figures take every record as
    before - the equity curve has always included open positions."""
    if not equity_curve:
        raise ValueError("equity_curve must not be empty")

    ending_balance = equity_curve[-1].equity_usdt
    total_return_pct = float((ending_balance / starting_balance - 1) * 100) if starting_balance > 0 else 0.0
    net_profit = ending_balance - starting_balance

    # A position with a partial take-profit and an OPEN_AT_END remainder is
    # still open too - its sold slice alone isn't a finished trade.
    positions = _group_by_position(trades)
    still_open = [p for p in positions if any(t.close_reason == OPEN_AT_END_REASON for t in p)]
    closed = [_net_closed_position(p) for p in positions if not any(t.close_reason == OPEN_AT_END_REASON for t in p)]
    open_at_end_pnl = sum((t.net_pnl_usdt for t in trades if t.close_reason == OPEN_AT_END_REASON), Decimal(0))

    num_trades = len(closed)
    wins = [p for p in closed if p.net_pnl_usdt > 0]
    losses = [p for p in closed if p.net_pnl_usdt <= 0]
    win_rate = (len(wins) / num_trades * 100) if num_trades else 0.0
    avg_profit_pct = float(sum((p.net_pnl_percent for p in wins), Decimal(0)) / len(wins)) if wins else 0.0
    avg_loss_pct = float(sum((p.net_pnl_percent for p in losses), Decimal(0)) / len(losses)) if losses else 0.0

    gains = sum((p.net_pnl_usdt for p in wins), Decimal(0))
    abs_losses = sum((-p.net_pnl_usdt for p in losses), Decimal(0))
    if abs_losses > 0:
        profit_factor = float(gains / abs_losses)
    else:
        profit_factor = float("inf") if gains > 0 else 0.0

    equity_series = pd.Series(
        [float(p.equity_usdt) for p in equity_curve],
        index=pd.DatetimeIndex([p.timestamp for p in equity_curve]),
    )
    daily_equity = equity_series.resample("1D").last().ffill()
    daily_returns = daily_equity.pct_change().dropna()

    sharpe = _annualized_sharpe(daily_returns)
    sortino = _annualized_sortino(daily_returns)
    max_dd = _max_drawdown_percent(equity_series)

    avg_holding_hours = sum(p.holding_hours for p in closed) / num_trades if num_trades else 0.0

    positions_with_dca = [p for p in closed if p.dca_count > 0]
    dca_frequency_pct = (len(positions_with_dca) / num_trades * 100) if num_trades else 0.0
    avg_dca_count = (sum(p.dca_count for p in closed) / num_trades) if num_trades else 0.0

    # Deliberately over every record, still-open ones included: an open
    # position deep underwater at the end is exactly the risk to surface.
    worst_position_dd = min((t.worst_drawdown_percent for t in trades), default=0.0)

    return BacktestMetrics(
        start=equity_curve[0].timestamp, end=equity_curve[-1].timestamp,
        starting_balance=starting_balance, ending_balance=ending_balance,
        total_return_percent=total_return_pct, net_profit_usdt=net_profit,
        num_trades=num_trades, win_rate=win_rate, avg_profit_percent=avg_profit_pct, avg_loss_percent=avg_loss_pct,
        profit_factor=profit_factor, sharpe_ratio=sharpe, sortino_ratio=sortino, max_drawdown_percent=max_dd,
        avg_holding_time_hours=avg_holding_hours, exposure_percent=_exposure_percent(trades, equity_curve),
        dca_frequency_percent=dca_frequency_pct, avg_dca_count=avg_dca_count,
        worst_position_drawdown_percent=worst_position_dd, total_fees_usdt=total_fees_usdt,
        open_at_end_count=len(still_open), open_at_end_unrealized_pnl_usdt=open_at_end_pnl,
    )
