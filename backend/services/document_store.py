"""Per-user RAG document stores.

The previous design had one process-global collection, so every user's documents
shared a namespace and one upload was retrievable by everyone. Stores are now
keyed by user id: Chroma gets a collection per user, and the in-memory fallback
gets its own instance per user.

Remaining limitation, stated rather than hidden: in-memory stores live in this
process, so they are lost on restart and not shared between workers. Chroma
persists to disk and is the backend to use if that matters.
"""
from __future__ import annotations

import threading

from rag.pipeline import (
    ChromaVectorStore, InMemoryVectorStore, VectorStore, chunk_text)
from backend.utils.config import get_settings
from backend.utils.logging_config import logger

# user_id -> store. Guarded by a lock because FastAPI serves requests from a
# threadpool and two uploads from one user could otherwise race on creation.
_STORES: dict[int, VectorStore] = {}
_LOCK = threading.Lock()


def collection_name_for(user_id: int) -> str:
    """Chroma collection name scoped to one user."""
    return f"{get_settings().chroma_collection}_user_{user_id}"


def _build_store(user_id: int) -> VectorStore:
    """Create the configured backend for one user."""
    settings = get_settings()
    if settings.vector_store == "chroma":
        try:
            return ChromaVectorStore(
                persist_dir=settings.chroma_persist_dir,
                collection_name=collection_name_for(user_id),
            )
        except ImportError:
            logger.warning(
                "VECTOR_STORE=chroma but chromadb is not installed; using the "
                "in-memory store for user %s.", user_id)
    return InMemoryVectorStore()


def get_store(user_id: int) -> VectorStore:
    """Return this user's store, creating it on first use."""
    with _LOCK:
        store = _STORES.get(user_id)
        if store is None:
            store = _build_store(user_id)
            _STORES[user_id] = store
        return store


def add_document(user_id: int, text: str) -> int:
    """Chunk and ingest a document for one user. Returns the chunk count."""
    chunks = chunk_text(text)
    if chunks:
        get_store(user_id).add(chunks)
    logger.info("Ingested %s chunk(s) for user %s.", len(chunks), user_id)
    return len(chunks)


def search(user_id: int, question: str, top_k: int = 4) -> list[str]:
    """Retrieve the most relevant chunks from this user's store."""
    return get_store(user_id).query(question, k=top_k)


def document_count(user_id: int) -> int:
    """How many chunks this user's store currently holds.

    Both backends can report this, but by different means, so it is normalised
    here rather than pushed into the VectorStore protocol.
    """
    store = _STORES.get(user_id)
    if store is None:
        return 0
    if isinstance(store, InMemoryVectorStore):
        return len(store.docs)
    return int(store._collection.count())  # noqa: SLF001 -- Chroma has no public count


def active_backend(user_id: int) -> str:
    """'chroma' or 'memory' -- what this user's store actually resolved to."""
    store = _STORES.get(user_id)
    return "memory" if store is None or isinstance(store, InMemoryVectorStore) \
        else "chroma"


def reset() -> None:
    """Drop all cached stores. Used by tests to isolate cases."""
    with _LOCK:
        _STORES.clear()
