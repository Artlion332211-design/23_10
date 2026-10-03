from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from exchange.earn import EarnError, EarnManager


class FakeEarnClient:
    """Spot USDT and a Flexible Earn position that move like Binance's."""

    def __init__(self, *, spot: str = "0", earn: str = "0", redeem_delay_polls: int = 0):
        self.spot = Decimal(spot)
        self.earn = Decimal(earn)
        self.redeem_delay_polls = redeem_delay_polls  # balance reads before redeemed money shows on spot
        self.pending_redeem = Decimal("0")
        self.subscribed: list[Decimal] = []
        self.redeemed: list[Decimal | None] = []
        self.position_calls = 0
        self.can_redeem = True

    async def get_flexible_earn_product(self, asset):
        return {"asset": asset, "productId": "USDT001", "latestAnnualPercentageRate": "0.0265",
                "minPurchaseAmount": "0.01", "canPurchase": True, "canRedeem": self.can_redeem, "isSoldOut": False}

    async def get_flexible_earn_position(self, asset):
        self.position_calls += 1
        return {"asset": asset, "productId": "USDT001", "totalAmount": str(self.earn)} if self.earn > 0 else None

    async def subscribe_flexible_earn(self, product_id, amount):
        assert amount <= self.spot
        self.spot -= amount
        self.earn += amount
        self.subscribed.append(amount)
        return {"purchaseId": 1, "success": True}

    async def redeem_flexible_earn(self, product_id, amount):
        value = self.earn if amount is None else amount
        assert value <= self.earn
        self.earn -= value
        self.pending_redeem += value
        self.redeemed.append(amount)
        return {"redeemId": 1, "success": True}

    async def get_account_balances(self):
        if self.pending_redeem and self.redeem_delay_polls <= 0:
            self.spot += self.pending_redeem
            self.pending_redeem = Decimal("0")
        self.redeem_delay_polls -= 1
        return {"USDT": (self.spot, Decimal("0"))} if self.spot > 0 else {}


async def _no_sleep(_seconds):
    return None


def _manager(client, **kw):
    return EarnManager(client, spot_buffer_usdt=Decimal("150"), min_transfer_usdt=Decimal("10"), sleep=_no_sleep, **kw)


def test_sweep_keeps_the_spot_buffer_and_moves_the_rest_into_earn():
    client = FakeEarnClient(spot="2000.00")

    moved = asyncio.run(_manager(client).sweep())

    assert moved == Decimal("1850.00")
    assert client.spot == Decimal("150.00")
    assert client.earn == Decimal("1850.00")
    assert client.subscribed == [Decimal("1850.00")]


def test_sweep_ignores_small_amounts():
    client = FakeEarnClient(spot="155")
    assert asyncio.run(_manager(client).sweep()) == 0
    assert client.subscribed == []


def test_buy_that_fits_on_spot_touches_nothing():
    client = FakeEarnClient(spot="150", earn="900")
    assert asyncio.run(_manager(client).ensure_spot(Decimal("50"))) is True
    assert client.redeemed == []


def test_buy_short_on_spot_redeems_the_shortfall_plus_a_fresh_buffer_and_waits_for_it():
    """Redeeming only the shortfall left spot near zero, so every later buy
    waited for its own redemption (review 2026-10-03)."""
    client = FakeEarnClient(spot="30", earn="900", redeem_delay_polls=3)

    assert asyncio.run(_manager(client).ensure_spot(Decimal("75"))) is True

    assert client.redeemed == [Decimal("195.00")]  # 75 needed + 150 buffer - 30 on spot
    assert client.spot == Decimal("225.00")


def test_redeems_everything_when_the_shortfall_is_most_of_earn():
    client = FakeEarnClient(spot="40", earn="20")
    assert asyncio.run(_manager(client).ensure_spot(Decimal("60"))) is True
    assert client.redeemed == [None]  # redeemAll


def test_buy_that_spot_plus_earn_cannot_cover_redeems_nothing():
    client = FakeEarnClient(spot="10", earn="20")
    assert asyncio.run(_manager(client).ensure_spot(Decimal("50"))) is False
    assert client.redeemed == []


def test_redeemed_money_that_never_arrives_fails_the_buy():
    client = FakeEarnClient(spot="0", earn="900", redeem_delay_polls=1000)
    assert asyncio.run(_manager(client).ensure_spot(Decimal("50"))) is False


def test_redeem_refused_by_binance_raises():
    client = FakeEarnClient(spot="0", earn="900")
    client.can_redeem = False
    with pytest.raises(EarnError):
        asyncio.run(_manager(client).ensure_spot(Decimal("50")))


