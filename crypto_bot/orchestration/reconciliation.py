"""Startup reconciliation.

Binance is always the source of truth for what actually happened in LIVE
mode; in PAPER mode the durable Fill ledger plays the same role for the
purely in-memory `PaperAccount`. Either way, a restart must reconcile
local/in-memory state against that source of truth *before* the bot resumes
trading - never trust in-memory or stale DB state alone (project rule: see
`exchange/execution_engine.py`'s write-ahead-order-then-commit discipline,
which exists specifically so this reconciliation has something durable to
recover from after a crash mid-order).

Position-quantity mismatches are only ever logged and alerted, never
auto-corrected or auto-sold: per the project's crash/emergency-stop rules,
any action that touches real capital based on a surprising state must be a
deliberate, explicit, separately-configured decision - not a side effect of
starting up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from config.settings import Settings
from database.models import BotEventLevel, OrderSide, OrderStatus, OrderType
from database.repository import EventRepository, OrderRepository, PositionRepository
from database.session import session_scope
from exchange.binance_client import BinanceClient
from exchange.execution_engine import ExecutionEngine, OrderRequest
from market.market_regime import RegimeAssessment, RegimeLevel
from paper.simulator import PaperBroker, base_asset_of
from strategy.strategy_engine import StrategyEngine

logger = logging.getLogger(__name__)

# A restart can legitimately race a fill by a few dust units (base-asset
# rounding); only flag a mismatch big enough to mean something real changed.
BALANCE_TOLERANCE_FRACTION = Decimal("0.001")

# reconcile_live runs before BotRuntime.initialize() has computed a real BTC
# regime - only used as informational context for a Position's
# market_regime_at_entry field when an ENTRY order turns out to have filled
# while the bot was down; no decision logic ever reads that field back.
_STARTUP_PLACEHOLDER_REGIME = RegimeAssessment(
    level=RegimeLevel.NEUTRAL, score=0.0, reasons=["BTC regime not yet computed at startup"], crash=False
)


@dataclass
class ReconciliationReport:
    mode: str
    resolved_orders: list[str] = field(default_factory=list)
    position_mismatches: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def has_warnings(self) -> bool:
        return bool(self.position_mismatches)


async def reconcile_live(
    client: BinanceClient, execution_engine: ExecutionEngine, strategy_engine: StrategyEngine
) -> ReconciliationReport:
    report = ReconciliationReport(mode="LIVE")

    with session_scope() as session:
        pending_orders = OrderRepository(session).open_orders()

    for order in pending_orders:
        try:
            result = await execution_engine.reconcile_pending_order(order)
            report.resolved_orders.append(order.client_order_id)
            if result.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED) or result.fill_data_incomplete:
                # Still resting, or its fill data couldn't be confirmed this
                # attempt - reconcile_pending_order already re-registered it
                # with check_pending_limit_orders for ongoing polling, so
                # there is nothing to apply to a Position yet.
                continue
            # A genuine terminal resolution discovered only now, while the
            # bot was down, must still be turned into Position state exactly
            # like a delayed fill discovered while running (see
            # StrategyEngine.process_resolved_orders) - otherwise Binance
            # would show real money moved (an ENTRY/DCA fill, or an exit
            # that only partially closed) but the bot would come back up
            # with no Position/DCA/target tracking it at all. This is the
            # crux of "remember open orders across a restart": the Order/
            # Fill rows alone are not enough, the Position has to be
            # created/updated from them too.
            await strategy_engine.apply_resolved_order(order, result, btc_regime=_STARTUP_PLACEHOLDER_REGIME)
        except Exception as exc:  # noqa: BLE001 - one bad order must never abort startup
            logger.error("Failed to reconcile pending order %s: %r", order.client_order_id, exc)
            report.notes.append(f"could not reconcile order {order.client_order_id}: {exc!r}")

    try:
        balances = await client.get_account_balances()
    except Exception as exc:  # noqa: BLE001 - reconciliation must degrade gracefully, never crash startup
        logger.error("Could not fetch Binance balances for reconciliation: %r", exc)
        report.notes.append(f"could not fetch Binance balances: {exc!r}")
        balances = {}

    with session_scope() as session:
        open_positions = [(p.symbol, p.total_quantity) for p in PositionRepository(session).get_open_positions()]

    for symbol, expected_qty in open_positions:
        base_asset = symbol[:-4] if symbol.endswith("USDT") else symbol
        free, locked = balances.get(base_asset, (Decimal("0"), Decimal("0")))
        actual_qty = free + locked
        tolerance = max(expected_qty * BALANCE_TOLERANCE_FRACTION, Decimal("0.00000001"))
        if actual_qty + tolerance < expected_qty:
            msg = (
                f"{symbol}: DB expects {expected_qty} but Binance shows only {actual_qty} "
                "- position may have been sold/withdrawn outside the bot"
            )
            report.position_mismatches.append(msg)
            with session_scope() as session:
                EventRepository(session).log(level=BotEventLevel.CRITICAL, category="reconciliation", message=msg)

    logger.info(
        "LIVE reconciliation: %s pending order(s) resolved, %s position mismatch(es)",
        len(report.resolved_orders), len(report.position_mismatches),
    )
    return report


def reconcile_paper(settings: Settings, broker: PaperBroker, execution_engine: ExecutionEngine) -> ReconciliationReport:
    """Rebuilds `PaperBroker.account` from the durable Fill ledger, and
    re-registers every still-open (zero-fill) LIMIT order so it keeps
    getting polled after the restart.

    A fresh process's `PaperAccount` starts empty; without the balance
    rebuild, restarting with open positions would report a wrong balance
    and - worse - be unable to SELL a position it can no longer see any
    simulated holdings for. The replay mirrors `PaperBroker._fill_at`'s own
    bookkeeping exactly (BUY commission comes out of the base asset
    received, SELL commission out of the USDT proceeds), so it reconstructs
    the exact state a continuously running process would have had.

    Separately, a fresh `PaperBroker._resting` dict also starts empty, so a
    LIMIT order that was still resting (unfilled) when the process stopped
    would otherwise become permanently orphaned: never filled even if price
    later makes it marketable, never timed out/cancelled, its local Order
    row stuck at NEW forever. LIVE doesn't have this problem (Binance
    itself remembers a resting order regardless of the local process), so
    this half is PAPER-only - `reconcile_live` instead asks Binance for
    each open order's real current status.
    """
    report = ReconciliationReport(mode="PAPER")

    usdt_balance = settings.paper_starting_balance_usdt
    holdings: dict[str, Decimal] = {}
    resting_client_ids: list[str] = []

    with session_scope() as session:
        order_repo = OrderRepository(session)
        for order in order_repo.all_with_fills():
            base_asset = base_asset_of(order.symbol)
            for fill in order.fills:
                if order.side == OrderSide.BUY:
                    usdt_balance -= fill.price * fill.quantity
                    fee_in_base = fill.commission if fill.commission_asset == base_asset else Decimal("0")
                    holdings[base_asset] = holdings.get(base_asset, Decimal("0")) + fill.quantity - fee_in_base
                else:
                    fee_in_usdt = fill.commission if fill.commission_asset != base_asset else Decimal("0")
                    usdt_balance += fill.price * fill.quantity - fee_in_usdt
                    holdings[base_asset] = holdings.get(base_asset, Decimal("0")) - fill.quantity

        # PaperBroker never produces a partial fill (see `_fill_at`: every
        # order either fills in full immediately or rests with zero fills),
        # so every still-open order here is a zero-fill resting LIMIT order
        # and its full `requested_qty`/`requested_price` is exactly what was
        # resting - never a stale/partially-consumed quantity.
        for order in order_repo.open_orders():
            if order.type != OrderType.LIMIT or order.requested_price is None:
                continue
            request = OrderRequest(
                symbol=order.symbol, side=order.side, order_type=OrderType.LIMIT,
                client_order_id=order.client_order_id, quantity=order.requested_qty,
                limit_price=order.requested_price,
            )
            broker.restore_resting_order(request)
            execution_engine.restore_pending_limit_order(order)
            resting_client_ids.append(order.client_order_id)

    broker.account.usdt_balance = usdt_balance
    broker.account.holdings = {asset: qty for asset, qty in holdings.items() if qty != 0}
    report.resolved_orders = resting_client_ids
    report.notes.append(
        f"restored paper balance {usdt_balance:.2f} USDT and {len(broker.account.holdings)} holding(s) from fill ledger"
    )
    if resting_client_ids:
        report.notes.append(f"re-registered {len(resting_client_ids)} still-resting paper LIMIT order(s) for polling")
    logger.info("PAPER reconciliation: %s", "; ".join(report.notes))
    return report
