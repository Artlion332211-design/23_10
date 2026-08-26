"""Pure, vectorized technical indicators and derived boolean signals.

Every function is causal: the value at row `i` only ever depends on rows
`<= i`. This is what makes it safe to compute a column once over an entire
historical DataFrame and use it identically in live trading, paper trading
and backtesting - no lookahead bias, by construction. Swing-point-based
signals (divergence, higher-low structure, support) inherently confirm a few
bars late for the same reason: a swing low at bar `i` cannot be known until
`order` bars after it, so it simply stays `False` until confirmed rather than
peeking forward.

All math is done in float64 (via pandas/numpy) - this module is for
*analysis*, never for money. Order pricing/quantities always go through
`exchange/symbol_filters.py`'s `Decimal` arithmetic instead.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config.settings import IndicatorsConfig

REQUIRED_OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")


# ---------------------------------------------------------------------------
# Trend
# ---------------------------------------------------------------------------


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, min_periods=period, adjust=False).mean()


def ema_trend_signal(
    close: pd.Series, ema_fast: pd.Series, ema_mid: pd.Series, ema_slow: pd.Series
) -> pd.Series:
    """Bullish-enough trend structure for a mean-reversion BUY: price has
    reclaimed EMA-fast, EMA-fast is turning back up toward EMA-mid (or
    already above it), and EMA-mid isn't collapsing under EMA-slow."""
    price_reclaimed_fast = close > ema_fast
    fast_cross_up = (ema_fast > ema_mid) & (ema_fast.shift(3) <= ema_mid.shift(3))
    mid_rising = ema_mid > ema_mid.shift(3)
    not_below_slow_and_falling = ~((close < ema_slow) & (ema_mid < ema_mid.shift(5)))
    return price_reclaimed_fast & (fast_cross_up | mid_rising) & not_below_slow_and_falling


# ---------------------------------------------------------------------------
# Momentum: RSI, MACD
# ---------------------------------------------------------------------------


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI. `ewm(alpha=1/period)` is mathematically Wilder's
    smoothing recurrence; the only difference from the original is the seed
    for the first `period` bars, which decays away well before any of our
    lookback windows care about it."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        result = 100.0 - (100.0 / (1.0 + rs))
    result = result.mask(avg_loss == 0, 100.0)
    result = result.mask((avg_loss == 0) & (avg_gain == 0), 50.0)
    return result


def rsi_slope(rsi_series: pd.Series, lookback: int = 3) -> pd.Series:
    return (rsi_series - rsi_series.shift(lookback)) / lookback


def rsi_reversal_from_oversold(rsi_series: pd.Series, *, oversold: float = 30, lookback: int = 5) -> pd.Series:
    """True when RSI dipped below `oversold` within the last `lookback` bars
    (inclusive of the current one) and is now ticking up. Deliberately not
    just `rsi < 30`: a falling knife stays below 30 without ever satisfying
    the "turning up" half."""
    recently_oversold = rsi_series.rolling(lookback, min_periods=1).min() < oversold
    turning_up = rsi_series.diff() > 0
    return recently_oversold & turning_up


def macd(close: pd.Series, *, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "macd_signal": signal_line, "macd_hist": histogram})


def macd_bullish_signal(macd_df: pd.DataFrame) -> pd.Series:
    """True on a bullish crossover, OR when a still-negative histogram has
    been shrinking for 2+ bars with *accelerating* momentum. A bare
    crossover is intentionally not the only path - by itself it's too weak
    to count as confirmation (see project rule: simple MACD crossover must
    not be sufficient for entry)."""
    macd_line, signal_line, hist = macd_df["macd"], macd_df["macd_signal"], macd_df["macd_hist"]
    crossed_up = (macd_line > signal_line) & (macd_line.shift(1) <= signal_line.shift(1))
    hist_improving = (hist > hist.shift(1)) & (hist.shift(1) > hist.shift(2))
    accelerating = (hist - hist.shift(1)) > (hist.shift(1) - hist.shift(2))
    negative_hist_recovering = (hist < 0) & hist_improving & accelerating
    return crossed_up | negative_hist_recovering


