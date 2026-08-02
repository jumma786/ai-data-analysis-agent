"""Tests for the security and robustness work: SSRF allowlist, query timeout,
login rate limiting, per-user scoping, and optional chart rendering.

All offline: no database beyond in-memory SQLite, no network, no LLM.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.services.db import dialect_of, timeout_connect_args
from backend.services.report import render_chart_png
from backend.services.url_guard import (
    DatabaseHostNotAllowed, host_of, is_host_allowed, parse_allowlist)
from backend.utils.rate_limit import SlidingWindowRateLimiter


# --- SSRF host allowlist --------------------------------------------------

def test_parse_allowlist_trims_and_lowercases():
    assert parse_allowlist(" DB , localhost ,, 10.0.0.5 ") == {
        "db", "localhost", "10.0.0.5"}


def test_host_of_extracts_hostname():
    assert host_of("postgresql+psycopg2://u:p@db.internal:5432/x") == "db.internal"


def test_host_of_is_empty_for_sqlite():
    assert host_of("sqlite:///./local.db") == ""


def test_empty_allowlist_permits_everything():
    """Documented development default: no allowlist configured means no limit."""
    assert is_host_allowed("postgresql://u:p@169.254.169.254/x", set())


def test_allowlist_permits_listed_host():
    assert is_host_allowed("postgresql://u:p@db:5432/x", {"db", "localhost"})


def test_allowlist_blocks_unlisted_host():
    assert not is_host_allowed(
        "postgresql://u:p@169.254.169.254/x", {"db", "localhost"})


def test_allowlist_blocks_cloud_metadata_endpoint():
    """The canonical SSRF target."""
    assert not is_host_allowed(
        "postgresql://u:p@metadata.google.internal/x", {"db"})


def test_allowlist_is_case_insensitive():
    assert is_host_allowed("postgresql://u:p@DB.Internal/x", {"db.internal"})


def test_hostless_sqlite_url_is_permitted_even_with_allowlist():
    """SQLite opens no network connection, so it is not an SSRF vector."""
    assert is_host_allowed("sqlite:///./local.db", {"db"})


def test_assert_host_allowed_raises_for_blocked_host(monkeypatch):
    from backend.utils import config

    monkeypatch.setattr(config, "get_settings",
                        lambda: _settings_with(allowed_database_hosts="db"))
    from backend.services import url_guard
    monkeypatch.setattr(url_guard, "get_settings",
                        lambda: _settings_with(allowed_database_hosts="db"))
    with pytest.raises(DatabaseHostNotAllowed):
        url_guard.assert_host_allowed("postgresql://u:p@evil.example/x")


def _settings_with(**overrides):
    """Build a Settings object with fields overridden, bypassing the cache."""
    from backend.utils.config import Settings
    return Settings(**overrides)


# --- query timeout --------------------------------------------------------

def test_dialect_of_strips_the_driver():
    assert dialect_of("postgresql+psycopg2://u@h/db") == "postgresql"
    assert dialect_of("sqlite:///x.db") == "sqlite"


def test_postgres_gets_a_statement_timeout():
    args = timeout_connect_args("postgresql+psycopg2://u@h/db", 30)
    assert args == {"options": "-c statement_timeout=30000"}


def test_mysql_gets_max_execution_time():
    args = timeout_connect_args("mysql+pymysql://u@h/db", 30)
    assert "max_execution_time=30000" in args["init_command"]


def test_sqlite_gets_nothing_rather_than_a_misleading_timeout():
    """SQLite's `timeout` is a lock wait, not a statement cap; don't pretend."""
    assert timeout_connect_args("sqlite:///x.db", 30) == {}


def test_zero_timeout_disables_the_cap():
    assert timeout_connect_args("postgresql://u@h/db", 0) == {}


# --- login rate limiting --------------------------------------------------

def test_limiter_allows_up_to_the_threshold():
    limiter = SlidingWindowRateLimiter(max_attempts=3, window_seconds=60)
    for _ in range(2):
        limiter.record_failure("k", now=100.0)
    assert not limiter.is_limited("k", now=100.0)


def test_limiter_blocks_at_the_threshold():
    limiter = SlidingWindowRateLimiter(max_attempts=3, window_seconds=60)
    for _ in range(3):
        limiter.record_failure("k", now=100.0)
    assert limiter.is_limited("k", now=100.0)


def test_limiter_window_slides():
    limiter = SlidingWindowRateLimiter(max_attempts=3, window_seconds=60)
    for _ in range(3):
        limiter.record_failure("k", now=100.0)
    assert limiter.is_limited("k", now=150.0)
    assert not limiter.is_limited("k", now=161.0)   # all three aged out


def test_limiter_reset_clears_history():
    limiter = SlidingWindowRateLimiter(max_attempts=1, window_seconds=60)
    limiter.record_failure("k", now=100.0)
    limiter.reset("k")
    assert not limiter.is_limited("k", now=100.0)


def test_limiter_keys_are_independent():
    limiter = SlidingWindowRateLimiter(max_attempts=1, window_seconds=60)
    limiter.record_failure("a", now=100.0)
    assert limiter.is_limited("a", now=100.0)
    assert not limiter.is_limited("b", now=100.0)


def test_retry_after_is_positive_when_limited_and_zero_otherwise():
    limiter = SlidingWindowRateLimiter(max_attempts=1, window_seconds=60)
    assert limiter.retry_after("k", now=100.0) == 0
    limiter.record_failure("k", now=100.0)
    assert 0 < limiter.retry_after("k", now=100.0) <= 61


# --- report chart rendering (optional dependency) -------------------------

def test_render_chart_png_returns_false_without_a_dataframe(tmp_path):
    assert render_chart_png(None, "bar", tmp_path / "c.png") is False


def test_render_chart_png_returns_false_without_a_chart_type(tmp_path):
    assert render_chart_png(pd.DataFrame({"a": [1]}), "", tmp_path / "c.png") is False


def test_render_chart_png_never_raises_on_unrenderable_input(tmp_path):
    """The report must build even when the chart cannot; failure is a bool."""
    out = render_chart_png(pd.DataFrame(), "bar", tmp_path / "c.png")
    assert out is False


# --- JWT secret strength --------------------------------------------------

def test_short_jwt_secret_is_warned_about(monkeypatch, caplog):
    """A short HS256 key is brute-forceable offline; it must not pass silently."""
    from backend.utils import security

    monkeypatch.setattr(security, "_WARNED_SHORT_SECRET", False)
    monkeypatch.setattr(security, "get_settings",
                        lambda: _settings_with(jwt_secret_key="tooshort"))
    with caplog.at_level("WARNING"):
        assert security._get_secret() == "tooshort"   # noqa: SLF001
    assert any("JWT_SECRET_KEY is only" in r.message for r in caplog.records)


def test_long_jwt_secret_passes_without_warning(monkeypatch, caplog):
    from backend.utils import security

    strong = "x" * security.MIN_SECRET_BYTES
    monkeypatch.setattr(security, "_WARNED_SHORT_SECRET", False)
    monkeypatch.setattr(security, "get_settings",
                        lambda: _settings_with(jwt_secret_key=strong))
    with caplog.at_level("WARNING"):
        assert security._get_secret() == strong       # noqa: SLF001
    assert not any("JWT_SECRET_KEY is only" in r.message for r in caplog.records)
