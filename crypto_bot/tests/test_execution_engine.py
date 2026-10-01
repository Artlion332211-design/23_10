from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from database.models import OrderPurpose, OrderSide, OrderStatus, OrderType
from database.repository import FillRepository, OrderRepository
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

def _fill(trade_id: str) -> ExecutionFill:
    return ExecutionFill(
        price=Decimal("100"), quantity=Decimal("1"), commission=Decimal("0.001"), commission_asset="SOL",
        commission_usdt_equivalent=Decimal("0.1"), trade_id=trade_id, timestamp=utcnow(),
    )


def test_resolving_a_partially_filled_order_does_not_duplicate_fill_rows(db_engine, settings, filters_provider, sol_filters):
    """A LIMIT order that partially fills on submit persists that trade; when
    it resolves, Binance reports its cumulative fills (the same trade again
    plus the rest) - the ledger must end up with each trade exactly once."""
    from database.repository import FillRepository

    engine = ExecutionEngine(executor=FakeExecutor(sol_filters), filters_provider=filters_provider, settings=settings, dry_run=False)
    with session_scope() as session:
        order = OrderRepository(session).create(
            position_id=None, symbol="SOLUSDT", client_order_id="bot-dca-partial", side=OrderSide.BUY,
            type=OrderType.LIMIT, purpose=OrderPurpose.DCA_1, requested_price=Decimal("100"),
            requested_qty=Decimal("2"), requested_usdt=Decimal("200"),
        )
        order_id = order.id

    engine._persist_result(order_id, ExecutionResult(accepted=True, status=OrderStatus.PARTIALLY_FILLED, fills=[_fill("t1")]))
    engine._persist_result(order_id, ExecutionResult(accepted=True, status=OrderStatus.FILLED, fills=[_fill("t1"), _fill("t2")]))

    with session_scope() as session:
        trade_ids = sorted(f.trade_id for f in FillRepository(session).for_order(order_id))
    assert trade_ids == ["t1", "t2"]


def test_market_order_with_unknown_outcome_is_tracked_for_resolution(db_engine, settings, filters_provider, sol_filters):
    """A MARKET order whose response was lost comes back NEW; it must be
    polled like a resting LIMIT order or its NEW row blocks the symbol forever."""

    class LostResponseExecutor(FakeExecutor):
        async def submit(self, request):
            return ExecutionResult(accepted=False, status=OrderStatus.NEW, error_message="timed out")

    engine = ExecutionEngine(
        executor=LostResponseExecutor(sol_filters), filters_provider=filters_provider, settings=settings, dry_run=False
    )
    asyncio.run(engine.buy(
        symbol="SOLUSDT", usdt_amount=Decimal("100"), reference_price=Decimal("142.53"),
        spread_percent=Decimal("0.01"), purpose=OrderPurpose.ENTRY, position_id=None,
    ))

    assert len(engine._pending_limit_orders) == 1


# ---------------------------------------------------------------------------
# ExecutionEngine.cancel (force-close path) vs. results that aren't final yet
# ---------------------------------------------------------------------------


def _result_with_fills(status: OrderStatus, *trade_ids: str) -> ExecutionResult:
    fills = [_fill(t) for t in trade_ids]
    qty = Decimal(len(fills))
    return ExecutionResult(
        accepted=True, status=status, exchange_order_id="9", fills=fills, avg_fill_price=Decimal("100"),
        filled_quantity=qty, net_base_quantity=qty - Decimal("0.001") * len(fills), filled_quote=qty * Decimal("100"),
        commission_total_usdt_equivalent=Decimal("0.1") * len(fills),
    )


def _stored_status(client_order_id: str) -> OrderStatus:
    with session_scope() as session:
        return OrderRepository(session).get_by_client_id(client_order_id).status


def _stored_trade_ids(client_order_id: str) -> list[str]:
    with session_scope() as session:
        order = OrderRepository(session).get_by_client_id(client_order_id)
        return sorted(f.trade_id for f in FillRepository(session).for_order(order.id))


