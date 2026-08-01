"""End-to-end tests against a live database (and, for one test, a live LLM).

Nothing here runs during `pytest tests/ -q` unless you opt in:

    INTEGRATION_DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/analytics \
        pytest tests/integration -m integration -q

The table must already be populated -- see scripts/load_online_retail.py.

WHAT IS AND IS NOT VERIFIED HERE
--------------------------------
`test_execution_path_returns_rows` verifies the half of the pipeline that does
not need a model: validation -> execution -> chart selection, against real rows.
It uses a hand-written reference query, so it proves the SQL path works; it
proves nothing about NL -> SQL.

`test_full_pipeline_total_revenue_by_country` is the only test that verifies the
LLM step, and it is skipped unless a real provider is configured. We deliberately
do NOT stub the LLM into returning canned SQL: a mock that hands back SQL we
wrote ourselves would assert that our own fixture parses, not that the agent can
translate a question. If you have not run this test with a key (or a local
Ollama model), the NL -> SQL step is unverified -- say so rather than implying
otherwise.
"""
from __future__ import annotations

import os

import pandas as pd
import pytest
from sqlalchemy import create_engine, inspect, text

from backend.agents.analysis_agents import choose_chart
from backend.agents.graph import run_pipeline
from backend.agents.sql_validation import enforce_limit, validate_sql
from backend.services.schema_introspect import introspect_schema

pytestmark = pytest.mark.integration

TABLE = os.getenv("INTEGRATION_TABLE", "online_retail")
QUESTION = "total revenue by country"

# The reference SQL a correct model should produce something equivalent to.
REFERENCE_SQL = f"""
SELECT country, SUM(revenue) AS total_revenue
FROM {TABLE}
GROUP BY country
ORDER BY total_revenue DESC
"""

_DB_URL = os.getenv("INTEGRATION_DATABASE_URL", "")

requires_db = pytest.mark.skipif(
    not _DB_URL,
    reason="Set INTEGRATION_DATABASE_URL to a populated database to run this.",
)


def _llm_is_configured() -> bool:
    """True if a provider that can actually answer is configured.

    Ollama needs an explicit opt-in because `LLM_PROVIDER=ollama` alone does not
    tell us a model is pulled and the server is up.
    """
    from backend.utils.config import get_settings

    settings = get_settings()
    if settings.llm_provider == "ollama":
        return bool(os.getenv("INTEGRATION_ALLOW_OLLAMA"))
    return bool(settings.openai_api_key)


requires_llm = pytest.mark.skipif(
    not _llm_is_configured(),
    reason=(
        "No LLM configured. The NL->SQL step is unverified without one; we do "
        "not mock it, because a mocked model proves nothing about generation."
    ),
)


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(_DB_URL)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(scope="module")
def schema_text():
    return introspect_schema(_DB_URL)


@requires_db
def test_table_is_present_and_populated(engine):
    """Fail loudly and early if the loader has not been run."""
    assert TABLE in inspect(engine).get_table_names(), (
        f"Table '{TABLE}' not found. Run scripts/load_online_retail.py first."
    )
    with engine.connect() as conn:
        count = conn.execute(text(f'SELECT COUNT(*) FROM "{TABLE}"')).scalar_one()
    assert count > 0, f"Table '{TABLE}' is empty; load the dataset first."


@requires_db
def test_execution_path_returns_rows(engine):
    """Validation -> execution -> chart choice, using a known-good query.

    No LLM involved: this isolates the deterministic half of the pipeline.
    """
    assert validate_sql(REFERENCE_SQL).ok

    with engine.connect() as conn:
        df = pd.read_sql(text(enforce_limit(REFERENCE_SQL, 5000)), conn)

    assert len(df) > 0, "Reference query returned no rows."
    assert {"country", "total_revenue"} <= set(df.columns)
    assert df["total_revenue"].notna().any()
    # One categorical + one numeric column -> a categorical comparison chart.
    assert choose_chart(df) in {"bar", "pie"}


@requires_db
@requires_llm
def test_full_pipeline_total_revenue_by_country(schema_text):
    """The real thing: question in, model-generated SQL, live rows out.

    Asserts on the *shape* of the answer (rows returned, a numeric column, a
    country-like label) rather than exact values, because a model may
    legitimately phrase the aggregate differently between runs. It does assert
    the SQL passed validation and executed -- which is what "works end to end"
    has to mean.
    """
    from backend.utils.config import get_settings

    # run_pipeline executes against settings.database_url, not the URL this
    # module connects with directly. Catch the mismatch here rather than as a
    # confusing "relation does not exist" further down.
    assert get_settings().database_url == _DB_URL, (
        "DATABASE_URL and INTEGRATION_DATABASE_URL must point at the same "
        "database: the pipeline reads DATABASE_URL from settings."
    )

    state = run_pipeline(QUESTION, schema=schema_text)

    assert state.get("valid"), (
        f"Pipeline rejected the generated SQL: {state.get('error')}\n"
        f"SQL was: {state.get('sql')}"
    )
    df = state["df"]
    assert len(df) > 0, f"Generated SQL returned no rows: {state.get('sql')}"
    assert not df.select_dtypes("number").empty, "No numeric measure in the result."
    assert any("countr" in str(c).lower() for c in df.columns), (
        f"Result has no country dimension: {list(df.columns)}"
    )
    assert state.get("chart"), "No chart was selected."
    assert state.get("insight", "").strip(), "No insight text was generated."
