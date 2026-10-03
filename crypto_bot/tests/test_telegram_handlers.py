from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from database.repository import PositionRepository
from database.session import session_scope
from market.market_regime import RegimeAssessment, RegimeLevel
from risk.risk_manager import RiskManager
from strategy.strategy_engine import ManualSellResult
from telegram_bot.handlers import (
    CTX_KEY,
    BotContext,
    cmd_config,
    cmd_emergency_stop,
    cmd_market,
    cmd_pause,
    cmd_positions,
    cmd_resume,
    cmd_status,
    cmd_stop_dca,
)
from telegram_bot.notifications import StatusSnapshot
from utils.time import utcnow


def _make_update(user_id: int | None):
    update = MagicMock()
    update.effective_user.id = user_id
    update.message.reply_text = AsyncMock()
    update.effective_message = update.message  # what python-telegram-bot returns for a normal message
    return update


def _make_context(ctx: BotContext):
    context = MagicMock()
    context.bot_data = {CTX_KEY: ctx}
    return context


def _default_status_snapshot() -> StatusSnapshot:
    return StatusSnapshot(
        mode="PAPER", dry_run=False, uptime_seconds=0, btc_regime=None,
        buy_paused=False, dca_paused=False, emergency_stop=False, consecutive_bad_trades=0,
        open_positions_count=0, max_open_positions=3, total_unrealized_pnl_usdt=None,
        max_consecutive_bad_trades=3, watched_symbols=0, market_allows_buys=None, starting=False, problems=(),
    )


def _make_ctx(db_engine, settings, rules, *, allowed_user_id: int = 42) -> BotContext:
    risk_manager = RiskManager(settings)

    async def _trigger_emergency_stop() -> list[str] | None:
        risk_manager.trigger_emergency_stop()
        return None

    return BotContext(
        settings=settings, rules=rules, risk_manager=risk_manager,
        news_engine=MagicMock(), allowed_user_id=allowed_user_id, started_at=utcnow(),
        get_balance_text=AsyncMock(return_value="БАЛАНС\nUSDT: 1000.00"),
        get_current_regime=lambda: None,
        get_latest_signals=lambda: [],
        get_health_snapshot=lambda: {"last_scan": "n/a"},
        get_mark_prices=lambda: {},
        get_status_snapshot=_default_status_snapshot,
        trigger_emergency_stop=_trigger_emergency_stop,
    )


def test_unauthorized_user_is_silently_ignored(db_engine, settings, rules):
    ctx = _make_ctx(db_engine, settings, rules, allowed_user_id=42)
    update = _make_update(user_id=999)
    context = _make_context(ctx)

    asyncio.run(cmd_status(update, context))
    update.message.reply_text.assert_not_called()


def test_authorized_user_gets_a_reply(db_engine, settings, rules):
    ctx = _make_ctx(db_engine, settings, rules, allowed_user_id=42)
    ctx.get_status_snapshot = lambda: StatusSnapshot(
        mode=ctx.settings.mode.value, dry_run=False, uptime_seconds=0, btc_regime=None,
        buy_paused=False, dca_paused=False, emergency_stop=False, consecutive_bad_trades=0,
        open_positions_count=0, max_open_positions=3, total_unrealized_pnl_usdt=None,
        max_consecutive_bad_trades=3, watched_symbols=0, market_allows_buys=None, starting=False, problems=(),
    )
    update = _make_update(user_id=42)
    context = _make_context(ctx)

    asyncio.run(cmd_status(update, context))
    update.message.reply_text.assert_called_once()
    text = update.message.reply_text.call_args[0][0]
    assert "СТАТУС" in text
    assert ctx.settings.mode.value in text


def test_pause_resume_stop_dca_call_risk_manager(db_engine, settings, rules):
    ctx = _make_ctx(db_engine, settings, rules)
    context = _make_context(ctx)
    update = _make_update(user_id=42)

    asyncio.run(cmd_pause(update, context))
    assert ctx.risk_manager.status().buy_paused is True

    update2 = _make_update(user_id=42)
    asyncio.run(cmd_resume(update2, context))
    assert ctx.risk_manager.status().buy_paused is False

    update3 = _make_update(user_id=42)
    asyncio.run(cmd_stop_dca(update3, context))
    assert ctx.risk_manager.status().dca_paused is True


