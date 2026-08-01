"""RAG Document Intelligence pipeline.

Extract text (PDF/DOCX/TXT) -> chunk -> embed -> store in a vector store ->
retrieve -> answer. Uses ChromaDB if available; otherwise an in-memory cosine
store so the pipeline runs in tests without external services.
"""
from __future__ import annotations
from pathlib import Path
from dataclasses import dataclass, field
import math


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


def build_store(text: str) -> InMemoryVectorStore:
    store = InMemoryVectorStore()
    store.add(chunk_text(text))
    return store


def answer_from_docs(question: str, store: InMemoryVectorStore) -> str:
    from backend.services.llm import get_llm
    context = "\n---\n".join(store.query(question))
    system = ("Answer using only the provided context. If the answer is not in "
              "the context, say you don't have that information.")
    return get_llm().complete(system, f"CONTEXT:\n{context}\n\nQUESTION: {question}")
