from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from telegram import InlineKeyboardMarkup

from database.models import PositionStatus
from database.repository import PositionRepository
from database.session import session_scope
from strategy.strategy_engine import ManualSellResult
from telegram_bot.handlers import (
    _CONTROL_BUTTONS,
    _INFO_BUTTONS,
    CONTROL_CONFIRM_WINDOW,
    CTX_KEY,
    cmd_start,
    on_button,
    on_control_confirm,
    on_sell_button,
)
from telegram_bot.keyboard import ALL_BUTTONS, MAIN_KEYBOARD_ROWS, main_keyboard
from tests.test_telegram_handlers import _make_context, _make_ctx, _make_update, _open
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


# ---------------------------------------------------------------------------
# 💸 sell buttons under /positions (owner request 2026-10-11)
# ---------------------------------------------------------------------------

_GONE = "Ця кнопка вже не діє - відкрий 💼 Позиції ще раз"


def _sell_setup(db_engine, settings, rules):
    """SOL bought at 100, now 105 (+4.84 USDT after fees). Mutate `prices` to move it."""
    position_id = _open("SOLUSDT", "100")
    ctx = _make_ctx(db_engine, settings, rules)
    prices = {"SOLUSDT": Decimal("105")}
    ctx.get_mark_prices = lambda: dict(prices)
    ctx.manual_sell = AsyncMock(return_value=ManualSellResult("sold"))
    return ctx, position_id, {CTX_KEY: ctx}, prices


def _tap_update(ctx, data, *, bot_data, user_id=42):
    update = MagicMock()
    update.effective_user.id = user_id
    query = MagicMock(data=data)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.callback_query = query
    update.effective_message = MagicMock(reply_text=AsyncMock())
    context = _make_context(ctx)
    context.bot_data = bot_data
    return update, context


def _tap(ctx, data, *, bot_data, user_id=42):
    """One tap on a sell button; returns (query, the chat's reply_text mock)."""
    update, context = _tap_update(ctx, data, bot_data=bot_data, user_id=user_id)
    asyncio.run(on_sell_button(update, context))
    return update.callback_query, update.effective_message.reply_text


def _ask(ctx, position_id, bot_data):
    """Taps 💸 under /positions; returns (prompt text, ✅ data, ❌ data)."""
    _query, reply = _tap(ctx, f"sell:ask:{position_id}", bot_data=bot_data)
    yes, no = reply.await_args.kwargs["reply_markup"].inline_keyboard[0]
    return reply.await_args.args[0], yes.callback_data, no.callback_data


def test_the_positions_button_shows_a_sell_button_for_a_position_in_profit(db_engine, settings, rules):
    ctx, position_id, _bot_data, _prices = _sell_setup(db_engine, settings, rules)

    markup = _press(ctx, "💼 Позиції").await_args.kwargs["reply_markup"]

    assert markup.inline_keyboard[0][0].callback_data == f"sell:ask:{position_id}"


def test_a_sell_button_only_asks_with_fresh_numbers(db_engine, settings, rules):
    """A stray tap must never sell by itself."""
    ctx, position_id, bot_data, _prices = _sell_setup(db_engine, settings, rules)

    text, yes, no = _ask(ctx, position_id, bot_data)

    assert text.startswith("💸 ПРОДАТИ ВСЮ ПОЗИЦІЮ SOL ПО РИНКУ?")
    assert "≈ +4.84 USDT (+4.84%) після комісій" in text
    assert "Кнопка «Так» діє 2 хв" in text
    assert yes.startswith(f"sell:yes:{position_id}:") and no.startswith(f"sell:no:{position_id}:")
    ctx.manual_sell.assert_not_called()


def test_a_confirmed_sale_sells_once(db_engine, settings, rules):
    ctx, position_id, bot_data, _prices = _sell_setup(db_engine, settings, rules)
    _text, yes, _no = _ask(ctx, position_id, bot_data)

    query, reply = _tap(ctx, yes, bot_data=bot_data)

    ctx.manual_sell.assert_awaited_once_with("SOLUSDT")
    query.edit_message_text.assert_awaited_once_with("Продаю SOLUSDT по ринку...")  # the buttons are gone
    assert reply.await_args.args[0].startswith("✅ SOLUSDT продано повністю")

    again, _reply = _tap(ctx, yes, bot_data=bot_data)
    assert again.answer.await_args.args == (_GONE,)
    ctx.manual_sell.assert_awaited_once()


def test_two_quick_taps_on_yes_start_one_sale(db_engine, settings, rules):
    """The sale runs as its own task (it waits on Binance), so two taps are
    handled at the same time; the second must not start a second sale."""
    ctx, position_id, bot_data, _prices = _sell_setup(db_engine, settings, rules)
    _text, yes, _no = _ask(ctx, position_id, bot_data)

    async def slow_sale(symbol):
        await asyncio.sleep(0.01)
        return ManualSellResult("sold")

    ctx.manual_sell = AsyncMock(side_effect=slow_sale)
    first = _tap_update(ctx, yes, bot_data=bot_data)
    second = _tap_update(ctx, yes, bot_data=bot_data)

    async def both():
        await asyncio.gather(on_sell_button(*first), on_sell_button(*second))

    asyncio.run(both())
    ctx.manual_sell.assert_awaited_once_with("SOLUSDT")


