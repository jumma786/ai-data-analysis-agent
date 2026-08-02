import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def pytest_sessionstart(session):
    """Lower bcrypt's cost factor for the test run.

    Production uses passlib's default (12 rounds, ~250ms per hash by design).
    The auth tests hash and verify often enough that this dominated the suite --
    56s, most of it burning CPU on purpose. Four rounds keeps the algorithm and
    every property under test (salting, verification, the 72-byte limit) while
    removing the deliberate slowness. The cost factor itself is not what these
    tests are checking.
    """
    from backend.utils import security

    security._pwd_context = security.CryptContext(  # noqa: SLF001
        schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=4)


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
