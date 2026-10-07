"""Simple ordered SQL migration runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg


def migrations_dir() -> Path:
    """Return the repository migrations directory."""
    return Path(__file__).resolve().parents[2] / "migrations"


def apply_migrations(conn: psycopg.Connection[Any]) -> list[str]:
    """Apply pending *.sql migrations in sorted order."""
    applied: list[str] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute("SELECT version FROM schema_migrations")
        done = {row[0] for row in cur.fetchall()}

    for path in sorted(migrations_dir().glob("*.sql")):
        version = path.name
        if version in done:
            continue
        sql = path.read_text(encoding="utf-8")
        with conn.cursor() as cur:
            cur.execute(sql)  # pyright: ignore[reportCallIssue, reportArgumentType]
            cur.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s) "
                "ON CONFLICT DO NOTHING",
                (version,),
            )
        applied.append(version)
    conn.commit()
    return applied


__all__ = ["apply_migrations", "migrations_dir"]
