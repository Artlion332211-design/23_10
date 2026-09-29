"""Deterministic, keyword-based news sentiment scoring.

News is a *risk filter*, never a BUY trigger (project rule): the scorer only
needs to reliably catch clearly bad or clearly good news - especially the
"drop everything" categories (hack, exploit, delisting, bankruptcy, critical
regulatory action) - it doesn't need NLP-grade nuance to do that job, and
staying rule-based keeps it deterministic, auditable, and unit-testable
without an ML dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Each critical keyword forces the score to <= CRITICAL_SCORE_CEILING
# regardless of anything else in the text - these are the "drop everything,
# block the trade" categories called out explicitly in the spec.
CRITICAL_KEYWORDS: dict[str, int] = {
    "hack": -100, "hacked": -100, "hacker": -90, "exploit": -95, "exploited": -95,
    "rug pull": -100, "rugpull": -100, "delist": -90, "delisting": -90, "delisted": -90,
    "bankrupt": -95, "bankruptcy": -95, "insolvent": -90, "insolvency": -90,
    "halted trading": -80, "trading halt": -80, "trading suspended": -85, "suspends trading": -85,
    "security breach": -90, "wallet drained": -95, "funds stolen": -95, "stolen funds": -95,
    "network exploit": -95, "liquidity crisis": -80,
}

NEGATIVE_KEYWORDS: dict[str, int] = {
    "lawsuit": -40, "sued": -35, "sec charges": -60, "sec charge": -60, "sec lawsuit": -60,
    "investigation": -35, "probe": -30, "regulatory action": -45, "fine": -25, "fined": -25,
    "outage": -30, "network outage": -35, "downtime": -20, "bug": -25, "vulnerability": -40,
    "token unlock": -20, "unlock": -10, "sell-off": -25, "selloff": -25, "dump": -20,
    "ban": -50, "banned": -50, "restrict": -25, "restricted": -25, "warning": -20,
    "developer leaves": -20, "resigns": -15, "layoffs": -20,
}

POSITIVE_KEYWORDS: dict[str, int] = {
    "etf approval": 70, "etf approved": 80, "spot etf": 50, "partnership": 30,
    "integrat": 20, "listing": 25, "listed on": 25, "adoption": 25, "upgrade": 20,
    "mainnet launch": 30, "collaborat": 20, "institutional investment": 35,
    "acquisition": 20, "funding round": 20, "record high": 15, "all-time high": 20,
}

CRITICAL_SCORE_CEILING = -80

# Keywords are matched as whole words (plus common inflections), never as
# raw substrings - otherwise "hackathon" reads as a critical "hack",
# "banks" as "ban" and "refine" as "fine". These few are deliberate stems
# ("integration", "collaborates", ...) and only need a word start.
_STEM_KEYWORDS = frozenset({"integrat", "collaborat"})
_INFLECTIONS = r"(?:s|es|d|ed|ing|er|ers)?"


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    if keyword in _STEM_KEYWORDS:
        return re.compile(r"\b" + re.escape(keyword))
    return re.compile(r"\b" + re.escape(keyword) + _INFLECTIONS + r"\b")


def _compile(keywords: dict[str, int]) -> list[tuple[str, int, re.Pattern[str]]]:
    return [(keyword, weight, _keyword_pattern(keyword)) for keyword, weight in keywords.items()]


_CRITICAL_PATTERNS = _compile(CRITICAL_KEYWORDS)
_NEGATIVE_PATTERNS = _compile(NEGATIVE_KEYWORDS)
_POSITIVE_PATTERNS = _compile(POSITIVE_KEYWORDS)

_ALIASES: dict[str, str] = {
    "bitcoin": "BTC", "ethereum": "ETH", "binance coin": "BNB", "solana": "SOL",
    "ripple": "XRP", "cardano": "ADA", "chainlink": "LINK", "avalanche": "AVAX",
    "dogecoin": "DOGE", "polkadot": "DOT", "litecoin": "LTC", "polygon": "MATIC",
    "shiba inu": "SHIB", "tron": "TRX", "cosmos": "ATOM", "uniswap": "UNI",
}
_TICKER_PATTERN = re.compile(r"\b[A-Z]{2,10}\b")
# Checked longest-first so e.g. "SOLUSDT" strips "USDT" (not a shorter,
# coincidentally-matching suffix) to recover the base asset "SOL".
_QUOTE_SUFFIXES = ("USDT", "BUSD", "USDC", "USD", "BTC", "EUR", "TRY")


@dataclass(frozen=True)
class SentimentResult:
    score: int  # -100..100
    critical: bool
    matched_keywords: list[str] = field(default_factory=list)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def score_text(title: str, body: str = "") -> SentimentResult:
    text = _normalize(f"{title} {body}")
    matched: list[str] = []
    score = 0
    critical = False

    for keyword, weight, pattern in _CRITICAL_PATTERNS:
        if pattern.search(text):
            matched.append(keyword)
            score = min(score, weight)
            critical = True

    if not critical:
        for keyword, weight, pattern in _NEGATIVE_PATTERNS + _POSITIVE_PATTERNS:
            if pattern.search(text):
                matched.append(keyword)
                score += weight

    score = max(-100, min(100, score))
    if critical:
        score = min(score, CRITICAL_SCORE_CEILING)
    return SentimentResult(score=score, critical=critical, matched_keywords=matched)


def extract_symbols(title: str, body: str, known_bases: set[str]) -> list[str]:
    """Best-effort entity extraction: matches known Binance base-asset
    tickers directly, a full trading-pair symbol (e.g. "SOLUSDT" ->
    base "SOL"), and a small alias table for common full names. News that
    matches nothing is tagged "MARKET" (treated as a market-wide overlay)
    rather than dropped."""
    combined = f"{title} {body}"
    text_lower = _normalize(combined)
    found: set[str] = set()

    for alias, ticker in _ALIASES.items():
        if alias in text_lower and ticker in known_bases:
            found.add(ticker)

    for match in _TICKER_PATTERN.findall(combined):
        if match in known_bases:
            found.add(match)
            continue
        for suffix in _QUOTE_SUFFIXES:
            if match.endswith(suffix) and len(match) > len(suffix):
                base = match[: -len(suffix)]
                if base in known_bases:
                    found.add(base)
                break

    return sorted(found) if found else ["MARKET"]
