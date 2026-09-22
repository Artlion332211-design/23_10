"""Telegram command handlers.

Every command is gated by `_restricted`: only `TELEGRAM_ALLOWED_USER_ID` may
invoke anything, and any other caller is silently ignored (never told the
command exists, per the spec's Telegram-security rule). Handlers read live
state only through `BotContext`, never by importing exchange/market modules
directly - that keeps this module a thin presentation layer, and keeps it
unit-testable without a real Binance/Telegram connection.

All reply text is Ukrainian - see `telegram_bot/notifications.py`'s module
docstring for the one exception (universal technical-analysis jargon).
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
from telegram_bot.notifications import (
    SIGNAL_LABELS,
    DailyReportData,
    StatusSnapshot,
    close_reason_label,
    format_daily_report,
    format_status,
    regime_label,
)
from utils.time import utcnow

logger = logging.getLogger(__name__)

CTX_KEY = "ctx"

_ACTION_LABELS = {
    "BUY": "КУПІВЛЯ",
    "DCA": "ДОКУПКА",
    "NO_TRADE": "БЕЗ УГОДИ",
    "BLOCKED": "ЗАБЛОКОВАНО",
}


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
    get_mark_prices: Callable[[], dict[str, Decimal]]
    get_status_snapshot: Callable[[], StatusSnapshot]
    trigger_emergency_stop: Callable[[], Awaitable[list[str] | None]]


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


async def _reply(update: Update, text: str) -> None:
    assert update.message is not None
    await update.message.reply_text(text)


@_restricted
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    snapshot = _ctx(context).get_status_snapshot()
    await _reply(update, format_status(snapshot))


@_restricted
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = await _ctx(context).get_balance_text()
    await _reply(update, text)


@_restricted
async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = _ctx(context)
    max_dca = ctx.settings.max_dca_count
    mark_prices = ctx.get_mark_prices()
    with session_scope() as session:
        positions = PositionRepository(session).get_open_positions()
        if not positions:
            await _reply(update, "Відкритих позицій немає.")
            return
        lines = [f"ВІДКРИТІ ПОЗИЦІЇ ({len(positions)}/{ctx.settings.max_open_positions})"]
        for p in positions:
            current = mark_prices.get(p.symbol)
            if current is not None and p.avg_entry_price > 0:
                pnl_pct = (current / p.avg_entry_price - 1) * 100
                state = "у плюсі" if pnl_pct > 0 else ("у мінусі" if pnl_pct < 0 else "у нулі")
                price_info = f"поточна={current:.4f} PnL={pnl_pct:+.2f}% ({state})"
            else:
                price_info = "поточна ціна недоступна"
            lines.append(
                f"{p.symbol}: вхід={p.avg_entry_price:.4f} к-сть={p.total_quantity:.6f} "
                f"{price_info} ціль={p.target_price:.4f} DCA={p.dca_count}/{max_dca} "
                f"відкрито={p.opened_at.date()}"
            )
    await _reply(update, "\n".join(lines))


@_restricted
async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    decisions = _ctx(context).get_latest_signals()
    if not decisions:
        await _reply(update, "Ще немає результатів сканування.")
        return
    lines = ["ТОП СИГНАЛИ"]
    for decision in sorted(decisions, key=lambda d: d.breakdown.final_score, reverse=True)[:10]:
        top = ", ".join(decision.breakdown.top_reasons(3, label_map=SIGNAL_LABELS)) or "-"
        action = _ACTION_LABELS.get(decision.action, decision.action)
        lines.append(f"{decision.symbol}: {decision.breakdown.final_score:.0f}/100 [{action}] - {top}")
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
        f"PNL\nРеалізований PnL (за весь час): {realized:+.2f} USDT\n"
        f"Закритих угод: {total}\nУспішність: {win_rate:.1f}%",
    )


@_restricted
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    today = utcnow().date().isoformat()
    with session_scope() as session:
        stat = DailyStatRepository(session).get(today)
    if stat is None:
        await _reply(update, f"Ще немає статистики за {today}.")
        return
    await _reply(update, format_daily_report(DailyReportData.from_model(stat)))


@_restricted
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with session_scope() as session:
        closed = PositionRepository(session).recent_closed(limit=10)
    if not closed:
        await _reply(update, "Ще немає закритих угод.")
        return
    lines = ["ОСТАННІ УГОДИ"]
    for p in closed:
        assert p.closed_at is not None  # recent_closed() only returns CLOSED positions
        reason = close_reason_label(p.close_reason or "-")
        lines.append(f"{p.symbol}: {(p.realized_pnl_pct or Decimal('0')):+.2f}% ({reason}) закрито {p.closed_at.date()}")
    await _reply(update, "\n".join(lines))


@_restricted
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ctx(context).risk_manager.pause_buys()
    await _reply(update, "Нові купівлі призупинено.")


@_restricted
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    risk_manager = _ctx(context).risk_manager
    # /emergency_stop's own confirmation message tells the operator to use
    # /resume to recover - so this must also clear emergency_stop itself,
    # not just the buy-pause flag, or the kill switch can never be undone
    # from Telegram (can_open_new_position()/can_dca() check emergency_stop
    # independently of buy_paused, and nothing else in the app ever calls
    # RiskManager.clear_emergency_stop()).
    risk_manager.resume_buys()
    risk_manager.clear_emergency_stop()
    await _reply(update, "Нові купівлі відновлено. Аварійну зупинку (якщо була активна) знято.")


@_restricted
async def cmd_stop_dca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ctx(context).risk_manager.stop_dca()
    await _reply(update, "DCA (докупку) вимкнено.")


@_restricted
async def cmd_start_dca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ctx(context).risk_manager.start_dca()
    await _reply(update, "DCA (докупку) увімкнено.")


@_restricted
async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    regime = _ctx(context).get_current_regime()
    if regime is None:
        await _reply(update, "Режим ринку ще не розраховано.")
        return
    reasons = "\n".join(regime.reasons[:5]) if regime.reasons else "-"
    await _reply(update, f"РИНКОВИЙ РЕЖИМ\nBTC: {regime_label(regime.level.value)} (бал {regime.score:.0f})\n{reasons}")


@_restricted
async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with session_scope() as session:
        items = NewsRepository(session).recent(10)
    if not items:
        await _reply(update, "Останніх новин немає.")
        return
    lines = ["ОСТАННІ НОВИНИ"]
    for n in items:
        lines.append(f"[{n.sentiment_score:+d}] {n.title} ({', '.join(n.symbols)})")
    await _reply(update, "\n".join(lines))


@_restricted
async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = _ctx(context).settings
    lines = [
        "НАЛАШТУВАННЯ",
        f"MODE={s.mode.value}  DRY_RUN={s.dry_run}",
        f"INITIAL_ORDER_USDT={s.initial_order_usdt}  MAX_POSITION_USDT={s.max_position_usdt}",
        f"MAX_OPEN_POSITIONS={s.max_open_positions}  MAX_TOTAL_EXPOSURE_PERCENT={s.max_total_exposure_percent}%",
        f"TARGET_PROFIT_PERCENT={s.target_profit_percent}%  USE_TRAILING_AFTER_TP={s.use_trailing_after_tp}",
        f"MIN_BUY_SCORE={s.min_buy_score}  MIN_DCA_SCORE={s.min_dca_score}",
        f"MAX_DCA_COUNT={s.max_dca_count}  Рівні DCA: {s.dca_level_1}%/{s.dca_level_2}%/{s.dca_level_3}%",
        f"BTC_MARKET_FILTER={s.btc_market_filter}  NEWS_ENABLED={s.news_enabled}",
        f"MAX_CONSECUTIVE_BAD_TRADES={s.max_consecutive_bad_trades}  MARKET_CRASH_PAUSE={s.market_crash_pause}",
    ]
    await _reply(update, "\n".join(lines))


@_restricted
async def cmd_emergency_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    failed = await _ctx(context).trigger_emergency_stop()
    if failed is None:
        await _reply(
            update,
            "АВАРІЙНУ ЗУПИНКУ АКТИВОВАНО.\n"
            "Нові купівлі та DCA вимкнено. Відкриті позиції не чіпаються "
            "і продовжують відстежуватись - використай /resume після перевірки, щоб знову дозволити торгівлю.",
        )
    elif failed:
        await _reply(
            update,
            "АВАРІЙНУ ЗУПИНКУ АКТИВОВАНО.\n"
            "Нові купівлі та DCA вимкнено. EMERGENCY_AUTO_SELL активний: спроба ринкового продажу всіх позицій.\n"
            f"Не вдалося ліквідувати: {', '.join(failed)}. Перевір вручну!",
        )
    else:
        await _reply(
            update,
            "АВАРІЙНУ ЗУПИНКУ АКТИВОВАНО.\n"
            "Нові купівлі та DCA вимкнено. EMERGENCY_AUTO_SELL активний: "
            "усі відкриті позиції успішно продано за ринком.",
        )
