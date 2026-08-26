"""Telegram command handlers.

Every command is gated by `_restricted`: only `TELEGRAM_ALLOWED_USER_ID` may
invoke anything, and any other caller is silently ignored (never told the
command exists, per the spec's Telegram-security rule). Handlers read live
state only through `BotContext`, never by importing exchange/market modules
directly - that keeps this module a thin presentation layer, and keeps it
unit-testable without a real Binance/Telegram connection.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from config.settings import RulesConfig, Settings
from database.repository import DailyStatRepository, NewsRepository, PositionRepository
from database.session import session_scope
from market.market_regime import RegimeAssessment
from news.news_engine import NewsEngine
from risk.risk_manager import RiskManager
from strategy.strategy_engine import TradeDecision
from telegram_bot.notifications import DailyReportData, format_daily_report
from utils.time import utcnow

logger = logging.getLogger(__name__)

CTX_KEY = "ctx"


@dataclass
class BotContext:
    settings: Settings
    rules: RulesConfig
    risk_manager: RiskManager
    news_engine: NewsEngine
    allowed_user_id: int
    started_at: datetime
    get_balance_text: Callable[[], Awaitable[str]]
    get_current_regime: Callable[[], RegimeAssessment | None]
    get_latest_signals: Callable[[], list[TradeDecision]]
    get_health_snapshot: Callable[[], dict[str, Any]]


def _ctx(context: ContextTypes.DEFAULT_TYPE) -> BotContext:
    return context.bot_data[CTX_KEY]


def _restricted(
    handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]],
) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]:
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        bot_context = _ctx(context)
        user_id = update.effective_user.id if update.effective_user else None
        if user_id != bot_context.allowed_user_id:
            logger.warning("Unauthorized Telegram command from user_id=%s", user_id)
            return
        await handler(update, context)

    return wrapper


def _format_uptime(delta_seconds: float) -> str:
    hours, remainder = divmod(int(delta_seconds), 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes}m"


async def _reply(update: Update, text: str) -> None:
    assert update.message is not None
    await update.message.reply_text(text)


@_restricted
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = _ctx(context)
    flags = ctx.risk_manager.status()
    regime = ctx.get_current_regime()
    health = ctx.get_health_snapshot()
    uptime = (utcnow() - ctx.started_at).total_seconds()

    lines = [
        "STATUS",
        f"Mode: {ctx.settings.mode.value}{' (DRY_RUN)' if ctx.settings.dry_run else ''}",
        f"Uptime: {_format_uptime(uptime)}",
        f"BTC regime: {regime.level.value if regime else 'not yet computed'}",
        f"Buy paused: {flags.buy_paused}  DCA paused: {flags.dca_paused}  Emergency stop: {flags.emergency_stop}",
        f"Consecutive bad trades: {flags.consecutive_bad_trades}",
    ]
    for key, value in health.items():
        lines.append(f"{key}: {value}")
    await _reply(update, "\n".join(lines))


@_restricted
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = await _ctx(context).get_balance_text()
    await _reply(update, text)


@_restricted
async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    max_dca = _ctx(context).settings.max_dca_count
    with session_scope() as session:
        positions = PositionRepository(session).get_open_positions()
        if not positions:
            await _reply(update, "No open positions.")
            return
        lines = ["OPEN POSITIONS"]
        for p in positions:
            lines.append(
                f"{p.symbol}: avg={p.avg_entry_price:.4f} qty={p.total_quantity:.6f} "
                f"target={p.target_price:.4f} dca={p.dca_count}/{max_dca} opened={p.opened_at.date()}"
            )
    await _reply(update, "\n".join(lines))


@_restricted
async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    decisions = _ctx(context).get_latest_signals()
    if not decisions:
        await _reply(update, "No recent scan results yet.")
        return
    lines = ["TOP SIGNALS"]
    for decision in sorted(decisions, key=lambda d: d.breakdown.final_score, reverse=True)[:10]:
        top = ", ".join(decision.breakdown.top_reasons(3)) or "-"
        lines.append(f"{decision.symbol}: {decision.breakdown.final_score:.0f}/100 [{decision.action}] - {top}")
    await _reply(update, "\n".join(lines))


@_restricted
async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with session_scope() as session:
        closed = PositionRepository(session).recent_closed(limit=1000)
    realized = sum((p.realized_pnl_usdt or Decimal("0") for p in closed), Decimal("0"))
    wins = sum(1 for p in closed if (p.realized_pnl_usdt or Decimal("0")) > 0)
    total = len(closed)
    win_rate = (wins / total * 100) if total else 0.0
    await _reply(
        update,
        f"PNL\nRealized PnL (all time): {realized:+.2f} USDT\nClosed trades: {total}\nWin rate: {win_rate:.1f}%",
    )


@_restricted
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    today = utcnow().date().isoformat()
    with session_scope() as session:
        stat = DailyStatRepository(session).get(today)
    if stat is None:
        await _reply(update, f"No stats recorded yet for {today}.")
        return
    report = DailyReportData(
        date=stat.date, starting_balance=stat.starting_balance, current_balance=stat.ending_balance,
        realized_pnl=stat.realized_pnl, unrealized_pnl=stat.unrealized_pnl, trades_count=stat.trades_count,
        closed_trades_count=stat.closed_trades_count, win_rate=stat.win_rate, fees_paid=stat.fees_paid,
        open_positions_count=stat.open_positions_count, capital_exposure_pct=stat.capital_exposure_pct,
        best_trade_symbol=stat.best_trade_symbol, best_trade_pct=stat.best_trade_pct,
        btc_regime=stat.btc_regime or "unknown",
    )
    await _reply(update, format_daily_report(report))


@_restricted
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with session_scope() as session:
        closed = PositionRepository(session).recent_closed(limit=10)
    if not closed:
        await _reply(update, "No closed trades yet.")
        return
    lines = ["RECENT TRADES"]
    for p in closed:
        assert p.closed_at is not None  # recent_closed() only returns CLOSED positions
        lines.append(f"{p.symbol}: {(p.realized_pnl_pct or Decimal('0')):+.2f}% ({p.close_reason}) closed {p.closed_at.date()}")
    await _reply(update, "\n".join(lines))


@_restricted
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ctx(context).risk_manager.pause_buys()
    await _reply(update, "New BUYs paused.")


@_restricted
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ctx(context).risk_manager.resume_buys()
    await _reply(update, "New BUYs resumed.")


@_restricted
async def cmd_stop_dca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ctx(context).risk_manager.stop_dca()
    await _reply(update, "DCA disabled.")


@_restricted
async def cmd_start_dca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ctx(context).risk_manager.start_dca()
    await _reply(update, "DCA enabled.")


@_restricted
async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    regime = _ctx(context).get_current_regime()
    if regime is None:
        await _reply(update, "Market regime not yet computed.")
        return
    reasons = "\n".join(regime.reasons[:5]) if regime.reasons else "-"
    await _reply(update, f"MARKET REGIME\nBTC: {regime.level.value} (score {regime.score:.0f})\n{reasons}")


@_restricted
async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with session_scope() as session:
        items = NewsRepository(session).recent(10)
    if not items:
        await _reply(update, "No recent news.")
        return
    lines = ["RECENT NEWS"]
    for n in items:
        lines.append(f"[{n.sentiment_score:+d}] {n.title} ({', '.join(n.symbols)})")
    await _reply(update, "\n".join(lines))


@_restricted
async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = _ctx(context).settings
    lines = [
        "CONFIG",
        f"MODE={s.mode.value}  DRY_RUN={s.dry_run}",
        f"INITIAL_ORDER_USDT={s.initial_order_usdt}  MAX_POSITION_USDT={s.max_position_usdt}",
        f"MAX_OPEN_POSITIONS={s.max_open_positions}  MAX_TOTAL_EXPOSURE_PERCENT={s.max_total_exposure_percent}%",
        f"TARGET_PROFIT_PERCENT={s.target_profit_percent}%  USE_TRAILING_AFTER_TP={s.use_trailing_after_tp}",
        f"MIN_BUY_SCORE={s.min_buy_score}  MIN_DCA_SCORE={s.min_dca_score}",
        f"MAX_DCA_COUNT={s.max_dca_count}  DCA levels: {s.dca_level_1}%/{s.dca_level_2}%/{s.dca_level_3}%",
        f"BTC_MARKET_FILTER={s.btc_market_filter}  NEWS_ENABLED={s.news_enabled}",
        f"MAX_CONSECUTIVE_BAD_TRADES={s.max_consecutive_bad_trades}  MARKET_CRASH_PAUSE={s.market_crash_pause}",
    ]
    await _reply(update, "\n".join(lines))


@_restricted
async def cmd_emergency_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ctx(context).risk_manager.trigger_emergency_stop()
    await _reply(
        update,
        "EMERGENCY STOP triggered.\n"
        "New BUYs and DCA are disabled. Existing positions are left untouched "
        "and continue to be monitored - use /resume after review to re-enable trading.",
    )
