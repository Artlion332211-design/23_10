"""Binance market-data & user-data WebSocket streams.

Wraps `binance.BinanceSocketManager`. The underlying library already retries
transport-level drops internally (`ReconnectingWebsocket` /
`KeepAliveWebsocket`, which also renews the user-data-stream `listenKey`),
but it gives up after a bounded number of attempts. This module adds an
outer supervisor loop that, if a stream's internal retry budget is
exhausted, tears the connection down and opens a brand new one from
scratch with its own exponential backoff - so a prolonged outage degrades
to slow reconnect attempts instead of silently dying.

Callbacks should do minimal work (update an in-memory cache, push to a
queue) since they run inline in the read loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from binance import BinanceSocketManager

from exchange.binance_client import BinanceClient

logger = logging.getLogger(__name__)

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]
_BACKOFF_STEPS = (2, 5, 10, 30, 60)


class ReconnectingStream:
    """Runs one socket-manager stream forever, reconnecting with backoff on
    any drop, until `.stop()` is called."""

    def __init__(self, name: str, socket_factory: Callable[[], Any], on_message: MessageHandler) -> None:
        self._name = name
        self._socket_factory = socket_factory
        self._on_message = on_message
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.last_message_at: float = 0.0
        self.connected: bool = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"ws:{self._name}")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        attempt = 0
        while not self._stop_event.is_set():
            try:
                socket = self._socket_factory()
                async with socket as stream:
                    logger.info("WebSocket connected: %s", self._name)
                    self.connected = True
                    attempt = 0
                    while not self._stop_event.is_set():
                        msg = await stream.recv()
                        self.last_message_at = time.monotonic()
                        if msg is None:
                            continue
                        if isinstance(msg, dict) and msg.get("e") == "error":
                            raise RuntimeError(f"Stream error payload on {self._name}: {msg}")
                        await self._on_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                if self._stop_event.is_set():
                    break
                delay = _BACKOFF_STEPS[min(attempt, len(_BACKOFF_STEPS) - 1)]
                logger.warning("WebSocket %s dropped (%r); reconnecting in %ss", self._name, exc, delay)
                attempt += 1
                await asyncio.sleep(delay)
        self.connected = False


class WebSocketManager:
    def __init__(self, binance_client: BinanceClient) -> None:
        self._client = binance_client
        self._bsm = BinanceSocketManager(binance_client.raw)
        self._streams: dict[str, ReconnectingStream] = {}

    def start_kline_stream(self, symbols_intervals: list[tuple[str, str]], on_message: MessageHandler) -> None:
        """One multiplexed connection covering every (symbol, interval) pair -
        keeps us well under Binance's per-IP WebSocket connection limits even
        when tracking dozens of candidates across three timeframes."""
        names = [f"{symbol.lower()}@kline_{interval}" for symbol, interval in symbols_intervals]

        async def _handle(msg: dict[str, Any]) -> None:
            await on_message(msg.get("data", msg))

        stream = ReconnectingStream("klines", lambda: self._bsm.multiplex_socket(names), _handle)
        self._streams["klines"] = stream
        stream.start()

    def start_user_stream(self, on_event: MessageHandler) -> None:
        stream = ReconnectingStream("user_data", self._bsm.user_socket, on_event)
        self._streams["user_data"] = stream
        stream.start()

    def is_connected(self, name: str) -> bool:
        stream = self._streams.get(name)
        return bool(stream and stream.connected)

    def last_message_age_seconds(self, name: str) -> float | None:
        stream = self._streams.get(name)
        if stream is None or stream.last_message_at == 0.0:
            return None
        return time.monotonic() - stream.last_message_at

    async def stop_stream(self, name: str) -> None:
        stream = self._streams.pop(name, None)
        if stream is not None:
            await stream.stop()

    async def stop_all(self) -> None:
        await asyncio.gather(*(s.stop() for s in self._streams.values()), return_exceptions=True)
        self._streams.clear()
