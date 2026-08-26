from __future__ import annotations

from unittest.mock import MagicMock

from telegram_bot.bot import _COMMANDS, CTX_KEY, attach_context, create_application

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
        registered.update(handler.commands)
    assert registered == set(_COMMANDS.keys())