def test_emergency_stop_sets_flags_and_replies(db_engine, settings, rules):
    ctx = _make_ctx(db_engine, settings, rules)
    context = _make_context(ctx)
    update = _make_update(user_id=42)

    asyncio.run(cmd_emergency_stop(update, context))
    flags = ctx.risk_manager.status()
    assert flags.emergency_stop is True
    assert flags.buy_paused is True
    text = update.message.reply_text.call_args[0][0]
    assert "АВАРІЙНУ ЗУПИНКУ" in text


def test_resume_also_clears_emergency_stop(db_engine, settings, rules):
    """/emergency_stop's own reply tells the operator to use /resume to
    recover - so /resume must clear emergency_stop itself, not just
    buy_paused, or the kill switch can never be undone from Telegram.
    /emergency_stop also pauses DCA (dca_paused=True) alongside buy_paused,
    so /resume must clear that too - there is no separate Telegram command
    the confirmation message points the operator to for DCA specifically."""
    ctx = _make_ctx(db_engine, settings, rules)
    context = _make_context(ctx)

    asyncio.run(cmd_emergency_stop(_make_update(user_id=42), context))
    assert ctx.risk_manager.status().emergency_stop is True
    assert ctx.risk_manager.status().dca_paused is True

    asyncio.run(cmd_resume(_make_update(user_id=42), context))
    flags = ctx.risk_manager.status()
    assert flags.emergency_stop is False
    assert flags.buy_paused is False
    assert flags.dca_paused is False


def test_config_never_leaks_secrets(db_engine, settings, rules):
    from config.settings import Settings as SettingsCls

    secretive = SettingsCls(
        binance_api_key="super-secret-key", binance_api_secret="super-secret-secret",
        telegram_bot_token="super-secret-token", cryptopanic_api_token="super-secret-news-token",
    )
    ctx = _make_ctx(db_engine, secretive, rules)
    context = _make_context(ctx)
    update = _make_update(user_id=42)

    asyncio.run(cmd_config(update, context))
    text = update.message.reply_text.call_args[0][0]
    for secret in ("super-secret-key", "super-secret-secret", "super-secret-token", "super-secret-news-token"):
        assert secret not in text


def test_positions_reports_open_positions(db_engine, settings, rules):
    with session_scope() as session:
        PositionRepository(session).create(
            symbol="SOLUSDT", opened_at=utcnow(), avg_entry_price=Decimal("142.53"),
            total_quantity=Decimal("0.7"), total_cost_usdt=Decimal("99.77"), target_price=Decimal("156.78"),
        )
    ctx = _make_ctx(db_engine, settings, rules)
    context = _make_context(ctx)
    update = _make_update(user_id=42)

    asyncio.run(cmd_positions(update, context))
    text = update.message.reply_text.call_args[0][0]
    assert "SOLUSDT" in text
    assert "142.53" in text
    assert "поточна ціна недоступна" in text  # get_mark_prices() is empty by default


def test_positions_shows_current_pnl_state_when_price_is_known(db_engine, settings, rules):
    with session_scope() as session:
        PositionRepository(session).create(
            symbol="SOLUSDT", opened_at=utcnow(), avg_entry_price=Decimal("100"),
            total_quantity=Decimal("1"), total_cost_usdt=Decimal("100"), target_price=Decimal("110"),
        )
    ctx = _make_ctx(db_engine, settings, rules)
    ctx.get_mark_prices = lambda: {"SOLUSDT": Decimal("94")}
    context = _make_context(ctx)
    update = _make_update(user_id=42)

    asyncio.run(cmd_positions(update, context))
    text = update.message.reply_text.call_args[0][0]
    assert "PnL=-6.00%" in text
    assert "у мінусі" in text


def test_market_before_and_after_regime_computed(db_engine, settings, rules):
    ctx = _make_ctx(db_engine, settings, rules)
    context = _make_context(ctx)

    update1 = _make_update(user_id=42)
    asyncio.run(cmd_market(update1, context))
    assert "ще не розраховано" in update1.message.reply_text.call_args[0][0]

    ctx.get_current_regime = lambda: RegimeAssessment(level=RegimeLevel.BULL, score=42.0, reasons=["strong uptrend"], crash=False)
    update2 = _make_update(user_id=42)
    asyncio.run(cmd_market(update2, context))
    text = update2.message.reply_text.call_args[0][0]
    assert "ЗРОСТАННЯ" in text
    assert "strong uptrend" in text


