"""Append-only SQL migration runner with checksum verification."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import psycopg


class MigrationChecksumError(RuntimeError):
    """Raised when a previously applied migration file was modified."""


def migrations_dir() -> Path:
    """Return the repository migrations directory."""
    return Path(__file__).resolve().parents[2] / "migrations"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def apply_migrations(conn: psycopg.Connection[Any]) -> list[str]:
    """Apply pending *.sql migrations in sorted order.

    Previously applied migrations must keep the same SHA-256 content.
    """
    applied: list[str] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                checksum TEXT
            )
            """
        )
        # Older installs may lack checksum column
        cur.execute(
            """
            ALTER TABLE schema_migrations
            ADD COLUMN IF NOT EXISTS checksum TEXT
            """
        )
        cur.execute("SELECT version, checksum FROM schema_migrations")
        done: dict[str, str | None] = {
            str(row[0]): (str(row[1]) if row[1] is not None else None)
            for row in cur.fetchall()
        }

    for path in sorted(migrations_dir().glob("*.sql")):
        version = path.name
        sql = path.read_text(encoding="utf-8")
        checksum = _sha256_text(sql)

        if version in done:
            recorded = done[version]
            if recorded is not None and recorded != checksum:
                raise MigrationChecksumError(
                    f"migration {version} was modified after application "
                    f"(recorded={recorded}, current={checksum})"
                )
            if recorded is None:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE schema_migrations SET checksum = %s WHERE version = %s",
                        (checksum, version),
                    )
            continue

        with conn.cursor() as cur:
            cur.execute(sql)  # pyright: ignore[reportCallIssue, reportArgumentType]
            cur.execute(
                "INSERT INTO schema_migrations (version, checksum) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (version, checksum),
            )
        applied.append(version)

    conn.commit()
    return applied


__all__ = ["MigrationChecksumError", "apply_migrations", "migrations_dir"]
