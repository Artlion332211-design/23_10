from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from database.models import OrderPurpose, OrderSide, OrderType
from database.repository import OrderRepository, PositionRepository
from database.session import session_scope
from orchestration.runtime import BotRuntime
from risk.risk_manager import RiskManager
from utils.time import Timeframe, utcnow


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


def test_monitor_open_positions_isolates_one_bad_symbol_from_the_rest(db_engine, settings, rules):
    """_monitor_open_positions wraps each open position's management in its
    own try/except specifically so one bad symbol (here: a failed order-
    book fetch) can never block managing every other open position that
    cycle - this was previously never exercised by any test."""
    with session_scope() as session:
        PositionRepository(session).create(
            symbol="BADUSDT", opened_at=utcnow(), avg_entry_price=Decimal("100"),
            total_quantity=Decimal("1"), total_cost_usdt=Decimal("100"), target_price=Decimal("110"),
        )
        PositionRepository(session).create(
            symbol="GOODUSDT", opened_at=utcnow(), avg_entry_price=Decimal("50"),
            total_quantity=Decimal("1"), total_cost_usdt=Decimal("50"), target_price=Decimal("55"),
        )
    strategy_engine = MagicMock()
    strategy_engine.process_resolved_orders = AsyncMock()
    strategy_engine.manage_position = AsyncMock()
    paper_broker = MagicMock()
    paper_broker.account.usdt_balance = Decimal("10000")
    notifier = MagicMock()
    notifier.on_error = AsyncMock()
    notifier.mark_exchange_ok = AsyncMock()
    runtime = _make_runtime(
        settings, rules, strategy_engine=strategy_engine, paper_broker=paper_broker, notifier=notifier
    )

    good_book = MagicMock()
    good_book.is_empty = False
    good_book.mid_price = Decimal("50")

    async def fake_get_order_book(symbol):
        if symbol == "BADUSDT":
            raise RuntimeError("simulated order-book fetch failure")
        return good_book

    runtime._get_order_book = fake_get_order_book

    asyncio.run(runtime._monitor_open_positions())

    assert strategy_engine.manage_position.call_count == 1  # GOODUSDT still got managed despite BADUSDT failing


def test_monitor_open_positions_still_manages_positions_when_the_balance_fetch_fails(db_engine, settings, rules):
    """The balance fetch sat outside the per-position try: one failed
    /account call skipped managing every position that cycle (no hard
    ceiling, trailing or take-profit). Now each position is managed with
    trading_balance_usdt=None, which skips only DCA."""
    with session_scope() as session:
        for symbol in ("SOLUSDT", "ETHUSDT"):
            PositionRepository(session).create(
                symbol=symbol, opened_at=utcnow(), avg_entry_price=Decimal("100"),
                total_quantity=Decimal("1"), total_cost_usdt=Decimal("100"), target_price=Decimal("110"),
            )
    strategy_engine = MagicMock()
    strategy_engine.process_resolved_orders = AsyncMock()
    strategy_engine.manage_position = AsyncMock()
    client = MagicMock()
    client.get_account_balances = AsyncMock(side_effect=RuntimeError("simulated /account failure"))
    notifier = MagicMock()
    notifier.on_error = AsyncMock()
    notifier.mark_exchange_ok = AsyncMock()
    runtime = _make_runtime(
        settings, rules, client=client, strategy_engine=strategy_engine, notifier=notifier, paper_broker=None
    )
    book = MagicMock()
    book.is_empty = False
    book.mid_price = Decimal("100")
    runtime._get_order_book = AsyncMock(return_value=book)

    asyncio.run(runtime._monitor_open_positions())

    assert strategy_engine.manage_position.await_count == 2
    assert all(c.kwargs["trading_balance_usdt"] is None for c in strategy_engine.manage_position.await_args_list)
    notifier.on_error.assert_awaited_once()
    assert "simulated /account failure" in notifier.on_error.await_args.args[0]
    notifier.mark_exchange_ok.assert_not_awaited()


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


def test_candle_close_enqueues_entry_evaluation_instead_of_running_it_inline(settings, rules):
    """A candle close must not await the (slow) entry evaluation inside the
    kline read loop - doing so for ~25 symbols overflowed python-binance's
    message queue on a real run and dropped most symbols' candle closes."""
    market_data = MagicMock()
    market_data.apply_kline_message.side_effect = [
        ("SOLUSDT", Timeframe.M15, True), ("ETHUSDT", Timeframe.M15, True), ("SOLUSDT", Timeframe.M15, True),
    ]
    runtime = _make_runtime(settings, rules, market_data=market_data)
    runtime._candidate_symbols = {"SOLUSDT", "ETHUSDT"}
    runtime._evaluate_entry = AsyncMock()

    async def scenario():
        for _ in range(3):
            await runtime._on_kline_message({})
        runtime._evaluate_entry.assert_not_called()
        worker = asyncio.create_task(runtime.run_entry_evaluation_loop())
        await asyncio.sleep(0.05)
        worker.cancel()

    asyncio.run(scenario())

    evaluated = [call.args[0] for call in runtime._evaluate_entry.call_args_list]
    assert evaluated == ["SOLUSDT", "ETHUSDT"]  # in order, and a duplicate close isn't queued twice


