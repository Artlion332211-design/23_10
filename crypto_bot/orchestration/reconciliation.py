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
from database.models import BotEventLevel, OrderSide
from database.repository import EventRepository, OrderRepository, PositionRepository
from database.session import session_scope
from exchange.binance_client import BinanceClient
from exchange.execution_engine import ExecutionEngine
from paper.simulator import PaperBroker, base_asset_of

logger = logging.getLogger(__name__)

# A restart can legitimately race a fill by a few dust units (base-asset
# rounding); only flag a mismatch big enough to mean something real changed.
BALANCE_TOLERANCE_FRACTION = Decimal("0.001")


@dataclass
class ReconciliationReport:
    mode: str
    resolved_orders: list[str] = field(default_factory=list)
    position_mismatches: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def has_warnings(self) -> bool:
        return bool(self.position_mismatches)


async def reconcile_live(client: BinanceClient, execution_engine: ExecutionEngine) -> ReconciliationReport:
    report = ReconciliationReport(mode="LIVE")

    with session_scope() as session:
        pending_orders = OrderRepository(session).open_orders()
        for order in pending_orders:
            try:
                await execution_engine.reconcile_pending_order(order)
                report.resolved_orders.append(order.client_order_id)
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


def reconcile_paper(settings: Settings, broker: PaperBroker) -> ReconciliationReport:
    """Rebuilds `PaperBroker.account` from the durable Fill ledger.

    A fresh process's `PaperAccount` starts empty; without this, restarting
    with open positions would report a wrong balance and - worse - be unable
    to SELL a position it can no longer see any simulated holdings for. The
    replay mirrors `PaperBroker._fill_at`'s own bookkeeping exactly (BUY
    commission comes out of the base asset received, SELL commission out of
    the USDT proceeds), so it reconstructs the exact state a continuously
    running process would have had.
    """
    report = ReconciliationReport(mode="PAPER")

    usdt_balance = settings.paper_starting_balance_usdt
    holdings: dict[str, Decimal] = {}

    with session_scope() as session:
        orders = OrderRepository(session).all_with_fills()
        for order in orders:
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

    broker.account.usdt_balance = usdt_balance
    broker.account.holdings = {asset: qty for asset, qty in holdings.items() if qty != 0}
    report.notes.append(
        f"restored paper balance {usdt_balance:.2f} USDT and {len(broker.account.holdings)} holding(s) from fill ledger"
    )
    logger.info("PAPER reconciliation: %s", report.notes[-1])
    return report
