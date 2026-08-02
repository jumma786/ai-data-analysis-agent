"""Host allowlisting for user-supplied database URLs.

`/connect-database` takes a URL from the caller and makes the server open an
outbound connection to it. Authentication narrows *who* can do that; it does not
stop a logged-in user reaching infrastructure the server can see and they cannot
-- cloud metadata endpoints, internal admin databases, or arbitrary ports for
scanning. Connection errors are echoed back, so responses are a usable oracle.

The mitigation is an allowlist, checked before any connection attempt.

Deliberate limitation: this matches on the hostname as written. It does not
resolve DNS, so it does not defend against a permitted name that resolves to an
internal address, nor against DNS rebinding. Closing those needs resolution plus
a private-range check at connect time, which SQLAlchemy does not expose a clean
hook for. Treat this as a coarse boundary, not a complete SSRF defence.
"""
from __future__ import annotations

from urllib.parse import urlparse

from backend.utils.config import get_settings


class DatabaseHostNotAllowed(ValueError):
    """Raised when a URL's host is not in the configured allowlist."""


def parse_allowlist(raw: str) -> set[str]:
    """Split the comma-separated setting into a normalised set of hosts."""
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def host_of(database_url: str) -> str:
    """Extract the lower-cased hostname from a SQLAlchemy URL.

    Returns "" for URLs with no host, which includes SQLite paths.
    """
    return (urlparse(database_url).hostname or "").lower()


def is_host_allowed(database_url: str, allowlist: set[str]) -> bool:
    """True if the URL may be dialled under this allowlist.

    An empty allowlist permits everything -- the documented development
    default. Hostless URLs (SQLite files) are permitted because they open no
    network connection.
    """
    if not allowlist:
        return True
    host = host_of(database_url)
    if not host:
        return True
    return host in allowlist


def assert_host_allowed(database_url: str) -> None:
    """Raise DatabaseHostNotAllowed if the URL's host is not permitted."""
    allowlist = parse_allowlist(get_settings().allowed_database_hosts)
    if not is_host_allowed(database_url, allowlist):
        raise DatabaseHostNotAllowed(
            f"Host {host_of(database_url)!r} is not in ALLOWED_DATABASE_HOSTS."
        )