def test_market_command_shows_both_the_intraday_regime_and_the_long_term_phase(db_engine, settings, rules):
    from datetime import date

    from market.macro_regime import MacroAssessment, MacroPhase

    ctx = _make_ctx(db_engine, settings, rules)
    ctx.get_current_regime = lambda: RegimeAssessment(level=RegimeLevel.NEUTRAL, score=3.0, reasons=["4h: price above EMA200"], crash=False)
    ctx.get_macro_assessment = lambda: MacroAssessment(
        phase=MacroPhase.BULL, as_of=date(2026, 10, 1), phase_since=date(2026, 8, 23), phase_days_at_least=40,
        btc_close=84_880.0,
        sma200=71_360.0, mayer=1.19, sma200_rising=True, sma50=77_300.0, early_warning=False, weekly_close=84_472.0,
        sma20w=70_367.0,
        ema21w=73_701.0, sma50w=78_211.0,
    )
    update = _make_update(user_id=42)

    asyncio.run(cmd_market(update, _make_context(ctx)))

    text = update.message.reply_text.call_args[0][0]
    assert "Зараз (15 хв - 4 год), BTC: НЕЙТРАЛЬНИЙ" in text
    assert "Фаза ринку (довгостроково): 🟢 ЗРОСТАННЯ, з 2026-08-23" in text
    assert "BTC 84 880 на 19% вище 200-денної середньої (71 360)" in text
    assert "Початок ведмежого ринку: 3 денні закриття BTC нижче 71 360" in text


def _ctx_with_position(db_engine, settings, rules):
    from telegram_bot.handlers import cmd_sell  # noqa: F401 - imported for the tests below

    with session_scope() as session:
        PositionRepository(session).create(
            symbol="AAVEUSDT", opened_at=utcnow(), avg_entry_price=Decimal("164.84"),
            total_quantity=Decimal("0.120879"), total_cost_usdt=Decimal("19.93"), target_price=Decimal("181.60"),
        )
    ctx = _make_ctx(db_engine, settings, rules)
    ctx.get_mark_prices = lambda: {"AAVEUSDT": Decimal("170")}
    ctx.manual_sell = AsyncMock(return_value=ManualSellResult("sold"))
    return ctx


def _sell(ctx, *args, bot_data=None):
    """One /sell message. Pass the same bot_data to chain a prompt and its confirmation."""
    from telegram_bot.handlers import cmd_sell

    update = _make_update(user_id=42)
    context = _make_context(ctx)
    if bot_data is not None:
        context.bot_data = bot_data
    context.args = list(args)
    asyncio.run(cmd_sell(update, context))
    return [c.args[0] for c in update.message.reply_text.call_args_list]


def _prompted(ctx, coin="AAVE"):
    bot_data = {CTX_KEY: ctx}
    _sell(ctx, coin, bot_data=bot_data)
    return bot_data


def test_sell_without_confirmation_only_shows_the_position_and_sells_nothing(db_engine, settings, rules):
    ctx = _ctx_with_position(db_engine, settings, rules)

    listing = _sell(ctx)
    prompt = _sell(ctx, "aave")

    assert "AAVE" in listing[0] and "/sell AAVE" in listing[0]
    assert "ПРОДАТИ ВСЮ ПОЗИЦІЮ AAVEUSDT ПО РИНКУ?" in prompt[0]
    assert "протягом 2 хв напиши: /sell AAVE так" in prompt[0]
    assert "+0.62 USDT" in prompt[0]  # (170 - 164.84) * 0.120879
    ctx.manual_sell.assert_not_called()


def test_sell_with_confirmation_right_after_the_prompt_market_sells_the_position(db_engine, settings, rules):
    ctx = _ctx_with_position(db_engine, settings, rules)
    bot_data = _prompted(ctx)

    replies = _sell(ctx, "AAVE", "так", bot_data=bot_data)

    ctx.manual_sell.assert_awaited_once_with("AAVEUSDT")
    assert replies[-1].startswith("✅ AAVEUSDT продано повністю")


