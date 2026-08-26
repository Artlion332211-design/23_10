from __future__ import annotations

import pytest

from config.settings import get_config
from database.migrations import run_migrations
from database.session import init_engine, reset_for_tests


@pytest.fixture
def db_engine(tmp_path):
    reset_for_tests()
    engine = init_engine(f"sqlite:///{tmp_path}/test.db")
    run_migrations(engine)
    yield engine
    reset_for_tests()


@pytest.fixture
def app_config():
    return get_config()


@pytest.fixture
def settings(app_config):
    return app_config.env


@pytest.fixture
def rules(app_config):
    return app_config.rules


def make_snapshot(tf, **overrides):
    """Build an IndicatorSnapshot with sane neutral defaults, overridden per
    test - avoids every test needing to know every field."""
    from market.market_data import IndicatorSnapshot

    base = dict(
        symbol="SOLUSDT", timeframe=tf, open_time=None, close=100.0, open=99.0, high=101.0, low=98.0, volume=2000.0,
        rsi=45.0, rsi_prev=40.0, rsi_slope=1.0, rsi_reversal=False, rsi_bullish_divergence=False,
        macd=0.1, macd_signal=0.05, macd_hist=0.05, macd_hist_prev=0.02, macd_bullish=False,
        ema_fast=99.0, ema_mid=98.0, ema_slow=95.0, ema_trend_ok=False,
        bb_upper=102.0, bb_mid=99.0, bb_lower=97.0, bb_recovery=False, bb_squeeze=False,
        atr=1.0, distance_from_ema_fast_atr=0.5, adx=20.0, plus_di=25.0, minus_di=15.0,
        volume_ma=1500.0, abnormal_volume=False, volume_confirmation=False, obv=100.0, obv_prev=90.0,
        vwap=99.5, vwap_recovery=False, swing_low=False, swing_high=False, higher_low_structure=False,
        higher_high_structure=False, support=97.0, distance_from_support_pct=1.0,
        candle_pattern_bullish=False, market_structure_bullish=False,
    )
    base.update(overrides)
    return IndicatorSnapshot(**base)
