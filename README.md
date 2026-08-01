# AI Data Analysis Agent

An AI-powered analytics platform. Upload a dataset or connect a database, then
ask questions in natural language. A LangGraph multi-agent pipeline generates
SQL, **validates it for safety**, executes it, picks a chart, and explains the
result. Includes a RAG document-Q&A pipeline and PDF report generation.

> Portfolio note: this is a working scaffold with verified core logic (18 unit
> tests pass). Parts that need live services (Postgres, an LLM key/Ollama) are
> wired but require configuration to run end-to-end. See "Status" below.

## Architecture

```
Streamlit UI ──HTTP──> FastAPI ──> LangGraph pipeline
                                     │
   schema → generate_sql → validate_sql ─(unsafe)→ stop
                                     │ (safe)
                              execute → analyze → insight
```

Agents (backend/agents/):
- **SQL Agent** – NL → single SELECT (LLM, provider-agnostic).
- **SQL Validation Agent** – blocks DROP/DELETE/UPDATE/stacked statements;
  read-only enforcement; comment stripping. Tested.
- **Analysis / Visualization / Insight Agents** – KPIs, deterministic chart
  choice, grounded explanation.
- **Report Agent** – PDF via reportlab.

Modular LLM layer (backend/services/llm.py): switch OpenAI ↔ Ollama via
`LLM_PROVIDER` with no changes to agent code.

## Quick start (local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add your OPENAI_API_KEY or set LLM_PROVIDER=ollama
uvicorn backend.main:app --reload           # backend on :8000
streamlit run frontend/streamlit_app.py     # UI on :8501
```

## Docker

```bash
docker compose up --build      # db + backend + frontend
```

## Tests

```bash
pytest tests/ -q               # 18 passing (validation, profiling, charts, RAG)
```

## API

| Method | Path                | Purpose                         |
|--------|---------------------|---------------------------------|
| GET    | /health             | Liveness + active LLM provider  |
| POST   | /upload             | Profile CSV/Excel/Parquet       |
| POST   | /connect-database   | Introspect a SQL database       |
| GET    | /schema             | Return cached schema            |
| POST   | /query              | NL question → SQL + result      |
| POST   | /chat               | Multi-turn (context-aware)      |
| POST   | /generate-report    | PDF report                      |

## Status (honest)

Verified by tests: SQL safety validation, dataset profiling, chart selection,
RAG chunking + retrieval (in-memory fallback store).

Wired but needs config/services to run: LLM calls (key or Ollama), live SQL
execution (Postgres), ChromaDB backend (falls back to in-memory store),
authentication (models exist; endpoints are a TODO — see DEVELOPMENT_GUIDE.md).

## Docs
- docs/ARCHITECTURE.md
- docs/API_DOCUMENTATION.md
- docs/DEVELOPMENT_GUIDE.md
