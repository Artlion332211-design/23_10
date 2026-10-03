from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from config.settings import Settings


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_strong_signal_defaults_are_50_usdt_with_a_5_point_margin():
    s = _settings()
    assert s.strong_signal_order_usdt == Decimal("50")
    assert s.strong_signal_score_margin == 5.0


def test_strong_signal_entry_is_switched_off_not_fatal_when_it_breaks_the_position_cap():
    """50 + 50 + 75 + 75 = 250 fits the 300 cap. A strong entry that would let
    one position outgrow MAX_POSITION_USDT used to stop the bot at startup -
    e.g. after the owner raised only the DCA sizes. Now just that extra is off."""
    fits = _settings(strong_signal_order_usdt=Decimal("100"))  # 100 + 200 = 300
    assert fits.strong_signal_off_reason is None
    assert fits.effective_strong_order_usdt == Decimal("100")

    too_big = _settings(strong_signal_order_usdt=Decimal("150"))
    assert "MAX_POSITION_USDT" in (too_big.strong_signal_off_reason or "")
    assert too_big.effective_strong_order_usdt == too_big.initial_order_usdt


def test_strong_signal_entry_not_above_the_normal_entry_is_off():
    s = _settings(strong_signal_order_usdt=Decimal("10"))
    assert s.strong_signal_off_reason is not None
    assert s.effective_strong_order_usdt == s.initial_order_usdt


def test_normal_entry_plus_the_dca_ladder_must_still_fit_the_position_cap():
    with pytest.raises(ValidationError, match="exceed"):
        _settings(initial_order_usdt=Decimal("120"))  # 120 + 200 > 300


def test_strong_signal_margin_cannot_be_negative():
    with pytest.raises(ValidationError, match="STRONG_SIGNAL_SCORE_MARGIN"):
        _settings(strong_signal_score_margin=-1)
