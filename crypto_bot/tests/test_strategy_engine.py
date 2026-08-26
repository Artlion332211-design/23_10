from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from database.models import OrderStatus
from database.repository import PositionRepository
from database.session import session_scope
from exchange.execution_engine import ExecutionEngine, ExecutionFill, ExecutionResult
from exchange.symbol_filters import SymbolFilters
from market.market_regime import RegimeAssessment, RegimeLevel
from market.orderbook import OrderBookSnapshot
from risk.risk_manager import RiskManager
from strategy.signal_engine import SignalEngine
from strategy.strategy_engine import NewsAssessment, StrategyEngine
from tests.conftest import make_snapshot
from utils.time import Timeframe, utcnow


class FakeMarketDataStore:
    def __init__(self):
        self._snaps = {}

    def set_snapshots(self, symbol, m15, h1, h4):
        self._snaps[(symbol, Timeframe.M15)] = m15
        self._snaps[(symbol, Timeframe.H1)] = h1
        self._snaps[(symbol, Timeframe.H4)] = h4

    def snapshot(self, symbol, tf):
        return self._snaps.get((symbol, tf))

    def dataframe(self, symbol, tf):
        return None


class FakeNewsProvider:
    async def get_symbol_news_score(self, symbol):
        return NewsAssessment(score=0, critical=False, headlines=[])


class FakeExecutor:
    """Fills every order instantly at `self.price` - a simple stand-in for
    Binance that still exercises the real ExecutionEngine (rounding, DRY_RUN
    gating, DB persistence) end to end."""

    def __init__(self):
        self.price = Decimal("100")

    async def submit(self, request):
        price = self.price
        qty = (request.quote_amount / price) if request.quote_amount else request.quantity
        is_buy = request.side.value == "BUY"
        commission = qty * Decimal("0.001")
        fill = ExecutionFill(
            price=price, quantity=qty, commission=commission,
            commission_asset="SOL" if is_buy else "USDT",
            commission_usdt_equivalent=commission * price if is_buy else commission,
            trade_id="t", timestamp=utcnow(),
        )
        net_base = qty - commission if is_buy else qty
        return ExecutionResult(
            accepted=True, status=OrderStatus.FILLED, exchange_order_id="1", fills=[fill],
            avg_fill_price=price, filled_quantity=qty, net_base_quantity=net_base,
            filled_quote=qty * price, commission_total_usdt_equivalent=fill.commission_usdt_equivalent,
        )

    async def cancel(self, symbol, *, client_order_id):
        return ExecutionResult(accepted=True, status=OrderStatus.CANCELED)

    async def get_status(self, symbol, *, client_order_id):
        return ExecutionResult(accepted=True, status=OrderStatus.NEW)


class SpyNotifier:
    def __init__(self):
        self.events: list[tuple] = []

    async def on_buy_signal(self, decision):
        self.events.append(("buy_signal", decision.action))

    async def on_no_trade(self, decision):
        self.events.append(("no_trade", tuple(decision.reasons)))

    async def on_buy_executed(self, event):
        self.events.append(("buy_executed", event.price, event.quantity, event.target_price))

    async def on_dca_signal(self, decision):
        self.events.append(("dca_signal", decision.action))

    async def on_dca_executed(self, event):
        self.events.append(("dca_executed", event.price, event.new_avg_entry, event.new_target_price))

    async def on_position_closed(self, event):
        self.events.append(("position_closed", event.close_reason, event.net_pnl_usdt, event.net_pnl_percent))

    async def on_error(self, message):
        self.events.append(("error", message))


@pytest.fixture
def bullish_snapshots():
    h1 = make_snapshot(
        Timeframe.H1, rsi=32.0, rsi_prev=28.0, rsi_reversal=True, macd_bullish=True, ema_trend_ok=True,
        bb_recovery=True, volume_confirmation=True, vwap_recovery=True, market_structure_bullish=True,
    )
    m15 = make_snapshot(Timeframe.M15, rsi=35.0, rsi_reversal=True)
    h4 = make_snapshot(
        Timeframe.H4, close=100.0, ema_fast=99.0, ema_mid=97.0, ema_slow=90.0, rsi=58.0, macd_hist=0.2,
        adx=28.0, plus_di=26.0, minus_di=12.0,
    )
    return m15, h1, h4


