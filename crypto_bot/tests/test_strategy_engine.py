from __future__ import annotations

import asyncio
import dataclasses
import enum
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from database.models import (
    OrderPurpose,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
    SignalDecision,
)
from database.repository import (
    OrderRepository,
    PositionRepository,
    SettingsRepository,
    SignalRepository,
)
from database.session import session_scope
from exchange.execution_engine import ExecutionEngine, ExecutionFill, ExecutionResult
from exchange.symbol_filters import SymbolFilters
from market.market_regime import RegimeAssessment, RegimeLevel
from market.orderbook import OrderBookSnapshot
from risk.risk_manager import RiskManager
from strategy.signal_engine import SignalEngine
from strategy.strategy_engine import NewsAssessment, StrategyEngine, in_manual_sell_cooldown
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

    def is_stale(self, symbol, tf, max_age_seconds):
        return False


class FakeNewsProvider:
    async def get_symbol_news_score(self, symbol):
        return NewsAssessment(score=0, critical=False, headlines=[])


class FakeExecutor:
    """Fills every order instantly at `self.price` - a simple stand-in for
    Binance that still exercises the real ExecutionEngine (rounding, DRY_RUN
    gating, DB persistence) end to end."""

    def __init__(self):
        self.price = Decimal("100")
        self.resting = False  # when True, submit() leaves the order resting (accepted, zero fill) instead of filling
        self.submit_calls = 0
        self.sides: list[str] = []
        self.delay = 0.0  # seconds each submit() stays in flight (lets a test interleave another task)

    async def submit(self, request):
        self.submit_calls += 1
        self.sides.append(request.side.value)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.resting:
            return ExecutionResult(accepted=True, status=OrderStatus.NEW, exchange_order_id="resting")
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

    async def on_delayed_fill(self, event):
        self.events.append(("delayed_fill", event.purpose, event.quantity))

    async def on_drawdown_warning(self, event):
        self.events.append(("drawdown_warning", event.drawdown_percent))

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
        # This fixture's scenarios test the plain target-price exit path
        # directly (jump straight to target+0.5 and expect an immediate
        # full close) - pinned explicitly, not left to whatever the
        # ambient .env happens to have EARLY_PROFIT_PROTECTION_ENABLED set
        # to, since that changes the exit mechanism entirely (arms a
        # trailing-stop at 9.5% instead of closing at target).
        "early_profit_protection_enabled": False,
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
        # Cost basis and quantity must be homogeneous (both net-of-commission)
        # so a later DCA's weighted-average recompute isn't skewed.
        assert position.total_cost_usdt == position.avg_entry_price * position.total_quantity

    # 2. Price drops 3% -> DCA fires
    executor.price = Decimal("97")
    asyncio.run(strategy.manage_position(position_id, btc_regime=neutral, current_price=Decimal("97"), order_book=book, trading_balance_usdt=Decimal("10000")))

    with session_scope() as session:
        position = PositionRepository(session).get(position_id)
        assert abs(position.total_cost_usdt - position.avg_entry_price * position.total_quantity) < Decimal("1e-8")
        assert position.dca_count == 1
        assert position.avg_entry_price < Decimal("100")
        target = position.target_price

    # 3. Price recovers past target -> take-profit close
    executor.price = target + Decimal("0.5")
    asyncio.run(strategy.manage_position(position_id, btc_regime=neutral, current_price=executor.price, order_book=book, trading_balance_usdt=Decimal("10000")))

    with session_scope() as session:
        position = PositionRepository(session).get(position_id)
        assert position.status.value == "CLOSED"
        assert position.close_reason == "TAKE_PROFIT"
        assert position.realized_pnl_usdt > 0
        assert position.realized_pnl_pct >= Decimal("9.5")

    event_types = [e[0] for e in notifier.events]
    assert event_types == ["buy_signal", "buy_executed", "dca_signal", "dca_executed", "position_closed"]


def test_manage_position_does_not_resubmit_while_an_order_is_still_resting(strategy_setup):
    """A LIMIT order can rest for up to LIMIT_ORDER_TIMEOUT_SECONDS (90s by
    default), longer than the position-monitor poll interval (60s by
    default) - manage_position must not submit a second DCA order for the
    same level while the first is still unresolved."""
    strategy, executor, notifier, book = strategy_setup
    neutral = RegimeAssessment(level=RegimeLevel.NEUTRAL, score=0, reasons=[], crash=False)

    decision = asyncio.run(strategy.try_open_position(
        "SOLUSDT", btc_regime=neutral, trading_balance_usdt=Decimal("10000"), order_book=book
    ))
    assert decision.action == "BUY"
    with session_scope() as session:
        position_id = PositionRepository(session).get_open_position_for_symbol("SOLUSDT").id

    executor.price = Decimal("97")  # a -3% move -> DCA level 1 triggers
    executor.resting = True
    submits_before = executor.submit_calls

    asyncio.run(strategy.manage_position(position_id, btc_regime=neutral, current_price=Decimal("97"), order_book=book, trading_balance_usdt=Decimal("10000")))
    assert executor.submit_calls == submits_before + 1  # the DCA buy was submitted once and now rests

    with session_scope() as session:
        position = PositionRepository(session).get(position_id)
        assert position.dca_count == 0  # unfilled - no fill was ever applied

    # Same triggering price/condition on the next poll tick - must not
    # submit a second DCA order while the first is still resting.
    asyncio.run(strategy.manage_position(position_id, btc_regime=neutral, current_price=Decimal("97"), order_book=book, trading_balance_usdt=Decimal("10000")))
    assert executor.submit_calls == submits_before + 1


def test_hard_profit_ceiling_force_closes_even_with_a_resting_order_present(strategy_setup):
    """HARD_PROFIT_CEILING_PERCENT (12% by default in the fixture's tuned
    settings' inherited default) must override even a still-resting order
    for the position - normally has_resting_order alone would make
    manage_position return early and do nothing."""
    strategy, executor, notifier, book = strategy_setup
    neutral = RegimeAssessment(level=RegimeLevel.NEUTRAL, score=0, reasons=[], crash=False)

    decision = asyncio.run(strategy.try_open_position(
        "SOLUSDT", btc_regime=neutral, trading_balance_usdt=Decimal("10000"), order_book=book
    ))
    assert decision.action == "BUY"
    with session_scope() as session:
        position_id = PositionRepository(session).get_open_position_for_symbol("SOLUSDT").id
        # Simulate some other order already resting against this position
        # (e.g. a DCA LIMIT order placed moments earlier) - on its own this
        # would make manage_position return early via has_resting_order,
        # but the hard ceiling must force a close anyway.
        OrderRepository(session).create(
            position_id=position_id, symbol="SOLUSDT", client_order_id="bot-dca-stuck",
            side=OrderSide.BUY, type=OrderType.LIMIT, purpose=OrderPurpose.DCA_1,
            requested_price=Decimal("97"), requested_qty=Decimal("1"), requested_usdt=Decimal("97"),
        )

    executor.price = Decimal("115")  # +15% price move, net profit well past the 12% ceiling after fees/slippage
    asyncio.run(strategy.manage_position(position_id, btc_regime=neutral, current_price=executor.price, order_book=book, trading_balance_usdt=Decimal("10000")))

    with session_scope() as session:
        position = PositionRepository(session).get(position_id)
        assert position.status.value == "CLOSED"
        assert position.close_reason == "HARD_PROFIT_CEILING"
        stuck = OrderRepository(session).get_by_client_id("bot-dca-stuck")
        assert stuck.status.value == "CANCELED"


