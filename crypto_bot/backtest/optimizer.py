"""Walk-forward parameter search.

Per project rule: never tune parameters against the whole history - that's
how a backtest ends up lying to you. Split chronologically into
train/validation/test (default 60/20/20, from `config.yaml`'s `backtest`
section), search a small grid on train+validation by a chosen objective,
then run the single winning parameter set once, untouched, on the held-out
test segment - that number, not the training score, is what should inform
a decision.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from backtest.engine import BacktestEngine
from backtest.metrics import BacktestMetrics
from config.settings import RulesConfig, Settings


@dataclass(frozen=True)
class WalkForwardSplit:
    train: dict[str, pd.DataFrame]
    validation: dict[str, pd.DataFrame]
    test: dict[str, pd.DataFrame]
    btc_train: pd.DataFrame
    btc_validation: pd.DataFrame
    btc_test: pd.DataFrame


def _split_three(df: pd.DataFrame, train_fraction: float, validation_fraction: float) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    n = len(df)
    train_end = int(n * train_fraction)
    val_end = int(n * (train_fraction + validation_fraction))
    return df.iloc[:train_end].copy(), df.iloc[train_end:val_end].copy(), df.iloc[val_end:].copy()


def split_chronologically(
    symbol_klines: dict[str, pd.DataFrame], btc_klines: pd.DataFrame, rules: RulesConfig
) -> WalkForwardSplit:
    train_frac = rules.backtest.train_fraction
    val_frac = rules.backtest.validation_fraction

    btc_train, btc_val, btc_test = _split_three(btc_klines, train_frac, val_frac)
    train: dict[str, pd.DataFrame] = {}
    validation: dict[str, pd.DataFrame] = {}
    test: dict[str, pd.DataFrame] = {}
    for symbol, df in symbol_klines.items():
        train[symbol], validation[symbol], test[symbol] = _split_three(df, train_frac, val_frac)

    return WalkForwardSplit(
        train=train, validation=validation, test=test,
        btc_train=btc_train, btc_validation=btc_val, btc_test=btc_test,
    )


ObjectiveFn = Callable[[BacktestMetrics], float]


def default_objective(metrics: BacktestMetrics) -> float:
    """Reward return and risk-adjusted return, punish drawdown - deliberately
    simple and transparent so a search can't just chase the highest win
    rate (project rule: win rate alone is not a valid optimization target).
    Requires a minimum trade count so a lucky handful of trades can't win."""
    if metrics.num_trades < 5:
        return float("-inf")
    drawdown_penalty = abs(metrics.max_drawdown_percent) * 2.0
    return metrics.total_return_percent + metrics.sharpe_ratio * 10.0 - drawdown_penalty


@dataclass(frozen=True)
class OptimizationResult:
    best_params: dict[str, object]
    train_metrics: BacktestMetrics
    validation_metrics: BacktestMetrics
    test_metrics: BacktestMetrics
    all_candidates: list[tuple[dict[str, object], float]]


def _expand_grid(param_grid: dict[str, list[object]]) -> list[dict[str, object]]:
    combos: list[dict[str, object]] = [{}]
    for key, values in param_grid.items():
        combos = [{**combo, key: value} for combo in combos for value in values]
    return combos


def grid_search(
    settings: Settings,
    rules: RulesConfig,
    param_grid: dict[str, list[object]],
    split: WalkForwardSplit,
    *,
    starting_balance: Decimal = Decimal("10000"),
    objective: ObjectiveFn = default_objective,
) -> OptimizationResult:
    """Search `param_grid` (e.g. `{"min_buy_score": [70, 75, 80]}`) on
    train+validation, then confirm the single winner once on the untouched
    test segment.

    Raises `ValueError` if `param_grid` itself is empty, or if every
    combination fails the objective's data-sufficiency bar (e.g.
    `default_objective`'s "fewer than 5 trades" guard) - refusing to name a
    "winner" tuned on too little history is the point, not a bug, but the
    two situations get distinct, actionable messages.
    """
    combos = _expand_grid(param_grid)
    if not combos:
        raise ValueError("param_grid is empty - nothing to search")

    candidates: list[tuple[dict[str, object], float]] = []
    best_params: dict[str, object] | None = None
    best_score = float("-inf")
    best_train_metrics: BacktestMetrics | None = None
    best_val_metrics: BacktestMetrics | None = None

    for combo in combos:
        trial_settings = settings.model_copy(update=combo)

        train_result = BacktestEngine(trial_settings, rules).run(split.train, split.btc_train, starting_balance=starting_balance)
        val_result = BacktestEngine(trial_settings, rules).run(split.validation, split.btc_validation, starting_balance=starting_balance)

        score = objective(train_result.metrics) + objective(val_result.metrics)
        candidates.append((combo, score))
        if score > best_score:
            best_score = score
            best_params = combo
            best_train_metrics = train_result.metrics
            best_val_metrics = val_result.metrics

    if best_params is None or best_train_metrics is None or best_val_metrics is None:
        raise ValueError(
            f"None of the {len(combos)} parameter combination(s) produced enough trades to "
            "score on train+validation (every candidate hit the objective's data-sufficiency "
            "floor, e.g. default_objective's minimum of 5 trades per segment). Use a longer "
            "history, a smaller/looser param_grid, or pass a custom `objective` with a lower bar."
        )

    final_settings = settings.model_copy(update=best_params)
    test_result = BacktestEngine(final_settings, rules).run(split.test, split.btc_test, starting_balance=starting_balance)

    return OptimizationResult(
        best_params=best_params, train_metrics=best_train_metrics, validation_metrics=best_val_metrics,
        test_metrics=test_result.metrics, all_candidates=sorted(candidates, key=lambda c: c[1], reverse=True),
    )
