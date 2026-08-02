"""Tests for /generate-report and /reports/{id}/download.

`run_pipeline` is stubbed -- these tests assert routing, persistence and
ownership enforcement, not SQL generation or model quality, so stubbing it here
is legitimate (see tests/test_documents_api.py's docstring for the same
argument). `build_report` runs for real: it is pure reportlab/pandas, no LLM.

Uses in-memory SQLite, mirroring tests/test_documents_api.py.
"""
from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database.models import Base, User
from backend.database.session import get_db
from backend.main import app
from backend.utils.security import create_access_token, hash_password

PASSWORD = "correct horse battery"


def _stub_state(**overrides) -> dict:
    state = {
        "sql": "SELECT 1",
        "valid": True,
        "df": pd.DataFrame({"a": [1, 2], "b": [3, 4]}),
        "chart": "",
        "insight": "Stub insight.",
    }
    state.update(overrides)
    return state


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setattr("backend.main.init_db", lambda: None)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def make_user(db_session, email: str) -> User:
    user = User(email=email, hashed_password=hash_password(PASSWORD))
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def auth_header(email: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(email)}"}


def test_generate_report_requires_authentication(client):
    resp = client.post("/generate-report", json={"question": "revenue"})
    assert resp.status_code == 401


def test_generate_report_returns_an_id_not_a_filesystem_path(client, db_session,
                                                             monkeypatch):
    make_user(db_session, "a@example.com")
    monkeypatch.setattr("backend.main.run_pipeline", lambda *a, **kw: _stub_state())

    resp = client.post("/generate-report", json={"question": "revenue"},
                       headers=auth_header("a@example.com"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {"report_id"}
    assert isinstance(body["report_id"], int)


def test_generate_report_rejects_invalid_sql(client, db_session, monkeypatch):
    make_user(db_session, "a@example.com")
    monkeypatch.setattr(
        "backend.main.run_pipeline",
        lambda *a, **kw: _stub_state(valid=False, error="unsafe SQL"))

    resp = client.post("/generate-report", json={"question": "drop everything"},
                       headers=auth_header("a@example.com"))
    assert resp.status_code == 400
    assert "unsafe SQL" in resp.text


def test_owner_can_download_their_report(client, db_session, monkeypatch):
    make_user(db_session, "a@example.com")
    monkeypatch.setattr("backend.main.run_pipeline", lambda *a, **kw: _stub_state())

    gen = client.post("/generate-report", json={"question": "revenue"},
                      headers=auth_header("a@example.com"))
    report_id = gen.json()["report_id"]

    dl = client.get(f"/reports/{report_id}/download",
                    headers=auth_header("a@example.com"))
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "application/pdf"
    assert dl.content.startswith(b"%PDF")


def test_another_user_cannot_download_the_report(client, db_session, monkeypatch):
    """The actual authorization bug this route exists to close."""
    make_user(db_session, "alice@example.com")
    make_user(db_session, "bob@example.com")
    monkeypatch.setattr("backend.main.run_pipeline", lambda *a, **kw: _stub_state())

    gen = client.post("/generate-report", json={"question": "revenue"},
                      headers=auth_header("alice@example.com"))
    report_id = gen.json()["report_id"]

    dl = client.get(f"/reports/{report_id}/download",
                    headers=auth_header("bob@example.com"))
    assert dl.status_code == 404


def test_nonexistent_report_id_is_404(client, db_session):
    make_user(db_session, "a@example.com")
    resp = client.get("/reports/999999/download", headers=auth_header("a@example.com"))
    assert resp.status_code == 404


def test_download_requires_authentication(client, db_session, monkeypatch):
    make_user(db_session, "a@example.com")
    monkeypatch.setattr("backend.main.run_pipeline", lambda *a, **kw: _stub_state())
    gen = client.post("/generate-report", json={"question": "revenue"},
                      headers=auth_header("a@example.com"))
    report_id = gen.json()["report_id"]

    resp = client.get(f"/reports/{report_id}/download")
    assert resp.status_code == 401


def test_concurrent_reports_do_not_collide_on_disk(client, db_session, monkeypatch):
    """Regression test for the fixed-filename race this fix removes."""
    make_user(db_session, "a@example.com")
    monkeypatch.setattr("backend.main.run_pipeline", lambda *a, **kw: _stub_state())

    first = client.post("/generate-report", json={"question": "revenue"},
                        headers=auth_header("a@example.com")).json()["report_id"]
    second = client.post("/generate-report", json={"question": "revenue"},
                         headers=auth_header("a@example.com")).json()["report_id"]

    assert first != second
    for rid in (first, second):
        dl = client.get(f"/reports/{rid}/download", headers=auth_header("a@example.com"))
        assert dl.status_code == 200
