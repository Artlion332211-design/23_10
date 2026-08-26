"""BotRuntime: the live/paper composition object.

Owns every long-lived component for LIVE/PAPER mode (Binance client,
WebSocket manager, market data store, strategy/risk/news engines, execution
engine, optional paper broker) and exposes:

* the long-running scheduler loops `app.py` registers with the `Watchdog`
  (`run_position_monitor_loop`, `run_universe_scanner_loop`,
  `run_news_refresh_loop`, `run_daily_report_loop`);
* the read-only callables `telegram_bot.handlers.BotContext` needs
  (`get_balance_text`, `get_current_regime`, `get_latest_signals`,
  `get_health_snapshot`).

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
from database.repository import NewsRepository, PositionRepository
from database.session import session_scope
from exchange.binance_client import BinanceClient
from exchange.execution_engine import ExecutionEngine
from exchange.websocket_manager import WebSocketManager
from market.market_data import MarketDataStore
from market.market_regime import MarketRegimeEngine, RegimeAssessment, RegimeLevel
from market.orderbook import OrderBookSnapshot, parse_order_book
from market.universe_scanner import UniverseScanner
from news.news_engine import NewsEngine
from orchestration.daily_report import build_daily_stat
from orchestration.watchdog import Watchdog
from paper.simulator import PaperBroker
from risk.risk_manager import RiskManager
from strategy.strategy_engine import StrategyEngine, TradeDecision
from telegram_bot.notifications import DailyReportData, TelegramNotifier
from utils.time import Timeframe, floor_to_timeframe, utcnow

logger = logging.getLogger(__name__)

_TRACKED_TIMEFRAMES = (Timeframe.M15, Timeframe.H1, Timeframe.H4)
_BACKFILL_BARS = 300


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

        self._regime_engine = MarketRegimeEngine(rules.crash_detector)
        self._btc_regime: RegimeAssessment | None = None
        self._latest_decisions: dict[str, TradeDecision] = {}
        self._candidate_symbols: set[str] = set()
        self._tracked_symbols: set[str] = set()
        self._alerted_news_ids: set[int] = set()
        self._last_universe_scan_at: datetime | None = None
        self._last_news_refresh_at: datetime | None = None

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

    def register_tasks(self) -> None:
        self._watchdog.register("position_monitor", self.run_position_monitor_loop)
        self._watchdog.register("universe_scanner", self.run_universe_scanner_loop)
        if self._settings.news_enabled:
            self._watchdog.register("news_refresh", self.run_news_refresh_loop)
        self._watchdog.register("daily_report", self.run_daily_report_loop)

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

        await self._ws_manager.stop_stream("klines")
        pairs = [(symbol, tf.value) for symbol in required for tf in _TRACKED_TIMEFRAMES]
        self._ws_manager.start_kline_stream(pairs, self._on_kline_message)
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
            await self._evaluate_entry(symbol)

    def _update_btc_regime(self) -> None:
        snapshots = {tf: self._market_data.snapshot("BTCUSDT", tf) for tf in _TRACKED_TIMEFRAMES}
        if not all(snapshots.values()):
            return
        recent_15m = self._market_data.dataframe("BTCUSDT", Timeframe.M15)
        previous_level = self._btc_regime.level if self._btc_regime else None
        self._btc_regime = self._regime_engine.evaluate(snapshots, recent_15m)  # type: ignore[arg-type]
        if self._btc_regime.level == RegimeLevel.CRASH and previous_level != RegimeLevel.CRASH:
            asyncio.create_task(self._notifier.crash_alert(self._btc_regime.reasons))

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

    async def _trading_balance_usdt(self) -> Decimal:
        if self._paper_broker is not None:
            return self._paper_broker.account.usdt_balance
        balances = await self._client.get_account_balances()
        free, _locked = balances.get("USDT", (Decimal("0"), Decimal("0")))
        return free

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
            open_count = position_repo.count_open()
            open_symbols = [p.symbol for p in position_repo.get_open_positions()]
        if open_count >= self._settings.max_open_positions:
            return

        regime = self._btc_regime or _default_neutral_regime()
        try:
            order_book = await self._get_order_book(symbol)
            trading_balance = await self._trading_balance_usdt()
            decision = await self._strategy_engine.try_open_position(
                symbol, btc_regime=regime, trading_balance_usdt=trading_balance,
                order_book=order_book, open_position_symbols=open_symbols,
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
        with session_scope() as session:
            open_positions = [(p.id, p.symbol) for p in PositionRepository(session).get_open_positions()]
        regime = self._btc_regime or _default_neutral_regime()
        for position_id, symbol in open_positions:
            try:
                order_book = await self._get_order_book(symbol)
                if order_book.is_empty:
                    continue
                await self._strategy_engine.manage_position(
                    position_id, btc_regime=regime, current_price=order_book.mid_price, order_book=order_book,
                )
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not block managing the rest
                logger.exception("Position monitor failed for %s: %r", symbol, exc)
                await self._notifier.on_error(f"Position monitor error for {symbol}: {exc!r}")

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

    async def _send_daily_report(self) -> None:
        now = utcnow()
        date_str = now.date().isoformat()
        day_start = floor_to_timeframe(now, Timeframe.D1)
        day_end = day_start + timedelta(days=1)

        mark_prices = self._mark_prices()
        with session_scope() as session:
            open_positions = PositionRepository(session).get_open_positions()
            unrealized_pnl = Decimal("0")
            for p in open_positions:
                price = mark_prices.get(p.symbol)
                if price is not None:
                    unrealized_pnl += (price - p.avg_entry_price) * p.total_quantity
            open_count = len(open_positions)
            total_open_cost = sum((p.total_cost_usdt for p in open_positions), Decimal("0"))

        current_balance = await self._trading_balance_usdt() + total_open_cost + unrealized_pnl
        exposure_pct = float(total_open_cost / current_balance * 100) if current_balance > 0 else 0.0
        regime_label = self._btc_regime.level.value if self._btc_regime else "unknown"

        with session_scope() as session:
            stat = build_daily_stat(
                session, date_str=date_str, day_start=day_start, day_end=day_end,
                current_balance=current_balance, unrealized_pnl=unrealized_pnl,
                open_positions_count=open_count, capital_exposure_pct=exposure_pct, btc_regime=regime_label,
            )
            report_data = DailyReportData.from_model(stat)
        await self._notifier.daily_report(report_data)

    # ------------------------------------------------------------------
    # Read-only callables for telegram_bot.handlers.BotContext
    # ------------------------------------------------------------------

    def _mark_prices(self) -> dict[str, Decimal]:
        prices: dict[str, Decimal] = {}
        for symbol in self._tracked_symbols:
            snap = self._market_data.snapshot(symbol, Timeframe.M15)
            if snap is not None:
                prices[symbol] = Decimal(str(snap.close))
        return prices

    async def get_balance_text(self) -> str:
        if self._paper_broker is not None:
            mark_prices = self._mark_prices()
            account = self._paper_broker.account
            lines = ["BALANCE (PAPER)", f"USDT free: {account.usdt_balance:.2f}"]
            for asset, qty in account.holdings.items():
                price = mark_prices.get(f"{asset}USDT")
                extra = f" (~{qty * price:.2f} USDT)" if price is not None else ""
                lines.append(f"{asset}: {qty:.6f}{extra}")
            lines.append(f"Total equity: {account.total_equity(mark_prices):.2f} USDT")
            return "\n".join(lines)

        balances = await self._client.get_account_balances()
        if not balances:
            return "BALANCE (LIVE)\n(no non-zero balances)"
        lines = ["BALANCE (LIVE)"]
        for asset, (free, locked) in sorted(balances.items()):
            lines.append(f"{asset}: free={free} locked={locked}")
        return "\n".join(lines)

    def get_current_regime(self) -> RegimeAssessment | None:
        return self._btc_regime

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
