"""Engine / session factory.

SQLite today, but every call site only ever uses the SQLAlchemy Core/ORM
API (no raw SQLite-specific SQL), so pointing `DATABASE_URL` at Postgres
later is a config change, not a rewrite.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def init_engine(database_url: str, *, echo: bool = False) -> Engine:
    global _engine, _session_factory

    if database_url.startswith("sqlite"):
        # e.g. sqlite:///./data/crypto_bot.db -> make sure ./data exists.
        path_part = database_url.split("///", 1)[-1]
        if path_part and path_part != ":memory:":
            Path(path_part).resolve().parent.mkdir(parents=True, exist_ok=True)

    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    from sqlalchemy import create_engine

    engine = create_engine(database_url, connect_args=connect_args, echo=echo, future=True)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    _engine = engine
    _session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database engine not initialized - call init_engine() first")
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commits on success, rolls back on any exception.

    `expire_on_commit=False` so ORM objects created/read inside the block
    remain usable (scalar attributes only) after it exits.
    """
    if _session_factory is None:
        raise RuntimeError("Database engine not initialized - call init_engine() first")
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_for_tests() -> None:
    """Drop the cached engine/session factory. Test-only helper."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
