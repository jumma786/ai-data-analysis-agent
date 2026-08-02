"""Tests for the RAG document routes and refresh-token lifecycle.

Uses in-memory SQLite and the in-memory vector store, so no Postgres, no
Chroma, and no network. The one place an LLM would be needed --
/documents/query's answer generation -- is stubbed at the `answer_from_docs`
boundary, which is legitimate here: these tests assert routing, scoping and
retrieval, not generation quality. The NL->SQL path, where mocking the model
*would* be dishonest, is covered by the integration suite instead.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database.models import Base, User
from backend.database.session import get_db
from backend.main import app
from backend.services import document_store
from backend.utils.security import create_access_token, hash_password

PASSWORD = "correct horse battery"
DOC = (b"The refund policy allows returns within thirty days of delivery. "
       b"Shipping is free on orders over fifty pounds.")


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


@pytest.fixture(autouse=True)
def clean_document_stores():
    """Per-user stores are process-global; isolate each test."""
    document_store.reset()
    yield
    document_store.reset()


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


# --- document ingestion ---------------------------------------------------

def test_upload_requires_authentication(client):
    resp = client.post("/documents/upload", files={"file": ("a.txt", DOC)})
    assert resp.status_code == 401


def test_upload_ingests_a_text_document(client, db_session):
    make_user(db_session, "a@example.com")
    resp = client.post("/documents/upload", files={"file": ("policy.txt", DOC)},
                       headers=auth_header("a@example.com"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["chunks_added"] >= 1
    assert body["backend"] == "memory"
    assert body["filename"] == "policy.txt"


def test_upload_rejects_unsupported_file_type(client, db_session):
    make_user(db_session, "a@example.com")
    resp = client.post("/documents/upload",
                       files={"file": ("data.parquet", b"\x00\x01")},
                       headers=auth_header("a@example.com"))
    assert resp.status_code == 400
    assert "Unsupported document type" in resp.text


def test_upload_rejects_a_document_with_no_extractable_text(client, db_session):
    make_user(db_session, "a@example.com")
    resp = client.post("/documents/upload", files={"file": ("blank.txt", b"   ")},
                       headers=auth_header("a@example.com"))
    assert resp.status_code == 400
    assert "No extractable text" in resp.text


# --- retrieval and per-user scoping ---------------------------------------

def test_query_returns_relevant_chunks(client, db_session, monkeypatch):
    make_user(db_session, "a@example.com")
    monkeypatch.setattr("rag.pipeline.answer_from_docs",
                        lambda q, store: "stubbed answer")
    client.post("/documents/upload", files={"file": ("p.txt", DOC)},
                headers=auth_header("a@example.com"))

    resp = client.post("/documents/query", json={"question": "refund policy"},
                       headers=auth_header("a@example.com"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["chunks"], "no chunks retrieved"
    assert "refund" in body["chunks"][0].lower()
    assert body["answer"] == "stubbed answer"


def test_query_with_nothing_ingested_returns_empty_not_an_invention(client,
                                                                   db_session):
    make_user(db_session, "a@example.com")
    resp = client.post("/documents/query", json={"question": "anything"},
                       headers=auth_header("a@example.com"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["chunks"] == []
    assert body["answer"] is None


def test_one_users_documents_are_not_visible_to_another(client, db_session,
                                                        monkeypatch):
    """The whole point of per-user stores."""
    make_user(db_session, "alice@example.com")
    make_user(db_session, "bob@example.com")
    monkeypatch.setattr("rag.pipeline.answer_from_docs", lambda q, s: "stub")

    client.post("/documents/upload", files={"file": ("p.txt", DOC)},
                headers=auth_header("alice@example.com"))

    bob = client.post("/documents/query", json={"question": "refund policy"},
                      headers=auth_header("bob@example.com"))
    assert bob.json()["chunks"] == []

    alice = client.post("/documents/query", json={"question": "refund policy"},
                        headers=auth_header("alice@example.com"))
    assert alice.json()["chunks"]


def test_status_reports_count_and_backend(client, db_session):
    make_user(db_session, "a@example.com")
    client.post("/documents/upload", files={"file": ("p.txt", DOC)},
                headers=auth_header("a@example.com"))
    resp = client.get("/documents/status", headers=auth_header("a@example.com"))
    assert resp.status_code == 200
    assert resp.json()["document_count"] >= 1
    assert resp.json()["backend"] == "memory"


def test_top_k_is_validated(client, db_session):
    make_user(db_session, "a@example.com")
    resp = client.post("/documents/query",
                       json={"question": "x", "top_k": 999},
                       headers=auth_header("a@example.com"))
    assert resp.status_code == 422


# --- per-user schema cache ------------------------------------------------

def test_schema_cache_is_scoped_per_user(client, db_session):
    """Previously one global entry: whoever connected last won."""
    make_user(db_session, "alice@example.com")
    make_user(db_session, "bob@example.com")

    connected = client.post("/connect-database",
                            json={"database_url": "sqlite://"},
                            headers=auth_header("alice@example.com"))
    assert connected.status_code == 200, connected.text

    bob_schema = client.get("/schema", headers=auth_header("bob@example.com"))
    assert bob_schema.json()["schema"] == ""

    alice_schema = client.get("/schema", headers=auth_header("alice@example.com"))
    assert alice_schema.json()["schema"] == connected.json()["schema"]
