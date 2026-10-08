from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from telegram import InlineKeyboardMarkup

from telegram_bot.handlers import (
    _CONTROL_BUTTONS,
    _INFO_BUTTONS,
    CONTROL_CONFIRM_WINDOW,
    CTX_KEY,
    cmd_start,
    on_button,
    on_control_confirm,
)
from telegram_bot.keyboard import ALL_BUTTONS, MAIN_KEYBOARD_ROWS, main_keyboard
from tests.test_telegram_handlers import _make_context, _make_ctx, _make_update
from utils.time import utcnow


def _press(ctx, label, *, user_id=42, bot_data=None):
    update = _make_update(user_id=user_id)
    update.message.text = label
    context = _make_context(ctx)
    if bot_data is not None:
        context.bot_data = bot_data
    asyncio.run(on_button(update, context))
    return update.message.reply_text


def _confirm(ctx, data, *, message_id=7, user_id=42, bot_data=None):
    update = MagicMock()
    update.effective_user.id = user_id
    query = MagicMock()
    query.data = data
    query.message.message_id = message_id
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    update.callback_query = query
    update.effective_message = MagicMock(reply_text=AsyncMock())
    context = _make_context(ctx)
    if bot_data is not None:
        context.bot_data = bot_data
    asyncio.run(on_control_confirm(update, context))
    return query, update.effective_message.reply_text


def _fresh(action):
    return f"ctl:{action}:{int(utcnow().timestamp())}"


def test_main_keyboard_is_three_rows_of_three_and_every_button_does_something():
    assert [len(row) for row in MAIN_KEYBOARD_ROWS] == [3, 3, 3]
    assert set(ALL_BUTTONS) == set(_INFO_BUTTONS) | set(_CONTROL_BUTTONS)
    markup = main_keyboard()
    assert [[b.text for b in row] for row in markup.keyboard] == MAIN_KEYBOARD_ROWS
    assert markup.resize_keyboard and markup.is_persistent


def test_start_shows_the_buttons(db_engine, settings, rules):
    ctx = _make_ctx(db_engine, settings, rules)
    update = _make_update(user_id=42)

    asyncio.run(cmd_start(update, _make_context(ctx)))

    assert update.message.reply_text.await_args.kwargs["reply_markup"].keyboard[0][0].text == "📊 Статус"


def test_info_buttons_answer_like_their_commands(db_engine, settings, rules):
    ctx = _make_ctx(db_engine, settings, rules)

    assert _press(ctx, "📜 Історія").await_args.args[0] == "Ще немає закритих угод."
    assert _press(ctx, "📊 Статус").await_args.args[0].startswith("СТАТУС")
    assert _press(ctx, "💼 Позиції").await_args.args[0] == "Відкритих позицій немає."


def test_a_stranger_pressing_buttons_gets_nothing(db_engine, settings, rules):
    ctx = _make_ctx(db_engine, settings, rules)

    _press(ctx, "📊 Статус", user_id=7).assert_not_called()
    _press(ctx, "🛑 СТОП", user_id=7).assert_not_called()
    query, _reply = _confirm(ctx, _fresh("stop"), user_id=7)
    query.answer.assert_not_called()
    assert not ctx.risk_manager.status().emergency_stop


def test_control_buttons_only_ask_first(db_engine, settings, rules):
    """A stray tap on the phone must never change trading by itself."""
    ctx = _make_ctx(db_engine, settings, rules)

    for label, action in (("⏸ Пауза", "pause"), ("▶️ Продовжити", "resume"), ("🛑 СТОП", "stop")):
        reply = _press(ctx, label)
        markup = reply.await_args.kwargs["reply_markup"]
        assert isinstance(markup, InlineKeyboardMarkup)
        yes, cancel = markup.inline_keyboard[0]
        assert yes.callback_data.startswith(f"ctl:{action}:")
        assert cancel.callback_data == "ctl:cancel"

    flags = ctx.risk_manager.status()
    assert not (flags.buy_paused or flags.emergency_stop)


