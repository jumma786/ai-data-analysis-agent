"""Unit tests for signup, login, and JWT verification.

Everything here runs against an in-memory SQLite database and never touches
Postgres, the network, or an LLM. The protected-endpoint tests only assert the
*rejection* paths (401) and the dependency itself -- exercising a successful
/query would require a live LLM, which is covered by the integration suite.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.auth import get_current_user, login_limiter, normalize_email
from backend.database.models import Base, RefreshToken, User
from backend.database.session import get_db
from backend.main import app
from backend.utils.config import get_settings
from backend.utils.security import (
    MAX_PASSWORD_BYTES, TokenError, create_access_token, decode_access_token,
    hash_password, password_too_long, verify_password)

VALID_PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def clean_rate_limiter():
    """The login limiter is module-global; stop failures leaking between tests."""
    login_limiter.clear_all()
    yield
    login_limiter.clear_all()


@pytest.fixture()
def db_session():
    """A fresh in-memory SQLite database per test.

    StaticPool keeps every connection pointed at the same in-memory database;
    without it each new connection would get an empty one.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db_session, monkeypatch):
    """TestClient for the real app with the metadata DB swapped for SQLite.

    `init_db` is stubbed out because the app's lifespan would otherwise create
    the configured metadata database (a SQLite file on disk by default), which
    a unit test has no business doing.
    """
    monkeypatch.setattr("backend.main.init_db", lambda: None)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def probe_client(db_session):
    """A throwaway app with one route guarded by `get_current_user`.

    Lets us verify the dependency's success path without invoking the agent
    pipeline (which would need an LLM).
    """
    probe = FastAPI()

    @probe.get("/protected")
    def protected(current_user: User = Depends(get_current_user)) -> dict:
        return {"email": current_user.email}

    probe.dependency_overrides[get_db] = lambda: db_session
    with TestClient(probe) as test_client:
        yield test_client


def _signup(client, email: str = "analyst@example.com",
            password: str = VALID_PASSWORD):
    return client.post("/auth/signup", json={"email": email, "password": password})


def _login(client, email: str = "analyst@example.com",
           password: str = VALID_PASSWORD):
    return client.post("/auth/login", json={"email": email, "password": password})


# --- password hashing -----------------------------------------------------

def test_hash_password_is_not_reversible_and_verifies():
    hashed = hash_password(VALID_PASSWORD)
    assert hashed != VALID_PASSWORD
    assert verify_password(VALID_PASSWORD, hashed)


def test_verify_password_rejects_wrong_password():
    assert not verify_password("wrong", hash_password(VALID_PASSWORD))


def test_hash_password_is_salted():
    assert hash_password(VALID_PASSWORD) != hash_password(VALID_PASSWORD)


def test_password_too_long_matches_bcrypt_limit():
    assert not password_too_long("a" * MAX_PASSWORD_BYTES)
    assert password_too_long("a" * (MAX_PASSWORD_BYTES + 1))


def test_verify_password_returns_false_on_malformed_hash():
    assert not verify_password(VALID_PASSWORD, "not-a-bcrypt-hash")


# --- token creation / verification ----------------------------------------

def test_token_round_trip_preserves_subject():
    token = create_access_token("analyst@example.com")
    assert decode_access_token(token)["sub"] == "analyst@example.com"


def test_expired_token_is_rejected():
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    token = create_access_token("analyst@example.com", expires_minutes=1, now=stale)
    with pytest.raises(TokenError):
        decode_access_token(token)


def test_tampered_token_is_rejected():
    token = create_access_token("analyst@example.com")
    head, payload, signature = token.split(".")
    tampered = f"{head}.{payload}.{signature[:-4]}AAAA"
    with pytest.raises(TokenError):
        decode_access_token(tampered)


def test_garbage_token_is_rejected():
    with pytest.raises(TokenError):
        decode_access_token("not.a.jwt")


# --- signup ---------------------------------------------------------------

def test_signup_creates_user(client, db_session):
    resp = _signup(client)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == "analyst@example.com"
    assert "password" not in body and "hashed_password" not in body
    stored = db_session.query(User).one()
    assert stored.hashed_password != VALID_PASSWORD


def test_signup_rejects_duplicate_email(client):
    assert _signup(client).status_code == 201
    assert _signup(client).status_code == 409


def test_signup_email_is_case_insensitive(client):
    assert _signup(client, email="Analyst@Example.com").status_code == 201
    assert _signup(client, email="analyst@example.com").status_code == 409


def test_signup_rejects_invalid_email(client):
    assert _signup(client, email="not-an-email").status_code == 422


def test_signup_rejects_short_password(client):
    assert _signup(client, password="short").status_code == 422


# --- login ----------------------------------------------------------------

