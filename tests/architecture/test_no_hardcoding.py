"""No hard-coded keywords, categories, or media-type handling in the intelligence layers."""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "waxprep"
LAYERS = ("context", "derive", "history", "index", "orchestration")
PROMPT_FILES = {"system_prompt.py", "tool_definitions.py"}
BANNED_WORDS = re.compile(
    r"\b(pdf|docx|worksheet|quiz|assignment|homework|greeting|syllabus|waec|jamb)\b",
    re.IGNORECASE,
)
FIXED_PROFILE_FIELDS = {
    "strengths",
    "weaknesses",
    "goals",
    "learning_style",
    "school",
    "class_level",
}


def _layer_files() -> list[Path]:
    return [
        p
        for layer in LAYERS
        for p in (SRC / layer).rglob("*.py")
        if p.name not in PROMPT_FILES
    ]


def test_no_topic_or_media_vocabulary_in_logic() -> None:
    hits = [
        f"{p.relative_to(SRC)}: {m.group(0)}"
        for p in _layer_files()
        for m in BANNED_WORDS.finditer(p.read_text(encoding="utf-8"))
    ]
    assert hits == []


def test_no_string_set_or_list_literals_used_to_match_student_words() -> None:
    for path in _layer_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Compare) and any(
                isinstance(op, ast.In) for op in node.ops
            ):
                for comparator in node.comparators:
                    if isinstance(comparator, (ast.Set, ast.List, ast.Tuple)):
                        words = [
                            e.value
                            for e in comparator.elts
                            if isinstance(e, ast.Constant) and isinstance(e.value, str)
                        ]
                        # structural enum-like checks are fine; word lists are not
                        assert len(words) < 6, (
                            f"{path.name} matches against a word list"
                        )


def test_no_fixed_student_profile_columns_or_fields() -> None:
    sql = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (SRC.parent.parent / "migrations").glob("*.sql")
    )
    for field in FIXED_PROFILE_FIELDS:
        assert not re.search(rf"\b{field}\b\s+(TEXT|JSONB|INT)", sql, re.IGNORECASE), (
            field
        )


def test_no_artifact_type_classes() -> None:
    names = [
        node.name
        for p in (SRC / "domain", SRC / "derive")
        for f in p.rglob("*.py")
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8")))
        if isinstance(node, ast.ClassDef)
    ]
    assert not [n for n in names if re.search(r"(Pdf|Image|Audio|Quiz|Worksheet)", n)]
