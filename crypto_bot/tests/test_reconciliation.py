from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from database.models import OrderPurpose, OrderSide, OrderStatus, OrderType
from database.repository import OrderRepository, PositionRepository
from database.session import session_scope
from exchange.execution_engine import ExecutionEngine, ExecutionResult
from exchange.symbol_filters import SymbolFilters
from orchestration.reconciliation import reconcile_live, reconcile_paper
from paper.simulator import PaperBroker
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


@pytest.fixture
def filters_provider(sol_filters):
    async def _provider(symbol):
        return sol_filters
    return _provider


class _FakeClient:
    def __init__(self, balances: dict[str, tuple[Decimal, Decimal]]) -> None:
        self._balances = balances

    async def get_account_balances(self) -> dict[str, tuple[Decimal, Decimal]]:
        return self._balances


class _StatusExecutor:
    """Never actually submits/cancels - reconciliation only ever asks for
    status of orders that already exist on the exchange."""

    def __init__(self, status: ExecutionResult) -> None:
        self._status = status
        self.get_status_calls: list[str] = []

    async def submit(self, request):  # pragma: no cover - must never be called
        raise AssertionError("submit should never be called during reconciliation")

    async def cancel(self, symbol, *, client_order_id):  # pragma: no cover - must never be called
        raise AssertionError("cancel should never be called during reconciliation")

    async def get_status(self, symbol, *, client_order_id):
        self.get_status_calls.append(client_order_id)
        return self._status


def _seed_pending_order(client_order_id: str) -> None:
    with session_scope() as session:
        order = OrderRepository(session).create(
            symbol="SOLUSDT", client_order_id=client_order_id, side=OrderSide.BUY, type=OrderType.LIMIT,
            purpose=OrderPurpose.ENTRY, requested_price=Decimal("140"), requested_qty=Decimal("1"),
            requested_usdt=Decimal("140"),
        )
        assert order.status == OrderStatus.NEW


def test_reconcile_live_persists_an_order_that_resolved_while_offline(db_engine, settings, filters_provider):
    _seed_pending_order("pending-1")
    executor = _StatusExecutor(ExecutionResult(accepted=True, status=OrderStatus.FILLED, exchange_order_id="999"))
    engine = ExecutionEngine(executor=executor, filters_provider=filters_provider, settings=settings, dry_run=False)

    report = asyncio.run(reconcile_live(_FakeClient({}), engine))

    assert report.resolved_orders == ["pending-1"]
    with session_scope() as session:
        updated = OrderRepository(session).get_by_client_id("pending-1")
        assert updated is not None
        assert updated.status == OrderStatus.FILLED


def test_reconcile_live_re_registers_a_limit_order_still_resting(db_engine, settings, filters_provider):
    _seed_pending_order("pending-2")
    executor = _StatusExecutor(ExecutionResult(accepted=True, status=OrderStatus.NEW, exchange_order_id="999"))
    engine = ExecutionEngine(executor=executor, filters_provider=filters_provider, settings=settings, dry_run=False)

    asyncio.run(reconcile_live(_FakeClient({}), engine))

    assert "pending-2" in engine._pending_limit_orders


def test_reconcile_live_flags_a_position_binance_no_longer_backs(db_engine, settings, filters_provider):
    with session_scope() as session:
        PositionRepository(session).create(
            symbol="SOLUSDT", opened_at=utcnow(), avg_entry_price=Decimal("140"),
            total_quantity=Decimal("1.0"), total_cost_usdt=Decimal("140"), target_price=Decimal("154"),
        )
    executor = _StatusExecutor(ExecutionResult(accepted=True, status=OrderStatus.NEW))
    engine = ExecutionEngine(executor=executor, filters_provider=filters_provider, settings=settings, dry_run=False)
    client = _FakeClient({"SOL": (Decimal("0.1"), Decimal("0"))})  # far less than the 1.0 the DB expects

    report = asyncio.run(reconcile_live(client, engine))

    assert report.has_warnings
    assert "SOLUSDT" in report.position_mismatches[0]


def test_reconcile_live_accepts_a_position_binance_fully_backs(db_engine, settings, filters_provider):
    with session_scope() as session:
        PositionRepository(session).create(
            symbol="SOLUSDT", opened_at=utcnow(), avg_entry_price=Decimal("140"),
            total_quantity=Decimal("1.0"), total_cost_usdt=Decimal("140"), target_price=Decimal("154"),
        )
    executor = _StatusExecutor(ExecutionResult(accepted=True, status=OrderStatus.NEW))
    engine = ExecutionEngine(executor=executor, filters_provider=filters_provider, settings=settings, dry_run=False)
    client = _FakeClient({"SOL": (Decimal("1.0"), Decimal("0"))})

    report = asyncio.run(reconcile_live(client, engine))

    assert not report.has_warnings


def test_reconcile_paper_rebuilds_balance_and_holdings_from_fill_ledger(db_engine, settings, filters_provider):
    async def price_source(symbol: str) -> Decimal:
        return Decimal("100")

    live_broker = PaperBroker(settings, price_source)
    engine = ExecutionEngine(executor=live_broker, filters_provider=filters_provider, settings=settings, dry_run=False)

    asyncio.run(engine.buy(
        symbol="SOLUSDT", usdt_amount=Decimal("100"), reference_price=Decimal("100"),
        spread_percent=Decimal("0.01"), purpose=OrderPurpose.ENTRY, position_id=None,
    ))
    asyncio.run(engine.buy(
        symbol="SOLUSDT", usdt_amount=Decimal("50"), reference_price=Decimal("95"),
        spread_percent=Decimal("0.01"), purpose=OrderPurpose.DCA_1, position_id=None,
    ))

    # Simulates a fresh process: a brand new broker starting from the
    # configured balance, as if the previous run's in-memory state never
    # existed - reconciliation must arrive at the same numbers regardless.
    restarted_broker = PaperBroker(settings, price_source)
    report = reconcile_paper(settings, restarted_broker)

    assert restarted_broker.account.usdt_balance == live_broker.account.usdt_balance
    assert restarted_broker.account.holdings == live_broker.account.holdings
    assert restarted_broker.account.usdt_balance == settings.paper_starting_balance_usdt - Decimal("100") - Decimal("50")
    assert "restored paper balance" in report.notes[0]
