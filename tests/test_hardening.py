"""Tests for the security and robustness work: SSRF allowlist, query timeout,
login rate limiting, per-user scoping, and optional chart rendering.

All offline: no database beyond in-memory SQLite, no network, no LLM.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.services.db import dialect_of, timeout_connect_args
from backend.services.report import render_chart_png
from backend.services import url_guard
from backend.services.url_guard import (
    DatabaseHostNotAllowed, host_of, is_host_allowed, is_metadata_address,
    parse_allowlist)
from backend.utils import rate_limit
from backend.utils.config import Settings
from backend.utils.rate_limit import (
    RedisSlidingWindowRateLimiter, SlidingWindowRateLimiter, get_rate_limiter)


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
    monkeypatch.setattr(url_guard, "get_settings",
                        lambda: _settings_with(allowed_database_hosts="db"))
    with pytest.raises(DatabaseHostNotAllowed):
        url_guard.assert_host_allowed("postgresql://u:p@evil.example/x")


# --- DNS resolution + cloud-metadata addresses ------------------------------
#
# Resolution itself is stubbed via `url_guard._resolve`, so these are
# deterministic and need no network -- consistent with the rest of the suite.

def test_is_metadata_address_flags_known_ranges():
    import ipaddress

    for addr in ("169.254.169.254", "169.254.170.2", "fe80::1",
                "fd00:ec2::254", "100.100.100.200"):
        assert is_metadata_address(ipaddress.ip_address(addr)), addr


def test_is_metadata_address_permits_ordinary_addresses():
    import ipaddress

    for addr in ("10.0.0.5", "127.0.0.1", "192.168.1.1", "8.8.8.8"):
        assert not is_metadata_address(ipaddress.ip_address(addr)), addr


def test_resolve_returns_the_literal_address_for_an_ip_host():
    addrs = url_guard._resolve("169.254.169.254")  # noqa: SLF001
    assert str(addrs[0]) == "169.254.169.254"


def test_resolve_returns_empty_for_an_unresolvable_host():
    assert url_guard._resolve("this-host-does-not-exist.invalid") == []  # noqa: SLF001


def test_assert_not_metadata_host_raises_when_resolution_hits_metadata(monkeypatch):
    import ipaddress

    monkeypatch.setattr(url_guard, "_resolve",
                        lambda host: [ipaddress.ip_address("169.254.169.254")])
    with pytest.raises(DatabaseHostNotAllowed):
        url_guard.assert_not_metadata_host("postgresql://u:p@analytics.internal/x")


def test_assert_not_metadata_host_permits_ordinary_resolution(monkeypatch):
    import ipaddress

    monkeypatch.setattr(url_guard, "_resolve",
                        lambda host: [ipaddress.ip_address("10.0.0.5")])
    url_guard.assert_not_metadata_host("postgresql://u:p@analytics.internal/x")


def test_assert_not_metadata_host_skips_hostless_urls(monkeypatch):
    def fail_if_called(host):
        raise AssertionError("resolution should not run for a hostless URL")

    monkeypatch.setattr(url_guard, "_resolve", fail_if_called)
    url_guard.assert_not_metadata_host("sqlite:///./local.db")


def test_assert_host_allowed_blocks_an_allowlisted_name_that_resolves_to_metadata(
        monkeypatch):
    """The gap a string match alone cannot close: a permitted name pointing at
    an internal/metadata address, e.g. via DNS rebinding."""
    import ipaddress

    monkeypatch.setattr(url_guard, "get_settings",
                        lambda: _settings_with(allowed_database_hosts="analytics.internal"))
    monkeypatch.setattr(url_guard, "_resolve",
                        lambda host: [ipaddress.ip_address("169.254.169.254")])
    with pytest.raises(DatabaseHostNotAllowed):
        url_guard.assert_host_allowed("postgresql://u:p@analytics.internal/x")


def test_assert_host_allowed_blocks_metadata_even_with_empty_allowlist(monkeypatch):
    """Empty allowlist means "permit any host" -- it should not mean "permit
    the cloud metadata endpoint"."""
    import ipaddress

    monkeypatch.setattr(url_guard, "get_settings",
                        lambda: _settings_with(allowed_database_hosts=""))
    monkeypatch.setattr(url_guard, "_resolve",
                        lambda host: [ipaddress.ip_address("169.254.169.254")])
    with pytest.raises(DatabaseHostNotAllowed):
        url_guard.assert_host_allowed("postgresql://u:p@169.254.169.254/x")


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


