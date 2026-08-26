from __future__ import annotations

from decimal import Decimal

import pytest

from strategy.take_profit import (
    Fill,
    compute_target_price,
    net_profit_percent,
    should_exit_trailing,
    should_start_trailing,
    trailing_stop_price,
    weighted_average_entry,
)


def test_weighted_average_entry_matches_spec_example():
    # Spec example: $100 @ $100, $50 @ $95, $75 @ $90
    fills = [
        Fill(price=Decimal("100"), quantity=Decimal("100") / Decimal("100")),
        Fill(price=Decimal("95"), quantity=Decimal("50") / Decimal("95")),
        Fill(price=Decimal("90"), quantity=Decimal("75") / Decimal("90")),
    ]
    avg = weighted_average_entry(fills)
    expected = Decimal("225") / (Decimal("100") / Decimal("100") + Decimal("50") / Decimal("95") + Decimal("75") / Decimal("90"))
    assert abs(avg - expected) < Decimal("1e-9")


def test_weighted_average_entry_empty_fills_is_zero():
    assert weighted_average_entry([]) == Decimal("0")


def test_target_price_is_above_naive_ten_percent_due_to_fees_and_slippage():
    avg_entry = Decimal("100")
    target = compute_target_price(
        avg_entry, target_profit_percent=Decimal("10"), taker_fee_rate=Decimal("0.001"),
        expected_slippage_percent=Decimal("0.05"),
    )
    naive = avg_entry * Decimal("1.10")
    assert target > naive


def test_selling_at_target_price_realizes_exactly_target_net_profit():
    avg_entry = Decimal("95.35315985130111524163568773")
    fee = Decimal("0.001")
    slippage = Decimal("0.05")
    target = compute_target_price(avg_entry, target_profit_percent=Decimal("10"), taker_fee_rate=fee, expected_slippage_percent=slippage)
    realized = net_profit_percent(avg_entry, target, taker_fee_rate=fee, expected_slippage_percent=slippage)
    assert abs(realized - Decimal("10")) < Decimal("0.0001")


def test_compute_target_price_rejects_non_positive_entry():
    with pytest.raises(ValueError):
        compute_target_price(Decimal("0"), target_profit_percent=Decimal("10"), taker_fee_rate=Decimal("0.001"), expected_slippage_percent=Decimal("0.05"))


def test_trailing_stop_price_below_peak(settings):
    peak = Decimal("120")
    stop = trailing_stop_price(peak, settings)
    assert stop < peak
    expected = peak * (Decimal(1) - settings.trailing_distance_percent / Decimal(100))
    assert stop == expected


def test_should_start_trailing_only_when_enabled_and_target_reached(settings):
    enabled = settings.model_copy(update={"use_trailing_after_tp": True})
    disabled = settings.model_copy(update={"use_trailing_after_tp": False})

    assert should_start_trailing(current_price=Decimal("111"), target_price=Decimal("110"), settings=enabled)
    assert not should_start_trailing(current_price=Decimal("109"), target_price=Decimal("110"), settings=enabled)
    assert not should_start_trailing(current_price=Decimal("111"), target_price=Decimal("110"), settings=disabled)


def test_should_exit_trailing_on_retrace(settings):
    peak = Decimal("120")
    stop = trailing_stop_price(peak, settings)
    assert should_exit_trailing(stop - Decimal("0.01"), peak, settings)
    assert not should_exit_trailing(peak, peak, settings)
