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
from waxprep.orchestration.contracts_ci_first import (
    TeacherInput as TeacherInputCiFirst,
)
from waxprep.orchestration.contracts_ci_first import (
    TeacherOutcome as TeacherOutcomeCiFirst,
)
from waxprep.orchestration.model_teacher_ci_first import (
    ModelTeacherCiFirst as ModelTeacher,
)
from waxprep.security.cipher import AesGcmFieldCipher, KeyRing
from waxprep.storage.memory import InMemoryStorage
from waxprep.storage.protocol import Storage
from waxprep.wiring import WaxPrepServices, build_services

TEST_CIPHER = AesGcmFieldCipher(KeyRing(keys={1: b"t" * 32}, active_key_id=1))
START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


@dataclass
class RecordingTeacher:
    """Teacher test double implementing the CI-first Teacher Protocol.

    Records every input it receives so tests can assert that the exact
    original message and ContextPackage reached the Teacher.
    """

    reply_text: str = "Let's work on it together."
    consult_question: str | None = None
    seen_inputs: list[TeacherInputCiFirst] = field(default_factory=list)

    async def teach(self, inp: TeacherInputCiFirst) -> TeacherOutcomeCiFirst:
        self.seen_inputs.append(inp)
        return TeacherOutcomeCiFirst(reply_text=self.reply_text)


@dataclass
class World:
    storage: Storage
    clock: FakeClock
    services: WaxPrepServices
    context_model: MockContextModel
    teacher: RecordingTeacher


@dataclass
class ModelTeacherWorld:
    """Test world wired with the real ``ModelTeacherCiFirst`` over a ``MockProvider``."""

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
    )
    return World(store, clock, services, model, tutor)


def make_model_teacher_world(
    *,
    provider: MockProvider | None = None,
    context_model: MockContextModel | None = None,
    embedder: EmbeddingProvider | None = None,
    start: datetime = START,
) -> ModelTeacherWorld:
    clock = FakeClock(start)
    store: Storage = InMemoryStorage()
    model = context_model or MockContextModel()
    p = provider or MockProvider()
    gateway = make_teacher_gateway(p)
    tutor = ModelTeacher(gateway)
    services = build_services(
        storage=store,
        cipher=TEST_CIPHER,
        clock=clock,
        context_model=model,
        teacher=tutor,
        gateway=gateway,
        embedder=embedder,
    )
    return ModelTeacherWorld(store, clock, services, model, p, gateway, tutor)


def add_student(
    world: World | ModelTeacherWorld, *, name: str = "Test Student"
) -> WaxId:
    """Create and return a new student in the world's storage."""
    wax_id = new_wax_id()
    world.storage.create_student(
        Student(
            wax_id=wax_id,
            created_at=world.clock.now(),
            updated_at=world.clock.now(),
            status=StudentStatus.ACTIVE,
        )
    )
    return wax_id


# Aliases for backward compatibility
TeacherInput = TeacherInputCiFirst
TeacherOutcome = TeacherOutcomeCiFirst