def test_manage_position_sends_drawdown_warnings_once_at_20_and_30_percent(strategy_setup):
    strategy, executor, notifier, book = strategy_setup
    neutral = RegimeAssessment(level=RegimeLevel.NEUTRAL, score=0, reasons=[], crash=False)
    strategy._risk_manager.stop_dca()  # isolate the warning check from DCA execution at these deep drops

    decision = asyncio.run(strategy.try_open_position(
        "SOLUSDT", btc_regime=neutral, trading_balance_usdt=Decimal("10000"), order_book=book
    ))
    assert decision.action == "BUY"
    with session_scope() as session:
        position_id = PositionRepository(session).get_open_position_for_symbol("SOLUSDT").id

    # -25% - crosses the 20% threshold but not 30%
    asyncio.run(strategy.manage_position(position_id, btc_regime=neutral, current_price=Decimal("75"), order_book=book, trading_balance_usdt=Decimal("10000")))
    warnings = [e for e in notifier.events if e[0] == "drawdown_warning"]
    assert warnings == [("drawdown_warning", Decimal("25"))]

    # still -25% on the next tick - must not re-alert the same threshold
    asyncio.run(strategy.manage_position(position_id, btc_regime=neutral, current_price=Decimal("75"), order_book=book, trading_balance_usdt=Decimal("10000")))
    warnings = [e for e in notifier.events if e[0] == "drawdown_warning"]
    assert warnings == [("drawdown_warning", Decimal("25"))]

    # -35% - now also crosses 30%
    asyncio.run(strategy.manage_position(position_id, btc_regime=neutral, current_price=Decimal("65"), order_book=book, trading_balance_usdt=Decimal("10000")))
    warnings = [e for e in notifier.events if e[0] == "drawdown_warning"]
    assert warnings == [("drawdown_warning", Decimal("25")), ("drawdown_warning", Decimal("35"))]

    with session_scope() as session:
        position = PositionRepository(session).get(position_id)
        assert position.drawdown_alert_20_sent is True
        assert position.drawdown_alert_30_sent is True


def test_emergency_liquidate_all_closes_positions_and_reports_failures(strategy_setup):
    strategy, executor, notifier, book = strategy_setup
    neutral = RegimeAssessment(level=RegimeLevel.NEUTRAL, score=0, reasons=[], crash=False)

    decision = asyncio.run(strategy.try_open_position(
        "SOLUSDT", btc_regime=neutral, trading_balance_usdt=Decimal("10000"), order_book=book
    ))
    assert decision.action == "BUY"

    failed = asyncio.run(strategy.emergency_liquidate_all(
        order_books={"SOLUSDT": book}, btc_regime=neutral,
    ))

    assert failed == []
    with session_scope() as session:
        position = PositionRepository(session).get_open_position_for_symbol("SOLUSDT")
        assert position is None  # fully closed
        closed = PositionRepository(session).recent_closed(limit=1)[0]
        assert closed.close_reason == "EMERGENCY_SELL"


def test_crash_regime_blocks_new_entry(strategy_setup):
    strategy, executor, notifier, book = strategy_setup
    crash = RegimeAssessment(level=RegimeLevel.CRASH, score=-100, reasons=["crash"], crash=True)

    decision = asyncio.run(strategy.try_open_position(
        "SOLUSDT", btc_regime=crash, trading_balance_usdt=Decimal("10000"), order_book=book
    ))
    assert decision.action == "BLOCKED"
    with session_scope() as session:
        assert PositionRepository(session).get_open_position_for_symbol("SOLUSDT") is None

def test_partially_filled_order_is_not_applied_at_submit_time():
    """Its cumulative fills are applied once when it resolves; applying the
    early slice at submit too would double-count it (double DCA, a sell
    reducing the position twice)."""
    from database.models import OrderStatus
    from exchange.execution_engine import ExecutionResult
    from strategy.strategy_engine import _still_resting

    partial = ExecutionResult(accepted=True, status=OrderStatus.PARTIALLY_FILLED, net_base_quantity=Decimal("0.4"))
    filled = ExecutionResult(accepted=True, status=OrderStatus.FILLED, net_base_quantity=Decimal("1"))
    resting = ExecutionResult(accepted=True, status=OrderStatus.NEW)

    assert _still_resting(partial)
    assert _still_resting(resting)
    assert not _still_resting(filled)


NEUTRAL = RegimeAssessment(level=RegimeLevel.NEUTRAL, score=0, reasons=[], crash=False)
BALANCE = Decimal("10000")


def _open_position(strategy, book) -> int:
    decision = asyncio.run(strategy.try_open_position(
        "SOLUSDT", btc_regime=NEUTRAL, trading_balance_usdt=BALANCE, order_book=book
    ))
    assert decision.action == "BUY"
    with session_scope() as session:
        return PositionRepository(session).get_open_position_for_symbol("SOLUSDT").id


def _arm_trailing(position_id, *, peak, is_early=False):
    with session_scope() as session:
        p = PositionRepository(session).get(position_id)
        PositionRepository(session).set_trailing(p, active=True, peak_price=peak, is_early=is_early)


def _order(position_id, purpose, client_order_id, *, side=OrderSide.SELL):
    with session_scope() as session:
        return OrderRepository(session).create(
            position_id=position_id, symbol="SOLUSDT", client_order_id=client_order_id,
            side=side, type=OrderType.LIMIT, purpose=purpose,
            requested_price=Decimal("108"), requested_qty=Decimal("0.4"), requested_usdt=Decimal("43.2"),
        )


def _fill(*, price=Decimal("108"), qty=Decimal("0.4"), status=OrderStatus.CANCELED):
    """A resolved order's fill; CANCELED by default = a LIMIT that timed out partially filled."""
    return ExecutionResult(
        accepted=True, status=status, avg_fill_price=price, filled_quantity=qty, net_base_quantity=qty,
        filled_quote=price * qty, commission_total_usdt_equivalent=Decimal("0.04"),
    )


def _errors(notifier):
    return [e[1] for e in notifier.events if e[0] == "error"]


def test_trailing_stop_exit_is_forced_to_market_even_on_a_wide_spread(strategy_setup):
    """A trailing-stop exit fires into a falling price; on the book's own
    wide spread ExecutionEngine used to place a passive LIMIT at mid that
    can sit unfilled while the price keeps dropping."""
    strategy, executor, notifier, book = strategy_setup
    position_id = _open_position(strategy, book)
    _arm_trailing(position_id, peak=Decimal("110"))
    wide = OrderBookSnapshot(
        symbol="SOLUSDT", best_bid=Decimal("98"), best_ask=Decimal("102"),  # ~4% spread -> LIMIT if not forced
        bid_depth_usdt=Decimal("50000"), ask_depth_usdt=Decimal("50000"),
    )

    asyncio.run(strategy.manage_position(
        position_id, btc_regime=NEUTRAL, current_price=Decimal("100"), order_book=wide, trading_balance_usdt=BALANCE,
    ))

    with session_scope() as session:
        exits = [o for o in OrderRepository(session).for_position(position_id) if o.purpose == OrderPurpose.TRAILING_STOP]
        assert [o.type for o in exits] == [OrderType.MARKET]
        assert PositionRepository(session).get(position_id).close_reason == "TRAILING_STOP"


