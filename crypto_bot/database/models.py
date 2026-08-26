"""SQLAlchemy 2.0 ORM models.

Binance remains the source of truth for orders/fills/balances (see
`exchange/` and the startup reconciliation service). These tables are how the
bot persists *local* strategy and portfolio state across restarts: open
positions, the order/fill trail behind them, every scored candidate (even
ones that were rejected - useful for "why didn't it buy" analysis), news,
operational events, daily performance, and small runtime flags (settings).

All monetary/quantity columns use the `DecimalString` type below (never
`Numeric` and never float) to avoid precision drift versus Binance's own
decimal amounts - see its docstring for why plain `Numeric` cannot do this
against SQLite.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import JSON, ForeignKey, String, Text, TypeDecorator, UniqueConstraint
from sqlalchemy import DateTime as SADateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class DecimalString(TypeDecorator):
    """Exact `Decimal` round-trip via TEXT storage - never a float.

    SQLite has no native DECIMAL type, and SQLAlchemy's generic `Numeric`
    binds through Python `float` for any dialect where
    `supports_native_decimal` is False (SQLite is one): every value written
    through a plain `Numeric` column here would silently round-trip through
    64-bit IEEE-754 float, which is exactly the precision drift this
    project's money handling is required to never have (see module
    docstring, and `exchange/symbol_filters.py`'s Decimal-only rounding).
    Storing the exact fixed-point string instead - the same non-scientific
    format Binance's own API requires - avoids the float conversion
    entirely, at any number of decimal places.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: object) -> str | None:
        if value is None:
            return None
        return format(value, "f")

    def process_result_value(self, value: str | None, dialect: object) -> Decimal | None:
        if value is None:
            return None
        return Decimal(value)


MONEY = DecimalString()
PERCENT = DecimalString()


class UTCDateTime(TypeDecorator):
    """Always round-trips as a timezone-aware UTC datetime.

    SQLite has no native timezone-aware datetime type: plain
    `DateTime(timezone=True)` silently comes back *naive* on read, which
    breaks arithmetic against `utils.time.utcnow()` (aware) with a
    `TypeError`. Normalizing on both bind and result here fixes it once,
    everywhere, instead of requiring every call site to remember to convert.
    """

    impl = SADateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


DateTime = UTCDateTime  # noqa: N816 - drop-in replacement used by every timestamp column below


class Base(DeclarativeBase):
    pass


class PositionStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class OrderSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, enum.Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, enum.Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class OrderPurpose(str, enum.Enum):
    ENTRY = "ENTRY"
    DCA_1 = "DCA_1"
    DCA_2 = "DCA_2"
    DCA_3 = "DCA_3"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    EMERGENCY_SELL = "EMERGENCY_SELL"


class SignalDecision(str, enum.Enum):
    BUY = "BUY"
    DCA = "DCA"
    NO_TRADE = "NO_TRADE"
    BLOCKED = "BLOCKED"


class BotEventLevel(str, enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[PositionStatus] = mapped_column(
        SAEnum(PositionStatus, native_enum=False, length=10), default=PositionStatus.OPEN, index=True
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    avg_entry_price: Mapped[Decimal] = mapped_column(MONEY)
    total_quantity: Mapped[Decimal] = mapped_column(MONEY)
    total_cost_usdt: Mapped[Decimal] = mapped_column(MONEY)
    dca_count: Mapped[int] = mapped_column(default=0)
    target_price: Mapped[Decimal] = mapped_column(MONEY)
    trailing_active: Mapped[bool] = mapped_column(default=False)
    trailing_peak_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    partial_closed_quantity: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))

    realized_pnl_usdt: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    realized_pnl_pct: Mapped[Decimal | None] = mapped_column(PERCENT, nullable=True)
    fees_paid_usdt: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))

    market_regime_at_entry: Mapped[str | None] = mapped_column(String(20), nullable=True)
    entry_score: Mapped[int | None] = mapped_column(nullable=True)
    entry_signals: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    orders: Mapped[list[Order]] = relationship(back_populates="position", cascade="all, delete-orphan")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    binance_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    side: Mapped[OrderSide] = mapped_column(SAEnum(OrderSide, native_enum=False, length=10))
    type: Mapped[OrderType] = mapped_column(SAEnum(OrderType, native_enum=False, length=10))
    purpose: Mapped[OrderPurpose] = mapped_column(SAEnum(OrderPurpose, native_enum=False, length=20))
    status: Mapped[OrderStatus] = mapped_column(
        SAEnum(OrderStatus, native_enum=False, length=20), default=OrderStatus.NEW
    )

    requested_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    requested_qty: Mapped[Decimal] = mapped_column(MONEY)
    requested_usdt: Mapped[Decimal] = mapped_column(MONEY)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    position: Mapped[Position | None] = relationship(back_populates="orders")
    fills: Mapped[list[Fill]] = relationship(back_populates="order", cascade="all, delete-orphan")