def test_earn_balance_is_cached_for_a_minute():
    """Each position call costs 150 request weight; the position monitor asks every minute."""
    now = [0.0]
    client = FakeEarnClient(earn="500")
    manager = _manager(client, clock=lambda: now[0])

    async def scenario():
        assert await manager.balance() == Decimal("500")
        await manager.balance()
        now[0] = 61.0
        await manager.balance()

    asyncio.run(scenario())
    assert client.position_calls == 2


def test_a_failed_earn_read_is_not_retried_for_a_few_minutes():
    """When /sapi is down every caller used to wait for its own timeouts -
    the position monitor's exits ran every ~140 s instead of 60 s."""
    now = [0.0]
    client = FakeEarnClient(earn="500")
    calls = []

    async def down(asset):
        calls.append(asset)
        raise TimeoutError("sapi")

    client.get_flexible_earn_position = down
    manager = _manager(client, clock=lambda: now[0])

    async def scenario():
        for _ in range(3):
            with pytest.raises((TimeoutError, EarnError)):  # the first try, then the remembered failure
                await manager.balance()
        now[0] = 301.0
        with pytest.raises(TimeoutError):
            await manager.balance()

    asyncio.run(scenario())
    assert len(calls) == 2  # once at t=0, once after the 5-minute pause


def test_read_balances_waits_for_a_running_sweep_so_money_is_not_counted_twice():
    """Spot read before a sweep + Earn read after it counted the swept money twice
    and loosened the 35% cap for that evaluation."""
    client = FakeEarnClient(spot="2000", earn="0")
    earn_answer = asyncio.Event()
    real_position = client.get_flexible_earn_position

    async def slow_position(asset):
        await earn_answer.wait()
        return await real_position(asset)

    client.get_flexible_earn_position = slow_position
    manager = _manager(client)

    async def scenario():
        read = asyncio.create_task(manager.read_balances())
        await asyncio.sleep(0)  # spot (2000) is read; the Earn answer is still on its way
        sweep = asyncio.create_task(manager.sweep())
        for _ in range(5):
            await asyncio.sleep(0)  # without the lock the sweep moves 1850 into Earn right here
        earn_answer.set()
        spot, earn = await read
        await sweep
        return spot, earn

    spot, earn = asyncio.run(scenario())
    assert spot + earn == Decimal("2000")
    assert client.earn == Decimal("1850.00")  # the sweep still ran, after the read


def test_read_balances_reports_earn_as_unknown_instead_of_failing():
    client = FakeEarnClient(spot="150", earn="900")

    async def down(asset):
        raise TimeoutError("sapi")

    client.get_flexible_earn_position = down
    assert asyncio.run(_manager(client).read_balances()) == (Decimal("150"), None)


def test_sweep_tops_a_spent_buffer_back_up_from_earn():
    client = FakeEarnClient(spot="20", earn="900")

    moved = asyncio.run(_manager(client).sweep())

    assert moved == Decimal("-130")
    assert client.redeemed == [Decimal("130")]


def test_sweep_leaves_spot_alone_right_after_a_buy_asked_for_money():
    """Otherwise it could move into Earn the money a buy had just redeemed
    and is about to spend."""
    now = [0.0]
    client = FakeEarnClient(spot="400", earn="500")
    manager = _manager(client, clock=lambda: now[0])

    async def scenario():
        assert await manager.ensure_spot(Decimal("300")) is True  # bigger than the buffer, fits on spot
        held = await manager.sweep()
        now[0] = 121.0
        later = await manager.sweep()
        return held, later

    held, later = asyncio.run(scenario())
    assert held == 0 and client.subscribed == [Decimal("250.00")]
    assert later == Decimal("250.00")


def test_earn_calls_are_never_retried_after_an_ambiguous_failure():
    """Reads: fail fast instead of ~80 s of retries per call when /sapi is
    down. Transfers: a resend after a lost response could move money twice."""
    from unittest.mock import AsyncMock

    from exchange.binance_client import BinanceClient

    client = BinanceClient("key", "secret", testnet=False)
    client._call = AsyncMock(return_value={"rows": []})

    async def scenario():
        await client.get_flexible_earn_product("USDT")
        await client.get_flexible_earn_position("USDT")
        await client.subscribe_flexible_earn("USDT001", Decimal("10"))
        await client.redeem_flexible_earn("USDT001", None)

    asyncio.run(scenario())
    assert [c.kwargs["retry_ambiguous"] for c in client._call.await_args_list] == [False] * 4
    assert client._call.await_args_list[2].kwargs["autoSubscribe"] == "false"
    assert client._call.await_args_list[3].kwargs["redeemAll"] == "true"
