"""Mechanical rules for the live exchange material.

Two functions live here and nothing else does:

- ``trim_exchange`` — how much raw material the application may supply.
- ``format_exchange`` — how supplied turns are rendered as plain text.

Both are mechanical. Neither judges a turn, ranks one, or decides that
one matters. Relevance is Context Intelligence's job, and the job is
done on the *assembled* exchange, not on a pre-filtered one.
"""

from __future__ import annotations

from dataclasses import replace

from waxprep.context.models import ExchangeTurn
from waxprep.domain.models import AuthorKind


def trim_exchange(
    turns: tuple[ExchangeTurn, ...],
    *,
    max_characters: int,
) -> tuple[ExchangeTurn, ...]:
    """Trim exchange material to a character budget, oldest turns first.

    The newest end survives, because that is the end a follow-up refers
    to ("that", "earlier", "you said"). No turn is reordered, merged,
    summarised or dropped for any reason other than length.
    """
    if max_characters <= 0 or not turns:
        return ()
    kept: list[ExchangeTurn] = []
    total = 0
    for turn in reversed(turns):
        cost = len(turn.text)
        if not kept:
            if cost > max_characters:
                # The newest turn alone blows the budget. Keep it anyway,
                # shortened — a partial newest turn still beats nothing.
                kept.append(replace(turn, text=turn.text[:max_characters]))
                total = max_characters
                break
            kept.append(turn)
            total = cost
            continue
        if total + cost > max_characters:
            break
        kept.append(turn)
        total += cost
    kept.reverse()
    return tuple(kept)


def format_exchange(turns: tuple[ExchangeTurn, ...]) -> str:
    """Render exchange turns as a plain, ordered transcript.

    Who said what, in order, no metadata — the shape a reader needs to
    read the exchange as an exchange.
    """
    lines: list[str] = []
    for turn in turns:
        text = turn.text.strip()
        if not text:
            continue
        if turn.author_kind == AuthorKind.STUDENT:
            lines.append(f"Student: {text}")
        elif turn.author_kind == AuthorKind.TUTOR:
            lines.append(f"Wax Prep: {text}")
        else:
            lines.append(f"System: {text}")
    return "\n".join(lines)


__all__ = ["format_exchange", "trim_exchange"]