# ---------------------------------------------------------------------------
# Volatility: Bollinger Bands, ATR
# ---------------------------------------------------------------------------


def bollinger_bands(close: pd.Series, *, period: int = 20, stddev: float = 2.0) -> pd.DataFrame:
    mid = close.rolling(period, min_periods=period).mean()
    std = close.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + stddev * std
    lower = mid - stddev * std
    bandwidth = (upper - lower) / mid
    return pd.DataFrame({"bb_upper": upper, "bb_mid": mid, "bb_lower": lower, "bb_bandwidth": bandwidth})


def bollinger_recovery_signal(close: pd.Series, bb: pd.DataFrame, *, lookback: int = 5) -> pd.Series:
    """Price poked below the lower band within `lookback` bars and has now
    reclaimed the channel - preferred over "currently below the band"."""
    touched_below = (close < bb["bb_lower"]).astype(int).rolling(lookback, min_periods=1).max().astype(bool)
    back_inside = close >= bb["bb_lower"]
    return touched_below & back_inside


def bollinger_squeeze(bb: pd.DataFrame, *, lookback: int = 40, percentile: float = 20) -> pd.Series:
    """True when bandwidth sits in the bottom `percentile` of its trailing
    `lookback` window - a volatility squeeze that often precedes expansion."""

    def _pct_rank(window: np.ndarray) -> float:
        if len(window) < 2 or np.isnan(window[-1]):
            return np.nan
        return float((window < window[-1]).sum()) / (len(window) - 1) * 100.0

    ranks = bb["bb_bandwidth"].rolling(lookback, min_periods=lookback).apply(_pct_rank, raw=True)
    return ranks <= percentile


def true_range(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    return pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, *, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def distance_from_ema_in_atr(close: pd.Series, ema_series: pd.Series, atr_series: pd.Series) -> pd.Series:
    return (close - ema_series) / atr_series.replace(0, np.nan)


# ---------------------------------------------------------------------------
# Trend strength: ADX / DI
# ---------------------------------------------------------------------------


def adx(df: pd.DataFrame, *, period: int = 14) -> pd.DataFrame:
    high, low = df["high"], df["low"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)

    smoothed_tr = true_range(df).ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    smoothed_plus_dm = plus_dm.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    smoothed_minus_dm = minus_dm.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * (smoothed_plus_dm / smoothed_tr)
        minus_di = 100.0 * (smoothed_minus_dm / smoothed_tr)
        dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx_line = dx.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    return pd.DataFrame({"plus_di": plus_di, "minus_di": minus_di, "adx": adx_line})


def adx_bearish_veto(adx_df: pd.DataFrame, *, adx_threshold: float = 35, di_diff_threshold: float = 10) -> pd.Series:
    """True when the trend is a strong, established downtrend (high ADX,
    -DI well above +DI) - blocks mean-reversion BUYs even on an oversold
    RSI, since that's much more likely a falling knife than a dip."""
    return (adx_df["adx"] >= adx_threshold) & ((adx_df["minus_di"] - adx_df["plus_di"]) >= di_diff_threshold)


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------


def volume_ma(volume: pd.Series, *, period: int = 20) -> pd.Series:
    return volume.rolling(period, min_periods=period).mean()


def abnormal_volume(volume: pd.Series, vol_ma: pd.Series, *, multiplier: float = 2.0) -> pd.Series:
    return volume > (vol_ma * multiplier)


def obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["close"].diff()).fillna(0.0)
    return (direction * df["volume"]).cumsum()


def volume_confirmation_signal(df: pd.DataFrame, vol_ma: pd.Series, *, multiplier: float = 1.2) -> pd.Series:
    """A bullish candle backed by real participation: above-average volume,
    or a confirming OBV up-tick. Volume is a required, independent
    confirmation - never inferred from price alone."""
    bullish_candle = df["close"] > df["open"]
    above_average_volume = df["volume"] > (vol_ma * multiplier)
    obv_rising = obv(df).diff() > 0
    return bullish_candle & (above_average_volume | obv_rising)


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------


