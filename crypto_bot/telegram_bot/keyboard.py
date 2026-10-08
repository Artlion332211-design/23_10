"""The owner's main-screen buttons (a persistent Telegram reply keyboard).

A button press arrives as an ordinary text message with the button's
label; `handlers.on_button` maps it to the same handler the matching
command uses. Kept in its own module because both the handlers and the
startup message (which (re)attaches the keyboard after every restart)
need it.
"""

from __future__ import annotations

from telegram import ReplyKeyboardMarkup

BUTTON_STATUS = "📊 Статус"
BUTTON_POSITIONS = "💼 Позиції"
BUTTON_BALANCE = "💰 Баланс"
BUTTON_MARKET = "🌍 Ринок"
BUTTON_REPORT = "📅 Звіт"
BUTTON_HISTORY = "📜 Історія"
BUTTON_PAUSE = "⏸ Пауза"
BUTTON_RESUME = "▶️ Продовжити"
BUTTON_STOP = "🛑 СТОП"

MAIN_KEYBOARD_ROWS = [
    [BUTTON_STATUS, BUTTON_POSITIONS, BUTTON_BALANCE],
    [BUTTON_MARKET, BUTTON_REPORT, BUTTON_HISTORY],
    [BUTTON_PAUSE, BUTTON_RESUME, BUTTON_STOP],
]
ALL_BUTTONS = [label for row in MAIN_KEYBOARD_ROWS for label in row]


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(MAIN_KEYBOARD_ROWS, resize_keyboard=True, is_persistent=True)
