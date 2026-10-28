"""Shared PostgreSQL fixtures. Skipped automatically without DATABASE_URL."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from support import TEST_CIPHER
from waxprep.migrations import apply_migrations
from waxprep.storage.postgres import PostgresStorage

DATABASE_URL = os.environ.get("DATABASE_URL", "")


@pytest.fixture
def pg_conn() -> Iterator[psycopg.Connection[Any]]:
    """A clean, fully migrated database for each test."""
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set")
    conn: psycopg.Connection[Any] = psycopg.connect(DATABASE_URL, autocommit=False)
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")
    conn.commit()
    apply_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def pg_store(pg_conn: psycopg.Connection[Any]) -> PostgresStorage:
    return PostgresStorage(pg_conn, TEST_CIPHER)


# ---------------------------------------------------------------------------
# Unraisable-exception observability
#
# ``filterwarnings = ["error"]`` turns unraisable exceptions (e.g. raised
# from ``__del__`` during garbage collection) into test failures. CPython
# may finalize the leaked object much later, so the failure is often
# attributed to an unrelated test. Chain onto the unraisable hook to
# record the real exception details and print them at the very end of
# the run, where CI log tails keep them, so the true source stays
# diagnosable from gate output.
# ---------------------------------------------------------------------------

_UNRAISABLE_DETAILS: list[str] = []
_UNRAISABLE_STORE_LIMIT = 40
_UNRAISABLE_SHOW = 12


def _describe_unraisable(unraisable: sys.UnraisableHookArgs) -> str:
    name = getattr(unraisable.exc_type, "__name__", "Exception")
    obj = repr(unraisable.object)
    if len(obj) > 160:
        obj = obj[:160] + "..."
    return f"{name}: {unraisable.exc_value!r} in {obj}"


@pytest.fixture(autouse=True)
def _chain_unraisable_hook() -> Iterator[None]:
    previous = sys.unraisablehook

    def chained(unraisable: sys.UnraisableHookArgs) -> None:
        try:
            if len(_UNRAISABLE_DETAILS) < _UNRAISABLE_STORE_LIMIT:
                _UNRAISABLE_DETAILS.append(_describe_unraisable(unraisable))
        except Exception:
            # Observability must never mask the hook it wraps.
            pass
        previous(unraisable)

    sys.unraisablehook = chained
    yield
    sys.unraisablehook = previous


def pytest_terminal_summary(
    terminalreporter: Any, exitstatus: Any, config: Any
) -> None:
    for detail in _UNRAISABLE_DETAILS[:_UNRAISABLE_SHOW]:
        terminalreporter.write_line("UNRAISABLE DETAIL: " + detail[:600])
    extra = len(_UNRAISABLE_DETAILS) - _UNRAISABLE_SHOW
    if extra > 0:
        terminalreporter.write_line(f"UNRAISABLE DETAIL: ... {extra} more")
