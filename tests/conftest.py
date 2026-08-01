import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def pytest_configure(config):
    """Register the `integration` marker.

    Tests carrying it need a live Postgres (and, for the LLM step, a real API
    key). They skip themselves when those are absent, so `pytest tests/ -q`
    stays offline and deterministic.
    """
    config.addinivalue_line(
        "markers",
        "integration: requires a live database (and optionally an LLM key); "
        "skipped automatically when INTEGRATION_DATABASE_URL is unset",
    )
    config.addinivalue_line(
        "markers",
        "chroma: exercises the ChromaDB backend; opt in with RUN_CHROMA_TESTS=1 "
        "and run in a process that has not imported pandas (see "
        "tests/test_rag_chroma.py for why)",
    )
