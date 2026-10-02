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
    StatusSnapshot,
    TelegramNotifier,
    format_buy_executed,
    format_buy_signal,
    format_crash_alert,
    format_daily_report,
    format_position_closed,
    format_status,
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
    assert "КУПІВЛЯ ВИКОНАНА" in text
    assert "Пара: SOLUSDT" in text
    assert "Ціна: $142.5300" in text
    assert "Розворот RSI ✓" in text
    assert "Бичачий MACD ✓" in text
    assert "Bollinger" not in text  # not confirmed in this scenario
    assert "BTC = НЕЙТРАЛЬНИЙ" in text
    assert "+5 НЕЙТРАЛЬНІ" in text
    assert "$156.7800" in text
    assert "-3%" in text and "-6%" in text and "-10%" in text


def test_format_buy_signal_translates_signal_names():
    regime = RegimeAssessment(level=RegimeLevel.BULL, score=20, reasons=[], crash=False)
    breakdown = _breakdown(["rsi_reversal", "vwap_recovery"])
    decision = TradeDecision(
        action="BUY", symbol="SOLUSDT", breakdown=breakdown, regime=regime, required_score=75, news_score=0, reasons=[],
    )
    text = format_buy_signal(decision)
    assert "СИГНАЛ НА КУПІВЛЮ" in text
    assert "Розворот RSI" in text
    assert "rsi_reversal" not in text  # internal identifier must not leak into the message
    assert "ЗРОСТАННЯ" in text


def test_format_position_closed_shows_pnl_and_reason():
    event = PositionClosedEvent(
        symbol="SOLUSDT", exit_price=Decimal("156.78"), avg_entry_price=Decimal("142.53"),
        net_pnl_usdt=Decimal("14.25"), net_pnl_percent=Decimal("10.0"), holding_time_seconds=3600 * 26,
        close_reason="TAKE_PROFIT", position_id=1,
    )
    text = format_position_closed(event)
    assert "ПОЗИЦІЯ ЗАКРИТА" in text
    assert "ТЕЙК-ПРОФІТ" in text
    assert "+14.25 USDT" in text
    assert "+10.00%" in text
    assert "26.0 год" in text  # under the 48h threshold, shown in hours not days


def test_format_crash_alert():
    text = format_crash_alert(["BTC dropped 6% in 60m on abnormal volume"])
    assert text.startswith("ТРИВОГА: ОБВАЛ РИНКУ")
    assert "ОБВАЛ" in text
    assert "dropped 6%" in text


def test_format_daily_report_includes_all_fields():
    data = DailyReportData(
        date="2026-08-26", starting_balance=Decimal("10000"), current_balance=Decimal("10150"),
        realized_pnl=Decimal("120"), unrealized_pnl=Decimal("30"), trades_count=3, closed_trades_count=2,
        win_rate=100.0, fees_paid=Decimal("2.5"), open_positions_count=1, capital_exposure_pct=12.5,
        best_trade_symbol="SOLUSDT", best_trade_pct=11.5, btc_regime="NEUTRAL",
    )
    text = format_daily_report(data)
    for expected in ("ЩОДЕННИЙ ЗВІТ", "10000.00", "10150.00", "+120.00", "+30.00", "Успішність: 100.0%", "SOLUSDT (+11.50%)", "НЕЙТРАЛЬНИЙ"):
        assert expected in text


def _status_snapshot(**overrides: object) -> StatusSnapshot:
    defaults: dict[str, object] = dict(
        mode="PAPER", dry_run=False, uptime_seconds=3725, btc_regime="BULL",
        buy_paused=False, dca_paused=False, emergency_stop=False, consecutive_bad_trades=0,
        open_positions_count=2, max_open_positions=3, total_unrealized_pnl_usdt=Decimal("15.5"),
        max_consecutive_bad_trades=3, watched_symbols=25, market_allows_buys=True, starting=False, problems=(),
    )
    defaults.update(overrides)
    return StatusSnapshot(**defaults)  # type: ignore[arg-type]


def test_format_status_shows_open_positions_and_pnl_state():
    text = format_status(_status_snapshot())
    assert "СТАТУС" in text
    assert "Відкриті позиції: 2 з 3" in text
    assert "+15.50 USDT" in text
    assert "у плюсі" in text
    assert "ЗРОСТАННЯ, ринок дозволяє купівлі" in text
    assert "Монет під наглядом: 25" in text
    assert "Стан: все працює нормально" in text
    assert "Обмеження: немає" in text


def test_format_status_is_free_of_raw_diagnostics():
    text = format_status(_status_snapshot())
    for raw in ("tracked_symbols", "websocket", "tasks", "heartbeat", "{"):
        assert raw not in text


def test_format_status_surfaces_problems_and_restrictions_in_plain_language():
    text = format_status(_status_snapshot(
        problems=("немає зв'язку з біржею, ціни не надходять",), buy_paused=True, emergency_stop=True,
        btc_regime="CRASH", market_allows_buys=False,
    ))
    assert "Стан: ПРОБЛЕМА - немає зв'язку з біржею" in text
    assert "перевір бота" in text
    assert "Обмеження: АВАРІЙНА ЗУПИНКА, купівлі на паузі" in text
    assert "ОБВАЛ, ринок забороняє нові купівлі" in text


