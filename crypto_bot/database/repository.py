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
            partial_closed_quantity=Decimal("0"),
            fees_paid_usdt=Decimal("0"),
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
        stmt = select(func.coalesce(func.sum(Position.total_cost_usdt), 0)).where(
            Position.status == PositionStatus.OPEN
        )
        total = self.session.scalar(stmt)
        return Decimal(str(total or 0))

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

    def set_trailing(self, position: Position, *, active: bool, peak_price: Decimal | None = None) -> Position:
        position.trailing_active = active
        if peak_price is not None:
            position.trailing_peak_price = peak_price
        self.session.flush()
        return position

    def record_partial_close(self, position: Position, quantity_closed: Decimal) -> Position:
        position.partial_closed_quantity = position.partial_closed_quantity + quantity_closed
        self.session.flush()
        return position

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

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        stmt = select(Order).where(Order.status.in_([OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED]))
        if symbol:
            stmt = stmt.where(Order.symbol == symbol)
        return list(self.session.scalars(stmt))

    def for_position(self, position_id: int) -> list[Order]:
        stmt = select(Order).where(Order.position_id == position_id).order_by(Order.created_at)
        return list(self.session.scalars(stmt))

    def recent(self, limit: int = 20) -> list[Order]:
        stmt = select(Order).order_by(Order.created_at.desc()).limit(limit)
        return list(self.session.scalars(stmt))


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
