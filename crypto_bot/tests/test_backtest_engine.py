from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

import backtest.engine as engine_module
from backtest.engine import (
    BACKTEST_RESUME_AFTER_HOURS,
    BacktestEngine,
    _BacktestPortfolio,
    merge_aligned,
    prepare_symbol_frames,
)
from backtest.metrics import (
    OPEN_AT_END_REASON,
    BacktestMetrics,
    EquityPoint,
    TradeRecord,
    compute_metrics,
)
from backtest.optimizer import default_objective, grid_search, split_chronologically
from backtest.reports import format_summary
from market.market_regime import RegimeAssessment, RegimeLevel


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


def test_position_still_open_when_data_ends_is_marked_not_dropped(settings, rules):
    """A position opened near the end of the backtested window (and thus
    unable to reach take-profit/DCA-exit before the data runs out) must
    still show up in result.trades and be reported as still open - dropping
    it would silently hide capital at risk on any short window, which is
    exactly the walk-forward optimizer's validation/test segments. It is a
    mark, not a result, so it must not count as a closed trade."""
    n = 2000
    symbol_klines = {"AAAUSDT": _synthetic_ohlcv(n, seed=10, regime="trend_with_dip")}
    btc_klines = _synthetic_ohlcv(n, seed=12, regime="trend_with_dip")
    tuned = settings.model_copy(update={"min_listing_age_days": 0, "news_enabled": False})

    full_result = BacktestEngine(tuned, rules).run(symbol_klines, btc_klines, starting_balance=Decimal("10000"))
    assert full_result.trades, "fixture must produce at least one trade to make this test meaningful"
    last_trade = max(full_result.trades, key=lambda t: t.opened_at)

    # Truncate the raw data to just after that trade's entry bar, before its
    # real close - it can no longer round-trip within the window.
    open_time = pd.Timestamp(last_trade.opened_at)
    cutoff = open_time + pd.Timedelta(hours=6)
    truncated_symbol = {
        s: df[df["open_time"] <= cutoff].reset_index(drop=True) for s, df in symbol_klines.items()
    }
    truncated_btc = btc_klines[btc_klines["open_time"] <= cutoff].reset_index(drop=True)

    truncated_result = BacktestEngine(tuned, rules).run(truncated_symbol, truncated_btc, starting_balance=Decimal("10000"))

    open_at_end = [t for t in truncated_result.trades if t.close_reason == "OPEN_AT_END"]
    assert len(open_at_end) == 1
    assert open_at_end[0].symbol == last_trade.symbol
    assert open_at_end[0].avg_entry_price == last_trade.avg_entry_price
    assert open_at_end[0].opened_at == last_trade.opened_at
    # No sell fee/slippage simulated for a mark-to-market valuation - proceeds
    # are exactly quantity x last close price.
    last_close = Decimal(str(truncated_symbol["AAAUSDT"]["close"].iloc[-1]))
    assert open_at_end[0].proceeds_usdt == open_at_end[0].quantity * last_close

    assert truncated_result.metrics.open_at_end_count == 1
    assert truncated_result.metrics.open_at_end_unrealized_pnl_usdt == open_at_end[0].net_pnl_usdt
    still_open_keys = {(t.symbol, t.opened_at) for t in open_at_end}
    closed_keys = {(t.symbol, t.opened_at) for t in truncated_result.trades} - still_open_keys
    assert truncated_result.metrics.num_trades == len(closed_keys)


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


def _record(symbol, opened_at, closed_at, *, cost, pnl, reason, dca_count=0, worst_dd=0.0):
    return TradeRecord(
        symbol=symbol, opened_at=opened_at, closed_at=closed_at,
        avg_entry_price=Decimal("100"), exit_price=Decimal("100"), quantity=Decimal(cost) / 100,
        cost_usdt=Decimal(cost), proceeds_usdt=Decimal(cost) + Decimal(pnl), net_pnl_usdt=Decimal(pnl),
        net_pnl_percent=Decimal(pnl) / Decimal(cost) * 100, dca_count=dca_count, close_reason=reason,
        worst_drawdown_percent=worst_dd,
    )


