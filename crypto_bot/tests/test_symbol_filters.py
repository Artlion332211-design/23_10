from __future__ import annotations

from decimal import Decimal

import pytest

from exchange.symbol_filters import OrderWouldBeInvalid, SymbolFilters, format_decimal


@pytest.fixture
def sol_filters() -> SymbolFilters:
    return SymbolFilters(
        symbol="SOLUSDT", base_asset="SOL", quote_asset="USDT", status="TRADING",
        tick_size=Decimal("0.01"), min_price=Decimal("0"), max_price=Decimal("100000"),
        lot_step_size=Decimal("0.001"), lot_min_qty=Decimal("0.001"), lot_max_qty=Decimal("1000000"),
        market_lot_step_size=Decimal("0.00001"), market_lot_min_qty=Decimal("0.00001"), market_lot_max_qty=Decimal("1000000"),
        min_notional=Decimal("5"), apply_min_notional_to_market=True,
        base_asset_precision=8, quote_asset_precision=8,
    )


def test_round_price_floors_to_tick_size(sol_filters):
    assert sol_filters.round_price(Decimal("142.5378")) == Decimal("142.53")
    assert sol_filters.round_price(Decimal("142.539")) == Decimal("142.53")


def test_round_quantity_floors_to_lot_step(sol_filters):
    assert sol_filters.round_quantity(Decimal("1.23456")) == Decimal("1.234")
    assert sol_filters.round_quantity(Decimal("1.2399999")) == Decimal("1.239")


def test_round_quantity_market_uses_market_lot_step(sol_filters):
    # market step is finer (0.00001) than the limit step (0.001)
    assert sol_filters.round_quantity(Decimal("1.23456"), market=True) == Decimal("1.23456")


def test_quantity_for_notional_never_exceeds_requested_spend(sol_filters):
    qty = sol_filters.quantity_for_notional(Decimal("142.53"), Decimal("100"))
    spend = qty * Decimal("142.53")
    assert spend <= Decimal("100")
    assert qty >= sol_filters.lot_min_qty


def test_quantity_for_notional_rejects_dust_below_min_notional(sol_filters):
    with pytest.raises(OrderWouldBeInvalid):
        sol_filters.quantity_for_notional(Decimal("142.53"), Decimal("1"))


def test_validate_notional_rejects_below_minimum(sol_filters):
    with pytest.raises(OrderWouldBeInvalid):
        sol_filters.validate_notional(Decimal("1"), Decimal("0.001"))


def test_validate_notional_accepts_at_or_above_minimum(sol_filters):
    sol_filters.validate_notional(Decimal("100"), Decimal("1"))  # 100 USDT notional, no raise


def test_format_decimal_never_uses_scientific_notation():
    tiny = Decimal("1E-7")
    assert "E" not in format_decimal(tiny)
    assert format_decimal(tiny) == "0.0000001"

    big = Decimal("1E+5")
    assert "E" not in format_decimal(big)


def test_is_tradable_reflects_status(sol_filters):
    assert sol_filters.is_tradable()
    halted = sol_filters.__class__(**{**sol_filters.__dict__, "status": "BREAK"})
    assert not halted.is_tradable()