def test_confirmed_pause_pauses_once(db_engine, settings, rules):
    ctx = _make_ctx(db_engine, settings, rules)
    bot_data = {CTX_KEY: ctx}  # shared across both taps

    query, reply = _confirm(ctx, _fresh("pause"), bot_data=bot_data)

    assert ctx.risk_manager.status().buy_paused
    assert reply.await_args.args[0] == "Нові купівлі призупинено."
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)  # the buttons are gone

    ctx.risk_manager.resume_trading()
    _confirm(ctx, _fresh("pause"), bot_data=bot_data)  # a second tap on the same prompt
    assert not ctx.risk_manager.status().buy_paused


def test_confirmed_stop_triggers_the_kill_switch_and_resume_clears_it(db_engine, settings, rules):
    ctx = _make_ctx(db_engine, settings, rules)

    _confirm(ctx, _fresh("stop"), message_id=1)
    assert ctx.risk_manager.status().emergency_stop

    _confirm(ctx, _fresh("resume"), message_id=2)
    assert not ctx.risk_manager.status().emergency_stop


def test_an_old_confirmation_changes_nothing(db_engine, settings, rules):
    """Scrolling back and tapping an old "✅ Так, відновити" must not resume
    buying hours after the owner stopped the bot."""
    ctx = _make_ctx(db_engine, settings, rules)
    ctx.risk_manager.trigger_emergency_stop()
    old = int((utcnow() - CONTROL_CONFIRM_WINDOW - timedelta(seconds=1)).timestamp())

    query, reply = _confirm(ctx, f"ctl:resume:{old}")

    assert ctx.risk_manager.status().emergency_stop
    assert "Час на підтвердження минув" in query.edit_message_text.await_args.args[0]
    reply.assert_not_called()


def test_cancel_changes_nothing(db_engine, settings, rules):
    ctx = _make_ctx(db_engine, settings, rules)

    query, _reply = _confirm(ctx, "ctl:cancel")

    assert "Скасовано" in query.edit_message_text.await_args.args[0]
    assert not ctx.risk_manager.status().buy_paused


def test_stop_prompt_warns_when_it_would_sell_everything(db_engine, settings, rules):
    ctx = _make_ctx(db_engine, settings.model_copy(update={"emergency_auto_sell": True}), rules)
    assert "ПРОДАСТЬ УСІ позиції" in _press(ctx, "🛑 СТОП").await_args.args[0]

    ctx = _make_ctx(db_engine, settings.model_copy(update={"emergency_auto_sell": False}), rules)
    text = _press(ctx, "🛑 СТОП").await_args.args[0]
    assert "Відкриті позиції лишаються" in text
    assert "/emergency_stop спрацьовує одразу" in text


def test_startup_message_brings_the_buttons_back_after_a_restart():
    from telegram_bot.notifications import TelegramNotifier

    sent = []

    class Sender:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append(kwargs)

    notifier = TelegramNotifier(Sender(), chat_id=1)
    asyncio.run(notifier.startup("LIVE", False, 3, reply_markup=main_keyboard()))
    asyncio.run(notifier.status_ping("ping"))

    assert sent[0]["reply_markup"].keyboard[0][0].text == "📊 Статус"
    assert sent[1] == {}  # ordinary messages don't touch the keyboard


def test_a_failed_button_removal_never_swallows_the_emergency_stop(db_engine, settings, rules):
    """Review 2026-10-08: the prompt was marked used and the buttons removed
    before the action ran - a Telegram hiccup on the edit left the kill
    switch untriggered and a second tap answered nothing."""
    ctx = _make_ctx(db_engine, settings, rules)
    bot_data = {CTX_KEY: ctx}
    update = MagicMock()
    update.effective_user.id = 42
    query = MagicMock(data=_fresh("stop"))
    query.message.message_id = 9
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock(side_effect=TimeoutError("telegram"))
    update.callback_query = query
    update.effective_message = MagicMock(reply_text=AsyncMock())
    context = _make_context(ctx)
    context.bot_data = bot_data

    asyncio.run(on_control_confirm(update, context))
    assert ctx.risk_manager.status().emergency_stop

    asyncio.run(on_control_confirm(update, context))  # tapped again
    assert query.answer.await_args.args == ("Вже виконано",)
    assert update.effective_message.reply_text.await_count == 1  # the stop ran once