def test_compute_metrics_counts_closed_positions_not_sell_slices_or_open_marks():
    """Regression: every TradeRecord used to count as a trade, so a partial
    take-profit position read as two trades (a win and, here, a loss) and
    positions still open when the data ended counted as finished trades on
    their mark-to-market alone. Trade stats are per closed position now;
    still-open ones are reported separately, and balance/equity/exposure
    figures still see every record."""
    start = pd.Timestamp("2024-01-01", tz="UTC")
    day = [(start + pd.Timedelta(days=i)).to_pydatetime() for i in range(6)]
    equity_curve = [EquityPoint(timestamp=d, equity_usdt=Decimal(v)) for d, v in zip(day, [10000, 10005, 10002, 10002, 10008, 10000], strict=True)]
    trades = [
        # A: partial TP (+5) then its remainder trails out at -1 -> ONE +4 USDT winner over 100 USDT, 48h.
        _record("AAAUSDT", day[0], day[1], cost=60, pnl=5, reason="TAKE_PROFIT_PARTIAL", dca_count=1),
        _record("AAAUSDT", day[0], day[2], cost=40, pnl=-1, reason="TRAILING_STOP", dca_count=1),
        # B: a plain -2 USDT loser, 24h.
        _record("BBBUSDT", day[1], day[2], cost=100, pnl=-2, reason="TRAILING_STOP"),
        # C: still open at the end, underwater.
        _record("CCCUSDT", day[3], day[5], cost=100, pnl=-3, reason=OPEN_AT_END_REASON, dca_count=2, worst_dd=-12.0),
        # D: partial TP taken, remainder still open -> the position isn't finished.
        _record("AAAUSDT", day[3], day[4], cost=60, pnl=6, reason="TAKE_PROFIT_PARTIAL"),
        _record("AAAUSDT", day[3], day[5], cost=40, pnl=1, reason=OPEN_AT_END_REASON),
    ]

    metrics = compute_metrics(trades, equity_curve, Decimal("10000"), Decimal("1"))

    assert metrics.num_trades == 2
    assert metrics.win_rate == 50.0
    assert metrics.avg_profit_percent == pytest.approx(4.0)
    assert metrics.avg_loss_percent == pytest.approx(-2.0)
    assert metrics.profit_factor == pytest.approx(2.0)
    assert metrics.avg_holding_time_hours == pytest.approx(36.0)
    assert metrics.dca_frequency_percent == 50.0
    assert metrics.avg_dca_count == 0.5
    assert metrics.open_at_end_count == 2
    assert metrics.open_at_end_unrealized_pnl_usdt == Decimal("-2")

    # Unchanged, every record included: 580 USDT-days deployed over 5 days of a 10000 account.
    assert metrics.exposure_percent == pytest.approx(580 / (10000 * 5) * 100)
    assert metrics.worst_position_drawdown_percent == -12.0
    assert metrics.ending_balance == Decimal("10000")

    summary = format_summary(metrics, symbols=["AAAUSDT", "BBBUSDT", "CCCUSDT"])
    assert "Trades: 2 " in summary
    assert "Still open at end: 2 " in summary


def test_loss_streak_pause_lifts_after_simulated_resume_window(settings):
    """Regression: the consecutive-loss pause cleared only on a win, but
    with no position open no win can happen, so one streak blocked every
    new entry for the rest of the run. BACKTEST_RESUME_AFTER_HOURS of
    simulated time now stands in for the owner's /resume, which also
    resets the loss counter."""
    tuned = settings.model_copy(update={"max_consecutive_bad_trades": 3})
    portfolio = _BacktestPortfolio(tuned, Decimal("10000"))
    neutral = RegimeAssessment(level=RegimeLevel.NEUTRAL, score=0.0, reasons=[], crash=False)
    t0 = pd.Timestamp("2024-01-05 12:00", tz="UTC")
    resume_at = t0 + pd.Timedelta(hours=BACKTEST_RESUME_AFTER_HOURS)

    for _ in range(3):
        portfolio.register_trade_result(is_win=False, now=t0)
    assert portfolio.buy_paused
    # A position still open during the pause closing at a loss must not push the resume back.
    portfolio.register_trade_result(is_win=False, now=t0 + pd.Timedelta(hours=20))

    portfolio.resume_if_due(resume_at - pd.Timedelta(minutes=15))
    assert portfolio.buy_paused
    assert "consecutive-losses pause active" in portfolio.can_open(tuned.initial_order_usdt, neutral, "2024-01-06")[1]

    portfolio.resume_if_due(resume_at)
    assert not portfolio.buy_paused
    assert portfolio.consecutive_bad_trades == 0
    assert "consecutive-losses pause active" not in portfolio.can_open(tuned.initial_order_usdt, neutral, "2024-01-06")[1]

    # Counter reset like /resume: one more loss starts a fresh streak instead of re-pausing.
    portfolio.register_trade_result(is_win=False, now=resume_at + pd.Timedelta(hours=1))
    assert not portfolio.buy_paused


