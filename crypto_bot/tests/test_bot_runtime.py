from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from database.models import OrderPurpose, OrderSide, OrderType
from database.repository import OrderRepository, PositionRepository
from database.session import session_scope
from orchestration.runtime import BotRuntime
from risk.risk_manager import RiskManager
from utils.time import utcnow


def _make_runtime(settings, rules, **overrides):
    defaults = dict(
        settings=settings, rules=rules, client=MagicMock(), ws_manager=MagicMock(),
        market_data=MagicMock(), universe_scanner=MagicMock(), risk_manager=RiskManager(settings),
        news_engine=MagicMock(), execution_engine=MagicMock(), strategy_engine=MagicMock(),
        notifier=MagicMock(), watchdog=MagicMock(), paper_broker=None, started_at=utcnow(),
    )
    defaults.update(overrides)
    return BotRuntime(**defaults)


def test_evaluate_entry_never_double_opens_a_symbol_with_an_open_position(db_engine, settings, rules):
    with session_scope() as session:
        PositionRepository(session).create(
            symbol="SOLUSDT", opened_at=utcnow(), avg_entry_price=Decimal("100"),
            total_quantity=Decimal("1"), total_cost_usdt=Decimal("100"), target_price=Decimal("110"),
        )
    strategy_engine = MagicMock()
    strategy_engine.try_open_position = AsyncMock()
    runtime = _make_runtime(settings, rules, strategy_engine=strategy_engine)

    asyncio.run(runtime._evaluate_entry("SOLUSDT"))

    strategy_engine.try_open_position.assert_not_called()


def test_evaluate_entry_skips_when_emergency_stop_is_active(db_engine, settings, rules):
    risk_manager = RiskManager(settings)
    risk_manager.trigger_emergency_stop()
    strategy_engine = MagicMock()
    strategy_engine.try_open_position = AsyncMock()
    runtime = _make_runtime(settings, rules, risk_manager=risk_manager, strategy_engine=strategy_engine)

    asyncio.run(runtime._evaluate_entry("SOLUSDT"))

    strategy_engine.try_open_position.assert_not_called()


def test_evaluate_entry_skips_when_an_entry_order_is_already_resting(db_engine, settings, rules):
    """A resting entry LIMIT order has no Position yet (only created on an
    actual fill), so get_open_position_for_symbol alone doesn't stop a
    second candle-close from submitting a duplicate entry while the first
    is still resolving."""
    with session_scope() as session:
        OrderRepository(session).create(
            position_id=None, symbol="SOLUSDT", client_order_id="bot-entry-resting",
            side=OrderSide.BUY, type=OrderType.LIMIT, purpose=OrderPurpose.ENTRY,
            requested_price=Decimal("100"), requested_qty=Decimal("1"), requested_usdt=Decimal("100"),
        )
    strategy_engine = MagicMock()
    strategy_engine.try_open_position = AsyncMock()
    runtime = _make_runtime(settings, rules, strategy_engine=strategy_engine)

    asyncio.run(runtime._evaluate_entry("SOLUSDT"))

    strategy_engine.try_open_position.assert_not_called()


def test_evaluate_entry_respects_max_open_positions(db_engine, settings, rules):
    tuned = settings.model_copy(update={"max_open_positions": 1})
    with session_scope() as session:
        PositionRepository(session).create(
            symbol="ETHUSDT", opened_at=utcnow(), avg_entry_price=Decimal("2000"),
            total_quantity=Decimal("0.1"), total_cost_usdt=Decimal("200"), target_price=Decimal("220"),
        )
    strategy_engine = MagicMock()
    strategy_engine.try_open_position = AsyncMock()
    runtime = _make_runtime(tuned, rules, strategy_engine=strategy_engine)

    asyncio.run(runtime._evaluate_entry("SOLUSDT"))  # a different symbol, but the cap is already full

    strategy_engine.try_open_position.assert_not_called()


def test_get_balance_text_paper_mode_reports_holdings_and_equity(db_engine, settings, rules):
    paper_broker = MagicMock()
    paper_broker.account.usdt_balance = Decimal("9000")
    paper_broker.account.holdings = {"SOL": Decimal("2")}
    paper_broker.account.total_equity.return_value = Decimal("9200")
    runtime = _make_runtime(settings, rules, paper_broker=paper_broker)

    text = asyncio.run(runtime.get_balance_text())

    assert "БАЛАНС (PAPER)" in text
    assert "9000.00" in text
    assert "SOL: 2.000000" in text
    assert "9200.00" in text


def test_health_snapshot_reports_watchdog_and_websocket_state(settings, rules):
    watchdog = MagicMock()
    watchdog.snapshot.return_value = {"position_monitor": {"running": True, "restart_count": 0}}
    ws_manager = MagicMock()
    ws_manager.is_connected.return_value = True
    ws_manager.last_message_age_seconds.return_value = 3.2
    runtime = _make_runtime(settings, rules, watchdog=watchdog, ws_manager=ws_manager)

    snapshot = runtime.get_health_snapshot()

    assert snapshot["websocket_klines_connected"] is True
    assert snapshot["last_kline_age_s"] == 3.2
    assert snapshot["tasks"] == {"position_monitor": {"running": True, "restart_count": 0}}


def _mock_snapshot_with_close(close: float):
    snap = MagicMock()
    snap.close = close
    return snap


def test_get_balance_text_live_mode_shows_total_usdt_value(settings, rules):
    client = MagicMock()
    client.get_account_balances = AsyncMock(return_value={
        "USDT": (Decimal("500"), Decimal("0")),
        "SOL": (Decimal("2"), Decimal("0")),
    })
    market_data = MagicMock()
    market_data.snapshot.side_effect = lambda symbol, tf: _mock_snapshot_with_close(100.0) if symbol == "SOLUSDT" else None
    runtime = _make_runtime(settings, rules, client=client, market_data=market_data, paper_broker=None)
    runtime._tracked_symbols = {"SOLUSDT"}

    text = asyncio.run(runtime.get_balance_text())

    assert "БАЛАНС (LIVE)" in text
    assert "SOL: вільно=2" in text
    assert "Загалом приблизно: 700.00 USDT" in text


def test_build_status_snapshot_reports_unrealized_pnl(db_engine, settings, rules):
    with session_scope() as session:
        PositionRepository(session).create(
            symbol="SOLUSDT", opened_at=utcnow(), avg_entry_price=Decimal("100"),
            total_quantity=Decimal("1"), total_cost_usdt=Decimal("100"), target_price=Decimal("110"),
        )
    market_data = MagicMock()
    market_data.snapshot.side_effect = lambda symbol, tf: _mock_snapshot_with_close(106.0)
    runtime = _make_runtime(settings, rules, market_data=market_data)
    runtime._tracked_symbols = {"SOLUSDT"}

    snapshot = runtime.build_status_snapshot()

    assert snapshot.open_positions_count == 1
    assert snapshot.total_unrealized_pnl_usdt == Decimal("6")
    assert snapshot.max_open_positions == settings.max_open_positions
