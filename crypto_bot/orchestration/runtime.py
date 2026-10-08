"""BotRuntime: the live/paper composition object.

Owns every long-lived component for LIVE/PAPER mode (Binance client,
WebSocket manager, market data store, strategy/risk/news engines, execution
engine, optional paper broker) and exposes:

* the long-running scheduler loops `app.py` registers with the `Watchdog`
  (`run_position_monitor_loop`, `run_entry_evaluation_loop`, `run_universe_scanner_loop`,
  `run_news_refresh_loop`, `run_daily_report_loop`, `run_status_ping_loop`);
* the read-only callables `telegram_bot.handlers.BotContext` needs
  (`get_balance_text`, `get_current_regime`, `get_latest_signals`,
  `get_health_snapshot`, `get_mark_prices`, `get_status_snapshot`).

Kept out of `app.py` so the composition root stays readable as "build the
pieces, wire them into a BotRuntime, hand it to the scheduler" instead of a
few-hundred-line function.

Two things are deliberately *not* scheduled here as Watchdog tasks: the
kline and user-data WebSocket streams. Both already run their own
reconnect-with-backoff supervisor (`exchange.websocket_manager.
ReconnectingStream`) and, by design, never exit on their own short of
`.stop()` - registering them again here would just be a second supervisor
watching a task that never needs restarting.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from config.settings import RulesConfig, Settings, TradingMode
from database.models import OrderPurpose
from database.repository import (
    NewsRepository,
    OrderRepository,
    PositionRepository,
    SettingsRepository,
)
from database.session import session_scope
from exchange.binance_client import BinanceClient
from exchange.earn import EarnManager
from exchange.execution_engine import ExecutionEngine
from exchange.websocket_manager import WebSocketManager
from market.macro_regime import (
    HISTORY_DAYS,
    MacroAssessment,
    MacroPhase,
    assess_macro,
    phase_change_alert_due,
)
from market.market_data import MarketDataStore
from market.market_regime import MarketRegimeEngine, RegimeAssessment, RegimeLevel
from market.orderbook import OrderBookSnapshot, parse_order_book
from market.universe_scanner import UniverseScanner
from news.news_engine import NewsEngine
from orchestration.daily_report import build_daily_stat, month_closed_trades
from orchestration.monthly_report import (
    MonthlyReportData,
    build_monthly_report,
    is_last_day_of_month,
    month_start_of,
)
from orchestration.watchdog import Watchdog
from paper.simulator import PaperBroker
from risk.risk_manager import RiskManager
from strategy.strategy_engine import (
    ManualSellResult,
    StrategyEngine,
    TradeDecision,
    in_manual_sell_cooldown,
)
from telegram_bot.notifications import (
    DailyReportData,
    StatusSnapshot,
    TelegramNotifier,
    format_earn_sweep,
    format_monthly_report,
    format_status,
    macro_status_detail,
)
from utils.time import Timeframe, floor_to_timeframe, utcnow

logger = logging.getLogger(__name__)

_TRACKED_TIMEFRAMES = (Timeframe.M15, Timeframe.H1, Timeframe.H4)
_BACKFILL_BARS = 300
# A multiplexed kline stream over ~25 symbols ticks every second or two;
# this long without a single message means the feed is effectively down.
_STALE_FEED_SECONDS = 120
# Longer than a planned stream swap on universe rescan (1-3s) plus the
# reconnect backoff's first steps, so only a real outage is reported.
_FEED_DOWN_GRACE_SECONDS = 60
# The long-term phase uses closed DAILY candles; an hourly check sees each
# new daily close within the hour, for one cheap REST call.
_MACRO_REFRESH_SECONDS = 3600
# Last phase announced in Telegram, persisted so a restart neither repeats an
# old alert nor misses a change that happened while the bot was down.
_MACRO_PHASE_KEY = "macro_phase"
# The phase comes from the last CLOSED daily candle (yesterday); older than
# this means the hourly refresh has been failing - don't size up on it.
_MACRO_MAX_AGE = timedelta(days=2)
_MACRO_FAILURE_ALERT_AFTER = 6  # consecutive hourly refresh failures
_EARN_FIRST_SWEEP_DELAY_SECONDS = 600  # let startup reconciliation settle first
_EARN_SWEEP_SECONDS = 1800
_EARN_FAILURE_ALERT_EVERY = 48  # sweeps (~a day) between repeated failure alerts
BEAR_ENTRY_BLOCK_REASON = "bear market (long-term phase): new entries are off - BEAR_ENTRY_BLOCK"
_MACRO_CAUTION_ALERT_KEY = "macro_caution_alert_at"
# After the owner's /sell the bot doesn't buy that coin back right away.


def _minutes(seconds: float) -> int:
    return max(1, int(seconds // 60))
_TASK_LABELS = {
    "position_monitor": "супровід позицій",
    "entry_evaluator": "пошук входів",
    "universe_scanner": "вибір монет",
    "news_refresh": "новини",
    "daily_report": "щоденний звіт",
    "status_ping": "статус-повідомлення",
    "macro_regime": "фаза ринку",
    "earn_sweep": "USDT в Earn",
}


def _default_neutral_regime() -> RegimeAssessment:
    return RegimeAssessment(level=RegimeLevel.NEUTRAL, score=0.0, reasons=["BTC regime not yet computed"], crash=False)


class BotRuntime:
    def __init__(
        self,
        *,
        settings: Settings,
        rules: RulesConfig,
        client: BinanceClient,
        ws_manager: WebSocketManager,
        market_data: MarketDataStore,
        universe_scanner: UniverseScanner,
        risk_manager: RiskManager,
        news_engine: NewsEngine,
        execution_engine: ExecutionEngine,
        strategy_engine: StrategyEngine,
        notifier: TelegramNotifier,
        watchdog: Watchdog,
        paper_broker: PaperBroker | None,
        started_at: datetime,
        earn: EarnManager | None = None,
    ) -> None:
        self._settings = settings
        self._rules = rules
        self._client = client
        self._ws_manager = ws_manager
        self._market_data = market_data
        self._universe_scanner = universe_scanner
        self._risk_manager = risk_manager
        self._news_engine = news_engine
        self._execution_engine = execution_engine
        self._strategy_engine = strategy_engine
        self._notifier = notifier
        self._watchdog = watchdog
        self._paper_broker = paper_broker
        self.started_at = started_at
        # Simple Earn for idle USDT (exchange/earn.py); None outside real LIVE trading.
        self._earn = earn
        self._earn_failures = 0
        self._earn_balance_alerted = False  # one alert per Earn outage

        self._regime_engine = MarketRegimeEngine(rules.crash_detector)
        self._btc_regime: RegimeAssessment | None = None
        self._macro: MacroAssessment | None = None
        self._macro_failures = 0
        self._latest_decisions: dict[str, TradeDecision] = {}
        self._candidate_symbols: set[str] = set()
        self._tracked_symbols: set[str] = set()
        self._stream_pairs: list[tuple[str, str]] = []
        self._alerted_news_ids: set[int] = set()
        self._last_universe_scan_at: datetime | None = None
        self._last_news_refresh_at: datetime | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        # Candle-close entry evaluations are handed off to a single worker
        # (run_entry_evaluation_loop) instead of running inline in the kline
        # read loop: evaluating ~25 symbols back-to-back (REST order-book
        # fetch + DB + indicators each) blocks the socket long enough to
        # overflow python-binance's 100-message queue, which drops the
        # connection and loses the remaining symbols' candle closes. One
        # worker (not a task per symbol) keeps evaluations serialized, so
        # two entries can never race past the open-position/exposure caps.
        self._entry_queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued_entries: set[str] = set()
        # Telegram starts answering before initialize() finishes (backfilling
        # ~25 symbols x 3 timeframes takes ~30-40s); /status must say
        # "starting" then, not report the not-yet-started feed as an outage.
        self._initialized = False

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        await self._rescan_universe()
        if self._settings.mode == TradingMode.LIVE:
            self._ws_manager.start_user_stream(self._on_user_event)
        if self._settings.news_enabled:
            await self._news_engine.refresh()
            self._last_news_refresh_at = utcnow()
        self._update_btc_regime()
        # Never blocks startup: until it succeeds the bear gate falls back to
        # the last stored phase and the bigger entry stays off.
        await self._refresh_macro_tracked()
        self._initialized = True

    def register_tasks(self) -> None:
        self._watchdog.register("position_monitor", self.run_position_monitor_loop)
        self._watchdog.register("entry_evaluator", self.run_entry_evaluation_loop)
        self._watchdog.register("universe_scanner", self.run_universe_scanner_loop)
        if self._settings.news_enabled:
            self._watchdog.register("news_refresh", self.run_news_refresh_loop)
        self._watchdog.register("daily_report", self.run_daily_report_loop)
        self._watchdog.register("status_ping", self.run_status_ping_loop)
        self._watchdog.register("macro_regime", self.run_macro_regime_loop)
        if self._earn is not None:
            self._watchdog.register("earn_sweep", self.run_earn_sweep_loop)

    # ------------------------------------------------------------------
    # Universe tracking + market data
    # ------------------------------------------------------------------

    async def _rescan_universe(self) -> None:
        candidates = await self._universe_scanner.scan()
        self._news_engine.set_known_bases({c.symbol[:-4] for c in candidates if c.symbol.endswith("USDT")})

        new_candidates = {c.symbol for c in candidates}
        with session_scope() as session:
            open_symbols = {p.symbol for p in PositionRepository(session).get_open_positions()}
        required = new_candidates | open_symbols | {"BTCUSDT"}

        for symbol in required - self._tracked_symbols:
            for timeframe in _TRACKED_TIMEFRAMES:
                await self._market_data.backfill(symbol, timeframe, limit=_BACKFILL_BARS)
        for symbol in self._tracked_symbols - required:
            self._market_data.drop_symbol(symbol)

        self._candidate_symbols = new_candidates
        self._tracked_symbols = required
        self._last_universe_scan_at = utcnow()

        # Only reconnect when the tracked set actually changed: every
        # reconnect is a window in which candle-close events are lost, and
        # the rescan cadence drifts, so an unconditional restart eventually
        # lands right on a :00/:15/:30/:45 close.
        pairs = sorted((symbol, tf.value) for symbol in required for tf in _TRACKED_TIMEFRAMES)
        if pairs != self._stream_pairs:
            await self._ws_manager.stop_stream("klines")
            self._ws_manager.start_kline_stream(pairs, self._on_kline_message)
            self._stream_pairs = pairs
        logger.info(
            "Universe rescanned: tracking %s symbol(s) (%s candidates, %s open position(s))",
            len(required), len(new_candidates), len(open_symbols),
        )

    async def run_universe_scanner_loop(self) -> None:
        while True:
            await self._sleep_with_heartbeat(self._settings.scanner_interval_minutes * 60, "universe_scanner")
            try:
                await self._rescan_universe()
            except Exception as exc:  # noqa: BLE001 - one bad scan must not kill the loop
                logger.exception("Universe scan failed: %r", exc)
                await self._notifier.on_error(f"Universe scan failed: {exc!r}")

    async def _sleep_with_heartbeat(self, seconds: float, task_name: str) -> None:
        await asyncio.sleep(seconds)
        self._watchdog.heartbeat(task_name)

    # ------------------------------------------------------------------
    # Market data / regime
    # ------------------------------------------------------------------

    async def _on_kline_message(self, payload: dict[str, Any]) -> None:
        result = self._market_data.apply_kline_message(payload)
        if result is None:
            return
        symbol, timeframe, closed = result
        if not closed:
            return
        if symbol == "BTCUSDT":
            self._update_btc_regime()
        if timeframe == Timeframe.M15 and symbol in self._candidate_symbols:
            self._enqueue_entry_evaluation(symbol)

    def _enqueue_entry_evaluation(self, symbol: str) -> None:
        if symbol in self._queued_entries:
            return
        self._queued_entries.add(symbol)
        self._entry_queue.put_nowait(symbol)

    async def run_entry_evaluation_loop(self) -> None:
        while True:
            symbol = await self._entry_queue.get()
            self._queued_entries.discard(symbol)
            # The universe may have been rescanned while this sat in the queue.
            if symbol in self._candidate_symbols:
                try:
                    await self._evaluate_entry(symbol)
                except Exception as exc:  # noqa: BLE001 - one bad candidate must not kill the worker
                    # _evaluate_entry guards the strategy call itself, but not its
                    # DB pre-checks (e.g. sqlite "database is locked"). Letting
                    # that escape would burn one of the watchdog's lifetime
                    # restarts per incident and eventually stop entry evaluation.
                    logger.exception("Entry evaluation failed for %s: %r", symbol, exc)
            self._watchdog.heartbeat("entry_evaluator")

    def _update_btc_regime(self) -> None:
        snapshots = {tf: self._market_data.snapshot("BTCUSDT", tf) for tf in _TRACKED_TIMEFRAMES}
        if not all(snapshots.values()):
            return
        recent_15m = self._market_data.dataframe("BTCUSDT", Timeframe.M15)
        previous_level = self._btc_regime.level if self._btc_regime else None
        self._btc_regime = self._regime_engine.evaluate(snapshots, recent_15m)  # type: ignore[arg-type]
        if self._btc_regime.level == RegimeLevel.CRASH and previous_level != RegimeLevel.CRASH:
            task = asyncio.create_task(self._notifier.crash_alert(self._btc_regime.reasons))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def run_macro_regime_loop(self) -> None:
        while True:
            await self._sleep_with_heartbeat(_MACRO_REFRESH_SECONDS, "macro_regime")
            await self._refresh_macro_tracked()

    async def _refresh_macro_tracked(self) -> None:
        """One refresh that never raises; after _MACRO_FAILURE_ALERT_AFTER
        failures in a row the owner hears about it once (the bear gate and
        the strong-entry rule both depend on a current phase)."""
        try:
            await self._refresh_macro()
        except Exception as exc:  # noqa: BLE001 - one failed refresh must not kill the loop
            self._macro_failures += 1
            logger.warning("Market phase refresh failed (%s in a row, retrying in an hour): %r", self._macro_failures, exc)
            if self._macro_failures == _MACRO_FAILURE_ALERT_AFTER:
                last = (
                    f"{self._macro.phase.value} за закриттям {self._macro.as_of.isoformat()}"
                    if self._macro is not None else "невідома"
                )
                # No exception text: a timeout would match the network category
                # and get a false "recovered" as soon as spot trading answers.
                await self._notifier.on_error(
                    f"Фаза ринку не оновлюється вже {self._macro_failures} год (деталі в логах). Бот використовує "
                    f"останню відому фазу ({last}); збільшений вхід вимкнеться, коли їй буде більше 2 днів, "
                    "а пауза купівель у ведмежому ринку тримається за останньою відомою фазою."
                )
            return
        self._macro_failures = 0

    def _fresh_macro(self) -> MacroAssessment | None:
        if self._macro is None or utcnow().date() - self._macro.as_of > _MACRO_MAX_AGE:
            return None
        return self._macro

    def _bear_entry_block_active(self) -> bool:
        """BEAR_ENTRY_BLOCK: no new entries in a BEAR/DEEP_BEAR phase. Fails
        closed: a stale phase still counts, and before the first refresh
        after a restart the last stored phase decides."""
        if not self._settings.bear_entry_block:
            return False
        if self._macro is not None:
            return self._macro.is_bear
        with session_scope() as session:
            stored = SettingsRepository(session).get(_MACRO_PHASE_KEY)
        return stored in (MacroPhase.BEAR.value, MacroPhase.DEEP_BEAR.value)

    async def _refresh_macro(self) -> None:
        """Recomputes the long-term phase from closed BTC daily candles and
        announces a change in Telegram. Drives the bear-market entry gate
        and the strong-signal entry size."""
        now = utcnow()
        daily = await self._client.get_historical_klines("BTCUSDT", "1d", now - timedelta(days=HISTORY_DAYS), now)
        assessment = assess_macro(daily, now=now)
        if assessment is None:
            raise RuntimeError(f"not enough BTC daily history yet ({len(daily)} candles)")
        self._macro = assessment
        phase = assessment.phase.value
        with session_scope() as session:
            settings_repo = SettingsRepository(session)
            previous = settings_repo.get(_MACRO_PHASE_KEY)
            last_caution_raw = settings_repo.get(_MACRO_CAUTION_ALERT_KEY)
            if previous is None:
                settings_repo.set(_MACRO_PHASE_KEY, phase)
        if previous is None:
            logger.info("Market phase initialised: %s (BTC %.0f, SMA200 %.0f)", phase, assessment.btc_close, assessment.sma200)
            return
        if previous == phase:
            return
        last_caution = datetime.fromisoformat(last_caution_raw) if last_caution_raw else None
        due = phase_change_alert_due(previous, phase, last_caution_alert_at=last_caution, now=now)
        logger.warning("Market phase changed: %s -> %s (alert: %s)", previous, phase, due)
        # Persist only after Telegram accepted the alert: a failed send keeps
        # the old phase stored, so the next hourly refresh announces it again.
        if due and not await self._notifier.macro_phase_change(
            previous, assessment, entry_block=self._settings.bear_entry_block
        ):
            logger.warning("Market phase alert not delivered - retrying at the next refresh")
            return
        with session_scope() as session:
            settings_repo = SettingsRepository(session)
            settings_repo.set(_MACRO_PHASE_KEY, phase)
            if due and phase == MacroPhase.CAUTION.value:
                settings_repo.set(_MACRO_CAUTION_ALERT_KEY, now.isoformat())

    async def _on_user_event(self, event: dict[str, Any]) -> None:
        """Supplementary low-latency signal only - order-state correctness
        never depends on this. `ExecutionEngine` persists every fill
        synchronously right after `submit()` returns, and
        `check_pending_limit_orders`/startup reconciliation cover the
        asynchronous LIMIT-order case by polling Binance directly, so a
        missed or delayed user-stream event can never desync local state."""
        logger.debug("User data event: %s", event.get("e"))

    async def _get_order_book(self, symbol: str) -> OrderBookSnapshot:
        raw = await self._client.get_order_book(symbol, limit=50)
        return parse_order_book(symbol, raw)

    async def _trading_balance_usdt(self, *, strict: bool = False) -> Decimal:
        """Free USDT: spot + Simple Earn. If Earn can't be read the risk
        caps fall back to spot only (fewer buys, never more); `strict`
        callers (equity for the reports) raise instead of recording a
        figure that is ~the whole Earn balance too low."""
        if self._paper_broker is not None:
            return self._paper_broker.account.usdt_balance
        if self._earn is None:
            balances = await self._client.get_account_balances()
            return balances.get("USDT", (Decimal("0"), Decimal("0")))[0]
        # Idle USDT waits in Earn; the risk caps measure the whole of it, so
        # moving money there doesn't shrink the bot.
        spot, earn = await self._earn.read_balances()
        if earn is not None:
            self._earn_balance_alerted = False
            return spot + earn
        if strict:
            raise RuntimeError("Simple Earn balance unavailable - equity would be understated")
        if not self._earn_balance_alerted:
            self._earn_balance_alerted = True
            # Worded to match no exchange-error category: spot trading works,
            # so a "recovered" message right after would be noise.
            await self._notifier.on_error(
                "Simple Earn: баланс Earn недоступний - доки Binance не відповість, ліміти купівель рахуються "
                "лише від USDT на споті (бот купуватиме менше, не більше). Деталі в логах."
            )
        return spot

    # ------------------------------------------------------------------
    # Simple Earn sweep
    # ------------------------------------------------------------------

    async def run_earn_sweep_loop(self) -> None:
        await self._sleep_with_heartbeat(_EARN_FIRST_SWEEP_DELAY_SECONDS, "earn_sweep")
        while True:
            await self._sweep_to_earn()
            await self._sleep_with_heartbeat(_EARN_SWEEP_SECONDS, "earn_sweep")

    async def _sweep_to_earn(self) -> None:
        assert self._earn is not None
        try:
            moved = await self._earn.sweep()
        except Exception as exc:  # noqa: BLE001 - the next round sees the real balances
            self._earn_failures += 1
            logger.warning("Earn sweep failed (%s in a row): %r", self._earn_failures, exc)
            if self._earn_failures % _EARN_FAILURE_ALERT_EVERY == 1:
                hint = " Ключ API, схоже, не має дозволу на Simple Earn (код 2015)." if "-2015" in repr(exc) else ""
                await self._notifier.on_error(
                    "Simple Earn: переміщення USDT між спотом і Earn не вдалося або біржа його не підтвердила. "
                    f"Наступна спроба через 30 хв; баланси видно в /balance.{hint} Деталі в логах."
                )
            return
        self._earn_failures = 0
        if moved > 0:
            try:
                held, apr = await self._earn.balance(), (await self._earn.product()).apr
            except Exception as exc:  # noqa: BLE001 - the move itself succeeded; report it without the totals
                logger.warning("Earn totals after the sweep unavailable: %r", exc)
                held, apr = None, None
            await self._notifier.status_ping(format_earn_sweep(moved, held, apr, self._settings.earn_spot_buffer_usdt))

    # ------------------------------------------------------------------
    # Entry evaluation (candle-close driven)
    # ------------------------------------------------------------------

    async def _evaluate_entry(self, symbol: str) -> None:
        if self._risk_manager.status().emergency_stop:
            return
        with session_scope() as session:
            position_repo = PositionRepository(session)
            if position_repo.get_open_position_for_symbol(symbol) is not None:
                return
            # A resting entry LIMIT order has no Position yet (that's only
            # created on an actual fill - see StrategyEngine._apply_entry_fill),
            # so the check above alone doesn't stop a second candle-close
            # from submitting a duplicate entry while the first is still
            # resolving; process_resolved_orders() is what finishes it.
            if OrderRepository(session).has_resting_order(symbol=symbol, purpose=OrderPurpose.ENTRY):
                return
            open_count = position_repo.count_open()
            open_symbols = [p.symbol for p in position_repo.get_open_positions()]
        if open_count >= self._settings.max_open_positions:
            return
        if in_manual_sell_cooldown(symbol):
            return

        regime = self._btc_regime or _default_neutral_regime()
        fresh_macro = self._fresh_macro()
        try:
            # Independent REST round trips - run concurrently rather than
            # paying both latencies back-to-back on this candle-close path.
            order_book, trading_balance = await asyncio.gather(
                self._get_order_book(symbol), self._trading_balance_usdt()
            )
            await self._notifier.mark_exchange_ok()
            decision = await self._strategy_engine.try_open_position(
                symbol, btc_regime=regime, trading_balance_usdt=trading_balance,
                order_book=order_book, open_position_symbols=open_symbols,
                # The bigger strong-signal entry only while the long-term phase is BULL:
                # never in an early warning or a bear, never on a stale or unknown phase.
                strong_size_allowed=fresh_macro is not None and fresh_macro.phase == MacroPhase.BULL,
                entry_block_reason=BEAR_ENTRY_BLOCK_REASON if self._bear_entry_block_active() else None,
            )
            self._latest_decisions[symbol] = decision
        except Exception as exc:  # noqa: BLE001 - one bad candidate must not kill the feed
            logger.exception("Entry evaluation failed for %s: %r", symbol, exc)
            await self._notifier.on_error(f"Entry evaluation error for {symbol}: {exc!r}")

    # ------------------------------------------------------------------
    # Position management (polled - a TP/DCA price threshold can be
    # crossed intra-candle, so this cannot wait for a candle close)
    # ------------------------------------------------------------------

    async def run_position_monitor_loop(self) -> None:
        while True:
            try:
                await self._monitor_open_positions()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive across a bad cycle
                logger.exception("Position monitor cycle failed: %r", exc)
                await self._notifier.on_error(f"Position monitor error: {exc!r}")
            await self._sleep_with_heartbeat(self._rules.scheduler.position_monitor_interval_seconds, "position_monitor")

    async def _monitor_open_positions(self) -> None:
        regime = self._btc_regime or _default_neutral_regime()
        try:
            await self._strategy_engine.process_resolved_orders(btc_regime=regime)
        except Exception as exc:  # noqa: BLE001 - a bad resolution cycle must not block position management
            logger.exception("Resolving pending limit orders failed: %r", exc)
            await self._notifier.on_error(f"Order resolution error: {exc!r}")

        with session_scope() as session:
            open_positions = [(p.id, p.symbol) for p in PositionRepository(session).get_open_positions()]
        if not open_positions:
            return
        # Fetched once per cycle, not once per position - manage_position
        # needs it to gate DCA against MAX_TOTAL_EXPOSURE_PERCENT/
        # MAX_DAILY_NEW_CAPITAL_USDT the same way entry evaluation already does.
        # A failed fetch must not skip the whole cycle: the hard ceiling,
        # trailing and take-profit don't need the balance, so every position
        # is still managed with None, which skips only DCA.
        trading_balance: Decimal | None
        try:
            trading_balance = await self._trading_balance_usdt()
        except Exception as exc:  # noqa: BLE001 - exits must still be managed without a balance
            logger.exception("Trading balance fetch failed - managing positions without DCA this cycle: %r", exc)
            await self._notifier.on_error(f"Balance fetch failed, DCA skipped this cycle (exits still managed): {exc!r}")
            trading_balance = None
        else:
            await self._notifier.mark_exchange_ok()
        for position_id, symbol in open_positions:
            try:
                order_book = await self._get_order_book(symbol)
                if order_book.is_empty:
                    continue
                await self._strategy_engine.manage_position(
                    position_id, btc_regime=regime, current_price=order_book.mid_price, order_book=order_book,
                    trading_balance_usdt=trading_balance,
                )
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not block managing the rest
                logger.exception("Position monitor failed for %s: %r", symbol, exc)
                await self._notifier.on_error(f"Position monitor error for {symbol}: {exc!r}")

    # ------------------------------------------------------------------
    # Emergency stop
    # ------------------------------------------------------------------

    async def manual_sell(self, symbol: str) -> ManualSellResult:
        """The owner's /sell: market-sell the whole open position in
        `symbol`. The sale itself starts the no-re-buy cooldown
        (strategy_engine.MANUAL_SELL_COOLDOWN)."""
        try:
            order_book: OrderBookSnapshot | None = await self._get_order_book(symbol)
        except Exception as exc:  # noqa: BLE001 - reported as no_order_book below
            logger.exception("Failed to fetch order book for manual sell of %s: %r", symbol, exc)
            order_book = None
        regime = self._btc_regime or _default_neutral_regime()
        return await self._strategy_engine.manual_sell(symbol, order_book=order_book, btc_regime=regime)

    async def emergency_stop(self) -> list[str] | None:
        """Triggers the kill switch (stop new BUY/DCA) and, only if
        `EMERGENCY_AUTO_SELL=true`, immediately market-sells every open
        position. Returns `None` if no liquidation was attempted (the
        setting is off), otherwise the list of symbols that failed to
        liquidate (empty list = every position sold)."""
        self._risk_manager.trigger_emergency_stop()
        if not self._settings.emergency_auto_sell:
            return None

        with session_scope() as session:
            symbols = [p.symbol for p in PositionRepository(session).get_open_positions()]
        order_books: dict[str, OrderBookSnapshot] = {}
        for symbol in symbols:
            try:
                order_books[symbol] = await self._get_order_book(symbol)
            except Exception as exc:  # noqa: BLE001 - a missing book is handled per-symbol below
                logger.exception("Failed to fetch order book for emergency liquidation of %s: %r", symbol, exc)
        regime = self._btc_regime or _default_neutral_regime()
        return await self._strategy_engine.emergency_liquidate_all(order_books=order_books, btc_regime=regime)

    # ------------------------------------------------------------------
    # News
    # ------------------------------------------------------------------

    async def run_news_refresh_loop(self) -> None:
        while True:
            await self._sleep_with_heartbeat(self._settings.news_refresh_minutes * 60, "news_refresh")
            try:
                stored = await self._news_engine.refresh()
                self._last_news_refresh_at = utcnow()
                if stored:
                    await self._alert_new_critical_news()
            except Exception as exc:  # noqa: BLE001 - a failed fetch must not kill the loop
                logger.exception("News refresh failed: %r", exc)

    async def _alert_new_critical_news(self) -> None:
        with session_scope() as session:
            since = utcnow() - timedelta(minutes=self._settings.news_refresh_minutes * 2)
            critical = NewsRepository(session).recent_critical(since)
            unseen = [item for item in critical if item.id not in self._alerted_news_ids]
            payloads = [(item.id, item.symbols or [], item.sentiment_score, item.title) for item in unseen]
        for news_id, symbols, score, title in payloads:
            for symbol in symbols:
                await self._notifier.news_alert(symbol, score, title)
            self._alerted_news_ids.add(news_id)

    # ------------------------------------------------------------------
    # Daily report
    # ------------------------------------------------------------------

    async def run_daily_report_loop(self) -> None:
        while True:
            now = utcnow()
            target = now.replace(hour=self._settings.daily_report_hour_utc, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await self._sleep_with_heartbeat((target - now).total_seconds(), "daily_report")
            try:
                await self._send_daily_report()
            except Exception as exc:  # noqa: BLE001 - a failed report must not kill the loop
                logger.exception("Daily report failed: %r", exc)
                await self._notifier.on_error(f"Daily report failed: {exc!r}")
            if is_last_day_of_month(utcnow().date()):
                try:
                    await self._notifier.monthly_report(await self._monthly_report_data(month_to_date=False))
                except Exception as exc:  # noqa: BLE001 - same as the daily report
                    logger.exception("Monthly report failed: %r", exc)
                    await self._notifier.on_error(f"Monthly report failed: {exc!r}")

    async def _equity_now(self) -> tuple[Decimal, Decimal, Decimal, int]:
        """(equity, unrealized PnL, cost of open positions, open count).
        Equity = free USDT (spot + Earn) + open positions at mark price."""
        mark_prices = self.get_mark_prices()
        with session_scope() as session:
            open_positions = PositionRepository(session).get_open_positions()
            unrealized_pnl = Decimal("0")
            for p in open_positions:
                price = mark_prices.get(p.symbol)
                if price is not None:
                    unrealized_pnl += (price - p.avg_entry_price) * p.total_quantity
            open_count = len(open_positions)
            total_open_cost = sum((p.total_cost_usdt for p in open_positions), Decimal("0"))
        equity = await self._trading_balance_usdt(strict=True) + total_open_cost + unrealized_pnl
        return equity, unrealized_pnl, total_open_cost, open_count

    async def _monthly_report_data(self, *, month_to_date: bool) -> MonthlyReportData:
        now = utcnow()
        equity, unrealized, _cost, _count = await self._equity_now()
        since = datetime.combine(month_start_of(now.date()), datetime.min.time(), tzinfo=now.tzinfo) - timedelta(days=1)
        btc_daily = await self._client.get_historical_klines("BTCUSDT", "1d", since, now)
        earn_apr: float | None = None
        if self._earn is not None:
            try:
                earn_apr = (await self._earn.product()).apr
            except Exception as exc:  # noqa: BLE001 - the report shows "н/д" instead
                logger.warning("Earn rate for the monthly report unavailable: %r", exc)
        with session_scope() as session:
            return build_monthly_report(
                session, now=now, equity_now=equity, unrealized_now=unrealized, btc_daily=btc_daily,
                earn_apr=earn_apr, month_to_date=month_to_date, report_hour_utc=self._settings.daily_report_hour_utc,
            )

    async def get_monthly_report_text(self) -> str:
        """/report: the month so far."""
        return format_monthly_report(await self._monthly_report_data(month_to_date=True))

    async def _send_daily_report(self) -> None:
        now = utcnow()
        date_str = now.date().isoformat()
        day_start = floor_to_timeframe(now, Timeframe.D1)
        day_end = day_start + timedelta(days=1)

        current_balance, unrealized_pnl, total_open_cost, open_count = await self._equity_now()
        exposure_pct = float(total_open_cost / current_balance * 100) if current_balance > 0 else 0.0
        btc_regime_value = self._btc_regime.level.value if self._btc_regime else "unknown"

        with session_scope() as session:
            stat = build_daily_stat(
                session, date_str=date_str, day_start=day_start, day_end=day_end,
                current_balance=current_balance, unrealized_pnl=unrealized_pnl,
                open_positions_count=open_count, capital_exposure_pct=exposure_pct, btc_regime=btc_regime_value,
            )
            report_data = DailyReportData.from_model(stat, month_closed_trades(session, date_str))
        await self._notifier.daily_report(report_data)

    # ------------------------------------------------------------------
    # Status heartbeat (proactive push 3x/day, independent of /status pull)
    # ------------------------------------------------------------------

    async def run_status_ping_loop(self) -> None:
        hours = [
            self._settings.status_ping_hour_1_utc,
            self._settings.status_ping_hour_2_utc,
            self._settings.status_ping_hour_3_utc,
        ]
        while True:
            now = utcnow()
            candidates = []
            for hour in hours:
                target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                candidates.append(target)
            next_target = min(candidates)
            await self._sleep_with_heartbeat((next_target - now).total_seconds(), "status_ping")
            try:
                await self._notifier.status_ping(format_status(self.build_status_snapshot()))
            except Exception as exc:  # noqa: BLE001 - a failed ping must not kill the loop
                logger.exception("Status ping failed: %r", exc)

    # ------------------------------------------------------------------
    # Read-only callables for telegram_bot.handlers.BotContext
    # ------------------------------------------------------------------

    def get_mark_prices(self) -> dict[str, Decimal]:
        """Live stream price per tracked symbol (what paper fills and exits
        use), falling back to the last closed 15m candle before the first
        stream tick arrives."""
        prices: dict[str, Decimal] = {}
        for symbol in self._tracked_symbols:
            live = self._market_data.live_price(symbol)
            if live is not None:
                prices[symbol] = Decimal(str(live))
                continue
            snap = self._market_data.snapshot(symbol, Timeframe.M15)
            if snap is not None:
                prices[symbol] = Decimal(str(snap.close))
        return prices

    async def get_balance_text(self) -> str:
        if self._paper_broker is not None:
            mark_prices = self.get_mark_prices()
            account = self._paper_broker.account
            lines = ["БАЛАНС (PAPER)", f"USDT вільно: {account.usdt_balance:.2f}"]
            for asset, qty in account.holdings.items():
                price = mark_prices.get(f"{asset}USDT")
                extra = f" (~{qty * price:.2f} USDT)" if price is not None else ""
                lines.append(f"{asset}: {qty:.6f}{extra}")
            lines.append(f"Загальний капітал: {account.total_equity(mark_prices):.2f} USDT")
            return "\n".join(lines)

        balances = await self._client.get_account_balances()
        earn_line, earn_usdt = None, Decimal("0")
        if self._earn is not None:
            try:
                earn_usdt = await self._earn.balance()
                apr = (await self._earn.product()).apr
                earn_line = f"🏦 USDT в Earn (депозит, {apr * 100:.2f}% річних): {earn_usdt:.2f}"
            except Exception as exc:  # noqa: BLE001 - show the rest of the balance anyway
                logger.warning("Earn balance for /balance failed: %r", exc)
                earn_line = "🏦 USDT в Earn: недоступно (помилка біржі)"
        if not balances and earn_usdt <= 0:
            return "💰 БАЛАНС\n(немає ненульових балансів)"

        # Short on purpose (owner 2026-10-08): where the money is and in what -
        # spot USDT, the Earn deposit, coins worth >= 1 USDT; dust is counted only.
        mark_prices = self.get_mark_prices()
        spot_free, spot_locked = balances.get("USDT", (Decimal("0"), Decimal("0")))
        usdt = spot_free + spot_locked
        coins: list[tuple[str, Decimal]] = []
        dust = 0
        for asset, (free, locked) in balances.items():
            # LD* (e.g. LDUSDT) is how Binance lists the Earn deposit among
            # spot balances - already in the Earn line, never count it twice.
            if asset == "USDT" or (self._earn is not None and asset.startswith("LD")):
                continue
            price = mark_prices.get(f"{asset}USDT")
            value = (free + locked) * price if price is not None else None
            if value is not None and value >= 1:
                coins.append((asset, value))
            else:
                dust += 1
        coins_total = sum((value for _asset, value in coins), Decimal("0"))
        lines = ["💰 БАЛАНС", f"💵 USDT на споті: {usdt:.2f}" + (f" (з них у відкритих ордерах {spot_locked:.2f})" if spot_locked > 0 else "")]
        if earn_line is not None:
            lines.append(earn_line)
        if coins:
            lines.append(f"🪙 У монетах: {coins_total:.2f} USDT")
            lines.append("   " + " · ".join(f"{asset} {value:.2f}" for asset, value in sorted(coins, key=lambda c: -c[1])))
        if dust:
            lines.append(f"🧹 Дрібні залишки: {dust} монет (менше 1 USDT або без ціни, у підсумок не входять)")
        lines.append(f"Разом: ~{usdt + earn_usdt + coins_total:.2f} USDT")
        return "\n".join(lines)

    def build_status_snapshot(self) -> StatusSnapshot:
        flags = self._risk_manager.status()
        mark_prices = self.get_mark_prices()
        with session_scope() as session:
            open_positions = PositionRepository(session).get_open_positions()
            total_unrealized = Decimal("0")
            priced_any = False
            for p in open_positions:
                price = mark_prices.get(p.symbol)
                if price is not None:
                    total_unrealized += (price - p.avg_entry_price) * p.total_quantity
                    priced_any = True
            open_count = len(open_positions)

        effective_dry_run = self._settings.dry_run if self._settings.mode == TradingMode.LIVE else False
        return StatusSnapshot(
            mode=self._settings.mode.value, dry_run=effective_dry_run,
            uptime_seconds=(utcnow() - self.started_at).total_seconds(),
            btc_regime=self._btc_regime.level.value if self._btc_regime else None,
            buy_paused=flags.buy_paused, dca_paused=flags.dca_paused, emergency_stop=flags.emergency_stop,
            consecutive_bad_trades=flags.consecutive_bad_trades,
            open_positions_count=open_count, max_open_positions=self._settings.max_open_positions,
            total_unrealized_pnl_usdt=total_unrealized if priced_any else None,
            max_consecutive_bad_trades=self._settings.max_consecutive_bad_trades,
            watched_symbols=len(self._candidate_symbols),
            market_allows_buys=(
                self._strategy_engine.regime_allows_buy(self._btc_regime.level) if self._btc_regime else None
            ),
            starting=not self._initialized,
            problems=tuple(self._status_problems()) if self._initialized else (),
            macro_phase=self._macro.phase.value if self._macro else None,
            macro_detail=macro_status_detail(self._macro.btc_close, self._macro.sma200) if self._macro else None,
            bear_entry_block=self._bear_entry_block_active(),
        )

    def _status_problems(self) -> list[str]:
        """Plain-language operational problems for the status message; the
        raw diagnostics behind them stay in get_health_snapshot()/the logs.

        Only conditions that need attention: a brief planned reconnect
        (universe rescan swaps the stream) is within the grace period, and a
        task the watchdog already restarted successfully is not a problem -
        restart history is in the logs, and a lifetime counter here would
        keep crying wolf for days after a single recovered hiccup."""
        problems: list[str] = []
        down_for = self._ws_manager.seconds_disconnected("klines")
        kline_age = self._ws_manager.last_message_age_seconds("klines")
        if down_for is not None and down_for > _FEED_DOWN_GRACE_SECONDS:
            problems.append(f"немає зв'язку з біржею вже {_minutes(down_for)} хв, ціни не надходять")
        # Not gated on being connected right now: a connection that keeps
        # flapping (connect, close, retry) would otherwise hide hours without data.
        elif kline_age is not None and kline_age > _STALE_FEED_SECONDS:
            problems.append(f"ціни не оновлювались {_minutes(kline_age)} хв")

        monitor_limit = max(300, 5 * self._rules.scheduler.position_monitor_interval_seconds)
        for name, state in self._watchdog.snapshot().items():
            label = _TASK_LABELS.get(name, name)
            if state["gave_up"]:
                problems.append(f"зупинилась задача \"{label}\"")
            elif name == "position_monitor" and state["running"]:
                heartbeat_age = float(state["seconds_since_heartbeat"])  # type: ignore[arg-type]
                if heartbeat_age > monitor_limit:
                    # The one loop with a fixed short cadence: a stale heartbeat
                    # there means it's hung and open positions aren't managed.
                    problems.append(f"задача \"{label}\" не відповідає {_minutes(heartbeat_age)} хв")
        return problems

    def get_current_regime(self) -> RegimeAssessment | None:
        return self._btc_regime

    def get_macro_assessment(self) -> MacroAssessment | None:
        return self._macro

    def get_latest_signals(self) -> list[TradeDecision]:
        return list(self._latest_decisions.values())

    def get_health_snapshot(self) -> dict[str, Any]:
        kline_age = self._ws_manager.last_message_age_seconds("klines")
        snapshot: dict[str, Any] = {
            "tracked_symbols": len(self._tracked_symbols),
            "candidates": len(self._candidate_symbols),
            "websocket_klines_connected": self._ws_manager.is_connected("klines"),
            "last_kline_age_s": round(kline_age, 1) if kline_age is not None else "never",
        }
        if self._settings.mode == TradingMode.LIVE:
            snapshot["websocket_user_stream_connected"] = self._ws_manager.is_connected("user_data")
        if self._last_universe_scan_at:
            snapshot["last_universe_scan"] = self._last_universe_scan_at.isoformat()
        if self._last_news_refresh_at:
            snapshot["last_news_refresh"] = self._last_news_refresh_at.isoformat()
        snapshot["tasks"] = self._watchdog.snapshot()
        return snapshot