def test_backtest_opens_positions_again_after_a_loss_streak_pause(settings, rules, monkeypatch):
    """End-to-end half of the /resume regression: the run loop must
    actually apply the simulated resume. The run starts paused just before
    the baseline's first entry; that entry must be refused for the pause,
    and entries must resume once the window has passed."""
    n = 2000
    symbol_klines = {"AAAUSDT": _synthetic_ohlcv(n, seed=10, regime="trend_with_dip")}
    btc_klines = _synthetic_ohlcv(n, seed=12, regime="trend_with_dip")
    tuned = settings.model_copy(update={
        "min_listing_age_days": 0, "news_enabled": False, "max_consecutive_bad_trades": 3,
    })

    baseline = BacktestEngine(tuned, rules).run(symbol_klines, btc_klines, starting_balance=Decimal("10000"))
    assert baseline.trades, "fixture must produce at least one trade to make this test meaningful"
    first_entry = pd.Timestamp(min(t.opened_at for t in baseline.trades))
    pause_start = first_entry - pd.Timedelta(minutes=15)
    resume_at = pause_start + pd.Timedelta(hours=BACKTEST_RESUME_AFTER_HOURS)

    class _PausedByLossStreak(_BacktestPortfolio):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            for _ in range(tuned.max_consecutive_bad_trades):
                self.register_trade_result(is_win=False, now=pause_start)

    monkeypatch.setattr(engine_module, "_BacktestPortfolio", _PausedByLossStreak)
    result = BacktestEngine(tuned, rules).run(symbol_klines, btc_klines, starting_balance=Decimal("10000"))

    paused_blocks = [
        row for row in result.no_trade_log
        if row["action"] == "BLOCKED" and "consecutive-losses pause active" in row["reasons"]
    ]
    assert any(row["timestamp"] == first_entry for row in paused_blocks)
    assert all(row["timestamp"] < resume_at for row in paused_blocks)
    assert result.trades, "entries must resume after the simulated /resume"
    assert all(pd.Timestamp(t.opened_at) >= resume_at for t in result.trades)


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


def test_grid_search_empty_param_grid_runs_one_baseline_candidate(settings, rules):
    """An empty param_grid isn't an error - `_expand_grid({})` yields one
    candidate (the unchanged baseline settings), which is a legitimate way
    to just run the current configuration through the same plumbing."""
    n = 1500
    df = _synthetic_ohlcv(n, seed=50, regime="trend_with_dip")
    tuned = settings.model_copy(update={"min_listing_age_days": 0, "news_enabled": False})
    split = split_chronologically({"AAAUSDT": df}, df, rules)

    result = grid_search(tuned, rules, {}, split, objective=lambda m: m.num_trades)
    assert result.best_params == {}
    assert len(result.all_candidates) == 1


def test_grid_search_insufficient_trades_raises_actionable_error(settings, rules):
    """default_objective refuses to name a winner tuned on too few trades -
    this must fail loudly with a message explaining *why*, not with the
    generic "no candidates" message (that's for a genuinely empty grid)."""
    n = 1500  # long enough to clear indicator warmup in every split segment
    df = _synthetic_ohlcv(n, seed=51, regime="flat")  # flat price -> no reversal/dip setups -> ~0 trades
    tuned = settings.model_copy(update={"min_listing_age_days": 0, "news_enabled": False})
    split = split_chronologically({"AAAUSDT": df}, df, rules)

    with pytest.raises(ValueError, match="enough trades"):
        grid_search(tuned, rules, {"min_buy_score": [70, 80]}, split, objective=default_objective)


