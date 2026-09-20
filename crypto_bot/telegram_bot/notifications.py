"""Telegram message formatting + the notifier that sends them.

All user-facing text is Ukrainian. Universal technical-analysis jargon (RSI,
MACD, EMA, ADX, VWAP, BTC, USDT, DCA) is left as-is - traders use these exact
acronyms in Ukrainian too, and there is no different Ukrainian term for them.

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
from typing import Any, Protocol

from database.models import DailyStat
from strategy.strategy_engine import (
    BuyExecutedEvent,
    DCAExecutedEvent,
    DelayedFillEvent,
    PositionClosedEvent,
    TradeDecision,
)

logger = logging.getLogger(__name__)

# Public: also used by telegram_bot/handlers.py (/signals, /market, /history)
# to translate the same internal identifiers consistently everywhere they
# reach a Telegram message, not just here.
SIGNAL_LABELS = {
    "rsi_reversal": "Розворот RSI",
    "macd_bullish": "Бичачий MACD",
    "ema_trend": "Тренд по EMA",
    "bollinger_recovery": "Відскок від Bollinger",
    "volume_confirmation": "Підтвердження об'ємом",
    "vwap_recovery": "Відновлення VWAP",
    "market_structure": "Структура ринку",
}

_REGIME_LABELS = {
    "STRONG_BULL": "ПОТУЖНЕ ЗРОСТАННЯ",
    "BULL": "ЗРОСТАННЯ",
    "NEUTRAL": "НЕЙТРАЛЬНИЙ",
    "BEAR": "ПАДІННЯ",
    "STRONG_BEAR": "СИЛЬНЕ ПАДІННЯ",
    "CRASH": "ОБВАЛ",
    "unknown": "невідомо",
}


def regime_label(value: str) -> str:
    return _REGIME_LABELS.get(value, value)


_CLOSE_REASON_LABELS = {
    "TAKE_PROFIT": "ТЕЙК-ПРОФІТ",
    "TRAILING_STOP": "ТРЕЙЛІНГ-СТОП",
    "OPEN_AT_END": "ВІДКРИТА НА КІНЕЦЬ ПЕРІОДУ",  # backtest-only, never appears live
}


def close_reason_label(value: str) -> str:
    return _CLOSE_REASON_LABELS.get(value, value)


def _news_label(score: int) -> str:
    if score <= -80:
        return "КРИТИЧНО НЕГАТИВНІ"
    if score <= -30:
        return "НЕГАТИВНІ"
    if score < 10:
        return "НЕЙТРАЛЬНІ"
    if score < 50:
        return "ПОЗИТИВНІ"
    return "ДУЖЕ ПОЗИТИВНІ"


def _format_timedelta_hours(seconds: float) -> str:
    hours = seconds / 3600
    if hours < 1:
        return f"{seconds / 60:.0f} хв"
    if hours < 48:
        return f"{hours:.1f} год"
    return f"{hours / 24:.1f} д"


def format_uptime(seconds: float) -> str:
    hours, remainder = divmod(int(seconds), 3600)
    minutes = remainder // 60
    return f"{hours} год {minutes} хв"


def format_buy_signal(decision: TradeDecision) -> str:
    top = ", ".join(decision.breakdown.top_reasons(5, label_map=SIGNAL_LABELS)) or "-"
    return (
        "СИГНАЛ НА КУПІВЛЮ\n"
        f"Пара: {decision.symbol}\n"
        f"Бал: {decision.breakdown.final_score:.0f}/100 (потрібно {decision.required_score:.0f})\n"
        f"Топ-сигнали: {top}\n"
        f"Режим BTC: {regime_label(decision.regime.level.value)}"
    )


def format_no_trade(decision: TradeDecision) -> str:
    reasons = "\n".join(decision.reasons) if decision.reasons else "-"
    return f"БЕЗ УГОДИ {decision.symbol}\nБал: {decision.breakdown.final_score:.0f}/100\nПричина:\n{reasons}"


def format_dca_signal(decision: TradeDecision) -> str:
    return (
        "СИГНАЛ ДОКУПКИ (DCA)\n"
        f"Пара: {decision.symbol}\n"
        f"Бал повторного аналізу: {decision.breakdown.final_score:.0f}/100\n"
        f"Режим BTC: {regime_label(decision.regime.level.value)}"
    )


def format_buy_executed(event: BuyExecutedEvent) -> str:
    confirmed = [s.name for s in event.breakdown.signals if s.confirmed]
    signal_lines = "\n".join(f"{SIGNAL_LABELS.get(name, name)} ✓" for name in confirmed) or "-"
    dca_lines = "\n".join(f"{level.drop_percent}%" for level in event.dca_plan) or "-"
    return (
        "КУПІВЛЯ ВИКОНАНА\n"
        f"Пара: {event.symbol}\n"
        f"Ціна: ${event.price:.4f}\n"
        f"Сума: ${event.usdt_amount:.2f}\n"
        f"БАЛ КУПІВЛІ: {event.breakdown.final_score:.0f}/100\n"
        "Сигнали:\n"
        f"{signal_lines}\n"
        "Ринок:\n"
        f"BTC = {regime_label(event.regime.level.value)}\n"
        "Новини:\n"
        f"{event.news_score:+d} {_news_label(event.news_score)}\n"
        "Ціль:\n"
        f"${event.target_price:.4f}\n"
        "Рівні докупки (DCA):\n"
        f"{dca_lines}"
    )


def format_dca_executed(event: DCAExecutedEvent) -> str:
    return (
        "ДОКУПКА (DCA) ВИКОНАНА\n"
        f"Пара: {event.symbol}\n"
        f"Рівень: DCA{event.level_index}\n"
        f"Ціна: ${event.price:.4f}\n"
        f"Сума: ${event.usdt_amount:.2f}\n"
        f"Нова середня ціна входу: ${event.new_avg_entry:.4f}\n"
        f"Нова ціль: ${event.new_target_price:.4f}"
    )


def format_position_closed(event: PositionClosedEvent) -> str:
    return (
        "ПОЗИЦІЯ ЗАКРИТА\n"
        f"Пара: {event.symbol}\n"
        f"Причина: {close_reason_label(event.close_reason)}\n"
        f"Вхід: ${event.avg_entry_price:.4f}  Вихід: ${event.exit_price:.4f}\n"
        f"Чистий PnL: {event.net_pnl_usdt:+.2f} USDT ({event.net_pnl_percent:+.2f}%)\n"
        f"Утримувалась: {_format_timedelta_hours(event.holding_time_seconds)}"
    )


_SIDE_LABELS = {"BUY": "купівля", "SELL": "продаж"}


def format_delayed_fill(event: DelayedFillEvent) -> str:
    return (
        "ВІДКЛАДЕНЕ ВИКОНАННЯ ОРДЕРА\n"
        f"Пара: {event.symbol}\n"
        f"Тип: {_SIDE_LABELS.get(event.side, event.side)} ({event.purpose})\n"
        f"Ціна: ${event.price:.4f}\n"
        f"Кількість: {event.quantity:.6f}\n"
        f"Сума: ${event.usdt_amount:.2f}\n"
        "Ордер стояв у книзі (LIMIT) і щойно виконався."
    )


def format_startup(mode: str, dry_run: bool, open_positions: int) -> str:
    return f"ЗАПУСК\nРежим: {mode}{' (DRY_RUN)' if dry_run else ''}\nВідновлено відкритих позицій: {open_positions}"


def format_shutdown(reason: str = "") -> str:
    return "ЗУПИНКА" + (f"\nПричина: {reason}" if reason else "")


def format_error(message: str) -> str:
    return f"ПОМИЛКА\n{message}"


def format_api_error(message: str) -> str:
    return f"ПОМИЛКА API\n{message}"


def format_news_alert(symbol: str, score: int, headline: str) -> str:
    return f"ВАЖЛИВА НОВИНА\nМонета: {symbol}\nОцінка: {score:+d} {_news_label(score)}\n{headline}"


def format_crash_alert(reasons: list[str]) -> str:
    body = "\n".join(reasons) if reasons else "-"
    return f"ТРИВОГА: ОБВАЛ РИНКУ\nРежим ринку BTC: ОБВАЛ\n{body}"


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
        f"ЩОДЕННИЙ ЗВІТ ({data.date})\n"
        f"Баланс на початок дня: {data.starting_balance:.2f} USDT\n"
        f"Поточний баланс:       {data.current_balance:.2f} USDT\n"
        f"Реалізований PnL:      {data.realized_pnl:+.2f} USDT\n"
        f"Нереалізований PnL:    {data.unrealized_pnl:+.2f} USDT\n"
        f"Угод сьогодні: {data.trades_count}  Закрито: {data.closed_trades_count}\n"
        f"Успішність: {data.win_rate:.1f}%\n"
        f"Сплачено комісій: {data.fees_paid:.2f} USDT\n"
        f"Відкритих позицій: {data.open_positions_count}\n"
        f"Задіяний капітал: {data.capital_exposure_pct:.1f}%\n"
        f"Найкраща угода: {best_trade}\n"
        f"Режим BTC: {regime_label(data.btc_regime)}"
    )


@dataclass(frozen=True)
class StatusSnapshot:
    """Everything `/status` (pull) and the 3x/day heartbeat push (proactive)
    both need - one shared shape so the two can never say something
    different about the same live state."""

    mode: str
    dry_run: bool
    uptime_seconds: float
    btc_regime: str | None
    buy_paused: bool
    dca_paused: bool
    emergency_stop: bool
    consecutive_bad_trades: int
    open_positions_count: int
    max_open_positions: int
    total_unrealized_pnl_usdt: Decimal | None
    health: dict[str, Any]


def format_status(snap: StatusSnapshot) -> str:
    regime_text = regime_label(snap.btc_regime) if snap.btc_regime else "ще не розраховано"
    lines = [
        "СТАТУС",
        f"Режим: {snap.mode}{' (DRY_RUN)' if snap.dry_run else ''}",
        f"Час роботи: {format_uptime(snap.uptime_seconds)}",
        f"Режим BTC: {regime_text}",
        f"Відкритих позицій: {snap.open_positions_count}/{snap.max_open_positions}",
    ]
    if snap.total_unrealized_pnl_usdt is not None:
        pnl = snap.total_unrealized_pnl_usdt
        state = "у плюсі" if pnl > 0 else ("у мінусі" if pnl < 0 else "у нулі")
        lines.append(f"Нереалізований PnL: {pnl:+.2f} USDT ({state})")
    lines.append(
        f"Купівлі призупинені: {'так' if snap.buy_paused else 'ні'}  "
        f"DCA призупинено: {'так' if snap.dca_paused else 'ні'}  "
        f"Аварійна зупинка: {'так' if snap.emergency_stop else 'ні'}"
    )
    lines.append(f"Збиткових угод поспіль: {snap.consecutive_bad_trades}")
    for key, value in snap.health.items():
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


class TelegramSender(Protocol):
    async def send_message(self, chat_id: int, text: str) -> object: ...


class TelegramNotifier:
    """Implements `strategy.strategy_engine.StrategyNotifier` plus the
    additional event types from the spec's Telegram section (startup,
    shutdown, news/crash alerts, daily report, status heartbeat) that
    aren't part of the per-trade decision lifecycle.

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

    async def on_delayed_fill(self, event: DelayedFillEvent) -> None:
        await self._send(format_delayed_fill(event))

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

    async def status_ping(self, text: str) -> None:
        await self._send(text)
