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

    assert text.splitlines()[0] == "💰 БАЛАНС"
    assert "💵 USDT на споті: 500.00" in text
    assert "🪙 У монетах: 200.00 USDT" in text and "SOL 200.00" in text
    assert "Разом: ~700.00 USDT" in text


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


def _btc_daily(closes):
    """Binance-style 1d klines whose last candle closed yesterday (UTC)."""
    from datetime import timedelta

    import pandas as pd

    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    opens = [today - timedelta(days=len(closes) - i) for i in range(len(closes))]
    return pd.DataFrame({
        "open_time": pd.to_datetime(opens, utc=True),
        "close": closes,
        "close_time": pd.to_datetime([t + timedelta(days=1) - timedelta(milliseconds=1) for t in opens], utc=True),
    })


def _macro_runtime(settings, rules, closes):
    client = MagicMock()
    client.get_historical_klines = AsyncMock(return_value=_btc_daily(closes))
    notifier = MagicMock()
    notifier.macro_phase_change = AsyncMock(return_value=True)
    return _make_runtime(settings, rules, client=client, notifier=notifier), client, notifier


def test_market_phase_is_stored_silently_on_first_run_and_announced_only_when_it_changes(db_engine, settings, rules):
    from database.repository import SettingsRepository

    runtime, client, notifier = _macro_runtime(settings, rules, [100.0] * 300)

    asyncio.run(runtime._refresh_macro())  # first ever: nothing to compare with -> no alert
    asyncio.run(runtime._refresh_macro())  # unchanged -> no alert
    notifier.macro_phase_change.assert_not_called()
    with session_scope() as session:
        assert SettingsRepository(session).get("macro_phase") == "BULL"

    client.get_historical_klines.return_value = _btc_daily([100.0] * 300 + [90.0] * 3)
    asyncio.run(runtime._refresh_macro())

    notifier.macro_phase_change.assert_awaited_once()
    previous, assessment = notifier.macro_phase_change.await_args.args
    assert (previous, assessment.phase.value) == ("BULL", "BEAR")
    with session_scope() as session:
        assert SettingsRepository(session).get("macro_phase") == "BEAR"


def test_market_phase_change_while_the_bot_was_down_is_announced_after_restart(db_engine, settings, rules):
    """The last announced phase lives in the DB, not in memory: a fresh
    process compares against it instead of treating its first check as new."""
    from database.repository import SettingsRepository

    with session_scope() as session:
        SettingsRepository(session).set("macro_phase", "BEAR")
    runtime, _client, notifier = _macro_runtime(settings, rules, [100.0] * 300)

    asyncio.run(runtime._refresh_macro())

    notifier.macro_phase_change.assert_awaited_once()
    assert notifier.macro_phase_change.await_args.args[0] == "BEAR"


def test_status_snapshot_shows_the_long_term_market_phase(db_engine, settings, rules):
    runtime, _client, _notifier = _macro_runtime(settings, rules, [100.0] * 300 + [70.0] * 5)
    runtime._strategy_engine.regime_allows_buy = MagicMock(return_value=True)

    assert runtime.build_status_snapshot().macro_phase is None  # not computed yet
    asyncio.run(runtime._refresh_macro())
    snapshot = runtime.build_status_snapshot()

    assert snapshot.macro_phase == "DEEP_BEAR"
    assert "нижче 200-денної середньої" in snapshot.macro_detail


def test_market_phase_alert_that_telegram_did_not_accept_is_retried(db_engine, settings, rules):
    from database.repository import SettingsRepository

    with session_scope() as session:
        SettingsRepository(session).set("macro_phase", "BULL")
    runtime, _client, notifier = _macro_runtime(settings, rules, [100.0] * 300 + [90.0] * 3)
    notifier.macro_phase_change.return_value = False  # e.g. Telegram API hiccup

    asyncio.run(runtime._refresh_macro())
    with session_scope() as session:
        assert SettingsRepository(session).get("macro_phase") == "BULL"  # not marked as announced

    notifier.macro_phase_change.return_value = True
    asyncio.run(runtime._refresh_macro())
    assert notifier.macro_phase_change.await_count == 2
    with session_scope() as session:
        assert SettingsRepository(session).get("macro_phase") == "BEAR"


