from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from database.repository import DailyStatRepository, PositionRepository
from database.session import session_scope
from orchestration.daily_report import build_daily_stat
from utils.time import Timeframe, floor_to_timeframe, utcnow


def test_build_daily_stat_aggregates_todays_closed_positions(db_engine, settings):
    now = utcnow()
    day_start = floor_to_timeframe(now, Timeframe.D1)
    day_end = day_start + timedelta(days=1)

    with session_scope() as session:
        repo = PositionRepository(session)
        win = repo.create(
            symbol="SOLUSDT", opened_at=now - timedelta(hours=2), avg_entry_price=Decimal("100"),
            total_quantity=Decimal("1"), total_cost_usdt=Decimal("100"), target_price=Decimal("110"),
        )
        repo.close(
            win, closed_at=now - timedelta(hours=1), realized_pnl_usdt=Decimal("10"),
            realized_pnl_pct=Decimal("10"), close_reason="TAKE_PROFIT",
        )
        loss = repo.create(
            symbol="ETHUSDT", opened_at=now - timedelta(hours=3), avg_entry_price=Decimal("200"),
            total_quantity=Decimal("1"), total_cost_usdt=Decimal("200"), target_price=Decimal("220"),
        )
        repo.close(
            loss, closed_at=now - timedelta(minutes=30), realized_pnl_usdt=Decimal("-5"),
            realized_pnl_pct=Decimal("-2.5"), close_reason="STOP",
        )

    with session_scope() as session:
        stat = build_daily_stat(
            session, date_str=now.date().isoformat(), day_start=day_start, day_end=day_end,
            current_balance=Decimal("10005"), unrealized_pnl=Decimal("0"),
            open_positions_count=0, capital_exposure_pct=0.0, btc_regime="NEUTRAL",
        )

    assert stat.realized_pnl == Decimal("5")
    assert stat.closed_trades_count == 2
    assert stat.wins == 1
    assert stat.losses == 1
    assert stat.win_rate == 50.0
    assert stat.best_trade_symbol == "SOLUSDT"
    assert stat.best_trade_pct == 10.0
    assert stat.trades_count == 0  # no Order rows were seeded in this test
    assert stat.fees_paid == Decimal("0")
    # no prior day's stat exists, so starting balance backs out today's realized pnl
    assert stat.starting_balance == Decimal("10005") - Decimal("5")


def test_build_daily_stat_carries_forward_previous_ending_balance(db_engine, settings):
    now = utcnow()
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    today = now.date().isoformat()
    day_start = floor_to_timeframe(now, Timeframe.D1)
    day_end = day_start + timedelta(days=1)

    with session_scope() as session:
        DailyStatRepository(session).upsert(
            yesterday, starting_balance=Decimal("9000"), ending_balance=Decimal("9500"),
            realized_pnl=Decimal("500"), unrealized_pnl=Decimal("0"), trades_count=1, closed_trades_count=1,
            wins=1, losses=0, win_rate=100.0, fees_paid=Decimal("1"), open_positions_count=0,
            capital_exposure_pct=0.0, best_trade_symbol="BTCUSDT", best_trade_pct=5.0, btc_regime="BULL",
        )

    with session_scope() as session:
        stat = build_daily_stat(
            session, date_str=today, day_start=day_start, day_end=day_end,
            current_balance=Decimal("9500"), unrealized_pnl=Decimal("0"),
            open_positions_count=0, capital_exposure_pct=0.0, btc_regime="NEUTRAL",
        )

    assert stat.starting_balance == Decimal("9500")
    assert stat.realized_pnl == Decimal("0")