def test_an_old_sell_confirmation_sells_nothing(db_engine, settings, rules):
    ctx, position_id, bot_data, _prices = _sell_setup(db_engine, settings, rules)
    _text, yes, _no = _ask(ctx, position_id, bot_data)
    key = yes.split(":", 2)[2]
    bot_data["sell_offers"][key] = replace(bot_data["sell_offers"][key], expires=utcnow() - timedelta(seconds=1))

    query, _reply = _tap(ctx, yes, bot_data=bot_data)

    assert "Час на підтвердження минув" in query.edit_message_text.await_args.args[0]
    ctx.manual_sell.assert_not_called()


def test_the_button_never_sells_a_position_no_longer_in_profit(db_engine, settings, rules):
    """The button promises "close it in profit": a position that slipped to
    a loss after fees between the prompt and the tap is left alone."""
    ctx, position_id, bot_data, prices = _sell_setup(db_engine, settings, rules)
    _text, yes, _no = _ask(ctx, position_id, bot_data)
    prices["SOLUSDT"] = Decimal("100.1")  # still above the entry on price - a loss after fees

    query, _reply = _tap(ctx, yes, bot_data=bot_data)

    text = query.edit_message_text.await_args.args[0]
    assert "SOL уже не в плюсі (-0.05 USDT після комісій) - нічого не продано" in text
    assert "/sell SOL" in text
    _query, reply = _tap(ctx, f"sell:ask:{position_id}", bot_data=bot_data)  # nor does a new tap offer it
    assert "уже не в плюсі" in reply.await_args.args[0]
    assert "reply_markup" not in reply.await_args.kwargs
    ctx.manual_sell.assert_not_called()


def test_the_button_never_sells_a_position_that_changed_or_closed(db_engine, settings, rules):
    ctx, position_id, bot_data, _prices = _sell_setup(db_engine, settings, rules)

    _text, yes, _no = _ask(ctx, position_id, bot_data)
    with session_scope() as session:  # a DCA filled after the prompt
        PositionRepository(session).get(position_id).total_quantity += Decimal("0.5")
    query, _reply = _tap(ctx, yes, bot_data=bot_data)
    assert "Позиція SOL змінилась" in query.edit_message_text.await_args.args[0]

    _text, yes, _no = _ask(ctx, position_id, bot_data)
    with session_scope() as session:  # the bot's own take-profit closed it
        PositionRepository(session).get(position_id).status = PositionStatus.CLOSED
    query, _reply = _tap(ctx, yes, bot_data=bot_data)
    assert "Цієї позиції вже немає" in query.edit_message_text.await_args.args[0]

    ctx.manual_sell.assert_not_called()


def test_cancel_sells_nothing_and_its_yes_button_then_does_nothing(db_engine, settings, rules):
    ctx, position_id, bot_data, _prices = _sell_setup(db_engine, settings, rules)
    _text, yes, no = _ask(ctx, position_id, bot_data)

    query, _reply = _tap(ctx, no, bot_data=bot_data)
    assert query.edit_message_text.await_args.args[0] == "❌ Скасовано - нічого не продано."

    again, _reply = _tap(ctx, yes, bot_data=bot_data)
    assert again.answer.await_args.args == (_GONE,)
    ctx.manual_sell.assert_not_called()


def test_after_a_restart_an_old_sell_confirmation_does_nothing(db_engine, settings, rules):
    ctx, position_id, bot_data, _prices = _sell_setup(db_engine, settings, rules)
    _text, yes, _no = _ask(ctx, position_id, bot_data)

    query, _reply = _tap(ctx, yes, bot_data={CTX_KEY: ctx})  # a restart forgets every offer

    assert query.answer.await_args.args == (_GONE,)
    ctx.manual_sell.assert_not_called()


def test_a_stranger_tapping_sell_buttons_gets_nothing(db_engine, settings, rules):
    ctx, position_id, bot_data, _prices = _sell_setup(db_engine, settings, rules)
    _text, yes, _no = _ask(ctx, position_id, bot_data)

    query, reply = _tap(ctx, yes, bot_data=bot_data, user_id=7)
    query.answer.assert_not_called()
    _query, reply = _tap(ctx, f"sell:ask:{position_id}", bot_data=bot_data, user_id=7)
    reply.assert_not_called()
    ctx.manual_sell.assert_not_called()

    _tap(ctx, yes, bot_data=bot_data)  # the owner's own tap still works
    ctx.manual_sell.assert_awaited_once_with("SOLUSDT")