# --- login rate limiting ---------------------------------------------------
#
# Behavioural tests run against both backends via `limiter_factory`, so the
# Redis-backed implementation is held to the same contract as the in-process
# one -- not just "it runs", but "it agrees". Redis is faked with `fakeredis`;
# no live server is needed, matching the rest of the suite's offline default.

@pytest.fixture(params=["memory", "redis"])
def limiter_factory(request):
    """Return a `(max_attempts, window_seconds) -> RateLimiter` builder."""
    if request.param == "memory":
        return SlidingWindowRateLimiter

    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeStrictRedis()

    def build(max_attempts: int, window_seconds: int) -> RedisSlidingWindowRateLimiter:
        return RedisSlidingWindowRateLimiter(
            client, max_attempts, window_seconds, prefix=f"test-{id(client)}")

    return build


def test_limiter_allows_up_to_the_threshold(limiter_factory):
    limiter = limiter_factory(3, 60)
    for _ in range(2):
        limiter.record_failure("k", now=100.0)
    assert not limiter.is_limited("k", now=100.0)


def test_limiter_blocks_at_the_threshold(limiter_factory):
    limiter = limiter_factory(3, 60)
    for _ in range(3):
        limiter.record_failure("k", now=100.0)
    assert limiter.is_limited("k", now=100.0)


def test_limiter_window_slides(limiter_factory):
    limiter = limiter_factory(3, 60)
    for _ in range(3):
        limiter.record_failure("k", now=100.0)
    assert limiter.is_limited("k", now=150.0)
    assert not limiter.is_limited("k", now=161.0)   # all three aged out


def test_limiter_reset_clears_history(limiter_factory):
    limiter = limiter_factory(1, 60)
    limiter.record_failure("k", now=100.0)
    limiter.reset("k")
    assert not limiter.is_limited("k", now=100.0)


def test_limiter_keys_are_independent(limiter_factory):
    limiter = limiter_factory(1, 60)
    limiter.record_failure("a", now=100.0)
    assert limiter.is_limited("a", now=100.0)
    assert not limiter.is_limited("b", now=100.0)


def test_retry_after_is_positive_when_limited_and_zero_otherwise(limiter_factory):
    limiter = limiter_factory(1, 60)
    assert limiter.retry_after("k", now=100.0) == 0
    limiter.record_failure("k", now=100.0)
    assert 0 < limiter.retry_after("k", now=100.0) <= 61


def test_clear_all_drops_every_key(limiter_factory):
    limiter = limiter_factory(1, 60)
    limiter.record_failure("a", now=100.0)
    limiter.record_failure("b", now=100.0)
    limiter.clear_all()
    assert not limiter.is_limited("a", now=100.0)
    assert not limiter.is_limited("b", now=100.0)


# --- backend selection ------------------------------------------------------

def test_get_rate_limiter_defaults_to_in_process(monkeypatch):
    monkeypatch.setattr(rate_limit, "get_settings",
                        lambda: Settings(redis_url=""))
    limiter = get_rate_limiter(5, 300)
    assert isinstance(limiter, SlidingWindowRateLimiter)


