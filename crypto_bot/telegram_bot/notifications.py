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
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from database.models import DailyStat
from strategy.strategy_engine import (
    BuyExecutedEvent,
    DCAExecutedEvent,
    DelayedFillEvent,
    DrawdownWarningEvent,
    PositionClosedEvent,
    TradeDecision,
)

logger = logging.getLogger(__name__)

# A repeat of the same error inside this window is counted, not re-sent.
ERROR_REPEAT_WINDOW_SECONDS = 3600

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
    "HARD_PROFIT_CEILING": "АВАРІЙНА МЕЖА ПРИБУТКУ",
    "EMERGENCY_SELL": "АВАРІЙНИЙ ПРОДАЖ",
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


def format_drawdown_warning(event: DrawdownWarningEvent) -> str:
    return (
        f"ПОПЕРЕДЖЕННЯ: ПРОСІДАННЯ ПОЗИЦІЇ НА {event.threshold_percent:.0f}%+\n"
        f"Пара: {event.symbol}\n"
        f"Вхід: ${event.avg_entry_price:.4f}  Поточна: ${event.current_price:.4f}\n"
        f"Фактичне падіння від входу: -{event.drawdown_percent:.2f}%"
    )


def format_startup(mode: str, dry_run: bool, open_positions: int) -> str:
    return f"ЗАПУСК\nРежим: {_mode_text(mode, dry_run)}\nВідновлено відкритих позицій: {open_positions}"


def format_shutdown(reason: str = "") -> str:
    return "ЗУПИНКА" + (f"\nПричина: {reason}" if reason else "")


@dataclass(frozen=True)
class ErrorCategory:
    key: str
    label: str
    explanation: str
    exchange_related: bool  # cleared by the next successful exchange call


# Known failure classes, matched against the raw error text. Each gets one
# plain-language explanation instead of a raw exception dump, and repeats
# collapse into a single alert however many symbols hit it (both a stale
# clock offset and a changed IP hit every symbol at every candle close).
_ERROR_CATEGORIES: list[tuple[re.Pattern[str], ErrorCategory]] = [
    (re.compile(r"-2015|Invalid API-key|401 Unauthorized|'status': 401"), ErrorCategory(
        "binance_auth", "ключ API",
        "Binance не приймає ключ API. Найімовірніше, провайдер змінив IP-адресу ноутбука, а ключ прив'язаний до IP. "
        "Додайте поточну IP на Binance: Управление API -> ключ -> Редактировать ограничения. "
        "Поки це не виправлено, бот не бачить баланс, не купує і НЕ МОЖЕ ПРОДАТИ відкриті позиції.",
        True,
    )),
    (re.compile(r"-1021|outside of the recvWindow"), ErrorCategory(
        "binance_clock", "годинник",
        "Годинник ноутбука розійшовся з годинником Binance. Бот сам звіряє час і повторює запит; "
        "якщо це повідомлення повторюється, напишіть Claude \"перевір бота\".",
        True,
    )),
    (re.compile(r"-1003|\b(418|429)\b|Too many requests|banned until"), ErrorCategory(
        "binance_rate_limit", "ліміт запитів",
        "Binance тимчасово обмежив кількість запитів. Бот зачекає й продовжить сам.",
        True,
    )),
    (re.compile(r"getaddrinfo|ClientConnector|Cannot connect to host|Network is unreachable|TimeoutError|timed out"),
     ErrorCategory(
        "network", "інтернет",
        "Немає зв'язку з інтернетом або з Binance. Бот сам повторить спроби, коли зв'язок повернеться.",
        True,
    )),
]
_BINANCE_ERROR_BODY = re.compile(r'"code":\s*(-?\d+),\s*"msg":\s*"([^"]*)"')
_SYMBOL = re.compile(r"\b[A-Z0-9]{2,20}USDT\b")
_MAX_DETAIL_CHARS = 300


def classify_error(message: str) -> ErrorCategory | None:
    for pattern, category in _ERROR_CATEGORIES:
        if pattern.search(message):
            return category
    return None


def error_key(message: str, category: ErrorCategory | None) -> str:
    """What counts as "the same error" for de-duplication: the category for
    known failure classes; otherwise the text with symbol names and numbers
    blanked out - except order failures, which stay per symbol so a second
    coin's failed order is never hidden behind the first."""
    if category is not None:
        return category.key
    text = message if "order" in message.lower() else _SYMBOL.sub("*", message)
    return re.sub(r"\d+", "#", text)[:160]


def _error_detail(message: str) -> str:
    body = _BINANCE_ERROR_BODY.search(message)
    if body:
        return f"{message.split(':', 1)[0]} - Binance {body.group(1)}: {body.group(2)}"
    return message if len(message) <= _MAX_DETAIL_CHARS else message[:_MAX_DETAIL_CHARS] + "..."


def format_error(
    message: str, *, category: ErrorCategory | None = None, repeats: int = 0, current_ip: str | None = None
) -> str:
    lines = ["ПОМИЛКА"]
    if category is not None:
        lines.append(category.explanation)
    if current_ip:
        lines.append(f"Поточна IP-адреса ноутбука: {current_ip} - додайте саме її (старі адреси можна залишити).")
    lines.append(f"Деталі: {_error_detail(message)}" if category is not None else _error_detail(message))
    if repeats:
        lines.append(f"(така сама помилка повторилась ще {repeats} раз(и) з попереднього повідомлення)")
    return "\n".join(lines)


