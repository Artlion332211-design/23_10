"""Signal score aggregation and explainability formatting.

Central invariant enforced here, not scattered across callers: a BUY is
"confirmed enough" only when both hold:

* the weighted point total reaches `MIN_BUY_SCORE`, AND
* at least `MIN_CONFIRMED_SIGNALS` individual signals fired, spanning at
  least `MIN_CONFIRMATION_CATEGORIES` of the 5 analytical categories
  (trend / momentum / volume / volatility / structure).

This is what stops "5 different EMA periods" from ever counting as "5
independent confirmations": each signal declares exactly one category in
`config.yaml`'s `signal_weights`, and category diversity is checked
separately from the raw confirmed-signal count.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config.settings import SignalWeight


@dataclass(frozen=True)
class SignalResult:
    name: str
    confirmed: bool
    category: str
    points: float  # points actually awarded (0 if not confirmed)
    max_points: float
    detail: str = ""


@dataclass(frozen=True)
class ScoreBreakdown:
    symbol: str
    technical_score: float  # sum of confirmed signal points, before news
    news_adjustment: float  # can be a large negative or a small positive
    regime_adjustment: float  # informational: required-score delta from market regime policy
    final_score: float  # technical_score + news_adjustment, floored at 0
    signals: list[SignalResult]
    confirmed_count: int
    confirmed_categories: list[str]
    vetoes: list[str] = field(default_factory=list)  # hard blocks, independent of score
    meets_confirmation_rule: bool = False

    @property
    def blocked(self) -> bool:
        return bool(self.vetoes)

    def top_reasons(self, n: int = 5) -> list[str]:
        confirmed = sorted((s for s in self.signals if s.confirmed), key=lambda s: s.points, reverse=True)
        return [f"{s.name} ({s.detail})" if s.detail else s.name for s in confirmed[:n]]

    def explain(self) -> str:
        lines = []
        for s in sorted(self.signals, key=lambda s: (-s.confirmed, -s.max_points)):
            mark = "+" if s.confirmed else " "
            detail = f"  {s.detail}" if s.detail else ""
            lines.append(f"{s.name:<24}{mark}{s.points:>5.1f}  [{s.category}]{detail}")
        lines.append("-" * 40)
        lines.append(f"{'TECHNICAL':<24} {self.technical_score:>6.1f}")
        if self.news_adjustment:
            lines.append(f"{'NEWS':<24} {self.news_adjustment:>+6.1f}")
        lines.append(f"{'TOTAL':<24} {self.final_score:>6.1f}")
        if self.vetoes:
            lines.append("VETO: " + "; ".join(self.vetoes))
        return "\n".join(lines)


def score_signals(
    symbol: str,
    raw_signals: dict[str, bool],
    weights: dict[str, SignalWeight],
    *,
    min_confirmed_signals: int,
    min_confirmation_categories: int,
    news_adjustment: float = 0.0,
    regime_adjustment: float = 0.0,
    vetoes: list[str] | None = None,
    details: dict[str, str] | None = None,
) -> ScoreBreakdown:
    details = details or {}
    signals: list[SignalResult] = []
    technical_score = 0.0
    confirmed_categories: set[str] = set()
    confirmed_count = 0

    for name, weight in weights.items():
        confirmed = bool(raw_signals.get(name, False))
        points = weight.points if confirmed else 0.0
        signals.append(
            SignalResult(
                name=name, confirmed=confirmed, category=weight.category,
                points=points, max_points=weight.points, detail=details.get(name, ""),
            )
        )
        if confirmed:
            technical_score += points
            confirmed_categories.add(weight.category)
            confirmed_count += 1

    final_score = max(0.0, technical_score + news_adjustment)
    meets_rule = confirmed_count >= min_confirmed_signals and len(confirmed_categories) >= min_confirmation_categories

    return ScoreBreakdown(
        symbol=symbol, technical_score=technical_score, news_adjustment=news_adjustment,
        regime_adjustment=regime_adjustment, final_score=final_score, signals=signals,
        confirmed_count=confirmed_count, confirmed_categories=sorted(confirmed_categories),
        vetoes=list(vetoes or []), meets_confirmation_rule=meets_rule,
    )