def test_partial_trailing_stop_fill_leaves_the_armed_trail_untouched(strategy_setup):
    """Re-arming after a partial TRAILING_STOP fill reset trailing_is_early
    and dropped the peak to the fill price - an early 1% trail silently
    became a 2.5% trail from a lower price."""
    strategy, executor, notifier, book = strategy_setup
    position_id = _open_position(strategy, book)
    _arm_trailing(position_id, peak=Decimal("110"), is_early=True)
    order = _order(position_id, OrderPurpose.TRAILING_STOP, "bot-trail-partial")

    asyncio.run(strategy.apply_resolved_order(order, _fill(), btc_regime=NEUTRAL))

    with session_scope() as session:
        p = PositionRepository(session).get(position_id)
        assert p.status == PositionStatus.OPEN
        assert p.total_quantity < Decimal("0.999")  # the partial fill itself was applied
        assert (p.trailing_active, p.trailing_peak_price, p.trailing_is_early) == (True, Decimal("110"), True)
    assert [e[0] for e in notifier.events].count("delayed_fill") == 1


@pytest.mark.parametrize("purpose", [OrderPurpose.EMERGENCY_SELL, OrderPurpose.HARD_CEILING])
def test_partial_forced_close_fill_does_not_arm_trailing(strategy_setup, purpose):
    strategy, executor, notifier, book = strategy_setup
    position_id = _open_position(strategy, book)
    order = _order(position_id, purpose, f"bot-{purpose.value}-partial")

    asyncio.run(strategy.apply_resolved_order(order, _fill(), btc_regime=NEUTRAL))

    with session_scope() as session:
        p = PositionRepository(session).get(position_id)
        assert p.status == PositionStatus.OPEN
        assert p.trailing_active is False
    assert [e[0] for e in notifier.events].count("delayed_fill") == 1


@pytest.mark.parametrize("use_trailing_after_tp", [True, False])
def test_partial_take_profit_fill_arms_trailing_only_with_trailing_after_tp(strategy_setup, use_trailing_after_tp):
    strategy, executor, notifier, book = strategy_setup
    strategy._settings = strategy._settings.model_copy(update={"use_trailing_after_tp": use_trailing_after_tp})
    position_id = _open_position(strategy, book)
    order = _order(position_id, OrderPurpose.TAKE_PROFIT, "bot-tp-partial")

    asyncio.run(strategy.apply_resolved_order(order, _fill(), btc_regime=NEUTRAL))

    with session_scope() as session:
        p = PositionRepository(session).get(position_id)
        assert p.trailing_active is use_trailing_after_tp
        if use_trailing_after_tp:
            assert (p.trailing_peak_price, p.trailing_is_early) == (Decimal("108"), False)


def test_emergency_liquidation_waits_for_a_running_manage_position_instead_of_selling_twice(strategy_setup):
    """/emergency_stop runs in the Telegram handler's task; while the
    monitor's trailing-exit SELL was in flight it read the same OPEN
    quantity and sold it a second time."""
    strategy, executor, notifier, book = strategy_setup
    position_id = _open_position(strategy, book)
    _arm_trailing(position_id, peak=Decimal("110"))
    executor.delay = 0.01

    async def scenario():
        monitor = asyncio.create_task(strategy.manage_position(
            position_id, btc_regime=NEUTRAL, current_price=Decimal("100"), order_book=book, trading_balance_usdt=BALANCE,
        ))
        await asyncio.sleep(0)  # the monitor runs up to its in-flight SELL
        failed = await strategy.emergency_liquidate_all(order_books={"SOLUSDT": book}, btc_regime=NEUTRAL)
        await monitor
        return failed

    failed = asyncio.run(scenario())

    assert executor.sides.count("SELL") == 1
    assert failed == []
    with session_scope() as session:
        assert PositionRepository(session).get(position_id).close_reason == "TRAILING_STOP"


class _UnknownPurpose(str, enum.Enum):
    MYSTERY = "MYSTERY"


def test_resolved_order_with_an_unhandled_purpose_is_reported_not_ignored(strategy_setup):
    strategy, executor, notifier, book = strategy_setup
    order = SimpleNamespace(
        id=1, purpose=_UnknownPurpose.MYSTERY, symbol="SOLUSDT", client_order_id="bot-mystery", position_id=None,
    )

    asyncio.run(strategy.apply_resolved_order(order, _fill(status=OrderStatus.FILLED), btc_regime=NEUTRAL))

    errors = _errors(notifier)
    assert len(errors) == 1
    assert "bot-mystery" in errors[0] and "unhandled purpose (MYSTERY)" in errors[0]


def test_manage_position_without_a_balance_skips_only_dca(strategy_setup):
    """A failed balance fetch used to skip the whole monitor cycle; now the
    runtime passes None, which must block DCA (its exposure cap needs the
    balance) but never an exit."""
    strategy, executor, notifier, book = strategy_setup
    position_id = _open_position(strategy, book)
    submits_before = executor.submit_calls

    asyncio.run(strategy.manage_position(
        position_id, btc_regime=NEUTRAL, current_price=Decimal("97"), order_book=book, trading_balance_usdt=None,
    ))  # -3%: DCA level 1 reached

    assert executor.submit_calls == submits_before
    assert not [e for e in notifier.events if e[0] in ("no_trade", "dca_signal")]
    with session_scope() as session:
        p = PositionRepository(session).get(position_id)
        assert p.dca_count == 0
        target = p.target_price

    executor.price = target + Decimal("0.5")
    asyncio.run(strategy.manage_position(
        position_id, btc_regime=NEUTRAL, current_price=executor.price, order_book=book, trading_balance_usdt=None,
    ))

    with session_scope() as session:
        assert PositionRepository(session).get(position_id).close_reason == "TAKE_PROFIT"


def _no_trade_signals() -> int:
    with session_scope() as session:
        rows = SignalRepository(session).recent(limit=100, symbol="SOLUSDT")
        return sum(1 for s in rows if s.decision == SignalDecision.NO_TRADE)


