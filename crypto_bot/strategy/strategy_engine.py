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

A BUY/SELL order accepted by the exchange is not necessarily *filled*: on a
wide spread `ExecutionEngine` places a LIMIT order that can come back
`accepted=True` with zero quantity filled so far (still resting in the
book). Every method below that submits an order checks actual filled
quantity, never just `accepted`, before creating/reducing/closing a
Position - and `process_resolved_orders()` is what finishes the job for an
order that was resting at submit time and only fills (or times out) later,
polling `ExecutionEngine.check_pending_limit_orders()`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol

from config.settings import RulesConfig, Settings
from database.models import (
    Order,
    OrderPurpose,
    OrderSide,
    OrderStatus,
    PositionStatus,
    SignalDecision,
)
from database.repository import OrderRepository, PositionRepository, SignalRepository
from database.session import session_scope
from exchange.execution_engine import ExecutionEngine, ExecutionResult
from market.market_data import MarketDataStore
from market.market_regime import RegimeAssessment, RegimeLevel
from market.orderbook import OrderBookSnapshot
from risk.correlation import check_correlation_limit
from risk.crash_detector import apply_crash_policy
from risk.risk_manager import RiskManager
from strategy.dca import DCALevel, dca_plan, evaluate_dca, next_dca_level
from strategy.filters import AntiFOMOFilter, check_blacklist, check_liquidity_fresh
from strategy.scoring import ScoreBreakdown
from strategy.signal_engine import MultiTimeframeSnapshot, SignalEngine
from strategy.take_profit import (
    compute_target_price,
    should_arm_early_protection,
    should_exit_trailing,
    should_force_close_ceiling,
)
from utils.time import Timeframe, utcnow

logger = logging.getLogger(__name__)

_MTF_TIMEFRAMES = (Timeframe.M15, Timeframe.H1, Timeframe.H4)
# A cached DCA re-analysis is never reused longer than one candle's worth of
# wall-clock time, even if the candle id somehow stops advancing.
_DCA_DECISION_MAX_AGE_SECONDS = 15 * 60
_STALE_DATA_VETO = "stale market data"
_INSUFFICIENT_DATA_VETO = "insufficient market data"
_MARKET_DATA_VETOES = (_STALE_DATA_VETO, _INSUFFICIENT_DATA_VETO)


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
    strong_signal: bool = False  # entered with STRONG_SIGNAL_ORDER_USDT


_PARTIAL_FILL = "біржа продала лише частину (мало покупців у стакані)"


@dataclass(frozen=True)
class ManualSellResult:
    """Outcome of the owner's /sell. status: sold | sold_with_warning |
    partial | failed | no_position | no_order_book."""

    status: str
    detail: str | None = None
    remaining: Decimal | None = None  # quantity still held when not fully sold


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


@dataclass(frozen=True)
class DrawdownWarningEvent:
    """One-shot (per position, per threshold) risk alert - see
    `Position.drawdown_alert_20_sent`/`_30_sent` and
    `Settings.drawdown_warning_percent_1`/`_2`. Purely informational: never
    changes DCA/exit decisions, only tells the operator a held position has
    dropped significantly below its entry price."""

    symbol: str
    avg_entry_price: Decimal
    current_price: Decimal
    drawdown_percent: Decimal  # positive number, e.g. 24.7 for a -24.7% move
    threshold_percent: Decimal  # the configured threshold that was crossed (20 or 30)
    position_id: int


@dataclass(frozen=True)
class DelayedFillEvent:
    """A LIMIT order that was still resting when its submitting call
    returned, and has now resolved (filled, in full or in part) via
    `process_resolved_orders()` - reported separately from the immediate-
    fill events above because none of the original scoring/decision
    context is available this long after the fact, only the resolved
    exchange fill itself."""

    symbol: str
    side: str  # "BUY" | "SELL"
    price: Decimal
    quantity: Decimal
    usdt_amount: Decimal
    purpose: str
    position_id: int


class StrategyNotifier(Protocol):
    async def on_buy_signal(self, decision: TradeDecision) -> None: ...
    async def on_no_trade(self, decision: TradeDecision) -> None: ...
    async def on_buy_executed(self, event: BuyExecutedEvent) -> None: ...
    async def on_dca_signal(self, decision: TradeDecision) -> None: ...
    async def on_dca_executed(self, event: DCAExecutedEvent) -> None: ...
    async def on_position_closed(self, event: PositionClosedEvent) -> None: ...
    async def on_delayed_fill(self, event: DelayedFillEvent) -> None: ...
    async def on_drawdown_warning(self, event: DrawdownWarningEvent) -> None: ...
    async def on_error(self, message: str) -> None: ...


class NullNotifier:
    """No-op notifier for tests and backtesting."""

    async def on_buy_signal(self, decision: TradeDecision) -> None: ...
    async def on_no_trade(self, decision: TradeDecision) -> None: ...
    async def on_buy_executed(self, event: BuyExecutedEvent) -> None: ...
    async def on_dca_signal(self, decision: TradeDecision) -> None: ...
    async def on_dca_executed(self, event: DCAExecutedEvent) -> None: ...
    async def on_position_closed(self, event: PositionClosedEvent) -> None: ...
    async def on_delayed_fill(self, event: DelayedFillEvent) -> None: ...
    async def on_drawdown_warning(self, event: DrawdownWarningEvent) -> None: ...
    async def on_error(self, message: str) -> None:
        logger.error(message)


_DCA_PURPOSE = {1: OrderPurpose.DCA_1, 2: OrderPurpose.DCA_2, 3: OrderPurpose.DCA_3}
_DCA_LEVEL_BY_PURPOSE = {v: k for k, v in _DCA_PURPOSE.items()}
_EXIT_CLOSE_REASON = {
    OrderPurpose.TAKE_PROFIT: "TAKE_PROFIT",
    OrderPurpose.TRAILING_STOP: "TRAILING_STOP",
    OrderPurpose.EMERGENCY_SELL: "EMERGENCY_SELL",
    OrderPurpose.HARD_CEILING: "HARD_PROFIT_CEILING",
    OrderPurpose.MANUAL_SELL: "MANUAL_SELL",
}