def test_execution_engine_cancel_keeps_polling_an_order_whose_status_could_not_be_determined(
    db_engine, settings, filters_provider,
):
    """BinanceExecutionAdapter.cancel falls back to get_status, which says NEW
    when a network blip hides the real status. cancel() used to untrack
    that order, leaving a NEW row nothing polled: has_resting_order() then
    stopped manage_position from doing anything for the position until a
    restart. The order must stay tracked with its original age, so the
    regular poll can resolve it."""
    _seed_order("bot-cancel-blip")
    unknown = ExecutionResult(accepted=False, status=OrderStatus.NEW, error_message="timed out")
    executor = _ScriptedExecutor([_result_with_fills(OrderStatus.FILLED, "t1")], cancel_results=[unknown])
    engine = ExecutionEngine(executor=executor, filters_provider=filters_provider, settings=settings, dry_run=False)
    placed_at = utcnow() - timedelta(seconds=30)
    engine._pending_limit_orders["bot-cancel-blip"] = ("SOLUSDT", placed_at)

    result = asyncio.run(engine.cancel("SOLUSDT", client_order_id="bot-cancel-blip"))

    assert result.status == OrderStatus.NEW
    assert engine._pending_limit_orders.get("bot-cancel-blip") == ("SOLUSDT", placed_at)
    assert _stored_status("bot-cancel-blip") == OrderStatus.NEW

    resolved = asyncio.run(engine.check_pending_limit_orders())

    assert [(cid, r.status) for _s, cid, r in resolved] == [("bot-cancel-blip", OrderStatus.FILLED)]
    assert "bot-cancel-blip" not in engine._pending_limit_orders
    assert _stored_status("bot-cancel-blip") == OrderStatus.FILLED
    assert _stored_trade_ids("bot-cancel-blip") == ["t1"]


def test_execution_engine_cancel_puts_an_untracked_unresolved_order_back_under_polling(
    db_engine, settings, filters_provider,
):
    """A NEW row that isn't tracked in memory (for example one left behind
    by the old cancel() behaviour) must be put back under polling after an
    inconclusive cancel. Its placement time comes from the DB row, so the
    timeout counts from the real placement."""
    _seed_order("bot-cancel-untracked")
    unknown = ExecutionResult(accepted=False, status=OrderStatus.NEW, error_message="timed out")
    executor = _ScriptedExecutor([unknown], cancel_results=[unknown])
    engine = ExecutionEngine(executor=executor, filters_provider=filters_provider, settings=settings, dry_run=False)

    asyncio.run(engine.cancel("SOLUSDT", client_order_id="bot-cancel-untracked"))

    with session_scope() as session:
        created_at = OrderRepository(session).get_by_client_id("bot-cancel-untracked").created_at
    assert engine._pending_limit_orders.get("bot-cancel-untracked") == ("SOLUSDT", created_at)


def test_execution_engine_cancel_does_not_persist_a_terminal_result_with_incomplete_fill_data(
    db_engine, settings, filters_provider,
):
    """A cancel can race a real partial fill and come back CANCELED while the
    myTrades lookup fails. Saving that as a zero-fill CANCELED would lose the
    partial fill for good. The row must stay unresolved, and the next poll
    must record the real fill."""
    _seed_order("bot-cancel-incomplete-2")
    incomplete = ExecutionResult(accepted=True, status=OrderStatus.CANCELED, exchange_order_id="9", fill_data_incomplete=True)
    executor = _ScriptedExecutor([_result_with_fills(OrderStatus.CANCELED, "t1")], cancel_results=[incomplete])
    engine = ExecutionEngine(executor=executor, filters_provider=filters_provider, settings=settings, dry_run=False)
    engine._pending_limit_orders["bot-cancel-incomplete-2"] = ("SOLUSDT", utcnow())

    result = asyncio.run(engine.cancel("SOLUSDT", client_order_id="bot-cancel-incomplete-2"))

    assert result.status == OrderStatus.CANCELED
    assert result.fill_data_incomplete is True  # kept, so the caller still alerts the owner
    assert _stored_status("bot-cancel-incomplete-2") == OrderStatus.NEW
    assert "bot-cancel-incomplete-2" in engine._pending_limit_orders

    resolved = asyncio.run(engine.check_pending_limit_orders())

    assert len(resolved) == 1
    assert resolved[0][2].filled_quantity == Decimal("1")
    assert _stored_status("bot-cancel-incomplete-2") == OrderStatus.CANCELED
    assert _stored_trade_ids("bot-cancel-incomplete-2") == ["t1"]