def test_dca_scoring_runs_once_per_candle_while_price_sits_below_an_unmet_level(strategy_setup, bullish_snapshots):
    """Every 60s tick re-scored the candidate and wrote a NO_TRADE row
    (~40 per candle instead of 25). The scoring is now reused within one
    15m candle, while the cheap gates still run every tick."""
    strategy, executor, notifier, book = strategy_setup
    _m15, h1, h4 = bullish_snapshots

    def set_candle(hour, minute):
        m15 = make_snapshot(
            Timeframe.M15, rsi=35.0, rsi_reversal=True, open_time=datetime(2026, 9, 30, hour, minute, tzinfo=UTC)
        )
        strategy._market_data.set_snapshots("SOLUSDT", m15, h1, h4)

    set_candle(12, 0)
    position_id = _open_position(strategy, book)
    evaluations: list[str] = []
    original_evaluate = strategy.evaluate_candidate

    async def counting_evaluate(symbol, **kwargs):
        evaluations.append(symbol)
        return await original_evaluate(symbol, **kwargs)

    strategy.evaluate_candidate = counting_evaluate
    strategy._risk_manager.stop_dca()  # DCA level reached, but blocked on every tick

    def tick():
        asyncio.run(strategy.manage_position(
            position_id, btc_regime=NEUTRAL, current_price=Decimal("97"), order_book=book, trading_balance_usdt=BALANCE,
        ))

    for _ in range(3):
        tick()
    assert len(evaluations) == 1
    assert _no_trade_signals() == 1
    assert [e[0] for e in notifier.events].count("no_trade") == 1

    set_candle(12, 15)  # a new candle closed -> scored (and recorded) again
    tick()
    assert len(evaluations) == 2
    assert _no_trade_signals() == 2

    strategy._risk_manager.start_dca()  # the gate opens mid-candle: DCA on the cached score, no re-scoring
    tick()
    assert len(evaluations) == 2
    with session_scope() as session:
        assert PositionRepository(session).get(position_id).dca_count == 1
    assert position_id not in strategy._dca_decisions


def test_dca_fill_for_a_position_closed_meanwhile_alerts_about_untracked_coins(strategy_setup):
    strategy, executor, notifier, book = strategy_setup
    position_id = _open_position(strategy, book)
    order = _order(position_id, OrderPurpose.DCA_1, "bot-dca-late", side=OrderSide.BUY)
    with session_scope() as session:  # e.g. force-closed by the hard ceiling while the DCA LIMIT rested
        p = PositionRepository(session).get(position_id)
        PositionRepository(session).close(
            p, closed_at=utcnow(), realized_pnl_usdt=Decimal("0"), realized_pnl_pct=Decimal("0"),
            close_reason="HARD_PROFIT_CEILING",
        )

    asyncio.run(strategy.apply_resolved_order(
        order, _fill(price=Decimal("97"), qty=Decimal("0.5"), status=OrderStatus.FILLED), btc_regime=NEUTRAL,
    ))

    errors = _errors(notifier)
    assert len(errors) == 1
    assert "SOLUSDT" in errors[0] and "NOT tracked" in errors[0]
    with session_scope() as session:
        p = PositionRepository(session).get(position_id)
        assert p.status == PositionStatus.CLOSED
        assert p.dca_count == 0


def test_late_entry_fill_with_a_position_already_open_alerts_about_untracked_coins(strategy_setup):
    strategy, executor, notifier, book = strategy_setup
    position_id = _open_position(strategy, book)
    order = _order(None, OrderPurpose.ENTRY, "bot-entry-late", side=OrderSide.BUY)

    asyncio.run(strategy.apply_resolved_order(
        order, _fill(price=Decimal("100"), qty=Decimal("1"), status=OrderStatus.FILLED), btc_regime=NEUTRAL,
    ))

    errors = _errors(notifier)
    assert len(errors) == 1
    assert "bot-entry-late" in errors[0] and "NOT tracked" in errors[0]
    with session_scope() as session:
        open_positions = PositionRepository(session).get_open_positions()
        assert [p.id for p in open_positions] == [position_id]
        assert open_positions[0].total_quantity == Decimal("0.999")  # the late fill was not folded in


def test_entry_fill_stores_the_entry_score_and_confirmed_signals(strategy_setup):
    strategy, executor, notifier, book = strategy_setup
    decision = asyncio.run(strategy.try_open_position(
        "SOLUSDT", btc_regime=NEUTRAL, trading_balance_usdt=BALANCE, order_book=book
    ))
    assert decision.action == "BUY"

    with session_scope() as session:
        p = PositionRepository(session).get_open_position_for_symbol("SOLUSDT")
        assert p.entry_score == round(decision.breakdown.final_score)
        expected = {s.name: s.points for s in decision.breakdown.signals if s.confirmed}
        assert expected and p.entry_signals == expected

class _UnconfirmedCancelExecutor(FakeExecutor):
    """cancel() can't confirm the order's fate (network blip): Binance may still hold it."""

    async def cancel(self, symbol, *, client_order_id):
        return ExecutionResult(accepted=False, status=OrderStatus.NEW, error_message="timeout")


def _sells(executor) -> int:
    return executor.sides.count("SELL")


def test_hard_ceiling_waits_when_a_resting_sell_cannot_be_confirmed_cancelled(strategy_setup):
    """A resting SELL whose cancel isn't confirmed may still lock the coins on
    Binance - a full-size MARKET sell on top would be rejected or oversell.
    The ceiling close is postponed (and retried next tick) instead."""
    strategy, executor, notifier, book = strategy_setup
    position_id = _open_position(strategy, book)
    _order(position_id, OrderPurpose.TAKE_PROFIT, "bot-tp-resting", side=OrderSide.SELL)
    strategy._execution_engine._executor = _UnconfirmedCancelExecutor()
    unconfirmed = strategy._execution_engine._executor
    unconfirmed.price = Decimal("115")

    asyncio.run(strategy.manage_position(
        position_id, btc_regime=NEUTRAL, current_price=Decimal("115"), order_book=book, trading_balance_usdt=BALANCE,
    ))

    assert _sells(unconfirmed) == 0
    with session_scope() as session:
        assert PositionRepository(session).get(position_id).status.value == "OPEN"
    assert any(e[0] == "error" and "postponed" in e[1] for e in notifier.events)


def test_hard_ceiling_still_sells_when_only_a_resting_dca_buy_is_unconfirmed(strategy_setup):
    """A resting DCA BUY locks USDT, not the coins - the protective sell goes ahead."""
    strategy, executor, notifier, book = strategy_setup
    position_id = _open_position(strategy, book)
    _order(position_id, OrderPurpose.DCA_1, "bot-dca-resting", side=OrderSide.BUY)
    strategy._execution_engine._executor = _UnconfirmedCancelExecutor()
    unconfirmed = strategy._execution_engine._executor
    unconfirmed.price = Decimal("115")

    asyncio.run(strategy.manage_position(
        position_id, btc_regime=NEUTRAL, current_price=Decimal("115"), order_book=book, trading_balance_usdt=BALANCE,
    ))

    assert _sells(unconfirmed) == 1
    with session_scope() as session:
        assert PositionRepository(session).get(position_id).close_reason == "HARD_PROFIT_CEILING"


def test_reused_dca_decision_still_sees_critical_news_that_arrived_mid_candle(strategy_setup, bullish_snapshots):
    """The DCA score is cached per candle, but a critical headline must block
    the DCA straight away, not up to 15 minutes later."""
    strategy, executor, notifier, book = strategy_setup
    _m15, h1, h4 = bullish_snapshots
    m15 = make_snapshot(Timeframe.M15, rsi=35.0, rsi_reversal=True, open_time=datetime(2026, 9, 30, 12, 0, tzinfo=UTC))
    strategy._market_data.set_snapshots("SOLUSDT", m15, h1, h4)
    position_id = _open_position(strategy, book)
    strategy._risk_manager.stop_dca()

    def tick():
        asyncio.run(strategy.manage_position(
            position_id, btc_regime=NEUTRAL, current_price=Decimal("97"), order_book=book, trading_balance_usdt=BALANCE,
        ))

    tick()  # scored and cached (no news yet), DCA blocked by the pause

    class CriticalNews:
        async def get_symbol_news_score(self, symbol):
            return NewsAssessment(score=-100, critical=True, headlines=["SOL exploit"])

    strategy._news_provider = CriticalNews()
    strategy._risk_manager.start_dca()  # everything else now allows the DCA
    tick()

    with session_scope() as session:
        assert PositionRepository(session).get(position_id).dca_count == 0

