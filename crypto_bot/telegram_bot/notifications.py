"""Telegram message formatting + the notifier that sends them.

Formatting is kept as pure `format_*` functions so the exact wording can be
unit-tested without a real Bot or network - `TelegramNotifier` just calls
`bot.send_message` with whatever they return. Plain text throughout (no
Markdown/HTML parse mode): symbol names and news headlines are external,
unescaped text, and Telegram's Markdown/MarkdownV2 parsing raises a hard
error on unescaped special characters - plain text can never fail to send.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from database.models import DailyStat
from strategy.strategy_engine import (
    BuyExecutedEvent,
    DCAExecutedEvent,
    PositionClosedEvent,
    TradeDecision,
)

logger = logging.getLogger(__name__)

_SIGNAL_LABELS = {
    "rsi_reversal": "RSI reversal",
    "macd_bullish": "MACD bullish",
    "ema_trend": "EMA trend",
    "bollinger_recovery": "Bollinger recovery",
    "volume_confirmation": "Volume",
    "vwap_recovery": "VWAP recovery",
    "market_structure": "Market structure",
}


def _news_label(score: int) -> str:
    if score <= -80:
        return "CRITICAL"
    if score <= -30:
        return "BEARISH"
    if score < 10:
        return "NEUTRAL"
    if score < 50:
        return "POSITIVE"
    return "BULLISH"


def _format_timedelta_hours(seconds: float) -> str:
    hours = seconds / 3600
    if hours < 1:
        return f"{seconds / 60:.0f}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def format_buy_signal(decision: TradeDecision) -> str:
    top = ", ".join(decision.breakdown.top_reasons(5)) or "-"
    return (
        "BUY SIGNAL\n"
        f"Pair: {decision.symbol}\n"
        f"Score: {decision.breakdown.final_score:.0f}/100 (required {decision.required_score:.0f})\n"
        f"Top signals: {top}\n"
        f"BTC regime: {decision.regime.level.value}"
    )


def format_no_trade(decision: TradeDecision) -> str:
    reasons = "\n".join(decision.reasons) if decision.reasons else "-"
    return f"NO TRADE {decision.symbol}\nScore: {decision.breakdown.final_score:.0f}/100\nReason:\n{reasons}"


def format_dca_signal(decision: TradeDecision) -> str:
    return (
        "DCA SIGNAL\n"
        f"Pair: {decision.symbol}\n"
        f"Re-analysis score: {decision.breakdown.final_score:.0f}/100\n"
        f"BTC regime: {decision.regime.level.value}"
    )


def format_buy_executed(event: BuyExecutedEvent) -> str:
    confirmed = [s.name for s in event.breakdown.signals if s.confirmed]
    signal_lines = "\n".join(f"{_SIGNAL_LABELS.get(name, name)} ✓" for name in confirmed) or "-"
    dca_lines = "\n".join(f"{level.drop_percent}%" for level in event.dca_plan) or "-"
    return (
        "BUY EXECUTED\n"
        f"Pair: {event.symbol}\n"
        f"Price: ${event.price:.4f}\n"
        f"Amount: ${event.usdt_amount:.2f}\n"
        f"BUY SCORE: {event.breakdown.final_score:.0f}/100\n"
        "Signals:\n"
        f"{signal_lines}\n"
        "Market:\n"
        f"BTC = {event.regime.level.value}\n"
        "News:\n"
        f"{event.news_score:+d} {_news_label(event.news_score)}\n"
        "Target:\n"
        f"${event.target_price:.4f}\n"
        "DCA levels:\n"
        f"{dca_lines}"
    )


def format_dca_executed(event: DCAExecutedEvent) -> str:
    return (
        "DCA EXECUTED\n"
        f"Pair: {event.symbol}\n"
        f"Level: DCA{event.level_index}\n"
        f"Price: ${event.price:.4f}\n"
        f"Amount: ${event.usdt_amount:.2f}\n"
        f"New avg entry: ${event.new_avg_entry:.4f}\n"
        f"New target: ${event.new_target_price:.4f}"
    )


def format_position_closed(event: PositionClosedEvent) -> str:
    return (
        "POSITION CLOSED\n"
        f"Pair: {event.symbol}\n"
        f"Reason: {event.close_reason}\n"
        f"Entry: ${event.avg_entry_price:.4f}  Exit: ${event.exit_price:.4f}\n"
        f"Net PnL: {event.net_pnl_usdt:+.2f} USDT ({event.net_pnl_percent:+.2f}%)\n"
        f"Held: {_format_timedelta_hours(event.holding_time_seconds)}"
    )


def format_startup(mode: str, dry_run: bool, open_positions: int) -> str:
    return f"STARTUP\nMode: {mode}{' (DRY_RUN)' if dry_run else ''}\nOpen positions recovered: {open_positions}"


def format_shutdown(reason: str = "") -> str:
    return "SHUTDOWN" + (f"\nReason: {reason}" if reason else "")


def format_error(message: str) -> str:
    return f"ERROR\n{message}"


def format_api_error(message: str) -> str:
    return f"API ERROR\n{message}"


def format_news_alert(symbol: str, score: int, headline: str) -> str:
    return f"NEWS ALERT\nSymbol: {symbol}\nScore: {score:+d} {_news_label(score)}\n{headline}"


def format_crash_alert(reasons: list[str]) -> str:
    body = "\n".join(reasons) if reasons else "-"
    return f"CRASH ALERT\nBTC market regime: CRASH\n{body}"


@dataclass(frozen=True)
class DailyReportData:
    date: str
    starting_balance: Decimal
    current_balance: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    trades_count: int
    closed_trades_count: int
    win_rate: float
    fees_paid: Decimal
    open_positions_count: int
    capital_exposure_pct: float
    best_trade_symbol: str | None
    best_trade_pct: float | None
    btc_regime: str

    @classmethod
    def from_model(cls, stat: DailyStat) -> DailyReportData:
        return cls(
            date=stat.date, starting_balance=stat.starting_balance, current_balance=stat.ending_balance,
            realized_pnl=stat.realized_pnl, unrealized_pnl=stat.unrealized_pnl, trades_count=stat.trades_count,
            closed_trades_count=stat.closed_trades_count, win_rate=stat.win_rate, fees_paid=stat.fees_paid,
            open_positions_count=stat.open_positions_count, capital_exposure_pct=stat.capital_exposure_pct,
            best_trade_symbol=stat.best_trade_symbol, best_trade_pct=stat.best_trade_pct,
            btc_regime=stat.btc_regime or "unknown",
        )


def format_daily_report(data: DailyReportData) -> str:
    best_trade = f"{data.best_trade_symbol} ({data.best_trade_pct:+.2f}%)" if data.best_trade_symbol else "-"
    return (
        f"DAILY REPORT ({data.date})\n"
        f"Starting balance: {data.starting_balance:.2f} USDT\n"
        f"Current balance:  {data.current_balance:.2f} USDT\n"
        f"Realized PnL:     {data.realized_pnl:+.2f} USDT\n"
        f"Unrealized PnL:   {data.unrealized_pnl:+.2f} USDT\n"
        f"Trades today: {data.trades_count}  Closed: {data.closed_trades_count}\n"
        f"Win rate: {data.win_rate:.1f}%\n"
        f"Fees paid: {data.fees_paid:.2f} USDT\n"
        f"Open positions: {data.open_positions_count}\n"
        f"Capital exposure: {data.capital_exposure_pct:.1f}%\n"
        f"Best trade: {best_trade}\n"
        f"BTC regime: {data.btc_regime}"
    )


class TelegramSender(Protocol):
    async def send_message(self, chat_id: int, text: str) -> object: ...


class TelegramNotifier:
    """Implements `strategy.strategy_engine.StrategyNotifier` plus the
    additional event types from the spec's Telegram section (startup,
    shutdown, news/crash alerts, daily report) that aren't part of the
    per-trade decision lifecycle.

    `on_no_trade` is intentionally a no-op here: NO_TRADE/BLOCKED decisions
    are already recorded to the DB and `signals.log` by StrategyEngine on
    every scan, visible via `/signals` and `/history` - pushing one to
    Telegram for every rejected candidate would spam the chat and isn't in
    the spec's explicit notification list.
    """

    def __init__(self, sender: TelegramSender, chat_id: int) -> None:
        self._sender = sender
        self._chat_id = chat_id

    async def _send(self, text: str) -> None:
        try:
            await self._sender.send_message(chat_id=self._chat_id, text=text)
        except Exception as exc:  # noqa: BLE001 - a failed notification must never crash the trading loop
            logger.error("Failed to send Telegram message: %r", exc)

    async def on_buy_signal(self, decision: TradeDecision) -> None:
        await self._send(format_buy_signal(decision))

    async def on_no_trade(self, decision: TradeDecision) -> None:
        return

    async def on_buy_executed(self, event: BuyExecutedEvent) -> None:
        await self._send(format_buy_executed(event))

    async def on_dca_signal(self, decision: TradeDecision) -> None:
        await self._send(format_dca_signal(decision))

    async def on_dca_executed(self, event: DCAExecutedEvent) -> None:
        await self._send(format_dca_executed(event))

    async def on_position_closed(self, event: PositionClosedEvent) -> None:
        await self._send(format_position_closed(event))

    async def on_error(self, message: str) -> None:
        await self._send(format_error(message))

    async def startup(self, mode: str, dry_run: bool, open_positions: int) -> None:
        await self._send(format_startup(mode, dry_run, open_positions))

    async def shutdown(self, reason: str = "") -> None:
        await self._send(format_shutdown(reason))

    async def api_error(self, message: str) -> None:
        await self._send(format_api_error(message))

    async def news_alert(self, symbol: str, score: int, headline: str) -> None:
        await self._send(format_news_alert(symbol, score, headline))

    async def crash_alert(self, reasons: list[str]) -> None:
        await self._send(format_crash_alert(reasons))

    async def daily_report(self, data: DailyReportData) -> None:
        await self._send(format_daily_report(data))
