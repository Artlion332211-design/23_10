from __future__ import annotations

import asyncio
from decimal import Decimal

from database.models import OrderPurpose, OrderSide, OrderStatus, OrderType
from exchange.execution_engine import ExecutionEngine, OrderRequest
from exchange.symbol_filters import SymbolFilters
from paper.simulator import PaperBroker, base_asset_of


def test_base_asset_of_strips_quote_suffix():
    assert base_asset_of("SOLUSDT") == "SOL"
    assert base_asset_of("ETHBTC") == "ETH"
    assert base_asset_of("BTCUSDT") == "BTC"


def _price_source(price: Decimal):
    async def _source(symbol: str) -> Decimal:
        return price
    return _source


def test_market_buy_fills_with_slippage_and_commission(settings):
    tuned = settings.model_copy(update={"expected_slippage_percent": Decimal("0.1"), "taker_fee_rate": Decimal("0.001")})
    broker = PaperBroker(tuned, _price_source(Decimal("100")), starting_balance=Decimal("1000"))

    request = OrderRequest(symbol="SOLUSDT", side=OrderSide.BUY, order_type=OrderType.MARKET, client_order_id="c1", quote_amount=Decimal("100"))
    result = asyncio.run(broker.submit(request))

    assert result.accepted and result.status == OrderStatus.FILLED
    expected_fill_price = Decimal("100") * Decimal("1.001")
    assert result.avg_fill_price == expected_fill_price
    assert broker.account.usdt_balance == Decimal("900")
    assert broker.account.holdings["SOL"] == result.net_base_quantity
    assert result.net_base_quantity < result.filled_quantity  # commission deducted from base asset


def test_market_buy_rejected_when_insufficient_balance(settings):
    broker = PaperBroker(settings, _price_source(Decimal("100")), starting_balance=Decimal("50"))
    request = OrderRequest(symbol="SOLUSDT", side=OrderSide.BUY, order_type=OrderType.MARKET, client_order_id="c1", quote_amount=Decimal("100"))
    result = asyncio.run(broker.submit(request))
    assert not result.accepted
    assert broker.account.usdt_balance == Decimal("50")  # untouched


def test_market_sell_credits_usdt_net_of_fee(settings):
    tuned = settings.model_copy(update={"expected_slippage_percent": Decimal("0"), "taker_fee_rate": Decimal("0.001")})
    broker = PaperBroker(tuned, _price_source(Decimal("100")), starting_balance=Decimal("0"))
    broker.account.holdings["SOL"] = Decimal("1.0")

    request = OrderRequest(symbol="SOLUSDT", side=OrderSide.SELL, order_type=OrderType.MARKET, client_order_id="c2", quantity=Decimal("1.0"))
    result = asyncio.run(broker.submit(request))

    assert result.accepted and result.status == OrderStatus.FILLED
    assert broker.account.holdings["SOL"] == Decimal("0")
    assert broker.account.usdt_balance == Decimal("100") * (Decimal(1) - Decimal("0.001"))


def test_market_sell_rejected_when_insufficient_holdings(settings):
    broker = PaperBroker(settings, _price_source(Decimal("100")), starting_balance=Decimal("0"))
    request = OrderRequest(symbol="SOLUSDT", side=OrderSide.SELL, order_type=OrderType.MARKET, client_order_id="c3", quantity=Decimal("1.0"))
    result = asyncio.run(broker.submit(request))
    assert not result.accepted


def test_limit_order_rests_then_fills_when_price_crosses(settings):
    prices = {"value": Decimal("110")}

    async def moving_price(symbol: str) -> Decimal:
        return prices["value"]

    broker = PaperBroker(settings, moving_price, starting_balance=Decimal("1000"))
    request = OrderRequest(
        symbol="SOLUSDT", side=OrderSide.BUY, order_type=OrderType.LIMIT, client_order_id="c4",
        quantity=Decimal("1"), limit_price=Decimal("100"),
    )

    result = asyncio.run(broker.submit(request))
    assert result.status == OrderStatus.NEW  # 110 > 100, not marketable yet

    status = asyncio.run(broker.get_status("SOLUSDT", client_order_id="c4"))
    assert status.status == OrderStatus.NEW  # still not marketable

    prices["value"] = Decimal("99")  # price drops through the limit
    status2 = asyncio.run(broker.get_status("SOLUSDT", client_order_id="c4"))
    assert status2.status == OrderStatus.FILLED
    assert status2.avg_fill_price == Decimal("100")  # fills at the limit price, not the crossed market price


def test_cancel_removes_resting_order(settings):
    broker = PaperBroker(settings, _price_source(Decimal("110")), starting_balance=Decimal("1000"))
    request = OrderRequest(
        symbol="SOLUSDT", side=OrderSide.BUY, order_type=OrderType.LIMIT, client_order_id="c5",
        quantity=Decimal("1"), limit_price=Decimal("100"),
    )
    asyncio.run(broker.submit(request))
    cancel_result = asyncio.run(broker.cancel("SOLUSDT", client_order_id="c5"))
    assert cancel_result.status == OrderStatus.CANCELED

    status = asyncio.run(broker.get_status("SOLUSDT", client_order_id="c5"))
    assert not status.accepted  # order no longer tracked


def test_paper_broker_integrates_with_real_execution_engine(db_engine, settings):
    """PaperBroker must satisfy the OrderExecutor protocol well enough for
    the real ExecutionEngine (rounding, DB persistence, DRY_RUN gating) -
    not just work in isolation."""
    sol_filters = SymbolFilters(
        symbol="SOLUSDT", base_asset="SOL", quote_asset="USDT", status="TRADING",
        tick_size=Decimal("0.01"), min_price=Decimal("0"), max_price=Decimal("100000"),
        lot_step_size=Decimal("0.00001"), lot_min_qty=Decimal("0.00001"), lot_max_qty=Decimal("1000000"),
        market_lot_step_size=Decimal("0.00001"), market_lot_min_qty=Decimal("0.00001"), market_lot_max_qty=Decimal("1000000"),
        min_notional=Decimal("5"), apply_min_notional_to_market=True, base_asset_precision=8, quote_asset_precision=8,
    )

    async def filters_provider(symbol: str) -> SymbolFilters:
        return sol_filters

    broker = PaperBroker(settings, _price_source(Decimal("142.53")), starting_balance=Decimal("1000"))
    engine = ExecutionEngine(executor=broker, filters_provider=filters_provider, settings=settings, dry_run=False)

    result = asyncio.run(engine.buy(
        symbol="SOLUSDT", usdt_amount=Decimal("100"), reference_price=Decimal("142.53"),
        spread_percent=Decimal("0.05"), purpose=OrderPurpose.ENTRY, position_id=None,
    ))
    assert result.status == OrderStatus.FILLED
    assert result.order_id is not None
    assert broker.account.usdt_balance == Decimal("900")
    assert broker.account.holdings["SOL"] > 0
