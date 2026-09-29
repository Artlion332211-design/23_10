from __future__ import annotations

import numpy as np
import pandas as pd

from risk.correlation import check_correlation_limit, compute_correlation


def test_correlation_blocks_when_cluster_limit_reached(rules):
    cfg = rules.correlation
    n = 200
    rng = np.random.default_rng(1)
    base_returns = rng.normal(0, 0.01, n)
    btc_like = pd.Series(100 * np.cumprod(1 + base_returns))
    eth_like = pd.Series(100 * np.cumprod(1 + base_returns * 0.95 + rng.normal(0, 0.001, n)))

    check_one_open = check_correlation_limit("SOLUSDT", eth_like, ["BTCUSDT"], {"BTCUSDT": btc_like}, cfg)
    assert check_one_open.passed  # only one correlated position open so far

    check_two_open = check_correlation_limit(
        "SOLUSDT", eth_like, ["BTCUSDT", "ETHUSDT"], {"BTCUSDT": btc_like, "ETHUSDT": eth_like}, cfg
    )
    assert not check_two_open.passed
    assert check_two_open.max_correlation >= cfg.correlation_threshold


def test_correlation_aligns_series_by_bar_time_not_position():
    """Two series of the same asset, one missing its first 5 bars (backfilled
    later) - aligning by position shifts them 5 bars apart and hides the
    near-perfect correlation."""
    n = 120
    rng = np.random.default_rng(3)
    times = pd.date_range("2026-09-01", periods=n, freq="h", tz="UTC")
    closes = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, n)), index=times)
    late_backfill = closes.iloc[5:]

    corr = compute_correlation(closes, late_backfill, lookback_bars=90)

    assert corr is not None and corr > 0.99


def test_correlation_passes_for_independent_series(rules):
    cfg = rules.correlation
    n = 200
    rng = np.random.default_rng(2)
    btc_like = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, n)))
    eth_like = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, n)))
    independent = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, n)))

    result = check_correlation_limit(
        "INDEPUSDT", independent, ["BTCUSDT", "ETHUSDT"], {"BTCUSDT": btc_like, "ETHUSDT": eth_like}, cfg
    )
    assert result.passed
