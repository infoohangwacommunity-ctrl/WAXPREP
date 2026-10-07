"""Model gateway contract tests."""

from __future__ import annotations

from uuid import uuid4

import pytest

from waxprep.model_gateway.models import (
    ContentItem,
    ContentType,
    ModelRequest,
    ModelRole,
)


def test_text_content_requires_text() -> None:
    with pytest.raises(ValueError):
        ContentItem(type=ContentType.TEXT)


def test_non_text_content_requires_uri() -> None:
    with pytest.raises(ValueError):
        ContentItem(type=ContentType.IMAGE)


def test_request_rejects_invalid_temperature() -> None:
    with pytest.raises(ValueError):
        ModelRequest(
            wax_id=uuid4(),
            role=ModelRole.TEACHER,
            model="test",
            instructions=None,
            input=(),
            temperature=3,
        )