def test_entry_worker_skips_symbols_dropped_from_the_universe_while_queued(settings, rules):
    runtime = _make_runtime(settings, rules)
    runtime._candidate_symbols = {"SOLUSDT"}
    runtime._evaluate_entry = AsyncMock()
    runtime._enqueue_entry_evaluation("SOLUSDT")
    runtime._candidate_symbols = set()  # rescan dropped it before the worker got to it

    async def scenario():
        worker = asyncio.create_task(runtime.run_entry_evaluation_loop())
        await asyncio.sleep(0.05)
        worker.cancel()

    asyncio.run(scenario())

    runtime._evaluate_entry.assert_not_called()


def test_entry_worker_survives_an_evaluation_exception(settings, rules):
    """_evaluate_entry's DB pre-checks sit outside its own try - an error
    there (e.g. sqlite "database is locked") must not kill the worker and
    burn one of the watchdog's lifetime restarts."""
    runtime = _make_runtime(settings, rules)
    runtime._candidate_symbols = {"BADUSDT", "SOLUSDT"}
    calls: list[str] = []

    async def flaky_evaluate(symbol):
        calls.append(symbol)
        if symbol == "BADUSDT":
            raise RuntimeError("database is locked")

    runtime._evaluate_entry = flaky_evaluate
    runtime._enqueue_entry_evaluation("BADUSDT")
    runtime._enqueue_entry_evaluation("SOLUSDT")

    async def scenario():
        worker = asyncio.create_task(runtime.run_entry_evaluation_loop())
        await asyncio.sleep(0.05)
        assert not worker.done()
        worker.cancel()

    asyncio.run(scenario())

    assert calls == ["BADUSDT", "SOLUSDT"]


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
    market_data.live_price.return_value = None  # no stream tick yet -> falls back to the closed candle
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
    market_data.live_price.return_value = None
    ws_manager = MagicMock()
    ws_manager.seconds_disconnected.return_value = None
    ws_manager.last_message_age_seconds.return_value = 1.0
    watchdog = MagicMock()
    watchdog.snapshot.return_value = {"position_monitor": _task()}
    runtime = _make_runtime(settings, rules, market_data=market_data, ws_manager=ws_manager, watchdog=watchdog)
    runtime._tracked_symbols = {"SOLUSDT"}
    runtime._initialized = True

    snapshot = runtime.build_status_snapshot()

    assert snapshot.open_positions_count == 1
    assert snapshot.total_unrealized_pnl_usdt == Decimal("6")
    assert snapshot.max_open_positions == settings.max_open_positions
    assert snapshot.problems == ()


def _task(*, running=True, restarts=0, gave_up=False, heartbeat=5.0):
    return {"running": running, "restart_count": restarts, "gave_up": gave_up, "seconds_since_heartbeat": heartbeat}


def _health_runtime(settings, rules, *, down_for=None, kline_age=1.0, tasks=None):
    ws_manager = MagicMock()
    ws_manager.seconds_disconnected.return_value = down_for
    ws_manager.last_message_age_seconds.return_value = kline_age
    watchdog = MagicMock()
    watchdog.snapshot.return_value = tasks or {}
    return _make_runtime(settings, rules, ws_manager=ws_manager, watchdog=watchdog)


def test_status_problems_report_a_real_feed_outage_and_a_task_given_up(settings, rules):
    runtime = _health_runtime(settings, rules, down_for=185.0, tasks={
        "entry_evaluator": _task(running=False, restarts=3, gave_up=True),
    })

    assert runtime._status_problems() == [
        "немає зв'язку з біржею вже 3 хв, ціни не надходять",
        "зупинилась задача \"пошук входів\"",
    ]


def test_status_problems_ignore_a_brief_planned_reconnect(settings, rules):
    """A universe rescan swaps the kline stream (1-3s disconnected) - a
    status landing in that gap must not report an outage."""
    assert _health_runtime(settings, rules, down_for=2.0)._status_problems() == []


def test_status_problems_ignore_a_task_that_already_recovered(settings, rules):
    """restart_count is lifetime; one recovered crash used to make every
    later status say ПРОБЛЕМА until the next process restart."""
    runtime = _health_runtime(settings, rules, tasks={"news_refresh": _task(restarts=1)})
    assert runtime._status_problems() == []


def test_status_problems_report_a_stale_feed_and_a_hung_position_monitor(settings, rules):
    runtime = _health_runtime(settings, rules, kline_age=250.0, tasks={
        "position_monitor": _task(heartbeat=900.0),
        "daily_report": _task(heartbeat=40_000.0),  # legitimately sleeps for hours
    })

    assert runtime._status_problems() == [
        "ціни не оновлювались 4 хв",
        "задача \"супровід позицій\" не відповідає 15 хв",
    ]


def test_status_snapshot_says_starting_until_initialize_finishes(db_engine, settings, rules):
    runtime = _health_runtime(settings, rules, down_for=None, kline_age=None)
    runtime._ws_manager.is_connected.return_value = False

    snapshot = runtime.build_status_snapshot()

    assert snapshot.starting
    assert snapshot.problems == ()