def test_cached_dca_decision_is_not_reused_once_the_feed_goes_stale(strategy_setup, bullish_snapshots):
    """A dead kline feed freezes the candle id; reusing the cached score would
    DCA on stale indicators, which evaluate_candidate's own veto blocks."""
    strategy, executor, notifier, book = strategy_setup
    _m15, h1, h4 = bullish_snapshots
    m15 = make_snapshot(Timeframe.M15, rsi=35.0, rsi_reversal=True, open_time=datetime(2026, 9, 30, 12, 0, tzinfo=UTC))
    strategy._market_data.set_snapshots("SOLUSDT", m15, h1, h4)
    position_id = _open_position(strategy, book)
    strategy._risk_manager.stop_dca()

    def tick(regime=NEUTRAL):
        asyncio.run(strategy.manage_position(
            position_id, btc_regime=regime, current_price=Decimal("97"), order_book=book, trading_balance_usdt=BALANCE,
        ))

    tick()  # scored on fresh data and cached; DCA blocked by the pause
    strategy._market_data.is_stale = lambda symbol, tf, max_age_seconds: True
    strategy._risk_manager.start_dca()
    tick()

    with session_scope() as session:
        assert PositionRepository(session).get(position_id).dca_count == 0


def test_cached_dca_decision_is_rescored_when_the_btc_regime_changes(strategy_setup, bullish_snapshots):
    strategy, executor, notifier, book = strategy_setup
    _m15, h1, h4 = bullish_snapshots
    m15 = make_snapshot(Timeframe.M15, rsi=35.0, rsi_reversal=True, open_time=datetime(2026, 9, 30, 12, 0, tzinfo=UTC))
    strategy._market_data.set_snapshots("SOLUSDT", m15, h1, h4)
    position_id = _open_position(strategy, book)
    strategy._risk_manager.stop_dca()
    scored: list[str] = []
    original_evaluate = strategy.evaluate_candidate

    async def counting_evaluate(symbol, **kwargs):
        scored.append(kwargs["btc_regime"].level.value)
        return await original_evaluate(symbol, **kwargs)

    strategy.evaluate_candidate = counting_evaluate
    strong_bear = RegimeAssessment(level=RegimeLevel.STRONG_BEAR, score=-60, reasons=[], crash=False)
    for regime in (NEUTRAL, NEUTRAL, strong_bear):
        asyncio.run(strategy.manage_position(
            position_id, btc_regime=regime, current_price=Decimal("97"), order_book=book, trading_balance_usdt=BALANCE,
        ))

    assert scored == ["NEUTRAL", "STRONG_BEAR"]


def test_emergency_liquidation_waits_for_a_running_order_resolution_poll(strategy_setup):
    """The poll persists a resolved order before applying its fill; the kill
    switch must not read the position's quantity in between."""
    strategy, executor, notifier, book = strategy_setup
    _open_position(strategy, book)

    async def scenario():
        await strategy._resolution_lock.acquire()  # a resolution poll is mid-flight
        task = asyncio.create_task(strategy.emergency_liquidate_all(order_books={"SOLUSDT": book}, btc_regime=NEUTRAL))
        await asyncio.sleep(0.05)
        sells_while_polling = _sells(executor)
        strategy._resolution_lock.release()
        await task
        return sells_while_polling

    assert asyncio.run(scenario()) == 0
    assert _sells(executor) == 1


def _count_scoring(strategy) -> list[str]:
    scored: list[str] = []
    original_evaluate = strategy.evaluate_candidate

    async def counting_evaluate(symbol, **kwargs):
        scored.append(symbol)
        return await original_evaluate(symbol, **kwargs)

    strategy.evaluate_candidate = counting_evaluate
    return scored


def _dca_setup(strategy_setup, bullish_snapshots):
    """An open position with price at DCA level 1 and M15 candle ids set, so
    the DCA scoring cache is active."""
    strategy, executor, notifier, book = strategy_setup
    _m15, h1, h4 = bullish_snapshots
    m15 = make_snapshot(Timeframe.M15, rsi=35.0, rsi_reversal=True, open_time=datetime(2026, 9, 30, 12, 0, tzinfo=UTC))
    strategy._market_data.set_snapshots("SOLUSDT", m15, h1, h4)
    position_id = _open_position(strategy, book)

    def tick():
        asyncio.run(strategy.manage_position(
            position_id, btc_regime=NEUTRAL, current_price=Decimal("97"), order_book=book, trading_balance_usdt=BALANCE,
        ))

    return position_id, m15, tick


def test_cached_dca_decision_is_rescored_when_an_h1_candle_closes(strategy_setup, bullish_snapshots):
    """Most confirming signals come from H1/H4; an H1 close in the middle of
    an M15 candle changes them, so the cached score must not outlive it."""
    strategy, executor, notifier, book = strategy_setup
    _m15, h1, h4 = bullish_snapshots
    _position_id, m15, tick = _dca_setup(strategy_setup, bullish_snapshots)
    strategy._risk_manager.stop_dca()
    scored = _count_scoring(strategy)

    tick()
    tick()
    assert len(scored) == 1
    strategy._market_data.set_snapshots(
        "SOLUSDT", m15, dataclasses.replace(h1, open_time=datetime(2026, 9, 30, 12, 0, tzinfo=UTC)), h4,
    )
    tick()

    assert len(scored) == 2


def test_cached_dca_decision_is_rescored_when_the_news_score_changes(strategy_setup, bullish_snapshots):
    """Non-critical news still moves the score by up to -15/+5 points, enough
    to cross the buy threshold either way."""
    strategy, executor, notifier, book = strategy_setup
    _position_id, _m15, tick = _dca_setup(strategy_setup, bullish_snapshots)
    strategy._risk_manager.stop_dca()

    class MutableNews:
        score = 0

        async def get_symbol_news_score(self, symbol):
            return NewsAssessment(score=self.score, critical=False, headlines=[])

    news = MutableNews()
    strategy._news_provider = news
    scored = _count_scoring(strategy)

    tick()
    tick()
    assert len(scored) == 1
    news.score = -60
    tick()
    tick()

    assert len(scored) == 2


def test_stale_data_block_is_not_cached_for_dca(strategy_setup, bullish_snapshots):
    """A 'stale market data' block was cached for the rest of the candle, so
    DCA stayed blocked after the feed recovered."""
    strategy, executor, notifier, book = strategy_setup
    position_id, _m15, tick = _dca_setup(strategy_setup, bullish_snapshots)
    scored = _count_scoring(strategy)

    strategy._market_data.is_stale = lambda symbol, tf, max_age_seconds: True
    tick()
    with session_scope() as session:
        assert PositionRepository(session).get(position_id).dca_count == 0
    strategy._market_data.is_stale = lambda symbol, tf, max_age_seconds: False
    tick()

    assert len(scored) == 2
    with session_scope() as session:
        assert PositionRepository(session).get(position_id).dca_count == 1


