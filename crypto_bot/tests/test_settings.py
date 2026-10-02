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


def test_strong_signal_entry_plus_the_dca_ladder_must_fit_the_position_cap():
    """50 + 50 + 75 + 75 = 250 fits the 300 cap; a bigger strong entry that
    would let one position outgrow MAX_POSITION_USDT is refused at startup."""
    _settings(strong_signal_order_usdt=Decimal("100"))  # 100 + 200 = 300: fits
    with pytest.raises(ValidationError, match="exceed"):
        _settings(strong_signal_order_usdt=Decimal("150"))


def test_strong_signal_entry_cannot_be_smaller_than_the_normal_entry():
    with pytest.raises(ValidationError, match="STRONG_SIGNAL_ORDER_USDT"):
        _settings(strong_signal_order_usdt=Decimal("10"))


def test_strong_signal_margin_cannot_be_negative():
    with pytest.raises(ValidationError, match="STRONG_SIGNAL_SCORE_MARGIN"):
        _settings(strong_signal_score_margin=-1)