def vwap_session(df: pd.DataFrame) -> pd.Series:
    """VWAP anchored to each UTC calendar day."""
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = typical_price * df["volume"]
    day = df["open_time"].dt.floor("D")
    cum_pv = pv.groupby(day).cumsum()
    cum_vol = df["volume"].groupby(day).cumsum()
    return cum_pv / cum_vol


def vwap_rolling(df: pd.DataFrame, *, window: int = 96) -> pd.Series:
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = typical_price * df["volume"]
    return pv.rolling(window, min_periods=window).sum() / df["volume"].rolling(window, min_periods=window).sum()


def vwap_recovery_signal(close: pd.Series, vwap: pd.Series, *, lookback: int = 5) -> pd.Series:
    below = (close < vwap).astype(int).rolling(lookback, min_periods=1).max().astype(bool)
    back_above = close >= vwap
    return below & back_above


# ---------------------------------------------------------------------------
# Market structure: swings, divergence, higher-low/high, support
# ---------------------------------------------------------------------------


def swing_lows(close: pd.Series, *, order: int = 3) -> pd.Series:
    """True at bar i if close[i] is the (first) minimum within +/- order bars.
    Confirms `order` bars late by construction - see module docstring."""
    values = close.to_numpy()
    n = len(values)
    result = np.zeros(n, dtype=bool)
    for i in range(order, n - order):
        window = values[i - order : i + order + 1]
        if values[i] == window.min() and int(np.argmin(window)) == order:
            result[i] = True
    return pd.Series(result, index=close.index)


def swing_highs(close: pd.Series, *, order: int = 3) -> pd.Series:
    values = close.to_numpy()
    n = len(values)
    result = np.zeros(n, dtype=bool)
    for i in range(order, n - order):
        window = values[i - order : i + order + 1]
        if values[i] == window.max() and int(np.argmax(window)) == order:
            result[i] = True
    return pd.Series(result, index=close.index)


def bullish_divergence(close: pd.Series, rsi_series: pd.Series, *, lookback: int = 20, order: int = 3) -> pd.Series:
    """Classic bullish divergence: the two most recent confirmed swing lows
    show price making a lower low while RSI makes a higher low."""
    is_swing_low = swing_lows(close, order=order).to_numpy()
    close_vals, rsi_vals = close.to_numpy(), rsi_series.to_numpy()
    n = len(close_vals)
    result = np.zeros(n, dtype=bool)
    positions: list[int] = []
    for i in range(n):
        if is_swing_low[i]:
            positions.append(i)
        while positions and positions[0] < i - lookback:
            positions.pop(0)
        if len(positions) >= 2:
            first, second = positions[-2], positions[-1]
            if close_vals[second] < close_vals[first] and rsi_vals[second] > rsi_vals[first]:
                result[i] = True
    return pd.Series(result, index=close.index)


def higher_low_structure(close: pd.Series, *, order: int = 3) -> pd.Series:
    """Persists True from the bar where the two most recent confirmed swing
    lows are rising, until a new, lower swing low breaks the pattern."""
    is_low = swing_lows(close, order=order).to_numpy()
    vals = close.to_numpy()
    n = len(vals)
    result = np.zeros(n, dtype=bool)
    positions: list[int] = []
    for i in range(n):
        if is_low[i]:
            positions.append(i)
        if len(positions) >= 2 and vals[positions[-1]] > vals[positions[-2]]:
            result[i] = True
    return pd.Series(result, index=close.index)


def higher_high_structure(close: pd.Series, *, order: int = 3) -> pd.Series:
    is_high = swing_highs(close, order=order).to_numpy()
    vals = close.to_numpy()
    n = len(vals)
    result = np.zeros(n, dtype=bool)
    positions: list[int] = []
    for i in range(n):
        if is_high[i]:
            positions.append(i)
        if len(positions) >= 2 and vals[positions[-1]] > vals[positions[-2]]:
            result[i] = True
    return pd.Series(result, index=close.index)


