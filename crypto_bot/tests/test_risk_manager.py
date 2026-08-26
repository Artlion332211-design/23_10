from __future__ import annotations

from decimal import Decimal

import pytest

from market.market_regime import RegimeAssessment, RegimeLevel
from risk.risk_manager import RiskManager


@pytest.fixture
def risk_manager(db_engine, settings):
    tuned = settings.model_copy(update={
        "max_open_positions": 2, "max_total_exposure_percent": Decimal("30"),
        "max_daily_new_capital_usdt": Decimal("250"), "max_consecutive_bad_trades": 2,
        "max_position_usdt": Decimal("300"),
    })
    return RiskManager(tuned)


NEUTRAL = RegimeAssessment(level=RegimeLevel.NEUTRAL, score=0, reasons=[], crash=False)
CRASH = RegimeAssessment(level=RegimeLevel.CRASH, score=-100, reasons=["crash"], crash=True)
STRONG_BEAR = RegimeAssessment(level=RegimeLevel.STRONG_BEAR, score=-60, reasons=[], crash=False)


def test_basic_buy_allowed(risk_manager):
    decision = risk_manager.can_open_new_position(requested_usdt=Decimal("100"), trading_balance_usdt=Decimal("1000"), regime=NEUTRAL)
    assert decision.allowed


def test_crash_regime_blocks_buys_and_dca(risk_manager):
    buy = risk_manager.can_open_new_position(requested_usdt=Decimal("100"), trading_balance_usdt=Decimal("1000"), regime=CRASH)
    dca = risk_manager.can_dca(regime=CRASH)
    assert not buy.allowed
    assert not dca.allowed


def test_strong_bear_blocks_buys_but_not_dca(risk_manager):
    buy = risk_manager.can_open_new_position(requested_usdt=Decimal("100"), trading_balance_usdt=Decimal("1000"), regime=STRONG_BEAR)
    dca = risk_manager.can_dca(regime=STRONG_BEAR)
    assert not buy.allowed
    assert dca.allowed


def test_exposure_limit_blocks_oversized_request(risk_manager):
    # 30% of 1000 = 300 max total exposure; requesting 350 exceeds both that and MAX_POSITION_USDT
    decision = risk_manager.can_open_new_position(requested_usdt=Decimal("350"), trading_balance_usdt=Decimal("1000"), regime=NEUTRAL)
    assert not decision.allowed


def test_daily_new_capital_cap_blocks_after_threshold(risk_manager):
    risk_manager.record_new_capital_deployed(Decimal("200"))
    decision = risk_manager.can_open_new_position(requested_usdt=Decimal("100"), trading_balance_usdt=Decimal("10000"), regime=NEUTRAL)
    assert not decision.allowed
    assert any("MAX_DAILY_NEW_CAPITAL" in r for r in decision.reasons)


def test_pause_and_resume_buys(risk_manager):
    risk_manager.pause_buys()
    assert not risk_manager.can_open_new_position(requested_usdt=Decimal("50"), trading_balance_usdt=Decimal("10000"), regime=NEUTRAL).allowed
    risk_manager.resume_buys()
    assert risk_manager.can_open_new_position(requested_usdt=Decimal("50"), trading_balance_usdt=Decimal("10000"), regime=NEUTRAL).allowed


def test_consecutive_losses_auto_pause_and_reset_on_win(risk_manager):
    risk_manager.register_trade_result(is_win=False)
    count = risk_manager.register_trade_result(is_win=False)
    assert count == 2
    assert not risk_manager.can_open_new_position(requested_usdt=Decimal("50"), trading_balance_usdt=Decimal("10000"), regime=NEUTRAL).allowed

    reset_count = risk_manager.register_trade_result(is_win=True)
    assert reset_count == 0


def test_emergency_stop_blocks_everything_until_cleared(risk_manager):
    risk_manager.trigger_emergency_stop()
    assert not risk_manager.can_open_new_position(requested_usdt=Decimal("50"), trading_balance_usdt=Decimal("10000"), regime=NEUTRAL).allowed
    assert not risk_manager.can_dca(regime=NEUTRAL).allowed
    risk_manager.clear_emergency_stop()
    risk_manager.resume_buys()
    assert risk_manager.can_open_new_position(requested_usdt=Decimal("50"), trading_balance_usdt=Decimal("10000"), regime=NEUTRAL).allowed


def test_dca_pause_and_resume(risk_manager):
    risk_manager.stop_dca()
    assert not risk_manager.can_dca(regime=NEUTRAL).allowed
    risk_manager.start_dca()
    assert risk_manager.can_dca(regime=NEUTRAL).allowed