def test_quiet_phase_changes_are_recorded_without_an_alert(db_engine, settings, rules):
    from database.repository import SettingsRepository

    with session_scope() as session:
        SettingsRepository(session).set("macro_phase", "DEEP_BEAR")
    runtime, _client, notifier = _macro_runtime(settings, rules, [100.0] * 300 + [90.0] * 3)  # now plain BEAR

    asyncio.run(runtime._refresh_macro())

    notifier.macro_phase_change.assert_not_called()
    with session_scope() as session:
        assert SettingsRepository(session).get("macro_phase") == "BEAR"


def test_first_ever_phase_check_is_silent_even_in_a_bear_market(db_engine, settings, rules):
    """Deploying the feature mid-bear must not greet the owner with a
    'phase changed' alert for a change nobody saw happen."""
    from database.repository import SettingsRepository

    runtime, _client, notifier = _macro_runtime(settings, rules, [100.0] * 300 + [90.0] * 3)

    asyncio.run(runtime._refresh_macro())

    notifier.macro_phase_change.assert_not_called()
    with session_scope() as session:
        assert SettingsRepository(session).get("macro_phase") == "BEAR"


def _phase(phase, *, age_days=1):
    from datetime import timedelta

    from market.macro_regime import MacroAssessment, MacroPhase

    return MacroAssessment(
        phase=MacroPhase(phase), as_of=utcnow().date() - timedelta(days=age_days), phase_since=None,
        phase_days_at_least=40, btc_close=1.0, sma200=1.0, mayer=1.0, sma200_rising=True, sma50=1.0,
        early_warning=False, weekly_close=None, sma20w=None, ema21w=None, sma50w=None,
    )


def _entry_runtime(settings, rules):
    strategy_engine = MagicMock()
    strategy_engine.try_open_position = AsyncMock()
    runtime = _make_runtime(settings, rules, strategy_engine=strategy_engine)
    runtime._get_order_book = AsyncMock(return_value=MagicMock())
    runtime._trading_balance_usdt = AsyncMock(return_value=Decimal("1000"))
    runtime._notifier.mark_exchange_ok = AsyncMock()

    def entry_kwargs(macro):
        runtime._macro = macro
        asyncio.run(runtime._evaluate_entry("SOLUSDT"))
        return strategy_engine.try_open_position.await_args.kwargs

    return runtime, entry_kwargs


def test_bigger_strong_signal_entry_is_allowed_only_in_a_fresh_bull_phase(db_engine, settings, rules):
    _runtime, entry_kwargs = _entry_runtime(settings, rules)

    assert entry_kwargs(_phase("BULL"))["strong_size_allowed"] is True
    assert entry_kwargs(None)["strong_size_allowed"] is False  # phase not computed yet
    assert entry_kwargs(_phase("CAUTION"))["strong_size_allowed"] is False
    assert entry_kwargs(_phase("BEAR"))["strong_size_allowed"] is False
    # The refresh has been failing for days: an old BULL must not size up.
    assert entry_kwargs(_phase("BULL", age_days=5))["strong_size_allowed"] is False


