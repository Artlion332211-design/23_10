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


def _scan(settings, rules, tickers):
    tuned = settings.model_copy(update={"min_listing_age_days": 0, "min_quote_volume_24h_usdt": Decimal("1")})
    client = MagicMock()
    client.get_exchange_info = AsyncMock(return_value={
        t["symbol"]: SimpleNamespace(base_asset=t["symbol"][:-4], quote_asset="USDT", status="TRADING")
        for t in tickers
    })
    client.get_ticker_24h = AsyncMock(return_value=tickers)
    return {c.symbol for c in asyncio.run(UniverseScanner(client, tuned, rules.universe).scan())}


def test_stablecoins_and_gold_tokens_never_enter_the_universe(settings, rules):
    """Incident #24: U, RLUSD and the gold tokens were missing from the
    stablecoin list, and a flat 24h change earns the full pullback bonus, so
    they ranked among the 25 candidates (live: UUSDT evaluated 96 times in a
    week). These tickers carry no 24h range, so only the list can stop them."""
    pegged = ["UUSDT", "RLUSDUSDT", "XUSDUSDT", "USDEUSDT", "BFUSDUSDT", "EURIUSDT", "USDSUSDT", "PAXGUSDT",
              "XAUTUSDT"]
    tickers = [
        {"symbol": s, "quoteVolume": "90000000", "priceChangePercent": "0", "lastPrice": "1", "count": 50000}
        for s in [*pegged, "SOLUSDT"]
    ]

    assert _scan(settings, rules, tickers) == {"SOLUSDT"}


def test_a_pair_whose_price_barely_moved_in_24h_is_left_out(settings, rules):
    """The next stablecoin, before anyone adds it to the list: a 24h high/low
    range under min_price_range_24h_percent (0.5%). A ticker without a range
    is kept rather than guessed about."""
    base = {"quoteVolume": "90000000", "priceChangePercent": "-1", "lastPrice": "1", "count": 50000}
    tickers = [
        {**base, "symbol": "NEWUSDUSDT", "highPrice": "1.0010", "lowPrice": "0.9990"},  # 0.2%
        {**base, "symbol": "SOLUSDT", "highPrice": "105", "lowPrice": "100"},  # 5%
        {**base, "symbol": "ETHUSDT"},  # no range in the answer
        {**base, "symbol": "ZEROUSDT", "highPrice": "1", "lowPrice": "0"},  # unusable range
    ]

    assert _scan(settings, rules, tickers) == {"SOLUSDT", "ETHUSDT", "ZEROUSDT"}
