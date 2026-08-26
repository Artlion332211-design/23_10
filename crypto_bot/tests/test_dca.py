from __future__ import annotations

from decimal import Decimal

from strategy.dca import dca_plan, evaluate_dca, next_dca_level
from strategy.scoring import ScoreBreakdown


def _empty_breakdown(score: float, blocked: bool = False) -> ScoreBreakdown:
    return ScoreBreakdown(
        symbol="SOLUSDT", technical_score=score, news_adjustment=0, regime_adjustment=0, final_score=score,
        signals=[], confirmed_count=5, confirmed_categories=["trend", "momentum", "volume", "volatility", "structure"],
        vetoes=["blocked"] if blocked else [], meets_confirmation_rule=True,
    )


def test_dca_plan_matches_configured_levels(settings):
    plan = dca_plan(settings)
    assert len(plan) == settings.max_dca_count
    assert plan[0].drop_percent == settings.dca_level_1
    assert plan[0].size_usdt == settings.dca_size_1_usdt


def test_next_dca_level_triggers_at_correct_threshold(settings):
    level = next_dca_level(current_price=Decimal("97"), avg_entry_price=Decimal("100"), dca_count_done=0, settings=settings)
    assert level is not None
    assert level.level_index == 1


def test_next_dca_level_none_when_drop_insufficient(settings):
    level = next_dca_level(current_price=Decimal("99"), avg_entry_price=Decimal("100"), dca_count_done=0, settings=settings)
    assert level is None


def test_next_dca_level_respects_max_dca_count(settings):
    level = next_dca_level(
        current_price=Decimal("50"), avg_entry_price=Decimal("100"),
        dca_count_done=settings.max_dca_count, settings=settings,
    )
    assert level is None


def test_evaluate_dca_allowed_when_thesis_holds(settings):
    decision = evaluate_dca(
        current_price=Decimal("97"), avg_entry_price=Decimal("100"), dca_count_done=0,
        current_position_cost_usdt=Decimal("100"), settings=settings, score_breakdown=_empty_breakdown(80),
        market_crash=False, news_blocks_trading=False, liquidity_ok=True,
    )
    assert decision.allowed
    assert decision.level is not None


def test_evaluate_dca_blocked_on_market_crash(settings):
    decision = evaluate_dca(
        current_price=Decimal("97"), avg_entry_price=Decimal("100"), dca_count_done=0,
        current_position_cost_usdt=Decimal("100"), settings=settings, score_breakdown=_empty_breakdown(80),
        market_crash=True, news_blocks_trading=False, liquidity_ok=True,
    )
    assert not decision.allowed
    assert any("crash" in r.lower() for r in decision.reasons)


def test_evaluate_dca_blocked_below_min_dca_score(settings):
    decision = evaluate_dca(
        current_price=Decimal("97"), avg_entry_price=Decimal("100"), dca_count_done=0,
        current_position_cost_usdt=Decimal("100"), settings=settings,
        score_breakdown=_empty_breakdown(settings.min_dca_score - 1),
        market_crash=False, news_blocks_trading=False, liquidity_ok=True,
    )
    assert not decision.allowed


def test_evaluate_dca_blocked_when_would_exceed_max_position(settings):
    decision = evaluate_dca(
        current_price=Decimal("97"), avg_entry_price=Decimal("100"), dca_count_done=0,
        current_position_cost_usdt=settings.max_position_usdt - Decimal("1"),
        settings=settings, score_breakdown=_empty_breakdown(90),
        market_crash=False, news_blocks_trading=False, liquidity_ok=True,
    )
    assert not decision.allowed
    assert any("MAX_POSITION_USDT" in r for r in decision.reasons)


def test_evaluate_dca_blocked_on_bad_news_or_illiquidity(settings):
    news_blocked = evaluate_dca(
        current_price=Decimal("97"), avg_entry_price=Decimal("100"), dca_count_done=0,
        current_position_cost_usdt=Decimal("100"), settings=settings, score_breakdown=_empty_breakdown(80),
        market_crash=False, news_blocks_trading=True, liquidity_ok=True,
    )
    assert not news_blocked.allowed

    illiquid = evaluate_dca(
        current_price=Decimal("97"), avg_entry_price=Decimal("100"), dca_count_done=0,
        current_position_cost_usdt=Decimal("100"), settings=settings, score_breakdown=_empty_breakdown(80),
        market_crash=False, news_blocks_trading=False, liquidity_ok=False,
    )
    assert not illiquid.allowed
