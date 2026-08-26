from __future__ import annotations

import numpy as np
import pandas as pd

from market.indicators import compute_all_indicators, rsi


def _v_shape_ohlcv(n: int = 300, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    down = np.linspace(100, 70, n // 2)
    up = np.linspace(70, 95, n - n // 2)
    base = np.concatenate([down, up])
    noise = rng.normal(0, 0.3, n)
    close = base + noise
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) + np.abs(rng.normal(0.2, 0.1, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.2, 0.1, n))
    volume = np.abs(rng.normal(1000, 200, n))
    volume[140:160] *= 3  # volume spike near the bottom / early recovery
    return pd.DataFrame({"open_time": t, "open": open_, "high": high, "low": low, "close": close, "volume": volume})


def test_rsi_rising_series_approaches_100():
    series = pd.Series(np.linspace(1, 2, 50))
    result = rsi(series, period=14)
    assert result.iloc[-1] > 95


def test_rsi_falling_series_approaches_0():
    series = pd.Series(np.linspace(2, 1, 50))
    result = rsi(series, period=14)
    assert result.iloc[-1] < 5


def test_rsi_flat_series_is_neutral_50():
    series = pd.Series([5.0] * 50)
    result = rsi(series, period=14)
    assert abs(result.iloc[-1] - 50.0) < 1e-6


def test_compute_all_indicators_detects_v_shape_recovery(rules):
    df = _v_shape_ohlcv()
    out = compute_all_indicators(df, rules.indicators)

    # Warmup-period NaNs should be bounded, not spread throughout the series.
    assert out["rsi"].isna().sum() == rules.indicators.rsi.period
    assert out["ema_slow"].isna().sum() == rules.indicators.ema.slow - 1

    # Near the V-bottom / early recovery leg, the bullish signals this
    # project depends on should have fired at least once.
    assert out["rsi_reversal"].iloc[130:180].any()
    assert out["macd_bullish"].iloc[140:260].any()
    assert out["bb_recovery"].any()
    assert out["vwap_recovery"].any()
    assert out["volume_confirmation"].any()
    assert out["swing_low"].iloc[120:170].any()
    assert out["higher_low_structure"].iloc[160:].any()
    assert out["rsi_bullish_divergence"].any()


def test_indicators_have_no_lookahead():
    """Computing indicators on a truncated prefix of the series must produce
    the same values as the full series up to that same point - proof there
    is no forward-looking computation."""
    df = _v_shape_ohlcv(n=200)
    from config.settings import get_config

    cfg = get_config().rules.indicators
    full = compute_all_indicators(df, cfg)
    prefix = compute_all_indicators(df.iloc[:150].copy(), cfg)

    for col in ("rsi", "macd", "ema_fast", "atr", "adx"):
        full_val = full[col].iloc[149]
        prefix_val = prefix[col].iloc[149]
        if pd.isna(full_val) and pd.isna(prefix_val):
            continue
        assert full_val == prefix_val, f"{col} differs between full and truncated computation (lookahead bug)"
