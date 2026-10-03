"""Telegram bot composition.

Builds the python-telegram-bot `Application`, registers every command
handler, and exposes an explicit `start()`/`stop()` lifecycle so the
asyncio orchestration in `app.py` can run it alongside its own scheduler
tasks - never `Application.run_polling()`, which blocks and wants to own
the event loop itself.
"""

from __future__ import annotations

import logging

from telegram import Bot, BotCommand, BotCommandScopeChat
from telegram.ext import Application, CommandHandler, MessageHandler, filters

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
    cmd_report,
    cmd_resume,
    cmd_sell,
    cmd_signals,
    cmd_start_dca,
    cmd_status,
    cmd_stop_dca,
    cmd_today,
    edited_command_hint,
    on_handler_error,
)

logger = logging.getLogger(__name__)

_COMMANDS = {
    "status": cmd_status,
    "balance": cmd_balance,
    "positions": cmd_positions,
    "sell": cmd_sell,
    "signals": cmd_signals,
    "pnl": cmd_pnl,
    "today": cmd_today,
    "report": cmd_report,
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

# The "/" menu in Telegram, set from code at startup so it never drifts from
# the registered handlers (it used to be set by hand in BotFather).
COMMAND_DESCRIPTIONS = {
    "status": "стан бота",
    "positions": "відкриті позиції",
    "sell": "продати позицію по ринку (/sell AAVE так)",
    "market": "ринок і фаза (ведмежий / зростання)",
    "balance": "баланс",
    "pnl": "прибуток / збиток",
    "today": "підсумок за сьогодні",
    "report": "місяць: бот проти BTC і Earn",
    "history": "закриті угоди",
    "signals": "останні сигнали",
    "news": "новини",
    "config": "налаштування",
    "pause": "зупинити нові купівлі",
    "resume": "відновити купівлі",
    "stop_dca": "вимкнути докупки",
    "start_dca": "увімкнути докупки",
    "emergency_stop": "АВАРІЙНА ЗУПИНКА",
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


# A sale (or the report's candle download) waits on Binance for seconds; it
# must not hold up the commands behind it (above all /emergency_stop), so it
# runs as its own task.
_NON_BLOCKING = {"sell", "report"}


def attach_context(application: Application, ctx: BotContext) -> Application:
    application.bot_data[CTX_KEY] = ctx
    for name, handler in _COMMANDS.items():
        # New messages only: an edited message re-runs nothing (see edited_command_hint).
        application.add_handler(
            CommandHandler(name, handler, filters=filters.UpdateType.MESSAGE, block=name not in _NON_BLOCKING)
        )
    application.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.COMMAND, edited_command_hint))
    application.add_error_handler(on_handler_error)
    return application


def menu_commands() -> list[BotCommand]:
    """The "/" menu, in COMMAND_DESCRIPTIONS order (most used first)."""
    ordered = [name for name in COMMAND_DESCRIPTIONS if name in _COMMANDS]
    ordered += [name for name in _COMMANDS if name not in COMMAND_DESCRIPTIONS]
    return [BotCommand(name, COMMAND_DESCRIPTIONS.get(name, name)) for name in ordered]


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
        try:
            # Only the owner's chat gets the menu (a private chat's id is the
            # user's id); strangers opening the bot see no command list.
            ctx: BotContext = self._app.bot_data[CTX_KEY]
            await self._app.bot.set_my_commands(menu_commands(), scope=BotCommandScopeChat(chat_id=ctx.allowed_user_id))
            await self._app.bot.delete_my_commands()  # the public default-scope menu
        except Exception as exc:  # noqa: BLE001 - the menu is a convenience; never block startup on it
            logger.warning("Could not update the Telegram command menu: %r", exc)

    async def stop(self) -> None:
        if self._app.updater is not None and self._app.updater.running:
            await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()
        logger.info("Telegram bot stopped")