def test_login_returns_usable_token(client):
    _signup(client)
    resp = _login(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert decode_access_token(body["access_token"])["sub"] == "analyst@example.com"
    assert body["refresh_token"], "login should also return a refresh token"


def test_login_rejects_wrong_password(client):
    _signup(client)
    assert _login(client, password="wrong password").status_code == 401


def test_login_rejects_unknown_email(client):
    assert _login(client, email="nobody@example.com").status_code == 401


def test_login_accepts_differently_cased_email(client):
    _signup(client)
    assert _login(client, email="ANALYST@example.com").status_code == 200


# --- the get_current_user dependency --------------------------------------

def test_dependency_accepts_valid_token(probe_client, db_session):
    db_session.add(User(email="analyst@example.com",
                        hashed_password=hash_password(VALID_PASSWORD)))
    db_session.commit()
    token = create_access_token("analyst@example.com")
    resp = probe_client.get("/protected",
                            headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == "analyst@example.com"


def test_dependency_rejects_token_for_deleted_user(probe_client):
    token = create_access_token("ghost@example.com")
    resp = probe_client.get("/protected",
                            headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_dependency_rejects_missing_header(probe_client):
    assert probe_client.get("/protected").status_code == 401


# --- protected application endpoints --------------------------------------

@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/query", {"question": "total revenue by country"}),
        ("post", "/chat", {"messages": [{"role": "user", "content": "hi"}]}),
        ("post", "/generate-report", {"question": "total revenue by country"}),
        ("post", "/connect-database", {"database_url": "sqlite://"}),
    ],
)
def test_protected_endpoints_reject_anonymous_json_requests(client, method, path,
                                                            payload):
    assert getattr(client, method)(path, json=payload).status_code == 401


def test_upload_rejects_anonymous_request(client):
    resp = client.post("/upload", files={"file": ("t.csv", b"a,b\n1,2\n")})
    assert resp.status_code == 401


def test_schema_rejects_anonymous_request(client):
    """/schema leaks the cached database structure, so it is not public."""
    assert client.get("/schema").status_code == 401


def test_schema_is_readable_with_a_valid_token(client, db_session):
    """Sanity check that the guard admits real users, not just that it blocks."""
    db_session.add(User(email="analyst@example.com",
                        hashed_password=hash_password(VALID_PASSWORD)))
    db_session.commit()
    token = create_access_token("analyst@example.com")
    resp = client.get("/schema", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert "schema" in resp.json()


def test_protected_endpoint_rejects_invalid_token(client):
    resp = client.post("/query", json={"question": "x"},
                       headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401


def test_health_stays_public(client):
    assert client.get("/health").status_code == 200


# --- refresh tokens, rotation and revocation ------------------------------

def test_refresh_returns_a_new_pair(client):
    _signup(client)
    original = _login(client).json()
    resp = client.post("/auth/refresh",
                       json={"refresh_token": original["refresh_token"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"] != original["access_token"]


def test_refresh_rotates_the_old_token_out(client):
    """A refresh token is single-use: replaying it must fail."""
    _signup(client)
    original = _login(client).json()["refresh_token"]
    assert client.post("/auth/refresh",
                       json={"refresh_token": original}).status_code == 200
    replay = client.post("/auth/refresh", json={"refresh_token": original})
    assert replay.status_code == 401


def test_refresh_rejects_an_access_token(client):
    """Token type is enforced -- an access token is not a refresh token."""
    _signup(client)
    access = _login(client).json()["access_token"]
    assert client.post("/auth/refresh",
                       json={"refresh_token": access}).status_code == 401


def test_access_endpoint_rejects_a_refresh_token(client):
    """And the converse: a refresh token cannot authorise a request."""
    _signup(client)
    refresh_token = _login(client).json()["refresh_token"]
    resp = client.post("/query", json={"question": "x"},
                       headers={"Authorization": f"Bearer {refresh_token}"})
    assert resp.status_code == 401


def test_refresh_rejects_garbage(client):
    assert client.post("/auth/refresh",
                       json={"refresh_token": "not.a.jwt"}).status_code == 401


def test_logout_revokes_the_refresh_token(client):
    _signup(client)
    refresh_token = _login(client).json()["refresh_token"]
    assert client.post("/auth/logout",
                       json={"refresh_token": refresh_token}).status_code == 204
    assert client.post("/auth/refresh",
                       json={"refresh_token": refresh_token}).status_code == 401


def test_logout_is_silent_about_unknown_tokens(client):
    """204 either way, so logout cannot be used to probe which tokens exist."""
    assert client.post("/auth/logout",
                       json={"refresh_token": "not.a.jwt"}).status_code == 204


def test_refresh_token_is_stored_by_jti_not_verbatim(client, db_session):
    """The token is a credential; only its identifier belongs in the database."""
    _signup(client)
    refresh_token = _login(client).json()["refresh_token"]
    record = db_session.query(RefreshToken).one()
    assert record.jti not in (refresh_token, "")
    assert refresh_token not in (record.jti or "")


# --- login rate limiting --------------------------------------------------

def test_repeated_failures_are_throttled(client):
    _signup(client)
    for _ in range(get_settings().login_max_attempts):
        assert _login(client, password="wrong password").status_code == 401
    limited = _login(client, password="wrong password")
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers


def test_successful_login_clears_the_failure_count(client):
    _signup(client)
    _login(client, password="wrong password")
    assert _login(client).status_code == 200
    # The earlier failure was reset, so a fresh run of failures is allowed.
    for _ in range(get_settings().login_max_attempts - 1):
        assert _login(client, password="wrong password").status_code == 401


# --- helpers --------------------------------------------------------------

def test_normalize_email_trims_and_lowercases():
    assert normalize_email("  Analyst@Example.COM ") == "analyst@example.com"
