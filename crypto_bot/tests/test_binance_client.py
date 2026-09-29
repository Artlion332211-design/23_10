from __future__ import annotations

import asyncio

import pytest

from exchange.binance_client import BinanceClient


class _CountingRaw:
    def __init__(self) -> None:
        self.create_calls = 0
        self.status_calls = 0

    async def create_order(self, **kwargs):
        self.create_calls += 1
        raise TimeoutError("response lost")

    async def get_order(self, **kwargs):
        self.status_calls += 1
        if self.status_calls < 2:
            raise TimeoutError("transient")
        return {"status": "FILLED"}


def _client_with(raw: _CountingRaw) -> BinanceClient:
    client = BinanceClient("k", "s", testnet=True)
    client._client = raw  # type: ignore[assignment]
    return client


def test_create_order_is_never_blindly_resent_after_an_ambiguous_failure():
    """A timed-out order may already have filled on Binance, and Binance
    accepts a reused newClientOrderId once the first order filled - so a
    retry could buy twice. The failure must surface after one attempt."""
    raw = _CountingRaw()

    with pytest.raises(TimeoutError):
        asyncio.run(_client_with(raw).create_order(symbol="SOLUSDT", side="BUY", type="MARKET"))

    assert raw.create_calls == 1


def test_read_only_calls_still_retry_transient_failures(monkeypatch):
    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("exchange.binance_client.asyncio.sleep", no_sleep)
    raw = _CountingRaw()

    result = asyncio.run(_client_with(raw).get_order_status("SOLUSDT", orig_client_order_id="c1"))

    assert result == {"status": "FILLED"}
    assert raw.status_calls == 2