def test_execution_engine_cancel_hands_back_no_fills_until_the_order_resolves(db_engine, settings, filters_provider):
    """The force-close caller applies whatever fills cancel() returns to the
    position. If the cancel failed and get_status found the order
    PARTIALLY_FILLED, the poll later reports the cumulative fills again when
    the order resolves. Returning the partial fills now as well would apply
    them to the position twice."""
    _seed_order("bot-cancel-partial")
    executor = _ScriptedExecutor(
        [_result_with_fills(OrderStatus.FILLED, "t1", "t2")],
        cancel_results=[_result_with_fills(OrderStatus.PARTIALLY_FILLED, "t1")],
    )
    engine = ExecutionEngine(executor=executor, filters_provider=filters_provider, settings=settings, dry_run=False)
    engine._pending_limit_orders["bot-cancel-partial"] = ("SOLUSDT", utcnow())

    result = asyncio.run(engine.cancel("SOLUSDT", client_order_id="bot-cancel-partial"))

    assert result.status == OrderStatus.PARTIALLY_FILLED
    assert result.fills == []
    assert result.filled_quantity == result.net_base_quantity == result.filled_quote == Decimal("0")
    assert "bot-cancel-partial" in engine._pending_limit_orders
    assert _stored_status("bot-cancel-partial") == OrderStatus.NEW
    assert _stored_trade_ids("bot-cancel-partial") == []

    resolved = asyncio.run(engine.check_pending_limit_orders())

    assert len(resolved) == 1  # the one and only time these fills are handed out
    assert resolved[0][2].filled_quantity == Decimal("2")
    assert _stored_trade_ids("bot-cancel-partial") == ["t1", "t2"]


@pytest.mark.parametrize("cancel_status", [OrderStatus.NEW, OrderStatus.FILLED])
def test_execution_engine_cancel_leaves_alone_an_order_the_poll_resolved_meanwhile(
    db_engine, settings, filters_provider, cancel_status,
):
    """/emergency_stop runs in a Telegram task and can interleave with the
    monitor loop's poll. If the poll resolves the order while this cancel
    is waiting on Binance, the poll's caller applies its fills. cancel()
    must not return those fills again, overwrite the stored FILLED with a
    network-blip NEW, or start tracking the order again. Each of those
    would apply the fill twice or undo the resolution."""
    _seed_order("bot-cancel-race")
    filled = _result_with_fills(OrderStatus.FILLED, "t1")
    cancel_result = (
        filled if cancel_status == OrderStatus.FILLED
        else ExecutionResult(accepted=False, status=OrderStatus.NEW, error_message="timed out")
    )

    class _PollResolvesDuringCancel(_ScriptedExecutor):
        async def cancel(self, symbol, *, client_order_id):
            self.poll_resolved = await engine.check_pending_limit_orders()
            return await super().cancel(symbol, client_order_id=client_order_id)

    executor = _PollResolvesDuringCancel([filled], cancel_results=[cancel_result])
    engine = ExecutionEngine(executor=executor, filters_provider=filters_provider, settings=settings, dry_run=False)
    engine._pending_limit_orders["bot-cancel-race"] = ("SOLUSDT", utcnow())

    result = asyncio.run(engine.cancel("SOLUSDT", client_order_id="bot-cancel-race"))

    assert [cid for _s, cid, _r in executor.poll_resolved] == ["bot-cancel-race"]  # the poll owns applying it
    assert result.fills == []
    assert result.filled_quantity == result.net_base_quantity == Decimal("0")
    assert _stored_status("bot-cancel-race") == OrderStatus.FILLED
    assert "bot-cancel-race" not in engine._pending_limit_orders
    assert _stored_trade_ids("bot-cancel-race") == ["t1"]