def test_force_close_leaves_an_unconfirmed_cancel_to_the_poll_without_a_false_alert(strategy_setup):
    """A cancel that comes back without a complete fill breakdown isn't a final
    answer. Applying it raised "Check Binance manually" for an order the
    regular poll was still handling; now it stays tracked and unapplied."""
    strategy, executor, notifier, book = strategy_setup
    position_id = _open_position(strategy, book)
    _order(position_id, OrderPurpose.DCA_1, "bot-dca-incomplete", side=OrderSide.BUY)

    class IncompleteCancel(FakeExecutor):
        async def cancel(self, symbol, *, client_order_id):
            return ExecutionResult(accepted=True, status=OrderStatus.CANCELED, fill_data_incomplete=True)

    incomplete = IncompleteCancel()
    incomplete.price = Decimal("115")
    strategy._execution_engine._executor = incomplete

    asyncio.run(strategy.manage_position(
        position_id, btc_regime=NEUTRAL, current_price=Decimal("115"), order_book=book, trading_balance_usdt=BALANCE,
    ))

    assert not [e for e in _errors(notifier) if "Check Binance manually" in e]
    assert "bot-dca-incomplete" in strategy._execution_engine._pending_limit_orders
    with session_scope() as session:
        assert OrderRepository(session).get_by_client_id("bot-dca-incomplete").status == OrderStatus.NEW
        assert PositionRepository(session).get(position_id).close_reason == "HARD_PROFIT_CEILING"


def test_emergency_liquidation_reports_a_dca_buy_it_could_not_cancel(strategy_setup):
    """The position is sold, but a DCA BUY that may still be live on Binance
    can refill it; the kill switch must not report a clean liquidation."""
    strategy, executor, notifier, book = strategy_setup
    position_id = _open_position(strategy, book)
    _order(position_id, OrderPurpose.DCA_1, "bot-dca-unconfirmed", side=OrderSide.BUY)
    unconfirmed = _UnconfirmedCancelExecutor()
    strategy._execution_engine._executor = unconfirmed

    failed = asyncio.run(strategy.emergency_liquidate_all(order_books={"SOLUSDT": book}, btc_regime=NEUTRAL))

    assert failed == ["SOLUSDT"]
    assert _sells(unconfirmed) == 1
    assert any("bot-dca-unconfirmed" in e and "manually" in e for e in _errors(notifier))
    with session_scope() as session:
        assert PositionRepository(session).get(position_id).close_reason == "EMERGENCY_SELL"


def test_emergency_liquidation_is_not_held_up_by_a_slow_order_poll(strategy_setup):
    """The resolution lock used to cover the poll's Binance calls, so
    /emergency_stop could wait behind a slow network. The poll now waits
    outside the lock. When it finally returns, the order it asked about has
    already been cancelled and applied by the liquidation, so its result
    must be dropped, not applied a second time."""
    strategy, executor, notifier, book = strategy_setup
    position_id = _open_position(strategy, book)
    _order(position_id, OrderPurpose.TAKE_PROFIT, "bot-tp-slow")
    strategy._execution_engine._pending_limit_orders["bot-tp-slow"] = ("SOLUSDT", utcnow())
    tp_filled = _fill(status=OrderStatus.FILLED)  # 0.4 of the position sold at 108

    class SlowPoll(FakeExecutor):
        release: asyncio.Event

        async def get_status(self, symbol, *, client_order_id):
            await self.release.wait()
            return tp_filled

        async def cancel(self, symbol, *, client_order_id):
            return tp_filled

    slow = SlowPoll()
    strategy._execution_engine._executor = slow

    async def scenario():
        slow.release = asyncio.Event()
        poll = asyncio.create_task(strategy.process_resolved_orders(btc_regime=NEUTRAL))
        await asyncio.sleep(0)  # the poll is now waiting on Binance
        failed = await asyncio.wait_for(
            strategy.emergency_liquidate_all(order_books={"SOLUSDT": book}, btc_regime=NEUTRAL), timeout=1,
        )
        events_before_poll = list(notifier.events)
        slow.release.set()
        await poll
        return failed, events_before_poll

    failed, events_before_poll = asyncio.run(scenario())

    assert failed == []
    assert _sells(slow) == 1
    assert notifier.events == events_before_poll  # the poll's late result changed nothing
    assert not _errors(notifier)
    with session_scope() as session:
        assert PositionRepository(session).get(position_id).close_reason == "EMERGENCY_SELL"
        assert OrderRepository(session).get_by_client_id("bot-tp-slow").status == OrderStatus.FILLED


def test_one_coin_failing_does_not_stop_emergency_liquidation_of_the_others(strategy_setup):
    """An exception while liquidating one position escaped the loop, so
    /emergency_stop never even tried to sell the remaining positions."""
    strategy, executor, notifier, book = strategy_setup
    sol_id = _open_position(strategy, book)
    _order(sol_id, OrderPurpose.TAKE_PROFIT, "bot-tp-boom")
    with session_scope() as session:
        eth_id = PositionRepository(session).create(
            symbol="ETHUSDT", opened_at=utcnow(), avg_entry_price=Decimal("100"), total_quantity=Decimal("0.5"),
            total_cost_usdt=Decimal("50"), target_price=Decimal("110"),
        ).id

    class CancelBlowsUp(FakeExecutor):
        async def cancel(self, symbol, *, client_order_id):
            raise RuntimeError("boom")

    exploding = CancelBlowsUp()
    strategy._execution_engine._executor = exploding

    failed = asyncio.run(strategy.emergency_liquidate_all(
        order_books={"SOLUSDT": book, "ETHUSDT": book}, btc_regime=NEUTRAL,
    ))

    assert failed == ["SOLUSDT"]
    assert any("SOLUSDT" in e and "failed" in e for e in _errors(notifier))
    with session_scope() as session:
        assert PositionRepository(session).get(sol_id).status == PositionStatus.OPEN
        assert PositionRepository(session).get(eth_id).close_reason == "EMERGENCY_SELL"


def _entry_order_usdt(symbol="SOLUSDT"):
    with session_scope() as session:
        orders = [o for o in OrderRepository(session).recent(limit=50) if o.symbol == symbol and o.purpose == OrderPurpose.ENTRY]
        return orders[0].requested_usdt if orders else None


def test_strong_signal_enters_with_the_bigger_amount_only_when_allowed(strategy_setup):
    """Owner 2026-10-02: a strong signal enters bigger (live: 50 USDT instead of 20).
    'Strong' = the score beats what the regime requires by the margin, and the
    caller allows it (the long-term phase is BULL)."""
    strategy, executor, notifier, book = strategy_setup
    strategy._settings = strategy._settings.model_copy(update={
        "strong_signal_order_usdt": Decimal("150"), "max_position_usdt": Decimal("400"), "strong_signal_score_margin": 0.0,
    })

    decision = asyncio.run(strategy.try_open_position(
        "SOLUSDT", btc_regime=NEUTRAL, trading_balance_usdt=BALANCE, order_book=book, strong_size_allowed=True,
    ))

    assert decision.action == "BUY"
    assert strategy.is_strong_signal(decision)
    assert _entry_order_usdt() == Decimal("150")  # the fixture's normal entry is 100


