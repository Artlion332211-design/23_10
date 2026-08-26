"""Event-driven backtest engine.

Reuses the exact same decision logic as live/paper trading - `SignalEngine`,
`strategy.dca`, `strategy.take_profit`, `strategy.filters`,
`market.market_regime`, `risk.correlation` - so a backtest result reflects
the real strategy, not a separate approximation of it. The one deliberate
difference is risk bookkeeping: the live `RiskManager` is DB-backed (it
exists to survive a restart and answer to Telegram commands, neither of
which apply to a deterministic historical replay), so this module tracks
the same limits - open positions, exposure, daily capital, consecutive
losses, crash pause - with a lightweight in-memory `_BacktestPortfolio`
instead. One behavioral difference from live is called out where it
happens below (auto-clearing the consecutive-loss pause on a win, since a
backtest has no operator to send `/resume`).

No look-ahead by construction: every timeframe's indicators are computed
once, vectorized, over the whole causal series (`market.indicators`), and
multi-timeframe alignment uses `pd.merge_asof(..., direction="backward")`
so a 15m decision point only ever sees the most recently *closed* 1h/4h bar.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

import pandas as pd

from backtest.metrics import BacktestMetrics, EquityPoint, TradeRecord, compute_metrics
from config.settings import RulesConfig, Settings
from market.indicators import compute_all_indicators
from market.market_data import IndicatorSnapshot
from market.market_regime import MarketRegimeEngine, RegimeAssessment, RegimeLevel
from risk.correlation import check_correlation_limit
from strategy.dca import evaluate_dca, next_dca_level
from strategy.filters import AntiFOMOFilter, check_blacklist
from strategy.scoring import ScoreBreakdown
from strategy.signal_engine import MultiTimeframeSnapshot, SignalEngine
from strategy.take_profit import compute_target_price, should_exit_trailing
from utils.time import Timeframe

logger = logging.getLogger(__name__)

_PREV_COLUMNS = ("rsi", "macd_hist", "obv")


class NewsProviderLike(Protocol):
    def news_adjustment_for(self, symbol: str, timestamp: pd.Timestamp) -> tuple[float, bool, list[str]]: ...


class _NeutralNews:
    """Default: no news data in the backtest. News is a risk filter in live
    trading, never a source of edge, so backtesting the technical strategy
    without it is a legitimate (if slightly optimistic) baseline; a
    `NewsProviderLike` backed by historical headlines can be plugged in."""

    def news_adjustment_for(self, symbol: str, timestamp: pd.Timestamp) -> tuple[float, bool, list[str]]:
        return 0.0, False, []


def _resample_ohlcv(df_15m: pd.DataFrame, rule: str) -> pd.DataFrame:
    indexed = df_15m.set_index("open_time")
    resampled = indexed.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    resampled = resampled.reset_index()
    resampled["close_time"] = resampled["open_time"] + pd.tseries.frequencies.to_offset(rule)
    return resampled


def _with_prev_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in _PREV_COLUMNS:
        df[f"{col}_prev"] = df[col].shift(1)
    return df


def prepare_symbol_frames(df_15m: pd.DataFrame, rules: RulesConfig) -> dict[Timeframe, pd.DataFrame]:
    """Compute every indicator column, once, for all three timeframes.
    Purely causal (see `market.indicators`), so this is identical whether
    called once up-front (backtest) or incrementally (live)."""
    m15 = _with_prev_columns(compute_all_indicators(df_15m, rules.indicators))
    h1 = _with_prev_columns(compute_all_indicators(_resample_ohlcv(df_15m, Timeframe.H1.pandas_freq), rules.indicators))
    h4 = _with_prev_columns(compute_all_indicators(_resample_ohlcv(df_15m, Timeframe.H4.pandas_freq), rules.indicators))
    return {Timeframe.M15: m15, Timeframe.H1: h1, Timeframe.H4: h4}


def merge_aligned(frames: dict[Timeframe, pd.DataFrame]) -> pd.DataFrame:
    """One 15m-grained frame carrying every timeframe's indicators, each
    row showing only what would actually be known at that bar's close -
    the core no-lookahead guarantee for backtesting multi-timeframe logic."""
    m15 = frames[Timeframe.M15].copy()
    m15["decision_time"] = m15["open_time"] + pd.Timedelta(minutes=15)
    merged = m15.sort_values("decision_time")
    for timeframe, suffix in ((Timeframe.H1, "_h1"), (Timeframe.H4, "_h4")):
        other = frames[timeframe].add_suffix(suffix).sort_values(f"close_time{suffix}")
        merged = pd.merge_asof(
            merged, other, left_on="decision_time", right_on=f"close_time{suffix}", direction="backward"
        )
    return merged.set_index("decision_time").sort_index()


def _snapshot_from_row(row: pd.Series, suffix: str, symbol: str, timeframe: Timeframe) -> IndicatorSnapshot:
    def get(name: str, default: float = 0.0) -> Any:
        value = row.get(f"{name}{suffix}", default)
        return default if pd.isna(value) else value

    open_time = row.get(f"open_time{suffix}")
    return IndicatorSnapshot.from_getter(symbol, timeframe, get, open_time=open_time)


def _simulate_buy(price: Decimal, usdt_amount: Decimal, settings: Settings) -> tuple[Decimal, Decimal, Decimal]:
    """Returns (fill_price, net_quantity_received, commission_usdt). Mirrors
    the live accounting model: commission comes out of the base asset
    received (no BNB discount modeled), slippage pushes the buy fill price
    up. Both fee and slippage come straight from Settings, never hard-coded,
    so a backtest and its live counterpart always share one assumption."""
    fill_price = price * (Decimal(1) + settings.expected_slippage_percent / Decimal(100))
    gross_qty = usdt_amount / fill_price
    net_qty = gross_qty * (Decimal(1) - settings.taker_fee_rate)
    commission_usdt = gross_qty * settings.taker_fee_rate * fill_price
    return fill_price, net_qty, commission_usdt


def _simulate_sell(price: Decimal, quantity: Decimal, settings: Settings) -> tuple[Decimal, Decimal, Decimal]:
    """Returns (fill_price, net_proceeds_usdt, commission_usdt). Sell-side
    commission comes out of the USDT proceeds; slippage pushes the sell
    fill price down."""
    fill_price = price * (Decimal(1) - settings.expected_slippage_percent / Decimal(100))
    gross_proceeds = quantity * fill_price
    commission_usdt = gross_proceeds * settings.taker_fee_rate
    return fill_price, gross_proceeds - commission_usdt, commission_usdt


@dataclass
class _OpenPosition:
    symbol: str
    opened_at: Any
    avg_entry_price: Decimal
    total_quantity: Decimal
    total_cost_usdt: Decimal
    dca_count: int
    target_price: Decimal
    trailing_active: bool = False
    trailing_peak: Decimal | None = None
    partial_closed_qty: Decimal = Decimal("0")
    fees_paid_usdt: Decimal = Decimal("0")
    worst_price_seen: Decimal = Decimal("0")


class _BacktestPortfolio:
    """In-memory mirror of `risk.risk_manager.RiskManager`'s decision rules,
    without the DB/Telegram concerns that only make sense for a live bot."""

    def __init__(self, settings: Settings, starting_balance: Decimal) -> None:
        self.settings = settings
        self.cash = starting_balance
        self.starting_balance = starting_balance
        self.open_positions: dict[str, _OpenPosition] = {}
        self.consecutive_bad_trades = 0
        self.buy_paused = False
        self.daily_new_capital: dict[str, Decimal] = {}
        self.total_fees = Decimal("0")

    def equity(self, mark_prices: dict[str, Decimal]) -> Decimal:
        value = self.cash
        for symbol, pos in self.open_positions.items():
            value += pos.total_quantity * mark_prices.get(symbol, pos.avg_entry_price)
        return value

    def can_open(
        self, requested_usdt: Decimal, mark_prices: dict[str, Decimal], regime_level: RegimeLevel, day: str
    ) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if self.buy_paused:
            reasons.append("consecutive-losses pause active")
        if regime_level == RegimeLevel.CRASH:
            reasons.append("BTC regime is CRASH")
        elif regime_level == RegimeLevel.STRONG_BEAR:
            reasons.append("BTC regime is STRONG_BEAR")
        if len(self.open_positions) >= self.settings.max_open_positions:
            reasons.append("MAX_OPEN_POSITIONS reached")
        if requested_usdt > self.settings.max_position_usdt:
            reasons.append("exceeds MAX_POSITION_USDT")
        equity = self.equity(mark_prices)
        total_open_cost = sum((p.total_cost_usdt for p in self.open_positions.values()), Decimal("0"))
        if equity > 0 and (total_open_cost + requested_usdt) / equity * 100 > self.settings.max_total_exposure_percent:
            reasons.append("exceeds MAX_TOTAL_EXPOSURE_PERCENT")
        deployed_today = self.daily_new_capital.get(day, Decimal("0"))
        if deployed_today + requested_usdt > self.settings.max_daily_new_capital_usdt:
            reasons.append("exceeds MAX_DAILY_NEW_CAPITAL_USDT")
        if self.cash < requested_usdt:
            reasons.append("insufficient simulated cash")
        return not reasons, reasons

    def can_dca(self, regime_level: RegimeLevel) -> tuple[bool, list[str]]:
        if regime_level == RegimeLevel.CRASH:
            return False, ["BTC regime is CRASH - DCA paused"]
        return True, []

    def register_deployed(self, day: str, amount: Decimal) -> None:
        self.daily_new_capital[day] = self.daily_new_capital.get(day, Decimal("0")) + amount

    def register_trade_result(self, is_win: bool) -> None:
        if is_win:
            self.consecutive_bad_trades = 0
            # Diverges from live RiskManager (which needs an explicit
            # /resume): a backtest has no operator, so a win clearing the
            # auto-pause is what makes the circuit breaker mean anything
            # over a multi-month replay instead of ending the run early.
            self.buy_paused = False
        else:
            self.consecutive_bad_trades += 1
            if self.consecutive_bad_trades >= self.settings.max_consecutive_bad_trades:
                self.buy_paused = True


@dataclass
class BacktestResult:
    trades: list[TradeRecord]
    equity_curve: list[EquityPoint]
    metrics: BacktestMetrics
    no_trade_log: list[dict[str, Any]] = field(default_factory=list)


class BacktestEngine:
    def __init__(self, settings: Settings, rules: RulesConfig, news_provider: NewsProviderLike | None = None) -> None:
        self.settings = settings
        self.rules = rules
        self.signal_engine = SignalEngine(settings, rules)
        self.regime_engine = MarketRegimeEngine(rules.crash_detector)
        self.anti_fomo = AntiFOMOFilter(rules.anti_fomo)
        self.news_provider = news_provider or _NeutralNews()

    def _required_min_score(self, level: RegimeLevel) -> float:
        policy = self.rules.regime_policy.get(level.value)
        return float(self.settings.min_buy_score) + (policy.min_score_delta if policy else 0.0)

    def _regime_allows_buy(self, level: RegimeLevel) -> bool:
        policy = self.rules.regime_policy.get(level.value)
        return policy.allow_buy if policy else True

    def run(
        self,
        symbol_klines_15m: dict[str, pd.DataFrame],
        btc_klines_15m: pd.DataFrame,
        *,
        starting_balance: Decimal = Decimal("10000"),
    ) -> BacktestResult:
        btc_frames = prepare_symbol_frames(btc_klines_15m, self.rules)
        btc_merged = merge_aligned(btc_frames)

        symbol_frames = {s: prepare_symbol_frames(df, self.rules) for s, df in symbol_klines_15m.items()}
        symbol_merged = {s: merge_aligned(frames) for s, frames in symbol_frames.items()}

        common_index = btc_merged.index
        for df in symbol_merged.values():
            common_index = common_index.intersection(df.index)
        common_index = common_index.sort_values()
        if len(common_index) == 0:
            raise ValueError("No overlapping timestamps between BTC and symbol data")

        warmup = (
            max(
                self.rules.indicators.ema.slow,
                self.rules.indicators.bollinger.squeeze_lookback,
                self.rules.indicators.swing_structure.lookback,
            )
            + 5
        )

        portfolio = _BacktestPortfolio(self.settings, starting_balance)
        trades: list[TradeRecord] = []
        equity_curve: list[EquityPoint] = []
        no_trade_log: list[dict[str, Any]] = []

        for i, ts in enumerate(common_index):
            if i < warmup:
                continue

            btc_row = btc_merged.loc[ts]
            if pd.isna(btc_row.get("rsi_h1")) or pd.isna(btc_row.get("rsi_h4")):
                continue

            btc_snapshots = {
                Timeframe.M15: _snapshot_from_row(btc_row, "", "BTCUSDT", Timeframe.M15),
                Timeframe.H1: _snapshot_from_row(btc_row, "_h1", "BTCUSDT", Timeframe.H1),
                Timeframe.H4: _snapshot_from_row(btc_row, "_h4", "BTCUSDT", Timeframe.H4),
            }
            if self.settings.btc_market_filter:
                recent_btc_15m = btc_frames[Timeframe.M15]
                recent_btc_15m = recent_btc_15m[recent_btc_15m["open_time"] <= ts].tail(60)
                regime = self.regime_engine.evaluate(btc_snapshots, recent_btc_15m)
            else:
                regime = RegimeAssessment(level=RegimeLevel.NEUTRAL, score=0.0, reasons=[], crash=False)

            mark_prices = {s: Decimal(str(symbol_merged[s].loc[ts, "close"])) for s in symbol_merged}
            equity_curve.append(EquityPoint(timestamp=ts.to_pydatetime(), equity_usdt=portfolio.equity(mark_prices)))
            day_str = ts.date().isoformat()

            for symbol in list(portfolio.open_positions.keys()):
                row = symbol_merged[symbol].loc[ts]
                if pd.isna(row.get("rsi_h1")):
                    continue
                self._manage_position(portfolio, symbol, row, ts, mark_prices[symbol], regime, trades, no_trade_log)

            if len(portfolio.open_positions) < self.settings.max_open_positions:
                for symbol, df in symbol_merged.items():
                    if symbol in portfolio.open_positions:
                        continue
                    row = df.loc[ts]
                    if pd.isna(row.get("rsi_h1")) or pd.isna(row.get("rsi_h4")):
                        continue
                    self._try_open(portfolio, symbol, row, ts, regime, mark_prices, day_str, symbol_merged, no_trade_log)

        if not equity_curve:
            raise ValueError("Backtest produced no equity points - not enough history past the indicator warmup period")

        metrics = compute_metrics(trades, equity_curve, starting_balance, portfolio.total_fees)
        return BacktestResult(trades=trades, equity_curve=equity_curve, metrics=metrics, no_trade_log=no_trade_log)

    def _build_mtf(self, row: pd.Series, symbol: str) -> MultiTimeframeSnapshot:
        return MultiTimeframeSnapshot(
            m15=_snapshot_from_row(row, "", symbol, Timeframe.M15),
            h1=_snapshot_from_row(row, "_h1", symbol, Timeframe.H1),
            h4=_snapshot_from_row(row, "_h4", symbol, Timeframe.H4),
        )

    def _try_open(
        self,
        portfolio: _BacktestPortfolio,
        symbol: str,
        row: pd.Series,
        ts: pd.Timestamp,
        regime: RegimeAssessment,
        mark_prices: dict[str, Decimal],
        day_str: str,
        symbol_merged: dict[str, pd.DataFrame],
        no_trade_log: list[dict[str, Any]],
    ) -> None:
        mtf = self._build_mtf(row, symbol)
        h1, h4 = mtf.h1, mtf.h4

        news_adj, news_critical, news_headlines = self.news_provider.news_adjustment_for(symbol, ts)
        extra_vetoes: list[str] = []
        if news_critical:
            extra_vetoes.append("critical news block: " + "; ".join(news_headlines[:2]))
        if not self._regime_allows_buy(regime.level):
            extra_vetoes.append(f"BTC regime {regime.level.value} blocks new positions")

        change_1h = (h1.close - h1.open) / h1.open * 100 if h1.open else 0.0
        change_4h = (h4.close - h4.open) / h4.open * 100 if h4.open else 0.0
        fomo = self.anti_fomo.check(h1, change_1h_percent=change_1h, change_4h_percent=change_4h)
        if not fomo.passed:
            extra_vetoes.append(fomo.reason or "AntiFOMO block")

        blacklist = check_blacklist(symbol, self.rules.universe)
        if not blacklist.passed:
            extra_vetoes.append(blacklist.reason or "blacklisted")

        if portfolio.open_positions:
            candidate_closes = symbol_merged[symbol].loc[:ts, "close"]
            closes_by_symbol = {s: symbol_merged[s].loc[:ts, "close"] for s in portfolio.open_positions}
            corr_check = check_correlation_limit(
                symbol, candidate_closes, list(portfolio.open_positions.keys()), closes_by_symbol, self.rules.correlation
            )
            if not corr_check.passed:
                extra_vetoes.append(corr_check.reason or "correlation limit reached")

        regime_policy = self.rules.regime_policy.get(regime.level.value)
        breakdown = self.signal_engine.evaluate(
            symbol, mtf, news_adjustment=news_adj,
            regime_adjustment=regime_policy.min_score_delta if regime_policy else 0.0,
            extra_vetoes=extra_vetoes,
        )
        required_score = self._required_min_score(regime.level)

        if breakdown.blocked or breakdown.final_score < required_score or not breakdown.meets_confirmation_rule:
            no_trade_log.append(self._log_entry(ts, symbol, "NO_TRADE", breakdown, required_score))
            return

        can_open, risk_reasons = portfolio.can_open(self.settings.initial_order_usdt, mark_prices, regime.level, day_str)
        if not can_open:
            no_trade_log.append(self._log_entry(ts, symbol, "BLOCKED", breakdown, required_score, risk_reasons))
            return

        fill_price, net_qty, commission = _simulate_buy(mark_prices[symbol], self.settings.initial_order_usdt, self.settings)
        target = compute_target_price(
            fill_price, target_profit_percent=self.settings.target_profit_percent,
            taker_fee_rate=self.settings.taker_fee_rate, expected_slippage_percent=self.settings.expected_slippage_percent,
        )
        portfolio.open_positions[symbol] = _OpenPosition(
            symbol=symbol, opened_at=ts.to_pydatetime(), avg_entry_price=fill_price,
            total_quantity=net_qty, total_cost_usdt=fill_price * net_qty, dca_count=0,
            target_price=target, fees_paid_usdt=commission, worst_price_seen=fill_price,
        )
        portfolio.cash -= self.settings.initial_order_usdt
        portfolio.total_fees += commission
        portfolio.register_deployed(day_str, self.settings.initial_order_usdt)

    def _manage_position(
        self,
        portfolio: _BacktestPortfolio,
        symbol: str,
        row: pd.Series,
        ts: pd.Timestamp,
        current_price: Decimal,
        regime: RegimeAssessment,
        trades: list[TradeRecord],
        no_trade_log: list[dict[str, Any]],
    ) -> None:
        pos = portfolio.open_positions[symbol]
        pos.worst_price_seen = min(pos.worst_price_seen, current_price) if pos.worst_price_seen > 0 else current_price

        if pos.trailing_active:
            new_peak = max(pos.trailing_peak or current_price, current_price)
            pos.trailing_peak = new_peak
            if should_exit_trailing(current_price, new_peak, self.settings):
                remaining = pos.total_quantity - pos.partial_closed_qty
                self._close(portfolio, symbol, remaining, current_price, ts, "TRAILING_STOP", trades)
            return

        if current_price >= pos.target_price:
            if self.settings.use_trailing_after_tp:
                partial_qty = pos.total_quantity * self.settings.trailing_partial_close_fraction
                fill_price, proceeds, commission = _simulate_sell(current_price, partial_qty, self.settings)
                pos.partial_closed_qty += partial_qty
                pos.fees_paid_usdt += commission
                portfolio.cash += proceeds
                portfolio.total_fees += commission
                pos.trailing_active = True
                pos.trailing_peak = current_price
            else:
                self._close(portfolio, symbol, pos.total_quantity, current_price, ts, "TAKE_PROFIT", trades)
            return

        level = next_dca_level(
            current_price=current_price, avg_entry_price=pos.avg_entry_price,
            dca_count_done=pos.dca_count, settings=self.settings,
        )
        if level is None:
            return

        mtf = self._build_mtf(row, symbol)
        breakdown = self.signal_engine.evaluate(symbol, mtf)
        dca_risk_ok, dca_risk_reasons = portfolio.can_dca(regime.level)
        dca_decision = evaluate_dca(
            current_price=current_price, avg_entry_price=pos.avg_entry_price, dca_count_done=pos.dca_count,
            current_position_cost_usdt=pos.total_cost_usdt, settings=self.settings, score_breakdown=breakdown,
            market_crash=(regime.level == RegimeLevel.CRASH), news_blocks_trading=False, liquidity_ok=True,
        )
        if not (dca_risk_ok and dca_decision.allowed):
            no_trade_log.append(
                self._log_entry(ts, symbol, "NO_DCA", breakdown, self.settings.min_dca_score, dca_risk_reasons + dca_decision.reasons)
            )
            return

        fill_price, net_qty, commission = _simulate_buy(current_price, level.size_usdt, self.settings)
        new_total_qty = pos.total_quantity + net_qty
        new_total_cost = pos.total_cost_usdt + fill_price * net_qty
        pos.avg_entry_price = new_total_cost / new_total_qty
        pos.total_quantity = new_total_qty
        pos.total_cost_usdt = new_total_cost
        pos.dca_count += 1
        pos.fees_paid_usdt += commission
        pos.target_price = compute_target_price(
            pos.avg_entry_price, target_profit_percent=self.settings.target_profit_percent,
            taker_fee_rate=self.settings.taker_fee_rate, expected_slippage_percent=self.settings.expected_slippage_percent,
        )
        portfolio.cash -= level.size_usdt
        portfolio.total_fees += commission
        portfolio.register_deployed(ts.date().isoformat(), level.size_usdt)

    def _close(
        self,
        portfolio: _BacktestPortfolio,
        symbol: str,
        quantity: Decimal,
        current_price: Decimal,
        ts: pd.Timestamp,
        reason: str,
        trades: list[TradeRecord],
    ) -> None:
        pos = portfolio.open_positions.pop(symbol)
        fill_price, proceeds, commission = _simulate_sell(current_price, quantity, self.settings)
        portfolio.cash += proceeds
        portfolio.total_fees += commission

        cost_basis = pos.avg_entry_price * quantity
        net_pnl = proceeds - cost_basis
        net_pnl_pct = (proceeds / cost_basis - 1) * 100 if cost_basis > 0 else Decimal("0")
        worst_dd = float((pos.worst_price_seen - pos.avg_entry_price) / pos.avg_entry_price * 100) if pos.avg_entry_price > 0 else 0.0

        trades.append(
            TradeRecord(
                symbol=symbol, opened_at=pos.opened_at, closed_at=ts.to_pydatetime(),
                avg_entry_price=pos.avg_entry_price, exit_price=fill_price, quantity=quantity,
                cost_usdt=pos.total_cost_usdt, proceeds_usdt=proceeds, net_pnl_usdt=net_pnl,
                net_pnl_percent=net_pnl_pct, dca_count=pos.dca_count, close_reason=reason,
                worst_drawdown_percent=min(0.0, worst_dd),
            )
        )
        portfolio.register_trade_result(is_win=net_pnl > 0)

    @staticmethod
    def _log_entry(
        ts: pd.Timestamp, symbol: str, action: str, breakdown: ScoreBreakdown, required_score: float,
        extra_reasons: list[str] | None = None,
    ) -> dict[str, Any]:
        reasons = list(breakdown.vetoes) + (extra_reasons or [])
        if not reasons and breakdown.final_score < required_score:
            reasons = [f"score {breakdown.final_score:.0f} below required {required_score:.0f}"]
        return {
            "timestamp": ts, "symbol": symbol, "action": action, "score": breakdown.final_score,
            "required_score": required_score, "reasons": reasons,
        }
