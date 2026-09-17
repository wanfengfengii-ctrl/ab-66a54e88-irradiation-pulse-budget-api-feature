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


def make_engine(database_url: str, *, begin_immediate: bool = True) -> Engine:
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

    if begin_immediate:

        @event.listens_for(engine, "begin")
        def _begin_immediate(connection):
            # Acquire the write lock when the transaction starts: concurrent
            # writers then queue on the busy timeout instead of failing with
            # mid-transaction lock-upgrade deadlocks.
            connection.exec_driver_sql("BEGIN IMMEDIATE")

    # Without the listener above transactions start as plain BEGIN (DEFERRED):
    # they only take a read lock on the first SELECT, so under WAL read-only
    # sessions never block pulse deductions.
    return engine


def make_engines(database_url: str) -> tuple[Engine, Engine]:
    """Return (write_engine, read_engine) for `database_url`.

    The write engine opens every transaction with BEGIN IMMEDIATE; the read
    engine uses deferred transactions so audit/listing queries never hold the
    write lock. An in-memory database is a single shared connection, so both
    factories share one engine there.
    """
    write_engine = make_engine(database_url)
    if _sqlite_path(database_url) is None:
        return write_engine, write_engine
    return write_engine, make_engine(database_url, begin_immediate=False)


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