def test_new_entries_are_blocked_in_a_bear_phase(db_engine, settings, rules):
    """Owner 2026-10-03: no new buys in BEAR/DEEP_BEAR (every backtested bear lost money)."""
    from orchestration.runtime import BEAR_ENTRY_BLOCK_REASON

    runtime, entry_kwargs = _entry_runtime(settings, rules)

    assert entry_kwargs(_phase("BEAR"))["entry_block_reason"] == BEAR_ENTRY_BLOCK_REASON
    assert entry_kwargs(_phase("DEEP_BEAR"))["entry_block_reason"] == BEAR_ENTRY_BLOCK_REASON
    assert entry_kwargs(_phase("BEAR", age_days=5))["entry_block_reason"] == BEAR_ENTRY_BLOCK_REASON  # fails closed
    assert entry_kwargs(_phase("BULL"))["entry_block_reason"] is None
    assert entry_kwargs(_phase("CAUTION"))["entry_block_reason"] is None
    assert runtime.build_status_snapshot().bear_entry_block is False

    runtime._macro = _phase("BEAR")
    assert runtime.build_status_snapshot().bear_entry_block is True

    runtime._settings = settings.model_copy(update={"bear_entry_block": False})
    assert entry_kwargs(_phase("BEAR"))["entry_block_reason"] is None  # owner switched it off


def test_bear_gate_uses_the_stored_phase_until_the_first_refresh_after_a_restart(db_engine, settings, rules):
    from database.repository import SettingsRepository
    from orchestration.runtime import BEAR_ENTRY_BLOCK_REASON

    _runtime, entry_kwargs = _entry_runtime(settings, rules)
    assert entry_kwargs(None)["entry_block_reason"] is None  # nothing known at all

    with session_scope() as session:
        SettingsRepository(session).set("macro_phase", "BEAR")
    assert entry_kwargs(None)["entry_block_reason"] == BEAR_ENTRY_BLOCK_REASON


def test_owner_is_told_once_when_the_market_phase_cannot_be_refreshed_for_hours(db_engine, settings, rules):
    runtime, client, notifier = _macro_runtime(settings, rules, [100.0] * 300)
    notifier.on_error = AsyncMock()
    runtime._macro = _phase("BULL")  # known before the outage
    client.get_historical_klines.side_effect = ConnectionError("down")

    for _ in range(5):
        asyncio.run(runtime._refresh_macro_tracked())
    notifier.on_error.assert_not_called()
    asyncio.run(runtime._refresh_macro_tracked())
    alert = notifier.on_error.await_args.args[0]
    assert "не оновлюється вже 6 год" in alert
    assert "останню відому фазу (BULL" in alert  # the strong entry is not off yet - it says when it will be
    assert "більше 2 днів" in alert
    asyncio.run(runtime._refresh_macro_tracked())
    assert notifier.on_error.await_count == 1  # not every hour after that

    client.get_historical_klines.side_effect = None  # recovered -> the counter starts over
    asyncio.run(runtime._refresh_macro_tracked())
    assert runtime._macro_failures == 0


def test_bot_does_not_buy_back_a_coin_for_24h_after_the_owner_sold_it(db_engine, settings, rules):
    from datetime import timedelta

    from database.repository import SettingsRepository

    strategy_engine = MagicMock()
    strategy_engine.try_open_position = AsyncMock()
    runtime = _make_runtime(settings, rules, strategy_engine=strategy_engine)
    runtime._get_order_book = AsyncMock(return_value=MagicMock())
    runtime._trading_balance_usdt = AsyncMock(return_value=Decimal("1000"))
    runtime._notifier.mark_exchange_ok = AsyncMock()

    def sold_hours_ago(hours):
        with session_scope() as session:  # written by StrategyEngine when the MANUAL_SELL fill is applied
            SettingsRepository(session).set("manual_sell_at:AAVEUSDT", (utcnow() - timedelta(hours=hours)).isoformat())

    sold_hours_ago(1)
    asyncio.run(runtime._evaluate_entry("AAVEUSDT"))
    strategy_engine.try_open_position.assert_not_called()

    sold_hours_ago(25)  # 25 hours later the coin is a normal candidate again
    asyncio.run(runtime._evaluate_entry("AAVEUSDT"))
    strategy_engine.try_open_position.assert_awaited_once()


