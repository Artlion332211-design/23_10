from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pandas as pd

from market.market_data import MarketDataStore
from utils.time import Timeframe, utcnow


def test_backfill_drops_the_still_forming_candle(rules):
    """Binance REST klines end with the in-progress bar; seeding it would
    freeze a partial candle in as the latest *closed* one until the next
    close (up to 4h on the 4h series)."""
    now = pd.Timestamp(utcnow()).floor("15min")
    closed_open = now - timedelta(minutes=15)
    df = pd.DataFrame({
        "open_time": [closed_open, now],
        "open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0], "close": [1.0, 2.0], "volume": [1.0, 1.0],
        "close_time": [now - timedelta(milliseconds=1), now + timedelta(minutes=15) - timedelta(milliseconds=1)],
    })
    client = MagicMock()
    client.get_klines = AsyncMock(return_value=df)
    store = MarketDataStore(client, rules.indicators)

    asyncio.run(store.backfill("SOLUSDT", Timeframe.M15))

    frame = store.dataframe("SOLUSDT", Timeframe.M15)
    assert frame is not None
    assert list(frame["close"]) == [1.0]


def test_live_price_is_the_forming_candle_not_the_last_close(rules):
    store = MarketDataStore(MagicMock(), rules.indicators)
    t = int(pd.Timestamp(utcnow()).floor("15min").timestamp() * 1000)
    store.apply_kline_message({"s": "SOLUSDT", "k": {
        "t": t, "T": t + 899_999, "i": "15m", "o": "100", "h": "101", "l": "99", "c": "100.5", "v": "10", "x": False,
    }})

    assert store.live_price("SOLUSDT") == 100.5
    assert store.live_price("ETHUSDT") is None
