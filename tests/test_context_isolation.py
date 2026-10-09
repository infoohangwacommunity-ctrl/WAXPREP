"""Student isolation for CI retrieval (postgres opt-in)."""

import pytest

pytestmark = pytest.mark.postgres


def test_context_search_is_student_scoped() -> None:
    """Placeholder: full isolation needs DATABASE_URL + knowledge seed data."""
    pytest.skip("Requires DATABASE_URL and knowledge fixtures (later).")
