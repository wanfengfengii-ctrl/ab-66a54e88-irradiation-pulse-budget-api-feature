"""Engine and session factory setup, tuned for concurrent SQLite writers."""
from __future__ import annotations

import os

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

_BUSY_TIMEOUT_MS = 30_000


def _sqlite_path(database_url: str) -> str | None:
    """Return the file path of a sqlite URL, or None for in-memory databases."""
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return None
    path = database_url[len(prefix):]
    if path in ("", ":memory:") or path.startswith("file:"):
        return None
    return path


def make_engine(database_url: str) -> Engine:
    if not database_url.startswith("sqlite"):
        raise ValueError(f"only sqlite URLs are supported, got: {database_url!r}")

    path = _sqlite_path(database_url)
    if path:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)

    kwargs: dict = {"connect_args": {"check_same_thread": False, "timeout": _BUSY_TIMEOUT_MS / 1000}}
    if path is None:
        # In-memory database: share a single connection so every session sees
        # the same data.
        kwargs["poolclass"] = StaticPool
    engine = create_engine(database_url, **kwargs)

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _begin_immediate(connection):
        # Acquire the write lock when the transaction starts: concurrent
        # writers then queue on the busy timeout instead of failing with
        # mid-transaction lock-upgrade deadlocks.
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
