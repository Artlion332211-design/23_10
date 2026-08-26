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

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from database.models import Base, SchemaVersion
from utils.time import utcnow

logger = logging.getLogger(__name__)

Migration = tuple[int, str, Callable[[Engine], None]]


def _migration_001_initial_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)


MIGRATIONS: list[Migration] = [
    (1, "initial schema (positions/orders/fills/signals/snapshots/news/events/daily_stats/settings)",
     _migration_001_initial_schema),
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
