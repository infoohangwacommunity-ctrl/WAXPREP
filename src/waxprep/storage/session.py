"""PostgreSQL request context for Row-Level Security."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from waxprep.domain.identifiers import WaxId


def set_current_wax_id(conn: psycopg.Connection[Any], wax_id: WaxId | UUID) -> None:
    """Bind the current request's WAX ID for RLS policies (transaction-local)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT set_config('app.current_wax_id', %s, true)",
            (str(wax_id),),
        )


def clear_current_wax_id(conn: psycopg.Connection[Any]) -> None:
    """Clear the request WAX ID (transaction-local)."""
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.current_wax_id', '', true)")


__all__ = ["clear_current_wax_id", "set_current_wax_id"]
