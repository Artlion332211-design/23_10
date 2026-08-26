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


@dataclass(frozen=True)
class TradeRecord:
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
    num_trades: int
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
    if not equity_curve:
        raise ValueError("equity_curve must not be empty")

    ending_balance = equity_curve[-1].equity_usdt
    total_return_pct = float((ending_balance / starting_balance - 1) * 100) if starting_balance > 0 else 0.0
    net_profit = ending_balance - starting_balance

    num_trades = len(trades)
    wins = [t for t in trades if t.net_pnl_usdt > 0]
    losses = [t for t in trades if t.net_pnl_usdt <= 0]
    win_rate = (len(wins) / num_trades * 100) if num_trades else 0.0
    avg_profit_pct = float(sum((t.net_pnl_percent for t in wins), Decimal(0)) / len(wins)) if wins else 0.0
    avg_loss_pct = float(sum((t.net_pnl_percent for t in losses), Decimal(0)) / len(losses)) if losses else 0.0

    gains = sum((t.net_pnl_usdt for t in wins), Decimal(0))
    abs_losses = sum((-t.net_pnl_usdt for t in losses), Decimal(0))
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

    avg_holding_hours = (
        sum((t.closed_at - t.opened_at).total_seconds() / 3600 for t in trades) / num_trades if num_trades else 0.0
    )

    trades_with_dca = [t for t in trades if t.dca_count > 0]
    dca_frequency_pct = (len(trades_with_dca) / num_trades * 100) if num_trades else 0.0
    avg_dca_count = (sum(t.dca_count for t in trades) / num_trades) if num_trades else 0.0

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
    )
