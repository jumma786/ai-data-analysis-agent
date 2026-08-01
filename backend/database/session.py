"""Engine / session management for the application metadata database.

This is the database that holds users, datasets, conversations and reports --
*not* the analytics database the agent queries on the user's behalf. Keeping the
two separate is what lets the analytics connection use a read-only role.

The engine is created lazily so importing this module never opens a connection;
tests replace the `get_db` dependency with their own in-memory SQLite session.
"""
from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database.models import Base
from backend.utils.config import get_settings
from backend.utils.logging_config import logger


@lru_cache
def get_engine() -> Engine:
    """Return the process-wide metadata engine, creating it on first use."""
    url = get_settings().metadata_database_url
    # SQLite's default thread check rejects connections reused across FastAPI's
    # threadpool workers; other drivers do not need (or accept) the argument.
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    logger.info("Creating metadata engine for %s", url.split("://", 1)[0])
    return create_engine(url, connect_args=connect_args, future=True)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    """Return the process-wide sessionmaker bound to the metadata engine."""
    return sessionmaker(bind=get_engine(), autocommit=False, autoflush=False)


def init_db() -> None:
    """Create metadata tables if they do not exist.

    Adequate for a single-node demo. A real deployment should use Alembic
    migrations instead, so schema changes are versioned and reversible.
    """
    Base.metadata.create_all(bind=get_engine())
    logger.info("Metadata tables ensured.")


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a metadata session, closed after the request."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
