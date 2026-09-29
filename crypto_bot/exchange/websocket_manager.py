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
# python-binance's default of 100 is roughly one second of traffic for a
# multiplexed kline stream over ~25 symbols x 3 timeframes; overflowing it
# closes the connection. Headroom for a brief stall, not a license for slow
# callbacks.
_MAX_QUEUE_SIZE = 2000
# Last-resort only: the websockets keepalive already detects a dead/half-open
# connection within ~40s, and python-binance then reconnects on its own (up
# to 5 attempts). This fires only if the feed is still silent after that -
# long enough not to cut in on the library's own reconnect attempts.
_KLINE_INACTIVITY_SECONDS = 300.0
# Error payloads after which python-binance keeps its read loop alive and
# reconnects by itself (see ReconnectingWebsocket._read_loop). Tearing the
# socket down on these instead races that reconnect: __aexit__ waits for a
# read loop that a successful reconnect has just revived, nobody consumes
# the queue, and the feed stays dead until the queue overflows - seen live
# 19:53-19:55 on 2026-09-29. Any other error type means the read loop has
# stopped, so tearing down (and reconnecting from scratch) is correct.
_LIBRARY_RECONNECTS_ITSELF = frozenset(
    {"IncompleteReadError", "gaierror", "ConnectionClosedError", "ConnectionClosedOK", "BinanceWebsocketClosed"}
)


class ReconnectingStream:
    """Runs one socket-manager stream forever, reconnecting with backoff on
    any drop, until `.stop()` is called."""

    def __init__(
        self,
        name: str,
        socket_factory: Callable[[], Any],
        on_message: MessageHandler,
        *,
        inactivity_timeout: float | None = None,
    ) -> None:
        self._name = name
        self._socket_factory = socket_factory
        self._on_message = on_message
        # Force a reconnect after this long without a single message. Only for
        # streams that tick constantly (klines): a half-open socket otherwise
        # never errors, and the bot would stay blind to prices indefinitely.
        # Never for the user-data stream, which is legitimately silent.
        self._inactivity_timeout = inactivity_timeout
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.last_message_at: float = 0.0
        self.connected: bool = False
        # When the stream last went from connected to not (or was created) -
        # lets callers tell a brief planned reconnect from a real outage.
        self.disconnected_since: float = time.monotonic()

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
                    connected_at = time.monotonic()
                    while not self._stop_event.is_set():
                        msg = await self._recv(stream)
                        if msg is None:
                            self._check_inactivity(connected_at)
                            continue
                        # An error payload is not market data - it must not
                        # make a dead feed look fresh.
                        if isinstance(msg, dict) and msg.get("e") == "error":
                            if msg.get("type") in _LIBRARY_RECONNECTS_ITSELF:
                                logger.warning(
                                    "WebSocket %s interrupted (%s); library is reconnecting", self._name, msg.get("type")
                                )
                                self._mark_disconnected()
                                continue
                            raise RuntimeError(f"Stream error payload on {self._name}: {msg}")
                        self.last_message_at = time.monotonic()
                        if not self.connected:
                            self.connected = True
                            logger.info("WebSocket %s receiving data again", self._name)
                        await self._on_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._mark_disconnected()
                if self._stop_event.is_set():
                    break
                delay = _BACKOFF_STEPS[min(attempt, len(_BACKOFF_STEPS) - 1)]
                logger.warning("WebSocket %s dropped (%r); reconnecting in %ss", self._name, exc, delay)
                attempt += 1
                await asyncio.sleep(delay)
        self._mark_disconnected()

    async def _recv(self, stream: Any) -> Any:
        if self._inactivity_timeout is None:
            return await stream.recv()
        try:
            return await asyncio.wait_for(stream.recv(), timeout=self._inactivity_timeout)
        except TimeoutError:
            return None

    def _check_inactivity(self, connected_at: float) -> None:
        if self._inactivity_timeout is None:
            return
        silent_for = time.monotonic() - max(self.last_message_at, connected_at)
        if silent_for > self._inactivity_timeout:
            raise TimeoutError(f"no messages on {self._name} for {silent_for:.0f}s - forcing reconnect")

    def _mark_disconnected(self) -> None:
        if self.connected:
            self.disconnected_since = time.monotonic()
        self.connected = False


class WebSocketManager:
    def __init__(self, binance_client: BinanceClient) -> None:
        self._client = binance_client
        self._bsm = BinanceSocketManager(binance_client.raw, max_queue_size=_MAX_QUEUE_SIZE)
        self._streams: dict[str, ReconnectingStream] = {}

    def start_kline_stream(self, symbols_intervals: list[tuple[str, str]], on_message: MessageHandler) -> None:
        """One multiplexed connection covering every (symbol, interval) pair -
        keeps us well under Binance's per-IP WebSocket connection limits even
        when tracking dozens of candidates across three timeframes."""
        names = [f"{symbol.lower()}@kline_{interval}" for symbol, interval in symbols_intervals]

        async def _handle(msg: dict[str, Any]) -> None:
            await on_message(msg.get("data", msg))

        stream = ReconnectingStream(
            "klines", lambda: self._bsm.multiplex_socket(names), _handle, inactivity_timeout=_KLINE_INACTIVITY_SECONDS
        )
        self._streams["klines"] = stream
        stream.start()

    def start_user_stream(self, on_event: MessageHandler) -> None:
        stream = ReconnectingStream("user_data", self._bsm.user_socket, on_event)
        self._streams["user_data"] = stream
        stream.start()

    def is_connected(self, name: str) -> bool:
        stream = self._streams.get(name)
        return bool(stream and stream.connected)

    def seconds_disconnected(self, name: str) -> float | None:
        """How long the named stream has been without a connection; None if
        it's connected or not started (e.g. mid-rescan swap)."""
        stream = self._streams.get(name)
        if stream is None or stream.connected:
            return None
        return time.monotonic() - stream.disconnected_since

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
