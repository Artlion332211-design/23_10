from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pandas as pd

from database.repository import DailyStatRepository, PositionRepository
from database.session import session_scope
from orchestration.monthly_report import (
    MonthlyReportData,
    btc_change_pct,
    build_monthly_report,
    is_last_day_of_month,
)
from telegram_bot.notifications import format_monthly_report

NOW = datetime(2026, 10, 31, 21, 0, tzinfo=UTC)


def _stat(day: str, *, start: str, end: str) -> None:
    with session_scope() as session:
        DailyStatRepository(session).upsert(
            day, starting_balance=Decimal(start), ending_balance=Decimal(end), realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"), trades_count=0, closed_trades_count=0, wins=0, losses=0, win_rate=0.0,
            fees_paid=Decimal("0"), open_positions_count=0, capital_exposure_pct=0.0, btc_regime="BULL",
        )


def _closed_trade(closed_at: datetime, pnl: str) -> None:
    with session_scope() as session:
        repo = PositionRepository(session)
        p = repo.create(
            symbol="AAVEUSDT", opened_at=closed_at - timedelta(days=2), avg_entry_price=Decimal("100"),
            total_quantity=Decimal("0.2"), total_cost_usdt=Decimal("20"), target_price=Decimal("110"),
        )
        repo.close(p, closed_at=closed_at, realized_pnl_usdt=Decimal(pnl), realized_pnl_pct=Decimal("10"),
                   close_reason="TAKE_PROFIT")


def _btc(opens_closes: list[tuple[str, float, float]]) -> pd.DataFrame:
    return pd.DataFrame({
        "open_time": pd.to_datetime([d for d, _o, _c in opens_closes], utc=True),
        "open": [o for _d, o, _c in opens_closes],
        "close": [c for _d, _o, c in opens_closes],
    })


def test_month_report_compares_the_bot_with_btc_and_the_passive_mix(db_engine):
    _stat("2026-09-29", start="4980", end="4990")
    _stat("2026-09-30", start="4990", end="5000")  # last report before the month = the starting capital
    _stat("2026-10-15", start="5000", end="5005")
    _closed_trade(datetime(2026, 10, 2, tzinfo=UTC), "2.58")
    _closed_trade(datetime(2026, 9, 29, tzinfo=UTC), "5.00")  # last month: not counted
    btc = _btc([("2026-09-30", 110_000, 112_000), ("2026-10-01", 112_000, 113_000), ("2026-10-31", 116_000, 116_480)])

    with session_scope() as session:
        data = build_monthly_report(
            session, now=NOW, equity_now=Decimal("5050"), unrealized_now=Decimal("-1.30"), btc_daily=btc,
            earn_apr=0.0265, month_to_date=False,
        )

    assert data.equity_start == Decimal("5000")
    assert data.bot_change_usdt == Decimal("50")
    assert round(data.bot_change_pct, 2) == 1.0
    assert (data.closed_trades, data.wins, data.realized_pnl) == (1, 1, Decimal("2.58"))
    assert round(data.btc_change_pct, 2) == 4.0  # from the 1 Oct open (112 000), not the 30 Sep candle
    assert data.days == 31
    assert round(data.earn_pct, 3) == round(2.65 * 31 / 365, 3)
    assert round(data.mix_pct, 3) == round(0.35 * 4.0 + 0.65 * 2.65 * 31 / 365, 3)

    text = format_monthly_report(data)
    assert text.startswith("📊 ЗВІТ ЗА МІСЯЦЬ: жовтень 2026 (01.10-31.10)")
    assert "Бот: +50.00 USDT (+1.00%) - капітал 5000.00 -> 5050.00 USDT" in text
    assert "закрито угод: 1 (у плюсі 1), прибуток з них +2.58 USDT" in text
    assert "тримати лише BTC: +4.00%" in text
    assert "лише USDT в Earn (2.65% річних): +0.23%" in text
    assert "⚠️ Бот відстає від «35% BTC + 65% Earn» на 0.55 пункту" in text


def test_first_month_starts_from_the_first_recorded_day(db_engine):
    _stat("2026-09-29", start="4990", end="4995")
    btc = _btc([("2026-09-29", 100.0, 100.0), ("2026-09-30", 100.0, 101.0)])

    with session_scope() as session:
        data = build_monthly_report(
            session, now=datetime(2026, 9, 30, 21, tzinfo=UTC), equity_now=Decimal("5000"),
            unrealized_now=Decimal("0"), btc_daily=btc, earn_apr=None, month_to_date=False,
        )

    assert data.period_start == date(2026, 9, 29)
    assert data.equity_start == Decimal("4990")
    assert data.mix_pct is None  # no Earn rate -> no mix, shown as "н/д"
    assert "35% BTC + 65% USDT в Earn: н/д" in format_monthly_report(data)


def test_report_without_any_daily_stats_says_so(db_engine):
    with session_scope() as session:
        data = build_monthly_report(
            session, now=NOW, equity_now=Decimal("5000"), unrealized_now=Decimal("0"),
            btc_daily=_btc([]), earn_apr=0.0265, month_to_date=True,
        )
    text = format_monthly_report(data)
    assert text.startswith("📊 ЗВІТ З ПОЧАТКУ МІСЯЦЯ")
    assert "ще немає щоденної статистики" in text
    assert "тримати лише BTC: н/д" in text