def format_recovered(labels: list[str]) -> str:
    return "ВІДНОВЛЕНО\nЗв'язок з Binance знову працює (" + ", ".join(labels) + "). Бот продовжує роботу."


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
    max_consecutive_bad_trades: int
    watched_symbols: int
    # The BTC regime's verdict alone (pauses/limits are listed separately).
    market_allows_buys: bool | None
    # Still loading market data after a (re)start - not an outage.
    starting: bool
    # Human-readable operational problems (no exchange feed, a hung
    # internal loop, ...) - empty means everything is healthy. Raw health
    # diagnostics stay out of the message; they're in the logs.
    problems: tuple[str, ...]


def _mode_text(mode: str, dry_run: bool) -> str:
    if mode == "PAPER":
        return "PAPER (віртуальні кошти)"
    if mode == "LIVE":
        return "LIVE (DRY_RUN: ордери не надсилаються)" if dry_run else "LIVE (реальні кошти)"
    return mode


def format_status(snap: StatusSnapshot) -> str:
    if snap.starting:
        health_text = "запускається, завантажую ринкові дані (до хвилини)"
    elif snap.problems:
        health_text = "ПРОБЛЕМА - " + "; ".join(snap.problems) + ". Напишіть Claude \"перевір бота\""
    else:
        health_text = "все працює нормально"

    if snap.btc_regime is None:
        market_text = "ще не розраховано"
    else:
        market_text = regime_label(snap.btc_regime)
        if snap.market_allows_buys is not None:
            market_text += ", ринок дозволяє купівлі" if snap.market_allows_buys else ", ринок забороняє нові купівлі"

    lines = [
        "СТАТУС",
        f"Режим: {_mode_text(snap.mode, snap.dry_run)}",
        f"Працює: {format_uptime(snap.uptime_seconds)}",
        f"Стан: {health_text}",
        f"Ринок (BTC): {market_text}",
        f"Монет під наглядом: {snap.watched_symbols}",
        f"Відкриті позиції: {snap.open_positions_count} з {snap.max_open_positions}",
    ]
    if snap.total_unrealized_pnl_usdt is not None:
        pnl = snap.total_unrealized_pnl_usdt
        state = "у плюсі" if pnl > 0 else ("у мінусі" if pnl < 0 else "у нулі")
        lines.append(f"Прибуток/збиток відкритих позицій: {pnl:+.2f} USDT ({state})")
    lines.append(f"Збиткових угод поспіль: {snap.consecutive_bad_trades} з {snap.max_consecutive_bad_trades}")

    limits = []
    if snap.emergency_stop:
        limits.append("АВАРІЙНА ЗУПИНКА")
    if snap.buy_paused:
        limits.append("купівлі на паузі")
    if snap.dca_paused:
        limits.append("докупівлі (DCA) на паузі")
    lines.append(f"Обмеження: {', '.join(limits) if limits else 'немає'}")
    return "\n".join(lines)


class TelegramSender(Protocol):
    async def send_message(self, chat_id: int, text: str) -> object: ...


PublicIpProvider = Callable[[], Awaitable[str | None]]


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

    def __init__(
        self, sender: TelegramSender, chat_id: int, *, public_ip_provider: PublicIpProvider | None = None
    ) -> None:
        self._sender = sender
        self._chat_id = chat_id
        self._public_ip_provider = public_ip_provider
        # error key -> (monotonic time last sent, repeats suppressed since)
        self._active_errors: dict[str, tuple[float, int]] = {}
        self._error_categories: dict[str, ErrorCategory] = {}

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

    async def on_drawdown_warning(self, event: DrawdownWarningEvent) -> None:
        await self._send(format_drawdown_warning(event))

    async def on_error(self, message: str) -> None:
        """One alert per distinct problem: repeats of the same error within
        ERROR_REPEAT_WINDOW_SECONDS are counted, not sent, and the count is
        reported with the next alert once the window has passed."""
        category = classify_error(message)
        key = error_key(message, category)
        now = time.monotonic()
        last = self._active_errors.get(key)
        if last is not None and now - last[0] < ERROR_REPEAT_WINDOW_SECONDS:
            self._active_errors[key] = (last[0], last[1] + 1)
            return
        self._active_errors[key] = (now, 0)
        if category is not None:
            self._error_categories[key] = category
        current_ip = await self._current_ip() if category is not None and category.key == "binance_auth" else None
        await self._send(format_error(message, category=category, repeats=last[1] if last else 0, current_ip=current_ip))

    async def _current_ip(self) -> str | None:
        if self._public_ip_provider is None:
            return None
        try:
            return await self._public_ip_provider()
        except Exception as exc:  # noqa: BLE001 - the alert must go out even without the IP
            logger.warning("Could not determine public IP for the API-key alert: %r", exc)
            return None

    async def mark_exchange_ok(self) -> None:
        """Call after a successful exchange round trip: closes out any active
        exchange/network alert with a single "recovered" message."""
        recovered = [key for key, cat in self._error_categories.items() if cat.exchange_related]
        if not recovered:
            return
        labels = [self._error_categories.pop(key).label for key in recovered]
        for key in recovered:
            self._active_errors.pop(key, None)
        await self._send(format_recovered(labels))

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
