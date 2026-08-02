"""Host allowlisting for user-supplied database URLs.

`/connect-database` takes a URL from the caller and makes the server open an
outbound connection to it. Authentication narrows *who* can do that; it does not
stop a logged-in user reaching infrastructure the server can see and they cannot
-- cloud metadata endpoints, internal admin databases, or arbitrary ports for
scanning. Connection errors are echoed back, so responses are a usable oracle.

The mitigation has two layers, both checked before any connection attempt:

1. An allowlist matched on the hostname as written.
2. DNS resolution of that hostname, rejected if any resolved address is a known
   cloud-metadata address (see `_BLOCKED_METADATA_NETWORKS`). This closes the
   specific gap a string match cannot: an allowlisted *name* resolving to
   169.254.169.254 or similar, whether by DNS rebinding or attacker-influenced
   DNS. It deliberately does *not* extend to ordinary private ranges (10/8,
   172.16/12, 192.168/16) or loopback: those are the normal case for a
   self-hosted "internal" database, and this project's own documented example
   (`localhost,127.0.0.1,db,analytics.internal`) depends on them staying
   permitted.

Deliberate limitation, still: resolution happens at check time, not at the
moment the DBAPI driver opens its socket, because SQLAlchemy does not expose a
hook to pin a connection to a specific resolved address. A DNS answer that
changes in the (small) window between this check and the actual connection
would not be caught. Treat this as a coarse boundary, not a complete SSRF
defence.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from backend.utils.config import get_settings

# Addresses with no legitimate use as a database host, regardless of what
# hostname resolves to them -- unlike RFC1918/loopback, which real deployments
# route database traffic over deliberately.
_BLOCKED_METADATA_NETWORKS = [
    ipaddress.ip_network("169.254.0.0/16"),    # AWS/Azure/GCP/DO IMDS (link-local)
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local, same reasoning
    ipaddress.ip_network("fd00:ec2::254/128"), # AWS IMDSv6
    ipaddress.ip_network("100.100.100.200/32"),# Alibaba Cloud metadata
]


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


def _resolve(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve a hostname to its IP addresses. Empty list if resolution fails.

    A failed lookup is not itself an SSRF signal; the connection attempt that
    follows fails on its own and reports an ordinary connection error. Raising
    a different error from this layer for that case would be misleading, so it
    is treated the same as "nothing to flag" here.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return []
    return list({ipaddress.ip_address(info[4][0]) for info in infos})


def is_metadata_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if `ip` falls in a known cloud-metadata range."""
    return any(ip in network for network in _BLOCKED_METADATA_NETWORKS)


def assert_not_metadata_host(database_url: str) -> None:
    """Raise if `database_url`'s host resolves to a cloud-metadata address."""
    host = host_of(database_url)
    if not host:
        return
    for ip in _resolve(host):
        if is_metadata_address(ip):
            raise DatabaseHostNotAllowed(
                f"Host {host!r} resolves to {ip}, a cloud metadata address."
            )


def assert_host_allowed(database_url: str) -> None:
    """Raise DatabaseHostNotAllowed if the URL's host is not permitted."""
    allowlist = parse_allowlist(get_settings().allowed_database_hosts)
    if not is_host_allowed(database_url, allowlist):
        raise DatabaseHostNotAllowed(
            f"Host {host_of(database_url)!r} is not in ALLOWED_DATABASE_HOSTS."
        )
    # Runs even with an empty (permit-any) allowlist: there is no allowlist
    # value that should ever make a metadata endpoint an acceptable database.
    assert_not_metadata_host(database_url)
