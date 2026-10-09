"""Regression: CI must not route student meaning via keyword lists.

These tests protect the architecture: vocabulary must not determine
whether a message requires contextual investigation.
"""

from __future__ import annotations

import ast
from pathlib import Path
from uuid import uuid4

import pytest

from waxprep.context.investigator import ContextInvestigator
from waxprep.context.mock_model import MockContextModel
from waxprep.context.model import ContextModelDecision
from waxprep.context.models import InvestigationRequest
from waxprep.context.no_context_fast_path import is_blank_message
from waxprep.domain.identifiers import new_wax_id

CONTEXT_SRC = Path(__file__).resolve().parents[2] / "src" / "waxprep" / "context"


def test_application_does_not_skip_ci_for_greetings() -> None:
    """Non-blank greetings are not mechanically exempt from CI."""
    assert is_blank_message("hello") is False
    assert is_blank_message("thanks") is False
    assert is_blank_message("okay") is False


def test_same_keyword_does_not_imply_same_routing() -> None:
    """Sharing a word must not force identical application-level routing."""
    short = "hello"
    longer = "Hello, can we continue that physics quiz?"
    # Application only distinguishes blank vs non-blank.
    assert is_blank_message(short) is False
    assert is_blank_message(longer) is False
    # Both are eligible for CI; meaning is not decided here.


@pytest.mark.asyncio
async def test_ci_script_not_vocabulary_drives_investigation() -> None:
    """Investigation happens when CI decides — including odd phrases."""
    model = MockContextModel(
        decision_script=[
            ContextModelDecision(
                action="search_conversation",
                arguments={"query": "red thing", "limit": 4},
                reason="Mock CI investigates an open-world reference.",
            ),
            ContextModelDecision(
                action="stop",
                arguments={},
                reason="Done.",
            ),
        ]
    )
    investigator = ContextInvestigator(model=model, tools={})
    package = await investigator.investigate(
        InvestigationRequest(
            wax_id=new_wax_id(),
            conversation_id=uuid4(),
            query="Can we pick up that red thing from before?",
        )
    )
    assert any("search_conversation" in step for step in package.investigated)


@pytest.mark.asyncio
async def test_historical_reference_without_category_words() -> None:
    """Neutral historical references do not need quiz/assignment keywords."""
    model = MockContextModel(
        decision_script=[
            ContextModelDecision(
                action="search_semantically",
                arguments={"query": "where were we", "limit": 4},
                reason="Mock CI treats this as needing prior context.",
            ),
            ContextModelDecision(
                action="stop",
                arguments={},
                reason="Stop.",
            ),
        ]
    )
    investigator = ContextInvestigator(model=model, tools={})
    package = await investigator.investigate(
        InvestigationRequest(
            wax_id=new_wax_id(),
            conversation_id=uuid4(),
            query="Where were we?",
        )
    )
    assert model.calls >= 1
    assert package.investigated


def test_no_semantic_vocabulary_sets_in_context_routing() -> None:
    """Guard: no large hardcoded phrase sets in context routing modules."""
    routing_files = (
        CONTEXT_SRC / "no_context_fast_path.py",
        CONTEXT_SRC / "investigator.py",
    )
    banned_names = {
        "_GREETINGS",
        "_QUIZ_WORDS",
        "_ASSIGNMENT_WORDS",
        "_TEACHER_WORDS",
        "_SAFE_PHRASES",
        "_SKIP_CI_PHRASES",
        "GREETING_WORDS",
        "HARMLESS_MESSAGES",
    }
    for path in routing_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in banned_names:
                        raise AssertionError(
                            f"{path.name} reintroduced semantic vocabulary "
                            f"set {target.id!r}"
                        )


def test_no_context_fast_path_has_no_greeting_frozenset() -> None:
    source = (CONTEXT_SRC / "no_context_fast_path.py").read_text(encoding="utf-8")
    assert "_GREETINGS" not in source
    assert "good morning" not in source
    assert "frozenset" not in source or "is_blank_message" in source


def test_mock_model_has_no_keyword_if_chains() -> None:
    """Mock CI must not classify student text with if 'word' in query."""
    source = (CONTEXT_SRC / "mock_model.py").read_text(encoding="utf-8")
    assert 'if "remember"' not in source
    assert 'if "continue"' not in source
    assert "in query" not in source
