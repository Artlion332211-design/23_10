from __future__ import annotations

from unittest.mock import MagicMock

from market.universe_scanner import UniverseScanner


def test_leveraged_token_filter_does_not_reject_ordinary_coins_ending_in_up(settings, rules):
    """A bare endswith("UP") rejected JUP, a top-liquidity pair. A leveraged
    token is an existing asset plus the suffix (BTCUP)."""
    scanner = UniverseScanner(MagicMock(), settings, rules.universe)
    known = {"BTC", "JUP", "BTCUP", "ETH", "ETHDOWN"}

    assert scanner._exclusion_reason("JUPUSDT", "JUP", "USDT", "TRADING", known) is None
    assert scanner._exclusion_reason("BTCUPUSDT", "BTCUP", "USDT", "TRADING", known) == "leveraged token"
    assert scanner._exclusion_reason("ETHDOWNUSDT", "ETHDOWN", "USDT", "TRADING", known) == "leveraged token"