def _earn_runtime(settings, rules, *, spot="150", earn="900"):
    from tests.test_earn import FakeEarnClient, _manager

    fake = FakeEarnClient(spot=spot, earn=earn)
    client = MagicMock()
    client.get_account_balances = fake.get_account_balances
    notifier = MagicMock()
    notifier.on_error = AsyncMock()
    notifier.status_ping = AsyncMock()
    runtime = _make_runtime(settings, rules, client=client, notifier=notifier, earn=_manager(fake))
    return runtime, fake, notifier


def test_trading_balance_counts_usdt_held_in_earn(db_engine, settings, rules):
    """The risk caps are a share of the trading balance: moving idle USDT into
    Earn must not shrink the bot."""
    runtime, _fake, _notifier = _earn_runtime(settings, rules)

    assert asyncio.run(runtime._trading_balance_usdt()) == Decimal("1050")


def test_trading_balance_falls_back_to_spot_when_earn_is_unavailable(db_engine, settings, rules):
    runtime, fake, notifier = _earn_runtime(settings, rules)
    fake.get_flexible_earn_position = AsyncMock(side_effect=ConnectionError("sapi down"))

    assert asyncio.run(runtime._trading_balance_usdt()) == Decimal("150")  # fewer buys, never more
    assert "баланс Earn недоступний" in notifier.on_error.await_args.args[0]


def test_sweep_moves_idle_usdt_into_earn_and_tells_the_owner(db_engine, settings, rules):
    runtime, fake, notifier = _earn_runtime(settings, rules, spot="2000.00", earn="0")

    asyncio.run(runtime._sweep_to_earn())

    assert fake.earn == Decimal("1850.00")
    text = notifier.status_ping.await_args.args[0]
    assert "1850.00 USDT переміщено в Simple Earn" in text
    assert "2.65% річних" in text


def test_failing_sweep_alerts_once_a_day_not_every_half_hour(db_engine, settings, rules):
    runtime, fake, notifier = _earn_runtime(settings, rules, spot="1000", earn="0")
    fake.subscribe_flexible_earn = AsyncMock(side_effect=RuntimeError("APIError(code=-2015)"))

    for _ in range(48):
        asyncio.run(runtime._sweep_to_earn())
    assert notifier.on_error.await_count == 1
    asyncio.run(runtime._sweep_to_earn())
    assert notifier.on_error.await_count == 2


def test_balance_shows_the_earn_line(db_engine, settings, rules):
    runtime, _fake, _notifier = _earn_runtime(settings, rules)
    runtime.get_mark_prices = lambda: {}

    text = asyncio.run(runtime.get_balance_text())

    assert "🏦 USDT в Earn (депозит, 2.65% річних): 900.00" in text
    assert "Разом: ~1050.00 USDT" in text


def test_an_earn_outage_alerts_once_without_a_false_recovered_message(db_engine, settings, rules):
    """Each balance read sent "ПОМИЛКА" + "ВІДНОВЛЕНО" (the timeout text matched
    the network category and spot trading then marked the exchange ok)."""
    from telegram_bot.notifications import TelegramNotifier
    from tests.test_earn import FakeEarnClient, _manager
    from tests.test_telegram_notifications import _RecordingSender

    fake = FakeEarnClient(spot="150", earn="900")

    async def down(asset):
        raise TimeoutError("Connection timed out to api.binance.com")

    fake.get_flexible_earn_position = down
    client = MagicMock()
    client.get_account_balances = fake.get_account_balances
    sender = _RecordingSender()
    notifier = TelegramNotifier(sender, chat_id=1)
    runtime = _make_runtime(settings, rules, client=client, notifier=notifier, earn=_manager(fake))

    async def cycles():
        for _ in range(3):
            assert await runtime._trading_balance_usdt() == Decimal("150")
            await notifier.mark_exchange_ok()

    asyncio.run(cycles())
    assert len(sender.sent) == 1
    assert "баланс Earn недоступний" in sender.sent[0][1]