@pytest.fixture
def strategy_setup(db_engine, settings, rules, bullish_snapshots):
    tuned = settings.model_copy(update={
        "initial_order_usdt": Decimal("100"), "max_position_usdt": Decimal("300"),
        "max_open_positions": 3, "max_total_exposure_percent": Decimal("50"),
        "max_daily_new_capital_usdt": Decimal("1000"), "target_profit_percent": Decimal("10"),
        "dca_level_1": Decimal("-3"), "dca_size_1_usdt": Decimal("50"),
    })

    market_data = FakeMarketDataStore()
    m15, h1, h4 = bullish_snapshots
    market_data.set_snapshots("SOLUSDT", m15, h1, h4)

    sol_filters = SymbolFilters(
        symbol="SOLUSDT", base_asset="SOL", quote_asset="USDT", status="TRADING",
        tick_size=Decimal("0.01"), min_price=Decimal("0"), max_price=Decimal("100000"),
        lot_step_size=Decimal("0.00001"), lot_min_qty=Decimal("0.00001"), lot_max_qty=Decimal("1000000"),
        market_lot_step_size=Decimal("0.00001"), market_lot_min_qty=Decimal("0.00001"), market_lot_max_qty=Decimal("1000000"),
        min_notional=Decimal("5"), apply_min_notional_to_market=True, base_asset_precision=8, quote_asset_precision=8,
    )

    async def filters_provider(symbol):
        return sol_filters

    executor = FakeExecutor()
    execution_engine = ExecutionEngine(executor=executor, filters_provider=filters_provider, settings=tuned, dry_run=False)
    signal_engine = SignalEngine(tuned, rules)
    risk_manager = RiskManager(tuned)
    notifier = SpyNotifier()

    strategy = StrategyEngine(
        settings=tuned, rules=rules, signal_engine=signal_engine, risk_manager=risk_manager,
        execution_engine=execution_engine, market_data=market_data, news_provider=FakeNewsProvider(),
        notifier=notifier,
    )
    book = OrderBookSnapshot(
        symbol="SOLUSDT", best_bid=Decimal("99.99"), best_ask=Decimal("100.01"),
        bid_depth_usdt=Decimal("50000"), ask_depth_usdt=Decimal("50000"),
    )
    return strategy, executor, notifier, book


def test_full_buy_dca_take_profit_lifecycle(strategy_setup):
    strategy, executor, notifier, book = strategy_setup
    neutral = RegimeAssessment(level=RegimeLevel.NEUTRAL, score=0, reasons=[], crash=False)

    # 1. BUY
    decision = asyncio.run(strategy.try_open_position(
        "SOLUSDT", btc_regime=neutral, trading_balance_usdt=Decimal("10000"), order_book=book
    ))
    assert decision.action == "BUY"

    with session_scope() as session:
        position = PositionRepository(session).get_open_position_for_symbol("SOLUSDT")
        position_id = position.id
        assert position.dca_count == 0
        assert position.avg_entry_price == Decimal("100")

    # 2. Price drops 3% -> DCA fires
    executor.price = Decimal("97")
    asyncio.run(strategy.manage_position(position_id, btc_regime=neutral, current_price=Decimal("97"), order_book=book))

    with session_scope() as session:
        position = PositionRepository(session).get(position_id)
        assert position.dca_count == 1
        assert position.avg_entry_price < Decimal("100")
        target = position.target_price

    # 3. Price recovers past target -> take-profit close
    executor.price = target + Decimal("0.5")
    asyncio.run(strategy.manage_position(position_id, btc_regime=neutral, current_price=executor.price, order_book=book))

    with session_scope() as session:
        position = PositionRepository(session).get(position_id)
        assert position.status.value == "CLOSED"
        assert position.close_reason == "TAKE_PROFIT"
        assert position.realized_pnl_usdt > 0
        assert position.realized_pnl_pct >= Decimal("9.5")

    event_types = [e[0] for e in notifier.events]
    assert event_types == ["buy_signal", "buy_executed", "dca_signal", "dca_executed", "position_closed"]


def test_crash_regime_blocks_new_entry(strategy_setup):
    strategy, executor, notifier, book = strategy_setup
    crash = RegimeAssessment(level=RegimeLevel.CRASH, score=-100, reasons=["crash"], crash=True)

    decision = asyncio.run(strategy.try_open_position(
        "SOLUSDT", btc_regime=crash, trading_balance_usdt=Decimal("10000"), order_book=book
    ))
    assert decision.action == "BLOCKED"
    with session_scope() as session:
        assert PositionRepository(session).get_open_position_for_symbol("SOLUSDT") is None
