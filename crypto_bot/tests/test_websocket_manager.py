from __future__ import annotations

import asyncio

from exchange.websocket_manager import ReconnectingStream


class _SilentSocket:
    """A connected socket that never delivers anything (half-open)."""

    def __init__(self, messages=None) -> None:
        self._messages = list(messages or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def recv(self):
        if self._messages:
            return self._messages.pop(0)
        await asyncio.Event().wait()  # blocks forever


def test_silent_kline_socket_is_reconnected_after_the_inactivity_timeout(monkeypatch):
    """A half-open socket never raises, so without an inactivity timeout the
    bot would stay blind to prices (no entries, no DCA re-analysis) forever."""
    monkeypatch.setattr("exchange.websocket_manager._BACKOFF_STEPS", (0,))
    connects = 0

    def factory():
        nonlocal connects
        connects += 1
        return _SilentSocket()

    async def handler(msg):
        return None

    async def scenario():
        stream = ReconnectingStream("klines", factory, handler, inactivity_timeout=0.05)
        stream.start()
        await asyncio.sleep(0.3)
        await stream.stop()

    asyncio.run(scenario())

    assert connects >= 2


def test_error_payload_does_not_count_as_fresh_data(monkeypatch):
    monkeypatch.setattr("exchange.websocket_manager._BACKOFF_STEPS", (0,))
    received = []

    def factory():
        return _SilentSocket([{"e": "error", "m": "Max reconnections reached"}])

    async def handler(msg):
        received.append(msg)

    async def scenario():
        stream = ReconnectingStream("klines", factory, handler)
        stream.start()
        await asyncio.sleep(0.05)
        await stream.stop()
        return stream

    stream = asyncio.run(scenario())

    assert received == []
    assert stream.last_message_at == 0.0


def test_disconnected_since_is_kept_across_failed_reconnect_attempts(monkeypatch):
    """Otherwise every failed retry would restart the outage clock and a
    long outage would never exceed the status grace period."""
    monkeypatch.setattr("exchange.websocket_manager._BACKOFF_STEPS", (0,))

    def factory():
        raise OSError("getaddrinfo failed")

    async def handler(msg):
        return None

    async def scenario():
        stream = ReconnectingStream("klines", factory, handler)
        first = stream.disconnected_since
        stream.start()
        await asyncio.sleep(0.05)
        await stream.stop()
        return first, stream.disconnected_since

    first, after = asyncio.run(scenario())

    assert after == first