def _still_resting(result: ExecutionResult) -> bool:
    """True for an accepted order that hasn't finished: nothing filled yet,
    or PARTIALLY_FILLED and still in the book. A partial fill must NOT be
    applied at submit time - the order stays tracked, and when it resolves
    Binance reports its *cumulative* fills (myTrades for the whole order),
    which the resolution path applies. Applying both would count the early
    slice twice (double DCA / double-reduced position / untracked coins)."""
    return result.net_base_quantity <= 0 or result.status == OrderStatus.PARTIALLY_FILLED


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
        # Per-position mutual exclusion between the position monitor
        # (manage_position) and /emergency_stop (emergency_liquidate_all,
        # run from the Telegram handler's own task): both read the same OPEN
        # quantity and sell it, so interleaved they could sell it twice.
        # Never removed - one tiny Lock per position opened this process.
        self._position_locks: dict[int, asyncio.Lock] = {}
        # Serializes the order-resolution poll's commit + apply of each
        # resolved order (persisted first, fill applied after, with awaits
        # in between) with /emergency_stop's liquidation, so the kill switch
        # never reads a position's quantity in that window and sells a
        # stale amount. The poll's exchange calls happen outside it.
        # Lock order is always resolution -> position; nothing holding a
        # position lock takes this one.
        self._resolution_lock = asyncio.Lock()
        # position_id -> ((DCA level index, M15/H1/H4 candle open_times,
        # regime level), decision, monotonic scored-at, (news score,
        # critical)): see _dca_candidate_decision.
        self._dca_decisions: dict[
            int, tuple[tuple[int, tuple[Any, ...], RegimeLevel], TradeDecision, float, tuple[int, bool]]
        ] = {}

    def _position_lock(self, position_id: int) -> asyncio.Lock:
        """asyncio.Lock is not re-entrant: take it only at the two entry
        points (manage_position, emergency_liquidate_all), never in a helper
        they call (_force_close_ceiling, _cancel_resting_orders_for_position,
        apply_resolved_order, ...) or the second acquire deadlocks."""
        lock = self._position_locks.get(position_id)
        if lock is None:
            lock = self._position_locks[position_id] = asyncio.Lock()
        return lock

    def _required_min_score(self, level: RegimeLevel) -> float:
        policy = self._rules.regime_policy.get(level.value)
        delta = policy.min_score_delta if policy else 0.0
        return float(self._settings.min_buy_score) + delta

    def regime_allows_buy(self, level: RegimeLevel) -> bool:
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

        stale = (m15 and h1 and h4) and any(
            self._market_data.is_stale(symbol, tf, self._settings.market_data_stale_seconds)
            for tf in _MTF_TIMEFRAMES
        )
        if not (m15 and h1 and h4) or stale:
            reason = _STALE_DATA_VETO if stale else _INSUFFICIENT_DATA_VETO
            empty = ScoreBreakdown(
                symbol=symbol, technical_score=0, news_adjustment=0, regime_adjustment=0, final_score=0,
                signals=[], confirmed_count=0, confirmed_categories=[],
                vetoes=[reason], meets_confirmation_rule=False,
            )
            return TradeDecision(
                action="BLOCKED", symbol=symbol, breakdown=empty, regime=btc_regime,
                required_score=required_score, news_score=0, reasons=[reason],
            )

        mtf = MultiTimeframeSnapshot(m15=m15, h1=h1, h4=h4)

        news = await self._news_provider.get_symbol_news_score(symbol)
        news_adjustment = max(-15.0, min(5.0, news.score / 10.0))

        extra_vetoes: list[str] = []
        if news.critical:
            extra_vetoes.append("critical news block: " + "; ".join(news.headlines[:2]))
        if not self.regime_allows_buy(btc_regime.level):
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
                # Indexed by candle open time so returns align by bar, not position.
                closes_by_symbol = {}
                for other_symbol in open_position_symbols:
                    other_df = self._market_data.dataframe(other_symbol, Timeframe.H1)
                    if other_df is not None and not other_df.empty:
                        closes_by_symbol[other_symbol] = other_df.set_index("open_time")["close"]
                corr_check = check_correlation_limit(
                    symbol, candidate_df.set_index("open_time")["close"], open_position_symbols, closes_by_symbol,
                    self._rules.correlation,
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

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    async def try_open_position(
        self,
        symbol: str,
        *,
        btc_regime: RegimeAssessment,
        trading_balance_usdt: Decimal,
        order_book: OrderBookSnapshot,
        open_position_symbols: list[str] | None = None,
        strong_size_allowed: bool = False,
    ) -> TradeDecision:
        """`strong_size_allowed`: the caller's long-term market check (the
        bigger entry is only ever used while the long-term phase is BULL)."""
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

        strong = strong_size_allowed and self.is_strong_signal(decision)
        order_usdt = self._settings.strong_signal_order_usdt if strong else self._settings.initial_order_usdt
        risk_decision = self._risk_manager.can_open_new_position(
            requested_usdt=order_usdt,
            trading_balance_usdt=trading_balance_usdt,
            regime=btc_regime,
        )
        if not risk_decision.allowed and strong:
            # The bigger entry must never cost the trade itself: if the caps
            # don't fit it, take the normal size instead.
            strong, order_usdt = False, self._settings.initial_order_usdt
            risk_decision = self._risk_manager.can_open_new_position(
                requested_usdt=order_usdt, trading_balance_usdt=trading_balance_usdt, regime=btc_regime,
            )
        if not risk_decision.allowed:
            blocked = replace(decision, action="BLOCKED", reasons=risk_decision.reasons)
            await self._notifier.on_no_trade(blocked)
            return blocked

        result = await self._execution_engine.buy(
            symbol=symbol, usdt_amount=order_usdt, reference_price=order_book.mid_price,
            spread_percent=order_book.spread_percent, purpose=OrderPurpose.ENTRY, position_id=None,
        )
        if not result.accepted:
            await self._notifier.on_error(f"BUY order for {symbol} failed: {result.error_message}")
            return replace(decision, action="BLOCKED", reasons=[result.error_message or "order failed"])
        if _still_resting(result):
            # Accepted but not (fully) filled yet - a LIMIT order resting in
            # the book, not a failure. ExecutionEngine already tracks it in
            # _pending_limit_orders; process_resolved_orders() creates the
            # Position once it resolves, or does nothing if it times out
            # unfilled.
            return replace(decision, action="BLOCKED", reasons=["order accepted, resting in book - awaiting fill or timeout"])

        position_id, target_price = await self._apply_entry_fill(
            symbol, result=result, btc_regime=btc_regime, order_id=result.order_id,
            entry_score=round(decision.breakdown.final_score),
            entry_signals={s.name: s.points for s in decision.breakdown.signals if s.confirmed},
        )
        await self._notifier.on_buy_executed(
            BuyExecutedEvent(
                symbol=symbol, price=result.avg_fill_price, usdt_amount=result.filled_quote,
                quantity=result.net_base_quantity, breakdown=decision.breakdown, regime=btc_regime,
                news_score=decision.news_score, target_price=target_price,
                dca_plan=dca_plan(self._settings), position_id=position_id, strong_signal=strong,
            )
        )
        return replace(decision, action="BUY")

    def is_strong_signal(self, decision: TradeDecision) -> bool:
        """The score beats what the current BTC regime requires by at least
        STRONG_SIGNAL_SCORE_MARGIN. Relative, not an absolute score: in weak
        markets the required score is already 80-85, so an absolute "80+"
        rule would put the bigger entries exactly into the riskiest regimes."""
        return (
            self._settings.strong_signal_order_usdt > self._settings.initial_order_usdt
            and decision.breakdown.final_score >= decision.required_score + self._settings.strong_signal_score_margin
        )

    async def _apply_entry_fill(
        self,
        symbol: str,
        *,
        result: ExecutionResult,
        btc_regime: RegimeAssessment,
        order_id: int | None,
        entry_score: int | None,
        entry_signals: dict[str, float] | None,
    ) -> tuple[int, Decimal]:
        """Creates the Position for a filled entry - shared by the
        immediate-fill path (`try_open_position`) and the delayed-fill path
        (`_resolve_entry_order`, once a resting LIMIT entry finally fills).
        `entry_score`/`entry_signals` (confirmed signal name -> points) record
        why it was bought; the delayed path no longer has the decision and
        passes None. Returns (position_id, target_price)."""
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
                entry_score=entry_score, entry_signals=entry_signals,
                fees_paid_usdt=result.commission_total_usdt_equivalent,
            )
            position_id = position.id
            if order_id is not None:
                order_repo = OrderRepository(session)
                order = order_repo.get(order_id)
                assert order is not None
                order_repo.set_position(order, position_id)

        self._risk_manager.record_new_capital_deployed(result.filled_quote)
        return position_id, target_price

    # ------------------------------------------------------------------
    # Position management (DCA / take-profit / trailing-stop)
    # ------------------------------------------------------------------

    async def manage_position(
        self,
        position_id: int,
        *,
        btc_regime: RegimeAssessment,
        current_price: Decimal,
        order_book: OrderBookSnapshot,
        trading_balance_usdt: Decimal | None,
    ) -> None:
        """`trading_balance_usdt=None` means the caller could not fetch the
        balance this cycle: only DCA (whose exposure cap needs it) is
        skipped - drawdown alerts, the hard ceiling, trailing, early arm and
        take-profit never depend on it and still run.

        Holds the position's lock for the whole call, so an /emergency_stop
        liquidation can't sell this position in the middle of it."""
        async with self._position_lock(position_id):
            await self._manage_position_locked(
                position_id, btc_regime=btc_regime, current_price=current_price,
                order_book=order_book, trading_balance_usdt=trading_balance_usdt,
            )

    async def _manage_position_locked(
        self,
        position_id: int,
        *,
        btc_regime: RegimeAssessment,
        current_price: Decimal,
        order_book: OrderBookSnapshot,
        trading_balance_usdt: Decimal | None,
    ) -> None:
        with session_scope() as session:
            position = PositionRepository(session).get(position_id)
            if position is None or position.status != PositionStatus.OPEN:
                self._dca_decisions.pop(position_id, None)
                return
            symbol = position.symbol
            avg_entry = position.avg_entry_price
            target_price = position.target_price
            dca_count = position.dca_count
            total_cost = position.total_cost_usdt
            total_qty = position.total_quantity
            trailing_active = position.trailing_active
            trailing_peak = position.trailing_peak_price
            trailing_is_early = position.trailing_is_early
            drawdown_20_sent = position.drawdown_alert_20_sent
            drawdown_30_sent = position.drawdown_alert_30_sent
            # A LIMIT order can rest for up to LIMIT_ORDER_TIMEOUT_SECONDS
            # (90s by default), longer than this loop's own polling
            # interval (60s by default) - without this guard, the same
            # DCA/target/trailing-exit condition still being true on the
            # next tick would submit a second order for the same
            # level/exit before the first has had a chance to resolve.
            # process_resolved_orders() is what finishes the resting one;
            # this method just waits for it.
            has_resting_order = OrderRepository(session).has_resting_order(symbol=symbol, position_id=position_id)

        # Pure notification, no effect on what follows - always evaluated,
        # regardless of resting orders/trailing/ceiling state below.
        await self._check_drawdown_warnings(
            position_id, symbol=symbol, avg_entry=avg_entry, current_price=current_price,
            already_sent_20=drawdown_20_sent, already_sent_30=drawdown_30_sent,
        )

        if should_force_close_ceiling(avg_entry_price=avg_entry, current_price=current_price, settings=self._settings):
            # HARD_PROFIT_CEILING_PERCENT backstop - deliberately checked
            # before has_resting_order below: this must override a stuck
            # resting order too, since the whole point is to catch cases
            # where the normal exit logic should already have closed the
            # position but, for whatever reason, has not.
            await self._force_close_ceiling(position_id, symbol=symbol, current_price=current_price, btc_regime=btc_regime)
            return

        if has_resting_order:
            return

        if trailing_active:
            new_peak = max(trailing_peak or current_price, current_price)
            if new_peak != trailing_peak:
                with session_scope() as session:
                    p = PositionRepository(session).get(position_id)
                    assert p is not None
                    PositionRepository(session).set_trailing(p, active=True, peak_price=new_peak, is_early=trailing_is_early)
            distance = (
                self._settings.early_profit_trailing_distance_percent
                if trailing_is_early else self._settings.trailing_distance_percent
            )
            if should_exit_trailing(current_price, new_peak, distance):
                await self._submit_and_apply_sell(
                    position_id, symbol=symbol, quantity=total_qty, reference_price=current_price,
                    # force MARKET: a protective exit fires into a falling price, where a
                    # wide-spread LIMIT at mid can sit unfilled while the drop continues
                    spread_percent=Decimal("0"), purpose=OrderPurpose.TRAILING_STOP,
                    reason="TRAILING_STOP", error_context="SELL order",
                )
            return

        if should_arm_early_protection(avg_entry_price=avg_entry, current_price=current_price, settings=self._settings):
            with session_scope() as session:
                p = PositionRepository(session).get(position_id)
                assert p is not None
                PositionRepository(session).set_trailing(p, active=True, peak_price=current_price, is_early=True)
            return

        if current_price >= target_price:
            if self._settings.use_trailing_after_tp:
                # Exact step/precision rounding happens inside ExecutionEngine
                # (it has the live SymbolFilters); this only needs to be close.
                partial_qty = (total_qty * self._settings.trailing_partial_close_fraction).quantize(
                    Decimal("0.00000001"), rounding=ROUND_DOWN
                )
                outcome = await self._submit_and_apply_sell(
                    position_id, symbol=symbol, quantity=partial_qty, reference_price=current_price,
                    spread_percent=order_book.spread_percent, purpose=OrderPurpose.TAKE_PROFIT,
                    reason="TAKE_PROFIT", error_context="Partial take-profit",
                )
                if outcome is not None and not outcome[1]:
                    with session_scope() as session:
                        p = PositionRepository(session).get(position_id)
                        assert p is not None
                        PositionRepository(session).set_trailing(p, active=True, peak_price=current_price)
                return
            await self._submit_and_apply_sell(
                position_id, symbol=symbol, quantity=total_qty, reference_price=current_price,
                spread_percent=order_book.spread_percent, purpose=OrderPurpose.TAKE_PROFIT,
                reason="TAKE_PROFIT", error_context="SELL order",
            )
            return

        level = next_dca_level(
            current_price=current_price, avg_entry_price=avg_entry, dca_count_done=dca_count, settings=self._settings
        )
        if level is None:
            return
        if trading_balance_usdt is None:
            logger.info(
                "DCA level %s reached for %s but the trading balance is unavailable this cycle - DCA waits for the next tick",
                level.level_index, symbol,
            )
            return

        decision, fresh_decision = await self._dca_candidate_decision(
            position_id, symbol, level_index=level.level_index, btc_regime=btc_regime
        )
        liquidity_check = check_liquidity_fresh(order_book, self._settings)
        dca_risk = self._risk_manager.can_dca(
            regime=btc_regime, requested_usdt=level.size_usdt, trading_balance_usdt=trading_balance_usdt
        )
        news_blocks = any("news" in v.lower() for v in decision.breakdown.vetoes)
        dca_decision = evaluate_dca(
            current_price=current_price, avg_entry_price=avg_entry, dca_count_done=dca_count,
            current_position_cost_usdt=total_cost, settings=self._settings, score_breakdown=decision.breakdown,
            market_crash=apply_crash_policy(btc_regime, self._settings).dca_paused,
            news_blocks_trading=news_blocks,
            liquidity_ok=liquidity_check.passed,
        )
        if not (dca_risk.allowed and dca_decision.allowed):
            if fresh_decision:  # a reused decision's NO_TRADE was already recorded this candle
                reasons = dca_risk.reasons + dca_decision.reasons
                self._record_signal(replace(decision, action="NO_TRADE", reasons=reasons))
                await self._notifier.on_no_trade(replace(decision, action="NO_TRADE", reasons=reasons))
            return

        self._dca_decisions.pop(position_id, None)
        self._record_signal(replace(decision, action="DCA"))
        await self._notifier.on_dca_signal(replace(decision, action="DCA"))

        result = await self._execution_engine.buy(
            symbol=symbol, usdt_amount=level.size_usdt, reference_price=current_price,
            spread_percent=order_book.spread_percent, purpose=_DCA_PURPOSE[level.level_index], position_id=position_id,
        )
        if not result.accepted:
            await self._notifier.on_error(f"DCA order for {symbol} failed: {result.error_message}")
            return
        if _still_resting(result):
            # Resting LIMIT order - process_resolved_orders() applies the
            # fill once it resolves, or does nothing if it times out unfilled.
            return

        await self._apply_dca_fill(position_id, symbol=symbol, level_index=level.level_index, result=result)

    async def _dca_candidate_decision(
        self, position_id: int, symbol: str, *, level_index: int, btc_regime: RegimeAssessment
    ) -> tuple[TradeDecision, bool]:
        """The full `evaluate_candidate()` behind a DCA, run at most once per
        position per DCA level per closed 15m candle. Returns (decision,
        fresh); `fresh` is False for a reused decision, whose NO_TRADE signal
        was already recorded.

        The position monitor polls every 60s but the indicators only change
        on a candle close: while price sat below an unmet DCA level, every
        tick re-scored and wrote another NO_TRADE row (~40 signal rows per
        candle instead of the 25 the health check expects). Only this
        scoring is cached - the caller still runs the cheap gates (liquidity,
        can_dca, evaluate_dca) every tick, so a DCA still happens mid-candle
        if e.g. exposure frees up. Without an M15 snapshot there is no
        candle id, so nothing is cached.

        A cached decision is re-scored early when anything it was scored on
        changes: an H1/H4 candle closing, the BTC regime (e.g. into
        STRONG_BEAR, whose veto is the only thing blocking DCA there), or
        the symbol's news. The news check is a cheap DB read, so a critical
        headline still blocks a DCA straight away rather than up to 15
        minutes later."""
        candle_ids = tuple(
            snap.open_time if snap is not None else None
            for snap in (self._market_data.snapshot(symbol, tf) for tf in _MTF_TIMEFRAMES)
        )
        key = (level_index, candle_ids, btc_regime.level)
        news = await self._news_provider.get_symbol_news_score(symbol)
        news_state = (news.score, news.critical)
        cached = self._dca_decisions.get(position_id)
        if (
            cached is not None and cached[0] == key and cached[3] == news_state
            and time.monotonic() - cached[2] < _DCA_DECISION_MAX_AGE_SECONDS
            and not self._any_series_stale(symbol)
        ):
            return cached[1], False
        decision = await self.evaluate_candidate(symbol, btc_regime=btc_regime)
        if candle_ids[0] is None or any(r in _MARKET_DATA_VETOES for r in decision.breakdown.vetoes):
            # A 'stale/insufficient market data' block says nothing about
            # the next tick: once the feed recovers the position must be
            # scored for real, not held off by a cached block.
            self._dca_decisions.pop(position_id, None)
        else:
            self._dca_decisions[position_id] = (key, decision, time.monotonic(), news_state)
        return decision, True

    def _any_series_stale(self, symbol: str) -> bool:
        """A dead kline feed freezes the last candle's open_time, so the
        candle-keyed cache alone would keep reusing a decision scored on
        data that is by now stale - the very case evaluate_candidate's own
        'stale market data' veto exists to block."""
        return any(
            self._market_data.is_stale(symbol, tf, self._settings.market_data_stale_seconds) for tf in _MTF_TIMEFRAMES
        )

    async def _check_drawdown_warnings(
        self,
        position_id: int,
        *,
        symbol: str,
        avg_entry: Decimal,
        current_price: Decimal,
        already_sent_20: bool,
        already_sent_30: bool,
    ) -> None:
        """One-shot (per position, per threshold) risk alert when price has
        dropped DRAWDOWN_WARNING_PERCENT_1/2 below avg_entry - never
        re-sent once its flag is set (see `Position.drawdown_alert_20_sent`/
        `_30_sent`), including across a restart. Deliberately uses the raw
        price ratio, not the fee/slippage-adjusted net-profit basis the
        take-profit side uses: this is a market-risk warning, not an exit
        price calculation."""
        if avg_entry <= 0:
            return
        drawdown_percent = (avg_entry - current_price) / avg_entry * 100
        for threshold, already_sent, level in (
            (self._settings.drawdown_warning_percent_2, already_sent_30, 30),
            (self._settings.drawdown_warning_percent_1, already_sent_20, 20),
        ):
            if drawdown_percent < threshold or already_sent:
                continue
            with session_scope() as session:
                p = PositionRepository(session).get(position_id)
                if p is None or p.status != PositionStatus.OPEN:
                    return
                PositionRepository(session).mark_drawdown_alert_sent(p, level=level)
            await self._notifier.on_drawdown_warning(
                DrawdownWarningEvent(
                    symbol=symbol, avg_entry_price=avg_entry, current_price=current_price,
                    drawdown_percent=drawdown_percent, threshold_percent=threshold, position_id=position_id,
                )
            )

    async def _apply_dca_fill(
        self, position_id: int, *, symbol: str, level_index: int, result: ExecutionResult
    ) -> None:
        """Folds a filled DCA buy into its position - shared by the
        immediate-fill path (`manage_position`) and the delayed-fill path
        (`_resolve_dca_order`)."""
        with session_scope() as session:
            p = PositionRepository(session).get(position_id)
            position_open = p is not None and p.status == PositionStatus.OPEN
            if p is not None and position_open:
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
        if not position_open:
            # e.g. a resting DCA LIMIT that filled after a force-close: real
            # coins were bought that no position tracks, so nothing will ever
            # sell them - the operator must hear about it.
            if result.net_base_quantity > 0:
                await self._notifier.on_error(
                    f"DCA order for {symbol} filled {result.net_base_quantity} at {result.avg_fill_price} after "
                    f"position #{position_id} was already closed - these coins are NOT tracked by the bot. "
                    "Check Binance and handle them manually."
                )
            return

        self._risk_manager.record_new_capital_deployed(result.filled_quote)
        await self._notifier.on_dca_executed(
            DCAExecutedEvent(
                symbol=symbol, level_index=level_index, price=result.avg_fill_price,
                usdt_amount=result.filled_quote, new_avg_entry=new_avg, new_target_price=new_target,
                position_id=position_id,
            )
        )

    async def _apply_sell_result(
        self, position_id: int, *, result: ExecutionResult, reason: str
    ) -> tuple[Decimal, bool] | None:
        """Reduces or fully closes a position from a SELL ExecutionResult -
        shared by every exit path (full take-profit, a deliberate partial
        take-profit slice, a trailing-stop exit, and the delayed-fill path
        for a LIMIT sell that only resolves later). Returns (this slice's
        PnL, whether the position is now fully closed), or None if nothing
        was actually filled or the position was already gone.
        """
        if result.filled_quantity <= 0:
            return None
        proceeds = result.filled_quote - result.commission_total_usdt_equivalent
        with session_scope() as session:
            position = PositionRepository(session).get(position_id)
            if position is None or position.status != PositionStatus.OPEN:
                return None
            symbol = position.symbol
        # Buy-side commission is taken in the base asset, so a position is
        # routinely off the lot grid (e.g. 7.992 XRP, step 0.1): the full
        # exit sells 7.9 and leaves 0.092 that can never be sold. Without
        # this, that remainder kept the position OPEN forever.
        unsellable = await self._execution_engine.unsellable_quantity(symbol, result.avg_fill_price)
        with session_scope() as session:
            position = PositionRepository(session).get(position_id)
            if position is None or position.status != PositionStatus.OPEN:
                return None
            avg_entry, opened_at = position.avg_entry_price, position.opened_at
            slice_pnl, fully_closed = PositionRepository(session).apply_sell_fill(
                position, sold_quantity=result.filled_quantity, proceeds_usdt=proceeds, now=utcnow(),
                close_reason=reason, unsellable_below=unsellable,
            )
            cumulative_pnl = position.realized_pnl_usdt
            cumulative_pnl_pct = position.realized_pnl_pct

        if fully_closed:
            assert cumulative_pnl is not None and cumulative_pnl_pct is not None
            self._dca_decisions.pop(position_id, None)
            holding_seconds = (utcnow() - opened_at).total_seconds()
            if reason != _EXIT_CLOSE_REASON[OrderPurpose.MANUAL_SELL]:
                # The loss streak measures the strategy; the owner's own /sell
                # is his decision, and counting it could pause all buying
                # behind his back (or reset a real losing streak).
                self._risk_manager.register_trade_result(is_win=cumulative_pnl > 0)
            await self._notifier.on_position_closed(
                PositionClosedEvent(
                    symbol=symbol, exit_price=result.avg_fill_price, avg_entry_price=avg_entry,
                    net_pnl_usdt=cumulative_pnl, net_pnl_percent=cumulative_pnl_pct,
                    holding_time_seconds=holding_seconds, close_reason=reason, position_id=position_id,
                )
            )
        return slice_pnl, fully_closed

    async def _submit_and_apply_sell(
        self,
        position_id: int,
        *,
        symbol: str,
        quantity: Decimal,
        reference_price: Decimal,
        spread_percent: Decimal,
        purpose: OrderPurpose,
        reason: str,
        error_context: str,
        error_sink: list[str] | None = None,
    ) -> tuple[Decimal, bool] | None:
        """Submits a SELL and, if accepted, applies whatever it filled via
        `_apply_sell_result` - the "submit -> check accepted -> notify on
        rejection -> apply the fill" sequence every exit path (trailing-
        stop, full/partial take-profit, the hard profit-ceiling backstop,
        emergency liquidation) needs, so a future change to how a
        rejected/partial sell is handled only has to be made once. Returns
        `_apply_sell_result`'s own `(slice_pnl, fully_closed)` on success,
        or `None` if the exchange rejected the order outright (already
        notified) or nothing ended up filled."""
        result = await self._execution_engine.sell(
            symbol=symbol, quantity=quantity, reference_price=reference_price,
            spread_percent=spread_percent, purpose=purpose, position_id=position_id,
        )
        if not result.accepted:
            await self._notifier.on_error(f"{error_context} for {symbol} failed: {result.error_message}")
            if error_sink is not None:
                error_sink.append(result.error_message or "order rejected")
            return None
        if _still_resting(result):
            return None  # applied once, in full, when process_resolved_orders() sees it resolve
        return await self._apply_sell_result(position_id, result=result, reason=reason)

    async def _cancel_resting_orders_for_position(
        self, position_id: int, symbol: str, *, btc_regime: RegimeAssessment
    ) -> list[Order]:
        """Cancels any order still resting for this position (a DCA or exit
        LIMIT order that hasn't resolved yet) and applies whatever fill it
        had already picked up before being cancelled, via the same dispatch
        `process_resolved_orders` uses. Without this, a force-close
        (emergency liquidation or the hard profit-ceiling backstop) would
        either read a stale `total_quantity` (understating what's actually
        held) or have its own force-sell rejected/oversized against
        Binance's real balance, since the resting order's fill was never
        folded in. Only ever sees DCA_*/TAKE_PROFIT/TRAILING_STOP/
        EMERGENCY_SELL/HARD_CEILING orders here - an ENTRY order has no
        `position_id` until it fills (see `OrderRepository.set_position`),
        so it can never show up in `for_position`.

        Returns the resting orders that could not be confirmed cancelled.
        Their results are not final, so nothing is applied for them here:
        the regular poll keeps tracking them and applies their fills once
        they resolve. The caller must not force-sell while a SELL is among
        them (Binance may still hold its coins locked, so a full-size
        MARKET sell would be rejected or oversell) and retries on a later
        tick instead. An unconfirmed BUY (DCA) doesn't lock the coins: the
        sell can proceed, and if that DCA fills later the untracked-coins
        alert in `_apply_dca_fill` fires.
        """
        with session_scope() as session:
            resting = [
                o for o in OrderRepository(session).for_position(position_id)
                if o.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)
            ]
        unconfirmed: list[Order] = []
        for order in resting:
            result = await self._execution_engine.cancel(symbol, client_order_id=order.client_order_id)
            if result.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED) or result.fill_data_incomplete:
                # Applying it here raised a false "check Binance manually"
                # alert for an order the poll is still handling.
                unconfirmed.append(order)
                logger.warning(
                    "Resting %s %s for %s could not be confirmed cancelled (%s) - left to the order poll",
                    order.side.value, order.client_order_id, symbol, result.status.value,
                )
                continue
            await self.apply_resolved_order(order, result, btc_regime=btc_regime)
        return unconfirmed

    async def _force_close_ceiling(
        self, position_id: int, *, symbol: str, current_price: Decimal, btc_regime: RegimeAssessment
    ) -> None:
        """HARD_PROFIT_CEILING_PERCENT backstop: cancel anything resting for
        this position (folding in whatever it already filled), then force a
        full MARKET close on whatever quantity remains - regardless of
        trailing state. See `manage_position`'s call site for why this must
        run before the resting-order early return."""
        unconfirmed = await self._cancel_resting_orders_for_position(position_id, symbol, btc_regime=btc_regime)
        if any(o.side == OrderSide.SELL for o in unconfirmed):
            # The ceiling condition still holds next tick, so this retries.
            await self._notifier.on_error(
                f"Hard profit-ceiling close for {symbol} postponed: a resting SELL could not be confirmed "
                "cancelled - retrying on the next check"
            )
            return
        with session_scope() as session:
            position = PositionRepository(session).get(position_id)
            if position is None or position.status != PositionStatus.OPEN:
                return  # a cancelled order's own fill already closed it
            quantity = position.total_quantity
        if quantity <= 0:
            return
        await self._submit_and_apply_sell(
            position_id, symbol=symbol, quantity=quantity, reference_price=current_price,
            spread_percent=Decimal("0"),  # force MARKET: a hard safety-net close must not wait in a resting LIMIT order
            purpose=OrderPurpose.HARD_CEILING, reason="HARD_PROFIT_CEILING", error_context="Hard profit-ceiling SELL",
        )

    async def emergency_liquidate_all(
        self, *, order_books: dict[str, OrderBookSnapshot], btc_regime: RegimeAssessment
    ) -> list[str]:
        """Market-sells every open position immediately, regardless of
        current price/target - the explicit, separately-configured action
        `EMERGENCY_AUTO_SELL=true` promises (RiskManager.trigger_emergency_stop
        itself never sells anything on its own). Returns the symbols that
        failed to liquidate so the caller can alert on them specifically.
        """
        async with self._resolution_lock:
            return await self._emergency_liquidate_all_locked(order_books=order_books, btc_regime=btc_regime)

    async def _emergency_liquidate_all_locked(
        self, *, order_books: dict[str, OrderBookSnapshot], btc_regime: RegimeAssessment
    ) -> list[str]:
        with session_scope() as session:
            open_positions = [(p.id, p.symbol) for p in PositionRepository(session).get_open_positions()]

        failed: list[str] = []
        for position_id, symbol in open_positions:
            order_book = order_books.get(symbol)
            if order_book is None or order_book.is_empty:
                failed.append(symbol)
                await self._notifier.on_error(f"Emergency liquidation for {symbol} skipped: no order book available")
                continue
            try:
                liquidated, _detail = await self._force_sell_position(
                    position_id, symbol, order_book=order_book, btc_regime=btc_regime,
                    purpose=OrderPurpose.EMERGENCY_SELL, label="Emergency liquidation",
                )
            except Exception as exc:  # noqa: BLE001 - one coin's failure must not stop the kill switch selling the rest
                logger.exception("Emergency liquidation for %s failed: %r", symbol, exc)
                await self._notifier.on_error(f"Emergency liquidation for {symbol} failed: {exc!r} - check Binance manually")
                liquidated = False
            if not liquidated:
                failed.append(symbol)
        return failed

    async def manual_sell(
        self, symbol: str, *, order_book: OrderBookSnapshot | None, btc_regime: RegimeAssessment
    ) -> ManualSellResult:
        """The owner's /sell: market-sell the whole open position in `symbol`
        now, through the same cancel-then-sell path as the kill switch. The
        result carries the reason in plain words, so the Telegram reply never
        depends on the de-duplicated error alerts."""
        async with self._resolution_lock:  # see __init__: same interleaving rules as /emergency_stop
            with session_scope() as session:
                position = PositionRepository(session).get_open_position_for_symbol(symbol)
                position_id = position.id if position is not None else None
            if position_id is None:
                return ManualSellResult("no_position")
            if order_book is None or order_book.is_empty:
                return ManualSellResult("no_order_book")
            try:
                sold, detail = await self._force_sell_position(
                    position_id, symbol, order_book=order_book, btc_regime=btc_regime,
                    purpose=OrderPurpose.MANUAL_SELL, label="Manual sell",
                )
            except Exception as exc:  # noqa: BLE001 - report, never crash the Telegram handler
                logger.exception("Manual sell for %s failed: %r", symbol, exc)
                await self._notifier.on_error(f"Manual sell for {symbol} failed: {exc!r} - check Binance manually")
                return ManualSellResult("failed", f"помилка: {exc!r}")
            with session_scope() as session:
                left = PositionRepository(session).get(position_id)
                remaining = left.total_quantity if left is not None and left.status == PositionStatus.OPEN else None
            if remaining is None:
                return ManualSellResult("sold" if sold else "sold_with_warning", detail)
            if detail == _PARTIAL_FILL:
                return ManualSellResult("partial", detail, remaining=remaining)
            return ManualSellResult("failed", detail, remaining=remaining)

    async def _force_sell_position(
        self, position_id: int, symbol: str, *, order_book: OrderBookSnapshot, btc_regime: RegimeAssessment,
        purpose: OrderPurpose, label: str,
    ) -> tuple[bool, str | None]:
        """Market-sells one whole position (emergency liquidation or the
        owner's /sell). Returns (cleanly sold, reason in plain Ukrainian when
        not). Only a position that ended up CLOSED counts as sold: a MARKET
        sell that Binance cut short (EXPIRED after a partial fill on a thin
        book) leaves the rest open and must not be reported as done."""
        # Runs in the Telegram handler's task, concurrently with the
        # position monitor: held from the cancel through the sell so
        # manage_position can't read the same OPEN quantity and sell it
        # too. If it is mid-call, this waits for it and then re-reads
        # below whatever it left (possibly already closed).
        async with self._position_lock(position_id):
            # Cancel (and fold in the fill of) anything still resting first,
            # so the quantity read below is accurate and the force-sell
            # can't be rejected/oversized against what Binance actually holds.
            unconfirmed = await self._cancel_resting_orders_for_position(position_id, symbol, btc_regime=btc_regime)
            if any(o.side == OrderSide.SELL for o in unconfirmed):
                await self._notifier.on_error(
                    f"{label} for {symbol} skipped: a resting SELL could not be confirmed cancelled"
                )
                return False, ("на біржі висить ордер на продаж цієї монети, і його не вдалося скасувати - "
                               "нічого не продано; скасуй його на Binance і повтори")
            clean, detail = True, None
            if unconfirmed:
                # The sell below still goes ahead, but a live DCA BUY
                # could refill the position after it: the kill switch
                # must not report this symbol as cleanly liquidated.
                clean = False
                detail = "ордер докупки (DCA) не вдалося скасувати - скасуй його на Binance вручну"
                await self._notifier.on_error(
                    f"{label} for {symbol}: DCA order(s) "
                    f"{', '.join(o.client_order_id for o in unconfirmed)} could not be confirmed cancelled - "
                    "cancel them on Binance manually"
                )
            with session_scope() as session:
                position = PositionRepository(session).get(position_id)
                if position is None or position.status != PositionStatus.OPEN:
                    return clean, detail  # a cancelled order's own fill already closed it
                quantity = position.total_quantity
            if quantity <= 0:
                return clean, detail
            errors: list[str] = []
            outcome = await self._submit_and_apply_sell(
                position_id, symbol=symbol, quantity=quantity, reference_price=order_book.mid_price,
                spread_percent=Decimal("0"),  # force MARKET: a forced sell must not wait in a resting LIMIT order
                purpose=purpose, reason=_EXIT_CLOSE_REASON[purpose], error_context=f"{label} SELL", error_sink=errors,
            )
        if outcome is None:
            if errors:
                return False, f"біржа відхилила продаж: {errors[0]}"
            # Accepted but not confirmed filled yet (e.g. the response was lost):
            # the order poll resolves it and closes the position then.
            return False, "біржа ще не підтвердила продаж - результат буде за хвилину-дві, перевір /positions"
        if not outcome[1]:
            await self._notifier.on_error(f"{label} for {symbol}: Binance filled only part of the MARKET sell")
            return False, _PARTIAL_FILL
        return clean, detail

    # ------------------------------------------------------------------
    # Delayed resolution of LIMIT orders that were resting at submit time
    # ------------------------------------------------------------------

    async def process_resolved_orders(self, *, btc_regime: RegimeAssessment) -> None:
        """Polls `ExecutionEngine.check_pending_limit_orders()` for LIMIT
        orders that have just resolved (filled, cancelled, rejected, or
        timed out) and finishes whatever their purpose implies. Without
        this, a resting LIMIT order that later fills has no code path that
        ever turns it into tracked Position state: capital would move on
        the exchange with zero visibility here. Must be polled periodically
        by the caller (see `orchestration.runtime`) - it does nothing on
        its own.

        The exchange calls run outside `_resolution_lock`, so a slow poll
        never holds up /emergency_stop. Only the commit + apply of each
        result is locked. If a force-close cancelled and applied the same
        order meanwhile, `commit_resolution` sees the row already resolved
        and the result is dropped rather than applied twice.
        """
        resolved = await self._execution_engine.poll_pending_limit_orders()
        for _symbol, client_order_id, result in resolved:
            async with self._resolution_lock:  # see __init__: commit-then-apply must not interleave with /emergency_stop
                try:
                    committed = self._execution_engine.commit_resolution(client_order_id, result)
                except Exception as exc:  # noqa: BLE001 - still tracked, so the next poll retries it
                    logger.exception("Failed to record resolved order %s: %r - retrying next poll", client_order_id, exc)
                    continue
                if not committed:
                    continue
                with session_scope() as session:
                    order = OrderRepository(session).get_by_client_id(client_order_id)
                if order is None:
                    continue
                await self.apply_resolved_order(order, result, btc_regime=btc_regime)

    async def apply_resolved_order(self, order: Order, result: ExecutionResult, *, btc_regime: RegimeAssessment) -> None:
        """Turns one resolved order (from the polling loop above, from
        `_cancel_resting_orders_for_position`, or from startup reconciliation
        - see `orchestration.reconciliation.reconcile_live`) into the
        matching Position update, dispatching by `order.purpose` exactly
        like the immediate-fill call sites do."""
        if result.fill_data_incomplete:
            await self._notifier.on_error(
                f"{order.symbol} order {order.client_order_id} ({order.purpose.value}) resolved as "
                f"{result.status.value} but the real fill breakdown could not be confirmed - the position's "
                "tracked quantity may be understated. Check Binance manually."
            )
        try:
            if order.purpose == OrderPurpose.ENTRY:
                await self._resolve_entry_order(order, result, btc_regime)
            elif order.purpose in _DCA_LEVEL_BY_PURPOSE:
                await self._resolve_dca_order(order, result)
            elif order.purpose in _EXIT_CLOSE_REASON:
                await self._resolve_exit_order(order, result)
            else:
                # Every OrderPurpose has a branch today; one added later without
                # a branch here would otherwise resolve silently while its fill
                # moved real money and no position was updated.
                logger.error(
                    "Resolved order %s for %s has unhandled purpose %s - no position updated",
                    order.client_order_id, order.symbol, order.purpose,
                )
                await self._notifier.on_error(
                    f"{order.symbol} order {order.client_order_id} resolved as {result.status.value} with an "
                    f"unhandled purpose ({order.purpose.value}) - the bot did NOT update any position from it. "
                    "Check Binance manually."
                )
        except Exception as exc:  # noqa: BLE001 - one bad resolution must not block the rest
            logger.exception("Failed to resolve order %s (%s) for %s: %r", order.client_order_id, order.purpose, order.symbol, exc)
            await self._notifier.on_error(f"Failed to finalize resolved order for {order.symbol}: {exc!r}")

    async def _resolve_entry_order(self, order: Order, result: ExecutionResult, btc_regime: RegimeAssessment) -> None:
        if result.net_base_quantity <= 0:
            logger.info("Entry LIMIT order for %s resolved with no fill (%s) - nothing to track", order.symbol, order.status)
            return
        with session_scope() as session:
            already_open = PositionRepository(session).get_open_position_for_symbol(order.symbol)
        if already_open is not None:
            logger.warning(
                "Entry LIMIT order for %s filled late but a position already exists (id=%s) - skipping duplicate",
                order.symbol, already_open.id,
            )
            # Skipping keeps the open position's accounting intact, but these
            # coins were really bought and nothing will ever sell them.
            await self._notifier.on_error(
                f"Entry order {order.client_order_id} for {order.symbol} filled {result.net_base_quantity} late, but "
                f"position #{already_open.id} is already open - the late fill was NOT added to it, so these coins "
                "are NOT tracked by the bot. Check Binance and handle them manually."
            )
            return

        position_id, target_price = await self._apply_entry_fill(
            order.symbol, result=result, btc_regime=btc_regime, order_id=order.id,
            entry_score=None, entry_signals=None,  # the decision that placed it is long gone
        )
        await self._notifier.on_delayed_fill(
            DelayedFillEvent(
                symbol=order.symbol, side="BUY", price=result.avg_fill_price, quantity=result.net_base_quantity,
                usdt_amount=result.filled_quote, purpose="ENTRY", position_id=position_id,
            )
        )

    async def _resolve_dca_order(self, order: Order, result: ExecutionResult) -> None:
        if result.net_base_quantity <= 0:
            logger.info("DCA LIMIT order for %s resolved with no fill - nothing to apply", order.symbol)
            return
        if order.position_id is None:
            # Never expected (a DCA order is always placed for a position),
            # but real coins were bought - never drop that silently.
            await self._notifier.on_error(
                f"DCA order {order.client_order_id} for {order.symbol} filled {result.net_base_quantity} but is not "
                "linked to any position - these coins are NOT tracked by the bot. Check Binance and handle them manually."
            )
            return
        level_index = _DCA_LEVEL_BY_PURPOSE[order.purpose]
        await self._apply_dca_fill(order.position_id, symbol=order.symbol, level_index=level_index, result=result)

    async def _resolve_exit_order(self, order: Order, result: ExecutionResult) -> None:
        if result.filled_quantity <= 0 or order.position_id is None:
            logger.info("Exit LIMIT order for %s resolved with no fill - position remains open, unchanged", order.symbol)
            return
        reason = _EXIT_CLOSE_REASON[order.purpose]
        outcome = await self._apply_sell_result(order.position_id, result=result, reason=reason)
        if outcome is not None and not outcome[1]:
            if order.purpose == OrderPurpose.TAKE_PROFIT and self._settings.use_trailing_after_tp:
                # Mirrors manage_position's immediate-fill partial-TP path
                # (USE_TRAILING_AFTER_TP branch): a partial exit that didn't
                # fully close the position must arm trailing on the remainder
                # too, or the remainder is left with trailing_active=False and
                # manage_position keeps re-submitting a fresh partial-close
                # order against a shrinking remainder every tick instead of
                # trailing it - silently breaking USE_TRAILING_AFTER_TP whenever
                # the partial TP happens to rest as a LIMIT order first. Uses
                # the fill price as the trailing peak since there is no "current
                # price" available this long after the fact.
                with session_scope() as session:
                    p = PositionRepository(session).get(order.position_id)
                    if p is not None and p.status == PositionStatus.OPEN:
                        PositionRepository(session).set_trailing(p, active=True, peak_price=result.avg_fill_price)
            # Every other partial exit leaves the trailing state as it was.
            # Re-arming after a partial TRAILING_STOP reset trailing_is_early
            # and lowered the peak to the fill price (an early 1% trail
            # became a 2.5% trail from a lower price); a partial
            # EMERGENCY_SELL/HARD_CEILING is a forced close, not a profit-
            # taking step that hands the remainder over to a trail.
            await self._notifier.on_delayed_fill(
                DelayedFillEvent(
                    symbol=order.symbol, side="SELL", price=result.avg_fill_price, quantity=result.filled_quantity,
                    usdt_amount=result.filled_quote, purpose=f"{reason} (частково)", position_id=order.position_id,
                )
            )
