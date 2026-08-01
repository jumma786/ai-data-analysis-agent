"""RAG Document Intelligence pipeline.

Extract text (PDF/DOCX/TXT) -> chunk -> embed -> store in a vector store ->
retrieve -> answer.

Two stores implement the same `VectorStore` protocol:

* `InMemoryVectorStore` -- bag-of-words cosine similarity, no dependencies.
  The default, so tests and a bare checkout run with no external services.
* `ChromaVectorStore` -- persistent ChromaDB collection with real embeddings.
  Opt in with `VECTOR_STORE=chroma`.

Chroma is not the default because its built-in embedding function downloads an
ONNX model the first time it runs. That is fine for a deployment and wrong for
a test suite, so it has to be asked for explicitly.
"""
from __future__ import annotations
import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from backend.utils.logging_config import logger


@runtime_checkable
class VectorStore(Protocol):
    """The two-method interface the RAG pipeline needs from a store."""

    def add(self, chunks: list[str]) -> None: ...

    def query(self, q: str, k: int = 3) -> list[str]: ...


def extract_text(path: str | Path) -> str:
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".txt":
        return p.read_text(encoding="utf-8", errors="ignore")
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(p))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if ext == ".docx":
        import docx
        d = docx.Document(str(p))
        return "\n".join(para.text for para in d.paragraphs)
    raise ValueError(f"Unsupported document type: {ext}")


def chunk_text(text: str, size: int = 800, overlap: int = 100) -> list[str]:
    words = text.split()
    chunks, i = [], 0
    while i < len(words):
        chunks.append(" ".join(words[i:i + size]))
        i += size - overlap
    return [c for c in chunks if c.strip()]


@dataclass
class InMemoryVectorStore:
    """Fallback store using bag-of-words cosine similarity (no deps)."""
    docs: list[str] = field(default_factory=list)
    _vecs: list[dict] = field(default_factory=list)

    @staticmethod
    def _vectorize(text: str) -> dict:
        vec: dict[str, int] = {}
        for w in text.lower().split():
            vec[w] = vec.get(w, 0) + 1
        return vec

    def add(self, chunks: list[str]) -> None:
        for c in chunks:
            self.docs.append(c)
            self._vecs.append(self._vectorize(c))

    @staticmethod
    def _cosine(a: dict, b: dict) -> float:
        common = set(a) & set(b)
        num = sum(a[w] * b[w] for w in common)
        da = math.sqrt(sum(v * v for v in a.values()))
        db = math.sqrt(sum(v * v for v in b.values()))
        return num / (da * db) if da and db else 0.0

    def query(self, q: str, k: int = 3) -> list[str]:
        qv = self._vectorize(q)
        scored = sorted(
            ((self._cosine(qv, v), d) for v, d in zip(self._vecs, self.docs)),
            reverse=True,
        )
        return [d for _, d in scored[:k]]


def chunk_id(chunk: str) -> str:
    """Stable content-addressed id.

    Chroma requires an id per document. Hashing the content makes re-ingesting
    the same document overwrite rather than duplicate, which matters because
    `build_store` is called once per upload and users re-upload the same file.
    """
    return hashlib.sha256(chunk.encode("utf-8")).hexdigest()[:32]


class ChromaVectorStore:
    """Persistent ChromaDB-backed store.

    `embedding_function` is injectable so this class can be exercised without
    Chroma's default model (and therefore without a network round trip); pass
    None in production to use Chroma's bundled all-MiniLM-L6-v2.

    Note the collection is shared, not per-user: two users ingesting documents
    hit the same namespace. Scope `collection_name` per user before this holds
    anything confidential.
    """

    def __init__(self, persist_dir: str | None = None,
                 collection_name: str = "documents",
                 embedding_function: Any = None) -> None:
        import chromadb

        if persist_dir:
            self._client = chromadb.PersistentClient(path=persist_dir)
        else:
            # Ephemeral: nothing touches disk. Used by the tests.
            self._client = chromadb.EphemeralClient()

        kwargs: dict[str, Any] = {"name": collection_name}
        if embedding_function is not None:
            kwargs["embedding_function"] = embedding_function
        self._collection = self._client.get_or_create_collection(**kwargs)
        logger.info("Chroma collection '%s' ready (%s documents).",
                    collection_name, self._collection.count())

    def add(self, chunks: list[str]) -> None:
        """Upsert chunks. No-op on an empty list, which Chroma rejects."""
        if not chunks:
            return
        self._collection.upsert(ids=[chunk_id(c) for c in chunks],
                                documents=list(chunks))

    def query(self, q: str, k: int = 3) -> list[str]:
        """Return up to k documents ranked by embedding similarity."""
        count = self._collection.count()
        if not count:
            return []
        # Asking for more than the collection holds errors on some versions.
        res = self._collection.query(query_texts=[q], n_results=min(k, count))
        documents = res.get("documents") or [[]]
        return list(documents[0])


def build_store(text: str, backend: str | None = None,
                **store_kwargs: Any) -> VectorStore:
    """Chunk `text` and load it into a vector store.

    `backend` overrides the `VECTOR_STORE` setting; it exists so tests and
    callers can be explicit rather than depending on ambient config. Falls back
    to the in-memory store, with a warning, if Chroma is requested but its
    import fails -- degrading is better than failing an upload outright.
    """
    if backend is None:
        from backend.utils.config import get_settings
        backend = get_settings().vector_store

    chunks = chunk_text(text)
    store: VectorStore

    if backend == "chroma":
        from backend.utils.config import get_settings
        settings = get_settings()
        kwargs = {"persist_dir": settings.chroma_persist_dir,
                  "collection_name": settings.chroma_collection}
        kwargs.update(store_kwargs)
        try:
            store = ChromaVectorStore(**kwargs)
        except ImportError:
            logger.warning(
                "VECTOR_STORE=chroma but chromadb is not installed; falling "
                "back to the in-memory store. `pip install chromadb` to fix.")
            store = InMemoryVectorStore()
    elif backend == "memory":
        store = InMemoryVectorStore()
    else:
        raise ValueError(
            f"Unknown vector store backend {backend!r}; expected "
            f"'memory' or 'chroma'.")

    store.add(chunks)
    return store


def answer_from_docs(question: str, store: VectorStore) -> str:
    from backend.services.llm import get_llm
    context = "\n---\n".join(store.query(question))
    system = ("Answer using only the provided context. If the answer is not in "
              "the context, say you don't have that information.")
    return get_llm().complete(system, f"CONTEXT:\n{context}\n\nQUESTION: {question}")
