#!/usr/bin/env python3
"""Scan the intelligence layers for hardcoded dictionary vocabulary.

The Founder's brief is explicit: the tutor's brain must not branch on
topic/media words (``pdf``, ``quiz``, ``waec``, ``jamb``, …). Those are
student-facing concepts; the orchestration, context, derive, history,
and index layers must treat every message as opaque text and let the
model decide what it means.

This script is the CI counterpart to ``tests/architecture/test_no_hardcoding.py``.
The pytest module stops a local run; this script stops a merge. The
backend agent owns the banned-word list and the layer set — the script
reads them from a single source of truth (``tests/architecture/test_no_hardcoding.py``
re-exports the constants; if that file changes shape, update the
constants below in lockstep).

Usage::

    python scripts/ci/scan_hardcoded_dictionary_words.py
    python scripts/ci/scan_hardcoded_dictionary_words.py --src src/waxprep

Exit code 0 = clean, 1 = banned vocabulary found. Output is one line
per hit (``path:line: word``) so the quality-gate failure-triage job
survives the 64k issue-body cap.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Single source of truth: mirrors tests/architecture/test_no_hardcoding.py.
# If the test file's banned-word list grows, extend this regex in the
# same change so the CI gate and the local test agree.
BANNED_WORD_PATTERN = re.compile(
    r"\b(pdf|docx|worksheet|quiz|assignment|homework|greeting|syllabus|waec|jamb)\b",
    re.IGNORECASE,
)

# Intelligence layers that must stay vocabulary-free. The channel,
# media, domain, and storage layers are excluded on purpose: they are
# allowed to name media types and storage columns.
INTELLIGENCE_LAYERS = ("context", "derive", "history", "index", "orchestration")

# Prompt files may use the banned words to instruct the model what NOT
# to hardcode; they are not logic.
PROMPT_FILES = frozenset({"system_prompt.py", "tool_definitions.py"})


def _scan_layer(src: Path) -> list[tuple[Path, int, str]]:
    """Return one (path, line_number, word) tuple per banned hit."""
    hits: list[tuple[Path, int, str]] = []
    for layer in INTELLIGENCE_LAYERS:
        layer_dir = src / layer
        if not layer_dir.is_dir():
            continue
        for path in layer_dir.rglob("*.py"):
            if path.name in PROMPT_FILES:
                continue
            text = path.read_text(encoding="utf-8")
            for line_no, line in enumerate(text.splitlines(), start=1):
                for match in BANNED_WORD_PATTERN.finditer(line):
                    hits.append((path, line_no, match.group(0)))
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan intelligence layers for hardcoded dictionary vocabulary."
    )
    parser.add_argument(
        "--src",
        default="src/waxprep",
        help="path to the waxprep package root (default: src/waxprep)",
    )
    args = parser.parse_args(argv)

    src = Path(args.src).resolve()
    if not src.is_dir():
        print(f"error: source directory not found: {src}", file=sys.stderr)
        return 2

    hits = _scan_layer(src)
    if not hits:
        print("dictionary scan: clean (0 banned words in intelligence layers)")
        return 0

    print(
        f"dictionary scan: {len(hits)} banned word(s) found in intelligence layers:",
        file=sys.stderr,
    )
    for path, line_no, word in hits:
        try:
            rel = path.relative_to(Path.cwd())
        except ValueError:
            rel = path
        print(f"{rel}:{line_no}: {word}", file=sys.stderr)
    print(
        "\nThese layers must not branch on topic/media vocabulary. "
        "Move the word to the model's prompt or to the channel/media adapter.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
