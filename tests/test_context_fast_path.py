"""No-context fast path tests."""

from waxprep.context.no_context_fast_path import can_use_no_context_fast_path


def test_hello_uses_cheap_path() -> None:
    assert can_use_no_context_fast_path("hello") is True
    assert can_use_no_context_fast_path("HELLO") is True


def test_real_question_does_not_use_cheap_path() -> None:
    assert can_use_no_context_fast_path("Remember the assignment I sent you?") is False
