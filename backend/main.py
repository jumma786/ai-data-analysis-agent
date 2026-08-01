"""FastAPI application entry point.

/health is public. Everything that reads data or spends money on an LLM call
requires a bearer token issued by /auth/login -- see backend/api/auth.py.
"""
from __future__ import annotations
import tempfile, shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends, FastAPI, UploadFile, File, HTTPException

from backend.api.auth import get_current_user, router as auth_router
from backend.database.models import User
from backend.database.session import init_db
from backend.models.schemas import (
    QueryRequest, QueryResponse, DBConnectRequest, ChatRequest)
from backend.agents.graph import run_pipeline
from backend.services.profiling import load_dataframe, profile_dataset, clean_dataset
from backend.services.schema_introspect import introspect_schema
from backend.utils.config import get_settings
from backend.utils.logging_config import logger

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Ensure metadata tables exist so /auth/signup works on a fresh checkout."""
    init_db()
    yield


app = FastAPI(title="AI Data Analysis Agent", lifespan=lifespan)
app.include_router(auth_router)

# In-memory schema cache for the demo. Replace with per-user persistence.
_SCHEMA_CACHE: dict[str, str] = {}


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
    profile = profile_dataset(df)
    cleaned = clean_dataset(df)
    profile["rows_after_dedup"] = len(cleaned)
    return {"dataset": file.filename, **profile}


@app.post("/connect-database")
def connect_database(req: DBConnectRequest,
                     current_user: User = Depends(get_current_user)) -> dict:
    # Authentication only narrows this to known accounts; it does not make the
    # endpoint safe. Any logged-in user can still make the server open an
    # outbound connection to a URL of their choosing (an SSRF primitive), and
    # the cache below is process-global rather than per-user. An allowlist of
    # permitted hosts is the real fix.
    try:
        schema = introspect_schema(req.database_url)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Connection failed: {e}")
    _SCHEMA_CACHE["current"] = schema
    return {"connected": True, "schema": schema}


@app.get("/schema")
def get_schema(current_user: User = Depends(get_current_user)) -> dict:
    return {"schema": _SCHEMA_CACHE.get("current", "")}


def _run_query(req: QueryRequest) -> QueryResponse:
    """Run the agent pipeline and shape the response.

    Kept separate from the route so /chat can reuse it without going through
    FastAPI's dependency injection a second time.
    """
    schema = req.schema_text or _SCHEMA_CACHE.get("current", "")
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
    return _run_query(req)


@app.post("/chat", response_model=QueryResponse)
def chat(req: ChatRequest,
         current_user: User = Depends(get_current_user)) -> QueryResponse:
    history = "\n".join(f"{m.role}: {m.content}" for m in req.messages[:-1])
    last = req.messages[-1].content if req.messages else ""
    return _run_query(QueryRequest(question=last, schema_text=req.schema_text,
                                   history=history))


@app.post("/generate-report")
def generate_report(req: QueryRequest,
                    current_user: User = Depends(get_current_user)) -> dict:
    # Delegates to the report service (PDF). See backend/services/report.py.
    from backend.services.report import build_report
    state = run_pipeline(req.question, schema=req.schema_text or
                         _SCHEMA_CACHE.get("current", ""))
    if not state.get("valid"):
        raise HTTPException(400, state.get("error", "Invalid query"))
    path = build_report(req.question, state)
    return {"report_path": path}
