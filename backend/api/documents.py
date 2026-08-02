"""RAG document router: ingest documents, then ask questions about them.

Previously the RAG pipeline existed but nothing in the web layer called it, so
the feature was reachable only from Python. These are the missing routes.

    POST /documents/upload  -> extract text, chunk, embed, store (per user)
    POST /documents/query   -> retrieve relevant chunks and answer from them
    GET  /documents/status  -> how many chunks are stored, and on which backend

Every store is scoped to the authenticated user; see services/document_store.py.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from backend.api.auth import get_current_user
from backend.database.models import User
from backend.models.schemas import (
    DocumentIngestResponse, DocumentQueryRequest, DocumentQueryResponse)
from backend.services import document_store
from backend.utils.logging_config import logger
from rag.pipeline import extract_text

router = APIRouter(prefix="/documents", tags=["documents"])

SUPPORTED_SUFFIXES = {".txt", ".pdf", ".docx"}


@router.post("/upload", response_model=DocumentIngestResponse,
             status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
) -> DocumentIngestResponse:
    """Ingest a TXT/PDF/DOCX document into the caller's own document store."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unsupported document type {suffix!r}. "
            f"Supported: {', '.join(sorted(SUPPORTED_SUFFIXES))}.")

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        text = extract_text(tmp_path)
    except Exception as exc:  # noqa: BLE001 -- pypdf/docx raise varied types
        logger.warning("Failed to extract text from %s: %s", file.filename, exc)
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"Could not read the document: {exc}") from exc
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if not text.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No extractable text found. Scanned PDFs need OCR, which is not "
            "implemented.")

    added = document_store.add_document(current_user.id, text)
    return DocumentIngestResponse(
        filename=file.filename or "",
        chunks_added=added,
        document_count=document_store.document_count(current_user.id),
        backend=document_store.active_backend(current_user.id),
    )


@router.post("/query", response_model=DocumentQueryResponse)
def query_documents(
    req: DocumentQueryRequest,
    current_user: User = Depends(get_current_user),
) -> DocumentQueryResponse:
    """Answer a question from the caller's ingested documents.

    Returns the retrieved chunks alongside the answer so the response can be
    checked against its sources rather than taken on trust. If nothing has been
    ingested, returns empty rather than inventing an answer.
    """
    chunks = document_store.search(current_user.id, req.question, req.top_k)
    if not chunks:
        return DocumentQueryResponse(question=req.question, answer=None,
                                     chunks=[], document_count=0)

    # Imported here so the module stays importable without an LLM configured.
    from rag.pipeline import answer_from_docs

    store = document_store.get_store(current_user.id)
    answer = answer_from_docs(req.question, store)
    return DocumentQueryResponse(
        question=req.question,
        answer=answer,
        chunks=chunks,
        document_count=document_store.document_count(current_user.id),
    )


@router.get("/status")
def document_status(current_user: User = Depends(get_current_user)) -> dict:
    """Report how much the caller has ingested, and which backend is in use."""
    return {
        "document_count": document_store.document_count(current_user.id),
        "backend": document_store.active_backend(current_user.id),
    }
