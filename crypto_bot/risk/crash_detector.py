"""Interprets `MarketRegimeEngine`'s assessment into risk *policy*: whether
new BUYs and DCA get paused.

The crash-detection math itself lives in `market.market_regime` (it's a
market-data classification, not a risk decision) - this module only decides
what the bot does about it, per the regime-policy table in the spec:
STRONG_BEAR -> no new positions; CRASH -> no BUY and no DCA.
"""

from __future__ import annotations

from dataclasses import dataclass

from config.settings import Settings
from market.market_regime import RegimeAssessment, RegimeLevel


@dataclass(frozen=True)
class CrashPolicy:
    buys_paused: bool
    dca_paused: bool
    reason: str | None


def apply_crash_policy(assessment: RegimeAssessment, settings: Settings) -> CrashPolicy:
    if not settings.market_crash_pause:
        return CrashPolicy(buys_paused=False, dca_paused=False, reason=None)

    if assessment.level == RegimeLevel.CRASH:
        return CrashPolicy(buys_paused=True, dca_paused=True, reason="BTC market regime is CRASH")
    if assessment.level == RegimeLevel.STRONG_BEAR:
        return CrashPolicy(
            buys_paused=True, dca_paused=False, reason="BTC market regime is STRONG_BEAR - no new positions"
        )
    return CrashPolicy(buys_paused=False, dca_paused=False, reason=None)
