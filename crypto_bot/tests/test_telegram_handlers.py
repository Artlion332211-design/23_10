from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from database.repository import PositionRepository
from database.session import session_scope
from market.market_regime import RegimeAssessment, RegimeLevel
from risk.risk_manager import RiskManager
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
    return update


def _make_context(ctx: BotContext):
    context = MagicMock()
    context.bot_data = {CTX_KEY: ctx}
    return context


def _default_status_snapshot() -> StatusSnapshot:
    return StatusSnapshot(
        mode="PAPER", dry_run=False, uptime_seconds=0, btc_regime=None,
        buy_paused=False, dca_paused=False, emergency_stop=False, consecutive_bad_trades=0,
        open_positions_count=0, max_open_positions=3, total_unrealized_pnl_usdt=None, health={"last_scan": "n/a"},
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
        open_positions_count=0, max_open_positions=3, total_unrealized_pnl_usdt=None, health={},
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
    buy_paused, or the kill switch can never be undone from Telegram."""
    ctx = _make_ctx(db_engine, settings, rules)
    context = _make_context(ctx)

    asyncio.run(cmd_emergency_stop(_make_update(user_id=42), context))
    assert ctx.risk_manager.status().emergency_stop is True

    asyncio.run(cmd_resume(_make_update(user_id=42), context))
    flags = ctx.risk_manager.status()
    assert flags.emergency_stop is False
    assert flags.buy_paused is False


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
