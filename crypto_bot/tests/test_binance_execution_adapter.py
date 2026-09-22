from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from database.models import OrderStatus
from exchange.execution_engine import BinanceExecutionAdapter
from exchange.symbol_filters import SymbolFilters


@pytest.fixture
def sol_filters() -> SymbolFilters:
    return SymbolFilters(
        symbol="SOLUSDT", base_asset="SOL", quote_asset="USDT", status="TRADING",
        tick_size=Decimal("0.01"), min_price=Decimal("0"), max_price=Decimal("100000"),
        lot_step_size=Decimal("0.001"), lot_min_qty=Decimal("0.001"), lot_max_qty=Decimal("1000000"),
        market_lot_step_size=Decimal("0.00001"), market_lot_min_qty=Decimal("0.00001"), market_lot_max_qty=Decimal("1000000"),
        min_notional=Decimal("5"), apply_min_notional_to_market=True, base_asset_precision=8, quote_asset_precision=8,
    )


class FakeBinanceClient:
    """Mimics the raw dict shapes real Binance REST responses actually have:
    only the order-*placement* response carries `fills`; GET/DELETE .../order
    never do (see BinanceExecutionAdapter._resolve_fills's docstring)."""

    def __init__(self, filters: SymbolFilters):
        self._filters = filters
        self.my_trades_calls: list[tuple[str, str]] = []
        self.order_status_raw: dict = {}
        self.my_trades_raw: list[dict] = []
        self.my_trades_should_raise = False

    async def get_symbol_filters(self, symbol: str, *, force_refresh: bool = False) -> SymbolFilters:
        return self._filters

    async def get_order_status(self, symbol: str, *, order_id=None, orig_client_order_id=None) -> dict:
        return self.order_status_raw

    async def cancel_order(self, **kwargs) -> dict:
        return self.order_status_raw

    async def get_my_trades(self, symbol: str, *, order_id: str) -> list[dict]:
        self.my_trades_calls.append((symbol, order_id))
        if self.my_trades_should_raise:
            from binance.exceptions import BinanceRequestException
            raise BinanceRequestException("transient network error")
        return self.my_trades_raw


def test_get_status_recovers_fill_data_via_my_trades_when_missing_from_order_response(sol_filters):
    """GET /api/v3/order never includes `fills` on real Binance, even when
    the order genuinely filled - this is the bug the review caught: without
    the myTrades fallback, get_status() would report status=FILLED with
    net_base_quantity=0, which is exactly the input
    check_pending_limit_orders()/reconcile_pending_order() use to decide a
    resting LIMIT order can be turned into a tracked Position."""
    client = FakeBinanceClient(sol_filters)
    client.order_status_raw = {
        "orderId": 555, "status": "FILLED", "executedQty": "1.5", "cummulativeQuoteQty": "150.375",
    }
    client.my_trades_raw = [
        {"id": 1, "price": "100.00", "qty": "1.0", "commission": "0.001", "commissionAsset": "SOL", "time": 1700000000000},
        {"id": 2, "price": "100.75", "qty": "0.5", "commission": "0.0005", "commissionAsset": "SOL", "time": 1700000001000},
    ]
    adapter = BinanceExecutionAdapter(client)  # type: ignore[arg-type]

    result = asyncio.run(adapter.get_status("SOLUSDT", client_order_id="bot-entry-abc"))

    assert result.status == OrderStatus.FILLED
    assert result.filled_quantity == Decimal("1.5")
    assert result.net_base_quantity == Decimal("1.5") - Decimal("0.0015")  # gross minus base-asset commission
    assert result.filled_quote == Decimal("100.00") * Decimal("1.0") + Decimal("100.75") * Decimal("0.5")
    assert client.my_trades_calls == [("SOLUSDT", "555")]


def test_get_status_skips_my_trades_when_nothing_executed(sol_filters):
    client = FakeBinanceClient(sol_filters)
    client.order_status_raw = {"orderId": 555, "status": "NEW", "executedQty": "0"}
    adapter = BinanceExecutionAdapter(client)  # type: ignore[arg-type]

    result = asyncio.run(adapter.get_status("SOLUSDT", client_order_id="bot-entry-abc"))

    assert result.status == OrderStatus.NEW
    assert result.filled_quantity == Decimal("0")
    assert client.my_trades_calls == []


def test_get_status_uses_placement_response_fills_directly_without_extra_call(sol_filters):
    """The one Binance response shape that *does* carry `fills` (an
    order-placement response) must not trigger a redundant myTrades call."""
    client = FakeBinanceClient(sol_filters)
    client.order_status_raw = {
        "orderId": 555, "status": "FILLED", "executedQty": "1.0", "cummulativeQuoteQty": "100.0",
        "fills": [{"price": "100.0", "qty": "1.0", "commission": "0.001", "commissionAsset": "SOL", "tradeId": 1}],
    }
    adapter = BinanceExecutionAdapter(client)  # type: ignore[arg-type]

    result = asyncio.run(adapter.get_status("SOLUSDT", client_order_id="bot-entry-abc"))

    assert result.filled_quantity == Decimal("1.0")
    assert client.my_trades_calls == []


def test_get_status_treats_my_trades_failure_as_unresolved_not_zero_fill(sol_filters):
    """A transient failure fetching the real fill breakdown must not look
    identical to 'nothing filled' - callers (check_pending_limit_orders)
    rely on filled_quantity<=0 plus status to decide whether to retry."""
    client = FakeBinanceClient(sol_filters)
    client.order_status_raw = {"orderId": 555, "status": "FILLED", "executedQty": "1.5"}
    client.my_trades_should_raise = True
    adapter = BinanceExecutionAdapter(client)  # type: ignore[arg-type]

    result = asyncio.run(adapter.get_status("SOLUSDT", client_order_id="bot-entry-abc"))

    assert result.status == OrderStatus.FILLED
    assert result.filled_quantity == Decimal("0")  # caller (ExecutionEngine) is what retries on this combination
