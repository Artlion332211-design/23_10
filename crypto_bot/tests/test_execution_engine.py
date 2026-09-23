from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from database.models import OrderPurpose, OrderSide, OrderStatus, OrderType
from database.repository import OrderRepository
from database.session import session_scope
from exchange.execution_engine import ExecutionEngine, ExecutionFill, ExecutionResult
from exchange.symbol_filters import SymbolFilters
from utils.time import utcnow


@pytest.fixture
def sol_filters() -> SymbolFilters:
    return SymbolFilters(
        symbol="SOLUSDT", base_asset="SOL", quote_asset="USDT", status="TRADING",
        tick_size=Decimal("0.01"), min_price=Decimal("0"), max_price=Decimal("100000"),
        lot_step_size=Decimal("0.001"), lot_min_qty=Decimal("0.001"), lot_max_qty=Decimal("1000000"),
        market_lot_step_size=Decimal("0.00001"), market_lot_min_qty=Decimal("0.00001"), market_lot_max_qty=Decimal("1000000"),
        min_notional=Decimal("5"), apply_min_notional_to_market=True, base_asset_precision=8, quote_asset_precision=8,
    )


class FakeExecutor:
    def __init__(self, filters: SymbolFilters, fill_price: Decimal = Decimal("142.53")):
        self.filters = filters
        self.fill_price = fill_price
        self.submitted = []

    async def submit(self, request):
        self.submitted.append(request)
        if request.order_type.value == "MARKET":
            qty = self.filters.round_quantity(request.quote_amount / self.fill_price, market=True)
            fill = ExecutionFill(
                price=self.fill_price, quantity=qty, commission=qty * Decimal("0.001"), commission_asset="SOL",
                commission_usdt_equivalent=qty * Decimal("0.001") * self.fill_price, trade_id="t1", timestamp=utcnow(),
            )
            return ExecutionResult(
                accepted=True, status=OrderStatus.FILLED, exchange_order_id="123", fills=[fill],
                avg_fill_price=self.fill_price, filled_quantity=qty, net_base_quantity=qty - fill.commission,
                filled_quote=qty * self.fill_price, commission_total_usdt_equivalent=fill.commission_usdt_equivalent,
            )
        return ExecutionResult(accepted=True, status=OrderStatus.NEW, exchange_order_id="124")

    async def cancel(self, symbol, *, client_order_id):
        return ExecutionResult(accepted=True, status=OrderStatus.CANCELED, exchange_order_id="124")

    async def get_status(self, symbol, *, client_order_id):
        return ExecutionResult(accepted=True, status=OrderStatus.NEW, exchange_order_id="124")


@pytest.fixture
def filters_provider(sol_filters):
    async def _provider(symbol):
        return sol_filters
    return _provider


def test_tight_spread_uses_market_order(db_engine, settings, sol_filters, filters_provider):
    executor = FakeExecutor(sol_filters)
    engine = ExecutionEngine(executor=executor, filters_provider=filters_provider, settings=settings, dry_run=False)

    result = asyncio.run(engine.buy(
        symbol="SOLUSDT", usdt_amount=Decimal("100"), reference_price=Decimal("142.53"),
        spread_percent=Decimal("0.05"), purpose=OrderPurpose.ENTRY, position_id=None,
    ))
    assert result.status == OrderStatus.FILLED
    assert result.order_id is not None
    assert result.net_base_quantity > 0
    assert executor.submitted[0].order_type.value == "MARKET"


def test_wide_spread_uses_limit_order_and_times_out(db_engine, settings, sol_filters, filters_provider):
    tuned = settings.model_copy(update={"limit_order_timeout_seconds": 1})
    executor = FakeExecutor(sol_filters)
    engine = ExecutionEngine(executor=executor, filters_provider=filters_provider, settings=tuned, dry_run=False)

    result = asyncio.run(engine.buy(
        symbol="SOLUSDT", usdt_amount=Decimal("100"), reference_price=Decimal("142.53"),
        spread_percent=Decimal("5.0"), purpose=OrderPurpose.DCA_1, position_id=None,
    ))
    assert result.status == OrderStatus.NEW
    assert executor.submitted[0].order_type.value == "LIMIT"
    assert len(engine._pending_limit_orders) == 1

    # Backdate placed_at past the 1s timeout instead of a real sleep, which
    # left only ~100ms of margin and could flake under a loaded/parallel
    # CI runner.
    client_order_id, (symbol, _placed_at) = next(iter(engine._pending_limit_orders.items()))
    engine._pending_limit_orders[client_order_id] = (symbol, utcnow() - timedelta(seconds=1.1))
    resolved = asyncio.run(engine.check_pending_limit_orders())
    assert len(resolved) == 1
    assert resolved[0][2].status == OrderStatus.CANCELED
    assert len(engine._pending_limit_orders) == 0


