"""Shared PostgreSQL fixtures. Skipped automatically without DATABASE_URL."""

from __future__ import annotations

import os
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
