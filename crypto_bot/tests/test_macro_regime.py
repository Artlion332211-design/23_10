from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from market.macro_regime import MacroPhase, assess_macro

FLAT = [100.0] * 300


def _daily(closes: list[float], start: datetime = datetime(2025, 1, 1, tzinfo=UTC)) -> pd.DataFrame:
    """Binance-style 1d klines: one row per UTC day."""
    opens = [start + timedelta(days=i) for i in range(len(closes))]
    return pd.DataFrame({
        "open_time": pd.to_datetime(opens, utc=True),
        "close": closes,
        "close_time": pd.to_datetime([t + timedelta(days=1) - timedelta(milliseconds=1) for t in opens], utc=True),
    })


def _assess(closes: list[float]):
    daily = _daily(closes)
    return assess_macro(daily, now=daily["close_time"].iloc[-1].to_pydatetime() + timedelta(minutes=1))


def test_bear_needs_three_consecutive_daily_closes_below_the_200_day_sma():
    """One or two closes below the line were mostly noise since 2014 (32 of
    43 raw flips reversed within a month)."""
    assert _assess(FLAT + [90.0] * 2).phase == MacroPhase.BULL
    a = _assess(FLAT + [90.0] * 3)
    assert a.phase == MacroPhase.BEAR
    assert a.is_bear
    assert round(a.mayer, 2) == 0.90


def test_bear_ends_only_after_three_consecutive_closes_back_above():
    assert _assess(FLAT + [90.0] * 5 + [110.0] * 2).phase == MacroPhase.BEAR
    a = _assess(FLAT + [90.0] * 5 + [110.0] * 3)
    assert a.phase == MacroPhase.BULL
    assert a.phase_since == a.as_of  # the phase started on the third close back above


def test_deep_bear_when_btc_is_more_than_20_percent_below_the_200_day_sma():
    a = _assess(FLAT + [70.0] * 5)
    assert a.phase == MacroPhase.DEEP_BEAR
    assert a.is_bear


def test_caution_when_the_weekly_close_loses_the_bull_market_support_band():
    """Early warning before the daily 200-day line breaks: daily close still
    above the SMA200, weekly close already below the 20-week SMA and the
    21-week EMA."""
    a = _assess(list(np.linspace(100, 400, 320)) + list(np.linspace(400, 340, 35)))
    assert a.phase == MacroPhase.CAUTION
    assert not a.is_bear
    assert a.btc_close > a.sma200
    assert a.weekly_close is not None and a.sma20w is not None and a.ema21w is not None
    assert a.weekly_close < a.sma20w and a.weekly_close < a.ema21w


def test_the_still_open_daily_candle_is_ignored():
    """Binance returns today's candle too; its 'close' is just the latest
    price, so a mid-day dip must not count as a daily close."""
    daily = _daily(FLAT + [90.0] * 2 + [80.0])
    now = daily["close_time"].iloc[-2].to_pydatetime() + timedelta(hours=5)  # last candle still open
    a = assess_macro(daily, now=now)
    assert a is not None
    assert a.phase == MacroPhase.BULL
    assert a.as_of == daily["open_time"].iloc[-2].date()


def test_returns_none_without_enough_history_for_the_200_day_sma():
    assert assess_macro(_daily([100.0] * 210), now=datetime(2030, 1, 1, tzinfo=UTC)) is None


def test_deep_bear_does_not_flip_daily_when_btc_hovers_around_the_threshold():
    """Mayer alternating 0.79/0.81 used to flip BEAR<->DEEP_BEAR on every
    close (10 alerts in a month in Nov-Dec 2025). Entering needs 3 closes
    below 0.80, leaving 3 closes at or above 0.85."""
    hovering = [79.0, 81.0] * 6
    assert _assess(FLAT + [90.0] * 3 + hovering).phase == MacroPhase.BEAR
    a = _assess(FLAT + [90.0] * 3 + [79.0] * 3 + hovering)
    assert a.phase == MacroPhase.DEEP_BEAR  # 0.81 is inside the band: stays deep
    assert _assess(FLAT + [90.0] * 3 + [79.0] * 3 + [86.0] * 2).phase == MacroPhase.DEEP_BEAR
    assert _assess(FLAT + [90.0] * 3 + [79.0] * 3 + [86.0] * 3).phase == MacroPhase.BEAR


def test_phase_start_date_is_unknown_rather_than_wrong_for_a_phase_older_than_the_window():
    a = _assess([100.0 + i * 0.1 for i in range(600)])  # one long bull run
    assert a.phase == MacroPhase.BULL
    assert a.phase_since is None
    assert a.phase_days_at_least >= 400


def test_alert_policy_announces_bear_start_end_and_deep_zone_but_not_every_step_back():
    from datetime import datetime as dt

    from market.macro_regime import phase_change_alert_due

    now = dt(2026, 11, 5, tzinfo=UTC)
    due = lambda prev, cur, last=None: phase_change_alert_due(prev, cur, last_caution_alert_at=last, now=now)  # noqa: E731
    assert due("BULL", "BEAR") and due("CAUTION", "BEAR") and due("BEAR", "BULL") and due("DEEP_BEAR", "CAUTION")
    assert due("BEAR", "DEEP_BEAR")
    assert not due("DEEP_BEAR", "BEAR")
    assert not due("CAUTION", "BULL")
    assert due("BULL", "CAUTION")
    assert not due("BULL", "CAUTION", now - timedelta(days=13))  # repeated early warning within 14 days
    assert due("BULL", "CAUTION", now - timedelta(days=14))


def test_early_warning_fires_on_a_falling_50_day_sma_before_the_weekly_band_and_the_200_day_line():
    """The earliest warning at every BTC top since 2017: 3 closes below a
    50-day SMA that is itself falling, while the weekly band and the 200-day
    line still hold."""
    base = list(np.linspace(100, 400, 300))
    a = _assess(base + list(np.linspace(400, 380, 40)) + [361.0] * 3)
    assert a.phase == MacroPhase.CAUTION
    assert a.early_warning
    assert a.btc_close > a.sma200
    assert a.weekly_close is not None and a.sma20w is not None and a.ema21w is not None
    assert not (a.weekly_close < a.sma20w and a.weekly_close < a.ema21w)  # the weekly rule alone would say BULL
    # The same dip while the 50-day SMA is still rising is not a warning.
    b = _assess(base + list(np.linspace(400, 380, 30)) + [361.0] * 3)
    assert b.phase == MacroPhase.BULL
    assert not b.early_warning
