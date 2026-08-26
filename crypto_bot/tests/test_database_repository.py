from __future__ import annotations

from decimal import Decimal

from database.migrations import run_migrations
from database.models import BotEventLevel, OrderPurpose, OrderSide, OrderStatus, OrderType
from database.repository import (
    EventRepository,
    FillRepository,
    OrderRepository,
    PositionRepository,
    SettingsRepository,
)
from database.session import init_engine, reset_for_tests, session_scope
from utils.time import utcnow


def test_position_survives_process_restart(db_engine, tmp_path):
    """Simulates a bot restart: drop the in-memory engine/session factory,
    then re-open the same on-disk SQLite file and confirm everything the
    strategy needs to resume is still there."""
    db_path = tmp_path / "test.db"

    with session_scope() as session:
        position = PositionRepository(session).create(
            symbol="SOLUSDT", opened_at=utcnow(), avg_entry_price=Decimal("142.53"),
            total_quantity=Decimal("0.7"), total_cost_usdt=Decimal("99.771"),
            target_price=Decimal("156.78"), market_regime_at_entry="NEUTRAL", entry_score=81,
            entry_signals={"rsi_reversal": True},
        )
        position_id = position.id
        order = OrderRepository(session).create(
            position_id=position.id, symbol="SOLUSDT", client_order_id="cid-1",
            side=OrderSide.BUY, type=OrderType.MARKET, purpose=OrderPurpose.ENTRY,
            requested_price=None, requested_qty=Decimal("0.7"), requested_usdt=Decimal("100"),
        )
        FillRepository(session).add(
            order_id=order.id, price=Decimal("142.53"), quantity=Decimal("0.7"),
            commission=Decimal("0.0007"), commission_asset="SOL", timestamp=utcnow(),
        )
        SettingsRepository(session).set_bool("buy_paused", False)
        EventRepository(session).log(level=BotEventLevel.INFO, category="startup", message="bot started")

    # --- simulate restart ---
    reset_for_tests()
    engine = init_engine(f"sqlite:///{db_path}")
    run_migrations(engine)

    with session_scope() as session:
        open_positions = PositionRepository(session).get_open_positions()
        assert len(open_positions) == 1
        assert open_positions[0].id == position_id
        assert open_positions[0].avg_entry_price == Decimal("142.53")
        assert open_positions[0].entry_signals == {"rsi_reversal": True}

        assert SettingsRepository(session).get_bool("buy_paused", True) is False

        orders = OrderRepository(session).for_position(position_id)
        assert len(orders) == 1
        fills = FillRepository(session).for_order(orders[0].id)
        assert len(fills) == 1
        assert fills[0].commission_asset == "SOL"

        events = EventRepository(session).recent(5)
        assert any(e.category == "startup" for e in events)


def test_apply_fill_and_recompute_weighted_average(db_engine):
    with session_scope() as session:
        repo = PositionRepository(session)
        position = repo.create(
            symbol="SOLUSDT", opened_at=utcnow(), avg_entry_price=Decimal("100"),
            total_quantity=Decimal("1"), total_cost_usdt=Decimal("100"), target_price=Decimal("110"),
        )
        repo.apply_fill_and_recompute(
            position, fill_price=Decimal("90"), fill_qty=Decimal("1"), fee_usdt_equivalent=Decimal("0.1"), dca=True
        )
        assert position.total_quantity == Decimal("2")
        assert position.avg_entry_price == Decimal("95")  # (100*1 + 90*1) / 2
        assert position.dca_count == 1
        assert position.fees_paid_usdt == Decimal("0.1")


def test_settings_repository_typed_getters(db_engine):
    with session_scope() as session:
        repo = SettingsRepository(session)
        repo.set_bool("flag", True)
        repo.set("count", "3")
        repo.set("amount", "12.5")
        assert repo.get_bool("flag") is True
        assert repo.get_int("count") == 3
        assert repo.get_decimal("amount") == Decimal("12.5")
        assert repo.get_bool("missing_flag", default=True) is True


def test_order_status_lifecycle(db_engine):
    with session_scope() as session:
        order_repo = OrderRepository(session)
        order = order_repo.create(
            position_id=None, symbol="BTCUSDT", client_order_id="abc123",
            side=OrderSide.BUY, type=OrderType.LIMIT, purpose=OrderPurpose.ENTRY,
            requested_price=Decimal("60000"), requested_qty=Decimal("0.001"), requested_usdt=Decimal("60"),
        )
        assert order.status == OrderStatus.NEW
        order_repo.update_status(order, OrderStatus.FILLED, binance_order_id="99")
        assert order.status == OrderStatus.FILLED
        assert order.binance_order_id == "99"

        fetched = order_repo.get_by_client_id("abc123")
        assert fetched is not None
        assert fetched.id == order.id
