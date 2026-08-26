from __future__ import annotations

import numpy as np
import pandas as pd

from risk.correlation import check_correlation_limit


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
