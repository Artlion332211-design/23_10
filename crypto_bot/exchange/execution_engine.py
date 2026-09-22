"""Order execution: the only layer allowed to turn a trading decision into a
real (or simulated) Binance order.

`StrategyEngine` decides *what* to do; `RiskManager` decides *whether* it's
allowed; `ExecutionEngine` decides *how* to place it (order type, rounding,
timeout/cancel) and talks to an `OrderExecutor` (either the real
`BinanceExecutionAdapter` below or `paper.simulator.PaperBroker`) - never to
`binance.AsyncClient` directly. This keeps the mandated call chain
Strategy -> Risk -> Execution -> Binance honest.

Write-ahead ordering matters here: the local Order row is created and
*committed* before Binance is ever called, so a crash between "Binance
accepted the order" and "we recorded the result" leaves a durable NEW-status
row that the startup reconciliation service (see the app composition root)
can repair against Binance's own order history - Binance remains the source
of truth for what actually happened.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol

from binance.exceptions import BinanceAPIException, BinanceRequestException

from config.settings import Settings
from database.models import Order, OrderPurpose, OrderSide, OrderStatus, OrderType
from database.repository import FillRepository, OrderRepository
from database.session import session_scope
from exchange.binance_client import BinanceClient
from exchange.symbol_filters import OrderWouldBeInvalid, SymbolFilters, format_decimal
from utils.time import utcnow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data contracts shared by every OrderExecutor implementation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: OrderSide
    order_type: OrderType
    client_order_id: str
    quote_amount: Decimal | None = None  # MARKET BUY sized by USDT (quoteOrderQty)
    quantity: Decimal | None = None  # SELL, or LIMIT BUY, sized by base asset qty
    limit_price: Decimal | None = None  # LIMIT orders only


@dataclass(frozen=True)
class ExecutionFill:
    price: Decimal
    quantity: Decimal
    commission: Decimal
    commission_asset: str
    commission_usdt_equivalent: Decimal
    trade_id: str | None
    timestamp: datetime


@dataclass
class ExecutionResult:
    accepted: bool
    status: OrderStatus
    exchange_order_id: str | None = None
    order_id: int | None = None  # local DB Order.id, filled in by ExecutionEngine
    fills: list[ExecutionFill] = field(default_factory=list)
    avg_fill_price: Decimal = Decimal("0")
    filled_quantity: Decimal = Decimal("0")  # gross base qty per exchange fill report
    net_base_quantity: Decimal = Decimal("0")  # filled_quantity minus base-asset commission
    filled_quote: Decimal = Decimal("0")
    commission_total_usdt_equivalent: Decimal = Decimal("0")
    error_message: str | None = None
    # True when Binance confirms a terminal order status (FILLED/CANCELED/
    # EXPIRED) with executedQty>0, but the real fill breakdown could not be
    # fetched this attempt (a transient `GET /api/v3/myTrades` failure, or
    # an empty response despite executedQty>0) - `filled_quantity`/
    # `net_base_quantity` above are then a phantom zero, NOT a genuine
    # "nothing filled" result. Callers must treat this as unresolved and
    # retry, never as a final answer (see `BinanceExecutionAdapter._resolve_fills`).
    fill_data_incomplete: bool = False


class OrderWouldExceedTimeout(Exception):
    pass


class OrderExecutor(Protocol):
    """Implemented by `BinanceExecutionAdapter` (live/testnet) and
    `paper.simulator.PaperBroker` (paper trading). Both share the exact same
    interface so the rest of the app never branches on MODE."""

    async def submit(self, request: OrderRequest) -> ExecutionResult: ...

    async def cancel(self, symbol: str, *, client_order_id: str) -> ExecutionResult: ...

    async def get_status(self, symbol: str, *, client_order_id: str) -> ExecutionResult: ...


# ---------------------------------------------------------------------------
# Live Binance adapter
# ---------------------------------------------------------------------------


def _new_client_order_id(purpose: OrderPurpose) -> str:
    return f"bot-{purpose.value[:12].lower()}-{uuid.uuid4().hex[:12]}"


def _commission_usdt_equivalent(commission: Decimal, commission_asset: str, price: Decimal, *, base_asset: str, quote_asset: str) -> Decimal:
    if commission_asset == quote_asset:
        return commission
    if commission_asset == base_asset:
        return commission * price
    # e.g. BNB fee discount: exact fee is preserved on the Fill row via
    # `commission` / `commission_asset`, but converting an arbitrary third
    # asset to USDT here would need an extra price lookup this parsing step
    # doesn't have access to. Documented approximation: it only affects the
    # *reported* USDT-equivalent fee total, never the actual traded
    # price/quantity accounting.
    return Decimal("0")


def _parse_fill(raw: dict[str, Any], *, base_asset: str, quote_asset: str) -> ExecutionFill:
    """One entry of an order-*placement* response's `fills` array (`POST
    /api/v3/order`) - the only Binance response shape that carries this."""
    price = Decimal(raw["price"])
    quantity = Decimal(raw["qty"])
    commission = Decimal(raw.get("commission", "0"))
    commission_asset = raw.get("commissionAsset", quote_asset)
    return ExecutionFill(
        price=price,
        quantity=quantity,
        commission=commission,
        commission_asset=commission_asset,
        commission_usdt_equivalent=_commission_usdt_equivalent(
            commission, commission_asset, price, base_asset=base_asset, quote_asset=quote_asset
        ),
        trade_id=str(raw.get("tradeId")) if raw.get("tradeId") is not None else None,
        timestamp=utcnow(),
    )


def _parse_trade(raw: dict[str, Any], *, base_asset: str, quote_asset: str) -> ExecutionFill:
    """One entry of `GET /api/v3/myTrades` - same information as an
    order-placement fill, but under different keys (`id`/`time` instead of
    `tradeId`, with a real historical trade timestamp instead of "now")."""
    price = Decimal(raw["price"])
    quantity = Decimal(raw["qty"])
    commission = Decimal(raw.get("commission", "0"))
    commission_asset = raw.get("commissionAsset", quote_asset)
    trade_time = raw.get("time")
    return ExecutionFill(
        price=price,
        quantity=quantity,
        commission=commission,
        commission_asset=commission_asset,
        commission_usdt_equivalent=_commission_usdt_equivalent(
            commission, commission_asset, price, base_asset=base_asset, quote_asset=quote_asset
        ),
        trade_id=str(raw.get("id")) if raw.get("id") is not None else None,
        timestamp=datetime.fromtimestamp(trade_time / 1000, tz=UTC) if trade_time is not None else utcnow(),
    )


_BINANCE_STATUS_MAP = {
    "NEW": OrderStatus.NEW,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED,
    "PENDING_CANCEL": OrderStatus.CANCELED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
    "EXPIRED_IN_MATCH": OrderStatus.EXPIRED,
}


def _build_execution_result(
    raw: dict[str, Any], fills: list[ExecutionFill], *, base_asset: str, fill_data_incomplete: bool = False
) -> ExecutionResult:
    filled_qty = sum((f.quantity for f in fills), Decimal("0"))
    filled_quote = sum((f.price * f.quantity for f in fills), Decimal("0"))
    base_commission = sum((f.commission for f in fills if f.commission_asset == base_asset), Decimal("0"))
    commission_usdt = sum((f.commission_usdt_equivalent for f in fills), Decimal("0"))
    avg_price = (filled_quote / filled_qty) if filled_qty > 0 else Decimal("0")
    status = _BINANCE_STATUS_MAP.get(raw.get("status", ""), OrderStatus.NEW)
    return ExecutionResult(
        accepted=True,
        status=status,
        exchange_order_id=str(raw.get("orderId")) if raw.get("orderId") is not None else None,
        fills=fills,
        avg_fill_price=avg_price,
        filled_quantity=filled_qty,
        net_base_quantity=filled_qty - base_commission,
        filled_quote=filled_quote,
        commission_total_usdt_equivalent=commission_usdt,
        fill_data_incomplete=fill_data_incomplete,
    )


class BinanceExecutionAdapter:
    """Real (or Spot Testnet) order placement."""

    def __init__(self, client: BinanceClient) -> None:
        self._client = client

    async def submit(self, request: OrderRequest) -> ExecutionResult:
        filters = await self._client.get_symbol_filters(request.symbol)
        params: dict[str, Any] = {
            "symbol": request.symbol,
            "side": request.side.value,
            "type": request.order_type.value,
            "newClientOrderId": request.client_order_id,
            "newOrderRespType": "FULL",
        }
        if request.order_type == OrderType.MARKET:
            if request.side == OrderSide.BUY:
                assert request.quote_amount is not None
                params["quoteOrderQty"] = format_decimal(request.quote_amount)
            else:
                assert request.quantity is not None
                params["quantity"] = format_decimal(request.quantity)
        else:
            assert request.quantity is not None and request.limit_price is not None
            params["quantity"] = format_decimal(request.quantity)
            params["price"] = format_decimal(request.limit_price)
            params["timeInForce"] = "GTC"

        try:
            raw = await self._client.create_order(**params)
        except (BinanceAPIException, BinanceRequestException) as exc:
            logger.error("Order rejected by Binance: %s %s", request, exc)
            return ExecutionResult(accepted=False, status=OrderStatus.REJECTED, error_message=str(exc))

        # The order-*placement* response is the one Binance response that
        # does carry `fills` directly - no extra round-trip needed here.
        fills = [_parse_fill(f, base_asset=filters.base_asset, quote_asset=filters.quote_asset) for f in raw.get("fills", [])]
        return _build_execution_result(raw, fills, base_asset=filters.base_asset)

    async def cancel(self, symbol: str, *, client_order_id: str) -> ExecutionResult:
        filters = await self._client.get_symbol_filters(symbol)
        try:
            raw = await self._client.cancel_order(symbol=symbol, origClientOrderId=client_order_id)
        except (BinanceAPIException, BinanceRequestException) as exc:
            # Binance rejects a cancel for more than one reason - "still
            # resting" is only one of them; "already FILLED" or "already
            # CANCELED" reject too, with the same exception type. Blindly
            # reporting NEW here would make a caller believe an already-
            # filled order is still open, silently losing that fill. Ask
            # for the order's real current status instead of guessing -
            # get_status has its own exception handling that correctly
            # falls back to "still pending" if the real status truly can't
            # be determined either.
            logger.warning(
                "Cancel failed for %s/%s: %s - falling back to get_status to find the real state", symbol, client_order_id, exc
            )
            return await self.get_status(symbol, client_order_id=client_order_id)
        fills, incomplete = await self._resolve_fills(raw, symbol=symbol, filters=filters)
        return _build_execution_result(raw, fills, base_asset=filters.base_asset, fill_data_incomplete=incomplete)

    async def get_status(self, symbol: str, *, client_order_id: str) -> ExecutionResult:
        filters = await self._client.get_symbol_filters(symbol)
        try:
            raw = await self._client.get_order_status(symbol, orig_client_order_id=client_order_id)
        except (BinanceAPIException, BinanceRequestException) as exc:
            # Treat "couldn't confirm status" as still-pending (like `cancel`'s
            # failure path below), never as resolved - `check_pending_limit_orders`
            # would otherwise have to interpret an unrelated exception as
            # FILLED/CANCELED/REJECTED/EXPIRED for every other order still
            # queued behind this one in the same poll cycle.
            logger.warning("get_status failed for %s/%s: %s", symbol, client_order_id, exc)
            return ExecutionResult(accepted=False, status=OrderStatus.NEW, error_message=str(exc))
        fills, incomplete = await self._resolve_fills(raw, symbol=symbol, filters=filters)
        return _build_execution_result(raw, fills, base_asset=filters.base_asset, fill_data_incomplete=incomplete)

    async def _resolve_fills(
        self, raw: dict[str, Any], *, symbol: str, filters: SymbolFilters
    ) -> tuple[list[ExecutionFill], bool]:
        """`GET /api/v3/order` and `DELETE /api/v3/order` (unlike the
        order-*placement* response) never include a `fills` breakdown, even
        when `executedQty`/`status` show the order genuinely filled - this
        is a real Binance API contract gap, not a transient omission.
        Without this, `get_status`/`cancel` would silently report zero
        filled quantity for exactly the case `check_pending_limit_orders`/
        `reconcile_pending_order` exist to handle: a resting LIMIT order
        that only resolves later. Falls back to `GET /api/v3/myTrades`
        (which does carry real per-trade commission) whenever something
        was actually executed.

        Returns `(fills, fill_data_incomplete)`: the second element is True
        only when `executedQty` says something genuinely filled but the
        real breakdown could not be obtained this attempt (transient
        myTrades failure, or an empty response despite executedQty>0) - the
        caller must NOT treat that combination as "nothing filled"."""
        if raw.get("fills"):
            return [_parse_fill(f, base_asset=filters.base_asset, quote_asset=filters.quote_asset) for f in raw["fills"]], False
        if Decimal(raw.get("executedQty", "0")) <= 0:
            return [], False
        order_id = raw.get("orderId")
        if order_id is None:
            return [], False
        try:
            trades = await self._client.get_my_trades(symbol, order_id=str(order_id))
        except (BinanceAPIException, BinanceRequestException) as exc:
            logger.warning("get_my_trades failed for %s/order %s: %s", symbol, order_id, exc)
            return [], True
        if not trades:
            # executedQty>0 but myTrades came back empty - most likely
            # eventual-consistency lag right after the trade, not a
            # genuine zero fill. Same "retry, don't resolve" treatment.
            logger.warning("get_my_trades returned no trades for %s/order %s despite executedQty>0", symbol, order_id)
            return [], True
        return [_parse_trade(t, base_asset=filters.base_asset, quote_asset=filters.quote_asset) for t in trades], False


# ---------------------------------------------------------------------------
# ExecutionEngine: order-type choice, rounding, persistence, DRY_RUN gating
# ---------------------------------------------------------------------------


FiltersProvider = Any  # Callable[[str], Awaitable[SymbolFilters]] (see app.py wiring)

# How many LIMIT_ORDER_TIMEOUT_SECONDS-sized windows check_pending_limit_orders
# keeps retrying an order stuck reporting a terminal status with incomplete
# fill data before giving up and resolving anyway (see its docstring).
_FILL_DATA_GIVEUP_MULTIPLIER = 5


class ExecutionEngine:
    def __init__(
        self,
        *,
        executor: OrderExecutor,
        filters_provider: FiltersProvider,
        settings: Settings,
        dry_run: bool,
    ) -> None:
        self._executor = executor
        self._filters_provider = filters_provider
        self._settings = settings
        self._dry_run = dry_run
        self._pending_limit_orders: dict[str, tuple[str, datetime]] = {}  # client_order_id -> (symbol, placed_at)

    def _choose_order_type(self, spread_percent: Decimal) -> OrderType:
        """Tight spread -> MARKET (minimal slippage risk, instant fill).
        Wide spread -> LIMIT (protects against paying deep into the book)."""
        threshold = self._settings.max_spread_percent / 2
        return OrderType.MARKET if spread_percent <= threshold else OrderType.LIMIT

    async def _get_filters(self, symbol: str) -> SymbolFilters:
        return await self._filters_provider(symbol)

    def _dry_run_result(self, order: Order) -> ExecutionResult:
        logger.info("[DRY_RUN] Not sending order to Binance: %s %s %s", order.symbol, order.side, order.type)
        return ExecutionResult(
            accepted=False, status=OrderStatus.CANCELED, order_id=order.id, error_message="DRY_RUN"
        )

    async def reconcile_pending_order(self, order: Order) -> ExecutionResult:
        """Called once at startup for every locally NEW/PARTIALLY_FILLED
        order: asks the exchange for its authoritative current status and
        persists it - Binance (or, in PAPER mode, the fill ledger the
        `PaperBroker` was rebuilt from) is the source of truth, never the
        local row alone. A LIMIT order still resting (or whose fill detail
        could not be confirmed this attempt) is re-registered with the
        in-memory timeout tracker, which does not survive a restart, so
        `check_pending_limit_orders` resumes polling it instead of leaving
        it to rest forever.

        Deliberately does NOT persist a terminal status (FILLED/CANCELED/
        EXPIRED) until the result is actually complete: persisting early
        used to write `Order.status=FILLED` to the DB even while this same
        method decided the fill data was unusable and re-queued the order
        for retry - `OrderRepository.has_resting_order()` only looks at
        `Order.status`, so that premature write silently defeated its own
        duplicate-order guard for exactly the order still being retried.
        """
        result = await self._executor.get_status(order.symbol, client_order_id=order.client_order_id)
        if result.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED) or result.fill_data_incomplete:
            self._pending_limit_orders[order.client_order_id] = (order.symbol, order.created_at)
            return result
        self._persist_result(order.id, result)
        return result

    def _persist_result(self, order_id: int, result: ExecutionResult) -> None:
        with session_scope() as session:
            order_repo = OrderRepository(session)
            order = order_repo.get(order_id)
            assert order is not None
            order_repo.update_status(order, result.status, binance_order_id=result.exchange_order_id)
            fill_repo = FillRepository(session)
            for f in result.fills:
                fill_repo.add(
                    order_id=order.id,
                    price=f.price,
                    quantity=f.quantity,
                    commission=f.commission,
                    commission_asset=f.commission_asset,
                    commission_usdt_equivalent=f.commission_usdt_equivalent,
                    trade_id=f.trade_id,
                    timestamp=f.timestamp,
                )
        result.order_id = order_id

    async def buy(
        self,
        *,
        symbol: str,
        usdt_amount: Decimal,
        reference_price: Decimal,
        spread_percent: Decimal,
        purpose: OrderPurpose,
        position_id: int | None,
    ) -> ExecutionResult:
        filters = await self._get_filters(symbol)
        if not filters.is_tradable():
            return ExecutionResult(accepted=False, status=OrderStatus.REJECTED, error_message=f"{symbol} not TRADING")

        order_type = self._choose_order_type(spread_percent)
        client_order_id = _new_client_order_id(purpose)

        try:
            if order_type == OrderType.MARKET:
                quote_amount = usdt_amount.quantize(Decimal(10) ** -filters.quote_asset_precision, rounding=ROUND_DOWN)
                # Sanity-check against LOT_SIZE/MIN_NOTIONAL using the reference
                # price even though quoteOrderQty is what actually gets sent -
                # this is what catches a too-small order before Binance does.
                filters.quantity_for_notional(reference_price, quote_amount, market=True)
                requested_price, requested_qty = None, Decimal("0")
                request = OrderRequest(
                    symbol=symbol, side=OrderSide.BUY, order_type=OrderType.MARKET,
                    client_order_id=client_order_id, quote_amount=quote_amount,
                )
                requested_usdt = quote_amount
            else:
                limit_price = filters.round_price(reference_price)
                quantity = filters.quantity_for_notional(limit_price, usdt_amount)
                requested_price, requested_qty = limit_price, quantity
                request = OrderRequest(
                    symbol=symbol, side=OrderSide.BUY, order_type=OrderType.LIMIT,
                    client_order_id=client_order_id, quantity=quantity, limit_price=limit_price,
                )
                requested_usdt = usdt_amount
        except OrderWouldBeInvalid as exc:
            logger.warning("BUY request invalid for %s: %s", symbol, exc)
            return ExecutionResult(accepted=False, status=OrderStatus.REJECTED, error_message=str(exc))

        with session_scope() as session:
            order = OrderRepository(session).create(
                position_id=position_id, symbol=symbol, client_order_id=client_order_id,
                side=OrderSide.BUY, type=order_type, purpose=purpose,
                requested_price=requested_price, requested_qty=requested_qty, requested_usdt=requested_usdt,
            )
            order_id = order.id

        if self._dry_run:
            with session_scope() as session:
                order_repo = OrderRepository(session)
                dry_run_order = order_repo.get(order_id)
                assert dry_run_order is not None
                order_repo.update_status(dry_run_order, OrderStatus.CANCELED)
                return self._dry_run_result(dry_run_order)

        result = await self._executor.submit(request)
        self._persist_result(order_id, result)
        if order_type == OrderType.LIMIT and result.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
            self._pending_limit_orders[client_order_id] = (symbol, utcnow())
        return result

    async def sell(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        reference_price: Decimal,
        spread_percent: Decimal,
        purpose: OrderPurpose,
        position_id: int,
    ) -> ExecutionResult:
        filters = await self._get_filters(symbol)
        if not filters.is_tradable():
            return ExecutionResult(accepted=False, status=OrderStatus.REJECTED, error_message=f"{symbol} not TRADING")

        order_type = self._choose_order_type(spread_percent)
        client_order_id = _new_client_order_id(purpose)

        try:
            qty = filters.round_quantity(quantity, market=(order_type == OrderType.MARKET))
            if order_type == OrderType.MARKET:
                filters.validate_notional(reference_price, qty, market=True)
                requested_price = None
                request = OrderRequest(
                    symbol=symbol, side=OrderSide.SELL, order_type=OrderType.MARKET,
                    client_order_id=client_order_id, quantity=qty,
                )
            else:
                limit_price = filters.round_price(reference_price)
                filters.validate_notional(limit_price, qty)
                requested_price = limit_price
                request = OrderRequest(
                    symbol=symbol, side=OrderSide.SELL, order_type=OrderType.LIMIT,
                    client_order_id=client_order_id, quantity=qty, limit_price=limit_price,
                )
        except OrderWouldBeInvalid as exc:
            logger.warning("SELL request invalid for %s: %s", symbol, exc)
            return ExecutionResult(accepted=False, status=OrderStatus.REJECTED, error_message=str(exc))

        with session_scope() as session:
            order = OrderRepository(session).create(
                position_id=position_id, symbol=symbol, client_order_id=client_order_id,
                side=OrderSide.SELL, type=order_type, purpose=purpose,
                requested_price=requested_price, requested_qty=qty, requested_usdt=qty * reference_price,
            )
            order_id = order.id

        if self._dry_run:
            with session_scope() as session:
                order_repo = OrderRepository(session)
                dry_run_order = order_repo.get(order_id)
                assert dry_run_order is not None
                order_repo.update_status(dry_run_order, OrderStatus.CANCELED)
                return self._dry_run_result(dry_run_order)

        result = await self._executor.submit(request)
        self._persist_result(order_id, result)
        if order_type == OrderType.LIMIT and result.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
            self._pending_limit_orders[client_order_id] = (symbol, utcnow())
        return result

    async def check_pending_limit_orders(self) -> list[tuple[str, str, ExecutionResult]]:
        """Poll resting LIMIT orders; cancel any that have sat unfilled past
        `LIMIT_ORDER_TIMEOUT_SECONDS`. Returns (symbol, client_order_id, result)
        for every order that was cancelled or discovered filled, so the
        caller (StrategyEngine) can react (retry as MARKET, finalize a
        position, etc).
        """
        timeout = self._settings.limit_order_timeout_seconds
        now = utcnow()
        resolved: list[tuple[str, str, ExecutionResult]] = []
        for client_order_id, (symbol, placed_at) in list(self._pending_limit_orders.items()):
            age = (now - placed_at).total_seconds()
            status = await self._executor.get_status(symbol, client_order_id=client_order_id)
            if status.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED) and status.fill_data_incomplete:
                # Exchange confirms a terminal status but the real fill
                # breakdown wasn't fetchable this cycle - retry rather than
                # resolving off a phantom zero-quantity result. Not
                # cancellable anymore either way, so the timeout check below
                # is skipped too - UNLESS this has been stuck long enough
                # that continuing to retry silently forever is worse than
                # resolving with what little data is available: at that
                # point, resolve anyway (status.fill_data_incomplete stays
                # True on the returned result) so the caller can at least
                # alert a human to reconcile the order manually.
                if age < timeout * _FILL_DATA_GIVEUP_MULTIPLIER:
                    logger.warning(
                        "LIMIT order %s/%s reports %s with incomplete fill data, will retry (age=%.0fs)",
                        symbol, client_order_id, status.status.value, age,
                    )
                    continue
                logger.error(
                    "LIMIT order %s/%s stuck reporting %s with incomplete fill data for %.0fs - giving up and "
                    "resolving anyway; manual reconciliation against the exchange may be needed",
                    symbol, client_order_id, status.status.value, age,
                )
            if status.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
                del self._pending_limit_orders[client_order_id]
                self._persist_result_by_client_id(client_order_id, status)
                resolved.append((symbol, client_order_id, status))
                continue
            if age >= timeout:
                logger.info("LIMIT order %s/%s timed out after %.0fs, cancelling", symbol, client_order_id, age)
                cancel_result = await self._executor.cancel(symbol, client_order_id=client_order_id)
                del self._pending_limit_orders[client_order_id]
                self._persist_result_by_client_id(client_order_id, cancel_result)
                resolved.append((symbol, client_order_id, cancel_result))
        return resolved

    def restore_pending_limit_order(self, order: Order) -> None:
        """Resumes `LIMIT_ORDER_TIMEOUT_SECONDS` tracking for a still-open
        order across a restart (see `orchestration.reconciliation`) - this
        in-memory dict does not survive a restart on its own. Mirrors what
        `reconcile_pending_order` does for LIVE after a real Binance status
        check; PAPER has no exchange to ask, so the caller re-hydrates the
        executor's own resting-order state separately first."""
        self._pending_limit_orders[order.client_order_id] = (order.symbol, order.created_at)

    async def cancel(self, symbol: str, *, client_order_id: str) -> ExecutionResult:
        """Public cancel for callers outside the timeout-polling loop above
        (e.g. a forced position close that must not leave a resting order
        stale behind it - see `StrategyEngine._cancel_resting_orders_for_position`).
        Mirrors exactly what `check_pending_limit_orders`'s own timeout-
        cancel branch does, so both paths agree on how a cancel result is
        persisted and how `_pending_limit_orders` tracking is cleared."""
        result = await self._executor.cancel(symbol, client_order_id=client_order_id)
        self._pending_limit_orders.pop(client_order_id, None)
        self._persist_result_by_client_id(client_order_id, result)
        return result

    def _persist_result_by_client_id(self, client_order_id: str, result: ExecutionResult) -> None:
        with session_scope() as session:
            order = OrderRepository(session).get_by_client_id(client_order_id)
            if order is None:
                return
            order_id = order.id
        self._persist_result(order_id, result)