def test_format_status_while_starting_is_not_a_problem():
    """/status answered during startup (before market data is loaded) must
    not tell the user something is broken - that false alarm was reported
    on the very first restart with the new status."""
    text = format_status(_status_snapshot(starting=True, btc_regime=None, watched_symbols=0))
    assert "Стан: запускається" in text
    assert "ПРОБЛЕМА" not in text


def test_format_status_shows_minus_state_for_negative_pnl():
    text = format_status(_status_snapshot(total_unrealized_pnl_usdt=Decimal("-8.2")))
    assert "-8.20 USDT" in text
    assert "у мінусі" in text


def test_format_status_handles_no_regime_and_no_positions_yet():
    text = format_status(_status_snapshot(btc_regime=None, open_positions_count=0, total_unrealized_pnl_usdt=None))
    assert "ще не розраховано" in text
    assert "Відкриті позиції: 0 з 3" in text


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


def test_notifier_status_ping_sends_raw_text():
    sender = _RecordingSender()
    notifier = TelegramNotifier(sender, chat_id=123)
    asyncio.run(notifier.status_ping("СТАТУС\nусе гаразд"))
    assert sender.sent == [(123, "СТАТУС\nусе гаразд")]

_AUTH_ERROR = (
    "Entry evaluation error for {sym}: BinanceAPIException(<ClientResponse(https://api.binance.com/api/v3/account"
    "?recvWindow=10000&timestamp=1790739899955&signature=abc) [401 Unauthorized]>, 401, "
    "'{{\"code\":-2015,\"msg\":\"Invalid API-key, IP, or permissions for action.\"}}')"
)


def test_same_exchange_error_across_many_symbols_is_sent_once_in_plain_language():
    """A changed IP (-2015) hit all 25 symbols at every candle close and sent
    25 raw exception dumps each time. It must be one readable alert."""
    sender = _RecordingSender()
    notifier = TelegramNotifier(sender, chat_id=123)

    async def scenario():
        for sym in ("BNBUSDT", "UUSDT", "SEIUSDT", "BTCUSDT", "ETHUSDT"):
            await notifier.on_error(_AUTH_ERROR.format(sym=sym))

    asyncio.run(scenario())

    assert len(sender.sent) == 1
    text = sender.sent[0][1]
    assert "IP-адресу" in text
    assert "Binance -2015: Invalid API-key, IP, or permissions for action." in text
    assert "ClientResponse" not in text and "signature" not in text


def test_repeats_are_counted_and_reported_after_the_window(monkeypatch):
    import telegram_bot.notifications as n

    clock = [1000.0]
    monkeypatch.setattr(n.time, "monotonic", lambda: clock[0])
    sender = _RecordingSender()
    notifier = TelegramNotifier(sender, chat_id=123)

    async def scenario():
        await notifier.on_error(_AUTH_ERROR.format(sym="BNBUSDT"))
        await notifier.on_error(_AUTH_ERROR.format(sym="UUSDT"))
        await notifier.on_error(_AUTH_ERROR.format(sym="SEIUSDT"))
        clock[0] += n.ERROR_REPEAT_WINDOW_SECONDS + 1
        await notifier.on_error(_AUTH_ERROR.format(sym="BTCUSDT"))

    asyncio.run(scenario())

    assert len(sender.sent) == 2
    assert "повторилась ще 2 раз" in sender.sent[1][1]


def test_recovery_is_announced_once_after_an_exchange_error():
    sender = _RecordingSender()
    notifier = TelegramNotifier(sender, chat_id=123)

    async def scenario():
        await notifier.mark_exchange_ok()  # nothing active -> silent
        await notifier.on_error(_AUTH_ERROR.format(sym="BNBUSDT"))
        await notifier.mark_exchange_ok()
        await notifier.mark_exchange_ok()  # already recovered -> silent
        await notifier.on_error(_AUTH_ERROR.format(sym="BNBUSDT"))  # a new outage alerts again

    asyncio.run(scenario())

    texts = [t for _, t in sender.sent]
    assert len(texts) == 3
    assert texts[1].startswith("ВІДНОВЛЕНО")
    assert "ключ API" in texts[1]


def test_distinct_order_failures_on_different_symbols_are_not_merged():
    sender = _RecordingSender()
    notifier = TelegramNotifier(sender, chat_id=123)

    async def scenario():
        await notifier.on_error("BUY order for SOLUSDT failed: insufficient balance")
        await notifier.on_error("BUY order for ETHUSDT failed: insufficient balance")

    asyncio.run(scenario())

    assert len(sender.sent) == 2

