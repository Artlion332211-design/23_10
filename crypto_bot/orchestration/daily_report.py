"""Builds and persists the once-a-day `DailyStat` row that the `/today`
command and the scheduled daily Telegram report both read from -
`DailyStat` is the single source of truth for a day's numbers, so a live
push and a later `/today` lookup can never disagree.

Kept as one pure-ish function (all reads/writes happen through the passed
`Session`, no I/O of its own) so it is testable against a plain `db_engine`
fixture without any live exchange or Telegram dependency.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from database.models import DailyStat
from database.repository import (
    DailyStatRepository,
    FillRepository,
    OrderRepository,
    PositionRepository,
)


def build_daily_stat(
    session: Session,
    *,
    date_str: str,
    day_start: datetime,
    day_end: datetime,
    current_balance: Decimal,
    unrealized_pnl: Decimal,
    open_positions_count: int,
    capital_exposure_pct: float,
    btc_regime: str,
) -> DailyStat:
    closed_today = PositionRepository(session).closed_between(day_start, day_end)
    realized_pnl = sum((p.realized_pnl_usdt or Decimal("0") for p in closed_today), Decimal("0"))
    wins = sum(1 for p in closed_today if (p.realized_pnl_usdt or Decimal("0")) > 0)
    closed_trades_count = len(closed_today)
    win_rate = (wins / closed_trades_count * 100) if closed_trades_count else 0.0
    trades_count = OrderRepository(session).count_filled_between(day_start, day_end)
    fees_paid = FillRepository(session).total_commission_usdt_between(day_start, day_end)

    best_trade_symbol: str | None = None
    best_trade_pct: float | None = None
    if closed_today:
        scored = [p for p in closed_today if p.realized_pnl_pct is not None]
        if scored:
            best = max(scored, key=lambda p: p.realized_pnl_pct)  # type: ignore[arg-type,return-value]
            best_trade_symbol = best.symbol
            best_trade_pct = float(best.realized_pnl_pct)  # type: ignore[arg-type]

    previous = DailyStatRepository(session).get(_previous_date_str(date_str))
    starting_balance = previous.ending_balance if previous is not None else (current_balance - realized_pnl)

    return DailyStatRepository(session).upsert(
        date_str,
        starting_balance=starting_balance,
        ending_balance=current_balance,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        trades_count=trades_count,
        closed_trades_count=closed_trades_count,
        wins=wins,
        losses=closed_trades_count - wins,
        win_rate=win_rate,
        fees_paid=fees_paid,
        open_positions_count=open_positions_count,
        capital_exposure_pct=capital_exposure_pct,
        best_trade_symbol=best_trade_symbol,
        best_trade_pct=best_trade_pct,
        btc_regime=btc_regime,
    )


def _previous_date_str(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return (d - timedelta(days=1)).isoformat()
