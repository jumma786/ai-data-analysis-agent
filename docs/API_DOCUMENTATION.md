# API Documentation

Base URL: `http://localhost:8000`

## Authentication

Signup and login are implemented (`backend/api/auth.py`). Tokens are HS256 JWTs
signed with `JWT_SECRET_KEY`; they carry the user's email in `sub` and expire
after `ACCESS_TOKEN_EXPIRE_MINUTES` (default 60).

Protected endpoints require a bearer token:

```
Authorization: Bearer <access_token>
```

Any missing, malformed, expired, or unknown-user token returns
`401 {"detail": "Invalid or missing credentials."}`.

| Endpoint            | Auth required |
|---------------------|---------------|
| GET  /health        | no            |
| POST /auth/signup   | no            |
| POST /auth/login    | no            |
| POST /auth/refresh  | no (the refresh token *is* the credential) |
| POST /auth/logout   | no (same)     |
| GET  /auth/me       | **yes**       |
| POST /upload        | **yes**       |
| POST /query         | **yes**       |
| POST /chat          | **yes**       |
| POST /generate-report | **yes**     |
| POST /connect-database | **yes**    |
| GET  /schema        | **yes**       |
| POST /documents/upload | **yes**    |
| POST /documents/query  | **yes**    |
| GET  /documents/status | **yes**    |

### Token model

Two token types, distinguished by a `type` claim so one cannot be replayed as
the other:

| | Lifetime | Stateless? | Revocable? |
|---|---|---|---|
| **Access** | `ACCESS_TOKEN_EXPIRE_MINUTES` (60) | yes — no DB read per request | no, until expiry |
| **Refresh** | `REFRESH_TOKEN_EXPIRE_DAYS` (14) | no — `jti` recorded | **yes** |

Refreshing **rotates**: the presented refresh token is revoked as the new pair
is issued, so replaying it returns 401. Only the `jti` is stored, never the
token itself.

### POST /auth/signup
Body: `{ "email": "analyst@example.com", "password": "at least 8 chars" }`.
Emails are normalized to lowercase. Passwords are bcrypt-hashed via passlib and
must be 8–72 characters (72 bytes is bcrypt's hard limit).

- `201` → `{ "id": 1, "email": "...", "created_at": "..." }`
- `409` → email already registered
- `422` → invalid email or password length

Signup does **not** return a token; call `/auth/login` next.

### POST /auth/login
Body: `{ "email": "...", "password": "..." }`.

- `200` → `{ "access_token": "...", "refresh_token": "...", "token_type": "bearer", "expires_in": 3600 }`
- `401` → wrong password or unknown email (deliberately indistinguishable)
- `429` → too many failed attempts; includes a `Retry-After` header

Failed logins are throttled per `(email, client IP)` — `LOGIN_MAX_ATTEMPTS`
within `LOGIN_WINDOW_SECONDS`. A successful login clears the counter. The
counters are in-process: with multiple workers the effective limit multiplies,
so this raises the cost of online guessing rather than eliminating it.

### POST /auth/refresh
Body: `{ "refresh_token": "..." }`. Returns a new pair and revokes the one
presented.

- `200` → a fresh `TokenResponse`
- `401` → unknown, expired, already-rotated, or revoked token; also returned if
  an *access* token is supplied here

### POST /auth/logout
Body: `{ "refresh_token": "..." }`. Revokes it.

- `204` → always, whether or not the token existed, so this cannot be used to
  probe which tokens are valid

Access tokens already issued remain valid until they expire; that is the
documented cost of keeping them stateless.

### GET /auth/me
Returns the caller's `{ id, email, created_at }`. Useful for checking a token by
hand.

## GET /health
Returns `{ "status": "ok", "provider": "openai" }`. Public, for liveness probes.

## POST /upload  (multipart, auth required)
Field `file`: CSV/XLSX/Parquet. Returns profile: rows, columns_count,
missing_total, duplicate_records, per-column stats, rows_after_dedup.

## POST /connect-database  (auth required)
Body: `{ "database_url": "postgresql+psycopg2://..." }`. Returns the introspected
schema text and caches it **for the calling user only**.

- `403` → the URL's host is not in `ALLOWED_DATABASE_HOSTS`
- `400` → the connection or introspection failed

`ALLOWED_DATABASE_HOSTS` is empty by default, which permits any host — fine
locally, not for a deployment. See "Known gaps" for what the allowlist does and
does not cover.

## GET /schema  (auth required)
Returns the schema cached for the calling user, or `""`.

## POST /documents/upload  (multipart, auth required)
Field `file`: TXT/PDF/DOCX. Extracts text, chunks it, and stores it in the
caller's own vector store.

- `201` → `{ "filename", "chunks_added", "document_count", "backend" }`
- `400` → unsupported type, unreadable file, or no extractable text (scanned
  PDFs need OCR, which is not implemented)

## POST /documents/query  (auth required)
Body: `{ "question": "...", "top_k": 4 }` (`top_k` is 1–20).
Returns the answer **and the retrieved chunks**, so a response can be checked
against its sources. With nothing ingested, returns `answer: null` and an empty
`chunks` list rather than inventing an answer.

## GET /documents/status  (auth required)
`{ "document_count": n, "backend": "memory" | "chroma" }`.

## POST /query  (auth required)
Body: `{ "question": "...", "schema_text": "", "history": "" }`.
Returns: sql, valid, error?, chart, insight, row_count, rows (first 100).

## POST /chat  (auth required)
Body: `{ "messages": [{ "role": "...", "content": "..." }], "schema_text": "" }`.
Uses prior messages as context for the last question.

## POST /generate-report  (auth required)
Body: same as /query. Returns `{ "report_path": "..." }` to a generated PDF.
Embeds the chart as a PNG when `kaleido` is installed; without it the report
still builds, with a note in place of the image.

## Known gaps

What this API still does *not* do, stated plainly:

- **No roles or permissions.** Every persisted resource is owner-scoped —
  reports check ownership before serving, documents and the schema cache are
  partitioned by user id — but all authenticated users have identical rights.
  There is no admin, no sharing, and no role hierarchy.
- **The allowlist is not a complete SSRF defence.** It matches the hostname as
  written, then resolves DNS and rejects known cloud-metadata addresses
  (169.254.169.254 and similar) even for an allowlisted name. It deliberately
  does not extend that check to ordinary private ranges, where self-hosted
  databases legitimately live. Resolution happens at check time, not when the
  driver actually connects, so DNS rebinding in that window is still
  unaddressed. It is also empty by default, which permits any hostname.
- **Access tokens cannot be revoked.** Only refresh tokens are tracked. A
  stolen access token works until it expires — bound the exposure with a short
  `ACCESS_TOKEN_EXPIRE_MINUTES`.
- **Rate limiting is per-process unless `REDIS_URL` is set.** Without it,
  multiple workers multiply the effective limit and a restart clears the
  counters. With it, limits are shared across workers and survive a restart —
  though they still only raise the cost of guessing, rather than defending
  against an attacker spread across many source IPs.
- **Per-user state is process-local.** The schema cache and in-memory document
  stores do not survive a restart and are not shared between workers. Use
  `VECTOR_STORE=chroma` for documents that need to persist.
- **No statement timeout on SQLite.** SQLite has no server-side equivalent, so
  queries against it are uncapped; `services/db.py` logs a warning rather than
  implying otherwise. PostgreSQL and MySQL are capped.
