"""Long-term market phase from BTC daily and weekly candles.

The intraday regime (`market.market_regime`) reads 15m/1h/4h only - its
longest lookback is the 4h EMA200, about 33 days - so during a months-long
bear market every relief rally reads as NEUTRAL or BULL and the bot keeps
buying altcoins into it. Backtests of the 2018, 2022 and 2025-26 bears in the
bot's own engine lost money every time for that reason. This module adds the
slow view:

* BEAR: BTC's daily close below its 200-day SMA on 3 consecutive days. It
  ends after 3 consecutive daily closes back above. Since 2014 the 3-day
  confirmation cut false flips from 32 to 10 for about 2 days of extra lag.
* CAUTION (early warning): not BEAR, but the last weekly close is below both
  the 20-week SMA and the 21-week EMA (the "bull-market support band"). It
  was the earliest slow signal in 2021 and 2025 (-25% and -11% from the top).
* DEEP_BEAR: BEAR with BTC more than 20% below its 200-day SMA (Mayer
  multiple < 0.8 on 3 consecutive closes; it ends after 3 closes at >= 0.85 so
  a price hovering around 0.8 doesn't flip it daily) - historically the zone
  where BTC was cheapest.
* BULL: none of the above.

Informational only: nothing here blocks or changes a trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum

import pandas as pd

SMA_DAYS = 200
CONFIRM_DAYS = 3
SLOPE_DAYS = 20
DEEP_ENTER_MAYER = 0.80
DEEP_EXIT_MAYER = 0.85
# One Binance request (limit 1000). About 800 days have a valid SMA200, so a
# phase's start date is known for runs up to ~2 years (see phase_since).
HISTORY_DAYS = 1000
# The first valid days carry warm-up effects (state machines start "off",
# the weekly EMA is still seeding), so a run reaching back into them has an
# unknown start date rather than a made-up one.
_WARMUP_DAYS = 30
# Early warnings (CAUTION) flip on weekly closes; repeat one at most this often.
CAUTION_ALERT_MIN_INTERVAL = timedelta(days=14)


class MacroPhase(str, Enum):
    BULL = "BULL"
    CAUTION = "CAUTION"
    BEAR = "BEAR"
    DEEP_BEAR = "DEEP_BEAR"


@dataclass(frozen=True)
class MacroAssessment:
    phase: MacroPhase
    as_of: date  # the last CLOSED daily candle used
    phase_since: date | None  # first day of the current phase; None = older than the data window
    phase_days_at_least: int  # days the phase has lasted (a lower bound when phase_since is None)
    btc_close: float
    sma200: float
    mayer: float  # btc_close / sma200
    sma200_rising: bool  # SMA200 today vs SLOPE_DAYS ago
    weekly_close: float | None
    sma20w: float | None
    ema21w: float | None
    sma50w: float | None

    @property
    def is_bear(self) -> bool:
        return self.phase in (MacroPhase.BEAR, MacroPhase.DEEP_BEAR)


def _closed_daily_closes(daily: pd.DataFrame, now: datetime) -> pd.Series:
    """Daily closes indexed by UTC day, without today's still-open candle."""
    df = daily[pd.to_datetime(daily["close_time"], utc=True) < pd.Timestamp(now)]
    closes = pd.Series(df["close"].astype(float).to_numpy(),
                       index=pd.to_datetime(df["open_time"], utc=True).dt.tz_localize(None).dt.normalize())
    return closes[~closes.index.duplicated(keep="last")].sort_index()


def _confirmed(enter: pd.Series, leave: pd.Series, active: pd.Series) -> list[bool]:
    """On after CONFIRM_DAYS consecutive `enter` days, off after
    CONFIRM_DAYS consecutive `leave` days; forced off while not `active`.
    `enter` and `leave` may both be false on a day (a hysteresis band): such
    a day breaks both runs and keeps the current state."""
    state, on_run, off_run, out = False, 0, 0, []
    for is_enter, is_leave, ok in zip(enter, leave, active, strict=True):
        if not ok:
            state, on_run, off_run = False, 0, 0
            out.append(False)
            continue
        on_run = on_run + 1 if is_enter else 0
        off_run = off_run + 1 if is_leave else 0
        if not state and on_run >= CONFIRM_DAYS:
            state = True
        elif state and off_run >= CONFIRM_DAYS:
            state = False
        out.append(state)
    return out