def test_earn_outage_alert_is_sent_once_per_outage_by_the_runtime_itself(db_engine, settings, rules):
    """Not left to the notifier's 1-hour de-duplication: an outage of several
    hours must not re-alert every hour, and a new outage must alert again."""
    runtime, fake, notifier = _earn_runtime(settings, rules)
    working = fake.get_flexible_earn_position
    fake.get_flexible_earn_position = AsyncMock(side_effect=TimeoutError("sapi"))

    for _ in range(3):
        runtime._earn._failed_read_at = None  # each read really asks Binance
        asyncio.run(runtime._trading_balance_usdt())
    assert notifier.on_error.await_count == 1

    fake.get_flexible_earn_position = working  # recovered
    runtime._earn._failed_read_at = None
    assert asyncio.run(runtime._trading_balance_usdt()) == Decimal("1050")
    fake.get_flexible_earn_position = AsyncMock(side_effect=TimeoutError("sapi"))  # a new outage
    runtime._earn._balance = None
    asyncio.run(runtime._trading_balance_usdt())
    assert notifier.on_error.await_count == 2


def test_report_equity_is_not_recorded_when_earn_cannot_be_read(db_engine, settings, rules):
    """The spot-only fallback stored a daily ending balance short by the whole Earn balance:
    a fake loss in that month's scoreboard and a fake gain the next."""
    import pytest

    from database.repository import DailyStatRepository

    runtime, fake, notifier = _earn_runtime(settings, rules)
    fake.get_flexible_earn_position = AsyncMock(side_effect=TimeoutError("sapi"))
    runtime.get_mark_prices = lambda: {}

    with pytest.raises(RuntimeError, match="equity would be understated"):
        asyncio.run(runtime._send_daily_report())
    with session_scope() as session:
        assert DailyStatRepository(session).recent() == []


def test_a_sweep_that_moved_money_is_reported_even_if_the_totals_cannot_be_read(db_engine, settings, rules):
    runtime, fake, notifier = _earn_runtime(settings, rules, spot="1000", earn="0")
    runtime._earn.product = AsyncMock(side_effect=[
        __import__("exchange.earn", fromlist=["EarnProduct"]).EarnProduct("USDT001", 0.0265, Decimal("0.01"), True, True),
        TimeoutError("sapi"),
    ])

    asyncio.run(runtime._sweep_to_earn())

    notifier.on_error.assert_not_called()
    assert "850.00 USDT переміщено в Simple Earn" in notifier.status_ping.await_args.args[0]


def test_balance_is_short_counts_dust_and_never_counts_the_earn_receipt_twice(db_engine, settings, rules):
    """Owner 2026-10-08: /balance listed ~30 lines of dust plus LDUSDT (Binance's
    Earn receipt) next to the Earn line; he wants where the money is, briefly."""
    runtime, fake, _notifier = _earn_runtime(settings, rules)
    real = fake.get_account_balances

    async def balances():
        out = await real()
        out.update({
            "AVAX": (Decimal("5"), Decimal("0")), "SC": (Decimal("500"), Decimal("0")),
            "BNB": (Decimal("0.00000037"), Decimal("0")), "LDUSDT": (Decimal("800"), Decimal("0")),
        })
        return out

    runtime._client.get_account_balances = balances
    runtime.get_mark_prices = lambda: {"AVAXUSDT": Decimal("11"), "BNBUSDT": Decimal("600")}

    text = asyncio.run(runtime.get_balance_text())

    assert text == (
        "💰 БАЛАНС\n"
        "💵 USDT на споті: 150.00\n"
        "🏦 USDT в Earn (депозит, 2.65% річних): 900.00\n"
        "🪙 У монетах: 55.00 USDT\n"
        "   AVAX 55.00\n"
        "🧹 Дрібні залишки: 2 монет (менше 1 USDT або без ціни, у підсумок не входять)\n"
        "Разом: ~1105.00 USDT"
    )
