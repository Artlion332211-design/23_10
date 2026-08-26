"""Telegram bot composition.

Builds the python-telegram-bot `Application`, registers every command
handler, and exposes an explicit `start()`/`stop()` lifecycle so the
asyncio orchestration in `app.py` can run it alongside its own scheduler
tasks - never `Application.run_polling()`, which blocks and wants to own
the event loop itself.
"""

from __future__ import annotations

import logging

from telegram import Bot
from telegram.ext import Application, CommandHandler

from telegram_bot.handlers import (
    CTX_KEY,
    BotContext,
    cmd_balance,
    cmd_config,
    cmd_emergency_stop,
    cmd_history,
    cmd_market,
    cmd_news,
    cmd_pause,
    cmd_pnl,
    cmd_positions,
    cmd_resume,
    cmd_signals,
    cmd_start_dca,
    cmd_status,
    cmd_stop_dca,
    cmd_today,
)

logger = logging.getLogger(__name__)

_COMMANDS = {
    "status": cmd_status,
    "balance": cmd_balance,
    "positions": cmd_positions,
    "signals": cmd_signals,
    "pnl": cmd_pnl,
    "today": cmd_today,
    "history": cmd_history,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "stop_dca": cmd_stop_dca,
    "start_dca": cmd_start_dca,
    "market": cmd_market,
    "news": cmd_news,
    "config": cmd_config,
    "emergency_stop": cmd_emergency_stop,
}


def create_application(bot_token: str) -> Application:
    """Builds the bare `Application` (and its `Bot`) without a `BotContext`.

    Split from `attach_context` because of a construction-order dependency:
    `TelegramNotifier` needs `application.bot` to exist, but `BotContext`
    needs a `TelegramNotifier` (it flows into `StrategyEngine`) plus several
    read-only callables from the fully-wired runtime - so the app
    composition root builds this bare `Application` first, then the
    runtime/notifier/context, then calls `attach_context` last.
    """
    return Application.builder().token(bot_token).build()


def attach_context(application: Application, ctx: BotContext) -> Application:
    application.bot_data[CTX_KEY] = ctx
    for name, handler in _COMMANDS.items():
        application.add_handler(CommandHandler(name, handler))
    return application


class TelegramBotRunner:
    def __init__(self, application: Application) -> None:
        self._app = application

    @property
    def bot(self) -> Bot:
        return self._app.bot

    async def start(self) -> None:
        await self._app.initialize()
        await self._app.start()
        if self._app.updater is not None:
            await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot polling started")

    async def stop(self) -> None:
        if self._app.updater is not None and self._app.updater.running:
            await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()
        logger.info("Telegram bot stopped")
