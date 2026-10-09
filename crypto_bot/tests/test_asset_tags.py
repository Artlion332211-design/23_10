from __future__ import annotations

import asyncio

from market.asset_tags import AssetTags


def _products(*tagged, filler=250):
    """A product list shaped like Binance's answer: `tagged` (symbol, tags,
    quote) entries plus untagged filler pairs."""
    items = [{"s": symbol, "q": quote, "tags": list(tags)} for symbol, tags, quote in tagged]
    items += [{"s": f"COIN{i}USDT", "q": "USDT", "tags": ["defi"]} for i in range(filler)]
    return {"code": "000000", "data": items, "success": True}


LIVE_LIKE = _products(
    ("MOVEUSDT", ["Monitoring", "Seed", "Launchpool"], "USDT"),
    ("NILUSDT", ["Seed"], "USDT"),
    ("MOVEBTC", ["Monitoring"], "BTC"),
)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _tags(answers, *, excluded=("Monitoring",), clock=None):
    """AssetTags whose fetches return (or raise) `answers` in order."""
    queue = list(answers)
    calls = []

    async def fetch():
        calls.append(1)
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    tags = AssetTags(frozenset(excluded), quote_asset="USDT", fetch=fetch, clock=clock or FakeClock())
    return tags, calls


def test_only_usdt_pairs_with_an_excluded_tag_are_kept_out():
    tags, _ = _tags([LIVE_LIKE])
    asyncio.run(tags.refresh_if_due())

    assert tags.excluded_tags("MOVEUSDT") == ("Monitoring",)
    assert "Monitoring" in (tags.exclusion_reason("MOVEUSDT") or "")
    assert tags.excluded_tags("NILUSDT") == ()  # Seed is not on the default list
    assert tags.exclusion_reason("NILUSDT") is None
    assert tags.excluded_tags("MOVEBTC") == ()  # the bot trades USDT pairs only


def test_tag_names_match_whatever_their_case():
    tags, _ = _tags([LIVE_LIKE], excluded=("monitoring", "SEED"))
    asyncio.run(tags.refresh_if_due())

    assert tags.excluded_tags("MOVEUSDT") == ("Monitoring", "Seed")
    assert tags.excluded_tags("NILUSDT") == ("Seed",)


def test_nothing_is_excluded_until_the_first_list_arrives():
    """Fails open: an unreachable web page must not stop trading."""
    tags, _ = _tags([RuntimeError("HTTP 403")])
    asyncio.run(tags.refresh_if_due())

    assert tags.excluded_tags("MOVEUSDT") == ()


def test_a_failed_refresh_keeps_the_last_good_list():
    clock = FakeClock()
    tags, _ = _tags([LIVE_LIKE, RuntimeError("timeout")], clock=clock)
    asyncio.run(tags.refresh_if_due())
    clock.now += 3601
    asyncio.run(tags.refresh_if_due())

    assert tags.excluded_tags("MOVEUSDT") == ("Monitoring",)


def test_a_cut_off_or_malformed_answer_does_not_empty_the_list():
    clock = FakeClock()
    tags, _ = _tags([LIVE_LIKE, _products(filler=3), {"data": None}, "<html>"], clock=clock)
    asyncio.run(tags.refresh_if_due())
    for _ in range(3):
        clock.now += 3601
        asyncio.run(tags.refresh_if_due())

    assert tags.excluded_tags("MOVEUSDT") == ("Monitoring",)


def test_list_is_fetched_hourly_and_retried_on_the_next_scan_after_a_failure():
    clock = FakeClock()
    tags, calls = _tags([LIVE_LIKE, RuntimeError("timeout"), LIVE_LIKE], clock=clock)

    asyncio.run(tags.refresh_if_due())
    clock.now += 15 * 60
    asyncio.run(tags.refresh_if_due())  # 15 min later: still fresh, no fetch
    assert len(calls) == 1

    clock.now += 3600
    asyncio.run(tags.refresh_if_due())  # due: fails
    clock.now += 15 * 60
    asyncio.run(tags.refresh_if_due())  # retried at the next scan
    assert len(calls) == 3


def test_status_line_when_the_list_is_missing_for_an_hour_or_older_than_a_day():
    clock = FakeClock()
    tags, _ = _tags([RuntimeError("timeout"), LIVE_LIKE], clock=clock)
    asyncio.run(tags.refresh_if_due())
    assert tags.problem() is None  # just started: give it time

    clock.now += 3601
    assert "не завантажуються" in (tags.problem() or "")

    asyncio.run(tags.refresh_if_due())
    assert tags.problem() is None

    clock.now += 25 * 3600
    assert "25 год" in (tags.problem() or "")
