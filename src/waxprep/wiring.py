"""One place where every part of the unified architecture is connected.

The architecture is Teacher-first:

    Student message
        |
        v
    Teacher (drives the turn)
        |   may consult Context Intelligence (bounded)
        v
    Teacher eventually produces the response
        |
        v
    Tutor response is durably saved + outbound delivery recorded

The Teacher intelligence itself is **intentionally unimplemented** in
this build. The Teacher is a Protocol; tests use ``MockTeacher``.
"""

from __future__ import annotations

from dataclasses import dataclass

from waxprep.clock import Clock
from waxprep.context.evolution import EvolutionService
from waxprep.context.investigator import ContextInvestigator
from waxprep.context.model import ContextModel
from waxprep.context.retrieval import Retriever
from waxprep.context.tool_impls import build_default_tools
from waxprep.derive.artifacts import ArtifactProcessor, ArtifactUnderstandingService
from waxprep.derive.consolidation import ConsolidationModel, ConsolidationService
from waxprep.derive.worker import DerivationWorker
from waxprep.embeddings.protocol import EmbeddingProvider
from waxprep.history.service import HistoryService
from waxprep.index.indexer import Indexer
from waxprep.index.tokens import TokenIndex
from waxprep.orchestration import Teacher, TurnService
from waxprep.security.cipher import FieldCipher
from waxprep.storage.protocol import Storage


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
    teacher: Teacher,
    embedder: EmbeddingProvider | None = None,
    consolidation_model: ConsolidationModel | None = None,
    artifact_processor: ArtifactProcessor | None = None,
    max_consultations: int = 3,
) -> WaxPrepServices:
    """Build the unified service graph.

    Args:
        max_consultations: Hard upper bound on Teacher↔CI consultations
            per student turn. The default (3) is conservative; the value
            is configuration, not buried business logic.
    """
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
        max_consultations=max_consultations,
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
