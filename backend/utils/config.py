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
    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/analytics"
    query_timeout_seconds: int = 30
    max_result_rows: int = 5000

    class Config:
        env_file = ".env"


@lru_cache
def get_settings() -> "Settings":
    return Settings()
