"""Centralized configuration via environment variables."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Safe to import here: logging_config imports nothing from this package, so
# there is no cycle.
from backend.utils.logging_config import logger


class Settings(BaseSettings):
    app_name: str = "AI Data Analysis Agent"

    # "development" | "production". Several defaults below are chosen for
    # zero-config local demos and lose data when deployed; setting this to
    # "production" turns those from silent traps into a refusal to boot.
    # See assert_deployment_safe().
    environment: str = "development"

    llm_provider: str = "openai"          # "openai" | "ollama"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3"

    # Per-request LLM timeout. 120s is fine for a hosted API, but a local
    # reasoning model on CPU can spend longer than that on a single call --
    # measured at ~35s cold load plus ~77s generation for qwen3:4b -- so this
    # needs to be raisable without editing code.
    llm_timeout_seconds: int = 120

    # Analytics database: the data the user asks questions about. The agent only
    # ever issues SELECTs here; point it at a least-privilege read-only role.
    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/analytics"

    # Application metadata database: users, refresh tokens, reports, documents.
    # Deliberately separate from `database_url` so the analytics connection can
    # stay read-only. Defaults to a local SQLite file for zero-config demos.
    metadata_database_url: str = "sqlite:///./app_metadata.db"

    # Enforced on the analytics connection via driver options -- see
    # backend/services/db.py. A runaway aggregate should be killed by the
    # database, not left to hold a worker open.
    query_timeout_seconds: int = 30
    max_result_rows: int = 5000

    # Comma-separated hosts that /connect-database may dial. Empty means "allow
    # anything", which is only safe for local development: without it, any
    # authenticated user can point the server at internal infrastructure.
    # Example: "localhost,127.0.0.1,db,analytics.internal"
    allowed_database_hosts: str = ""

    # RAG vector store. "memory" is the default on purpose: Chroma's default
    # embedding function downloads an ONNX model on first use, which is a
    # surprising side effect to trigger implicitly and would make the test suite
    # depend on the network. Set "chroma" to opt into the persistent backend.
    vector_store: str = "memory"          # "memory" | "chroma"
    chroma_persist_dir: str = "./chroma_data"
    chroma_collection: str = "documents"

    # Auth. `jwt_secret_key` MUST be set for any deployment where tokens need to
    # survive a restart or be validated by more than one process; when it is
    # empty the app falls back to a per-process random key and logs a warning.
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # Refresh tokens live far longer than access tokens, so unlike access
    # tokens they are tracked server-side and can be revoked on logout.
    refresh_token_expire_days: int = 14

    # Login throttling. Counted per (email, client IP) so one attacker cannot
    # lock every account, and one account cannot be locked from every IP.
    login_max_attempts: int = 5
    login_window_seconds: int = 300

    # When set, rate limiting is backed by Redis instead of an in-process dict,
    # so the limit holds across multiple workers. See utils/rate_limit.py.
    redis_url: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> "Settings":
    return Settings()


class UnsafeDeploymentConfig(RuntimeError):
    """Raised at startup when configuration would silently lose data."""


def deployment_problems(settings: Settings) -> list[str]:
    """Return configuration problems that only matter in a real deployment.

    Split out from the raising wrapper so the checks stay individually
    testable, and so adding one is a single list entry.
    """
    problems: list[str] = []
    if settings.metadata_database_url.startswith("sqlite"):
        problems.append(
            "METADATA_DATABASE_URL is SQLite "
            f"({settings.metadata_database_url!r}), which is a file inside the "
            "container. Container filesystems do not survive a redeploy, so "
            "every deploy would silently discard all users, reports and "
            "documents. Point it at Postgres."
        )
    if not settings.allowed_database_hosts.strip():
        problems.append(
            "ALLOWED_DATABASE_HOSTS is empty, which permits any host. Any "
            "authenticated user could then point /connect-database at internal "
            "infrastructure. Set an explicit comma-separated allowlist."
        )
    return problems


def deployment_warnings(settings: Settings) -> list[str]:
    """Return deployment concerns that are real but not unconditionally wrong.

    Separate from `deployment_problems` on purpose. A check that refuses to
    boot a legitimate deployment gets switched off, and a switched-off check
    protects nothing -- so anything with a defensible production use stays a
    warning.
    """
    warnings: list[str] = []
    if settings.vector_store == "memory":
        warnings.append(
            "VECTOR_STORE=memory: uploaded documents are held in process and "
            "lost on every restart. Set VECTOR_STORE=chroma with a persistent "
            "CHROMA_PERSIST_DIR if the RAG features are in use. Harmless if "
            "they are not."
        )
    return warnings


def assert_deployment_safe(settings: Settings | None = None) -> None:
    """Refuse to start when ENVIRONMENT=production and a demo default is live.

    These defaults fail silently -- the app boots, serves traffic normally, and
    loses the data later -- so a warning in the log is too weak. Only checked
    under ENVIRONMENT=production so local development keeps working unchanged.
    """
    s = settings if settings is not None else get_settings()
    if s.environment.lower() != "production":
        return

    # Logged before the raise: if several things are wrong, seeing all of them
    # in one boot beats fixing them one redeploy at a time.
    for warning in deployment_warnings(s):
        logger.warning("Deployment warning: %s", warning)

    problems = deployment_problems(s)
    if problems:
        raise UnsafeDeploymentConfig(
            "Refusing to start with ENVIRONMENT=production:\n- "
            + "\n- ".join(problems))
