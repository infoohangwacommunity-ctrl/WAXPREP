"""Context Intelligence model protocol."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from waxprep.context.models import ExchangeTurn
from waxprep.domain.identifiers import WaxId


@dataclass(frozen=True, slots=True)
class ContextModelRequest:
    system_prompt: str
    user_query: str
    evidence: str
    tools: list[dict[str, Any]]
    max_output_tokens: int
    wax_id: WaxId
    now: datetime
    exchange: tuple[ExchangeTurn, ...] = ()
    """The live exchange material the application supplied for this turn.

    CI sees it so it can decide what the Teacher needs in order to be
    *inside* the exchange. The application assembled it without judging
    it; CI decides whether it travels."""

    exchange_empty: bool = False
    """True when the application had no exchange material to supply — the
    student has no prior exchange yet. CI is not being asked to carry
    anything in that case, and is told so plainly rather than left to
    infer it from a missing block."""


@dataclass(frozen=True, slots=True)
class ContextModelDecision:
    action: str
    arguments: dict[str, Any]
    reason: str


@dataclass(frozen=True, slots=True)
class ContextModelFinal:
    summary: str
    selected_evidence_ids: tuple[str, ...]
    uncertainty: tuple[str, ...]
    stopped_reason: str
    include_exchange: bool = True
    """Whether the Teacher receives the exchange material in the package.

    The default is *include*. The Teacher cannot see the thread unless
    CI carries it, so silence must never mean "dropped" — a malformed or
    missing answer keeps the exchange, and dropping it has to be an
    explicit, reasoned decision."""

    include_exchange_reason: str = ""
    """Why CI included or dropped the exchange. Recorded in the audit
    trail so the decision is observable rather than invisible."""


class ContextModel(Protocol):
    async def decide(self, request: ContextModelRequest) -> ContextModelDecision: ...

    async def finalize(self, request: ContextModelRequest) -> ContextModelFinal: ...
