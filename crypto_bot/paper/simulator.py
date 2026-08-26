"""Paper trading: a simulated `OrderExecutor` that fills orders against
real, live market prices without ever sending a real order to Binance.

Shares the exact same `ExecutionEngine` (rounding, order-type choice,
DRY_RUN-independent gating, DB persistence, LIMIT timeout/cancel) and the
same `StrategyEngine` / `RiskManager` / DB schema as live trading - only
this adapter differs. That is deliberate: paper mode exists to validate
the *entire* pipeline except real order placement, not a simplified
separate code path that could quietly drift from what LIVE actually does.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal

from config.settings import Settings
from database.models import OrderSide, OrderStatus
from exchange.execution_engine import ExecutionFill, ExecutionResult, OrderRequest
from utils.time import utcnow

logger = logging.getLogger(__name__)

PriceSource = Callable[[str], Awaitable[Decimal]]

_QUOTE_SUFFIXES = ("USDT", "BUSD", "USDC", "USD", "BTC")


def base_asset_of(symbol: str) -> str:
    """Best-effort quote-suffix stripping for paper accounting. Live/real
    trading always gets the true base asset from `exchangeInfo` instead."""
    for suffix in _QUOTE_SUFFIXES:
        if symbol.endswith(suffix) and len(symbol) > len(suffix):
            return symbol[: -len(suffix)]
    return symbol


@dataclass
class PaperAccount:
    usdt_balance: Decimal
    holdings: dict[str, Decimal] = field(default_factory=dict)

    def total_equity(self, mark_prices: dict[str, Decimal]) -> Decimal:
        value = self.usdt_balance
        for asset, qty in self.holdings.items():
            price = mark_prices.get(f"{asset}USDT")
            if price is not None:
                value += qty * price
        return value


class PaperBroker:
    """Implements `exchange.execution_engine.OrderExecutor`.

    MARKET orders fill instantly at the current price (adjusted for
    simulated slippage + commission). LIMIT orders fill instantly if
    already marketable against the current price (at the limit price
    itself - no simulated price improvement); otherwise they rest until a
    later `get_status()` call finds them marketable, or `cancel()` /
    `ExecutionEngine`'s own LIMIT timeout removes them - the exact same
    lifecycle a live resting order goes through.
    """

    def __init__(
        self, settings: Settings, price_source: PriceSource, *, starting_balance: Decimal | None = None
    ) -> None:
        self._settings = settings
        self._price_source = price_source
        self.account = PaperAccount(
            usdt_balance=starting_balance if starting_balance is not None else settings.paper_starting_balance_usdt
        )
        self._resting: dict[str, OrderRequest] = {}

    def get_total_equity(self, mark_prices: dict[str, Decimal]) -> Decimal:
        return self.account.total_equity(mark_prices)

    async def submit(self, request: OrderRequest) -> ExecutionResult:
        current_price = await self._price_source(request.symbol)

        if request.order_type.value == "MARKET":
            return self._fill_market(request, current_price)

        assert request.limit_price is not None
        if self._is_marketable(request, current_price):
            return self._fill_at(request, request.limit_price)

        self._resting[request.client_order_id] = request
        return ExecutionResult(accepted=True, status=OrderStatus.NEW, exchange_order_id=f"paper-{request.client_order_id}")

    async def cancel(self, symbol: str, *, client_order_id: str) -> ExecutionResult:
        self._resting.pop(client_order_id, None)
        return ExecutionResult(accepted=True, status=OrderStatus.CANCELED, exchange_order_id=f"paper-{client_order_id}")

    async def get_status(self, symbol: str, *, client_order_id: str) -> ExecutionResult:
        request = self._resting.get(client_order_id)
        if request is None:
            return ExecutionResult(
                accepted=False, status=OrderStatus.REJECTED, error_message="paper order not found (already resolved)"
            )
        current_price = await self._price_source(symbol)
        if self._is_marketable(request, current_price):
            del self._resting[client_order_id]
            assert request.limit_price is not None
            return self._fill_at(request, request.limit_price)
        return ExecutionResult(accepted=True, status=OrderStatus.NEW, exchange_order_id=f"paper-{client_order_id}")

    @staticmethod
    def _is_marketable(request: OrderRequest, current_price: Decimal) -> bool:
        assert request.limit_price is not None
        if request.side == OrderSide.BUY:
            return current_price <= request.limit_price
        return current_price >= request.limit_price

    def _fill_market(self, request: OrderRequest, price: Decimal) -> ExecutionResult:
        slippage = self._settings.expected_slippage_percent / Decimal(100)
        fill_price = price * (Decimal(1) + slippage) if request.side == OrderSide.BUY else price * (Decimal(1) - slippage)
        return self._fill_at(request, fill_price)

    def _fill_at(self, request: OrderRequest, fill_price: Decimal) -> ExecutionResult:
        base_asset = base_asset_of(request.symbol)
        fee = self._settings.taker_fee_rate

        if request.side == OrderSide.BUY:
            usdt_amount = request.quote_amount if request.quote_amount is not None else (request.quantity or Decimal("0")) * fill_price
            if self.account.usdt_balance < usdt_amount:
                return ExecutionResult(accepted=False, status=OrderStatus.REJECTED, error_message="insufficient paper USDT balance")
            gross_qty = usdt_amount / fill_price
            commission = gross_qty * fee
            net_qty = gross_qty - commission
            self.account.usdt_balance -= usdt_amount
            self.account.holdings[base_asset] = self.account.holdings.get(base_asset, Decimal("0")) + net_qty
            fill = ExecutionFill(
                price=fill_price, quantity=gross_qty, commission=commission, commission_asset=base_asset,
                commission_usdt_equivalent=commission * fill_price, trade_id=f"paper-{utcnow().timestamp()}", timestamp=utcnow(),
            )
            return ExecutionResult(
                accepted=True, status=OrderStatus.FILLED, exchange_order_id=f"paper-{request.client_order_id}",
                fills=[fill], avg_fill_price=fill_price, filled_quantity=gross_qty, net_base_quantity=net_qty,
                filled_quote=gross_qty * fill_price, commission_total_usdt_equivalent=commission * fill_price,
            )

        assert request.quantity is not None
        held = self.account.holdings.get(base_asset, Decimal("0"))
        if held < request.quantity:
            return ExecutionResult(accepted=False, status=OrderStatus.REJECTED, error_message="insufficient paper holdings")
        gross_proceeds = request.quantity * fill_price
        commission = gross_proceeds * fee
        net_proceeds = gross_proceeds - commission
        self.account.holdings[base_asset] = held - request.quantity
        self.account.usdt_balance += net_proceeds
        fill = ExecutionFill(
            price=fill_price, quantity=request.quantity, commission=commission, commission_asset="USDT",
            commission_usdt_equivalent=commission, trade_id=f"paper-{utcnow().timestamp()}", timestamp=utcnow(),
        )
        return ExecutionResult(
            accepted=True, status=OrderStatus.FILLED, exchange_order_id=f"paper-{request.client_order_id}",
            fills=[fill], avg_fill_price=fill_price, filled_quantity=request.quantity, net_base_quantity=request.quantity,
            filled_quote=gross_proceeds, commission_total_usdt_equivalent=commission,
        )