def nearest_support(close: pd.Series, *, order: int = 3, lookback: int = 60) -> pd.Series:
    """Most recent confirmed swing-low price, held for up to `lookback` bars.
    A deliberately simple proxy for "nearest support" - one input among many
    into scoring, not a full clustering S/R engine."""
    is_low = swing_lows(close, order=order).to_numpy()
    vals = close.to_numpy()
    n = len(vals)
    result = np.full(n, np.nan)
    last_low_pos: int | None = None
    last_low_val = np.nan
    for i in range(n):
        if is_low[i]:
            last_low_pos, last_low_val = i, vals[i]
        if last_low_pos is not None and (i - last_low_pos) <= lookback:
            result[i] = last_low_val
    return pd.Series(result, index=close.index)


def distance_from_support_pct(close: pd.Series, support: pd.Series) -> pd.Series:
    with np.errstate(divide="ignore", invalid="ignore"):
        return (close - support) / support * 100.0


# ---------------------------------------------------------------------------
# Candle patterns (inputs into market-structure scoring only - never
# sufficient for a BUY on their own, per project rule)
# ---------------------------------------------------------------------------


def bullish_engulfing(df: pd.DataFrame) -> pd.Series:
    prev_open, prev_close = df["open"].shift(1), df["close"].shift(1)
    bearish_prev = prev_close < prev_open
    bullish_now = df["close"] > df["open"]
    engulfs = (df["open"] <= prev_close) & (df["close"] >= prev_open)
    return bearish_prev & bullish_now & engulfs


def hammer(df: pd.DataFrame, *, body_ratio: float = 0.35, lower_wick_ratio: float = 2.0) -> pd.Series:
    body = (df["close"] - df["open"]).abs()
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    lower_wick = df[["open", "close"]].min(axis=1) - df["low"]
    upper_wick = df["high"] - df[["open", "close"]].max(axis=1)
    small_body = (body / rng) <= body_ratio
    safe_body = body.replace(0, np.nan).fillna(rng * 0.01)
    long_lower_wick = lower_wick >= (lower_wick_ratio * safe_body)
    small_upper_wick = upper_wick <= (safe_body * 1.2)
    return small_body & long_lower_wick & small_upper_wick & rng.notna()


def morning_star(df: pd.DataFrame) -> pd.Series:
    open1, close1 = df["open"].shift(2), df["close"].shift(2)
    open2, close2 = df["open"].shift(1), df["close"].shift(1)
    open3, close3 = df["open"], df["close"]
    day1_bearish = close1 < open1
    day1_body = (open1 - close1).abs()
    day2_small_body = (open2 - close2).abs() < (day1_body * 0.5)
    day2_gapped_down = pd.concat([open2, close2], axis=1).max(axis=1) < close1
    day3_bullish = close3 > open3
    day3_recovers = close3 > ((open1 + close1) / 2.0)
    return day1_bearish & day2_small_body & day2_gapped_down & day3_bullish & day3_recovers


def rejection_candle(df: pd.DataFrame, *, wick_ratio: float = 2.0) -> pd.Series:
    """Long lower wick, closing in the top part of the range - price was
    pushed down hard intrabar and rejected."""
    body = (df["close"] - df["open"]).abs()
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    lower_wick = df[["open", "close"]].min(axis=1) - df["low"]
    close_position = (df["close"] - df["low"]) / rng
    safe_body = body.replace(0, np.nan).fillna(rng * 0.05)
    return (lower_wick >= (wick_ratio * safe_body)) & (close_position >= 0.6) & rng.notna()


# ---------------------------------------------------------------------------
# Orchestration: compute every indicator/signal column in one pass
# ---------------------------------------------------------------------------


