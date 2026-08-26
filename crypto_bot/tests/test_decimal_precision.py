from __future__ import annotations

from decimal import Decimal

from database.models import Position, PositionStatus
from database.session import session_scope
from utils.time import utcnow


def test_decimal_column_round_trips_high_precision_value_exactly(db_engine):
    """`Position.total_quantity` (and every other MONEY/PERCENT column) must
    never round-trip through a 64-bit float: SQLAlchemy's generic `Numeric`
    silently does exactly that against SQLite (`supports_native_decimal` is
    False, so its bind_processor is `to_float`), which is why
    `database.models.DecimalString` stores the exact fixed-point string
    instead. This value has 28 significant digits specifically to make any
    float round-trip through this column detectable - float64 only carries
    about 15-17 significant decimal digits.
    """
    tricky = Decimal("1.497751124437781109445277361")

    with session_scope() as session:
        position = Position(
            symbol="SOLUSDT", status=PositionStatus.OPEN, opened_at=utcnow(),
            avg_entry_price=Decimal("100"), total_quantity=tricky, total_cost_usdt=Decimal("100"),
            dca_count=0, target_price=Decimal("110"), trailing_active=False,
            partial_closed_quantity=Decimal("0"), fees_paid_usdt=Decimal("0"),
        )
        session.add(position)
        session.flush()
        position_id = position.id

    # A fresh session forces an actual read from SQLite, not merely
    # in-memory identity from before the flush.
    with session_scope() as session:
        reloaded = session.get(Position, position_id)
        assert reloaded is not None
        assert reloaded.total_quantity == tricky
        assert str(reloaded.total_quantity) == str(tricky)
