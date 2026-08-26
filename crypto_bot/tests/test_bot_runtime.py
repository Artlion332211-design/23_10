from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from database.repository import PositionRepository
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

    assert "BALANCE (PAPER)" in text
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