def compute_all_indicators(df: pd.DataFrame, config: IndicatorsConfig) -> pd.DataFrame:
    """Add every indicator/derived-signal column to a copy of `df`.

    `df` must have at least `open_time, open, high, low, close, volume`
    (as produced by `exchange.binance_client.get_klines`).
    """
    missing = [c for c in REQUIRED_OHLCV_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"compute_all_indicators: missing required columns {missing}")

    out = df.copy()

    out["ema_fast"] = ema(out["close"], config.ema.fast)
    out["ema_mid"] = ema(out["close"], config.ema.mid)
    out["ema_slow"] = ema(out["close"], config.ema.slow)
    out["ema_trend_ok"] = ema_trend_signal(out["close"], out["ema_fast"], out["ema_mid"], out["ema_slow"])

    out["rsi"] = rsi(out["close"], config.rsi.period)
    out["rsi_slope"] = rsi_slope(out["rsi"], config.rsi.slope_lookback)
    out["rsi_reversal"] = rsi_reversal_from_oversold(
        out["rsi"], oversold=config.rsi.oversold, lookback=config.rsi.reversal_lookback
    )
    out["rsi_bullish_divergence"] = bullish_divergence(
        out["close"], out["rsi"], lookback=config.swing_structure.lookback, order=config.swing_structure.order
    )

    macd_df = macd(out["close"], fast=config.macd.fast, slow=config.macd.slow, signal=config.macd.signal)
    out = pd.concat([out, macd_df], axis=1)
    out["macd_bullish"] = macd_bullish_signal(macd_df)

    bb_df = bollinger_bands(out["close"], period=config.bollinger.period, stddev=config.bollinger.stddev)
    out = pd.concat([out, bb_df], axis=1)
    out["bb_recovery"] = bollinger_recovery_signal(out["close"], bb_df)
    out["bb_squeeze"] = bollinger_squeeze(
        bb_df, lookback=config.bollinger.squeeze_lookback, percentile=config.bollinger.squeeze_percentile
    )

    out["atr"] = atr(out, period=config.atr.period)
    out["distance_from_ema_fast_atr"] = distance_from_ema_in_atr(out["close"], out["ema_fast"], out["atr"])

    adx_df = adx(out, period=config.adx.period)
    out = pd.concat([out, adx_df], axis=1)

    out["volume_ma"] = volume_ma(out["volume"], period=config.volume.ma_period)
    out["abnormal_volume"] = abnormal_volume(out["volume"], out["volume_ma"], multiplier=config.volume.abnormal_multiplier)
    out["obv"] = obv(out)
    out["volume_confirmation"] = volume_confirmation_signal(
        out, out["volume_ma"], multiplier=config.volume.confirmation_multiplier
    )

    if config.vwap.anchor == "session" and "open_time" in out.columns:
        out["vwap"] = vwap_session(out)
    else:
        out["vwap"] = vwap_rolling(out, window=config.vwap.rolling_window)
    out["vwap_recovery"] = vwap_recovery_signal(out["close"], out["vwap"])

    order, lookback = config.swing_structure.order, config.swing_structure.lookback
    out["swing_low"] = swing_lows(out["close"], order=order)
    out["swing_high"] = swing_highs(out["close"], order=order)
    out["higher_low_structure"] = higher_low_structure(out["close"], order=order)
    out["higher_high_structure"] = higher_high_structure(out["close"], order=order)
    out["support"] = nearest_support(out["close"], order=order, lookback=lookback)
    out["distance_from_support_pct"] = distance_from_support_pct(out["close"], out["support"])

    out["bullish_engulfing"] = bullish_engulfing(out)
    out["hammer"] = hammer(out)
    out["morning_star"] = morning_star(out)
    out["rejection_candle"] = rejection_candle(out)
    out["candle_pattern_bullish"] = (
        out["bullish_engulfing"] | out["hammer"] | out["morning_star"] | out["rejection_candle"]
    )

    close_to_support = out["distance_from_support_pct"].abs() <= 1.5
    out["market_structure_bullish"] = out["higher_low_structure"] | out["candle_pattern_bullish"] | close_to_support

    return out
