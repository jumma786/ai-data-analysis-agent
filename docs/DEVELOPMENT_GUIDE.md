# Development Guide

## Environment
- Python 3.11+. `pip install -r requirements.txt`.
- Copy `.env.example` → `.env`. Set `LLM_PROVIDER=openai` (+ key) or `ollama`.
- Auth settings (see `backend/utils/config.py`):
  - `JWT_SECRET_KEY` — required in any real deployment. If unset the app
    generates a random per-process key and logs a warning; tokens then die on
    restart and cannot be verified by a second worker.
  - `JWT_ALGORITHM` (default `HS256`), `ACCESS_TOKEN_EXPIRE_MINUTES` (default 60).
  - `METADATA_DATABASE_URL` — where users/datasets/conversations live. Defaults
    to `sqlite:///./app_metadata.db`. Kept separate from `DATABASE_URL` so the
    analytics connection can use a read-only role.

## Running tests

`pytest tests/ -q` runs the unit suite. It needs no database, no network and no
LLM key: the auth tests use in-memory SQLite, and the retail-loader tests use
small in-memory frames.

Integration tests live in `tests/integration/`, carry `@pytest.mark.integration`,
and skip themselves unless `INTEGRATION_DATABASE_URL` is set. They are collected
by `pytest tests/ -q` but always report as skipped there.

## End-to-end demo path (real data)

1. **Start Postgres.** Create `.env` first — `docker-compose.yml` declares
   `env_file: .env` on the backend service, and Compose validates the whole file
   even when you only target `db`, so without it the command fails with
   `Failed to load .env`:

   ```bash
   cp .env.example .env
   docker compose up -d db
   ```

   To skip Compose entirely (no `.env` needed):

   ```bash
   docker run -d --name adaa-pg -e POSTGRES_USER=postgres \
     -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=analytics \
     -p 5432:5432 postgres:16
   ```

2. **Download the dataset** — UCI *Online Retail II*, `online_retail_II.xlsx`,
   from https://archive.ics.uci.edu/dataset/502/online+retail+ii. It is not
   committed to this repo.

3. **Load it**:

   ```bash
   pip install psycopg2-binary   # if not already installed from requirements
   export DATABASE_URL="postgresql+psycopg2://postgres:postgres@localhost:5432/analytics"
   python scripts/load_online_retail.py /path/to/online_retail_II.xlsx
   ```

   The script normalizes column names, drops rows with a null `Invoice` or
   `StockCode`, drops cancelled invoices (`C`-prefixed), derives
   `revenue = quantity * price`, and prints the row count it read back from the
   database. Returns (negative quantities) are kept.

4. **Run the integration tests**:

   ```bash
   export INTEGRATION_DATABASE_URL="$DATABASE_URL"
   pytest tests/integration -m integration -q
   ```

   Both URLs must point at the same database — the pipeline executes against
   `DATABASE_URL` from settings, while the tests connect directly via
   `INTEGRATION_DATABASE_URL`.

   Recorded result of an actual run (2026-08-01) against `online_retail_II.xlsx`,
   into Postgres 16. Byte-identical counts were produced by a SQLite run, so the
   cleaning is driver-independent:

   ```
   Found 2 sheet(s): ['Year 2009-2010', 'Year 2010-2011']
   Read 1067371 raw row(s).            # matches the published UCI row count
   Dropped 0 row(s) with a null invoice/stock_code.
   Dropped 19494 cancelled invoice row(s).
   Loaded 1047877 rows into table 'online_retail'.
   ```

   `pytest tests/integration -m integration -q` then gave `2 passed, 1 skipped`
   (the skip being the LLM test). "Total revenue by country" returns United
   Kingdom 17,712,301.64 ahead of EIRE 664,431.78 and the Netherlands
   554,232.34, over an invoice range of 2009-12-01 to 2011-12-09.

   The full NL → SQL test additionally needs a real provider (`OPENAI_API_KEY`,
   or `LLM_PROVIDER=ollama` plus `INTEGRATION_ALLOW_OLLAMA=1`). Without one it
   skips, and the SQL-generation step remains unverified — the LLM is
   deliberately not mocked there, since canned SQL would prove nothing about
   generation.

   It has been run for real: with `LLM_PROVIDER=ollama`, `OLLAMA_MODEL=qwen3:4b`
   and `LLM_TIMEOUT_SECONDS=900`, all three integration tests pass in ~3m17s.
   Asked "total revenue by country" against the introspected schema, qwen3:4b
   produced, unedited:

   ```sql
   SELECT country, SUM(revenue) AS total_revenue FROM online_retail
   GROUP BY country ORDER BY total_revenue DESC LIMIT 10
   ```

   **Raise `LLM_TIMEOUT_SECONDS` for local models.** The 120s default is sized
   for a hosted API. Measured on CPU, qwen3:4b needs ~35s to cold-load and ~77s
   per generation, and the pipeline makes two sequential calls — so the default
   times out. This is a hardware-dependent number, not a benchmark; measure your
   own rather than trusting these figures.

## Known TODOs (be honest in interviews)
- **Authorization**: authentication is done (signup/login/JWT dependency, see
  `backend/api/auth.py`) and every non-public route is guarded, but there are no
  roles, no per-user data scoping, no token revocation/refresh, and no login rate
  limiting.
- **Schema cache is global**: `_SCHEMA_CACHE` in `backend/main.py` is one entry
  shared by all users. Key it per user before a second person uses an instance.
- **`/connect-database` SSRF**: a logged-in user can point the server at any URL.
  Add a host allowlist. See "Known gaps" in API_DOCUMENTATION.md.
- **ChromaDB**: production path is stubbed to the in-memory store; wire the
  Chroma client in `rag/pipeline.build_store`.
- **Query timeout**: config value exists; enforce it via DB driver options
  (e.g. `options=-c statement_timeout=30000` for Postgres).
- **MySQL / SQL Server**: introspection uses SQLAlchemy so it should work, but
  is only tested against SQLite/Postgres-style URLs.
- **Report charts**: current PDF embeds text + KPIs; embedding the Plotly figure
  as an image (kaleido) is a follow-up.

## Coding standards
Type hints, docstrings, module-level logging, small pure functions for anything
testable (validation, chart choice, profiling).

## Cloud deployment sketch
- **AWS**: ECS Fargate (backend + frontend tasks), RDS Postgres, ALB.
- **Azure**: Container Apps + Azure Database for PostgreSQL.
- **GCP**: Cloud Run (two services) + Cloud SQL.
Build images from `docker/Dockerfile.*`; inject env via the platform secret store.
