"""Analytics-database engine construction with an enforced statement timeout.

A generated query that plans badly can otherwise pin a worker until the HTTP
client gives up, and abandoning the request does not stop the database doing the
work. The only reliable place to cap that is the database itself, so the timeout
is pushed down as a driver option rather than wrapped around the Python call.

Support is per-dialect, and deliberately explicit rather than best-effort:
`timeout_connect_args` returns what a dialect accepts, and nothing otherwise, so
callers can tell the difference between "capped" and "not capped".
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from sqlalchemy import Engine, create_engine

from backend.utils.config import get_settings
from backend.utils.logging_config import logger


def dialect_of(database_url: str) -> str:
    """Return the SQLAlchemy dialect name, e.g. 'postgresql' from a URL."""
    scheme = urlparse(database_url).scheme
    return scheme.split("+", 1)[0].lower()


def timeout_connect_args(database_url: str, timeout_seconds: int) -> dict[str, Any]:
    """Driver kwargs that enforce a server-side statement timeout.

    Returns an empty dict for dialects with no equivalent, so nothing is
    silently assumed to be capped.

    * PostgreSQL -- `statement_timeout` in milliseconds, applied per connection.
    * MySQL / MariaDB -- `max_execution_time`, also milliseconds (SELECT only).
    * SQLite -- `timeout` is a *lock-wait*, not a statement cap; SQLite has no
      server-side statement timeout, so we return nothing rather than pretend.
    """
    if timeout_seconds <= 0:
        return {}
    dialect = dialect_of(database_url)
    millis = timeout_seconds * 1000
    if dialect == "postgresql":
        return {"options": f"-c statement_timeout={millis}"}
    if dialect in {"mysql", "mariadb"}:
        return {"init_command": f"SET SESSION max_execution_time={millis}"}
    return {}


def create_analytics_engine(database_url: str | None = None,
                            timeout_seconds: int | None = None) -> Engine:
    """Build an engine for the analytics database with the timeout applied."""
    settings = get_settings()
    url = database_url or settings.database_url
    timeout = (settings.query_timeout_seconds if timeout_seconds is None
               else timeout_seconds)

    connect_args = timeout_connect_args(url, timeout)
    if not connect_args and timeout > 0:
        logger.warning(
            "No statement-timeout mechanism for dialect %r; queries against "
            "this database are NOT capped at %ss.", dialect_of(url), timeout)

    return create_engine(url, connect_args=connect_args, pool_pre_ping=True)
