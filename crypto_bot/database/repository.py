"""Repository pattern over a SQLAlchemy `Session`.

Convention used throughout the app: open one `session_scope()` per logical
unit of work, construct the repositories you need against that session, do
all your reads/writes, and only pass plain scalars/dataclasses out of the
`with` block. ORM objects returned by these methods remain readable
(`expire_on_commit=False`) after the session closes, but do not lazy-load
un-fetched relationships on them once detached.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import (
    BotEvent,
    BotEventLevel,
    DailyStat,
    Fill,
    MarketSnapshot,
    News,
    Order,
    OrderPurpose,
    OrderStatus,
    Position,
    PositionStatus,
    Setting,
    Signal,
    SignalDecision,
)
from utils.time import utcnow


class PositionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        symbol: str,
        opened_at: datetime,
        avg_entry_price: Decimal,
        total_quantity: Decimal,
        total_cost_usdt: Decimal,
        target_price: Decimal,
        market_regime_at_entry: str | None = None,
        entry_score: int | None = None,
        entry_signals: dict[str, Any] | None = None,
        fees_paid_usdt: Decimal = Decimal("0"),
    ) -> Position:
        position = Position(
            symbol=symbol,
            status=PositionStatus.OPEN,
            opened_at=opened_at,
            avg_entry_price=avg_entry_price,
            total_quantity=total_quantity,
            total_cost_usdt=total_cost_usdt,
            dca_count=0,
            target_price=target_price,
            trailing_active=False,
            total_sold_cost_usdt=Decimal("0"),
            fees_paid_usdt=fees_paid_usdt,
            market_regime_at_entry=market_regime_at_entry,
            entry_score=entry_score,
            entry_signals=entry_signals,
        )
        self.session.add(position)
        self.session.flush()
        return position

    def get(self, position_id: int) -> Position | None:
        return self.session.get(Position, position_id)

    def get_open_positions(self, symbol: str | None = None) -> list[Position]:
        stmt = select(Position).where(Position.status == PositionStatus.OPEN)
        if symbol:
            stmt = stmt.where(Position.symbol == symbol)
        return list(self.session.scalars(stmt))

    def get_open_position_for_symbol(self, symbol: str) -> Position | None:
        stmt = select(Position).where(Position.status == PositionStatus.OPEN, Position.symbol == symbol)
        return self.session.scalars(stmt).first()

    def count_open(self) -> int:
        stmt = select(func.count()).select_from(Position).where(Position.status == PositionStatus.OPEN)
        return self.session.scalar(stmt) or 0

    def total_open_cost_usdt(self) -> Decimal:
        """Summed in Python, not SQL: `Position.total_cost_usdt` is a
        DecimalString (TEXT-affinity) column, and SQLite's SUM() aggregate
        coerces TEXT operands to floating point before adding them -
        silently reintroducing the exact float-precision drift
        DecimalString exists to prevent. Fetching the (already-parsed-as-
        Decimal-by-DecimalString) values and summing them in Python keeps
        the whole computation on exact Decimal arithmetic throughout."""
        stmt = select(Position.total_cost_usdt).where(Position.status == PositionStatus.OPEN)
        return sum(self.session.scalars(stmt), Decimal("0"))

    def apply_fill_and_recompute(
        self,
        position: Position,
        *,
        fill_price: Decimal,
        fill_qty: Decimal,
        fee_usdt_equivalent: Decimal,
        dca: bool = False,
    ) -> Position:
        """Recompute the weighted-average entry price after a new BUY fill.

        `fill_qty` must already be net of any commission taken in the
        purchased asset itself (see exchange/execution_engine.py), otherwise
        the position would silently think it holds more than it actually
        does.
        """
        new_total_qty = position.total_quantity + fill_qty
        new_total_cost = position.total_cost_usdt + (fill_price * fill_qty)
        position.total_quantity = new_total_qty
        position.total_cost_usdt = new_total_cost
        position.avg_entry_price = (new_total_cost / new_total_qty) if new_total_qty > 0 else Decimal("0")
        position.fees_paid_usdt = position.fees_paid_usdt + fee_usdt_equivalent
        if dca:
            position.dca_count += 1
        self.session.flush()
        return position

    def update_target_price(self, position: Position, target_price: Decimal) -> Position:
        position.target_price = target_price
        self.session.flush()
        return position

    def set_trailing(
        self, position: Position, *, active: bool, peak_price: Decimal | None = None, is_early: bool = False
    ) -> Position:
        position.trailing_active = active
        if peak_price is not None:
            position.trailing_peak_price = peak_price
        if active:
            position.trailing_is_early = is_early
        self.session.flush()
        return position

    def mark_drawdown_alert_sent(self, position: Position, *, level: int) -> Position:
        """One-shot dedup for DRAWDOWN_WARNING_PERCENT_1/2: never re-sent for
        the same position once its flag is set, even across a restart."""
        if level == 20:
            position.drawdown_alert_20_sent = True
        elif level == 30:
            position.drawdown_alert_30_sent = True
        else:
            raise ValueError(f"unsupported drawdown alert level: {level}")
        self.session.flush()
        return position

    def apply_sell_fill(
        self,
        position: Position,
        *,
        sold_quantity: Decimal,
        proceeds_usdt: Decimal,
        now: datetime,
        close_reason: str,
    ) -> tuple[Decimal, bool]:
        """Reduces (or fully closes) a position by a sold quantity, using
        average-cost-basis accounting uniformly whether this is a full
        take-profit, a deliberate partial take-profit slice, a trailing-
        stop exit, or a LIMIT sell that only partially filled before timing
        out - there is exactly one code path for "some quantity was sold",
        not a separate under-tested one for the partial case.

        Realized PnL accumulates across every slice ever sold from this
        position (see `Position.total_sold_cost_usdt`/`realized_pnl_usdt`)
        rather than being computed only once at final close, so a position
        that partially took profit and later fully exits reports its true
        total PnL, not just the last leg.

        `proceeds_usdt` must already be net of this fill's own sell-side
        commission (the caller subtracts it, since that commission is known
        only from the sell's own ExecutionResult). Buy-side commission
        (`fees_paid_usdt`, accumulated from the entry and every DCA fill)
        is handled here instead: it was never folded into avg_entry_price,
        so without this every close would silently ignore what was paid to
        acquire the position, overstating realized PnL. It is allocated
        proportionally to the fraction of the position being sold.

        Returns (this slice's PnL, whether the position is now fully closed).
        """
        fee_fraction = (sold_quantity / position.total_quantity) if position.total_quantity > 0 else Decimal("0")
        cost_basis = position.avg_entry_price * sold_quantity
        fee_share = position.fees_paid_usdt * fee_fraction
        slice_pnl = proceeds_usdt - cost_basis - fee_share

        position.total_quantity = position.total_quantity - sold_quantity
        position.total_cost_usdt = position.total_cost_usdt - cost_basis
        position.fees_paid_usdt = position.fees_paid_usdt - fee_share
        position.total_sold_cost_usdt = position.total_sold_cost_usdt + cost_basis + fee_share
        position.realized_pnl_usdt = (position.realized_pnl_usdt or Decimal("0")) + slice_pnl
        position.realized_pnl_pct = (
            position.realized_pnl_usdt / position.total_sold_cost_usdt * 100
            if position.total_sold_cost_usdt > 0
            else Decimal("0")
        )

        # A "sell everything" request is rounded down to the exchange's lot
        # step size before being sent (see `ExecutionEngine.sell`), so
        # `sold_quantity` routinely comes back a hair under what was truly
        # remaining - a fixed absolute epsilon alone would leave that
        # unsellable rounding dust looking like a still-open position
        # forever. Treat anything under 0.1% of *this slice* as dust too;
        # a genuine partial exit sells a materially larger fraction than
        # that, so it never gets caught by this.
        dust = max(Decimal("0.00000001"), sold_quantity * Decimal("0.001"))
        fully_closed = position.total_quantity <= dust
        if fully_closed:
            position.status = PositionStatus.CLOSED
            position.closed_at = now
            position.close_reason = close_reason
            position.total_quantity = Decimal("0")
            position.total_cost_usdt = Decimal("0")
            position.fees_paid_usdt = Decimal("0")

        self.session.flush()
        return slice_pnl, fully_closed

    def close(
        self,
        position: Position,
        *,
        closed_at: datetime,
        realized_pnl_usdt: Decimal,
        realized_pnl_pct: Decimal,
        close_reason: str,
    ) -> Position:
        position.status = PositionStatus.CLOSED
        position.closed_at = closed_at
        position.realized_pnl_usdt = realized_pnl_usdt
        position.realized_pnl_pct = realized_pnl_pct
        position.close_reason = close_reason
        self.session.flush()
        return position

    def recent_closed(self, limit: int = 20) -> list[Position]:
        stmt = (
            select(Position)
            .where(Position.status == PositionStatus.CLOSED)
            .order_by(Position.closed_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(stmt))

    def closed_between(self, start: datetime, end: datetime) -> list[Position]:
        stmt = select(Position).where(
            Position.status == PositionStatus.CLOSED,
            Position.closed_at >= start,
            Position.closed_at < end,
        )
        return list(self.session.scalars(stmt))


class OrderRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, **kwargs: Any) -> Order:
        order = Order(created_at=utcnow(), updated_at=utcnow(), status=OrderStatus.NEW, **kwargs)
        self.session.add(order)
        self.session.flush()
        return order

    def get(self, order_id: int) -> Order | None:
        return self.session.get(Order, order_id)

    def get_by_client_id(self, client_order_id: str) -> Order | None:
        return self.session.scalar(select(Order).where(Order.client_order_id == client_order_id))

    def get_by_binance_id(self, binance_order_id: str) -> Order | None:
        return self.session.scalar(select(Order).where(Order.binance_order_id == binance_order_id))

    def update_status(
        self, order: Order, status: OrderStatus, *, binance_order_id: str | None = None
    ) -> Order:
        order.status = status
        order.updated_at = utcnow()
        if binance_order_id:
            order.binance_order_id = binance_order_id
        self.session.flush()
        return order

    def set_position(self, order: Order, position_id: int) -> Order:
        """Retroactively links an entry Order to the Position it created -
        needed because an entry order is placed before any Position exists
        (see `strategy_engine.StrategyEngine._apply_entry_fill`), unlike
        DCA/exit orders which already know their position_id at placement
        time."""
        order.position_id = position_id
        self.session.flush()
        return order

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        stmt = select(Order).where(Order.status.in_([OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED]))
        if symbol:
            stmt = stmt.where(Order.symbol == symbol)
        return list(self.session.scalars(stmt))

    def has_resting_order(
        self, *, symbol: str, position_id: int | None = None, purpose: OrderPurpose | None = None
    ) -> bool:
        """True if a NEW/PARTIALLY_FILLED order already exists matching the
        given filters - the guard that keeps `manage_position`/entry
        evaluation from re-submitting a duplicate DCA/exit/entry order on
        every poll tick while the first one is still resting. A LIMIT order
        can rest for up to LIMIT_ORDER_TIMEOUT_SECONDS (90s by default),
        which is longer than POSITION_MONITOR_INTERVAL_SECONDS (60s by
        default) - without this, the same trigger condition being true on
        the next tick submits a second order for the same DCA level/exit
        before the first has had a chance to fill or time out."""
        stmt = select(Order.id).where(
            Order.symbol == symbol, Order.status.in_([OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED])
        )
        if position_id is not None:
            stmt = stmt.where(Order.position_id == position_id)
        if purpose is not None:
            stmt = stmt.where(Order.purpose == purpose)
        return self.session.scalar(stmt) is not None

    def for_position(self, position_id: int) -> list[Order]:
        stmt = select(Order).where(Order.position_id == position_id).order_by(Order.created_at)
        return list(self.session.scalars(stmt))

    def recent(self, limit: int = 20) -> list[Order]:
        stmt = select(Order).order_by(Order.created_at.desc()).limit(limit)
        return list(self.session.scalars(stmt))

    def all_with_fills(self) -> list[Order]:
        """Every order that has at least one recorded fill, oldest first -
        the complete cash-flow ledger for a fresh process to replay (see
        `orchestration.reconciliation.reconcile_paper`). Filtering on
        `Order.status` instead would miss a LIMIT order that partially
        filled before being cancelled - it still moved real (paper) cash."""
        stmt = (
            select(Order)
            .where(Order.id.in_(select(Fill.order_id)))
            .order_by(Order.created_at)
        )
        return list(self.session.scalars(stmt))

    def count_filled_between(self, start: datetime, end: datetime) -> int:
        stmt = (
            select(func.count())
            .select_from(Order)
            .where(Order.status == OrderStatus.FILLED, Order.updated_at >= start, Order.updated_at < end)
        )
        return self.session.scalar(stmt) or 0


class FillRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, **kwargs: Any) -> Fill:
        fill = Fill(**kwargs)
        self.session.add(fill)
        self.session.flush()
        return fill

    def for_order(self, order_id: int) -> list[Fill]:
        stmt = select(Fill).where(Fill.order_id == order_id)
        return list(self.session.scalars(stmt))

    def total_commission_usdt_between(self, start: datetime, end: datetime) -> Decimal:
        """Summed in Python for the same reason as
        `PositionRepository.total_open_cost_usdt` - SQLite's SUM() coerces
        this DecimalString (TEXT-affinity) column to float."""
        stmt = select(Fill.commission_usdt_equivalent).where(Fill.timestamp >= start, Fill.timestamp < end)
        values = (v for v in self.session.scalars(stmt) if v is not None)
        return sum(values, Decimal("0"))


class SignalRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        symbol: str,
        buy_score: int,
        breakdown: dict[str, Any],
        confirmed_categories: list[str],
        decision: SignalDecision,
        reasons: list[str],
        timestamp: datetime | None = None,
    ) -> Signal:
        sig = Signal(
            symbol=symbol,
            timestamp=timestamp or utcnow(),
            buy_score=buy_score,
            breakdown=breakdown,
            confirmed_categories=confirmed_categories,
            decision=decision,
            reasons=reasons,
        )
        self.session.add(sig)
        self.session.flush()
        return sig

    def recent(self, limit: int = 20, symbol: str | None = None) -> list[Signal]:
        stmt = select(Signal).order_by(Signal.timestamp.desc()).limit(limit)
        if symbol:
            stmt = stmt.where(Signal.symbol == symbol)
        return list(self.session.scalars(stmt))

    def top_recent_buys(self, limit: int = 10, since: datetime | None = None) -> list[Signal]:
        stmt = (
            select(Signal)
            .where(Signal.decision.in_([SignalDecision.BUY, SignalDecision.NO_TRADE]))
            .order_by(Signal.buy_score.desc())
            .limit(limit)
        )
        if since:
            stmt = stmt.where(Signal.timestamp >= since)
        return list(self.session.scalars(stmt))


class MarketSnapshotRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(self, *, symbol: str, timeframe: str, open_time: datetime, **fields: Any) -> MarketSnapshot:
        stmt = select(MarketSnapshot).where(
            MarketSnapshot.symbol == symbol,
            MarketSnapshot.timeframe == timeframe,
            MarketSnapshot.open_time == open_time,
        )
        row = self.session.scalar(stmt)
        if row is None:
            row = MarketSnapshot(symbol=symbol, timeframe=timeframe, open_time=open_time, **fields)
            self.session.add(row)
        else:
            for key, value in fields.items():
                setattr(row, key, value)
        self.session.flush()
        return row


class NewsRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def exists(self, dedup_hash: str) -> bool:
        stmt = select(func.count()).select_from(News).where(News.dedup_hash == dedup_hash)
        return (self.session.scalar(stmt) or 0) > 0

    def add(self, **kwargs: Any) -> News:
        item = News(**kwargs)
        self.session.add(item)
        self.session.flush()
        return item

    def recent(self, limit: int = 20) -> list[News]:
        stmt = select(News).order_by(News.published_at.desc()).limit(limit)
        return list(self.session.scalars(stmt))

    def recent_for_symbol(self, symbol: str, since: datetime) -> list[News]:
        stmt = select(News).where(News.published_at >= since).order_by(News.published_at.desc())
        items: Sequence[News] = self.session.scalars(stmt).all()
        return [n for n in items if symbol in (n.symbols or []) or "MARKET" in (n.symbols or [])]

    def recent_critical(self, since: datetime) -> list[News]:
        stmt = (
            select(News)
            .where(News.published_at >= since, News.critical.is_(True))
            .order_by(News.published_at.desc())
        )
        return list(self.session.scalars(stmt))


class EventRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def log(
        self, *, level: BotEventLevel, category: str, message: str, context: dict[str, Any] | None = None
    ) -> BotEvent:
        event = BotEvent(timestamp=utcnow(), level=level, category=category, message=message, context=context)
        self.session.add(event)
        self.session.flush()
        return event

    def recent(self, limit: int = 50, min_level: BotEventLevel | None = None) -> list[BotEvent]:
        stmt = select(BotEvent).order_by(BotEvent.timestamp.desc()).limit(limit)
        events = list(self.session.scalars(stmt))
        if min_level is None:
            return events
        order = {BotEventLevel.INFO: 0, BotEventLevel.WARNING: 1, BotEventLevel.ERROR: 2, BotEventLevel.CRITICAL: 3}
        floor = order[min_level]
        return [e for e in events if order[e.level] >= floor]


class DailyStatRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, date: str) -> DailyStat | None:
        return self.session.scalar(select(DailyStat).where(DailyStat.date == date))

    def upsert(self, date: str, **fields: Any) -> DailyStat:
        row = self.get(date)
        if row is None:
            row = DailyStat(date=date, **fields)
            self.session.add(row)
        else:
            for key, value in fields.items():
                setattr(row, key, value)
        self.session.flush()
        return row

    def recent(self, limit: int = 30) -> list[DailyStat]:
        stmt = select(DailyStat).order_by(DailyStat.date.desc()).limit(limit)
        return list(self.session.scalars(stmt))


class SettingsRepository:
    """Small persisted key/value store for runtime flags that must survive a
    restart: buy_paused, dca_enabled, emergency_stop, consecutive_bad_trades,
    per-day deployed capital counters, etc."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.session.get(Setting, key)
        return row.value if row else default

    def get_bool(self, key: str, default: bool = False) -> bool:
        raw = self.get(key)
        if raw is None:
            return default
        return raw.strip().lower() in ("1", "true", "yes", "on")

    def get_int(self, key: str, default: int = 0) -> int:
        raw = self.get(key)
        return int(raw) if raw is not None else default

    def get_decimal(self, key: str, default: Decimal = Decimal("0")) -> Decimal:
        raw = self.get(key)
        return Decimal(raw) if raw is not None else default

    def set(self, key: str, value: str) -> None:
        row = self.session.get(Setting, key)
        if row is None:
            row = Setting(key=key, value=value, updated_at=utcnow())
            self.session.add(row)
        else:
            row.value = value
            row.updated_at = utcnow()
        self.session.flush()

    def set_bool(self, key: str, value: bool) -> None:
        self.set(key, "true" if value else "false")
