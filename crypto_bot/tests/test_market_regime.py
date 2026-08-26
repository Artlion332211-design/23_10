from __future__ import annotations

import numpy as np
import pandas as pd

from market.market_regime import MarketRegimeEngine, RegimeLevel, TrendRegime, classify_symbol_trend
from tests.conftest import make_snapshot
from utils.time import Timeframe


def _mtf(**per_tf_overrides):
    return {
        Timeframe.M15: make_snapshot(Timeframe.M15, **per_tf_overrides),
        Timeframe.H1: make_snapshot(Timeframe.H1, **per_tf_overrides),
        Timeframe.H4: make_snapshot(Timeframe.H4, **per_tf_overrides),
    }


def test_bullish_snapshots_classify_as_bull_or_strong_bull(rules):
    engine = MarketRegimeEngine(rules.crash_detector)
    snaps = _mtf(close=105.0, ema_fast=104.0, ema_mid=102.0, ema_slow=100.0, rsi=60.0, macd_hist=0.5, adx=30.0, plus_di=25.0, minus_di=15.0)
    result = engine.evaluate(snaps, recent_df_15m=None)
    assert result.level in (RegimeLevel.BULL, RegimeLevel.STRONG_BULL)
    assert not result.crash


def test_bearish_snapshots_classify_as_bear_or_strong_bear(rules):
    engine = MarketRegimeEngine(rules.crash_detector)
    snaps = _mtf(close=95.0, ema_fast=96.0, ema_mid=98.0, ema_slow=100.0, rsi=35.0, macd_hist=-0.5, adx=30.0, plus_di=10.0, minus_di=25.0)
    result = engine.evaluate(snaps, recent_df_15m=None)
    assert result.level in (RegimeLevel.BEAR, RegimeLevel.STRONG_BEAR)


def test_sharp_drop_with_volume_spike_triggers_crash(rules):
    engine = MarketRegimeEngine(rules.crash_detector)
    bullish_snaps = _mtf(close=105.0, ema_fast=104.0, ema_mid=102.0, ema_slow=100.0, rsi=60.0, macd_hist=0.5, adx=30.0, plus_di=25.0, minus_di=15.0)

    n = 60
    t = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    closes = np.concatenate([np.full(n - 4, 100.0), [96, 92, 90, 88]])
    volumes = np.concatenate([np.full(n - 4, 1000.0), [4000, 4500, 5000, 5200]])
    crash_df = pd.DataFrame({"open_time": t, "close": closes, "volume": volumes})

    result = engine.evaluate(bullish_snaps, recent_df_15m=crash_df)
    assert result.level == RegimeLevel.CRASH
    assert result.crash


def test_no_crash_without_volume_confirmation(rules):
    """A price drop alone (no abnormal volume) should not trip the crash
    detector - it requires both, per spec."""
    engine = MarketRegimeEngine(rules.crash_detector)
    bullish_snaps = _mtf(close=105.0, ema_fast=104.0, ema_mid=102.0, ema_slow=100.0, rsi=60.0, macd_hist=0.5, adx=30.0, plus_di=25.0, minus_di=15.0)

    n = 60
    t = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    closes = np.concatenate([np.full(n - 4, 100.0), [96, 92, 90, 88]])
    volumes = np.full(n, 1000.0)  # flat, no spike
    df = pd.DataFrame({"open_time": t, "close": closes, "volume": volumes})

    result = engine.evaluate(bullish_snaps, recent_df_15m=df)
    assert result.level != RegimeLevel.CRASH


def test_classify_symbol_trend_collapses_to_four_levels():
    bullish = make_snapshot(Timeframe.H4, close=105.0, ema_fast=104.0, ema_mid=102.0, ema_slow=100.0, rsi=60.0, macd_hist=0.5, adx=30.0, plus_di=25.0, minus_di=15.0)
    assert classify_symbol_trend(bullish) == TrendRegime.BULLISH

    crashy = make_snapshot(Timeframe.H4, close=80.0, ema_fast=85.0, ema_mid=90.0, ema_slow=100.0, rsi=25.0, macd_hist=-1.0, adx=45.0, plus_di=8.0, minus_di=35.0)
    assert classify_symbol_trend(crashy) == TrendRegime.CRASH