def test_dry_run_never_calls_executor(db_engine, settings, sol_filters, filters_provider):
    executor = FakeExecutor(sol_filters)
    engine = ExecutionEngine(executor=executor, filters_provider=filters_provider, settings=settings, dry_run=True)

    result = asyncio.run(engine.buy(
        symbol="SOLUSDT", usdt_amount=Decimal("100"), reference_price=Decimal("142.53"),
        spread_percent=Decimal("0.05"), purpose=OrderPurpose.ENTRY, position_id=None,
    ))
    assert result.accepted is False
    assert result.error_message == "DRY_RUN"
    assert executor.submitted == []


def test_notional_too_small_is_rejected_before_touching_exchange(db_engine, settings, sol_filters, filters_provider):
    executor = FakeExecutor(sol_filters)
    engine = ExecutionEngine(executor=executor, filters_provider=filters_provider, settings=settings, dry_run=False)

    result = asyncio.run(engine.buy(
        symbol="SOLUSDT", usdt_amount=Decimal("1"), reference_price=Decimal("142.53"),
        spread_percent=Decimal("0.05"), purpose=OrderPurpose.ENTRY, position_id=None,
    ))
    assert result.accepted is False
    assert "minNotional" in (result.error_message or "")
    assert executor.submitted == []


class _ScriptedExecutor:
    """Returns a scripted sequence of get_status() results (repeating the
    last one once exhausted), and an independently scriptable cancel()
    result - for exercising retry/give-up timing precisely, independent of
    FakeExecutor's fixed always-fills behavior."""

    def __init__(self, results: list[ExecutionResult], *, cancel_results: list[ExecutionResult] | None = None):
        self._results = list(results)
        self._cancel_results = list(cancel_results) if cancel_results is not None else None
        self.get_status_calls = 0
        self.cancel_calls: list[str] = []

    async def submit(self, request):  # pragma: no cover - unused by these tests
        raise AssertionError("submit should not be called")

    async def cancel(self, symbol, *, client_order_id):
        self.cancel_calls.append(client_order_id)
        if self._cancel_results is not None:
            idx = min(len(self.cancel_calls) - 1, len(self._cancel_results) - 1)
            return self._cancel_results[idx]
        return ExecutionResult(accepted=True, status=OrderStatus.CANCELED, exchange_order_id="124")

    async def get_status(self, symbol, *, client_order_id):
        idx = min(self.get_status_calls, len(self._results) - 1)
        self.get_status_calls += 1
        return self._results[idx]


def _seed_order(client_order_id: str) -> None:
    with session_scope() as session:
        OrderRepository(session).create(
            symbol="SOLUSDT", client_order_id=client_order_id, side=OrderSide.BUY, type=OrderType.LIMIT,
            purpose=OrderPurpose.ENTRY, requested_price=Decimal("140"), requested_qty=Decimal("1"), requested_usdt=Decimal("140"),
        )


def test_reconcile_pending_order_does_not_persist_a_phantom_filled_status(db_engine, settings, filters_provider):
    """A terminal status with fill_data_incomplete=True must not be written
    to the DB yet - doing so would defeat has_resting_order()'s duplicate-
    order guard for an order this same call is about to retry (this is the
    exact self-contradiction the code review caught: persisting the status
    unconditionally, then separately deciding the fill data was unusable
    and re-queuing for retry)."""
    _seed_order("bot-reconcile-1")
    executor = _ScriptedExecutor([
        ExecutionResult(accepted=True, status=OrderStatus.FILLED, exchange_order_id="1", fill_data_incomplete=True),
    ])
    engine = ExecutionEngine(executor=executor, filters_provider=filters_provider, settings=settings, dry_run=False)
    with session_scope() as session:
        order = OrderRepository(session).get_by_client_id("bot-reconcile-1")

    result = asyncio.run(engine.reconcile_pending_order(order))

    assert result.status == OrderStatus.FILLED
    assert result.fill_data_incomplete is True
    with session_scope() as session:
        stored = OrderRepository(session).get_by_client_id("bot-reconcile-1")
        assert stored.status == OrderStatus.NEW  # unchanged - not persisted while unresolved
    assert "bot-reconcile-1" in engine._pending_limit_orders


