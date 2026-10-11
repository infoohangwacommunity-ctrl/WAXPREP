"""Time helpers — when something was said matters as much as what was said.

Age changes how easy something is to find. It never removes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TemporalFact:
    node_id: str
    valid_from: datetime | None
    valid_until: datetime | None
    status: str


def choose_current_fact(facts: list[TemporalFact], at: datetime) -> list[TemporalFact]:
    return [
        fact
        for fact in facts
        if (fact.valid_from is None or fact.valid_from <= at)
        and (fact.valid_until is None or at < fact.valid_until)
    ]


def describe_age(now: datetime, then: datetime) -> str:
    """Plain-language age, e.g. 'about 5 months ago'. Presentation only."""
    seconds = (now - then).total_seconds()
    if seconds < 0:
        return "in the future relative to now"
    days = seconds / 86400
    if days < 1:
        hours = int(seconds // 3600)
        return "less than an hour ago" if hours < 1 else f"about {hours} hours ago"
    if days < 14:
        return f"about {int(days)} days ago"
    if days < 60:
        return f"about {int(days // 7)} weeks ago"
    if days < 730:
        return f"about {int(days // 30)} months ago"
    return f"about {int(days // 365)} years ago"


def recency_weight(now: datetime, then: datetime, half_life_days: float) -> float:
    """Gentle tie-breaker between 0.5 and 1.0. Old items are never zeroed."""
    if half_life_days <= 0:
        return 1.0
    age_days = max(0.0, (now - then).total_seconds() / 86400)
    return 0.5 + 0.5 * (0.5 ** (age_days / half_life_days))
