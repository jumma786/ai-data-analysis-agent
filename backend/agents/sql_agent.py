"""SQL Agent — turns natural language + schema into a single SELECT query."""
from __future__ import annotations
from backend.services.llm import get_llm

_SYSTEM = """You are an expert analytics SQL generator.
Rules:
- Output ONE read-only SQL SELECT statement and nothing else.
- No prose, no markdown fences, no semicolons chaining.
- Never use DROP/DELETE/UPDATE/INSERT/ALTER.
- Use only tables/columns present in the provided schema.
- Prefer explicit column lists and add sensible ORDER BY / LIMIT."""


def generate_sql(question: str, schema: str, history: str = "") -> str:
    user = f"SCHEMA:\n{schema}\n\nCONVERSATION:\n{history}\n\nQUESTION: {question}\n\nSQL:"
    raw = get_llm().complete(_SYSTEM, user)
    # Strip accidental markdown fences.
    return raw.replace("```sql", "").replace("```", "").strip()