def test_sell_confirmation_without_a_fresh_prompt_sells_nothing(db_engine, settings, rules):
    """'/sell AAVE так' typed from memory (or an old one resent) used to sell
    at once, skipping the step that shows the position and its PnL."""
    from datetime import timedelta

    from telegram_bot.handlers import SELL_CONFIRM_WINDOW

    ctx = _ctx_with_position(db_engine, settings, rules)

    cold = _sell(ctx, "AAVE", "так")
    assert "Спершу напиши /sell AAVE" in cold[-1]

    bot_data = _prompted(ctx)
    bot_data["sell_pending"]["AAVEUSDT"] = utcnow() - timedelta(seconds=1)  # the window has passed
    assert "Спершу напиши /sell AAVE" in _sell(ctx, "AAVE", "так", bot_data=bot_data)[-1]

    bot_data = _prompted(ctx)
    _sell(ctx, "AAVE", "так", bot_data=bot_data)
    assert "Спершу напиши" in _sell(ctx, "AAVE", "так", bot_data=bot_data)[-1]  # one prompt, one sale
    ctx.manual_sell.assert_awaited_once_with("AAVEUSDT")
    assert timedelta(minutes=1) <= SELL_CONFIRM_WINDOW <= timedelta(minutes=5)


def test_sell_of_a_coin_without_an_open_position_does_nothing(db_engine, settings, rules):
    ctx = _ctx_with_position(db_engine, settings, rules)

    replies = _sell(ctx, "ETH", "так")

    ctx.manual_sell.assert_not_called()
    assert "Позиції ETHUSDT немає" in replies[0]


def test_sell_reports_a_failed_sale_plainly(db_engine, settings, rules):
    ctx = _ctx_with_position(db_engine, settings, rules)
    ctx.manual_sell = AsyncMock(return_value=ManualSellResult("failed", "біржа відхилила продаж: Account has insufficient balance"))

    replies = _sell(ctx, "AAVE", "так", bot_data=_prompted(ctx))

    assert replies[-1].startswith("❌ Продаж AAVEUSDT не виконано.")
    assert "Причина: біржа відхилила продаж: Account has insufficient balance" in replies[-1]  # the reason is in the reply itself


def test_sell_that_raises_says_the_result_is_unknown(db_engine, settings, rules):
    """An exception used to end the handler silently after "Продаю..." -
    the owner never learned whether the order went through."""
    ctx = _ctx_with_position(db_engine, settings, rules)
    ctx.manual_sell = AsyncMock(side_effect=RuntimeError("db locked"))

    replies = _sell(ctx, "AAVE", "так", bot_data=_prompted(ctx))

    assert "результат невідомий" in replies[-1]
    assert "/positions" in replies[-1]


def test_handler_error_is_reported_to_the_owner_only(db_engine, settings, rules):
    from telegram import Update

    from telegram_bot.handlers import on_handler_error

    ctx = _make_ctx(db_engine, settings, rules)
    for user_id, expected_replies in ((42, 1), (7, 0)):
        update = MagicMock(spec=Update)
        update.effective_user = MagicMock(id=user_id)
        update.effective_message = MagicMock(reply_text=AsyncMock())
        context = _make_context(ctx)
        context.error = RuntimeError("boom")
        asyncio.run(on_handler_error(update, context))
        assert update.effective_message.reply_text.await_count == expected_replies


def test_partial_sale_reply_tells_the_owner_what_is_left_and_what_to_do():
    from telegram_bot.handlers import _sell_reply

    text = _sell_reply("AAVEUSDT", ManualSellResult("partial", "біржа продала лише частину", remaining=Decimal("0.06")))
    assert text.startswith("⚠️ AAVEUSDT продано ЧАСТКОВО - залишок 0.06")
    assert "/sell AAVE так" in text


def test_partial_and_pending_sale_replies_say_what_to_do_next(db_engine, settings, rules):
    from telegram_bot.handlers import _sell_reply

    partial = _sell_reply("AAVEUSDT", ManualSellResult("partial", "мало покупців", remaining=Decimal("0.06")))
    assert "/sell AAVE, потім /sell AAVE так" in partial  # the old confirmation is used up

    pending = _sell_reply("AAVEUSDT", ManualSellResult("pending", "біржа ще не підтвердила"))
    assert pending.startswith("⏳")
    assert "не виконано" not in pending