@pytest.mark.parametrize("cancel_status", [OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED])
def test_timeout_cancel_that_cannot_be_confirmed_keeps_the_order_under_polling(
    db_engine, settings, filters_provider, cancel_status,
):
    """The poll's cancel-on-timeout used to untrack and persist an unconfirmed
    cancel (network outage, key/IP rejection): a NEW row nothing polled, so
    has_resting_order() silently froze that position's exits and DCA until a
    restart. It must stay tracked, unpersisted, and not be reported resolved."""
    _seed_order("bot-timeout-blip")
    unknown = ExecutionResult(accepted=False, status=OrderStatus.NEW, error_message="timed out")
    fallback = ExecutionResult(accepted=False, status=cancel_status, error_message="cancel failed")
    executor = _ScriptedExecutor([unknown], cancel_results=[fallback])
    engine = ExecutionEngine(executor=executor, filters_provider=filters_provider, settings=settings, dry_run=False)
    placed_at = utcnow() - timedelta(seconds=settings.limit_order_timeout_seconds + 60)
    engine._pending_limit_orders["bot-timeout-blip"] = ("SOLUSDT", placed_at)

    resolved = asyncio.run(engine.check_pending_limit_orders())

    assert resolved == []
    assert engine._pending_limit_orders.get("bot-timeout-blip") == ("SOLUSDT", placed_at)
    assert _stored_status("bot-timeout-blip") == OrderStatus.NEW


def test_poll_result_is_dropped_when_a_cancel_resolved_the_order_while_the_poll_waited(
    db_engine, settings, filters_provider,
):
    """process_resolved_orders polls Binance outside its lock, so a
    force-close can cancel the same order and apply its fill in between. The
    poll's own result must then be dropped at commit: applying it as well
    would count the fill twice."""
    _seed_order("bot-poll-race")
    filled = _result_with_fills(OrderStatus.FILLED, "t1")
    executor = _ScriptedExecutor([filled], cancel_results=[filled])
    engine = ExecutionEngine(executor=executor, filters_provider=filters_provider, settings=settings, dry_run=False)
    engine._pending_limit_orders["bot-poll-race"] = ("SOLUSDT", utcnow())

    polled = asyncio.run(engine.poll_pending_limit_orders())

    assert [cid for _s, cid, _r in polled] == ["bot-poll-race"]
    assert _stored_status("bot-poll-race") == OrderStatus.NEW  # the network half persists nothing

    cancelled = asyncio.run(engine.cancel("SOLUSDT", client_order_id="bot-poll-race"))
    assert cancelled.filled_quantity == Decimal("1")  # the force-close applies this one

    assert engine.commit_resolution("bot-poll-race", polled[0][2]) is False
    assert _stored_status("bot-poll-race") == OrderStatus.FILLED
    assert _stored_trade_ids("bot-poll-race") == ["t1"]
    assert "bot-poll-race" not in engine._pending_limit_orders


def test_resolved_order_stays_under_polling_when_recording_it_fails(db_engine, settings, filters_provider):
    """The order used to be untracked before its result was written. A DB
    error in between (sqlite "database is locked") left a NEW row nothing
    polled, freezing that position's exits and DCA until a restart."""
    _seed_order("bot-persist-fails")
    executor = _ScriptedExecutor([_result_with_fills(OrderStatus.FILLED, "t1")])
    engine = ExecutionEngine(executor=executor, filters_provider=filters_provider, settings=settings, dry_run=False)
    engine._pending_limit_orders["bot-persist-fails"] = ("SOLUSDT", utcnow())
    real_persist = engine._persist_result

    def locked(order_id, result):
        raise RuntimeError("database is locked")

    engine._persist_result = locked
    assert asyncio.run(engine.check_pending_limit_orders()) == []
    assert "bot-persist-fails" in engine._pending_limit_orders
    assert _stored_status("bot-persist-fails") == OrderStatus.NEW

    engine._persist_result = real_persist
    resolved = asyncio.run(engine.check_pending_limit_orders())

    assert [cid for _s, cid, _r in resolved] == ["bot-persist-fails"]
    assert _stored_status("bot-persist-fails") == OrderStatus.FILLED
    assert "bot-persist-fails" not in engine._pending_limit_orders