def phase_change_alert_due(
    previous: str, current: str, *, last_caution_alert_at: datetime | None, now: datetime
) -> bool:
    """Which phase changes are worth a Telegram message. Bear start/end and
    the move into the deep zone always are. The weekly-based early warning
    (BULL -> CAUTION) at most every CAUTION_ALERT_MIN_INTERVAL. Steps back
    toward calm inside a group (DEEP_BEAR -> BEAR, CAUTION -> BULL) only show
    in /status and /market - announcing every one spammed the owner with
    contradicting messages days apart in a replay of 2017-2026."""
    bear = {MacroPhase.BEAR.value, MacroPhase.DEEP_BEAR.value}
    if (previous in bear) != (current in bear):
        return True
    if previous == MacroPhase.BEAR.value and current == MacroPhase.DEEP_BEAR.value:
        return True
    if previous == MacroPhase.BULL.value and current == MacroPhase.CAUTION.value:
        return last_caution_alert_at is None or now - last_caution_alert_at >= CAUTION_ALERT_MIN_INTERVAL
    return False


def assess_macro(daily: pd.DataFrame, *, now: datetime) -> MacroAssessment | None:
    """`daily`: Binance 1d klines (`open_time`, `close`, `close_time`, as
    returned by `BinanceClient.get_historical_klines`). Returns None when
    there is not enough closed history for the 200-day SMA."""
    closes = _closed_daily_closes(daily, now)
    if len(closes) < SMA_DAYS + SLOPE_DAYS:
        return None
    sma = closes.rolling(SMA_DAYS).mean()
    valid = sma.notna()
    below = closes < sma
    bear = pd.Series(_confirmed(below, ~below, valid), index=closes.index)
    mayer = closes / sma
    deep = pd.Series(
        _confirmed(mayer < DEEP_ENTER_MAYER, mayer >= DEEP_EXIT_MAYER, bear), index=closes.index
    )

    # Weekly closes (Sunday UTC); only weeks complete by the last closed day.
    weekly = closes.resample("W-SUN").last()
    weekly = weekly[weekly.index <= closes.index[-1]]
    sma20w = weekly.rolling(20).mean()
    ema21w = weekly.ewm(span=21, adjust=False).mean().where(weekly.expanding().count() >= 21)
    sma50w = weekly.rolling(50).mean()

    def on_days(series: pd.Series) -> pd.Series:  # latest complete week as of each day
        return series.reindex(closes.index, method="ffill")

    w_close, w20, w21 = on_days(weekly), on_days(sma20w), on_days(ema21w)
    band_below = ((w_close < w21) & (w_close < w20)).fillna(False)

    phases = pd.Series(MacroPhase.BULL, index=closes.index, dtype=object)
    phases[band_below] = MacroPhase.CAUTION
    phases[bear] = MacroPhase.BEAR
    phases[deep] = MacroPhase.DEEP_BEAR
    phases = phases[valid]

    current = phases.iloc[-1]
    run_start = len(phases) - 1
    while run_start > 0 and phases.iloc[run_start - 1] == current:
        run_start -= 1
    phase_since = phases.index[run_start].date() if run_start >= _WARMUP_DAYS else None

    def last(series: pd.Series) -> float | None:
        value = series.iloc[-1] if len(series) else None
        return None if value is None or pd.isna(value) else float(value)

    return MacroAssessment(
        phase=current,
        as_of=closes.index[-1].date(),
        phase_since=phase_since,
        phase_days_at_least=len(phases) - run_start,
        btc_close=float(closes.iloc[-1]),
        sma200=float(sma.iloc[-1]),
        mayer=float(mayer.iloc[-1]),
        sma200_rising=bool(sma.iloc[-1] > sma.iloc[-1 - SLOPE_DAYS]),
        weekly_close=last(weekly),
        sma20w=last(sma20w),
        ema21w=last(ema21w),
        sma50w=last(sma50w),
    )
