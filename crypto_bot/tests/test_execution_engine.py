from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from database.models import OrderPurpose, OrderStatus
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

    asyncio.run(asyncio.sleep(1.1))
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
