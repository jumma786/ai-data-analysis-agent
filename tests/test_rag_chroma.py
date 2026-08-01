"""Tests for the ChromaDB-backed vector store.

Run them with:

    RUN_CHROMA_TESTS=1 pytest tests/test_rag_chroma.py -q

WHY THESE ARE OPT-IN RATHER THAN PART OF THE DEFAULT RUN
--------------------------------------------------------
They pass 11/11 on their own, but crash the process with a Windows access
violation when collected alongside any test that imports pandas
(test_analysis.py, test_retail_loader.py). Root cause is environmental, not in
this repo's code:

    python -c "import onnxruntime"                 # fine
    python -c "import pandas; import onnxruntime"  # ImportError: DLL load failed

Once that failed DLL initialisation has happened, Chroma's Rust core
segfaults on upsert. `KMP_DUPLICATE_LIB_OK=TRUE` does not help. Until the
environment is fixed, these must run in a pandas-free process -- hence the
opt-in flag rather than a skipif that would quietly hide the crash.

WHAT THEY DO AND DO NOT COVER
-----------------------------
They inject a stub embedding function to stay offline and deterministic, since
Chroma's bundled all-MiniLM-L6-v2 downloads an ONNX model on first use. So they
verify the adapter -- ids, upsert semantics, query plumbing, k clamping,
persistence across clients -- and say nothing about embedding quality. Real
semantic retrieval was checked separately by hand and is recorded in
docs/DEVELOPMENT_GUIDE.md.
"""
from __future__ import annotations

import hashlib
import os
from uuid import uuid4

import pytest

pytestmark = [
    pytest.mark.chroma,
    pytest.mark.skipif(
        not os.getenv("RUN_CHROMA_TESTS"),
        reason="Set RUN_CHROMA_TESTS=1 to run these; they must not share a "
               "process with pandas (see this module's docstring).",
    ),
]

from rag.pipeline import (
    ChromaVectorStore, InMemoryVectorStore, VectorStore, build_store, chunk_id)

chromadb = pytest.importorskip("chromadb", reason="chromadb is not installed")

from chromadb.api.types import EmbeddingFunction  # noqa: E402


class HashingEmbeddingFunction(EmbeddingFunction):
    """Deterministic bag-of-words embedding: no model, no network.

    Subclasses Chroma's own base class so it inherits `embed_query` (which
    delegates to `__call__`) and passes the `is_legacy` check, which requires
    name/get_config/build_from_config to be implemented.

    Uses a stable hash rather than Python's `hash()`, whose string seed varies
    per process and would break the persistence test across client instances.
    """

    DIM = 64

    def __init__(self) -> None:
        # Chroma warns if an embedding function has no __init__.
        pass

    def __call__(self, input: list[str]) -> list[list[float]]:  # noqa: A002
        vectors = []
        for text in input:
            vec = [0.0] * self.DIM
            for word in text.lower().split():
                digest = hashlib.md5(word.encode("utf-8")).digest()
                vec[digest[0] % self.DIM] += 1.0
            norm = sum(v * v for v in vec) ** 0.5
            vectors.append([v / norm for v in vec] if norm else vec)
        return vectors

    @staticmethod
    def name() -> str:
        return "hashing-test-ef"

    def get_config(self) -> dict:
        return {}

    @staticmethod
    def build_from_config(config: dict) -> "HashingEmbeddingFunction":
        return HashingEmbeddingFunction()


@pytest.fixture()
def store() -> ChromaVectorStore:
    """An ephemeral (in-process, no disk) Chroma collection.

    The collection name is unique per test because `chromadb.EphemeralClient()`
    is cached per process: every call returns the same client, so a fixed name
    would leak documents from one test into the next.
    """
    return ChromaVectorStore(persist_dir=None,
                             collection_name=f"test_docs_{uuid4().hex}",
                             embedding_function=HashingEmbeddingFunction())


def test_chunk_id_is_stable_and_content_addressed():
    assert chunk_id("hello") == chunk_id("hello")
    assert chunk_id("hello") != chunk_id("world")


def test_add_then_query_returns_documents(store):
    store.add(["the refund policy allows returns within 30 days",
               "shipping is free over fifty pounds"])
    hits = store.query("refund policy returns", k=1)
    assert len(hits) == 1
    assert "refund" in hits[0].lower()


def test_query_on_empty_collection_returns_empty(store):
    assert store.query("anything") == []


def test_add_empty_list_is_a_noop(store):
    store.add([])                      # Chroma raises on an empty add.
    assert store.query("anything") == []


def test_k_is_clamped_to_collection_size(store):
    store.add(["only one document here"])
    assert len(store.query("document", k=10)) == 1


def test_reingesting_the_same_chunks_does_not_duplicate(store):
    chunks = ["alpha beta gamma", "delta epsilon zeta"]
    store.add(chunks)
    store.add(chunks)
    # Content-addressed ids mean the second add upserts over the first.
    assert len(store.query("alpha", k=10)) == 2


def test_chroma_store_satisfies_the_protocol(store):
    assert isinstance(store, VectorStore)


def test_build_store_rejects_an_unknown_backend():
    with pytest.raises(ValueError, match="Unknown vector store backend"):
        build_store("text", backend="pinecone")


def test_build_store_memory_backend_is_explicit():
    assert isinstance(build_store("some text", backend="memory"),
                      InMemoryVectorStore)


def test_build_store_chroma_backend_returns_chroma(tmp_path):
    built = build_store(
        "the refund policy allows returns within 30 days",
        backend="chroma",
        persist_dir=str(tmp_path / "chroma"),
        collection_name="built_docs",
        embedding_function=HashingEmbeddingFunction(),
    )
    assert isinstance(built, ChromaVectorStore)
    assert built.query("refund", k=1)


def test_chroma_persists_across_client_instances(tmp_path):
    """The whole point of the Chroma path: survive a process restart."""
    path = str(tmp_path / "chroma")
    first = ChromaVectorStore(persist_dir=path, collection_name="persisted",
                              embedding_function=HashingEmbeddingFunction())
    first.add(["the refund policy allows returns within 30 days"])
    del first

    second = ChromaVectorStore(persist_dir=path, collection_name="persisted",
                               embedding_function=HashingEmbeddingFunction())
    hits = second.query("refund", k=1)
    assert hits and "refund" in hits[0].lower()
