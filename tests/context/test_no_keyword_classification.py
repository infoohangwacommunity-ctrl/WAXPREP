"""Regression: CI must not route student meaning via keyword lists."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from waxprep.context.investigator import ContextInvestigator
from waxprep.context.mock_model import MockContextModel
from waxprep.context.model import ContextModelDecision
from waxprep.context.models import InvestigationRequest
from waxprep.context.no_context_fast_path import is_blank_message
from waxprep.domain.identifiers import new_wax_id

CONTEXT_SRC = Path(__file__).resolve().parents[2] / "src" / "waxprep" / "context"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_application_does_not_skip_ci_for_greetings() -> None:
    for text in ("hello", "thanks", "okay"):
        assert is_blank_message(text) is False


@pytest.mark.asyncio
async def test_investigation_happens_when_ci_decides_not_by_vocabulary() -> None:
    model = MockContextModel(
        decision_script=[
            ContextModelDecision(
                "find_exact", {"query": "red thing"}, "open-world reference"
            ),
            ContextModelDecision("stop", {}, "done"),
        ]
    )
    package = await ContextInvestigator(model=model, tools={}).investigate(
        InvestigationRequest(
            wax_id=new_wax_id(), query="pick up that red thing?", now=NOW
        )
    )
    assert any("find_exact" in s for s in package.investigated)


def test_no_semantic_vocabulary_sets_in_context_routing() -> None:
    banned = {
        "_GREETINGS",
        "_QUIZ_WORDS",
        "_ASSIGNMENT_WORDS",
        "_TEACHER_WORDS",
        "_SAFE_PHRASES",
        "_SKIP_CI_PHRASES",
        "GREETING_WORDS",
        "HARMLESS_MESSAGES",
    }
    for path in (
        CONTEXT_SRC / "no_context_fast_path.py",
        CONTEXT_SRC / "investigator.py",
    ):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    assert not (isinstance(target, ast.Name) and target.id in banned)


def test_mock_model_has_no_keyword_if_chains() -> None:
    source = (CONTEXT_SRC / "mock_model.py").read_text(encoding="utf-8")
    assert "in query" not in source and 'if "remember"' not in source
