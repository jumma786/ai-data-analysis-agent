# Deployment

Written against the free-tier layout this repository is configured for: the
backend on Render (`render.yaml`), the frontend on Streamlit Community Cloud,
Postgres on Neon.

Both images (`docker/Dockerfile.backend`, `docker/Dockerfile.frontend`) read
`$PORT` at runtime, so Railway, Fly.io and Cloud Run differ only in how you set
environment variables and attach volumes. `railway.backend.json` and
`railway.frontend.json` are kept for the all-on-one-host alternative noted under
each service below.

`docker-compose.yml` is for local development only. It hardcodes
`POSTGRES_PASSWORD: postgres` and reaches the backend at the compose-internal
hostname `backend`, neither of which survives contact with a real host.

---

## Topology

The frontend is Streamlit, so it calls the API **server-side** — the browser
never talks to the backend directly. That means no CORS configuration is needed
under either layout below.

Free tier, the split the repository is configured for:

```
[frontend]  ──HTTPS──> [backend]  ──> analytics DB   (read-only role)
 Community              Render     └─> metadata DB   (read-write)
 Cloud                  public          both on Neon
```

Two providers, so the two services reach each other over the internet and
**both** need a public domain. The backend's exposure is not incidental — see
*Frontend service* below for what it costs.

All on one host, the `railway.*.json` layout:

```
[frontend] ──HTTP──> [backend] ──> analytics DB   (read-only role)
  public              internal  └─> metadata DB   (read-write)
```

Here only the frontend needs a public domain and the backend stays on the
internal network, which is strictly the safer arrangement.

One Postgres instance with two databases is fine and halves the cost. Keep the
two *roles* separate regardless — that separation is what keeps generated SQL
from being able to write anything.

---

## 1. Databases

Create two databases, `analytics` and `app_metadata`. On Neon that is one
project holding both.

Neon rather than Render's own Postgres, because Render deletes a free database
30 days after creation — precisely the silent data loss `ENVIRONMENT=production`
exists to refuse. `render.yaml` therefore provisions no database and leaves both
URLs to the dashboard.

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

Render: *New → Blueprint*, pointed at the repository. It reads `render.yaml` —
one service, building `docker/Dockerfile.backend` and health-checking `/health`.
The build context is the repository root rather than `docker/`, because the
Dockerfile copies `requirements.txt` and the `backend` package from there.

The Dockerfile's `CMD` is deliberately left in place rather than overridden by a
start command, so the `$PORT` handling lives in exactly one spot. Render injects
`$PORT` the same way Railway does.

The blueprint sets `ENVIRONMENT=production` and generates `JWT_SECRET_KEY`
itself. The four variables marked `sync: false` are prompted for when the
blueprint is applied and are never stored in the repository; anything else in
the table below is optional and goes in the dashboard.

Generate a public domain for the service — under this layout Community Cloud
reaches it over the internet.

**Free instances sleep.** After 15 minutes without traffic the instance spins
down and the next request takes about a minute to wake it. That is the price of
the tier, not a fault in the app, but it has one visible consequence: the
Dashboard page health-checks with `timeout=5` (`streamlit_app.py`), so the first
visit after an idle period reliably reports *Backend unreachable* — rerun it once
the instance is up. The calls that matter (login, query, reports) pass no
timeout and simply wait.

**On Railway instead:** point the service at `railway.backend.json` (*Settings →
Config as code*). Same Dockerfile, same health check. Railway does not sleep,
and can keep the backend off the public internet entirely.

## 3. Frontend service

Streamlit Community Cloud runs a single `streamlit run` process, so it can host
the frontend but not the backend.

| Setting | Value |
|---|---|
| Repository / branch | `jumma786/ai-data-analysis-agent`, `main` |
| Main file path | `frontend/streamlit_app.py` |
| Python version | 3.11, matching CI |
| Secrets (*Advanced settings*) | `API_URL = "https://<service>.onrender.com"` |

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
the internet, so the backend has a public domain and will receive unsolicited
traffic. Two consequences:

- `ENVIRONMENT=production` and a non-empty `ALLOWED_DATABASE_HOSTS` stop being
  good practice and become the things standing between an internet-wide scanner
  and `/connect-database`. The startup guard refuses to boot without them.
- Signup is open to anyone who finds the backend URL. There is no invite code
  and no admin role (see the README's known limitations), so treat any public
  instance as a demo holding no data you would mind a stranger seeing.

CORS still needs no configuration: Streamlit calls the API from the server side,
so the browser never talks to the backend directly.

**On Railway instead:** point a second service at `railway.frontend.json`, which
health-checks `/_stcore/health`, Streamlit's built-in probe. `API_URL` must then
be set to the backend's internal URL — `Dockerfile.frontend` bakes in
`API_URL=http://backend:8000` as a compose default, and off compose that
hostname does not resolve and every request fails.

## 4. Smoke test

1. `GET /health` on the backend — on a sleeping free instance, allow a minute
2. Sign up, then log in from the frontend
3. Ask a question that produces a chart
4. Generate a PDF report and download it
5. **Redeploy the backend, then log in again** — this is what catches the
   ephemeral-storage traps described below

---

## Environment variables

Set on the **backend** service. The *Blueprint* column says how `render.yaml`
handles each one: `set` is a literal value in the file, `generated` is a random
value Render creates, `prompted` is `sync: false` — asked for at apply time and
never committed. The rest are not in the blueprint at all; add them in the
dashboard if you need them.

| Variable | Value | Blueprint | Notes |
|---|---|---|---|
| `ENVIRONMENT` | `production` | set | Enables the startup guard — set this first |
| `JWT_SECRET_KEY` | 64-byte random string | generated | Elsewhere: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `METADATA_DATABASE_URL` | Postgres URL, read-write | prompted | **Do not leave as default** — see below |
| `DATABASE_URL` | Postgres URL, read-only role | prompted | The analytics database |
| `ALLOWED_DATABASE_HOSTS` | e.g. `db.internal,analytics.internal` | prompted | **Do not leave empty** — see below |
| `OPENAI_API_KEY` | a real key | prompted | |
| `OPENAI_MODEL` | `gpt-4o-mini` | — | Defaults to the same value in `config.py` |
| `REDIS_URL` | Redis URL | — | Required before scaling past one replica |
| `VECTOR_STORE` | `chroma` | — | Only if RAG uploads must persist |
| `CHROMA_PERSIST_DIR` | path on a mounted volume | — | Required when `VECTOR_STORE=chroma` |

Set on the **frontend**: `API_URL`, the backend's public URL. On Community
Cloud that is a *secret*, `API_URL = "https://<service>.onrender.com"`, not an
environment variable; on a container host it is an ordinary environment
variable.

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

On Render's free plan there is no volume to mount — persistent disks require a
paid instance — so uploaded documents cannot be made durable there at all. Leave
`VECTOR_STORE` at `memory` and treat document search as per-session, or move the
service to a plan that has a disk.

This one **warns rather than blocks**, deliberately: a deployment that never
touches the RAG routes is entitled to the in-memory store, and a check that
refuses a legitimate configuration is a check people switch off — at which
point it protects nothing.

---

## Scaling past one replica

Both `railway.*.json` files pin `numReplicas: 1` deliberately, and Render's free
plan gives one instance regardless — so this section only becomes relevant when
you move off the free tier.

`redis_url` defaults to empty, which selects an in-process rate limiter. Login
throttling (`login_max_attempts`, default 5) is then enforced per process, so
with N replicas an attacker gets N times the allowance and the limit means
little. Set `REDIS_URL` before raising the replica count.

JWT signing is symmetric HS256, so every replica needs the *same*
`JWT_SECRET_KEY` or tokens issued by one will not verify on another.
