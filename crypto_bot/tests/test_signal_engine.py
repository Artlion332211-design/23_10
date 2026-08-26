from __future__ import annotations

from strategy.scoring import SignalWeight, score_signals
from strategy.signal_engine import MultiTimeframeSnapshot, SignalEngine
from tests.conftest import make_snapshot
from utils.time import Timeframe


def test_strong_confirmed_setup_passes_threshold(settings, rules):
    engine = SignalEngine(settings, rules)
    h1 = make_snapshot(
        Timeframe.H1, rsi=32.0, rsi_prev=28.0, rsi_reversal=True, macd_bullish=True, ema_trend_ok=True,
        bb_recovery=True, volume_confirmation=True, vwap_recovery=True, market_structure_bullish=True,
        adx=20.0, plus_di=25.0, minus_di=15.0,
    )
    m15 = make_snapshot(Timeframe.M15, rsi=35.0, rsi_reversal=True)
    h4 = make_snapshot(
        Timeframe.H4, close=100.0, ema_fast=99.0, ema_mid=97.0, ema_slow=90.0, rsi=58.0, macd_hist=0.2,
        adx=28.0, plus_di=26.0, minus_di=12.0,
    )
    breakdown = engine.evaluate("SOLUSDT", MultiTimeframeSnapshot(m15=m15, h1=h1, h4=h4))

    assert breakdown.final_score >= settings.min_buy_score
    assert breakdown.meets_confirmation_rule
    assert not breakdown.blocked
    assert breakdown.confirmed_count >= settings.min_confirmed_signals
    assert len(breakdown.confirmed_categories) >= settings.min_confirmation_categories


def test_weak_setup_does_not_meet_confirmation_rule(settings, rules):
    engine = SignalEngine(settings, rules)
    h1 = make_snapshot(Timeframe.H1, rsi=25.0, rsi_prev=27.0, rsi_reversal=False)  # still falling
    mtf = MultiTimeframeSnapshot(m15=make_snapshot(Timeframe.M15), h1=h1, h4=make_snapshot(Timeframe.H4))
    breakdown = engine.evaluate("XYZUSDT", mtf)

    assert breakdown.final_score < settings.min_buy_score
    assert not breakdown.meets_confirmation_rule


def test_adx_strong_downtrend_vetoes_even_a_high_score(settings, rules):
    engine = SignalEngine(settings, rules)
    h1 = make_snapshot(
        Timeframe.H1, rsi=32.0, rsi_prev=28.0, rsi_reversal=True, macd_bullish=True, ema_trend_ok=True,
        bb_recovery=True, volume_confirmation=True, adx=40.0, plus_di=10.0, minus_di=30.0,
    )
    mtf = MultiTimeframeSnapshot(m15=make_snapshot(Timeframe.M15), h1=h1, h4=make_snapshot(Timeframe.H4))
    breakdown = engine.evaluate("ADXTEST", mtf)

    assert breakdown.blocked
    assert any("ADX veto" in v for v in breakdown.vetoes)


def test_bearish_4h_trend_context_vetoes_bullish_15m_1h(settings, rules):
    engine = SignalEngine(settings, rules)
    h1 = make_snapshot(
        Timeframe.H1, rsi=32.0, rsi_prev=28.0, rsi_reversal=True, macd_bullish=True, ema_trend_ok=True,
        bb_recovery=True, volume_confirmation=True,
    )
    h4_crash = make_snapshot(
        Timeframe.H4, close=80.0, ema_fast=85.0, ema_mid=90.0, ema_slow=100.0, rsi=25.0, macd_hist=-1.0,
        adx=45.0, plus_di=8.0, minus_di=35.0,
    )
    mtf = MultiTimeframeSnapshot(m15=make_snapshot(Timeframe.M15), h1=h1, h4=h4_crash)
    breakdown = engine.evaluate("CRASHCTX", mtf)

    assert breakdown.blocked
    assert any("4h trend" in v for v in breakdown.vetoes)


def test_category_diversity_required_not_just_raw_count():
    """Three confirmed signals worth 80 points but drawn from only two
    categories must NOT satisfy a >=3-signals/>=3-categories rule - this is
    the guard against '5 EMA periods counted as 5 confirmations'."""
    weights = {
        "rsi_reversal": SignalWeight(points=30, category="momentum"),
        "macd_bullish": SignalWeight(points=30, category="momentum"),
        "bollinger_recovery": SignalWeight(points=20, category="volatility"),
        "ema_trend": SignalWeight(points=20, category="trend"),
    }
    raw_signals = {"rsi_reversal": True, "macd_bullish": True, "bollinger_recovery": True, "ema_trend": False}

    breakdown = score_signals(
        "TEST", raw_signals, weights, min_confirmed_signals=3, min_confirmation_categories=3,
    )
    assert breakdown.final_score == 80
    assert breakdown.confirmed_count == 3
    assert len(breakdown.confirmed_categories) == 2  # momentum, volatility - not 3
    assert not breakdown.meets_confirmation_rule