class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    price: Mapped[Decimal] = mapped_column(MONEY)
    quantity: Mapped[Decimal] = mapped_column(MONEY)
    commission: Mapped[Decimal] = mapped_column(MONEY)
    commission_asset: Mapped[str] = mapped_column(String(20))
    commission_usdt_equivalent: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    trade_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    order: Mapped[Order] = relationship(back_populates="fills")


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    buy_score: Mapped[int] = mapped_column()
    breakdown: Mapped[dict] = mapped_column(JSON)
    confirmed_categories: Mapped[list] = mapped_column(JSON)
    decision: Mapped[SignalDecision] = mapped_column(SAEnum(SignalDecision, native_enum=False, length=10))
    reasons: Mapped[list] = mapped_column(JSON)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    __table_args__ = (UniqueConstraint("symbol", "timeframe", "open_time", name="uq_snapshot_bar"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    timeframe: Mapped[str] = mapped_column(String(5), index=True)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[Decimal] = mapped_column(MONEY)
    high: Mapped[Decimal] = mapped_column(MONEY)
    low: Mapped[Decimal] = mapped_column(MONEY)
    close: Mapped[Decimal] = mapped_column(MONEY)
    volume: Mapped[Decimal] = mapped_column(MONEY)
    rsi: Mapped[float | None] = mapped_column(nullable=True)
    macd: Mapped[float | None] = mapped_column(nullable=True)
    macd_signal: Mapped[float | None] = mapped_column(nullable=True)
    macd_hist: Mapped[float | None] = mapped_column(nullable=True)
    ema_fast: Mapped[float | None] = mapped_column(nullable=True)
    ema_mid: Mapped[float | None] = mapped_column(nullable=True)
    ema_slow: Mapped[float | None] = mapped_column(nullable=True)
    bb_upper: Mapped[float | None] = mapped_column(nullable=True)
    bb_mid: Mapped[float | None] = mapped_column(nullable=True)
    bb_lower: Mapped[float | None] = mapped_column(nullable=True)
    atr: Mapped[float | None] = mapped_column(nullable=True)
    adx: Mapped[float | None] = mapped_column(nullable=True)
    vwap: Mapped[float | None] = mapped_column(nullable=True)
    obv: Mapped[float | None] = mapped_column(nullable=True)


class News(Base):
    __tablename__ = "news"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(String(1000))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sentiment_score: Mapped[int] = mapped_column()
    symbols: Mapped[list] = mapped_column(JSON)
    dedup_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    critical: Mapped[bool] = mapped_column(default=False)


class BotEvent(Base):
    __tablename__ = "bot_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    level: Mapped[BotEventLevel] = mapped_column(SAEnum(BotEventLevel, native_enum=False, length=10), index=True)
    category: Mapped[str] = mapped_column(String(50))
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class DailyStat(Base):
    __tablename__ = "daily_stats"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[str] = mapped_column(String(10), unique=True, index=True)  # ISO YYYY-MM-DD, UTC
    starting_balance: Mapped[Decimal] = mapped_column(MONEY)
    ending_balance: Mapped[Decimal] = mapped_column(MONEY)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY)
    unrealized_pnl: Mapped[Decimal] = mapped_column(MONEY)
    trades_count: Mapped[int] = mapped_column(default=0)
    closed_trades_count: Mapped[int] = mapped_column(default=0)
    wins: Mapped[int] = mapped_column(default=0)
    losses: Mapped[int] = mapped_column(default=0)
    win_rate: Mapped[float] = mapped_column(default=0.0)
    fees_paid: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    open_positions_count: Mapped[int] = mapped_column(default=0)
    capital_exposure_pct: Mapped[float] = mapped_column(default=0.0)
    best_trade_symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    best_trade_pct: Mapped[float | None] = mapped_column(nullable=True)
    btc_regime: Mapped[str | None] = mapped_column(String(20), nullable=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SchemaVersion(Base):
    __tablename__ = "schema_version"

    version: Mapped[int] = mapped_column(primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
