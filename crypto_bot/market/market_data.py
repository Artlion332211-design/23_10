"""Multi-timeframe candle storage and indicator-snapshot extraction.

`MarketDataStore` is the single in-memory source of truth for OHLCV history
per (symbol, timeframe) while the bot runs: seeded via REST backfill when a
symbol first comes under watch, kept current by the WebSocket kline stream,
and always exposes indicators computed only up to the most recently
*closed* candle. The currently-forming candle is tracked separately
(`forming_candle`) and never feeds `latest_snapshot()` - trading decisions
must never be made on an unclosed bar (see project rule in the scheduler
section of the spec).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from config.settings import IndicatorsConfig
from exchange.binance_client import BinanceClient
from market.indicators import compute_all_indicators
from utils.time import Timeframe, utcnow

logger = logging.getLogger(__name__)

MAX_HISTORY_BARS = 500
MIN_BARS_FOR_INDICATORS = 5


@dataclass
class IndicatorSnapshot:
    """Latest closed-bar values for one (symbol, timeframe) - what
    SignalEngine actually reads. A flat, explicit dataclass (rather than a
    raw DataFrame row) so tests can build one by hand without pandas."""

    symbol: str
    timeframe: Timeframe
    open_time: Any
    close: float
    open: float
    high: float
    low: float
    volume: float
    rsi: float
    rsi_prev: float
    rsi_slope: float
    rsi_reversal: bool
    rsi_bullish_divergence: bool
    macd: float
    macd_signal: float
    macd_hist: float
    macd_hist_prev: float
    macd_bullish: bool
    ema_fast: float
    ema_mid: float
    ema_slow: float
    ema_trend_ok: bool
    bb_upper: float
    bb_mid: float
    bb_lower: float
    bb_recovery: bool
    bb_squeeze: bool
    atr: float
    distance_from_ema_fast_atr: float
    adx: float
    plus_di: float
    minus_di: float
    volume_ma: float
    abnormal_volume: bool
    volume_confirmation: bool
    obv: float
    obv_prev: float
    vwap: float
    vwap_recovery: bool
    swing_low: bool
    swing_high: bool
    higher_low_structure: bool
    higher_high_structure: bool
    support: float
    distance_from_support_pct: float
    candle_pattern_bullish: bool
    market_structure_bullish: bool

    @classmethod
    def from_dataframe_row(
        cls, symbol: str, timeframe: Timeframe, df: pd.DataFrame, index: int = -1
    ) -> IndicatorSnapshot:
        row = df.iloc[index]
        prev = df.iloc[index - 1] if len(df) > 1 and (index == -1 or index > 0) else row

        def get(name: str, default: float = 0.0) -> Any:
            if name.endswith("_prev"):
                base, source = name[: -len("_prev")], prev
            else:
                base, source = name, row
            value = source[base] if base in source.index else default
            return default if pd.isna(value) else value

        return cls.from_getter(symbol, timeframe, get, open_time=row["open_time"])

    @classmethod
    def from_getter(
        cls, symbol: str, timeframe: Timeframe, get: Callable[..., Any], *, open_time: Any
    ) -> IndicatorSnapshot:
        """Shared field mapping used both for live/paper (one DataFrame, a
        "previous row") and backtesting (one `pd.merge_asof`-aligned row
        with precomputed `*_prev` columns per timeframe) - a single
        authoritative list of fields/defaults for both code paths.

        `get(name, default=0.0)` resolves one field; callers decide where a
        name (including a `*_prev` variant) actually comes from.
        """
        return cls(
            symbol=symbol,
            timeframe=timeframe,
            open_time=open_time,
            close=float(get("close")), open=float(get("open")), high=float(get("high")),
            low=float(get("low")), volume=float(get("volume")),
            rsi=float(get("rsi", 50.0)), rsi_prev=float(get("rsi_prev", 50.0)), rsi_slope=float(get("rsi_slope")),
            rsi_reversal=bool(get("rsi_reversal", False)),
            rsi_bullish_divergence=bool(get("rsi_bullish_divergence", False)),
            macd=float(get("macd")), macd_signal=float(get("macd_signal")),
            macd_hist=float(get("macd_hist")), macd_hist_prev=float(get("macd_hist_prev")),
            macd_bullish=bool(get("macd_bullish", False)),
            ema_fast=float(get("ema_fast", get("close"))), ema_mid=float(get("ema_mid", get("close"))),
            ema_slow=float(get("ema_slow", get("close"))), ema_trend_ok=bool(get("ema_trend_ok", False)),
            bb_upper=float(get("bb_upper", get("close"))), bb_mid=float(get("bb_mid", get("close"))),
            bb_lower=float(get("bb_lower", get("close"))),
            bb_recovery=bool(get("bb_recovery", False)), bb_squeeze=bool(get("bb_squeeze", False)),
            atr=float(get("atr", 0.0)), distance_from_ema_fast_atr=float(get("distance_from_ema_fast_atr", 0.0)),
            adx=float(get("adx", 0.0)), plus_di=float(get("plus_di", 0.0)), minus_di=float(get("minus_di", 0.0)),
            volume_ma=float(get("volume_ma", get("volume"))), abnormal_volume=bool(get("abnormal_volume", False)),
            volume_confirmation=bool(get("volume_confirmation", False)),
            obv=float(get("obv", 0.0)), obv_prev=float(get("obv_prev", 0.0)),
            vwap=float(get("vwap", get("close"))), vwap_recovery=bool(get("vwap_recovery", False)),
            swing_low=bool(get("swing_low", False)), swing_high=bool(get("swing_high", False)),
            higher_low_structure=bool(get("higher_low_structure", False)),
            higher_high_structure=bool(get("higher_high_structure", False)),
            support=float(get("support", get("low"))),
            distance_from_support_pct=float(get("distance_from_support_pct", 0.0)),
            candle_pattern_bullish=bool(get("candle_pattern_bullish", False)),
            market_structure_bullish=bool(get("market_structure_bullish", False)),
        )


class SymbolTimeframeSeries:
    def __init__(self, symbol: str, timeframe: Timeframe, indicators_config: IndicatorsConfig) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self._config = indicators_config
        self._raw = pd.DataFrame(columns=["open_time", "open", "high", "low", "close", "volume", "close_time"])
        self._computed: pd.DataFrame | None = None
        self._dirty = True
        self.forming_candle: dict[str, Any] | None = None
        self.last_update: Any = None

    def seed(self, df: pd.DataFrame) -> None:
        self._raw = (
            df.sort_values("open_time").drop_duplicates("open_time").tail(MAX_HISTORY_BARS).reset_index(drop=True)
        )
        self._dirty = True
        self.last_update = utcnow()

    def apply_kline_event(self, k: dict[str, Any]) -> bool:
        """Apply one Binance kline WS payload's `k` object. Returns True iff
        this event closed a candle (callers use this to trigger analysis
        exactly on candle close, never mid-bar)."""
        open_time = pd.Timestamp(int(k["t"]), unit="ms", tz="UTC")
        row = {
            "open_time": open_time, "open": float(k["o"]), "high": float(k["h"]),
            "low": float(k["l"]), "close": float(k["c"]), "volume": float(k["v"]),
            "close_time": pd.Timestamp(int(k["T"]), unit="ms", tz="UTC"),
        }
        self.last_update = utcnow()
        if not bool(k.get("x", False)):
            self.forming_candle = row
            return False

        self.forming_candle = None
        if not self._raw.empty:
            self._raw = self._raw[self._raw["open_time"] != open_time]
        self._raw = pd.concat([self._raw, pd.DataFrame([row])], ignore_index=True)
        self._raw = self._raw.sort_values("open_time").tail(MAX_HISTORY_BARS).reset_index(drop=True)
        self._dirty = True
        return True

    def _ensure_computed(self) -> pd.DataFrame:
        if self._dirty or self._computed is None:
            if len(self._raw) < MIN_BARS_FOR_INDICATORS:
                self._computed = self._raw.copy()
            else:
                self._computed = compute_all_indicators(self._raw, self._config)
            self._dirty = False
        return self._computed

    def latest_snapshot(self) -> IndicatorSnapshot | None:
        computed = self._ensure_computed()
        if computed.empty:
            return None
        return IndicatorSnapshot.from_dataframe_row(self.symbol, self.timeframe, computed, index=-1)

    def has_enough_history(self, min_bars: int) -> bool:
        return len(self._raw) >= min_bars

    def is_stale(self, max_age_seconds: float) -> bool:
        if self.last_update is None:
            return True
        return (utcnow() - self.last_update).total_seconds() > max_age_seconds

    @property
    def dataframe(self) -> pd.DataFrame:
        return self._ensure_computed()


class MarketDataStore:
    def __init__(self, binance_client: BinanceClient, indicators_config: IndicatorsConfig) -> None:
        self._client = binance_client
        self._config = indicators_config
        self._series: dict[tuple[str, Timeframe], SymbolTimeframeSeries] = {}

    def _get_or_create(self, symbol: str, timeframe: Timeframe) -> SymbolTimeframeSeries:
        key = (symbol, timeframe)
        if key not in self._series:
            self._series[key] = SymbolTimeframeSeries(symbol, timeframe, self._config)
        return self._series[key]

    async def backfill(self, symbol: str, timeframe: Timeframe, *, limit: int = 300) -> None:
        df = await self._client.get_klines(symbol, timeframe.value, limit=limit)
        self._get_or_create(symbol, timeframe).seed(df)
        logger.info("Backfilled %s %s with %s candles", symbol, timeframe.value, len(df))

    def apply_kline_message(self, payload: dict[str, Any]) -> tuple[str, Timeframe, bool] | None:
        """`payload` is one Binance kline event, already unwrapped from the
        `{"stream": ..., "data": ...}` multiplex envelope."""
        k = payload.get("k")
        if not k:
            return None
        symbol = payload.get("s") or k.get("s")
        try:
            timeframe = Timeframe(k.get("i"))
        except ValueError:
            return None
        closed = self._get_or_create(symbol, timeframe).apply_kline_event(k)
        return symbol, timeframe, closed

    def snapshot(self, symbol: str, timeframe: Timeframe) -> IndicatorSnapshot | None:
        series = self._series.get((symbol, timeframe))
        return series.latest_snapshot() if series else None

    def dataframe(self, symbol: str, timeframe: Timeframe) -> pd.DataFrame | None:
        series = self._series.get((symbol, timeframe))
        return series.dataframe if series else None

    def has_series(self, symbol: str, timeframe: Timeframe) -> bool:
        return (symbol, timeframe) in self._series

    def has_enough_history(self, symbol: str, timeframe: Timeframe, min_bars: int) -> bool:
        series = self._series.get((symbol, timeframe))
        return bool(series and series.has_enough_history(min_bars))

    def is_stale(self, symbol: str, timeframe: Timeframe, max_age_seconds: float) -> bool:
        series = self._series.get((symbol, timeframe))
        return series.is_stale(max_age_seconds) if series else True

    def tracked_keys(self) -> list[tuple[str, Timeframe]]:
        return list(self._series.keys())
