"""Every table holding a wax_id must enforce Row-Level Security (PostgreSQL)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

pytestmark = pytest.mark.postgres

# Tables read through SECURITY DEFINER helper functions (waxprep_lookup_channel,
# waxprep_students_with_due_jobs, waxprep_orphan_student_ids, ...). Those helpers
# run as their owner — the table owner — so FORCE ROW LEVEL SECURITY would blind
# them on hosts where the owner is not a superuser (Render), while CI's superuser
# postgres user bypasses RLS anyway and hides the divergence. Migration 015 keeps
# RLS ENABLED on these tables (policies still apply to every other role) but
# drops only the FORCE flag, so the helpers can see rows in production.
NO_FORCE_EXCEPTIONS = frozenset(
    {
        "students",
        "channel_identities",
        "history_entries",
        "events",
        "derivation_jobs",
        "outbound_deliveries",
        "notebooks",
        "notebook_entries",
        "deletion_requests",
        "interactive_offers",
    }
)


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
    no_rls = sorted(r[0] for r in rows if not r[1])
    assert no_rls == [], "student tables without RLS enabled"
    unforced = sorted(r[0] for r in rows if not r[2])
    assert unforced == sorted(NO_FORCE_EXCEPTIONS)


def test_old_conversation_tables_are_gone(pg_conn: psycopg.Connection[Any]) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name IN ('conversations', 'messages')"
        )
        assert cur.fetchall() == []
