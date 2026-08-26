"""UTC time helpers and timeframe/candle-close arithmetic.

All strategy decisions are made on *closed* candles only (see project rule:
"Не приймати індикаторні рішення на незакритій свічці"). These helpers give a
single, well-tested source of truth for "what is the current candle" and
"has it closed yet" so that live trading, paper trading and backtesting all
agree on candle boundaries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import Enum

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    """Attach UTC tzinfo to a naive datetime, or return an aware one unchanged
    (converted to UTC if it carries a different offset)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class Timeframe(str, Enum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def timedelta(self) -> timedelta:
        return _TF_DELTAS[self]

    @property
    def pandas_freq(self) -> str:
        return _TF_PANDAS_FREQ[self]

    @property
    def minutes(self) -> float:
        return self.timedelta.total_seconds() / 60.0


_TF_DELTAS: dict[Timeframe, timedelta] = {
    Timeframe.M1: timedelta(minutes=1),
    Timeframe.M5: timedelta(minutes=5),
    Timeframe.M15: timedelta(minutes=15),
    Timeframe.H1: timedelta(hours=1),
    Timeframe.H4: timedelta(hours=4),
    Timeframe.D1: timedelta(days=1),
}

_TF_PANDAS_FREQ: dict[Timeframe, str] = {
    Timeframe.M1: "1min",
    Timeframe.M5: "5min",
    Timeframe.M15: "15min",
    Timeframe.H1: "1h",
    Timeframe.H4: "4h",
    Timeframe.D1: "1D",
}


def floor_to_timeframe(dt: datetime, timeframe: Timeframe) -> datetime:
    """Return the open time of the candle that contains `dt` (epoch-aligned, UTC)."""
    dt = ensure_utc(dt)
    tf_seconds = timeframe.timedelta.total_seconds()
    elapsed = (dt - _EPOCH).total_seconds()
    floored = (elapsed // tf_seconds) * tf_seconds
    return _EPOCH + timedelta(seconds=floored)


def candle_close_time(open_time: datetime, timeframe: Timeframe) -> datetime:
    return ensure_utc(open_time) + timeframe.timedelta


def is_candle_closed(open_time: datetime, timeframe: Timeframe, now: datetime | None = None) -> bool:
    now = ensure_utc(now) if now else utcnow()
    return now >= candle_close_time(open_time, timeframe)


def seconds_until_next_close(timeframe: Timeframe, now: datetime | None = None) -> float:
    now = ensure_utc(now) if now else utcnow()
    current_open = floor_to_timeframe(now, timeframe)
    close = candle_close_time(current_open, timeframe)
    return (close - now).total_seconds()


def last_closed_open_time(timeframe: Timeframe, now: datetime | None = None) -> datetime:
    """Open time of the most recently *closed* candle (i.e. excludes the one
    currently forming)."""
    now = ensure_utc(now) if now else utcnow()
    current_open = floor_to_timeframe(now, timeframe)
    return current_open - timeframe.timedelta
