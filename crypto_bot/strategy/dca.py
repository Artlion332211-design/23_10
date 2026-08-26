"""Controlled DCA (averaging into an open position) - explicitly NOT
martingale. A price drop alone is never sufficient: every level requires a
fresh technical re-analysis to confirm the original thesis still holds
(project rule: "-3% -> повторний аналіз -> якщо thesis залишається valid ->
DCA", never "-3% -> BUY незалежно від ситуації").
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from config.settings import Settings
from strategy.scoring import ScoreBreakdown


@dataclass(frozen=True)
class DCALevel:
    level_index: int  # 1-based
    drop_percent: Decimal  # negative, e.g. -3
    size_usdt: Decimal


def dca_plan(settings: Settings) -> list[DCALevel]:
    return [
        DCALevel(level_index=i + 1, drop_percent=drop, size_usdt=size)
        for i, (drop, size) in enumerate(settings.dca_plan)
    ]


def next_dca_level(
    *, current_price: Decimal, avg_entry_price: Decimal, dca_count_done: int, settings: Settings
) -> DCALevel | None:
    """Which DCA level (if any) the current price has reached, given how
    many have already executed. `MAX_DCA_COUNT` is enforced simply by the
    plan's length. Returns None if capped out or price hasn't dropped far
    enough yet for the next level."""
    plan = dca_plan(settings)
    if dca_count_done >= len(plan) or avg_entry_price <= 0:
        return None
    change_percent = (current_price - avg_entry_price) / avg_entry_price * 100
    next_level = plan[dca_count_done]
    if change_percent <= next_level.drop_percent:
        return next_level
    return None


@dataclass(frozen=True)
class DCADecision:
    allowed: bool
    level: DCALevel | None
    reasons: list[str]


def evaluate_dca(
    *,
    current_price: Decimal,
    avg_entry_price: Decimal,
    dca_count_done: int,
    current_position_cost_usdt: Decimal,
    settings: Settings,
    score_breakdown: ScoreBreakdown,
    market_crash: bool,
    news_blocks_trading: bool,
    liquidity_ok: bool,
) -> DCADecision:
    """A price drop only *offers* a DCA level; it's only *allowed* if the
    thesis still holds. Every one of these must pass, per the project's
    explicit DCA gating rules (no crash, no bad news, still liquid,
    structure hasn't broken, score still above MIN_DCA_SCORE)."""
    level = next_dca_level(
        current_price=current_price, avg_entry_price=avg_entry_price,
        dca_count_done=dca_count_done, settings=settings,
    )
    if level is None:
        return DCADecision(allowed=False, level=None, reasons=["no DCA level reached, or MAX_DCA_COUNT already hit"])

    reasons: list[str] = []
    if market_crash:
        reasons.append("market in CRASH regime - DCA paused")
    if news_blocks_trading:
        reasons.append("negative/critical news on this symbol - DCA blocked")
    if not liquidity_ok:
        reasons.append("liquidity degraded since entry - DCA blocked")
    if score_breakdown.blocked:
        reasons.append("hard veto still active: " + "; ".join(score_breakdown.vetoes))
    if score_breakdown.final_score < settings.min_dca_score:
        reasons.append(f"re-analysis score {score_breakdown.final_score:.0f} below MIN_DCA_SCORE {settings.min_dca_score}")

    projected_cost = current_position_cost_usdt + level.size_usdt
    if projected_cost > settings.max_position_usdt:
        reasons.append(f"would exceed MAX_POSITION_USDT ({projected_cost} > {settings.max_position_usdt})")

    return DCADecision(
        allowed=not reasons, level=level, reasons=reasons or ["re-analysis confirms thesis still valid"]
    )
