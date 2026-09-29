from __future__ import annotations

import asyncio
from datetime import timedelta

import aiohttp

from database.repository import NewsRepository
from database.session import session_scope
from news.news_engine import NewsEngine
from news.sentiment import CRITICAL_SCORE_CEILING, extract_symbols, score_text
from news.sources import RawNewsItem
from utils.time import utcnow


def test_critical_keyword_forces_score_below_ceiling():
    result = score_text("Exchange X Hacked, Millions in Funds Stolen")
    assert result.critical
    assert result.score <= CRITICAL_SCORE_CEILING


def test_positive_keywords_score_positive():
    result = score_text("Spot ETF Approved, Major Partnership Announced")
    assert not result.critical
    assert result.score > 0


def test_neutral_text_scores_zero():
    result = score_text("Daily market recap and price overview")
    assert result.score == 0
    assert not result.critical


def test_mixed_news_partially_offsets():
    good = score_text("Major partnership announced")
    bad = score_text("Major partnership announced amid ongoing investigation")
    assert bad.score < good.score


def test_extract_symbols_matches_ticker_and_alias():
    known = {"BTC", "ETH", "SOL"}
    assert extract_symbols("SOLUSDT price surges", "", known) == ["SOL"]
    assert extract_symbols("Bitcoin hits new high", "", known) == ["BTC"]


def test_extract_symbols_matches_trading_pair_suffix():
    known = {"BTC", "ETH", "SOL"}
    # "ETHBTC" is a real Binance pair name: base ETH quoted in BTC. Stripping
    # the BTC quote suffix should recover the base asset ETH.
    assert extract_symbols("ETHBTC breaks key resistance level", "", known) == ["ETH"]


def test_extract_symbols_falls_back_to_market():
    assert extract_symbols("Global regulators discuss crypto framework", "", {"BTC", "ETH"}) == ["MARKET"]


class FakeSource:
    def __init__(self, name: str, items: list[RawNewsItem]) -> None:
        self.name = name
        self._items = items

    async def fetch(self, session: aiohttp.ClientSession) -> list[RawNewsItem]:
        return self._items


class FailingSource:
    name = "failing"

    async def fetch(self, session: aiohttp.ClientSession) -> list[RawNewsItem]:
        raise RuntimeError("network down")


def test_news_engine_refresh_dedups_and_scores(db_engine, settings):
    now = utcnow()
    items = [
        RawNewsItem(source="a", title="SOL Network Hacked, Funds Stolen", url="http://a.com/1", published_at=now),
        RawNewsItem(source="b", title="SOL Network Hacked, Funds Stolen!!", url="http://b.com/1-repost", published_at=now + timedelta(minutes=1)),
        RawNewsItem(source="a", title="Bitcoin ETF approved by regulators", url="http://a.com/2", published_at=now),
    ]
    engine = NewsEngine(
        settings.model_copy(update={"news_enabled": True}),
        known_bases={"SOL", "BTC"},
        sources=[FakeSource("a", items[:1] + items[2:]), FakeSource("b", items[1:2]), FailingSource()],
    )

    stored = asyncio.run(engine.refresh())
    # 3 raw items in, but item[1] is a near-duplicate of item[0] -> only 2 stored
    assert stored == 2

    with session_scope() as session:
        all_news = NewsRepository(session).recent(10)
        assert len(all_news) == 2
        assert any(n.critical for n in all_news)


def test_news_engine_disabled_stores_nothing(db_engine, settings):
    engine = NewsEngine(
        settings.model_copy(update={"news_enabled": False}),
        known_bases={"SOL"},
        sources=[FakeSource("a", [RawNewsItem(source="a", title="SOL hacked", url="http://x", published_at=utcnow())])],
    )
    stored = asyncio.run(engine.refresh())
    assert stored == 0


def test_get_symbol_news_score_critical_dominates(db_engine, settings):
    engine = NewsEngine(settings, known_bases={"SOL"}, sources=[])
    with session_scope() as session:
        repo = NewsRepository(session)
        repo.add(source="a", title="SOL exploit drains funds", url="http://x/1", published_at=utcnow(),
                  fetched_at=utcnow(), sentiment_score=-95, symbols=["SOL"], dedup_hash="h1", critical=True)
        repo.add(source="a", title="SOL minor partnership", url="http://x/2", published_at=utcnow(),
                  fetched_at=utcnow(), sentiment_score=20, symbols=["SOL"], dedup_hash="h2", critical=False)

    assessment = asyncio.run(engine.get_symbol_news_score("SOLUSDT"))
    assert assessment.critical
    assert assessment.score <= CRITICAL_SCORE_CEILING


def test_keywords_match_whole_words_not_substrings():
    # Real false positives from substring matching: "hackathon" read as a
    # critical "hack", "banks" as "ban", "refine" as "fine".
    assert not score_text("Solana hackathon winners announced").critical
    assert score_text("Banks embrace stablecoins").score == 0
    assert score_text("Team plans to refine the roadmap").score == 0
    # Inflections of real keywords still match.
    assert score_text("Protocol hacked overnight").critical
    assert score_text("Exchange hacks cost users millions").critical
    assert score_text("Regulator bans token sales").score < 0


def _add_news(repo, *, title, symbols, critical, score, dedup):
    repo.add(source="a", title=title, url=f"http://x/{dedup}", published_at=utcnow(), fetched_at=utcnow(),
             sentiment_score=score, symbols=symbols, dedup_hash=dedup, critical=critical)


def test_market_wide_critical_news_about_another_venue_does_not_block_every_symbol(db_engine, settings):
    """A hack of some other exchange names none of our candidates, so it's
    tagged MARKET. That used to hard-block BUYs on every symbol for 48h (105
    of 105 evaluations on the first live paper run). It should now only
    weigh on the score."""
    engine = NewsEngine(settings, known_bases={"SOL"}, sources=[])
    with session_scope() as session:
        _add_news(NewsRepository(session), title="Bitget CEO says $388M hack exploited third-party vulnerability",
                  symbols=["MARKET"], critical=True, score=-100, dedup="m1")

    assessment = asyncio.run(engine.get_symbol_news_score("SOLUSDT"))

    assert not assessment.critical
    assert assessment.score < 0


def test_market_wide_critical_news_about_binance_still_blocks(db_engine, settings):
    engine = NewsEngine(settings, known_bases={"SOL"}, sources=[])
    with session_scope() as session:
        _add_news(NewsRepository(session), title="Binance suspends trading after security breach",
                  symbols=["MARKET"], critical=True, score=-90, dedup="b1")

    assert asyncio.run(engine.get_symbol_news_score("SOLUSDT")).critical


def test_critical_news_about_another_symbol_does_not_block(db_engine, settings):
    engine = NewsEngine(settings, known_bases={"SOL", "ETH"}, sources=[])
    with session_scope() as session:
        _add_news(NewsRepository(session), title="ETH bridge exploited", symbols=["ETH"], critical=True,
                  score=-95, dedup="e1")

    assert not asyncio.run(engine.get_symbol_news_score("SOLUSDT")).critical


def test_get_symbol_news_score_no_news_is_neutral(db_engine, settings):
    engine = NewsEngine(settings, known_bases={"SOL"}, sources=[])
    assessment = asyncio.run(engine.get_symbol_news_score("SOLUSDT"))
    assert assessment.score == 0
    assert not assessment.critical
