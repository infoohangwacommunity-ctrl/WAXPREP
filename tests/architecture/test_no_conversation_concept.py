"""The conversation concept is gone from code, tools, and prompts.

The architect's memory-architecture-redesign (branch
``ecc/memory-architecture-redesign``) narrows the original "no
conversation concept" ban to permit one well-scoped exception: a
``current_working_focus`` field carried inside a notebook entry's
``payload`` dict. This is the long-conversation-coherence mechanism —
a short-lived working focus the CI layer can read so a 50-turn thread
can still recall what was said at turn 5.

The exception is narrow on purpose:

- ``current_working_focus`` may appear as a *payload key* inside a
  ``NotebookEntry.payload`` dict (open-ended structured storage).
- ``current_working_focus`` may NOT be a top-level concept: no
  standalone table, no column on ``history_entries`` or ``notebooks``,
  no production dataclass field, no function argument named after it.
- The bare word "conversation" is still banned from application code,
  tool names, and migrations. The exception is for the focus field,
  not for re-introducing a conversation table.

These tests are RED until the backend lands ``current_working_focus``
support in the notebook layer. They were written by QA against the
NEW behaviour; the founder's brief requires the test gate to prove
the difference.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

SRC = Path(__file__).resolve().parents[2] / "src" / "waxprep"
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
BACKFILL = SRC / "history" / "backfill.py"

# The narrow exception: this exact string is permitted as a payload key
# inside notebook entries. Anything that smells like a "conversation"
# concept is still banned.
PERMITTED_FOCUS_KEY = "current_working_focus"


def _python_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if p != BACKFILL]


def _tokenize_identifiers(text: str) -> set[str]:
    """Extract Python identifiers (class, function, variable, attribute
    names) from source text.

    Strings, comments and docstrings are excluded so the word
    "conversation" appearing in a tool *description string* (natural
    language the model reads) is NOT a code-level identifier violation.
    The previous raw-text scan conflated the two.
    """
    # Strip comments.
    text = re.sub(r"#.*$", "", text, flags=re.MULTILINE)
    # Strip string literals (single-, double-, triple-quoted). Tool
    # description strings are natural-language text the model reads;
    # they are not code-level identifiers and may use any vocabulary.
    text = re.sub(r'""".*?"""', "", text, flags=re.DOTALL)
    text = re.sub(r"'''.*?'''", "", text, flags=re.DOTALL)
    text = re.sub(r'"(?:\\.|[^"\\])*"', "", text)
    text = re.sub(r"'(?:\\.|[^'\\])*'", "", text)
    # Extract identifier-like tokens.
    return {m.group(0) for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", text)}


def test_no_conversation_word_in_application_code() -> None:
    """The word "conversation" must not appear as a code-level
    identifier anywhere in ``src/waxprep``.

    Strings and comments are stripped before the scan so a tool
    description that says "the recent conversation" is permitted —
    the model reads natural-language text. What is banned is naming
    a class, function, variable, or attribute with the word.
    """
    offenders: list[str] = []
    for p in _python_files():
        text = p.read_text(encoding="utf-8")
        tokens = _tokenize_identifiers(text)
        if "conversation" in {t.lower() for t in tokens}:
            offenders.append(str(p.relative_to(SRC)))
    assert offenders == [], (
        "The word 'conversation' must not appear as a code-level "
        "identifier. Offenders: " + ", ".join(offenders)
    )


def test_new_migrations_never_create_conversation_columns() -> None:
    for path in sorted(MIGRATIONS.glob("*.sql")):
        number = int(path.name.split("_", 1)[0])
        if number >= 5 and number != 6:  # 006 only DROPS the old tables
            assert "conversation" not in path.read_text(encoding="utf-8").lower()


def test_history_tools_do_not_take_a_conversation_argument() -> None:
    from waxprep.context.tool_definitions import context_tool_definitions

    for tool in context_tool_definitions():
        # Tool NAMES must never reference "conversation"; tool
        # descriptions may use any natural-language vocabulary.
        assert "conversation" not in str(tool.get("name", "")).lower()
        # Tool argument names must never reference "conversation" either.
        props = tool.get("input_schema", {}).get("properties", {}) or {}
        for prop_name in props:
            assert "conversation" not in str(prop_name).lower()


# ---------------------------------------------------------------------------
# The narrow exception: ``current_working_focus`` payload key.
# ---------------------------------------------------------------------------


def test_current_working_focus_is_permitted_as_notebook_payload_key() -> None:
    """The architect permits ``current_working_focus`` as a key inside a
    ``NotebookEntry.payload`` dict. This is the long-conversation-coherence
    mechanism — CI reads the focus so a 50-turn thread can recall turn 5.

    The payload is an open-ended ``dict[str, Any]``; storing a focus
    key there does NOT introduce a new column or a new table. The
    notebook's schema is unchanged.
    """
    from waxprep.domain.identifiers import new_wax_id
    from waxprep.domain.models import NotebookEntry

    entry = NotebookEntry(
        id=uuid4(),
        wax_id=new_wax_id(),
        notebook_id=uuid4(),
        schema_version=1,
        payload={PERMITTED_FOCUS_KEY: "fractions — adding with unlike denominators"},
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        updated_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert PERMITTED_FOCUS_KEY in entry.payload
    assert entry.payload[PERMITTED_FOCUS_KEY]


def test_current_working_focus_does_not_reintroduce_a_conversation_table() -> None:
    """The narrow exception MUST NOT widen into a standalone table or a
    column on ``history_entries`` / ``notebooks`` / ``notebook_entries``.

    A grep across migrations for ``current_working_focus`` as a column
    name (rather than as a JSON payload key) is the guardrail. The
    field lives INSIDE the notebook payload dict — never as schema.
    """
    pattern = re.compile(
        r"\bcurrent_working_focus\b\s*(?:VARCHAR|TEXT|JSONB|BOOLEAN|"
        r"INTEGER|TIMESTAMP|UUID|BIGINT|BOOL|"
        r"text|varchar|jsonb|boolean|integer|timestamp|uuid|bigint)\b",
        re.IGNORECASE,
    )
    offenders: list[str] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        # A bare column declaration like ``current_working_focus TEXT``
        # is a violation. A JSON payload key reference (e.g. inside a
        # comment, or as ``payload->>'current_working_focus'``) is fine.
        for line in sql.splitlines():
            if pattern.search(line):
                offenders.append(f"{path.name}: {line.strip()}")
    assert offenders == [], (
        "current_working_focus must live inside NotebookEntry.payload "
        "(JSON), not as a SQL column. Offenders: " + "; ".join(offenders)
    )


def test_application_code_does_not_use_current_working_focus_as_top_level_concept() -> None:
    """The exception is for a *payload key*, not for a top-level concept.

    ``current_working_focus`` must NOT appear as:
    - a production dataclass field on a non-payload model
    - a function or method argument name
    - a top-level module attribute

    It MAY appear inside ``payload`` dicts (the open-ended
    NotebookEntry payload) and inside tool argument schemas that
    forward into ``payload``. This test guards against the exception
    silently widening back into a conversation concept.
    """
    # Forbid the literal token as a top-level identifier in application
    # code: it must only appear as a string KEY inside a dict literal
    # or as a tool schema property name that targets the payload.
    offenders: list[str] = []
    for p in _python_files():
        text = p.read_text(encoding="utf-8")
        # Strip strings/comments so we only see code-level identifiers.
        stripped = re.sub(r"#.*$", "", text, flags=re.MULTILINE)
        stripped = re.sub(r'""".*?"""', "", stripped, flags=re.DOTALL)
        stripped = re.sub(r"'''.*?'''", "", stripped, flags=re.DOTALL)
        stripped = re.sub(r'"(?:\\.|[^"\\])*"', "", stripped)
        stripped = re.sub(r"'(?:\\.|[^'\\])*'", "", stripped)
        # Find lines that declare current_working_focus as a parameter
        # or attribute (e.g. ``def f(current_working_focus: ...)`` or
        # ``self.current_working_focus = ...`` or a dataclass field).
        bad_patterns = [
            r"def\s+\w+\s*\([^)]*\bcurrent_working_focus\b",
            r"self\.current_working_focus\s*=",
            r"^\s*current_working_focus\s*:\s*",  # dataclass / annotation
            r"^\s*current_working_focus\s*=\s*",  # module-level attr
        ]
        for line_num, line in enumerate(stripped.splitlines(), 1):
            for pat in bad_patterns:
                if re.search(pat, line):
                    offenders.append(f"{p.relative_to(SRC)}:{line_num}: {line.strip()}")
    assert offenders == [], (
        "current_working_focus must be a payload-key, not a top-level "
        "concept (no function arg, no dataclass field, no module "
        "attribute). Offenders: " + "; ".join(offenders)
    )
