"""Weighted-average entry price, fee/slippage-aware take-profit target, and
optional trailing-after-TP.

The target is "approximately +10% net profit", not "+10% on price": fees
and slippage eat into proceeds on the way out, so the sell price has to be
set high enough to actually clear the target net of both.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from config.settings import Settings


@dataclass(frozen=True)
class Fill:
    price: Decimal
    quantity: Decimal


def weighted_average_entry(fills: list[Fill]) -> Decimal:
    """Actual executed fills only - never the requested/limit price. Callers
    must pass net quantities (already reduced by any base-asset-denominated
    commission - see `exchange.execution_engine.ExecutionResult.net_base_quantity`),
    so this reflects what is actually held, not what was nominally ordered.
    """
    total_qty = sum((f.quantity for f in fills), Decimal("0"))
    if total_qty <= 0:
        return Decimal("0")
    total_cost = sum((f.price * f.quantity for f in fills), Decimal("0"))
    return total_cost / total_qty


def compute_target_price(
    avg_entry_price: Decimal,
    *,
    target_profit_percent: Decimal,
    taker_fee_rate: Decimal,
    expected_slippage_percent: Decimal,
) -> Decimal:
    """Sell price that nets `target_profit_percent` after the sell-side fee
    and expected slippage.

    net_proceeds_per_unit = sell_price * (1 - fee) * (1 - slippage)
    We want net_proceeds_per_unit / avg_entry_price - 1 >= target_pct, so:
    sell_price = avg_entry_price * (1 + target_pct) / ((1 - fee) * (1 - slippage))
    """
    if avg_entry_price <= 0:
        raise ValueError("avg_entry_price must be positive")
    target_fraction = target_profit_percent / Decimal(100)
    slippage_fraction = expected_slippage_percent / Decimal(100)
    denominator = (Decimal(1) - taker_fee_rate) * (Decimal(1) - slippage_fraction)
    if denominator <= 0:
        raise ValueError("fee + slippage assumptions leave no room for a profitable sell")
    return avg_entry_price * (Decimal(1) + target_fraction) / denominator


def net_profit_percent(
    avg_entry_price: Decimal,
    sell_price: Decimal,
    *,
    taker_fee_rate: Decimal,
    expected_slippage_percent: Decimal,
) -> Decimal:
    """The actual net profit % a sell at `sell_price` would realize, net of
    the upcoming sell-side fee/slippage (the buy-side fee is already baked
    into `avg_entry_price`'s cost basis by `weighted_average_entry`)."""
    if avg_entry_price <= 0:
        return Decimal("0")
    slippage_fraction = expected_slippage_percent / Decimal(100)
    net_proceeds = sell_price * (Decimal(1) - taker_fee_rate) * (Decimal(1) - slippage_fraction)
    return (net_proceeds / avg_entry_price - Decimal(1)) * Decimal(100)


def trailing_stop_price(peak_price: Decimal, settings: Settings) -> Decimal:
    return peak_price * (Decimal(1) - settings.trailing_distance_percent / Decimal(100))


def should_start_trailing(*, current_price: Decimal, target_price: Decimal, settings: Settings) -> bool:
    return settings.use_trailing_after_tp and current_price >= target_price


def should_exit_trailing(current_price: Decimal, peak_price: Decimal, settings: Settings) -> bool:
    return current_price <= trailing_stop_price(peak_price, settings)
