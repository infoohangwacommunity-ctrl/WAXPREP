"""The conversation concept is gone from code, tools, and prompts."""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "waxprep"
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
BACKFILL = SRC / "history" / "backfill.py"


def _python_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if p != BACKFILL]


def test_no_conversation_word_in_application_code() -> None:
    offenders = [
        str(p.relative_to(SRC))
        for p in _python_files()
        if "conversation" in p.read_text(encoding="utf-8").lower()
    ]
    assert offenders == []


def test_new_migrations_never_create_conversation_columns() -> None:
    for path in sorted(MIGRATIONS.glob("*.sql")):
        number = int(path.name.split("_", 1)[0])
        if number >= 5 and number != 6:  # 006 only DROPS the old tables
            assert "conversation" not in path.read_text(encoding="utf-8").lower()


def test_history_tools_do_not_take_a_conversation_argument() -> None:
    from waxprep.context.tool_definitions import context_tool_definitions

    for tool in context_tool_definitions():
        assert "conversation" not in str(tool).lower()
