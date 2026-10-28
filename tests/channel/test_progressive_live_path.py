"""Live progressive path: Teacher stream deltas reach ProgressiveWhatsAppDelivery.

Infrastructure only — no educational branching. Confirms the end-to-end
wiring from Teacher on_text_delta → ProgressiveWhatsAppDelivery → send_text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from support import add_student, make_world
from waxprep.channel.contracts import NormalizedInbound, OutboundPayload, OutboundResult
from waxprep.channel.whatsapp.handler import (
    WhatsAppInboundConfig,
    WhatsAppInboundHandler,
)
from waxprep.context.models import ContextPackage
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    AuthorKind,
    ChannelIdentity,
    HistoryEntry,
    OutboundStatus,
    new_id,
)
from waxprep.model_gateway.gateway import ModelGateway
from waxprep.model_gateway.models import (
    ModelCapability,
    ModelRole,
)
from waxprep.model_gateway.providers.mock import MockProvider
from waxprep.model_gateway.registry import ModelConfig, ModelRegistry
from waxprep.orchestration.contracts_ci_first import TeacherInput
from waxprep.orchestration.model_teacher_ci_first import ModelTeacherCiFirst


@dataclass
class FakeWhatsAppClient:
    """Captures progressive send_text / send_typing calls."""

    texts: list[str] = field(default_factory=list)
    typing_calls: int = 0

    async def send_text(self, payload: OutboundPayload) -> OutboundResult:
        self.texts.append(payload.text or "")
        return OutboundResult(
            success=True,
            provider_message_id=f"wamid.prog.{len(self.texts)}",
            temporary_failure=False,
        )

    async def send_typing(
        self,
        recipient_external_id: str,
        *,
        message_id: str | None = None,
    ) -> None:
        self.typing_calls += 1


@pytest.mark.asyncio
async def test_live_progressive_delivery_sends_chunks_during_teach() -> None:
    world = make_world()
    wax_id = add_student(world)
    storage = world.storage
    now = datetime.now(tz=UTC)
    storage.create_channel_identity(
        ChannelIdentity(
            id=new_id(),
            wax_id=wax_id,
            channel="whatsapp",
            external_id="+15551234567",
            created_at=now,
        )
    )

    client = FakeWhatsAppClient()
    handler = WhatsAppInboundHandler(
        storage=storage,
        history=world.services.history,
        turn_service=world.services.turns,
        clock=world.clock,
        config=WhatsAppInboundConfig(),
        whatsapp_client=client,  # type: ignore[arg-type]
    )

    msg = NormalizedInbound(
        channel="whatsapp",
        provider_sender_id="+15551234567",
        wax_id=wax_id,
        provider_message_id=f"wamid.{uuid4().hex}",
        occurred_at=now,
        text="Explain photosynthesis briefly.",
    )

    result = await handler.handle_message(msg)

    assert result.accepted
    assert result.student_entry_id is not None
    assert len(client.texts) >= 1
    assert any(t.strip() for t in client.texts)

    tutor = storage.find_tutor_reply_to(wax_id, result.student_entry_id)
    assert tutor is not None
    outbound = storage.get_outbound_delivery_for_tutor(wax_id, tutor.id, "whatsapp")
    assert outbound is not None
    assert outbound.status == OutboundStatus.DELIVERED


@pytest.mark.asyncio
async def test_teacher_on_text_delta_receives_stream_chunks() -> None:
    """ModelTeacherCiFirst forwards TEXT_DELTA events to the sink."""
    provider = MockProvider(response_text="Hello student from mock stream")
    registry = ModelRegistry()
    registry.register_provider(provider)
    registry.register_model(
        ModelConfig(
            name="teacher",
            provider=provider.name,
            model="mock",
            role=ModelRole.TEACHER,
            capabilities=frozenset(
                {ModelCapability.TEXT_INPUT, ModelCapability.STREAMING}
            ),
        )
    )
    gateway = ModelGateway(registry)
    teacher = ModelTeacherCiFirst(gateway)

    deltas: list[str] = []

    async def sink(text: str) -> None:
        deltas.append(text)

    wax = new_wax_id()
    now = datetime.now(tz=UTC)
    entry = HistoryEntry(
        id=new_id(),
        wax_id=wax,
        author_kind=AuthorKind.STUDENT,
        content_kind="text/plain",
        text="Hi",
        occurred_at=now,
        recorded_at=now,
    )
    outcome = await teacher.teach(
        TeacherInput(
            wax_id=wax,
            original_message=entry,
            context_package=ContextPackage(
                wax_id=wax,
                query="Hi",
                evidence=(),
                summary="",
            ),
            now=now,
        ),
        on_text_delta=sink,
    )
    assert outcome.reply_text is not None
    assert "Hello" in outcome.reply_text
    assert deltas
    assert any("Hello" in d or "student" in d for d in deltas)
    assert provider.calls >= 1
