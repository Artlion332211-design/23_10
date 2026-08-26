"""StrategyEngine: the orchestrator that ties SignalEngine, RiskManager,
ExecutionEngine, DCA and TakeProfit together into actual trading decisions.

Call chain is always Strategy -> Risk -> Execution -> Binance: this module
calls `RiskManager` for permission and `ExecutionEngine` to place orders, but
never touches `exchange.binance_client` directly. It emits structured
events (`BuyExecutedEvent`, `DCAExecutedEvent`, `PositionClosedEvent`, ...)
through an injected `StrategyNotifier` - formatting those into the exact
Telegram wording lives in `telegram_bot/notifications.py`, not here, so this
module stays testable without any Telegram dependency.

News is consumed through the `NewsProvider` protocol (structural typing, no
import of `news.news_engine` needed) so this module can be exercised and
tested before/independently of the news layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from decimal import ROUND_DOWN, Decimal
from typing import Protocol

from config.settings import RulesConfig, Settings
from database.models import (
    OrderPurpose,
    PositionStatus,
    SignalDecision,
)
from database.repository import PositionRepository, SignalRepository
from database.session import session_scope
from exchange.execution_engine import ExecutionEngine
from market.market_data import MarketDataStore
from market.market_regime import RegimeAssessment, RegimeLevel
from market.orderbook import OrderBookSnapshot
from risk.correlation import check_correlation_limit
from risk.risk_manager import RiskManager
from strategy.dca import DCALevel, dca_plan, evaluate_dca, next_dca_level
from strategy.filters import AntiFOMOFilter, check_blacklist, check_liquidity_fresh
from strategy.scoring import ScoreBreakdown
from strategy.signal_engine import MultiTimeframeSnapshot, SignalEngine
from strategy.take_profit import compute_target_price, should_exit_trailing
from utils.time import Timeframe, utcnow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsAssessment:
    score: int  # -100..100
    critical: bool
    headlines: list[str]


class NewsProvider(Protocol):
    async def get_symbol_news_score(self, symbol: str) -> NewsAssessment: ...


@dataclass(frozen=True)
class TradeDecision:
    action: str  # "BUY" | "DCA" | "NO_TRADE" | "BLOCKED"
    symbol: str
    breakdown: ScoreBreakdown
    regime: RegimeAssessment
    required_score: float
    news_score: int
    reasons: list[str]


@dataclass(frozen=True)
class BuyExecutedEvent:
    symbol: str
    price: Decimal
    usdt_amount: Decimal
    quantity: Decimal
    breakdown: ScoreBreakdown
    regime: RegimeAssessment
    news_score: int
    target_price: Decimal
    dca_plan: list[DCALevel]
    position_id: int


@dataclass(frozen=True)
class DCAExecutedEvent:
    symbol: str
    level_index: int
    price: Decimal
    usdt_amount: Decimal
    new_avg_entry: Decimal
    new_target_price: Decimal
    position_id: int


@dataclass(frozen=True)
class PositionClosedEvent:
    symbol: str
    exit_price: Decimal
    avg_entry_price: Decimal
    net_pnl_usdt: Decimal
    net_pnl_percent: Decimal
    holding_time_seconds: float
    close_reason: str
    position_id: int


class StrategyNotifier(Protocol):
    async def on_buy_signal(self, decision: TradeDecision) -> None: ...
    async def on_no_trade(self, decision: TradeDecision) -> None: ...
    async def on_buy_executed(self, event: BuyExecutedEvent) -> None: ...
    async def on_dca_signal(self, decision: TradeDecision) -> None: ...
    async def on_dca_executed(self, event: DCAExecutedEvent) -> None: ...
    async def on_position_closed(self, event: PositionClosedEvent) -> None: ...
    async def on_error(self, message: str) -> None: ...


class NullNotifier:
    """No-op notifier for tests and backtesting."""

    async def on_buy_signal(self, decision: TradeDecision) -> None: ...
    async def on_no_trade(self, decision: TradeDecision) -> None: ...
    async def on_buy_executed(self, event: BuyExecutedEvent) -> None: ...
    async def on_dca_signal(self, decision: TradeDecision) -> None: ...
    async def on_dca_executed(self, event: DCAExecutedEvent) -> None: ...
    async def on_position_closed(self, event: PositionClosedEvent) -> None: ...
    async def on_error(self, message: str) -> None:
        logger.error(message)


_DCA_PURPOSE = {1: OrderPurpose.DCA_1, 2: OrderPurpose.DCA_2, 3: OrderPurpose.DCA_3}


class StrategyEngine:
    def __init__(
        self,
        *,
        settings: Settings,
        rules: RulesConfig,
        signal_engine: SignalEngine,
        risk_manager: RiskManager,
        execution_engine: ExecutionEngine,
        market_data: MarketDataStore,
        news_provider: NewsProvider,
        notifier: StrategyNotifier | None = None,
    ) -> None:
        self._settings = settings
        self._rules = rules
        self._signal_engine = signal_engine
        self._risk_manager = risk_manager
        self._execution_engine = execution_engine
        self._market_data = market_data
        self._news_provider = news_provider
        self._notifier = notifier or NullNotifier()
        self._anti_fomo = AntiFOMOFilter(rules.anti_fomo)

    def _required_min_score(self, level: RegimeLevel) -> float:
        policy = self._rules.regime_policy.get(level.value)
        delta = policy.min_score_delta if policy else 0.0
        return float(self._settings.min_buy_score) + delta

    def _regime_allows_buy(self, level: RegimeLevel) -> bool:
        policy = self._rules.regime_policy.get(level.value)
        return policy.allow_buy if policy else True

    async def evaluate_candidate(
        self,
        symbol: str,
        *,
        btc_regime: RegimeAssessment,
        open_position_symbols: list[str] | None = None,
    ) -> TradeDecision:
        m15 = self._market_data.snapshot(symbol, Timeframe.M15)
        h1 = self._market_data.snapshot(symbol, Timeframe.H1)
        h4 = self._market_data.snapshot(symbol, Timeframe.H4)
        required_score = self._required_min_score(btc_regime.level)

        if not (m15 and h1 and h4):
            empty = ScoreBreakdown(
                symbol=symbol, technical_score=0, news_adjustment=0, regime_adjustment=0, final_score=0,
                signals=[], confirmed_count=0, confirmed_categories=[],
                vetoes=["insufficient market data"], meets_confirmation_rule=False,
            )
            return TradeDecision(
                action="BLOCKED", symbol=symbol, breakdown=empty, regime=btc_regime,
                required_score=required_score, news_score=0, reasons=["insufficient market data"],
            )

        mtf = MultiTimeframeSnapshot(m15=m15, h1=h1, h4=h4)

        news = await self._news_provider.get_symbol_news_score(symbol)
        news_adjustment = max(-15.0, min(5.0, news.score / 10.0))

        extra_vetoes: list[str] = []
        if news.critical:
            extra_vetoes.append("critical news block: " + "; ".join(news.headlines[:2]))
        if not self._regime_allows_buy(btc_regime.level):
            extra_vetoes.append(f"BTC market regime is {btc_regime.level.value} - new positions blocked")

        change_1h_pct = (h1.close - h1.open) / h1.open * 100 if h1.open else 0.0
        change_4h_pct = (h4.close - h4.open) / h4.open * 100 if h4.open else 0.0
        fomo = self._anti_fomo.check(h1, change_1h_percent=change_1h_pct, change_4h_percent=change_4h_pct)
        if not fomo.passed:
            extra_vetoes.append(fomo.reason or "AntiFOMO block")

        blacklist_check = check_blacklist(symbol, self._rules.universe)
        if not blacklist_check.passed:
            extra_vetoes.append(blacklist_check.reason or "blacklisted")

        if open_position_symbols:
            candidate_df = self._market_data.dataframe(symbol, Timeframe.H1)
            if candidate_df is not None and not candidate_df.empty:
                closes_by_symbol = {}
                for other_symbol in open_position_symbols:
                    other_df = self._market_data.dataframe(other_symbol, Timeframe.H1)
                    if other_df is not None and not other_df.empty:
                        closes_by_symbol[other_symbol] = other_df["close"]
                corr_check = check_correlation_limit(
                    symbol, candidate_df["close"], open_position_symbols, closes_by_symbol, self._rules.correlation
                )
                if not corr_check.passed:
                    extra_vetoes.append(corr_check.reason or "correlation limit reached")

        regime_policy = self._rules.regime_policy.get(btc_regime.level.value)
        breakdown = self._signal_engine.evaluate(
            symbol, mtf, news_adjustment=news_adjustment,
            regime_adjustment=regime_policy.min_score_delta if regime_policy else 0.0,
            extra_vetoes=extra_vetoes,
        )

        if breakdown.blocked:
            action, reasons = "BLOCKED", list(breakdown.vetoes)
        elif breakdown.final_score >= required_score and breakdown.meets_confirmation_rule:
            action, reasons = "BUY", breakdown.top_reasons()
        else:
            reasons = []
            if breakdown.final_score < required_score:
                reasons.append(f"score {breakdown.final_score:.0f} below required {required_score:.0f}")
            if not breakdown.meets_confirmation_rule:
                reasons.append(
                    f"only {breakdown.confirmed_count} signal(s) across "
                    f"{len(breakdown.confirmed_categories)} categories "
                    f"(need {self._settings.min_confirmed_signals}/{self._settings.min_confirmation_categories})"
                )
            action = "NO_TRADE"

        return TradeDecision(
            action=action, symbol=symbol, breakdown=breakdown, regime=btc_regime,
            required_score=required_score, news_score=news.score, reasons=reasons,
        )

    def _record_signal(self, decision: TradeDecision) -> None:
        db_decision = {"BUY": SignalDecision.BUY, "DCA": SignalDecision.DCA, "BLOCKED": SignalDecision.BLOCKED}.get(
            decision.action, SignalDecision.NO_TRADE
        )
        with session_scope() as session:
            SignalRepository(session).record(
                symbol=decision.symbol,
                buy_score=int(decision.breakdown.final_score),
                breakdown={s.name: {"confirmed": s.confirmed, "points": s.points, "category": s.category} for s in decision.breakdown.signals},
                confirmed_categories=decision.breakdown.confirmed_categories,
                decision=db_decision,
                reasons=decision.reasons,
            )

    async def try_open_position(
        self,
        symbol: str,
        *,
        btc_regime: RegimeAssessment,
        trading_balance_usdt: Decimal,
        order_book: OrderBookSnapshot,
        open_position_symbols: list[str] | None = None,
    ) -> TradeDecision:
        decision = await self.evaluate_candidate(symbol, btc_regime=btc_regime, open_position_symbols=open_position_symbols)
        self._record_signal(decision)

        if decision.action != "BUY":
            await self._notifier.on_no_trade(decision)
            return decision

        liquidity_check = check_liquidity_fresh(order_book, self._settings)
        if not liquidity_check.passed:
            blocked = replace(decision, action="BLOCKED", reasons=[liquidity_check.reason or "illiquid"])
            await self._notifier.on_no_trade(blocked)
            return blocked

        await self._notifier.on_buy_signal(decision)

        risk_decision = self._risk_manager.can_open_new_position(
            requested_usdt=self._settings.initial_order_usdt,
            trading_balance_usdt=trading_balance_usdt,
            regime=btc_regime,
        )
        if not risk_decision.allowed:
            blocked = replace(decision, action="BLOCKED", reasons=risk_decision.reasons)
            await self._notifier.on_no_trade(blocked)
            return blocked

        result = await self._execution_engine.buy(
            symbol=symbol, usdt_amount=self._settings.initial_order_usdt, reference_price=order_book.mid_price,
            spread_percent=order_book.spread_percent, purpose=OrderPurpose.ENTRY, position_id=None,
        )
        if not result.accepted or result.net_base_quantity <= 0:
            await self._notifier.on_error(f"BUY order for {symbol} failed: {result.error_message}")
            return replace(decision, action="BLOCKED", reasons=[result.error_message or "order failed"])

        target_price = compute_target_price(
            result.avg_fill_price, target_profit_percent=self._settings.target_profit_percent,
            taker_fee_rate=self._settings.taker_fee_rate, expected_slippage_percent=self._settings.expected_slippage_percent,
        )
        with session_scope() as session:
            position = PositionRepository(session).create(
                symbol=symbol, opened_at=utcnow(), avg_entry_price=result.avg_fill_price,
                total_quantity=result.net_base_quantity,
                # Cost basis must be derived from the *net* quantity (price x
                # net_base_quantity), not the gross fill notional
                # (result.filled_quote) - otherwise the first DCA's call to
                # apply_fill_and_recompute() mixes a gross-based cost with a
                # net-based quantity and skews the recomputed average.
                total_cost_usdt=result.avg_fill_price * result.net_base_quantity,
                target_price=target_price, market_regime_at_entry=btc_regime.level.value,
                entry_score=int(decision.breakdown.final_score),
                entry_signals={s.name: s.confirmed for s in decision.breakdown.signals},
            )
            position_id = position.id

        self._risk_manager.record_new_capital_deployed(result.filled_quote)

        await self._notifier.on_buy_executed(
            BuyExecutedEvent(
                symbol=symbol, price=result.avg_fill_price, usdt_amount=result.filled_quote,
                quantity=result.net_base_quantity, breakdown=decision.breakdown, regime=btc_regime,
                news_score=decision.news_score, target_price=target_price,
                dca_plan=dca_plan(self._settings), position_id=position_id,
            )
        )
        return replace(decision, action="BUY")

    async def manage_position(
        self, position_id: int, *, btc_regime: RegimeAssessment, current_price: Decimal, order_book: OrderBookSnapshot
    ) -> None:
        with session_scope() as session:
            position = PositionRepository(session).get(position_id)
            if position is None or position.status != PositionStatus.OPEN:
                return
            symbol = position.symbol
            avg_entry = position.avg_entry_price
            target_price = position.target_price
            dca_count = position.dca_count
            total_cost = position.total_cost_usdt
            total_qty = position.total_quantity
            trailing_active = position.trailing_active
            trailing_peak = position.trailing_peak_price
            partial_closed = position.partial_closed_quantity

        if trailing_active:
            new_peak = max(trailing_peak or current_price, current_price)
            if new_peak != trailing_peak:
                with session_scope() as session:
                    p = PositionRepository(session).get(position_id)
                    assert p is not None
                    PositionRepository(session).set_trailing(p, active=True, peak_price=new_peak)
            if should_exit_trailing(current_price, new_peak, self._settings):
                remaining_qty = total_qty - partial_closed
                await self._close_position(position_id, exit_price=current_price, quantity=remaining_qty,
                                             reason="TRAILING_STOP", order_book=order_book)
            return

        if current_price >= target_price:
            if self._settings.use_trailing_after_tp:
                # Exact step/precision rounding happens inside ExecutionEngine
                # (it has the live SymbolFilters); this only needs to be close.
                partial_qty = (total_qty * self._settings.trailing_partial_close_fraction).quantize(
                    Decimal("0.00000001"), rounding=ROUND_DOWN
                )
                result = await self._execution_engine.sell(
                    symbol=symbol, quantity=partial_qty, reference_price=current_price,
                    spread_percent=order_book.spread_percent, purpose=OrderPurpose.TAKE_PROFIT, position_id=position_id,
                )
                if result.accepted:
                    with session_scope() as session:
                        p = PositionRepository(session).get(position_id)
                        assert p is not None
                        PositionRepository(session).record_partial_close(p, result.filled_quantity)
                        PositionRepository(session).set_trailing(p, active=True, peak_price=current_price)
                else:
                    await self._notifier.on_error(f"Partial take-profit for {symbol} failed: {result.error_message}")
                return
            await self._close_position(position_id, exit_price=current_price, quantity=total_qty,
                                         reason="TAKE_PROFIT", order_book=order_book)
            return

        level = next_dca_level(
            current_price=current_price, avg_entry_price=avg_entry, dca_count_done=dca_count, settings=self._settings
        )
        if level is None:
            return

        decision = await self.evaluate_candidate(symbol, btc_regime=btc_regime)
        liquidity_check = check_liquidity_fresh(order_book, self._settings)
        dca_risk = self._risk_manager.can_dca(regime=btc_regime)
        dca_decision = evaluate_dca(
            current_price=current_price, avg_entry_price=avg_entry, dca_count_done=dca_count,
            current_position_cost_usdt=total_cost, settings=self._settings, score_breakdown=decision.breakdown,
            market_crash=btc_regime.level == RegimeLevel.CRASH,
            news_blocks_trading=any("news" in v.lower() for v in decision.breakdown.vetoes),
            liquidity_ok=liquidity_check.passed,
        )
        if not (dca_risk.allowed and dca_decision.allowed):
            reasons = dca_risk.reasons + dca_decision.reasons
            self._record_signal(replace(decision, action="NO_TRADE", reasons=reasons))
            await self._notifier.on_no_trade(replace(decision, action="NO_TRADE", reasons=reasons))
            return

        self._record_signal(replace(decision, action="DCA"))
        await self._notifier.on_dca_signal(replace(decision, action="DCA"))

        result = await self._execution_engine.buy(
            symbol=symbol, usdt_amount=level.size_usdt, reference_price=current_price,
            spread_percent=order_book.spread_percent, purpose=_DCA_PURPOSE[level.level_index], position_id=position_id,
        )
        if not result.accepted or result.net_base_quantity <= 0:
            await self._notifier.on_error(f"DCA order for {symbol} failed: {result.error_message}")
            return

        with session_scope() as session:
            p = PositionRepository(session).get(position_id)
            assert p is not None
            PositionRepository(session).apply_fill_and_recompute(
                p, fill_price=result.avg_fill_price, fill_qty=result.net_base_quantity,
                fee_usdt_equivalent=result.commission_total_usdt_equivalent, dca=True,
            )
            new_target = compute_target_price(
                p.avg_entry_price, target_profit_percent=self._settings.target_profit_percent,
                taker_fee_rate=self._settings.taker_fee_rate, expected_slippage_percent=self._settings.expected_slippage_percent,
            )
            PositionRepository(session).update_target_price(p, new_target)
            new_avg = p.avg_entry_price

        self._risk_manager.record_new_capital_deployed(result.filled_quote)
        await self._notifier.on_dca_executed(
            DCAExecutedEvent(
                symbol=symbol, level_index=level.level_index, price=result.avg_fill_price,
                usdt_amount=result.filled_quote, new_avg_entry=new_avg, new_target_price=new_target,
                position_id=position_id,
            )
        )

    async def _close_position(
        self, position_id: int, *, exit_price: Decimal, quantity: Decimal, reason: str, order_book: OrderBookSnapshot
    ) -> None:
        with session_scope() as session:
            position = PositionRepository(session).get(position_id)
            if position is None:
                return
            symbol, avg_entry, opened_at = position.symbol, position.avg_entry_price, position.opened_at

        purpose = OrderPurpose.TAKE_PROFIT if reason == "TAKE_PROFIT" else OrderPurpose.TRAILING_STOP
        result = await self._execution_engine.sell(
            symbol=symbol, quantity=quantity, reference_price=exit_price,
            spread_percent=order_book.spread_percent, purpose=purpose, position_id=position_id,
        )
        if not result.accepted:
            await self._notifier.on_error(f"SELL order for {symbol} failed: {result.error_message}")
            return

        proceeds = result.filled_quote - result.commission_total_usdt_equivalent
        cost_basis = avg_entry * result.filled_quantity

        with session_scope() as session:
            position = PositionRepository(session).get(position_id)
            assert position is not None
            net_pnl = proceeds - cost_basis
            net_pnl_pct = (proceeds / cost_basis - 1) * 100 if cost_basis > 0 else Decimal("0")
            PositionRepository(session).close(
                position, closed_at=utcnow(), realized_pnl_usdt=net_pnl, realized_pnl_pct=net_pnl_pct, close_reason=reason
            )
            holding_seconds = (utcnow() - opened_at).total_seconds()

        self._risk_manager.register_trade_result(is_win=net_pnl > 0)
        await self._notifier.on_position_closed(
            PositionClosedEvent(
                symbol=symbol, exit_price=exit_price, avg_entry_price=avg_entry, net_pnl_usdt=net_pnl,
                net_pnl_percent=net_pnl_pct, holding_time_seconds=holding_seconds, close_reason=reason,
                position_id=position_id,
            )
        )
