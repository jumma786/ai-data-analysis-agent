"""Centralized configuration via environment variables."""
from functools import lru_cache
try:
    from pydantic_settings import BaseSettings
except ImportError:  # pydantic v1 fallback
    from pydantic import BaseSettings


class Settings(BaseSettings):
    app_name: str = "AI Data Analysis Agent"
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

    # Application metadata database: users, datasets, conversations, reports.
    # Deliberately separate from `database_url` so the analytics connection can
    # stay read-only. Defaults to a local SQLite file for zero-config demos.
    metadata_database_url: str = "sqlite:///./app_metadata.db"

    query_timeout_seconds: int = 30
    max_result_rows: int = 5000

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

    class Config:
        env_file = ".env"


@lru_cache
def get_settings() -> "Settings":
    return Settings()
