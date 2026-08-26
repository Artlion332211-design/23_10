"""Composition root and process entry point.

    python app.py                     # runs MODE from .env (PAPER by default)
    python app.py --mode paper        # override MODE for this run
    python app.py --mode live --confirm-live
    python app.py --mode backtest --symbols BTCUSDT,ETHUSDT --start 2024-01-01 --end 2024-06-01

LIVE/PAPER build every component (Binance client, WebSocket manager, market
data, strategy/risk/news engines, execution engine, Telegram bot), run
startup reconciliation, then hand the scheduler loops to a `Watchdog` and
run until SIGINT/SIGTERM. BACKTEST runs one bounded historical replay
through the existing `backtest.engine.BacktestEngine` and exits - this is
intentionally not folded into the live orchestration loop, since a finite
historical replay and a 24/7 event-driven bot have fundamentally different
shapes.

`--mode live` additionally requires `--confirm-live`, on top of everything
`Settings.validate_for_live()` already checks: a config file (`.env`) with
`MODE=LIVE` already reflects a deliberate, persistent choice, but a bare
CLI flag is exactly the kind of one-off command that must never enable real
trading by accident.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from config.settings import AppConfig, Settings, TradingMode, get_config
from database.migrations import run_migrations
from database.repository import PositionRepository
from database.session import init_engine, session_scope
from exchange.binance_client import BinanceClient
from exchange.execution_engine import BinanceExecutionAdapter, ExecutionEngine, OrderExecutor
from exchange.websocket_manager import WebSocketManager
from market.market_data import MarketDataStore
from market.universe_scanner import UniverseScanner
from news.news_engine import NewsEngine
from orchestration.reconciliation import reconcile_live, reconcile_paper
from orchestration.runtime import BotRuntime
from orchestration.watchdog import Watchdog
from paper.simulator import PaperBroker
from risk.risk_manager import RiskManager
from strategy.signal_engine import SignalEngine
from strategy.strategy_engine import StrategyEngine
from telegram_bot.bot import TelegramBotRunner, attach_context, create_application
from telegram_bot.handlers import BotContext
from telegram_bot.notifications import TelegramNotifier
from utils.logging import register_secret, setup_logging
from utils.time import Timeframe, utcnow

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Binance Spot trading bot")
    parser.add_argument("--mode", choices=["backtest", "paper", "live"], default=None, help="Override MODE from .env")
    parser.add_argument("--confirm-live", action="store_true", help="Required alongside --mode live")
    parser.add_argument("--symbols", help="BACKTEST only: comma-separated symbols, e.g. BTCUSDT,ETHUSDT")
    parser.add_argument("--start", help="BACKTEST only: history start date, YYYY-MM-DD")
    parser.add_argument("--end", help="BACKTEST only: history end date, YYYY-MM-DD (default: now)")
    parser.add_argument("--out", default="backtest_report", help="BACKTEST only: report output directory")
    return parser.parse_args()


def _resolve_config(args: argparse.Namespace) -> AppConfig:
    config = get_config()
    if not args.mode:
        return config
    if args.mode == "live" and not args.confirm_live:
        raise SystemExit("--mode live also requires --confirm-live (safety gate against enabling real trading by accident)")
    return AppConfig(env=config.env.model_copy(update={"mode": TradingMode(args.mode.upper())}), rules=config.rules)


async def run_backtest_mode(config: AppConfig, args: argparse.Namespace) -> None:
    from backtest.engine import BacktestEngine
    from backtest.reports import format_summary, write_full_report

    if not args.symbols or not args.start:
        raise SystemExit("--mode backtest requires --symbols and --start (YYYY-MM-DD)")
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=UTC)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=UTC) if args.end else utcnow()

    client = BinanceClient(
        config.env.binance_api_key.get_secret_value(), config.env.binance_api_secret.get_secret_value(),
        testnet=config.env.binance_testnet,
    )
    await client.connect()
    try:
        btc_klines = await client.get_historical_klines("BTCUSDT", "15m", start, end)
        symbol_klines = {s: await client.get_historical_klines(s, "15m", start, end) for s in symbols}
    finally:
        await client.close()

    engine = BacktestEngine(config.env, config.rules)
    result = engine.run(symbol_klines, btc_klines, starting_balance=config.env.paper_starting_balance_usdt)
    print(format_summary(result.metrics, symbols=symbols))
    report_path = write_full_report(result, Path(args.out), symbols=symbols)
    print(f"\nFull report written to {report_path.parent}/")


def _build_paper_broker(settings: Settings, market_data: MarketDataStore, client: BinanceClient) -> PaperBroker:
    async def _price_source(symbol: str) -> Decimal:
        snap = market_data.snapshot(symbol, Timeframe.M15)
        if snap is not None:
            return Decimal(str(snap.close))
        ticker = await client.get_symbol_ticker(symbol)
        return Decimal(str(ticker["price"]))

    return PaperBroker(settings, _price_source)


async def run_live_or_paper_mode(config: AppConfig, mode: TradingMode) -> None:
    settings, rules = config.env, config.rules
    if mode == TradingMode.LIVE:
        settings.validate_for_live()

    for secret in (
        settings.binance_api_key.get_secret_value(), settings.binance_api_secret.get_secret_value(),
        settings.telegram_bot_token.get_secret_value(), settings.cryptopanic_api_token.get_secret_value(),
    ):
        register_secret(secret)

    client = BinanceClient(
        settings.binance_api_key.get_secret_value(), settings.binance_api_secret.get_secret_value(),
        testnet=settings.binance_testnet,
    )
    await client.connect()

    ws_manager = WebSocketManager(client)
    market_data = MarketDataStore(client, rules.indicators)
    risk_manager = RiskManager(settings)
    news_engine = NewsEngine(settings)
    signal_engine = SignalEngine(settings, rules)
    universe_scanner = UniverseScanner(client, settings, rules.universe)

    # PAPER never touches real orders regardless of DRY_RUN - that flag only
    # means something for LIVE ("shadow" live against real market data).
    effective_dry_run = settings.dry_run if mode == TradingMode.LIVE else False
    paper_broker: PaperBroker | None = None
    executor: OrderExecutor
    if mode == TradingMode.PAPER:
        paper_broker = _build_paper_broker(settings, market_data, client)
        executor = paper_broker
    else:
        executor = BinanceExecutionAdapter(client)

    execution_engine = ExecutionEngine(
        executor=executor, filters_provider=client.get_symbol_filters, settings=settings, dry_run=effective_dry_run,
    )

    application = create_application(settings.telegram_bot_token.get_secret_value())
    notifier = TelegramNotifier(application.bot, chat_id=settings.telegram_allowed_user_id)

    strategy_engine = StrategyEngine(
        settings=settings, rules=rules, signal_engine=signal_engine, risk_manager=risk_manager,
        execution_engine=execution_engine, market_data=market_data, news_provider=news_engine, notifier=notifier,
    )

    async def _on_task_gave_up(name: str, exc: BaseException | None) -> None:
        await notifier.on_error(f"Task '{name}' crashed repeatedly and has stopped restarting: {exc!r}")

    watchdog = Watchdog(rules.watchdog, on_task_gave_up=_on_task_gave_up)

    runtime = BotRuntime(
        settings=settings, rules=rules, client=client, ws_manager=ws_manager, market_data=market_data,
        universe_scanner=universe_scanner, risk_manager=risk_manager, news_engine=news_engine,
        execution_engine=execution_engine, strategy_engine=strategy_engine, notifier=notifier,
        watchdog=watchdog, paper_broker=paper_broker, started_at=utcnow(),
    )

    ctx = BotContext(
        settings=settings, rules=rules, risk_manager=risk_manager, news_engine=news_engine,
        allowed_user_id=settings.telegram_allowed_user_id, started_at=runtime.started_at,
        get_balance_text=runtime.get_balance_text, get_current_regime=runtime.get_current_regime,
        get_latest_signals=runtime.get_latest_signals, get_health_snapshot=runtime.get_health_snapshot,
    )
    attach_context(application, ctx)
    bot_runner = TelegramBotRunner(application)

    try:
        if mode == TradingMode.LIVE:
            report = await reconcile_live(client, execution_engine)
        else:
            assert paper_broker is not None
            report = reconcile_paper(settings, paper_broker)
        for note in [*report.notes, *report.position_mismatches]:
            logger.warning("Reconciliation: %s", note)

        with session_scope() as session:
            open_count = len(PositionRepository(session).get_open_positions())

        await bot_runner.start()
        await notifier.startup(settings.mode.value, effective_dry_run, open_count)
        if report.has_warnings:
            await notifier.on_error(
                "Startup reconciliation found position mismatches - check logs and /positions before trusting the numbers."
            )

        await runtime.initialize()
        runtime.register_tasks()
        await watchdog.start()

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                signal.signal(sig, lambda *_: stop_event.set())

        logger.info("Bot running in %s mode (dry_run=%s). Press Ctrl+C to stop.", mode.value, effective_dry_run)
        await stop_event.wait()
        logger.info("Shutdown signal received, stopping...")
        await notifier.shutdown("received stop signal")
    finally:
        await watchdog.stop()
        await ws_manager.stop_all()
        await bot_runner.stop()
        await client.close()


def main() -> None:
    args = _parse_args()
    config = _resolve_config(args)

    setup_logging(config.env.log_dir, config.env.log_level)
    engine = init_engine(config.env.database_url)
    run_migrations(engine)

    if config.env.mode == TradingMode.BACKTEST:
        asyncio.run(run_backtest_mode(config, args))
    else:
        asyncio.run(run_live_or_paper_mode(config, config.env.mode))


if __name__ == "__main__":
    main()
