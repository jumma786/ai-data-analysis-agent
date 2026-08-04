# Deployment

Deploying the two containers (`docker/Dockerfile.backend`, `docker/Dockerfile.frontend`)
to a container host. Written against Railway, since both images already read
`$PORT` at runtime; Render, Fly.io and Cloud Run differ only in how you set
environment variables and attach volumes.

`docker-compose.yml` is for local development only. It hardcodes
`POSTGRES_PASSWORD: postgres` and reaches the backend at the compose-internal
hostname `backend`, neither of which survives contact with a real host.

---

## Topology

The frontend is Streamlit, so it calls the API **server-side** — the browser
never talks to the backend directly. That means no CORS configuration is
needed, and only the frontend needs a public domain.

```
[frontend] ──HTTP──> [backend] ──> analytics DB   (read-only role)
  public              internal  └─> metadata DB   (read-write)
```

One Postgres instance with two databases is fine and halves the cost. Keep the
two *roles* separate regardless — that separation is what keeps generated SQL
from being able to write anything.

---

## 1. Databases

Create two databases, `analytics` and `app_metadata`.

Load the demo dataset into `analytics`:

```bash
python scripts/load_online_retail.py
```

Then create a read-only role for the analytics connection. The agent only ever
issues `SELECT` (see the note on `database_url` in `backend/utils/config.py`),
so nothing more is needed, and this is the main limit on the blast radius of a
generated query:

```sql
CREATE ROLE analytics_ro LOGIN PASSWORD '<strong-password>';
GRANT CONNECT ON DATABASE analytics TO analytics_ro;
GRANT USAGE ON SCHEMA public TO analytics_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO analytics_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO analytics_ro;
```

No migration step is required for the metadata database — the app calls
`Base.metadata.create_all` on startup (`backend/database/session.py`).

## 2. Backend service

Point the service at `railway.backend.json` (Railway: *Settings → Config as
code*). It builds `docker/Dockerfile.backend` and health-checks `/health`.

The Dockerfile's `CMD` is deliberately left in place rather than overridden by a
`startCommand`, so the `$PORT` handling lives in exactly one spot.

Set the environment variables in the table below, then deploy.

## 3. Frontend service

Point the service at `railway.frontend.json`. It health-checks
`/_stcore/health`, Streamlit's built-in probe.

**`API_URL` must be set to the backend's public URL.** `Dockerfile.frontend`
bakes in `API_URL=http://backend:8000` as a compose default; off compose that
hostname does not resolve and every request fails.

### Alternative: frontend on Streamlit Community Cloud

Community Cloud runs a single `streamlit run` process, so it can host the
frontend but not the backend. The backend still needs a container host; only
the frontend service above is replaced.

| Setting | Value |
|---|---|
| Repository / branch | `jumma786/ai-data-analysis-agent`, `main` |
| Main file path | `frontend/streamlit_app.py` |
| Python version | 3.11, matching CI |
| Secrets (*Advanced settings*) | `API_URL = "https://<backend>.up.railway.app"` |

Dependencies come from `frontend/requirements.txt`, not the root file:
Community Cloud accepts a requirements file in the repository root *or* the
entrypoint's directory, and prefers the latter. That is the whole reason the
frontend list exists — the root file would install psycopg2, chromadb,
langgraph and openai into a process that imports none of them.

`API_URL` is read via `st.secrets` with an environment fallback
(`_api_url()` in `streamlit_app.py`), because the Community Cloud docs do not
promise that dashboard secrets appear in the environment.

**This topology exposes the backend.** The all-Railway layout keeps it on the
internal network with no public domain; here the Cloud runner reaches it over
the internet, so you must generate a public domain for the backend service and
it will receive unsolicited traffic. Two consequences:

- `ENVIRONMENT=production` and a non-empty `ALLOWED_DATABASE_HOSTS` stop being
  good practice and become the things standing between an internet-wide scanner
  and `/connect-database`. The startup guard refuses to boot without them.
