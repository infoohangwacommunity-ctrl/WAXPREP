"""One place where every part of the unified architecture is connected.

The architecture is CI-first:

    Student message
        |
        v
    Save exact student message
        |
        v
    Context Intelligence (mandatory for every message)
        |
        v
    CI produces ContextPackage
        |
        v
    Teacher receives: student message + ContextPackage
        |
        v
    Teacher produces final educational response
        |
        v
    Tutor response is durably saved + outbound delivery recorded

The production Teacher is ``orchestration.model_teacher_ci_first.ModelTeacherCiFirst``:
a model-backed adapter that runs on the existing ``ModelGateway`` and the
logical model registry. Pass either a ready ``Teacher`` or a
``gateway`` with a registered Teacher model (logical name ``"teacher"``,
``ModelRole.TEACHER``) to ``build_services``. Tests keep injecting the
deterministic ``MockTeacher`` / ``RecordingTeacher`` doubles.

OLD ARCHITECTURE (deprecated):
The old Teacher-first architecture is still available in turn.py and
model_teacher.py but is deprecated. Use CI-first for all new development.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from waxprep.clock import Clock
from waxprep.context.evolution import EvolutionService
from waxprep.context.investigator import ContextInvestigator
from waxprep.context.model import ContextModel
from waxprep.context.models import InvestigationLimits
from waxprep.context.retrieval import Retriever
from waxprep.context.tool_impls import build_default_tools
from waxprep.derive.artifacts import ArtifactProcessor, ArtifactUnderstandingService
from waxprep.derive.consolidation import ConsolidationModel, ConsolidationService
from waxprep.derive.worker import DerivationWorker
from waxprep.embeddings.protocol import EmbeddingProvider
from waxprep.history.service import HistoryService
from waxprep.index.indexer import Indexer
from waxprep.index.tokens import TokenIndex
from waxprep.model_gateway.gateway import ModelGateway
from waxprep.orchestration.contracts_ci_first import (
    DEFAULT_TEACHER_MODEL_NAME as DEFAULT_TEACHER_MODEL_NAME,
)
from waxprep.orchestration.contracts_ci_first import (
    Teacher as TeacherProtocol,
)
from waxprep.orchestration.model_teacher_ci_first import ModelTeacherCiFirst
from waxprep.orchestration.turn_ci_first import TurnServiceCiFirst as TurnService
from waxprep.security.cipher import FieldCipher
from waxprep.storage.protocol import Storage

if TYPE_CHECKING:
    pass


@dataclass(frozen=True, slots=True)
class WaxPrepServices:
    storage: Storage
    history: HistoryService
    indexer: Indexer
    retriever: Retriever
    evolution: EvolutionService
    investigator: ContextInvestigator
    turns: TurnService
    worker: DerivationWorker
    consolidation: ConsolidationService | None
    artifacts: ArtifactUnderstandingService | None


def build_services(
    *,
    storage: Storage,
    cipher: FieldCipher,
    clock: Clock,
    context_model: ContextModel,
    teacher: TeacherProtocol | None = None,
    gateway: ModelGateway | None = None,
    teacher_model_name: str = DEFAULT_TEACHER_MODEL_NAME,
    embedder: EmbeddingProvider | None = None,
    consolidation_model: ConsolidationModel | None = None,
    artifact_processor: ArtifactProcessor | None = None,
    ci_limits: InvestigationLimits | None = None,
) -> WaxPrepServices:
    """Build the unified service graph.

    Args:
        teacher: A ready Teacher (production passes ``ModelTeacherCiFirst``;
            tests pass a deterministic double). Required when ``gateway``
            is not given.
        gateway: Provider-neutral model gateway. When given and ``teacher``
            is omitted, the real ``ModelTeacherCiFirst`` is composed against it
            using ``teacher_model_name``. The logical Teacher model must be
            registered in the gateway's registry with role
            ``ModelRole.TEACHER`` - a missing registration fails clearly
            when the model is first resolved.
        teacher_model_name: Logical Teacher model name in the registry.
        ci_limits: InvestigationLimits for CI. When None, defaults are used.
            CI runs with its own internal bounds (max_steps, max_tool_calls,
            max_evidence, max_context_characters, max_model_output_tokens).

    Raises:
        ValueError: If neither ``teacher`` nor ``gateway`` is provided, so
            a misconfigured composition fails at startup instead of
            silently teaching without a Teacher.
    """
    if teacher is None:
        if gateway is None:
            raise ValueError(
                "build_services requires a Teacher instance or a ModelGateway "
                "with a registered Teacher model (role ModelRole.TEACHER)"
            )
        # Composition-time validation: logical Teacher model must exist and
        # be registered with ModelRole.TEACHER (not CONTEXT or other roles).
        from waxprep.model_gateway.models import ModelRole
        from waxprep.model_gateway.registry import ModelRegistry

        registry = getattr(gateway, "_registry", None)
        if isinstance(registry, ModelRegistry):
            registry.require_model_role(teacher_model_name, ModelRole.TEACHER)
        teacher = ModelTeacherCiFirst(gateway, model_name=teacher_model_name)
    token_index = TokenIndex(cipher)
    has_embedder = embedder is not None
    has_artifacts = artifact_processor is not None
    indexer = Indexer(
        storage,
        token_index,
        clock,
        has_embedder=has_embedder,
        has_artifact_processor=has_artifacts,
    )
    history = HistoryService(storage, indexer, clock)
    retriever = Retriever(storage, token_index)
    evolution = EvolutionService(storage=storage, indexer=indexer)
    investigator = ContextInvestigator(
        model=context_model,
        tools=build_default_tools(storage, retriever, embedder),
    )
    artifacts = (
        ArtifactUnderstandingService(storage, indexer, artifact_processor, clock)
        if artifact_processor is not None
        else None
    )
    consolidation = (
        ConsolidationService(storage, evolution, indexer, consolidation_model, clock)
        if consolidation_model is not None
        else None
    )
    worker = DerivationWorker(
        storage, token_index, clock, embedder=embedder, artifacts=artifacts
    )
    turns = TurnService(
        storage,
        history,
        investigator,
        teacher,
        clock,
        ci_limits=ci_limits,
    )
    return WaxPrepServices(
        storage=storage,
        history=history,
        indexer=indexer,
        retriever=retriever,
        evolution=evolution,
        investigator=investigator,
        turns=turns,
        worker=worker,
        consolidation=consolidation,
        artifacts=artifacts,
    )
