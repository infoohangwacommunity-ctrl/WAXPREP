"""Shared builders for tests. Fake data only; no network, no paid calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from waxprep.clock import FakeClock
from waxprep.context.mock_model import MockContextModel
from waxprep.derive.artifacts import ArtifactProcessor
from waxprep.derive.consolidation import ConsolidationModel
from waxprep.domain.identifiers import WaxId, new_wax_id
from waxprep.domain.models import Student, StudentStatus
from waxprep.embeddings.protocol import EmbeddingProvider
from waxprep.model_gateway.gateway import ModelGateway
from waxprep.model_gateway.models import ModelRole
from waxprep.model_gateway.providers.mock import MockProvider
from waxprep.model_gateway.registry import ModelConfig, ModelRegistry
from waxprep.orchestration import (
    TeacherInput,
    TeacherOutcome,
)
from waxprep.orchestration.model_teacher import ModelTeacher
from waxprep.security.cipher import AesGcmFieldCipher, KeyRing
from waxprep.storage.memory import InMemoryStorage
from waxprep.storage.protocol import Storage
from waxprep.wiring import WaxPrepServices, build_services

TEST_CIPHER = AesGcmFieldCipher(KeyRing(keys={1: b"t" * 32}, active_key_id=1))
START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


@dataclass
class RecordingTeacher:
    """Teacher test double implementing the new Teacher Protocol.

    Records every input it receives so tests can assert that the exact
    original message reached the Teacher untouched. By default it
    responds immediately without consulting CI; tests can configure
    consultation behaviour via ``script``.
    """

    reply_text: str = "Let's work on it together."
    consult_question: str | None = None
    seen_inputs: list[TeacherInput] = field(default_factory=list)

    async def teach(self, inp: TeacherInput) -> TeacherOutcome:
        self.seen_inputs.append(inp)
        if self.consult_question and inp.round_number == 0:
            # Consult CI once, then respond.
            from waxprep.orchestration import ConsultationRequest

            result = await inp.consultation.consult(
                ConsultationRequest(
                    wax_id=inp.wax_id,
                    original_message=inp.original_message,
                    teacher_question=self.consult_question,
                    now=inp.now,
                    prior_results=inp.prior_results,
                )
            )
            return TeacherOutcome(
                reply_text=None,
                next_question=self.consult_question,
                context_used=(*inp.prior_results, result),
                finished=False,
            )
        return TeacherOutcome(
            reply_text=self.reply_text,
            next_question=None,
            context_used=inp.prior_results,
            finished=True,
        )


@dataclass
class World:
    storage: Storage
    clock: FakeClock
    services: WaxPrepServices
    context_model: MockContextModel
    teacher: RecordingTeacher


@dataclass
class ModelTeacherWorld:
    """Test world wired with the real ``ModelTeacher`` over a ``MockProvider``."""

    storage: Storage
    clock: FakeClock
    services: WaxPrepServices
    context_model: MockContextModel
    provider: MockProvider
    gateway: ModelGateway
    teacher: ModelTeacher


def make_teacher_gateway(provider: MockProvider) -> ModelGateway:
    """Registry + gateway with a logical ``teacher`` model on the provider."""
    registry = ModelRegistry()
    registry.register_provider(provider)
    registry.register_model(
        ModelConfig(
            name="teacher",
            provider=provider.name,
            model="mock-teacher-endpoint",
            role=ModelRole.TEACHER,
            capabilities=provider.capabilities("mock-teacher-endpoint"),
        )
    )
    return ModelGateway(registry)


def make_world(
    *,
    storage: Storage | None = None,
    embedder: EmbeddingProvider | None = None,
    context_model: MockContextModel | None = None,
    teacher: RecordingTeacher | None = None,
    consolidation_model: ConsolidationModel | None = None,
    artifact_processor: ArtifactProcessor | None = None,
    start: datetime = START,
    max_consultations: int = 3,
) -> World:
    clock = FakeClock(start)
    store: Storage = storage if storage is not None else InMemoryStorage()
    model = context_model or MockContextModel()
    tutor = teacher or RecordingTeacher()
    services = build_services(
        storage=store,
        cipher=TEST_CIPHER,
        clock=clock,
        context_model=model,
        teacher=tutor,
        embedder=embedder,
        consolidation_model=consolidation_model,
        artifact_processor=artifact_processor,
        max_consultations=max_consultations,
    )
    return World(store, clock, services, model, tutor)


def make_model_teacher_world(
    *,
    provider: MockProvider | None = None,
    context_model: MockContextModel | None = None,
    embedder: EmbeddingProvider | None = None,
    start: datetime = START,
    max_consultations: int = 3,
) -> ModelTeacherWorld:
    """World wired with the real ModelTeacher and a deterministic provider."""
    clock = FakeClock(start)
    store: Storage = InMemoryStorage()
    mock_provider = provider or MockProvider(
        response_text='{"action": "reply", "reply_text": "mock response"}'
    )
    gateway = make_teacher_gateway(mock_provider)
    tutor = ModelTeacher(gateway, model_name="teacher")
    model = context_model or MockContextModel()
    services = build_services(
        storage=store,
        cipher=TEST_CIPHER,
        clock=clock,
        context_model=model,
        teacher=tutor,
        embedder=embedder,
        max_consultations=max_consultations,
    )
    return ModelTeacherWorld(
        storage=store,
        clock=clock,
        services=services,
        context_model=model,
        provider=mock_provider,
        gateway=gateway,
        teacher=tutor,
    )


def add_student(world: World | ModelTeacherWorld) -> WaxId:
    now = world.clock.now()
    student = Student(
        wax_id=new_wax_id(), created_at=now, updated_at=now, status=StudentStatus.ACTIVE
    )
    world.storage.create_student(student)
    return student.wax_id
