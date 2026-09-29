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


class _SkewedRaw:
    """Rejects the first signed request with -1021 (stale clock offset, as
    after a Windows time sync), then accepts once the offset is resynced."""

    def __init__(self) -> None:
        self.timestamp_offset = -30_000  # measured at startup, now wrong
        self.calls = 0

    async def get_server_time(self):
        import time
        return {"serverTime": int(time.time() * 1000)}

    async def _signed(self):
        from binance.exceptions import BinanceAPIException

        self.calls += 1
        if abs(self.timestamp_offset) > 10_000:
            raise BinanceAPIException(None, 400, '{"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."}')
        return {"ok": True}

    async def get_account(self, **kwargs):
        return await self._signed()

    async def create_order(self, **kwargs):
        return await self._signed()


def test_clock_skew_rejection_resyncs_the_offset_and_retries():
    raw = _SkewedRaw()
    client = _client_with(raw)  # type: ignore[arg-type]

    balances = asyncio.run(client.get_account_balances())

    assert balances == {}
    assert raw.calls == 2
    assert abs(raw.timestamp_offset) < 1_000


def test_order_rejected_for_clock_skew_is_safely_resent_after_resync():
    """-1021 is rejected before the matching engine sees the order, so unlike
    a timeout it is safe to resend - otherwise every order would fail until
    a restart."""
    raw = _SkewedRaw()

    result = asyncio.run(_client_with(raw).create_order(symbol="SOLUSDT", side="BUY", type="MARKET"))  # type: ignore[arg-type]

    assert result == {"ok": True}
    assert raw.calls == 2


def test_read_only_calls_still_retry_transient_failures(monkeypatch):
    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("exchange.binance_client.asyncio.sleep", no_sleep)
    raw = _CountingRaw()

    result = asyncio.run(_client_with(raw).get_order_status("SOLUSDT", orig_client_order_id="c1"))

    assert result == {"status": "FILLED"}
    assert raw.status_calls == 2
