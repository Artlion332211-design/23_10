"""Market regime classification - the "Global Market Filter" (BTC-wide) and
the per-symbol 4h trend-context gate share the same scoring core.

Two distinct things live here on purpose:

* `MarketRegimeEngine` - evaluated on BTCUSDT across 15m/1h/4h, produces the
  6-level `RegimeLevel` used by every BUY decision (project rule: "перед
  покупкою будь-якого altcoin бот повинен перевірити BTC"). Includes a fast,
  dedicated crash check (rapid drop + abnormal volume + bearish momentum)
  that can fire faster than the slower 4h-weighted trend composite.
* `classify_symbol_trend` - a cheaper single-timeframe read of a
  *candidate's own* 4h chart, collapsed to 4 levels, used only to stop a
  bullish 15m/5m blip from overriding a bearish 4h trend on that symbol
  (project rule in the multi-timeframe-analysis section). It is not a
  substitute for the BTC filter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd

from config.settings import CrashDetectorConfig
from market.market_data import IndicatorSnapshot
from utils.time import Timeframe


class RegimeLevel(str, Enum):
    STRONG_BULL = "STRONG_BULL"
    BULL = "BULL"
    NEUTRAL = "NEUTRAL"
    BEAR = "BEAR"
    STRONG_BEAR = "STRONG_BEAR"
    CRASH = "CRASH"


class TrendRegime(str, Enum):
    BULLISH = "BULLISH"
    NEUTRAL = "NEUTRAL"
    BEARISH = "BEARISH"
    CRASH = "CRASH"


_COLLAPSE_MAP = {
    RegimeLevel.STRONG_BULL: TrendRegime.BULLISH,
    RegimeLevel.BULL: TrendRegime.BULLISH,
    RegimeLevel.NEUTRAL: TrendRegime.NEUTRAL,
    RegimeLevel.BEAR: TrendRegime.BEARISH,
    RegimeLevel.STRONG_BEAR: TrendRegime.CRASH,
    RegimeLevel.CRASH: TrendRegime.CRASH,
}

_TIMEFRAME_WEIGHTS = {Timeframe.H4: 0.5, Timeframe.H1: 0.35, Timeframe.M15: 0.15}


@dataclass(frozen=True)
class RegimeAssessment:
    level: RegimeLevel
    score: float  # roughly -100 (worst) .. +100 (best)
    reasons: list[str]
    crash: bool


def _trend_points(snapshot: IndicatorSnapshot) -> tuple[float, list[str]]:
    points = 0.0
    reasons: list[str] = []
    tf = snapshot.timeframe.value

    if snapshot.close > snapshot.ema_slow:
        points += 15
        reasons.append(f"{tf}: price above EMA200")
    else:
        points -= 15
        reasons.append(f"{tf}: price below EMA200")

    if snapshot.ema_fast > snapshot.ema_mid:
        points += 10
    else:
        points -= 10

    if snapshot.macd_hist > 0:
        points += 8
    else:
        points -= 8

    if snapshot.rsi >= 55:
        points += 7
    elif snapshot.rsi <= 45:
        points -= 7

    if snapshot.adx >= 25:
        if snapshot.plus_di > snapshot.minus_di:
            points += 10
            reasons.append(f"{tf}: strong uptrend (ADX {snapshot.adx:.0f})")
        else:
            points -= 10
            reasons.append(f"{tf}: strong downtrend (ADX {snapshot.adx:.0f})")

    return points, reasons


def _score_to_level(score: float) -> RegimeLevel:
    if score >= 40:
        return RegimeLevel.STRONG_BULL
    if score >= 15:
        return RegimeLevel.BULL
    if score >= -15:
        return RegimeLevel.NEUTRAL
    if score >= -40:
        return RegimeLevel.BEAR
    return RegimeLevel.STRONG_BEAR


def classify_symbol_trend(snapshot_4h: IndicatorSnapshot) -> TrendRegime:
    points, _ = _trend_points(snapshot_4h)
    score = max(-100.0, min(100.0, points * 2.0))
    return _COLLAPSE_MAP[_score_to_level(score)]


def _detect_crash(df_15m: pd.DataFrame | None, cfg: CrashDetectorConfig) -> tuple[bool, list[str], float | None]:
    if df_15m is None or len(df_15m) < 3:
        return False, [], None
    bars = max(1, round(cfg.window_minutes / 15))
    window = df_15m.tail(bars + 1)
    if len(window) < 2:
        return False, [], None

    start_price = float(window["close"].iloc[0])
    end_price = float(window["close"].iloc[-1])
    if start_price <= 0:
        return False, [], None
    drop_pct = (end_price - start_price) / start_price * 100.0

    vol_ma = float(df_15m["volume"].tail(50).mean()) if "volume" in df_15m else 0.0
    recent_vol = float(window["volume"].iloc[1:].mean()) if "volume" in window and len(window) > 1 else 0.0
    abnormal_vol = bool(vol_ma > 0 and recent_vol > vol_ma * cfg.volume_multiplier)
    bearish_momentum = end_price < float(window["close"].mean())

    is_crash = drop_pct <= -cfg.drop_percent and abnormal_vol and bearish_momentum
    reasons = []
    if is_crash:
        reasons.append(
            f"BTC dropped {drop_pct:.2f}% in {cfg.window_minutes}m on abnormal volume "
            f"({recent_vol:,.0f} vs avg {vol_ma:,.0f})"
        )
    return is_crash, reasons, drop_pct


class MarketRegimeEngine:
    """Evaluated against BTCUSDT only - see `BTC_MARKET_FILTER` in settings."""

    def __init__(self, crash_config: CrashDetectorConfig) -> None:
        self._crash_config = crash_config

    def evaluate(
        self,
        snapshots: dict[Timeframe, IndicatorSnapshot],
        recent_df_15m: pd.DataFrame | None,
    ) -> RegimeAssessment:
        is_crash, crash_reasons, drop_pct = _detect_crash(recent_df_15m, self._crash_config)
        if is_crash:
            return RegimeAssessment(level=RegimeLevel.CRASH, score=-100.0, reasons=crash_reasons, crash=True)

        total = 0.0
        reasons: list[str] = []
        weight_used = 0.0
        for timeframe, weight in _TIMEFRAME_WEIGHTS.items():
            snapshot = snapshots.get(timeframe)
            if snapshot is None:
                continue
            points, tf_reasons = _trend_points(snapshot)
            total += points * weight
            weight_used += weight
            reasons.extend(tf_reasons)

        if weight_used == 0:
            return RegimeAssessment(level=RegimeLevel.NEUTRAL, score=0.0, reasons=["insufficient data"], crash=False)

        score = max(-100.0, min(100.0, (total / weight_used) * 2.0))

        if drop_pct is not None and drop_pct <= -self._crash_config.strong_bear_drop_percent:
            score = min(score, -45.0)
            reasons.append(f"fast drop {drop_pct:.2f}% in {self._crash_config.window_minutes}m window")

        return RegimeAssessment(level=_score_to_level(score), score=score, reasons=reasons, crash=False)