def test_verdicts():
    def verdict(bot_end: str) -> str:
        data = MonthlyReportData(
            month_start=date(2026, 10, 1), period_start=date(2026, 10, 1), period_end=date(2026, 10, 31),
            month_to_date=False, equity_start=Decimal("1000"), equity_end=Decimal(bot_end),
            realized_pnl=Decimal("0"), closed_trades=0, wins=0, unrealized_now=Decimal("0"),
            btc_change_pct=0.0, earn_apr=0.0,
        )
        return format_monthly_report(data).splitlines()[-2]

    assert verdict("1020").startswith("✅ Бот попереду")
    assert verdict("1000.5").startswith("➖ Бот іде нарівні")
    assert verdict("990").startswith("⚠️ Бот відстає")


def test_btc_change_and_month_end_helpers():
    assert round(btc_change_pct(_btc([("2026-10-01", 100.0, 105.0)]), date(2026, 10, 1)), 9) == 5.0
    assert btc_change_pct(_btc([("2026-09-30", 100.0, 105.0)]), date(2026, 10, 1)) is None
    assert is_last_day_of_month(date(2026, 10, 31))
    assert is_last_day_of_month(date(2028, 2, 29))
    assert not is_last_day_of_month(date(2026, 10, 30))


def test_report_command_replies_with_the_month_so_far(db_engine, settings, rules):
    from telegram_bot.handlers import cmd_report
    from tests.test_telegram_handlers import _make_context, _make_ctx, _make_update

    ctx = _make_ctx(db_engine, settings, rules)
    ctx.get_monthly_report_text = AsyncMock(return_value="📊 ЗВІТ З ПОЧАТКУ МІСЯЦЯ: ...")
    update = _make_update(user_id=42)

    asyncio.run(cmd_report(update, _make_context(ctx)))

    assert update.message.reply_text.await_args.args[0].startswith("📊 ЗВІТ З ПОЧАТКУ МІСЯЦЯ")


def test_runtime_builds_the_report_from_equity_btc_candles_and_the_earn_rate(db_engine, settings, rules):
    from tests.test_bot_runtime import _make_runtime
    from tests.test_earn import FakeEarnClient, _manager

    _stat("2026-09-30", start="990", end="1000")
    fake = FakeEarnClient(spot="150", earn="860")
    client = MagicMock()
    client.get_account_balances = fake.get_account_balances
    client.get_historical_klines = AsyncMock(return_value=_btc([("2026-10-01", 100.0, 110.0)]))
    runtime = _make_runtime(settings, rules, client=client, earn=_manager(fake))
    runtime.get_mark_prices = lambda: {}

    data = asyncio.run(runtime._monthly_report_data(month_to_date=True))

    assert data.equity_end == Decimal("1010")  # spot 150 + Earn 860
    assert data.bot_change_usdt == Decimal("10")
    assert data.earn_apr == 0.0265
    assert client.get_historical_klines.await_args.args[:2] == ("BTCUSDT", "1d")


def test_a_trade_closed_late_on_the_last_day_counts_in_the_next_month(db_engine):
    """Equity starts from the 21:00 UTC snapshot of the previous month's last
    day; trades closed after it were counted in neither month."""
    _stat("2026-09-30", start="4990", end="5000")
    _closed_trade(datetime(2026, 9, 30, 22, 30, tzinfo=UTC), "3.00")  # after the 21:00 snapshot
    _closed_trade(datetime(2026, 9, 30, 20, 0, tzinfo=UTC), "9.00")  # before it: September's

    with session_scope() as session:
        data = build_monthly_report(
            session, now=NOW, equity_now=Decimal("5010"), unrealized_now=Decimal("0"),
            btc_daily=_btc([]), earn_apr=None, month_to_date=False, report_hour_utc=21,
        )

    assert (data.closed_trades, data.realized_pnl) == (1, Decimal("3.00"))


def test_daily_report_shows_the_month_of_closed_trades_not_just_the_day(db_engine):
    """Owner 2026-10-08: the report for a day without closes said +0.00 although
    two trades had closed in profit earlier that month."""
    from orchestration.daily_report import month_closed_trades
    from telegram_bot.notifications import DailyReportData

    _stat("2026-10-07", start="5000", end="4990")
    _closed_trade(datetime(2026, 10, 2, 4, 52, tzinfo=UTC), "2.58")
    _closed_trade(datetime(2026, 10, 6, 13, 50, tzinfo=UTC), "1.86")
    _closed_trade(datetime(2026, 9, 30, 12, 0, tzinfo=UTC), "5.00")  # last month
    _closed_trade(datetime(2026, 10, 8, 1, 0, tzinfo=UTC), "7.00")  # after the reported day

    with session_scope() as session:
        stat = DailyStatRepository(session).get("2026-10-07")
        data = DailyReportData.from_model(stat, month_closed_trades(session, "2026-10-07"))

    from telegram_bot.notifications import format_daily_report

    text = format_daily_report(data)
    assert "Реалізований PnL за день: +0.00 USDT" in text
    assert "За місяць (закриті угоди): +4.44 USDT, закрито 2 (у плюсі 2)" in text
