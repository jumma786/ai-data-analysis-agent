# Development Guide

## Environment
- Python 3.11+. `pip install -r requirements.txt`.
- Copy `.env.example` → `.env`. Set `LLM_PROVIDER=openai` (+ key) or `ollama`.

## Running tests
`pytest tests/ -q`. The suite avoids external services by design; add
integration tests behind markers that require a live DB/LLM.

## Known TODOs (be honest in interviews)
- **Authentication**: `User` model + `passlib` are present, but signup/login
  endpoints and JWT middleware are not yet implemented. Add an `auth.py` router.
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