def test_get_rate_limiter_picks_redis_when_configured(monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    monkeypatch.setattr(rate_limit, "get_settings",
                        lambda: Settings(redis_url="redis://fake:6379/0"))
    monkeypatch.setattr("redis.from_url", lambda *a, **kw: fakeredis.FakeStrictRedis())

    limiter = get_rate_limiter(5, 300)
    assert isinstance(limiter, RedisSlidingWindowRateLimiter)
    # And it actually works, not just constructs.
    limiter.record_failure("k", now=100.0)
    assert not limiter.is_limited("k", now=100.0)


def test_get_rate_limiter_falls_back_if_redis_package_missing(monkeypatch):
    monkeypatch.setattr(rate_limit, "get_settings",
                        lambda: Settings(redis_url="redis://fake:6379/0"))

    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "redis":
            raise ImportError("no redis here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    limiter = get_rate_limiter(5, 300)
    assert isinstance(limiter, SlidingWindowRateLimiter)


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


# --- production startup guard ---------------------------------------------
#
# The SQLite metadata default is correct for a zero-config local demo and
# silently destructive when deployed: the app boots, works, and discards every
# user on the next redeploy. These pin the boundary between those two cases.

_PG = "postgresql+psycopg2://u:p@h:5432/app_metadata"


def test_production_with_sqlite_metadata_refuses_to_start():
    from backend.utils.config import UnsafeDeploymentConfig, assert_deployment_safe

    s = _settings_with(environment="production",
                       metadata_database_url="sqlite:///./app_metadata.db")
    with pytest.raises(UnsafeDeploymentConfig) as excinfo:
        assert_deployment_safe(s)
    # The message has to name the variable to fix, not just complain.
    assert "METADATA_DATABASE_URL" in str(excinfo.value)


def test_production_with_postgres_metadata_starts():
    from backend.utils.config import assert_deployment_safe

    assert_deployment_safe(_settings_with(environment="production",
                                          metadata_database_url=_PG,
                                          allowed_database_hosts="db"))


def test_development_with_sqlite_metadata_still_starts():
    """The zero-config local path must keep working -- that is the whole point
    of gating the check on ENVIRONMENT rather than on JWT_SECRET_KEY."""
    from backend.utils.config import assert_deployment_safe

    assert_deployment_safe(_settings_with(environment="development",
                                          metadata_database_url="sqlite:///./app_metadata.db"))


def test_environment_value_is_matched_case_insensitively():
    from backend.utils.config import UnsafeDeploymentConfig, assert_deployment_safe

    s = _settings_with(environment="Production",
                       metadata_database_url="sqlite:///./app_metadata.db")
    with pytest.raises(UnsafeDeploymentConfig):
        assert_deployment_safe(s)


def test_production_with_an_empty_host_allowlist_refuses_to_start():
    """Empty means "any host", which is an SSRF hole rather than a default."""
    from backend.utils.config import UnsafeDeploymentConfig, assert_deployment_safe

    s = _settings_with(environment="production", metadata_database_url=_PG,
                       allowed_database_hosts="")
    with pytest.raises(UnsafeDeploymentConfig) as excinfo:
        assert_deployment_safe(s)
    assert "ALLOWED_DATABASE_HOSTS" in str(excinfo.value)


def test_production_reports_every_problem_at_once():
    """Fixing one problem per redeploy is a miserable way to find the rest."""
    from backend.utils.config import UnsafeDeploymentConfig, assert_deployment_safe

    s = _settings_with(environment="production",
                       metadata_database_url="sqlite:///./app_metadata.db",
                       allowed_database_hosts="")
    with pytest.raises(UnsafeDeploymentConfig) as excinfo:
        assert_deployment_safe(s)
    message = str(excinfo.value)
    assert "METADATA_DATABASE_URL" in message and "ALLOWED_DATABASE_HOSTS" in message


def test_in_memory_vector_store_warns_but_does_not_block(caplog):
    """A deployment that never uses RAG is entitled to the in-memory store; a
    check that blocks it would get switched off, and then protects nothing."""
    from backend.utils.config import assert_deployment_safe

    s = _settings_with(environment="production", metadata_database_url=_PG,
                       allowed_database_hosts="db", vector_store="memory")
    with caplog.at_level("WARNING"):
        assert_deployment_safe(s)          # must not raise
    assert any("VECTOR_STORE" in r.getMessage() for r in caplog.records)


def test_chroma_vector_store_warns_about_nothing(caplog):
    from backend.utils.config import assert_deployment_safe

    s = _settings_with(environment="production", metadata_database_url=_PG,
                       allowed_database_hosts="db", vector_store="chroma")
    with caplog.at_level("WARNING"):
        assert_deployment_safe(s)
    assert not any("VECTOR_STORE" in r.getMessage() for r in caplog.records)
