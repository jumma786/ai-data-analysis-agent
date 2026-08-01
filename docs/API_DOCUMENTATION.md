# API Documentation

Base URL: `http://localhost:8000`

## GET /health
Returns `{ "status": "ok", "provider": "openai" }`.

## POST /upload  (multipart)
Field `file`: CSV/XLSX/Parquet. Returns profile: rows, columns_count,
missing_total, duplicate_records, per-column stats, rows_after_dedup.

## POST /connect-database
Body: `{ "database_url": "postgresql+psycopg2://..." }`. Returns the introspected
schema text and caches it.

## GET /schema
Returns the cached schema string.

## POST /query
Body: `{ "question": "...", "schema_text": "", "history": "" }`.
Returns: sql, valid, error?, chart, insight, row_count, rows (first 100).

## POST /chat
Body: `{ "messages": [{ "role": "...", "content": "..." }], "schema_text": "" }`.
Uses prior messages as context for the last question.

## POST /generate-report
Body: same as /query. Returns `{ "report_path": "..." }` to a generated PDF.