def test_no_bigger_entry_when_the_long_term_phase_does_not_allow_it(strategy_setup):
    strategy, executor, notifier, book = strategy_setup
    strategy._settings = strategy._settings.model_copy(update={
        "strong_signal_order_usdt": Decimal("150"), "max_position_usdt": Decimal("400"), "strong_signal_score_margin": 0.0,
    })

    asyncio.run(strategy.try_open_position(
        "SOLUSDT", btc_regime=NEUTRAL, trading_balance_usdt=BALANCE, order_book=book, strong_size_allowed=False,
    ))

    assert _entry_order_usdt() == strategy._settings.initial_order_usdt


def test_a_score_below_the_margin_is_not_a_strong_signal(strategy_setup):
    strategy, executor, notifier, book = strategy_setup
    strategy._settings = strategy._settings.model_copy(update={
        "strong_signal_order_usdt": Decimal("150"), "max_position_usdt": Decimal("400"), "strong_signal_score_margin": 100.0,
    })

    decision = asyncio.run(strategy.try_open_position(
        "SOLUSDT", btc_regime=NEUTRAL, trading_balance_usdt=BALANCE, order_book=book, strong_size_allowed=True,
    ))

    assert decision.action == "BUY"
    assert not strategy.is_strong_signal(decision)
    assert _entry_order_usdt() == strategy._settings.initial_order_usdt


def test_bigger_entry_that_does_not_fit_the_risk_caps_falls_back_to_the_normal_size(strategy_setup):
    """The bigger size must never cost the trade itself."""
    strategy, executor, notifier, book = strategy_setup
    tight = strategy._settings.model_copy(update={
        "strong_signal_order_usdt": Decimal("150"), "max_position_usdt": Decimal("400"), "strong_signal_score_margin": 0.0,
        "max_daily_new_capital_usdt": Decimal("120"),
    })
    strategy._settings = tight
    strategy._risk_manager = RiskManager(tight)

    decision = asyncio.run(strategy.try_open_position(
        "SOLUSDT", btc_regime=NEUTRAL, trading_balance_usdt=BALANCE, order_book=book, strong_size_allowed=True,
    ))

    assert decision.action == "BUY"
    assert _entry_order_usdt() == tight.initial_order_usdt


def test_manual_sell_market_sells_the_whole_position(strategy_setup):
    strategy, executor, notifier, book = strategy_setup
    position_id = _open_position(strategy, book)

    result = asyncio.run(strategy.manual_sell("SOLUSDT", order_book=book, btc_regime=NEUTRAL))

    assert result.status == "sold"
    with session_scope() as session:
        assert PositionRepository(session).get(position_id).close_reason == "MANUAL_SELL"
        sells = [o for o in OrderRepository(session).for_position(position_id) if o.side == OrderSide.SELL]
        assert [(o.purpose, o.type) for o in sells] == [(OrderPurpose.MANUAL_SELL, OrderType.MARKET)]
    assert [e[1] for e in notifier.events if e[0] == "position_closed"] == ["MANUAL_SELL"]


def test_manual_sell_reports_missing_position_or_prices_and_sells_nothing(strategy_setup):
    strategy, executor, notifier, book = strategy_setup
    _open_position(strategy, book)
    sells_before = _sells(executor)

    assert asyncio.run(strategy.manual_sell("ETHUSDT", order_book=book, btc_regime=NEUTRAL)).status == "no_position"
    assert asyncio.run(strategy.manual_sell("SOLUSDT", order_book=None, btc_regime=NEUTRAL)).status == "no_order_book"
    assert _sells(executor) == sells_before


def test_manual_sell_refuses_while_a_resting_sell_cannot_be_confirmed_cancelled(strategy_setup):
    """Same guard as the kill switch: Binance may still hold the coins in that
    order, so a full-size market sell on top could oversell."""
    strategy, executor, notifier, book = strategy_setup
    position_id = _open_position(strategy, book)
    _order(position_id, OrderPurpose.TAKE_PROFIT, "bot-tp-unconfirmed", side=OrderSide.SELL)
    unconfirmed = _UnconfirmedCancelExecutor()
    strategy._execution_engine._executor = unconfirmed

    result = asyncio.run(strategy.manual_sell("SOLUSDT", order_book=book, btc_regime=NEUTRAL))
    assert result.status == "failed"
    assert "висить ордер на продаж" in (result.detail or "")

    assert _sells(unconfirmed) == 0
    assert any("Manual sell for SOLUSDT skipped" in e for e in _errors(notifier))
    with session_scope() as session:
        assert PositionRepository(session).get(position_id).status == PositionStatus.OPEN


class _HalfFilledSellExecutor(FakeExecutor):
    """A MARKET SELL that Binance cuts short on a thin book: EXPIRED after half fills."""

    async def submit(self, request):
        result = await super().submit(request)
        if request.side.value != "SELL":
            return result
        half = result.filled_quantity / 2
        return dataclasses.replace(
            result, status=OrderStatus.EXPIRED, filled_quantity=half, net_base_quantity=half,
            filled_quote=half * result.avg_fill_price,
            commission_total_usdt_equivalent=result.commission_total_usdt_equivalent / 2,
        )


def test_manual_sell_that_only_partly_fills_is_reported_as_partial_not_sold(strategy_setup):
    """Telling the owner 'sold' while half the coins stayed in the position
    (and the bot kept managing them, DCA included) was the review's main find."""
    strategy, executor, notifier, book = strategy_setup
    position_id = _open_position(strategy, book)
    strategy._execution_engine._executor = _HalfFilledSellExecutor()

    result = asyncio.run(strategy.manual_sell("SOLUSDT", order_book=book, btc_regime=NEUTRAL))

    assert result.status == "partial"
    assert result.remaining is not None and result.remaining > 0
    with session_scope() as session:
        assert PositionRepository(session).get(position_id).status == PositionStatus.OPEN
    # The kill switch must not count such a symbol as cleanly liquidated either.
    assert asyncio.run(strategy.emergency_liquidate_all(order_books={"SOLUSDT": book}, btc_regime=NEUTRAL)) == ["SOLUSDT"]


def test_a_manual_sell_at_a_loss_does_not_count_toward_the_loss_streak_pause(strategy_setup):
    """Three /sell at a loss used to pause all buying silently (MAX_CONSECUTIVE_BAD_TRADES=3)."""
    strategy, executor, notifier, book = strategy_setup
    for _ in range(3):
        executor.price = Decimal("100")
        _open_position(strategy, book)
        executor.price = Decimal("95")
        assert asyncio.run(strategy.manual_sell("SOLUSDT", order_book=book, btc_regime=NEUTRAL)).status == "sold"

    flags = strategy._risk_manager.status()
    assert flags.consecutive_bad_trades == 0
    assert not flags.buy_paused


class _LostResponseSellExecutor(FakeExecutor):
    """The MARKET SELL reached Binance but its response was lost (timeout/5xx)."""

    async def submit(self, request):
        if request.side.value != "SELL":
            return await super().submit(request)
        self.sides.append("SELL")
        return ExecutionResult(accepted=False, status=OrderStatus.NEW, error_message="read timeout")


