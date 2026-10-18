"""Shared PostgreSQL fixtures. Skipped automatically without DATABASE_URL."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from support import TEST_CIPHER

DATABASE_URL = os.environ.get("DATABASE_URL", "")

try:
    import psycopg
    from waxprep.migrations import apply_migrations
    from waxprep.storage.postgres import PostgresStorage
except ImportError:  # minimal environments without psycopg
    psycopg = None  # type: ignore[assignment]
    apply_migrations = None  # type: ignore[assignment]
    PostgresStorage = None  # type: ignore[assignment,misc]


@pytest.fixture
def pg_conn() -> Iterator[Any]:
    """A clean, fully migrated database for each test."""
    if not DATABASE_URL or psycopg is None:
        pytest.skip("DATABASE_URL not set or psycopg unavailable")
    conn = psycopg.connect(DATABASE_URL, autocommit=False)
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")
    conn.commit()
    apply_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def pg_store(pg_conn: Any) -> Any:
    if PostgresStorage is None:
        pytest.skip("psycopg unavailable")
    return PostgresStorage(pg_conn, TEST_CIPHER)
