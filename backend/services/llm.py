"""Modular LLM layer. Swap providers via config without touching call sites.

Providers implement a single `complete(system, user)` method. This keeps the
agent code provider-agnostic (OpenAI today, Ollama for local, easily extended).
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from backend.utils.config import get_settings
from backend.utils.logging_config import logger


class LLMProvider(ABC):
    @abstractmethod
    def complete(self, system: str, user: str) -> str: ...


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str, model: str):
        from openai import OpenAI
        self._client = OpenAI(api_key=api_key)
        self._model = model

    def complete(self, system: str, user: str) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0,
        )
        return resp.choices[0].message.content or ""


class OllamaProvider(LLMProvider):
    def __init__(self, base_url: str, model: str, timeout: int = 120):
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    def complete(self, system: str, user: str) -> str:
        import requests
        r = requests.post(
            f"{self._base_url}/api/chat",
            json={"model": self._model, "stream": False,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}]},
            timeout=self._timeout,
        )
        r.raise_for_status()
        return r.json()["message"]["content"]


def get_llm() -> LLMProvider:
    s = get_settings()
    if s.llm_provider == "ollama":
        logger.info("Using Ollama provider (%s)", s.ollama_model)
        return OllamaProvider(s.ollama_base_url, s.ollama_model,
                              timeout=s.llm_timeout_seconds)
    logger.info("Using OpenAI provider (%s)", s.openai_model)
    return OpenAIProvider(s.openai_api_key, s.openai_model)
