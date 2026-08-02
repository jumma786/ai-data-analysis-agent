"""FastAPI application entry point.

/health and the signup/login endpoints are public. Everything else requires a
bearer token issued by /auth/login -- see backend/api/auth.py.
"""
from __future__ import annotations
import tempfile, shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends, FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from backend.api.auth import get_current_user, router as auth_router
from backend.api.documents import router as documents_router
from backend.database.models import Report, User
from backend.database.session import get_db, init_db
from backend.models.schemas import (
    QueryRequest, QueryResponse, DBConnectRequest, ChatRequest,
    ReportGenerateResponse)
from backend.agents.graph import run_pipeline
from backend.services.profiling import load_dataframe, profile_dataset, clean_dataset
from backend.services.schema_introspect import introspect_schema
from backend.services.url_guard import DatabaseHostNotAllowed, assert_host_allowed
from backend.utils.config import get_settings
from backend.utils.logging_config import logger


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Ensure metadata tables exist so /auth/signup works on a fresh checkout."""
    init_db()
    yield


app = FastAPI(title="AI Data Analysis Agent", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(documents_router)

# Introspected schema per user id. Previously a single global entry, which meant
# whoever connected last set the schema every other user queried against -- a
# correctness bug as soon as there were two users, not merely a privacy one.
# Still process-local: it does not survive a restart or span workers.
_SCHEMA_CACHE: dict[int, str] = {}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "provider": get_settings().llm_provider}


@app.post("/upload")
async def upload(file: UploadFile = File(...),
                 current_user: User = Depends(get_current_user)) -> dict:
    suffix = Path(file.filename).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name
    try:
        df = load_dataframe(tmp_path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    profile = profile_dataset(df)
    cleaned = clean_dataset(df)
    profile["rows_after_dedup"] = len(cleaned)
    return {"dataset": file.filename, **profile}


@app.post("/connect-database")
def connect_database(req: DBConnectRequest,
                     current_user: User = Depends(get_current_user)) -> dict:
    """Introspect a database and cache its schema for this user.

    The host allowlist is checked before any connection is attempted: without
    it, an authenticated user can make the server dial internal infrastructure
    and read the error back. See services/url_guard.py for what that does and
    does not defend against.
    """
    try:
        assert_host_allowed(req.database_url)
    except DatabaseHostNotAllowed as e:
        logger.warning("Blocked connection attempt to a non-allowlisted host.")
        raise HTTPException(403, str(e))
    try:
        schema = introspect_schema(req.database_url)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Connection failed: {e}")
    _SCHEMA_CACHE[current_user.id] = schema
    return {"connected": True, "schema": schema}


@app.get("/schema")
def get_schema(current_user: User = Depends(get_current_user)) -> dict:
    return {"schema": _SCHEMA_CACHE.get(current_user.id, "")}


def _run_query(req: QueryRequest, user_id: int) -> QueryResponse:
    """Run the agent pipeline and shape the response.

    Kept separate from the route so /chat can reuse it without going through
    FastAPI's dependency injection a second time.
    """
    schema = req.schema_text or _SCHEMA_CACHE.get(user_id, "")
    state = run_pipeline(req.question, schema=schema, history=req.history or "")
    if not state.get("valid"):
        return QueryResponse(sql=state.get("sql"), valid=False,
                             error=state.get("error"))
    df = state["df"]
    return QueryResponse(
        sql=state.get("sql"), valid=True, chart=state.get("chart"),
        insight=state.get("insight"), row_count=len(df),
        rows=df.head(100).to_dict(orient="records"),
    )


@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest,
          current_user: User = Depends(get_current_user)) -> QueryResponse:
    return _run_query(req, current_user.id)


@app.post("/chat", response_model=QueryResponse)
def chat(req: ChatRequest,
         current_user: User = Depends(get_current_user)) -> QueryResponse:
    history = "\n".join(f"{m.role}: {m.content}" for m in req.messages[:-1])
    last = req.messages[-1].content if req.messages else ""
    return _run_query(QueryRequest(question=last, schema_text=req.schema_text,
                                   history=history), current_user.id)


@app.post("/generate-report", response_model=ReportGenerateResponse)
def generate_report(req: QueryRequest,
                    current_user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)) -> ReportGenerateResponse:
    # Delegates to the report service (PDF). See backend/services/report.py.
    from backend.services.report import build_report
    state = run_pipeline(req.question, schema=req.schema_text or
                         _SCHEMA_CACHE.get(current_user.id, ""))
    if not state.get("valid"):
        raise HTTPException(400, state.get("error", "Invalid query"))
    path = build_report(req.question, state)

    # Persisted so /reports/{id}/download can check ownership. The path itself
    # is a server-local filesystem detail and is never returned to the client.
    report = Report(user_id=current_user.id, path=path)
    db.add(report)
    db.commit()
    db.refresh(report)
    return ReportGenerateResponse(report_id=report.id)


@app.get("/reports/{report_id}/download")
def download_report(report_id: int,
                    current_user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)) -> FileResponse:
    """Serve a previously generated report -- only to the user who generated it.

    404 (not 403) for both "doesn't exist" and "belongs to someone else", so a
    caller cannot use the response to enumerate other users' report ids.
    """
    report = db.query(Report).filter(Report.id == report_id).one_or_none()
    if report is None or report.user_id != current_user.id:
        raise HTTPException(404, "Report not found.")
    if not Path(report.path).exists():
        raise HTTPException(410, "Report file is no longer available.")
    return FileResponse(report.path, media_type="application/pdf",
                        filename="report.pdf")
