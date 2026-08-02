# AI Data Analysis Agent

Ask a database questions in plain English. A LangGraph multi-agent pipeline
turns the question into SQL, **refuses to run it if it isn't read-only**,
executes it, picks an appropriate chart, and explains the result in grounded
prose.

![Python](https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-1C3C3C)
![Postgres](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![Tests](https://img.shields.io/badge/tests-105%20passing-success)
![Licence](https://img.shields.io/badge/licence-MIT-blue)

```
"total revenue by country"
        │
        ▼
SELECT country, SUM(revenue) AS total_revenue
FROM online_retail GROUP BY country ORDER BY total_revenue DESC LIMIT 10
        │
        ▼
United Kingdom  17,712,301.64
EIRE               664,431.78
Netherlands        554,232.34
```

That SQL is verbatim output from a local `qwen3:4b` against the real UCI Online
Retail II dataset (1,047,877 rows in Postgres 16) — not a mock, not an
illustration. See [Verification](#verification).

![Chat With Data — question, grounded insight, generated SQL and live results](docs/images/chat-with-data.jpg)

*Unretouched screenshot: `qwen3:4b` answering "top 5 countries by revenue" against
the loaded dataset. The figures match a hand-written reference query exactly.*

---

## Contents

- [Why this is built the way it is](#why-this-is-built-the-way-it-is)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Demo with real data](#demo-with-real-data)
- [API](#api)
- [Configuration](#configuration)
- [Testing](#testing)
- [Verification](#verification)
- [Known limitations](#known-limitations)
- [Project layout](#project-layout)

---

## Why this is built the way it is

The interesting parts of this project are the constraints, not the feature list.

**An LLM is never trusted with a database connection.** Generated SQL passes
through a separate validation agent before execution: statement-count check to
reject stacked queries, a keyword denylist, read-only enforcement, and comment
stripping so keywords can't hide behind `--`. Validation failure short-circuits
the graph — unsafe SQL is never executed, not merely logged. This is a guardrail
layered *on top of* a least-privilege read-only database role, not a substitute
for one.

**Deterministic work is kept out of the model.** Chart selection is a pure
heuristic over dtypes and cardinality, so it is unit-testable and never
hallucinates. Insight generation is fed *computed statistics* rather than raw
rows, which narrows the surface for invented figures.

**The LLM is never mocked in tests that claim the pipeline works.** A stubbed
model returning SQL we wrote ourselves would assert that our own fixture parses
— nothing about generation. Tests that need a model skip loudly instead, and the
docstrings say so. This decision caught a real bug: a hardcoded 120s timeout
that no mocked test could ever have surfaced.

**Every external dependency is optional at import time.** The pipeline falls
back to a sequential runner without LangGraph, and to an in-memory cosine store
without ChromaDB. The unit suite therefore runs with no database, no network and
no API key — which is what makes it trustworthy in CI.

**Two databases, deliberately.** Application metadata (users, datasets) lives in
`METADATA_DATABASE_URL`, separate from the analytics database in `DATABASE_URL`,
so the analytics connection can stay read-only.

---

## Architecture

```
Streamlit UI ──HTTP──> FastAPI ──> LangGraph pipeline
    │                    │
  JWT ─────────────► get_current_user
                         │
   schema → generate_sql → validate_sql ──(unsafe)──► stop, return error
                                  │ (safe)
                          execute → analyze → insight
```

| Agent | Responsibility | Deterministic? |
|-------|----------------|----------------|
| **SQL Agent** | NL → a single `SELECT` | no (LLM) |
| **SQL Validation Agent** | Blocks DDL/DML, stacked statements, hidden keywords | **yes** |
| **Analysis Agent** | KPIs and summary statistics | **yes** |
| **Visualization Agent** | Chart-type choice from dtypes/cardinality | **yes** |
| **Insight Agent** | Explanation grounded in computed stats | no (LLM) |
| **Report Agent** | PDF via reportlab | **yes** |

The LLM layer (`backend/services/llm.py`) is a two-method interface, so swapping
OpenAI ↔ Ollama is a config change with no edits to agent code.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # set OPENAI_API_KEY, or LLM_PROVIDER=ollama

uvicorn backend.main:app --reload           # API  → :8000
streamlit run frontend/streamlit_app.py     # UI   → :8501
```

Or the whole stack:

```bash
docker compose up --build     # db + backend + frontend
```

Create an account, then use the token:

```bash
curl -X POST localhost:8000/auth/signup \
  -H 'Content-Type: application/json' \
  -d '{"email":"analyst@example.com","password":"correct horse battery"}'

TOKEN=$(curl -sX POST localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"analyst@example.com","password":"correct horse battery"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

curl -X POST localhost:8000/query -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"question":"total revenue by country"}'
```

---

## Demo with real data

The loader targets the UCI *Online Retail II* dataset. It is not bundled —
download `online_retail_II.xlsx` from
[UCI](https://archive.ics.uci.edu/dataset/502/online+retail+ii) (~44 MB).

```bash
cp .env.example .env          # compose validates env_file even for `up db`
docker compose up -d db

export DATABASE_URL="postgresql+psycopg2://postgres:postgres@localhost:5432/analytics"
python scripts/load_online_retail.py /path/to/online_retail_II.xlsx

export INTEGRATION_DATABASE_URL="$DATABASE_URL"
pytest tests/integration -m integration -q
```

The loader reads both workbook sheets, normalises column names (handling both
the *Online Retail II* and legacy *Online Retail* schemas), drops rows with a
null `Invoice`/`StockCode`, drops `C`-prefixed cancellations, and derives
`revenue = quantity × price`. Returns — negative quantities — are kept
deliberately, so `revenue` can be negative; filter them if you want gross rather
than net.

Full walkthrough with recorded output: [docs/DEVELOPMENT_GUIDE.md](docs/DEVELOPMENT_GUIDE.md).

---

## API

| Method | Path | Purpose | Auth |
|--------|------|---------|------|
| `GET`  | `/health` | Liveness + active LLM provider | – |
| `POST` | `/auth/signup` | Create an account | – |
| `POST` | `/auth/login` | Email + password → access + refresh tokens | – |
| `POST` | `/auth/refresh` | Rotate a refresh token for a new pair | – |
| `POST` | `/auth/logout` | Revoke a refresh token | – |
| `GET`  | `/auth/me` | Identity of the current token | ✅ |
| `POST` | `/upload` | Profile CSV/Excel/Parquet | ✅ |
| `POST` | `/connect-database` | Introspect a SQL database (host-allowlisted) | ✅ |
| `GET`  | `/schema` | Schema cached for *this* user | ✅ |
| `POST` | `/query` | NL question → SQL + result + chart + insight | ✅ |
| `POST` | `/chat` | Multi-turn, context-aware | ✅ |
| `POST` | `/generate-report` | PDF report with embedded chart | ✅ |
| `POST` | `/documents/upload` | Ingest TXT/PDF/DOCX into your RAG store | ✅ |
| `POST` | `/documents/query` | Answer from your documents, with sources | ✅ |
| `GET`  | `/documents/status` | Chunks stored + active backend | ✅ |

### Document Q&A

![Document Search — answer with the retrieved source chunk expanded](docs/images/document-search.jpg)

*Answers cite their sources. Asked "how long do I have to get my money back?" —
wording that appears nowhere in the document — it retrieved the relevant chunk
and answered from it. The expander shows exactly what the answer was built from,
so it can be checked rather than trusted.*

Protected routes take `Authorization: Bearer <access_token>`. Access tokens are
short-lived and stateless; refresh tokens are tracked server-side by `jti`,
**rotate on use**, and can be revoked. Full reference, including error codes and
an explicit "Known gaps" section:
[docs/API_DOCUMENTATION.md](docs/API_DOCUMENTATION.md).

---

## Configuration

All via environment or `.env` (see `backend/utils/config.py`).

| Variable | Default | Notes |
|----------|---------|-------|
| `LLM_PROVIDER` | `openai` | `openai` \| `ollama` |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | – / `gpt-4o-mini` | |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | `localhost:11434` / `llama3` | |
| `LLM_TIMEOUT_SECONDS` | `120` | **Raise well above this for local models** |
| `DATABASE_URL` | local Postgres | Analytics DB; use a read-only role |
| `METADATA_DATABASE_URL` | `sqlite:///./app_metadata.db` | Users, datasets |
| `JWT_SECRET_KEY` | – | **Required in deployment**; see below |
| `JWT_ALGORITHM` / `ACCESS_TOKEN_EXPIRE_MINUTES` | `HS256` / `60` | |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `14` | Refresh tokens are revocable |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_WINDOW_SECONDS` | `5` / `300` | Failed logins, per (email, IP) |
| `ALLOWED_DATABASE_HOSTS` | – | Comma-separated. **Empty permits any host** |
| `QUERY_TIMEOUT_SECONDS` | `30` | Enforced as a driver-level statement timeout |
| `VECTOR_STORE` | `memory` | `memory` \| `chroma` |
| `CHROMA_PERSIST_DIR` / `CHROMA_COLLECTION` | `./chroma_data` / `documents` | |
| `MAX_RESULT_ROWS` | `5000` | Appended as `LIMIT` when a query lacks one |

> **`JWT_SECRET_KEY` matters.** Left empty, the app generates a random
> per-process key and logs a warning: every token dies on restart, and a second
> worker cannot verify tokens issued by the first. Set shorter than 32 bytes and
> it warns too — RFC 7518 §3.2, since a short HS256 key can be brute-forced
> offline from one captured token. Generate one with:
> `python -c "import secrets; print(secrets.token_urlsafe(48))"`

---

## Testing

```bash
pytest tests/ -q                              # 105 passed, 14 skipped
pytest tests/integration -m integration -q    # needs INTEGRATION_DATABASE_URL
RUN_CHROMA_TESTS=1 pytest tests/test_rag_chroma.py -q   # 11 passed
```

The default run needs **no database, no network and no API key**. The 14 skips
are the 3 integration tests and 11 Chroma tests, which announce their skip
reasons rather than passing vacuously.

---

## Verification

What is actually proven, and by what. Claims are limited to what was executed.

| Capability | Verified by | Evidence |
|---|---|---|
| SQL safety validation | Unit tests | 10 tests: DDL/DML, stacked statements, comment-hidden keywords |
| Dataset profiling, chart choice | Unit tests | Deterministic, no external services |
| Auth: hashing, JWT, signup/login, route guards, refresh rotation, revocation, throttling | Unit tests | 41 tests on in-memory SQLite; replayed refresh tokens 401, refresh tokens rejected as access credentials |
| SSRF allowlist, statement timeouts, rate limiter | Unit tests | 24 tests, incl. cloud-metadata host blocked |
| RAG routes + per-user isolation | Unit tests | 10 tests; one user's documents are invisible to another |
| Loader cleaning rules | Unit tests | 10 tests on synthetic frames |
| Loader against the real dataset | Executed | 1,067,371 rows read → 19,494 cancellations dropped → **1,047,877 loaded** into Postgres 16; SQLite run gave identical counts |
| Query path on real rows | Integration suite | validate → execute → chart against the loaded table |
| **NL → SQL** | Integration suite, live model | `qwen3:4b` produced a correct aggregate that passed validation and returned rows. **Not mocked.** |
| Chroma vector store | 11 unit tests + manual check | Adapter tests use a stub embedder; real all-MiniLM-L6-v2 retrieval confirmed separately |

**Deliberately not claimed.** The NL → SQL result is one question against one
small local model. It demonstrates the path works end to end; it is **not** an
accuracy measurement, and no such metric is offered. Timing figures in the docs
are from one machine and are not benchmarks.

---

## Known limitations

Stated plainly, because a portfolio project that hides these is less useful than
one that doesn't.

**Security**
- **No authorization model.** Authentication is complete; *authorization* is
  not. Every authenticated user has identical rights — no roles, no permissions.
  `Dataset.owner_id` exists and nothing reads it.
- **The SSRF allowlist is coarse.** It matches the hostname as written without
  resolving DNS, so a permitted name pointing at an internal address still
  passes, and DNS rebinding is unaddressed. It is also empty by default.
- **Access tokens cannot be revoked.** Only refresh tokens are tracked
  server-side; a stolen access token works until it expires.
- **Rate limiting is per-process.** Multiple workers multiply the effective
  limit and a restart clears it. It raises the cost of guessing; it is not a
  defence against a distributed attacker.

**Correctness / scope**
- **Per-user state is process-local.** The schema cache and in-memory document
  stores are lost on restart and not shared between workers. Use
  `VECTOR_STORE=chroma` for documents that must persist.
- **RAG never deletes.** Re-ingesting upserts (chunk ids are content hashes),
  but a removed document's chunks stay retrievable. There is no delete route.
- **No statement timeout on SQLite.** It has no server-side equivalent, so those
  queries are uncapped — a warning is logged rather than implying otherwise.
  PostgreSQL and MySQL are capped.
- **Only Ollama has been exercised.** The OpenAI provider is wired but unrun.

**Environment**
- On Anaconda/Windows, importing pandas makes `onnxruntime` fail to load, which
  breaks Chroma's default embeddings *inside the backend process*. The store is
  sound; the environment is not. One-line check in the development guide.

---

## Project layout

```
backend/
  agents/        sql_agent, sql_validation, analysis_agents, graph (LangGraph)
  api/           auth.py — signup, login, get_current_user dependency
  database/      SQLAlchemy models + metadata session management
  services/      llm.py (provider-agnostic), profiling, schema_introspect, report
  utils/         config, security (hashing + JWT), logging
rag/             chunking, in-memory + Chroma vector stores, retrieval
frontend/        Streamlit client
scripts/         load_online_retail.py — UCI dataset loader
tests/           unit suite; tests/integration/ is opt-in
docs/            ARCHITECTURE, API_DOCUMENTATION, DEVELOPMENT_GUIDE
```

---

## Docs

- [Architecture](docs/ARCHITECTURE.md)
- [API reference](docs/API_DOCUMENTATION.md) — includes explicit "Known gaps"
- [Development guide](docs/DEVELOPMENT_GUIDE.md) — setup, demo path, recorded runs

## Licence

[MIT](LICENSE).
