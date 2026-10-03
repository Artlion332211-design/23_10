"""Keeps idle USDT in Binance Simple Earn Flexible and brings it back to
spot before a buy needs it (owner decision 2026-10-03).

Most of the account sits idle: the 35% exposure cap keeps two thirds of
the money in USDT at all times. Flexible Earn pays interest on it and can
be redeemed to spot at any moment, so the bot keeps a spot buffer
(EARN_SPOT_BUFFER_USDT, larger than any single entry or DCA order) and the
half-hourly `sweep` keeps spot near it in both directions. A buy that still
finds spot short redeems the shortfall plus a fresh buffer first
(`ensure_spot`).

The trading balance the risk caps are measured against is spot + Earn
(`read_balances`, both read under the transfer lock so a sweep in between
can't count the same money twice), so moving money into Earn doesn't shrink
the bot.

Each Earn list/position call costs 150 request weight, so the balance and
the product are cached. Earn reads are not retried and a failed read is
remembered for a few minutes: when Binance's /sapi side is down the bot
falls back to spot quickly instead of stalling its trading loops.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_CENT = Decimal("0.01")
_BALANCE_TTL_SECONDS = 60.0
_FAILED_READ_TTL_SECONDS = 300.0
_PRODUCT_TTL_SECONDS = 3600.0
_REDEEM_WAIT_SECONDS = 10
# After a buy asked for money the sweep leaves spot alone this long, so it
# can't move into Earn what the buy is about to spend.
_BUY_HOLD_SECONDS = 120.0


class EarnClient(Protocol):
    async def get_flexible_earn_product(self, asset: str) -> dict[str, Any] | None: ...
    async def get_flexible_earn_position(self, asset: str) -> dict[str, Any] | None: ...
    async def subscribe_flexible_earn(self, product_id: str, amount: Decimal) -> dict[str, Any]: ...
    async def redeem_flexible_earn(self, product_id: str, amount: Decimal | None) -> dict[str, Any]: ...
    async def get_account_balances(self) -> dict[str, tuple[Decimal, Decimal]]: ...


@dataclass(frozen=True)
class EarnProduct:
    product_id: str
    apr: float  # e.g. 0.0265 = 2.65% a year
    min_purchase: Decimal
    can_purchase: bool
    can_redeem: bool


class EarnError(RuntimeError):
    pass


class EarnManager:
    def __init__(
        self,
        client: EarnClient,
        *,
        spot_buffer_usdt: Decimal,
        min_transfer_usdt: Decimal,
        asset: str = "USDT",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._spot_buffer = spot_buffer_usdt
        self._min_transfer = min_transfer_usdt
        self._asset = asset
        self._sleep = sleep
        self._clock = clock
        # One transfer or balance read at a time: a sweep must not move the
        # money a buy has just redeemed, nor change balances mid-read.
        self._lock = asyncio.Lock()
        self._balance: tuple[float, Decimal] | None = None  # (fetched at, amount)
        self._failed_read_at: float | None = None
        self._product: tuple[float, EarnProduct] | None = None
        self._hold_until = 0.0

    async def product(self) -> EarnProduct:
        if self._product is not None and self._clock() - self._product[0] < _PRODUCT_TTL_SECONDS:
            return self._product[1]
        raw = await self._client.get_flexible_earn_product(self._asset)
        if raw is None:
            raise EarnError(f"no Simple Earn Flexible product for {self._asset}")
        product = EarnProduct(
            product_id=str(raw["productId"]),
            apr=float(raw.get("latestAnnualPercentageRate") or 0),
            min_purchase=Decimal(str(raw.get("minPurchaseAmount") or "0")),
            can_purchase=bool(raw.get("canPurchase", False)) and not bool(raw.get("isSoldOut", False)),
            can_redeem=bool(raw.get("canRedeem", False)),
        )
        self._product = (self._clock(), product)
        return product

    async def balance(self, *, fresh: bool = False) -> Decimal:
        """USDT held in Flexible Earn (cached for a minute; a failed read is
        re-raised without asking Binance again for a few minutes)."""
        now = self._clock()
        if not fresh and self._balance is not None and now - self._balance[0] < _BALANCE_TTL_SECONDS:
            return self._balance[1]
        if not fresh and self._failed_read_at is not None and now - self._failed_read_at < _FAILED_READ_TTL_SECONDS:
            raise EarnError("Simple Earn balance unavailable (recent read failed)")
        try:
            raw = await self._client.get_flexible_earn_position(self._asset)
        except Exception:
            self._failed_read_at = self._clock()
            raise
        self._failed_read_at = None
        amount = Decimal(str(raw.get("totalAmount") or "0")) if raw is not None else Decimal("0")
        self._balance = (self._clock(), amount)
        return amount

    async def read_balances(self) -> tuple[Decimal, Decimal | None]:
        """(free spot USDT, USDT in Earn or None if Earn can't be read). A
        failed spot read raises, like the plain spot balance always did."""
        async with self._lock:
            spot = await self._spot_free()
            try:
                return spot, await self.balance()
            except Exception as exc:  # noqa: BLE001 - the caller falls back to spot only
                logger.warning("Simple Earn balance unavailable: %r", exc)
                return spot, None

    async def _spot_free(self) -> Decimal:
        balances = await self._client.get_account_balances()
        return balances.get(self._asset, (Decimal("0"), Decimal("0")))[0]

    async def _redeem(self, product: EarnProduct, amount: Decimal, held: Decimal) -> Decimal:
        """Redeems `amount` (everything if that is most of what Earn holds); returns what was asked for."""
        redeem_all = amount >= held
        try:
            await self._client.redeem_flexible_earn(product.product_id, None if redeem_all else amount)
        finally:
            self._balance = None  # even after an ambiguous failure: the money may have moved
        return held if redeem_all else amount

    async def ensure_spot(self, needed_usdt: Decimal) -> bool:
        """Makes sure spot holds at least `needed_usdt` free. When it doesn't,
        redeems the shortfall plus a fresh spot buffer, so the next buys
        don't each wait for a redemption. False if even spot + Earn can't
        cover it or the money didn't arrive in time."""
        async with self._lock:
            self._hold_until = self._clock() + _BUY_HOLD_SECONDS
            spot = await self._spot_free()
            if spot >= needed_usdt:
                return True
            held = await self.balance(fresh=True)
            if spot + held < needed_usdt:
                logger.warning("Earn: %s needed, only %s on spot + %s in Earn", needed_usdt, spot, held)
                return False
            product = await self.product()
            if not product.can_redeem:
                raise EarnError("Binance doesn't allow redeeming the Flexible product right now")
            amount = (needed_usdt + self._spot_buffer - spot).quantize(_CENT, rounding=ROUND_UP)
            asked = await self._redeem(product, amount, held)
            logger.info("Earn: redeemed %s USDT to spot (spot %s, needed %s)", asked, spot, needed_usdt)
            for _ in range(_REDEEM_WAIT_SECONDS):
                await self._sleep(1)
                if await self._spot_free() >= needed_usdt:
                    self._hold_until = self._clock() + _BUY_HOLD_SECONDS
                    return True
            logger.warning("Earn: redeemed money not on spot after %ss", _REDEEM_WAIT_SECONDS)
            return False

    async def sweep(self) -> Decimal:
        """Keeps spot near the buffer: moves the surplus into Earn, or tops a
        spent buffer back up from Earn. Returns the amount moved into Earn
        (negative = taken out of Earn, 0 = nothing to do)."""
        async with self._lock:
            if self._clock() < self._hold_until:
                return Decimal("0")  # a buy is about to spend what's on spot
            spot = await self._spot_free()
            surplus = (spot - self._spot_buffer).quantize(_CENT, rounding=ROUND_DOWN)
            if surplus >= self._min_transfer:
                product = await self.product()
                if not product.can_purchase or surplus < product.min_purchase:
                    return Decimal("0")
                try:
                    await self._client.subscribe_flexible_earn(product.product_id, surplus)
                finally:
                    self._balance = None
                logger.info("Earn: moved %s USDT from spot into Flexible Earn", surplus)
                return surplus
            if -surplus >= self._min_transfer:
                held = await self.balance(fresh=True)
                product = await self.product()
                if held < self._min_transfer or not product.can_redeem:
                    return Decimal("0")
                asked = await self._redeem(product, min(-surplus, held), held)
                logger.info("Earn: topped the spot buffer up with %s USDT from Earn", asked)
                return -asked
            return Decimal("0")
