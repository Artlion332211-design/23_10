"""Lightweight, dependency-free schema migrations.

This is intentionally not Alembic: for a single-file local SQLite database a
small version-table runner is easier to audit and ships with zero extra
tooling. The moment this project moves to Postgres with a team touching the
schema concurrently, swap this module for Alembic - the ORM models in
`database/models.py` don't need to change, only how they get applied.

Each migration is `(version, description, upgrade_fn)`. `upgrade_fn` receives
the bound Engine and must be additive/idempotent-safe (SQLite's ALTER TABLE
support is limited to ADD COLUMN; anything more invasive means a new table +
copy, which is exactly where Alembic starts paying for itself).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from database.models import Base, SchemaVersion
from utils.time import utcnow

logger = logging.getLogger(__name__)

Migration = tuple[int, str, Callable[[Engine], None]]


def _existing_columns(engine: Engine, table_name: str) -> set[str]:
    """Dialect-portable column check via SQLAlchemy reflection (works on
    SQLite, Postgres, etc.) - a raw `PRAGMA table_info(...)` would silently
    fail (or worse, just error) on anything but SQLite, breaking the
    documented "Postgres is a config change away" invariant every other
    part of this codebase relies on."""
    return {col["name"] for col in inspect(engine).get_columns(table_name)}


def _migration_001_initial_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def _migration_002_trailing_is_early(engine: Engine) -> None:
    """`Base.metadata.create_all` (migration 1) already creates this column
    on any brand-new database, since `Position.trailing_is_early` is part
    of the current model - only a database that already existed *before*
    this field was added is actually missing it. Checked explicitly rather
    than assumed, so this migration stays additive/idempotent-safe (the
    documented contract every migration here must meet) instead of
    colliding with migration 1 on a fresh database."""
    if "trailing_is_early" not in _existing_columns(engine, "positions"):
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE positions ADD COLUMN trailing_is_early BOOLEAN NOT NULL DEFAULT FALSE"))


def _migration_003_drawdown_alerts(engine: Engine) -> None:
    """Same idempotent-if-pre-existing pattern as migration 2, for the two
    DRAWDOWN_WARNING_PERCENT_1/2 dedup columns."""
    existing = _existing_columns(engine, "positions")
    with engine.begin() as conn:
        if "drawdown_alert_20_sent" not in existing:
            conn.execute(text("ALTER TABLE positions ADD COLUMN drawdown_alert_20_sent BOOLEAN NOT NULL DEFAULT FALSE"))
        if "drawdown_alert_30_sent" not in existing:
            conn.execute(text("ALTER TABLE positions ADD COLUMN drawdown_alert_30_sent BOOLEAN NOT NULL DEFAULT FALSE"))


MIGRATIONS: list[Migration] = [
    (1, "initial schema (positions/orders/fills/signals/snapshots/news/events/daily_stats/settings)",
     _migration_001_initial_schema),
    (2, "positions.trailing_is_early (early profit protection)", _migration_002_trailing_is_early),
    (3, "positions.drawdown_alert_20_sent/30_sent (drawdown warnings)", _migration_003_drawdown_alerts),
]


def current_schema_version(engine: Engine) -> int:
    Base.metadata.tables["schema_version"].create(engine, checkfirst=True)
    with Session(engine) as session:
        row = session.execute(select(SchemaVersion.version).order_by(SchemaVersion.version.desc())).first()
        return row[0] if row else 0


def run_migrations(engine: Engine) -> int:
    """Apply any migrations newer than the current schema version. Returns the
    resulting schema version."""
    current = current_schema_version(engine)
    pending = sorted((m for m in MIGRATIONS if m[0] > current), key=lambda m: m[0])
    for version, description, upgrade in pending:
        logger.info("Applying migration %s: %s", version, description)
        upgrade(engine)
        with Session(engine) as session:
            session.add(SchemaVersion(version=version, applied_at=utcnow()))
            session.commit()
        current = version
    if not pending:
        logger.info("Database schema up to date at version %s", current)
    return current