def test_manual_sell_with_a_lost_response_is_reported_pending_and_its_late_fill_starts_the_cooldown(strategy_setup):
    """A lost response used to read "біржа відхилила продаж" although the
    order may well have filled; and the no-re-buy cooldown was only written
    by the /sell call itself, so a fill the poll resolved later never set it."""
    strategy, executor, notifier, book = strategy_setup
    position_id = _open_position(strategy, book)
    strategy._execution_engine._executor = _LostResponseSellExecutor()

    result = asyncio.run(strategy.manual_sell("SOLUSDT", order_book=book, btc_regime=NEUTRAL))

    assert result.status == "pending"  # not "❌ не виконано": it may well have filled
    assert "ще не підтвердила" in (result.detail or "")
    assert "відхилила" not in (result.detail or "")
    assert not in_manual_sell_cooldown("SOLUSDT")

    with session_scope() as session:  # the order poll later learns that it filled
        order = next(o for o in OrderRepository(session).for_position(position_id) if o.purpose == OrderPurpose.MANUAL_SELL)
        quantity = PositionRepository(session).get(position_id).total_quantity
    filled = _fill(price=Decimal("100"), qty=quantity, status=OrderStatus.FILLED)
    asyncio.run(strategy.apply_resolved_order(order, filled, btc_regime=NEUTRAL))

    with session_scope() as session:
        assert PositionRepository(session).get(position_id).close_reason == "MANUAL_SELL"
    assert in_manual_sell_cooldown("SOLUSDT")


@pytest.mark.parametrize(("answer", "expected"), [
    (ExecutionResult(accepted=True, status=OrderStatus.EXPIRED, exchange_order_id="2"), "нічого не продано"),
    (ExecutionResult(accepted=False, status=OrderStatus.REJECTED, error_message="LOT_SIZE"), "відхилила продаж: LOT_SIZE"),
])
def test_manual_sell_that_sold_nothing_says_why(strategy_setup, answer, expected):
    """An EXPIRED market sell with no fill (no buyers) used to be reported as
    "біржа ще не підтвердила" - as if a result were still coming."""
    strategy, executor, notifier, book = strategy_setup
    position_id = _open_position(strategy, book)

    class NothingSold(FakeExecutor):
        async def submit(self, request):
            if request.side.value != "SELL":
                return await super().submit(request)
            return answer

    strategy._execution_engine._executor = NothingSold()

    result = asyncio.run(strategy.manual_sell("SOLUSDT", order_book=book, btc_regime=NEUTRAL))

    assert result.status == "failed"
    assert expected in (result.detail or "")
    assert "ще не підтвердила" not in (result.detail or "")
    assert not in_manual_sell_cooldown("SOLUSDT")
    with session_scope() as session:
        assert PositionRepository(session).get(position_id).status == PositionStatus.OPEN


def test_no_dca_into_what_is_left_after_the_owner_sold_by_hand(strategy_setup, bullish_snapshots):
    """A /sell that Binance filled only partly leaves a position behind;
    averaging down into it would undo the owner's decision."""
    strategy, executor, notifier, book = strategy_setup
    _position_id, _m15, tick = _dca_setup(strategy_setup, bullish_snapshots)
    with session_scope() as session:
        SettingsRepository(session).set("manual_sell_at:SOLUSDT", utcnow().isoformat())

    tick()
    assert executor.sides.count("BUY") == 1  # only the entry

    with session_scope() as session:  # cooldown over -> the DCA goes ahead
        SettingsRepository(session).set("manual_sell_at:SOLUSDT", "2000-01-01T00:00:00+00:00")
    tick()
    assert executor.sides.count("BUY") == 2


def test_bear_gate_blocks_the_entry_but_the_signal_is_still_recorded(strategy_setup):
    """The 25-signals-per-candle health check must keep working in a bear."""
    strategy, executor, notifier, book = strategy_setup

    decision = asyncio.run(strategy.try_open_position(
        "SOLUSDT", btc_regime=NEUTRAL, trading_balance_usdt=BALANCE, order_book=book,
        entry_block_reason="bear market",
    ))

    assert decision.action == "BLOCKED"
    assert decision.reasons == ["bear market"]
    assert executor.submit_calls == 0
    with session_scope() as session:
        assert len(SignalRepository(session).recent(symbol="SOLUSDT")) == 1


def test_entry_brings_its_usdt_back_from_earn_before_buying(strategy_setup):
    strategy, executor, notifier, book = strategy_setup
    asked: list[Decimal] = []

    async def funds(amount):
        asked.append(amount)
        assert executor.submit_calls == 0  # before the order, not after
        return True

    strategy._funds_provider = funds
    position_id = _open_position(strategy, book)

    assert asked == [strategy._settings.initial_order_usdt]
    assert position_id is not None


@pytest.mark.parametrize("failure", ["short", "raises"])
def test_entry_is_skipped_when_earn_cannot_fund_it(strategy_setup, failure):
    strategy, executor, notifier, book = strategy_setup

    async def funds(amount):
        if failure == "raises":
            raise ConnectionError("sapi down")
        return False

    strategy._funds_provider = funds
    decision = asyncio.run(strategy.try_open_position(
        "SOLUSDT", btc_regime=NEUTRAL, trading_balance_usdt=BALANCE, order_book=book
    ))

    assert decision.action == "BLOCKED"
    assert executor.submit_calls == 0
    assert any("entry buy for SOLUSDT skipped" in e for e in _errors(notifier))


def test_dca_is_skipped_when_earn_cannot_fund_it(strategy_setup, bullish_snapshots):
    strategy, executor, notifier, book = strategy_setup
    _position_id, _m15, tick = _dca_setup(strategy_setup, bullish_snapshots)
    asked: list[Decimal] = []

    async def funds(amount):
        asked.append(amount)
        return False

    strategy._funds_provider = funds
    tick()
    tick()

    assert asked == [strategy._settings.dca_size_1_usdt] * 2
    assert executor.sides.count("BUY") == 1  # only the entry
    assert any("DCA buy for SOLUSDT skipped" in e for e in _errors(notifier))
    # No "DCA signal" message or DCA signal row for a DCA that can't be paid for.
    assert not [e for e in notifier.events if e[0] == "dca_signal"]
    with session_scope() as session:
        assert all(s.decision.value != "DCA" for s in SignalRepository(session).recent(symbol="SOLUSDT"))


def test_emergency_stop_during_an_earn_redemption_stops_the_entry(strategy_setup):
    """The risk check ran before the redemption's seconds of waiting; a kill
    switch pressed meanwhile let the entry through after "all sold"."""
    strategy, executor, notifier, book = strategy_setup

    async def slow_funds(amount):
        strategy._risk_manager.trigger_emergency_stop()  # pressed while we wait for Earn
        return True

    strategy._funds_provider = slow_funds
    decision = asyncio.run(strategy.try_open_position(
        "SOLUSDT", btc_regime=NEUTRAL, trading_balance_usdt=BALANCE, order_book=book
    ))

    assert decision.action == "BLOCKED"
    assert executor.submit_calls == 0


def test_dca_pause_during_an_earn_redemption_stops_the_dca(strategy_setup, bullish_snapshots):
    strategy, executor, notifier, book = strategy_setup
    _position_id, _m15, tick = _dca_setup(strategy_setup, bullish_snapshots)

    async def slow_funds(amount):
        strategy._risk_manager.stop_dca()
        return True

    strategy._funds_provider = slow_funds
    tick()

    assert executor.sides.count("BUY") == 1  # only the entry
