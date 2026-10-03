"""The monthly scoreboard: how the bot did against simply holding BTC and
against a passive "35% BTC + 65% USDT in Earn" mix (owner request
2026-10-03). The mix is the honest yardstick: it is what the best
consistent Binance copy-traders earned, with no work at all. If the bot
trails it month after month, the strategy needs rethinking.

Pure functions over a `Session` and plain numbers, so it is testable
without an exchange; the runtime fetches the BTC candles, the current
equity and the Earn rate and calls `build_monthly_report`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pandas as pd
from sqlalchemy.orm import Session

from database.repository import DailyStatRepository, PositionRepository

BTC_SHARE = 0.35  # the passive mix: 35% BTC, the rest in USDT Flexible Earn


@dataclass(frozen=True)
class MonthlyReportData:
    month_start: date
    period_start: date  # month_start, or the first day with data in the bot's first month
    period_end: date
    month_to_date: bool
    equity_start: Decimal | None  # None = no daily stats yet to start from
    equity_end: Decimal
    realized_pnl: Decimal
    closed_trades: int
    wins: int
    unrealized_now: Decimal
    btc_change_pct: float | None
    earn_apr: float | None  # e.g. 0.0265

    @property
    def days(self) -> int:
        """Calendar days covered, both ends included."""
        return (self.period_end - self.period_start).days + 1

    @property
    def bot_change_usdt(self) -> Decimal | None:
        return None if self.equity_start is None else self.equity_end - self.equity_start

    @property
    def bot_change_pct(self) -> float | None:
        if self.equity_start is None or self.equity_start <= 0:
            return None
        return float((self.equity_end - self.equity_start) / self.equity_start * 100)

    @property
    def earn_pct(self) -> float | None:
        return None if self.earn_apr is None else self.earn_apr * self.days / 365 * 100

    @property
    def mix_pct(self) -> float | None:
        if self.btc_change_pct is None or self.earn_pct is None:
            return None
        return BTC_SHARE * self.btc_change_pct + (1 - BTC_SHARE) * self.earn_pct


def month_start_of(day: date) -> date:
    return day.replace(day=1)


def is_last_day_of_month(day: date) -> bool:
    return (day + timedelta(days=1)).month != day.month


def btc_change_pct(daily: pd.DataFrame, since: date) -> float | None:
    """BTC from the open of `since` (00:00 UTC) to the latest close (the
    still-open candle's close is the current price)."""
    if daily.empty:
        return None
    open_times = pd.to_datetime(daily["open_time"], utc=True)
    in_period = daily[open_times >= pd.Timestamp(datetime.combine(since, time(), tzinfo=UTC))]
    if in_period.empty:
        return None
    start = float(in_period["open"].iloc[0])
    end = float(daily["close"].iloc[-1])
    return (end / start - 1) * 100 if start > 0 else None


def build_monthly_report(
    session: Session,
    *,
    now: datetime,
    equity_now: Decimal,
    unrealized_now: Decimal,
    btc_daily: pd.DataFrame,
    earn_apr: float | None,
    month_to_date: bool,
    report_hour_utc: int = 21,
) -> MonthlyReportData:
    """`report_hour_utc`: when the daily stats are written (DAILY_REPORT_HOUR_UTC).
    The month's trades are counted from the same moment as its starting
    equity, so a trade closed late on the previous month's last day lands
    in exactly one month."""
    today = now.date()
    month_start = month_start_of(today)
    stats = DailyStatRepository(session).recent(limit=400)
    before = [s for s in stats if s.date < month_start.isoformat()]
    within = sorted((s for s in stats if s.date >= month_start.isoformat()), key=lambda s: s.date)
    period_start = month_start
    equity_start: Decimal | None = None
    trades_from = datetime.combine(month_start, time(), tzinfo=UTC)
    if before:
        last_before = max(before, key=lambda s: s.date)
        equity_start = last_before.ending_balance
        trades_from = datetime.combine(date.fromisoformat(last_before.date), time(report_hour_utc), tzinfo=UTC)
    elif within:
        # The bot's first month: start from its first recorded day.
        period_start = date.fromisoformat(within[0].date)
        equity_start = within[0].starting_balance
        trades_from = datetime.combine(period_start, time(), tzinfo=UTC)

    closed = PositionRepository(session).closed_between(trades_from, now)
    realized = sum((p.realized_pnl_usdt or Decimal("0") for p in closed), Decimal("0"))
    wins = sum(1 for p in closed if (p.realized_pnl_usdt or Decimal("0")) > 0)
    return MonthlyReportData(
        month_start=month_start, period_start=period_start, period_end=today, month_to_date=month_to_date,
        equity_start=equity_start, equity_end=equity_now, realized_pnl=realized, closed_trades=len(closed), wins=wins,
        unrealized_now=unrealized_now, btc_change_pct=btc_change_pct(btc_daily, period_start), earn_apr=earn_apr,
    )
