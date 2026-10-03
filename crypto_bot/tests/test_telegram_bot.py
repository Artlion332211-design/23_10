from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from telegram import Chat, Message, MessageEntity, Update, User
from telegram.ext import CommandHandler

from telegram_bot.bot import (
    _COMMANDS,
    COMMAND_DESCRIPTIONS,
    CTX_KEY,
    TelegramBotRunner,
    attach_context,
    create_application,
    menu_commands,
)

_FAKE_TOKEN = "123456:FAKE-TOKEN-FOR-TESTS"


def test_create_application_builds_bot_without_a_context_yet():
    app = create_application(_FAKE_TOKEN)
    assert app.bot is not None
    assert CTX_KEY not in app.bot_data


def test_attach_context_registers_every_command_and_sets_bot_data():
    app = create_application(_FAKE_TOKEN)
    ctx = MagicMock()

    attach_context(app, ctx)

    assert app.bot_data[CTX_KEY] is ctx
    registered = set()
    for handler in app.handlers[0]:
        if isinstance(handler, CommandHandler):
            registered.update(handler.commands)
    assert registered == set(_COMMANDS.keys())


def _command_update(text: str, *, edited: bool) -> Update:
    message = Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=42, type="private"),
        from_user=User(id=42, first_name="owner", is_bot=False), text=text,
        entities=[MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(text.split()[0]))],
    )
    message.set_bot(MagicMock(username="cryptobot"))
    return Update(update_id=1, edited_message=message) if edited else Update(update_id=1, message=message)


def _matching(app, update) -> list:
    return [h for h in app.handlers[0] if h.check_update(update)]


def test_an_edited_command_is_not_executed_only_answered_with_a_hint():
    """Telegram delivers edits of messages of any age: editing an old
    "/sell AAVE" into "/sell AAVE так" sold at once (and an edited old
    /emergency_stop would fire again)."""
    from telegram_bot.handlers import edited_command_hint

    app = create_application(_FAKE_TOKEN)
    attach_context(app, MagicMock())

    fresh = _matching(app, _command_update("/sell AAVE так", edited=False))
    edited = _matching(app, _command_update("/sell AAVE так", edited=True))

    assert [h.commands for h in fresh] == [frozenset({"sell"})]
    assert len(edited) == 1 and not isinstance(edited[0], CommandHandler)
    assert edited[0].callback is edited_command_hint


def test_sell_runs_without_blocking_other_commands():
    app = create_application(_FAKE_TOKEN)
    attach_context(app, MagicMock())
    blocking = {name: h.block for h in app.handlers[0] if isinstance(h, CommandHandler) for name in h.commands}

    assert blocking["sell"] is False
    assert blocking["emergency_stop"] is True


def test_command_menu_is_set_for_the_owner_chat_only_in_menu_order():
    app = MagicMock()
    app.initialize = AsyncMock()
    app.start = AsyncMock()
    app.updater = None
    app.bot.set_my_commands = AsyncMock()
    app.bot.delete_my_commands = AsyncMock()
    app.bot_data = {CTX_KEY: MagicMock(allowed_user_id=42)}

    asyncio.run(TelegramBotRunner(app).start())

    commands = app.bot.set_my_commands.await_args.args[0]
    assert [c.command for c in commands] == [n for n in COMMAND_DESCRIPTIONS if n in _COMMANDS]
    assert app.bot.set_my_commands.await_args.kwargs["scope"].chat_id == 42
    app.bot.delete_my_commands.assert_awaited_once_with()  # the public default-scope menu
    assert {c.command for c in menu_commands()} == set(_COMMANDS)
