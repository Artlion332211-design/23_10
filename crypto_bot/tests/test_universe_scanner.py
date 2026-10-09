from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from market.universe_scanner import UniverseScanner


def test_leveraged_token_filter_does_not_reject_ordinary_coins_ending_in_up(settings, rules):
    """A bare endswith("UP") rejected JUP, a top-liquidity pair. A leveraged
    token is an existing asset plus the suffix (BTCUP)."""
    scanner = UniverseScanner(MagicMock(), settings, rules.universe)
    known = {"BTC", "JUP", "BTCUP", "ETH", "ETHDOWN"}

    assert scanner._exclusion_reason("JUPUSDT", "JUP", "USDT", "TRADING", known) is None
    assert scanner._exclusion_reason("BTCUPUSDT", "BTCUP", "USDT", "TRADING", known) == "leveraged token"
    assert scanner._exclusion_reason("ETHDOWNUSDT", "ETHDOWN", "USDT", "TRADING", known) == "leveraged token"


def _monitoring_tags(*symbols):
    tags = MagicMock()
    tags.refresh_if_due = AsyncMock()
    tags.excluded_tags.side_effect = lambda symbol: ("Monitoring",) if symbol in symbols else ()
    return tags


def test_scan_refreshes_binance_tags_and_leaves_tagged_coins_out(settings, rules):
    """EXCLUDED_ASSET_TAGS: a coin Binance may delist ("Monitoring") never
    enters the universe, so its evaluation slot goes to another coin."""
    tuned = settings.model_copy(update={"min_listing_age_days": 0, "min_quote_volume_24h_usdt": Decimal("1")})
    symbols = ("MOVEUSDT", "SOLUSDT")
    client = MagicMock()
    client.get_exchange_info = AsyncMock(return_value={
        s: SimpleNamespace(base_asset=s[:-4], quote_asset="USDT", status="TRADING") for s in symbols
    })
    client.get_ticker_24h = AsyncMock(return_value=[
        {"symbol": s, "quoteVolume": "9000000", "priceChangePercent": "-2", "lastPrice": "1", "count": 50000}
        for s in symbols
    ])
    tags = _monitoring_tags("MOVEUSDT")

    candidates = asyncio.run(UniverseScanner(client, tuned, rules.universe, asset_tags=tags).scan())

    assert [c.symbol for c in candidates] == ["SOLUSDT"]
    tags.refresh_if_due.assert_awaited_once()


def test_scan_without_a_tag_list_keeps_every_coin(settings, rules):
    scanner = UniverseScanner(MagicMock(), settings, rules.universe)
    assert scanner._exclusion_reason("MOVEUSDT", "MOVE", "USDT", "TRADING", {"MOVE"}) is None