def test_api_key_alert_includes_the_current_public_ip():
    """The home ISP hands out a dynamic IP and the key is IP-whitelisted -
    the alert should say exactly which address to add on Binance."""
    sender = _RecordingSender()

    async def fake_ip():
        return "176.107.62.119"

    notifier = TelegramNotifier(sender, chat_id=123, public_ip_provider=fake_ip)
    asyncio.run(notifier.on_error(_AUTH_ERROR.format(sym="BNBUSDT")))

    assert "Поточна IP-адреса ноутбука: 176.107.62.119" in sender.sent[0][1]


def test_api_key_alert_still_goes_out_when_the_ip_lookup_fails():
    sender = _RecordingSender()

    async def broken_ip():
        raise OSError("no internet")

    notifier = TelegramNotifier(sender, chat_id=123, public_ip_provider=broken_ip)
    asyncio.run(notifier.on_error(_AUTH_ERROR.format(sym="BNBUSDT")))

    assert len(sender.sent) == 1
    assert "Поточна IP-адреса" not in sender.sent[0][1]


def test_ip_lookup_is_only_done_for_api_key_errors():
    calls = []

    async def fake_ip():
        calls.append(1)
        return "1.2.3.4"

    notifier = TelegramNotifier(_RecordingSender(), chat_id=123, public_ip_provider=fake_ip)
    asyncio.run(notifier.on_error("Daily report failed: ValueError('x')"))

    assert calls == []


def _macro_assessment(phase: str = "BEAR", **overrides: object):
    from datetime import date

    from market.macro_regime import MacroAssessment, MacroPhase

    fields: dict[str, object] = dict(
        phase=MacroPhase(phase), as_of=date(2026, 11, 7), phase_since=date(2026, 11, 5), phase_days_at_least=3,
        btc_close=66_000.0, sma200=71_500.0, mayer=66_000 / 71_500, sma200_rising=False, sma50=69_000.0, early_warning=True,
        weekly_close=67_200.0, sma20w=70_300.0, ema21w=72_100.0, sma50w=78_000.0,
    )
    fields.update(overrides)
    return MacroAssessment(**fields)  # type: ignore[arg-type]


def test_format_status_shows_the_long_term_market_phase():
    text = format_status(_status_snapshot(macro_phase="BEAR", macro_detail="BTC 66 000 на 8% нижче 200-денної середньої (71 500)"))
    assert "Фаза ринку (довгостроково): 🐻 ВЕДМЕЖИЙ РИНОК - BTC 66 000 на 8% нижче" in text
    assert "Фаза ринку" not in format_status(_status_snapshot())  # not computed yet -> no line


def test_market_phase_change_alert_explains_what_it_means_in_plain_language():
    from telegram_bot.notifications import format_macro_change, macro_status_detail

    text = format_macro_change("CAUTION", _macro_assessment("BEAR"))
    assert text.startswith("🐻🔴 ПОЧАВСЯ ВЕДМЕЖИЙ РИНОК 🔴🐻\nФаза: ⚠️ ОБЕРЕЖНО (ринок слабшає) -> 🐻 ВЕДМЕЖИЙ РИНОК")
    assert "50-денна середня: 69 000, BTC нижче неї - раннє попередження увімкнено" in text
    assert "200-денна середня падає" in text
    assert "нижче смуги 20-тижневої (70 300) і 21-тижневої EMA (72 100)" in text
    assert "Кінець ведмежого ринку: 3 денні закриття BTC вище 71 500" in text  # in a bear: the exit rule
    assert "/pause" in text  # tells the owner what he can do
    assert macro_status_detail(86_526, 71_418) == "BTC 86 526 на 21% вище 200-денної середньої (71 418)"


def test_notifier_sends_the_market_phase_change():
    sender = _RecordingSender()
    notifier = TelegramNotifier(sender, chat_id=7)

    asyncio.run(notifier.macro_phase_change("BULL", _macro_assessment("CAUTION", btc_close=74_000.0, mayer=1.03)))

    assert len(sender.sent) == 1
    assert "⚠️⚠️⚠️ РАННЄ ПОПЕРЕДЖЕННЯ: РИНОК СЛАБШАЄ ⚠️⚠️⚠️" in sender.sent[0][1]
    assert "🟢 ЗРОСТАННЯ -> ⚠️ ОБЕРЕЖНО (ринок слабшає)" in sender.sent[0][1]


def test_market_phase_headlines_use_emoji_and_the_bear_end_flags_the_btc_bag_signal():
    from telegram_bot.notifications import format_macro_change

    assert format_macro_change("BULL", _macro_assessment("CAUTION", btc_close=74_000.0)).startswith("⚠️⚠️⚠️ РАННЄ ПОПЕРЕДЖЕННЯ")
    assert format_macro_change("BEAR", _macro_assessment("DEEP_BEAR")).startswith("🧊🐻 ГЛИБОКИЙ ВЕДМЕЖИЙ РИНОК")
    end = format_macro_change("BEAR", _macro_assessment("CAUTION", btc_close=72_000.0))
    assert end.startswith("✅🟢 ВЕДМЕЖИЙ РИНОК ЗАКІНЧИВСЯ 🟢✅")
    assert "Сигнал для «мішка BTC»" in end
    assert "мішка BTC" not in format_macro_change("BULL", _macro_assessment("BEAR"))
