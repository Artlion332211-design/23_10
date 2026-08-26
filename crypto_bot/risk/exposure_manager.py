"""Capital exposure accounting: total deployed vs trading balance, and the
per-day new-capital cap (`MAX_DAILY_NEW_CAPITAL_USDT`).

Kept as small pure/DB-read helpers rather than a stateful class - the
authority for *decisions* based on these numbers is `risk.risk_manager`.
"""

from __future__ import annotations

from decimal import Decimal

from database.repository import PositionRepository, SettingsRepository


def current_exposure(position_repo: PositionRepository, trading_balance_usdt: Decimal) -> tuple[Decimal, Decimal]:
    """Returns (total_open_cost_usdt, exposure_percent)."""
    total_open_cost = position_repo.total_open_cost_usdt()
    if trading_balance_usdt <= 0:
        return total_open_cost, Decimal("100")
    return total_open_cost, total_open_cost / trading_balance_usdt * 100


def _daily_capital_key(day: str) -> str:
    return f"daily_new_capital_{day}"


def get_daily_new_capital(settings_repo: SettingsRepository, day: str) -> Decimal:
    return settings_repo.get_decimal(_daily_capital_key(day), Decimal("0"))


def add_daily_new_capital(settings_repo: SettingsRepository, day: str, amount_usdt: Decimal) -> Decimal:
    new_total = get_daily_new_capital(settings_repo, day) + amount_usdt
    settings_repo.set(_daily_capital_key(day), str(new_total))
    return new_total