def _trend_klines():
    n = 2000
    symbols = {
        "AAAUSDT": _synthetic_ohlcv(n, seed=10, regime="trend_with_dip"),
        "BBBUSDT": _synthetic_ohlcv(n, seed=11, regime="trend_with_dip"),
    }
    return symbols, _synthetic_ohlcv(n, seed=12, regime="trend_with_dip")


def _phases(phase, symbols):
    from market.macro_regime import MacroPhase

    days = pd.to_datetime(next(iter(symbols.values()))["open_time"]).dt.date.unique()
    return {d: MacroPhase(phase) for d in days}


def _entry_sizes(monkeypatch):
    sizes = []
    real = engine_module._simulate_buy

    def spy(price, usdt_amount, settings):
        sizes.append(usdt_amount)
        return real(price, usdt_amount, settings)

    monkeypatch.setattr(engine_module, "_simulate_buy", spy)
    return sizes


def test_backtest_blocks_new_entries_in_a_bear_phase_like_live(settings, rules):
    symbols, btc = _trend_klines()
    tuned = settings.model_copy(update={"min_listing_age_days": 0, "news_enabled": False})

    baseline = BacktestEngine(tuned, rules).run(symbols, btc, starting_balance=Decimal("10000"))
    bear = BacktestEngine(tuned, rules).run(
        symbols, btc, starting_balance=Decimal("10000"), macro_phase_by_day=_phases("BEAR", symbols)
    )
    gate_off = BacktestEngine(tuned.model_copy(update={"bear_entry_block": False}), rules).run(
        symbols, btc, starting_balance=Decimal("10000"), macro_phase_by_day=_phases("BEAR", symbols)
    )

    assert baseline.trades  # the synthetic dips do get bought without the gate
    assert bear.trades == []
    assert any("bear market" in e["reasons"] for e in bear.no_trade_log if e["action"] == "BLOCKED")
    assert len(gate_off.trades) == len(baseline.trades)


def test_backtest_uses_the_strong_signal_size_only_in_a_bull_phase(settings, rules, monkeypatch):
    symbols, btc = _trend_klines()
    tuned = settings.model_copy(update={
        "min_listing_age_days": 0, "news_enabled": False, "initial_order_usdt": Decimal("20"),
        "strong_signal_order_usdt": Decimal("50"), "strong_signal_score_margin": 0.0,
        "max_open_positions": 1,  # entries only, so every buy recorded below is an entry
        "max_dca_count": 0,
    })

    sizes = _entry_sizes(monkeypatch)
    BacktestEngine(tuned, rules).run(symbols, btc, starting_balance=Decimal("10000"),
                                     macro_phase_by_day=_phases("BULL", symbols))
    assert sizes and set(sizes) == {Decimal("50")}

    sizes.clear()
    BacktestEngine(tuned, rules).run(symbols, btc, starting_balance=Decimal("10000"),
                                     macro_phase_by_day=_phases("CAUTION", symbols))
    assert sizes and set(sizes) == {Decimal("20")}

    sizes.clear()
    BacktestEngine(tuned, rules).run(symbols, btc, starting_balance=Decimal("10000"))  # phase unknown
    assert sizes and set(sizes) == {Decimal("20")}


def test_phase_by_day_matches_what_the_live_refresh_sees_each_morning():
    from datetime import date, timedelta

    from market.macro_regime import MacroPhase, phase_by_day

    first = date(2025, 1, 1)
    closes = [100.0] * 300 + [90.0] * 5
    opens = [pd.Timestamp(first + timedelta(days=i), tz="UTC") for i in range(len(closes))]
    daily = pd.DataFrame({
        "open_time": opens, "close": closes,
        "close_time": [t + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1) for t in opens],
    })
    third_low_close = first + timedelta(days=302)  # the 3rd day closing at 90

    phases = phase_by_day(daily, third_low_close - timedelta(days=1), third_low_close + timedelta(days=1))

    assert phases[third_low_close] == MacroPhase.BULL  # that day's own close isn't known yet in the morning
    assert phases[third_low_close + timedelta(days=1)] == MacroPhase.BEAR