- Signup is open to anyone who finds the backend URL. There is no invite code
  and no admin role (see the README's known limitations), so treat any public
  instance as a demo holding no data you would mind a stranger seeing.

CORS still needs no configuration: Streamlit calls the API from the server side,
so the browser never talks to the backend directly.

## 4. Smoke test

1. `GET /health` on the backend
2. Sign up, then log in from the frontend
3. Ask a question that produces a chart
4. Generate a PDF report and download it
5. **Redeploy the backend, then log in again** — this is what catches the
   ephemeral-storage traps described below

---

## Environment variables

Set on the **backend** service:

| Variable | Value | Notes |
|---|---|---|
| `ENVIRONMENT` | `production` | Enables the startup guard — set this first |
| `JWT_SECRET_KEY` | 64-byte random string | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `METADATA_DATABASE_URL` | Postgres URL, read-write | **Do not leave as default** — see below |
| `DATABASE_URL` | Postgres URL, read-only role | The analytics database |
| `ALLOWED_DATABASE_HOSTS` | e.g. `db.internal,analytics.internal` | **Do not leave empty** — see below |
| `OPENAI_API_KEY` | a real key | |
| `OPENAI_MODEL` | `gpt-4o-mini` | |
| `REDIS_URL` | Redis URL | Required before scaling past one replica |
| `VECTOR_STORE` | `chroma` | Only if RAG uploads must persist |
| `CHROMA_PERSIST_DIR` | path on a mounted volume | Required when `VECTOR_STORE=chroma` |

Set on the **frontend** service:

| Variable | Value |
|---|---|
| `API_URL` | the backend's public URL |

Supply all of these as platform secrets. Since the `.dockerignore` landed,
`.env` is no longer copied into the images, and it should stay that way.

---

## Traps

Three defaults are chosen for zero-config local demos and are actively wrong in
a deployment. All three fail *silently* — the app starts, serves traffic, and
loses data later.

**Set `ENVIRONMENT=production`.** The first two then refuse to boot rather than
failing silently, and all outstanding problems are reported in one startup so
you fix them in one pass instead of one redeploy each. The third only warns —
see below for why.

### Metadata storage is ephemeral by default

`metadata_database_url` defaults to `sqlite:///./app_metadata.db`, a file inside
the container. Container filesystems do not survive a redeploy, so **every
deploy silently wipes all users, reports and documents.** It
behaves perfectly right up until the first redeploy.

Point `METADATA_DATABASE_URL` at Postgres. This is the single most important
variable on the list.

With `ENVIRONMENT=production` the app refuses to boot rather than accepting
this, raising `UnsafeDeploymentConfig` before any table is created — so the
failure arrives in your deploy logs instead of arriving as missing users a week
later. The check lives in `assert_deployment_safe()`
(`backend/utils/config.py`) and runs from the FastAPI lifespan.

### `/connect-database` will dial anything

`allowed_database_hosts` defaults to empty, which means *allow any host*. Any
authenticated user can then point the server at internal infrastructure. Known
cloud-metadata addresses are always blocked, but nothing else is.

Set `ALLOWED_DATABASE_HOSTS` to an explicit comma-separated list. Under
`ENVIRONMENT=production` an empty allowlist also blocks startup.

### RAG documents vanish on restart

`vector_store` defaults to `memory`, so uploaded documents are lost on every
restart. Setting `VECTOR_STORE=chroma` without also mounting a volume at
`CHROMA_PERSIST_DIR` just relocates the problem — Chroma will write to a
container path that is equally ephemeral.

Chroma also downloads an ONNX embedding model on first use, so expect a slow
first request after switching.

This one **warns rather than blocks**, deliberately: a deployment that never
touches the RAG routes is entitled to the in-memory store, and a check that
refuses a legitimate configuration is a check people switch off — at which
point it protects nothing.

---

## Scaling past one replica

Both `railway.*.json` files pin `numReplicas: 1` deliberately.

`redis_url` defaults to empty, which selects an in-process rate limiter. Login
throttling (`login_max_attempts`, default 5) is then enforced per process, so
with N replicas an attacker gets N times the allowance and the limit means
little. Set `REDIS_URL` before raising the replica count.

JWT signing is symmetric HS256, so every replica needs the *same*
`JWT_SECRET_KEY` or tokens issued by one will not verify on another.
