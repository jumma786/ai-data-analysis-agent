# AI Data Analysis Agent

An AI-powered analytics platform. Upload a dataset or connect a database, then
ask questions in natural language. A LangGraph multi-agent pipeline generates
SQL, **validates it for safety**, executes it, picks a chart, and explains the
result. Includes a RAG document-Q&A pipeline and PDF report generation.

> Portfolio note: this is a working scaffold with a verified core — the unit
> suite covers SQL safety, profiling, chart choice, RAG retrieval and auth, and
> runs offline. An opt-in integration suite exercises the query path against a
> real Postgres loaded with the UCI Online Retail II dataset. The NL → SQL step
> needs an LLM key and is not mocked. See "Status" below for exactly what is and
> is not verified.

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
pytest tests/ -q                          # unit suite: no DB, no network, no LLM
pytest tests/integration -m integration -q  # needs a live DB (see below)
```

Integration tests are collected by the first command but skip themselves unless
`INTEGRATION_DATABASE_URL` is set.

## API

| Method | Path                | Purpose                         | Auth |
|--------|---------------------|---------------------------------|------|
| GET    | /health             | Liveness + active LLM provider  | –    |
| POST   | /auth/signup        | Create an account               | –    |
| POST   | /auth/login         | Email + password → JWT          | –    |
| GET    | /auth/me            | Identity of the current token   | ✅   |
| POST   | /upload             | Profile CSV/Excel/Parquet       | ✅   |
| POST   | /connect-database   | Introspect a SQL database       | ✅   |
| GET    | /schema             | Return cached schema            | ✅   |
| POST   | /query              | NL question → SQL + result      | ✅   |
| POST   | /chat               | Multi-turn (context-aware)      | ✅   |
| POST   | /generate-report    | PDF report                      | ✅   |

Protected routes take `Authorization: Bearer <token>`.

## Demo path with real data

The repo ships a loader for the UCI *Online Retail II* dataset (not bundled —
download `online_retail_II.xlsx` from
[UCI](https://archive.ics.uci.edu/dataset/502/online+retail+ii)):

```bash
docker compose up -d db
export DATABASE_URL="postgresql+psycopg2://postgres:postgres@localhost:5432/analytics"
python scripts/load_online_retail.py /path/to/online_retail_II.xlsx

export INTEGRATION_DATABASE_URL="$DATABASE_URL"
pytest tests/integration -m integration -q
```

Full walkthrough, including what the LLM step does and does not verify:
docs/DEVELOPMENT_GUIDE.md.

## Status (honest)

Verified by unit tests (no DB, no network, no LLM): SQL safety validation,
dataset profiling, chart selection, RAG chunking + retrieval (in-memory fallback
store), **password hashing, JWT issue/verify, signup/login, and rejection of
unauthenticated calls to the protected routes**, and the Online Retail cleaning
rules.

**Authentication is implemented** — `backend/api/auth.py` provides
`/auth/signup`, `/auth/login` (passlib/bcrypt + PyJWT), and a `get_current_user`
dependency guarding every non-public route. Only `/health` and the two auth
endpoints are open. Authorization is *not* implemented: no roles, no per-user
data scoping, no token revocation/refresh, no login rate limiting, and the schema
cache is shared across all users. `/connect-database` also remains an SSRF
primitive for any logged-in user. See "Known gaps" in docs/API_DOCUMENTATION.md.

Verified by the opt-in integration suite against the real dataset: the loader
reads all 1,067,371 rows of `online_retail_II.xlsx`, drops 19,494 cancellations,
and loads 1,047,877 rows; the validate → execute → chart path then returns real
results on them. Caveat: that run used SQLite, so Postgres-specific behaviour is
still unexercised — see docs/DEVELOPMENT_GUIDE.md for the recorded output.

Wired but unverified in this repo's automated runs: the NL → SQL step, which
needs a real LLM key or a local Ollama model. It is deliberately not mocked —
a stubbed model returning SQL we wrote ourselves would not demonstrate
generation. ChromaDB is still stubbed to the in-memory store.

## Docs
- docs/ARCHITECTURE.md
- docs/API_DOCUMENTATION.md
- docs/DEVELOPMENT_GUIDE.md
