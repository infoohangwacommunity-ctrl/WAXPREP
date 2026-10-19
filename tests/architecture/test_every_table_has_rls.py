"""Every table holding a wax_id must enforce Row-Level Security (PostgreSQL)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

pytestmark = pytest.mark.postgres


def test_every_student_table_forces_rls(pg_conn: psycopg.Connection[Any]) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN information_schema.columns col ON col.table_name = c.relname "
            "AND col.table_schema = n.nspname "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' AND col.column_name = 'wax_id'"
        )
        rows = cur.fetchall()
    assert rows, "no student tables found"
    unprotected = [r[0] for r in rows if not (r[1] and r[2])]
    assert unprotected == []


def test_old_conversation_tables_are_gone(pg_conn: psycopg.Connection[Any]) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name IN ('conversations', 'messages')"
        )
        assert cur.fetchall() == []
