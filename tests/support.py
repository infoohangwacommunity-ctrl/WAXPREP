"""Shared builders for tests. Fake data only; no network, no paid calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from waxprep.clock import FakeClock
from waxprep.context.mock_model import MockContextModel
from waxprep.context.models import ContextPackage
from waxprep.derive.artifacts import ArtifactProcessor
from waxprep.derive.consolidation import ConsolidationModel
from waxprep.domain.identifiers import WaxId, new_wax_id
from waxprep.domain.models import HistoryEntry, Student, StudentStatus
from waxprep.embeddings.protocol import EmbeddingProvider
from waxprep.security.cipher import AesGcmFieldCipher, KeyRing
from waxprep.storage.memory import InMemoryStorage
from waxprep.storage.protocol import Storage
from waxprep.wiring import WaxPrepServices, build_services

TEST_CIPHER = AesGcmFieldCipher(KeyRing(keys={1: b"t" * 32}, active_key_id=1))
START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


class RecordingTeacher:
    """Teacher test double: records what it was given."""

    def __init__(self, reply: str = "Let's work on it together.") -> None:
        self.reply_text = reply
        self.seen: list[tuple[HistoryEntry, ContextPackage]] = []

    async def reply(
        self, wax_id: WaxId, message: HistoryEntry, context: ContextPackage
    ) -> str:
        self.seen.append((message, context))
        return self.reply_text


@dataclass
class World:
    storage: Storage
    clock: FakeClock
    services: WaxPrepServices
    context_model: MockContextModel
    teacher: RecordingTeacher


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


def add_student(world: World) -> WaxId:
    now = world.clock.now()
    student = Student(
        wax_id=new_wax_id(), created_at=now, updated_at=now, status=StudentStatus.ACTIVE
    )
    world.storage.create_student(student)
    return student.wax_id
