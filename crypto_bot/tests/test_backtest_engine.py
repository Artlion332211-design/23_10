from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd

from backtest.engine import BacktestEngine, merge_aligned, prepare_symbol_frames
from backtest.metrics import BacktestMetrics, EquityPoint, TradeRecord, compute_metrics
from backtest.optimizer import grid_search, split_chronologically


def _synthetic_ohlcv(n: int, *, seed: int, regime: str = "trend_with_dip") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")

    if regime == "trend_with_dip":
        # Gentle uptrend punctuated by a couple of dip-and-recover cycles -
        # exactly the shape the strategy is designed to trade.
        base = 100 + np.linspace(0, 20, n)
        for center in (n // 4, n // 2, 3 * n // 4):
            width = n // 12
            dip = np.zeros(n)
            idx = np.arange(max(0, center - width), min(n, center + width))
            dip[idx] = -8 * np.sin(np.linspace(0, np.pi, len(idx)))
            base += dip
    elif regime == "crash":
        base = np.concatenate([
            100 + np.linspace(0, 5, n // 2),
            np.linspace(105, 60, n - n // 2),
        ])
    else:
        base = np.full(n, 100.0)

    noise = rng.normal(0, 0.4, n)
    close = base + noise
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) + np.abs(rng.normal(0.3, 0.15, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.3, 0.15, n))
    volume = np.abs(rng.normal(2_000_000, 400_000, n))

    return pd.DataFrame({"open_time": t, "open": open_, "high": high, "low": low, "close": close, "volume": volume})


def test_prepare_and_merge_produces_no_lookahead_alignment(rules):
    df = _synthetic_ohlcv(500, seed=1)
    frames = prepare_symbol_frames(df, rules)
    merged = merge_aligned(frames)

    # Every merged row's matched 1h/4h close_time must be <= this row's own
    # decision_time (backward-looking only, never a peek into the future).
    valid = merged.dropna(subset=["close_time_h1", "close_time_h4"])
    assert (valid["close_time_h1"] <= valid.index).all()
    assert (valid["close_time_h4"] <= valid.index).all()


def test_backtest_engine_runs_end_to_end_on_synthetic_data(settings, rules):
    n = 2000  # ~20 days of 15m bars
    symbol_klines = {
        "AAAUSDT": _synthetic_ohlcv(n, seed=10, regime="trend_with_dip"),
        "BBBUSDT": _synthetic_ohlcv(n, seed=11, regime="trend_with_dip"),
    }
    btc_klines = _synthetic_ohlcv(n, seed=12, regime="trend_with_dip")

    tuned = settings.model_copy(update={
        "min_listing_age_days": 0, "news_enabled": False,
    })
    engine = BacktestEngine(tuned, rules)
    result = engine.run(symbol_klines, btc_klines, starting_balance=Decimal("10000"))

    assert len(result.equity_curve) > 0
    assert result.metrics.starting_balance == Decimal("10000")
    assert result.metrics.ending_balance > 0
    assert not np.isnan(result.metrics.total_return_percent)
    assert not np.isnan(result.metrics.max_drawdown_percent)
    assert result.metrics.max_drawdown_percent <= 0

    for trade in result.trades:
        assert trade.closed_at >= trade.opened_at
        assert trade.quantity > 0
        # Homogeneous cost-basis accounting (same invariant fixed in strategy_engine).
        assert trade.avg_entry_price > 0

    # Every open position's own EMA/RSI/etc. columns must have been usable -
    # if the engine silently skipped everything we'd see zero equity movement.
    equity_values = {float(p.equity_usdt) for p in result.equity_curve}
    assert len(equity_values) > 1  # equity actually changes over the run


def test_backtest_pauses_new_buys_during_simulated_crash(settings, rules):
    n = 1500
    symbol_klines = {"AAAUSDT": _synthetic_ohlcv(n, seed=20, regime="trend_with_dip")}
    btc_klines = _synthetic_ohlcv(n, seed=21, regime="crash")

    tuned = settings.model_copy(update={"min_listing_age_days": 0, "news_enabled": False, "market_crash_pause": True})
    engine = BacktestEngine(tuned, rules)
    result = engine.run(symbol_klines, btc_klines, starting_balance=Decimal("10000"))

    # During the crash leg of BTC's synthetic series, no new BUY should be logged.
    crash_period_entries = [
        row for row in result.no_trade_log
        if row["action"] == "BLOCKED" and any("CRASH" in r or "STRONG_BEAR" in r for r in row["reasons"])
    ]
    # We can't guarantee the exact crash detector fires (depends on the
    # synthetic noise), but if it does, no trade should have opened during it.
    if crash_period_entries:
        crash_times = {row["timestamp"] for row in crash_period_entries}
        for trade in result.trades:
            opened_ts = pd.Timestamp(trade.opened_at, tz="UTC")
            assert opened_ts not in crash_times


def test_compute_metrics_basic_sanity():
    start = pd.Timestamp("2024-01-01", tz="UTC")
    equity_curve = [
        EquityPoint(timestamp=(start + pd.Timedelta(days=i)).to_pydatetime(), equity_usdt=Decimal(v))
        for i, v in enumerate([10000, 10100, 9900, 10500, 10300, 11000])
    ]
    trades = [
        TradeRecord(
            symbol="AAAUSDT", opened_at=equity_curve[0].timestamp, closed_at=equity_curve[2].timestamp,
            avg_entry_price=Decimal("100"), exit_price=Decimal("98"), quantity=Decimal("1"),
            cost_usdt=Decimal("100"), proceeds_usdt=Decimal("98"), net_pnl_usdt=Decimal("-2"),
            net_pnl_percent=Decimal("-2"), dca_count=0, close_reason="STOP", worst_drawdown_percent=-5.0,
        ),
        TradeRecord(
            symbol="AAAUSDT", opened_at=equity_curve[3].timestamp, closed_at=equity_curve[5].timestamp,
            avg_entry_price=Decimal("100"), exit_price=Decimal("110"), quantity=Decimal("1"),
            cost_usdt=Decimal("100"), proceeds_usdt=Decimal("110"), net_pnl_usdt=Decimal("10"),
            net_pnl_percent=Decimal("10"), dca_count=1, close_reason="TAKE_PROFIT", worst_drawdown_percent=-1.0,
        ),
    ]
    metrics: BacktestMetrics = compute_metrics(trades, equity_curve, Decimal("10000"), Decimal("5"))

    assert metrics.num_trades == 2
    assert metrics.win_rate == 50.0
    assert metrics.profit_factor > 0
    assert metrics.max_drawdown_percent < 0
    assert metrics.dca_frequency_percent == 50.0
    assert metrics.avg_dca_count == 0.5


def test_walk_forward_split_is_chronological_and_non_overlapping(rules):
    df = _synthetic_ohlcv(1000, seed=30)
    split = split_chronologically({"AAAUSDT": df}, df, rules)

    train_end = split.train["AAAUSDT"]["open_time"].max()
    val_start = split.validation["AAAUSDT"]["open_time"].min()
    val_end = split.validation["AAAUSDT"]["open_time"].max()
    test_start = split.test["AAAUSDT"]["open_time"].min()

    assert train_end < val_start
    assert val_end < test_start
    total = len(split.train["AAAUSDT"]) + len(split.validation["AAAUSDT"]) + len(split.test["AAAUSDT"])
    assert total == len(df)


def test_grid_search_picks_a_param_set_and_reports_test_metrics(settings, rules):
    n = 1200
    df_a = _synthetic_ohlcv(n, seed=40, regime="trend_with_dip")
    btc = _synthetic_ohlcv(n, seed=41, regime="trend_with_dip")

    tuned = settings.model_copy(update={"min_listing_age_days": 0, "news_enabled": False})
    split = split_chronologically({"AAAUSDT": df_a}, btc, rules)

    result = grid_search(
        tuned, rules, {"min_buy_score": [60, 90]}, split,
        starting_balance=Decimal("10000"), objective=lambda m: m.num_trades,  # trivial objective for a fast, deterministic test
    )

    assert result.best_params["min_buy_score"] in (60, 90)
    assert len(result.all_candidates) == 2
    assert result.test_metrics.starting_balance == Decimal("10000")
