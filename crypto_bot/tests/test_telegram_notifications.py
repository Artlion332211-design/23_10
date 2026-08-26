from __future__ import annotations

import asyncio
from decimal import Decimal

from market.market_regime import RegimeAssessment, RegimeLevel
from strategy.dca import DCALevel
from strategy.scoring import ScoreBreakdown, SignalResult
from strategy.strategy_engine import (
    BuyExecutedEvent,
    PositionClosedEvent,
    TradeDecision,
)
from telegram_bot.notifications import (
    DailyReportData,
    TelegramNotifier,
    format_buy_executed,
    format_crash_alert,
    format_daily_report,
    format_position_closed,
)


def _breakdown(confirmed_names: list[str]) -> ScoreBreakdown:
    all_signals = ["rsi_reversal", "macd_bullish", "ema_trend", "bollinger_recovery", "volume_confirmation", "vwap_recovery", "market_structure"]
    categories = {"rsi_reversal": "momentum", "macd_bullish": "momentum", "ema_trend": "trend", "bollinger_recovery": "volatility",
                  "volume_confirmation": "volume", "vwap_recovery": "structure", "market_structure": "structure"}
    signals = [
        SignalResult(name=n, confirmed=n in confirmed_names, category=categories[n], points=15.0 if n in confirmed_names else 0.0, max_points=15.0)
        for n in all_signals
    ]
    total = sum(s.points for s in signals)
    return ScoreBreakdown(
        symbol="SOLUSDT", technical_score=total, news_adjustment=0, regime_adjustment=0, final_score=total,
        signals=signals, confirmed_count=len(confirmed_names), confirmed_categories=sorted({categories[n] for n in confirmed_names}),
        vetoes=[], meets_confirmation_rule=True,
    )


def test_format_buy_executed_contains_required_sections():
    regime = RegimeAssessment(level=RegimeLevel.NEUTRAL, score=0, reasons=[], crash=False)
    breakdown = _breakdown(["rsi_reversal", "macd_bullish", "ema_trend", "vwap_recovery", "volume_confirmation"])
    event = BuyExecutedEvent(
        symbol="SOLUSDT", price=Decimal("142.53"), usdt_amount=Decimal("100"), quantity=Decimal("0.7"),
        breakdown=breakdown, regime=regime, news_score=5, target_price=Decimal("156.78"),
        dca_plan=[DCALevel(1, Decimal("-3"), Decimal("50")), DCALevel(2, Decimal("-6"), Decimal("75")), DCALevel(3, Decimal("-10"), Decimal("75"))],
        position_id=1,
    )
    text = format_buy_executed(event)
    assert "BUY EXECUTED" in text
    assert "Pair: SOLUSDT" in text
    assert "Price: $142.5300" in text
    assert "RSI reversal ✓" in text
    assert "MACD bullish ✓" in text
    assert "Bollinger recovery" not in text  # not confirmed in this scenario
    assert "BTC = NEUTRAL" in text
    assert "+5 NEUTRAL" in text
    assert "$156.7800" in text
    assert "-3%" in text and "-6%" in text and "-10%" in text


def test_format_position_closed_shows_pnl_and_reason():
    event = PositionClosedEvent(
        symbol="SOLUSDT", exit_price=Decimal("156.78"), avg_entry_price=Decimal("142.53"),
        net_pnl_usdt=Decimal("14.25"), net_pnl_percent=Decimal("10.0"), holding_time_seconds=3600 * 26,
        close_reason="TAKE_PROFIT", position_id=1,
    )
    text = format_position_closed(event)
    assert "POSITION CLOSED" in text
    assert "TAKE_PROFIT" in text
    assert "+14.25 USDT" in text
    assert "+10.00%" in text
    assert "26.0h" in text  # under the 48h threshold, shown in hours not days


def test_format_crash_alert():
    text = format_crash_alert(["BTC dropped 6% in 60m on abnormal volume"])
    assert text.startswith("CRASH ALERT")
    assert "CRASH" in text
    assert "dropped 6%" in text


def test_format_daily_report_includes_all_fields():
    data = DailyReportData(
        date="2026-08-26", starting_balance=Decimal("10000"), current_balance=Decimal("10150"),
        realized_pnl=Decimal("120"), unrealized_pnl=Decimal("30"), trades_count=3, closed_trades_count=2,
        win_rate=100.0, fees_paid=Decimal("2.5"), open_positions_count=1, capital_exposure_pct=12.5,
        best_trade_symbol="SOLUSDT", best_trade_pct=11.5, btc_regime="NEUTRAL",
    )
    text = format_daily_report(data)
    for expected in ("DAILY REPORT", "10000.00", "10150.00", "+120.00", "+30.00", "Win rate: 100.0%", "SOLUSDT (+11.50%)", "NEUTRAL"):
        assert expected in text


class _RecordingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


class _FailingSender:
    async def send_message(self, chat_id: int, text: str) -> None:
        raise RuntimeError("network down")


def test_notifier_on_no_trade_never_sends_anything():
    """NO_TRADE/BLOCKED decisions are already recorded to DB/logs - pushing
    every rejected candidate to Telegram would spam the chat and isn't in
    the spec's explicit notification list."""
    sender = _RecordingSender()
    notifier = TelegramNotifier(sender, chat_id=123)
    breakdown = _breakdown([])
    regime = RegimeAssessment(level=RegimeLevel.NEUTRAL, score=0, reasons=[], crash=False)
    decision = TradeDecision(action="NO_TRADE", symbol="SOLUSDT", breakdown=breakdown, regime=regime, required_score=75, news_score=0, reasons=["weak"])
    asyncio.run(notifier.on_no_trade(decision))
    assert sender.sent == []


def test_notifier_send_failure_is_swallowed_not_raised():
    notifier = TelegramNotifier(_FailingSender(), chat_id=123)
    asyncio.run(notifier.on_error("something broke"))  # must not raise