def test_check_pending_limit_orders_retries_incomplete_fill_data_then_gives_up(db_engine, settings, filters_provider):
    _seed_order("bot-stuck-1")
    tuned = settings.model_copy(update={"limit_order_timeout_seconds": 1})
    incomplete = ExecutionResult(accepted=True, status=OrderStatus.FILLED, exchange_order_id="1", fill_data_incomplete=True)
    executor = _ScriptedExecutor([incomplete])
    engine = ExecutionEngine(executor=executor, filters_provider=filters_provider, settings=tuned, dry_run=False)
    engine._pending_limit_orders["bot-stuck-1"] = ("SOLUSDT", utcnow())

    resolved = asyncio.run(engine.check_pending_limit_orders())
    assert resolved == []  # still within the retry window - not yet given up
    assert "bot-stuck-1" in engine._pending_limit_orders
    with session_scope() as session:
        assert OrderRepository(session).get_by_client_id("bot-stuck-1").status == OrderStatus.NEW

    # Backdate placed_at past the give-up window (5x the 1s timeout) instead
    # of sleeping in the test.
    engine._pending_limit_orders["bot-stuck-1"] = ("SOLUSDT", utcnow() - timedelta(seconds=10))
    resolved = asyncio.run(engine.check_pending_limit_orders())

    assert len(resolved) == 1
    assert resolved[0][2].fill_data_incomplete is True
    assert "bot-stuck-1" not in engine._pending_limit_orders
    with session_scope() as session:
        assert OrderRepository(session).get_by_client_id("bot-stuck-1").status == OrderStatus.FILLED


def test_check_pending_limit_orders_retries_a_cancel_that_comes_back_with_incomplete_fill_data(
    db_engine, settings, filters_provider,
):
    """The cancel()-on-timeout branch must apply the exact same incomplete-
    fill-data retry treatment as the get_status() poll above it - a resting
    order that ages past the timeout, whose cancel() call comes back
    reporting CANCELED but with the real fill breakdown unobtainable (e.g. a
    genuine partial fill right as the cancel raced it, and a transient
    myTrades failure), must not be silently persisted as a clean zero-fill
    cancel - that would permanently lose the partial fill."""
    _seed_order("bot-cancel-incomplete")
    tuned = settings.model_copy(update={"limit_order_timeout_seconds": 1})
    still_new = ExecutionResult(accepted=True, status=OrderStatus.NEW, exchange_order_id="1")
    incomplete_cancel = ExecutionResult(accepted=True, status=OrderStatus.CANCELED, exchange_order_id="1", fill_data_incomplete=True)
    executor = _ScriptedExecutor([still_new], cancel_results=[incomplete_cancel])
    engine = ExecutionEngine(executor=executor, filters_provider=filters_provider, settings=tuned, dry_run=False)
    # Past the 1s timeout (triggers the cancel branch) but well under the
    # 5x give-up window (5s), so this must still retry, not resolve.
    engine._pending_limit_orders["bot-cancel-incomplete"] = ("SOLUSDT", utcnow() - timedelta(seconds=2))

    resolved = asyncio.run(engine.check_pending_limit_orders())

    assert resolved == []  # the incomplete cancel result must not be resolved as final
    assert "bot-cancel-incomplete" in engine._pending_limit_orders
    assert executor.cancel_calls == ["bot-cancel-incomplete"]
    with session_scope() as session:
        assert OrderRepository(session).get_by_client_id("bot-cancel-incomplete").status == OrderStatus.NEW


def test_execution_engine_cancel_persists_and_clears_pending_tracking(db_engine, settings, sol_filters, filters_provider):
    _seed_order("bot-cancel-1")
    executor = FakeExecutor(sol_filters)
    engine = ExecutionEngine(executor=executor, filters_provider=filters_provider, settings=settings, dry_run=False)
    engine._pending_limit_orders["bot-cancel-1"] = ("SOLUSDT", utcnow())

    result = asyncio.run(engine.cancel("SOLUSDT", client_order_id="bot-cancel-1"))

    assert result.status == OrderStatus.CANCELED
    assert "bot-cancel-1" not in engine._pending_limit_orders
    with session_scope() as session:
        stored = OrderRepository(session).get_by_client_id("bot-cancel-1")
        assert stored.status == OrderStatus.CANCELED
