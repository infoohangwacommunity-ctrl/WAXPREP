"""Temporal reasoning helpers — currentness is not importance."""

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
