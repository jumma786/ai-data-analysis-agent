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
| GET  /auth/me       | **yes**       |
| POST /upload        | **yes**       |
| POST /query         | **yes**       |
| POST /chat          | **yes**       |
| POST /generate-report | **yes**     |
| POST /connect-database | **yes**    |
| GET  /schema        | **yes**       |

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

- `200` → `{ "access_token": "...", "token_type": "bearer", "expires_in": 3600 }`
- `401` → wrong password or unknown email (deliberately indistinguishable)

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
schema text and caches it.

## GET /schema  (auth required)
Returns the cached schema string.

## POST /query  (auth required)
Body: `{ "question": "...", "schema_text": "", "history": "" }`.
Returns: sql, valid, error?, chart, insight, row_count, rows (first 100).

## POST /chat  (auth required)
Body: `{ "messages": [{ "role": "...", "content": "..." }], "schema_text": "" }`.
Uses prior messages as context for the last question.

## POST /generate-report  (auth required)
Body: same as /query. Returns `{ "report_path": "..." }` to a generated PDF.

## Known gaps

Being explicit about what the current auth layer does *not* do:

- **`/connect-database` is authenticated but not safe.** A logged-in user can
  still make the server open an outbound connection to any URL they supply,
  which is an SSRF primitive: internal hosts reachable from the API container
  are reachable through it, and connection errors are echoed back in the 400.
  The fix is a host allowlist, not a login check.
- **The schema cache is process-global, not per-user.** Whoever calls
  `/connect-database` last sets the schema every other authenticated user sees
  and queries against. It needs to be keyed by user (or session) before more
  than one person uses an instance.
- **No authorization model.** Every authenticated user has identical access;
  there are no roles and no per-user data scoping. `Dataset.owner_id` exists in
  the model but nothing writes or checks it.
- **No token revocation or refresh.** A leaked token stays valid until it
  expires. There is no logout on the server side and no refresh rotation.
- **No rate limiting or lockout** on `/auth/login`, so credential stuffing is
  only slowed by bcrypt's work factor.
