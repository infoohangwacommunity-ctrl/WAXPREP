"""End-to-end test runner for the golden conversation dataset.

For each scenario in ``golden_conversations.ALL_SCENARIOS``:

1. Materialise the history seeds as real ``HistoryEntry`` rows in an
   in-memory storage.
2. Materialise the notebook seeds as notebook entries citing the
   corresponding history entries as evidence.
3. Save the trigger message as a new history entry.
4. Drive the investigator with a scripted ``MockContextModel`` whose
   decisions come from the scenario. Where the scenario's decisions
   reference history ids (e.g. ``item_id`` for ``trace_sources``), the
   runner substitutes the real materialised ids.
5. Assert on the resulting ``ContextPackage``:
   - ``investigation_status`` matches ``expected_status``
   - ``uncertainty`` is non-empty iff ``expects_uncertainty``
   - evidence count >= ``min_evidence_count``
   - the audit trail records ``proposal_accepted`` / ``proposal_rejected``
     according to the scenario
   - if ``must_not_propose``, no ``proposal_*`` entry appears in the
     audit trail

These are component evaluations (scripted model, no live AI calls). They
verify that the application behaves correctly when the model makes the
expected decisions. Live-model evaluation (M3 end-to-end) is gated
behind the existing ``-m live`` marker.

Blueprint §16 M5: "Before changing prompts or retrieval behaviour, run
the suite against the current implementation and record the results."
This file is that suite. A regression here means a code change broke
a previously-working scenario.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from uuid import UUID

import pytest

from evaluation.golden_conversations import (
    ALL_SCENARIOS,
    BASELINE_NOW,
    GoldenScenario,
)
from support import TEST_CIPHER
from waxprep.context.evolution import EvolutionService
from waxprep.context.investigator import ContextInvestigator
from waxprep.context.mock_model import MockContextModel
from waxprep.context.model import ContextModelDecision
from waxprep.context.models import InvestigationLimits, InvestigationRequest
from waxprep.context.tool_impls import build_default_tools
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    AuthorKind,
    EmbeddingRecord,
    HistoryEntry,
    ObjectType,
    Student,
    StudentStatus,
    new_id,
)
from waxprep.embeddings.protocol import EmbeddingProvider
from waxprep.index.indexer import Indexer
from waxprep.index.tokens import TokenIndex
from waxprep.security.cipher import AesGcmFieldCipher
from waxprep.storage.memory import InMemoryStorage


class WordBagEmbeddingProvider:
    """Deterministic mock embedder for offline evaluation.

    Real semantic embedders produce correlated vectors for similar-meaning
    text. This stand-in produces correlated vectors for texts that share
    words: each word hashes to a dimension, and the vector is the
    normalised sum of one-hot encodings. Cosine similarity is then
    proportional to word overlap.

    This is NOT a real semantic embedder — it's a deterministic stand-in
    so ``find_by_meaning`` returns relevant results in offline tests
    (where no paid embedding API is available). Live-model evaluation
    (M3 end-to-end) uses the real configured embedder.
    """

    dimensions: int = 256
    model: str = "mock-word-bag"

    async def embed(self, text: str) -> tuple[float, ...]:
        vec = [0.0] * self.dimensions
        for word in text.lower().split():
            cleaned = word.strip(".,!?;:'\"()[]{}").strip()
            if not cleaned:
                continue
            h = int(hashlib.sha256(cleaned.encode("utf-8")).hexdigest(), 16)
            vec[h % self.dimensions] += 1.0
        mag = sum(v * v for v in vec) ** 0.5
        if mag == 0:
            return tuple(0.0 for _ in range(self.dimensions))
        return tuple(v / mag for v in vec)

    async def embed_many(self, texts: list[str]) -> list[tuple[float, ...]]:
        return [await self.embed(t) for t in texts]


async def _index_embeddings_for_history(
    storage: InMemoryStorage,
    embedder: EmbeddingProvider,
    wax_id,
) -> None:
    """Generate and store embeddings for every history entry AND every
    notebook entry.

    In production, the background worker's ``_embed`` job handler does
    this. In the test runner, we do it inline so ``find_by_meaning`` has
    real vectors to score against — for both history and notebook entries.
    """
    # History embeddings.
    entries = storage.list_history(wax_id, limit=500, newest_first=False)
    for entry in entries:
        text = entry.text or ""
        if not text.strip():
            continue
        vector = await embedder.embed(text)
        storage.put_embedding(
            EmbeddingRecord(
                id=new_id(),
                wax_id=wax_id,
                object_type=ObjectType.HISTORY,
                object_id=entry.id,
                object_time=entry.occurred_at,
                model=embedder.model,
                vector=vector,
                created_at=BASELINE_NOW,
            )
        )
    # Notebook embeddings (so find_by_meaning can also surface notes).
    notebook = storage.get_notebook(wax_id)
    if notebook is not None:
        notes = storage.list_notebook_entries(
            wax_id, notebook.id, include_superseded=False
        )
        from waxprep.index.objects import flatten_payload

        for note in notes:
            text = flatten_payload(note.payload)
            if not text.strip():
                continue
            vector = await embedder.embed(text)
            storage.put_embedding(
                EmbeddingRecord(
                    id=new_id(),
                    wax_id=wax_id,
                    object_type=ObjectType.NOTEBOOK,
                    object_id=note.id,
                    object_time=note.created_at,
                    model=embedder.model,
                    vector=vector,
                    created_at=BASELINE_NOW,
                )
            )


class _FakeClock:
    """Clock that returns a fixed `now`."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def _make_history_entry(
    wax_id, *, text: str, author: AuthorKind, occurred_at: datetime
) -> HistoryEntry:
    return HistoryEntry(
        id=new_id(),
        wax_id=wax_id,
        author_kind=author,
        content_kind="text",
        text=text,
        occurred_at=occurred_at,
        recorded_at=occurred_at,
    )


def _materialise_scenario(
    scenario: GoldenScenario, storage: InMemoryStorage, cipher: AesGcmFieldCipher
) -> tuple:
    """Build the scenario's world in storage. Returns (wax_id, history_ids,
    notebook_entry_ids, trigger_entry).

    ``history_ids`` is a tuple aligned with ``scenario.history_seeds``;
    the trigger message is appended as an extra entry at the end.
    """
    wax_id = new_wax_id()
    storage.create_student(
        Student(
            wax_id=wax_id,
            created_at=BASELINE_NOW,
            updated_at=BASELINE_NOW,
            status=StudentStatus.ACTIVE,
        )
    )
    # Indexer + token index for history-token indexing.
    indexer = Indexer(
        storage=storage,
        token_index=TokenIndex(cipher=cipher),
        clock=_FakeClock(BASELINE_NOW),
    )
    # Materialise history seeds AND index their text so find_exact can
    # retrieve them. (append_history_entry alone does not index; the
    # token index is written separately via the indexer.)
    history_ids: list[UUID] = []
    for seed in scenario.history_seeds:
        entry = _make_history_entry(
            wax_id,
            text=seed.text,
            author=seed.author_kind,
            occurred_at=seed.occurred_at,
        )
        storage.append_history_entry(entry)
        # Write blind word index so find_exact works.
        indexer.index(
            wax_id,
            ObjectType.HISTORY,
            entry.id,
            seed.text,
            seed.occurred_at,
        )
        history_ids.append(entry.id)
    # Save the trigger message as a new history entry (the investigator
    # receives it via InvestigationRequest.query, but it must also be in
    # history so browse_time_window can find it).
    trigger_entry = _make_history_entry(
        wax_id,
        text=scenario.trigger_message,
        author=AuthorKind.STUDENT,
        occurred_at=BASELINE_NOW,
    )
    storage.append_history_entry(trigger_entry)
    indexer.index(
        wax_id,
        ObjectType.HISTORY,
        trigger_entry.id,
        scenario.trigger_message,
        BASELINE_NOW,
    )

    # Materialise notebook seeds.
    evolution = EvolutionService(storage=storage, indexer=indexer)
    notebook_entry_ids: list[UUID] = []
    for seed in scenario.notebook_seeds:
        if seed.supporting_history_index >= len(history_ids):
            pytest.skip(
                f"scenario {scenario.scenario_id}: notebook seed cites "
                f"history index {seed.supporting_history_index} but only "
                f"{len(history_ids)} seeds exist"
            )
        supporting_id = history_ids[seed.supporting_history_index]
        from waxprep.context.proposals import EvidenceRef, Operation, Proposal

        result = evolution.apply(
            Proposal(
                wax_id=wax_id,
                operation=Operation.ADD_NOTEBOOK_ENTRY.value,
                payload={"payload": seed.payload},
                evidence=(
                    EvidenceRef(
                        object_type=ObjectType.HISTORY, object_id=supporting_id
                    ),
                ),
                reason="scenario setup",
            ),
            now=BASELINE_NOW,
        )
        assert result.accepted, result.reason
        assert result.created_notebook_entry_id is not None
        notebook_entry_ids.append(result.created_notebook_entry_id)

    return wax_id, tuple(history_ids), tuple(notebook_entry_ids), trigger_entry


def _substitute_ids_in_decisions(
    scenario: GoldenScenario,
    history_ids: tuple[UUID, ...],
    notebook_entry_ids: tuple[UUID, ...],
    trigger_entry: HistoryEntry,
) -> tuple[ContextModelDecision, ...]:
    """Where a scenario's scripted decision references a placeholder id
    (empty string) or an evidence-history index, substitute the real
    materialised id.

    Substitution rules (conservative — only where the scenario clearly
    intends it):

    - ``trace_sources`` / ``check_currency`` with ``item_id=""`` and
      ``item_type="notebook"`` → substitute the first notebook entry id.
    - ``trace_sources`` with ``item_id=""`` and ``item_type="history"``
      → substitute the trigger entry id.
    - ``propose_lifecycle_change`` with ``evidence_refs=[]`` → fill
      with the scenario's ``expected_proposal.evidence_history_indices``
      pointing at the materialised history ids (plus the trigger entry
      when index == 0 and the trigger is the only history).
    - ``propose_lifecycle_change`` with ``payload.old_entry_id=""`` →
      substitute the first notebook entry id.

    IMPORTANT: this function deep-copies every decision's arguments so
    the original scenario (a module-level singleton) is never mutated.
    A previous version used a shallow ``dict(decision.arguments)`` and
    mutated the payload in place — that broke test isolation because
    the second run found the stale id from the first run and the
    substitution check ``payload.get("old_entry_id") == ""`` was False.
    """
    import copy

    out: list[ContextModelDecision] = []
    for decision in scenario.scripted_decisions:
        if decision.action != "propose_lifecycle_change":
            # Substitute ids in trace_sources / check_currency.
            new_args = _substitute_read_tool_args(
                decision, notebook_entry_ids, trigger_entry
            )
            out.append(ContextModelDecision(decision.action, new_args, decision.reason))
            continue
        # propose_lifecycle_change: deep-copy arguments so the singleton
        # scenario is not mutated across runs.
        new_args = copy.deepcopy(decision.arguments)
        # Evidence refs.
        if not new_args.get("evidence_refs") and scenario.expected_proposal is not None:
            indices = scenario.expected_proposal.evidence_history_indices
            refs: list[dict[str, Any]] = []
            if not history_ids:
                refs.append(
                    {
                        "object_type": ObjectType.HISTORY.value,
                        "object_id": str(trigger_entry.id),
                    }
                )
            else:
                for idx in indices:
                    if idx < len(history_ids):
                        refs.append(
                            {
                                "object_type": ObjectType.HISTORY.value,
                                "object_id": str(history_ids[idx]),
                            }
                        )
            new_args["evidence_refs"] = refs
        # old_entry_id for supersede_notebook_entry.
        payload = new_args.get("payload")
        if isinstance(payload, dict) and payload.get("old_entry_id") == "":
            if notebook_entry_ids:
                payload["old_entry_id"] = str(notebook_entry_ids[0])
        out.append(ContextModelDecision(decision.action, new_args, decision.reason))
    return tuple(out)


def _substitute_read_tool_args(
    decision: ContextModelDecision,
    notebook_entry_ids: tuple[UUID, ...],
    trigger_entry: HistoryEntry,
) -> dict[str, Any]:
    args = dict(decision.arguments)
    if decision.action in ("trace_sources", "check_currency"):
        item_id = args.get("item_id")
        item_type = args.get("item_type")
        if item_id == "" and item_type == "notebook" and notebook_entry_ids:
            args["item_id"] = str(notebook_entry_ids[0])
        elif item_id == "" and item_type == "history":
            args["item_id"] = str(trigger_entry.id)
    return args


def _build_investigator(
    scenario: GoldenScenario,
    storage: InMemoryStorage,
    cipher: AesGcmFieldCipher,
    wax_id,
    history_ids: tuple[UUID, ...],
    notebook_entry_ids: tuple[UUID, ...],
    trigger_entry: HistoryEntry,
) -> ContextInvestigator:
    """Build the investigator with the scenario's tools wired in and
    the scripted decisions substituted with real ids."""
    indexer = Indexer(
        storage=storage,
        token_index=TokenIndex(cipher=cipher),
        clock=_FakeClock(BASELINE_NOW),
    )
    evolution = EvolutionService(storage=storage, indexer=indexer)
    from waxprep.context.retrieval import Retriever

    embedder = WordBagEmbeddingProvider()
    retriever = Retriever(storage=storage, token_index=TokenIndex(cipher=cipher))
    tools = build_default_tools(storage, retriever, embedder, evolution=evolution)

    # For the relationships scenario, we need knowledge nodes, not notebook
    # entries. Materialise them now from the history seeds so the model's
    # create_edge proposal can reference real node ids.
    if scenario.scenario_id == "model_proposes_edge_with_evidence":
        import copy as _copy

        from waxprep.context.proposals import EvidenceRef, Operation, Proposal

        node_ids: list[UUID] = []
        for i, seed in enumerate(scenario.history_seeds):
            result = evolution.apply(
                Proposal(
                    wax_id=wax_id,
                    operation=Operation.CREATE_NODE.value,
                    payload={
                        "node_type": "concept",
                        "label": f"concept_{i}",
                        "description": seed.text,
                    },
                    evidence=(
                        EvidenceRef(
                            object_type=ObjectType.HISTORY, object_id=history_ids[i]
                        ),
                    ),
                    reason="scenario setup: knowledge node",
                ),
                now=BASELINE_NOW,
            )
            assert result.accepted, result.reason
            assert result.created_node_id is not None
            node_ids.append(result.created_node_id)
        # Patch the model's create_edge decision to reference these node ids.
        # Deep-copy arguments so the singleton scenario is not mutated
        # across runs (test isolation).
        patched_decisions: list[ContextModelDecision] = []
        for d in scenario.scripted_decisions:
            if d.action == "propose_lifecycle_change":
                new_args = _copy.deepcopy(d.arguments)
                payload = new_args.get("payload")
                if isinstance(payload, dict):
                    if payload.get("source_node_id") == "" and len(node_ids) >= 1:
                        payload["source_node_id"] = str(node_ids[0])
                    if payload.get("target_node_id") == "" and len(node_ids) >= 2:
                        payload["target_node_id"] = str(node_ids[1])
                # Evidence refs.
                if not new_args.get("evidence_refs") and scenario.expected_proposal:
                    refs = []
                    for idx in scenario.expected_proposal.evidence_history_indices:
                        if idx < len(history_ids):
                            refs.append(
                                {
                                    "object_type": ObjectType.HISTORY.value,
                                    "object_id": str(history_ids[idx]),
                                }
                            )
                    new_args["evidence_refs"] = refs
                patched_decisions.append(
                    ContextModelDecision(d.action, new_args, d.reason)
                )
            else:
                patched_decisions.append(_copy.deepcopy(d))
        scripted_decisions = tuple(patched_decisions)
    else:
        scripted_decisions = _substitute_ids_in_decisions(
            scenario, history_ids, notebook_entry_ids, trigger_entry
        )

    # Build a MockContextModel with the scripted decisions.
    model = MockContextModel(
        decision_script=list(scripted_decisions),
        final=scenario.scripted_final,
    )
    return ContextInvestigator(model=model, tools=tools)


def _run_scenario(
    scenario: GoldenScenario,
) -> tuple:
    """Materialise the scenario, run the investigator, return (package,
    wax_id, storage) for assertions."""
    storage = InMemoryStorage()
    cipher = TEST_CIPHER
    wax_id, history_ids, notebook_entry_ids, trigger_entry = _materialise_scenario(
        scenario, storage, cipher
    )
    # Generate embeddings for every history entry so find_by_meaning has
    # real vectors to score against. (In production the worker's _embed
    # job handler does this; here we do it inline for determinism.)
    import asyncio

    embedder = WordBagEmbeddingProvider()
    asyncio.run(_index_embeddings_for_history(storage, embedder, wax_id))
    investigator = _build_investigator(
        scenario,
        storage,
        cipher,
        wax_id,
        history_ids,
        notebook_entry_ids,
        trigger_entry,
    )
    request = InvestigationRequest(
        wax_id=wax_id,
        query=scenario.trigger_message,
        now=BASELINE_NOW,
        trigger_entry_id=trigger_entry.id,
        limits=InvestigationLimits(max_steps=10, max_tool_calls=10, max_evidence=20),
    )
    package = asyncio.run(investigator.investigate(request))
    return package, wax_id, storage


# ---------------------------------------------------------------------------
# Parameterised test over every golden scenario
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario", ALL_SCENARIOS, ids=[s.scenario_id for s in ALL_SCENARIOS]
)
def test_golden_scenario_investigation_status_matches_expected(
    scenario: GoldenScenario,
) -> None:
    """The runtime-computed investigation status must match the scenario's
    expected_status. This is the closed-vocabulary contract: the
    Teacher can branch on the status without parsing prose."""
    package, _, _ = _run_scenario(scenario)
    assert package.investigation_status is scenario.expected_status, (
        f"scenario {scenario.scenario_id}: expected status "
        f"{scenario.expected_status.value}, got "
        f"{package.investigation_status.value}; investigated="
        f"{package.investigated}"
    )


@pytest.mark.parametrize(
    "scenario", ALL_SCENARIOS, ids=[s.scenario_id for s in ALL_SCENARIOS]
)
def test_golden_scenario_uncertainty_contract(scenario: GoldenScenario) -> None:
    """If the scenario expects uncertainty, the package must surface it.
    If the scenario does not expect uncertainty, the package may still
    carry it (e.g. tool failures add uncertainty) — but the contract
    is one-directional: when expected, it must be present."""
    package, _, _ = _run_scenario(scenario)
    if scenario.expects_uncertainty:
        assert package.uncertainty, (
            f"scenario {scenario.scenario_id}: expected uncertainty but "
            f"the package carried none. investigated={package.investigated}"
        )


@pytest.mark.parametrize(
    "scenario", ALL_SCENARIOS, ids=[s.scenario_id for s in ALL_SCENARIOS]
)
def test_golden_scenario_evidence_count(scenario: GoldenScenario) -> None:
    """If the scenario specifies a minimum evidence count, the package
    must carry at least that many items. The evidence must be items
    actually returned by tools (the investigator's invariant)."""
    if scenario.min_evidence_count == 0:
        pytest.skip("scenario does not specify a minimum evidence count")
    package, _, _ = _run_scenario(scenario)
    assert len(package.evidence) >= scenario.min_evidence_count, (
        f"scenario {scenario.scenario_id}: expected at least "
        f"{scenario.min_evidence_count} evidence items, got "
        f"{len(package.evidence)}. investigated={package.investigated}"
    )


@pytest.mark.parametrize(
    "scenario", ALL_SCENARIOS, ids=[s.scenario_id for s in ALL_SCENARIOS]
)
def test_golden_scenario_no_unexpected_proposal(scenario: GoldenScenario) -> None:
    """If the scenario is marked ``must_not_propose``, the audit trail
    must contain no ``proposal_*`` entry. The model must not have
    proposed any lifecycle change."""
    if not scenario.must_not_propose:
        pytest.skip("scenario does not require absence of proposals")
    package, _, _ = _run_scenario(scenario)
    for step in package.investigated:
        assert not step.startswith("proposal_accepted"), (
            f"scenario {scenario.scenario_id}: must_not_propose is set, "
            f"but the audit trail records {step}. investigated="
            f"{package.investigated}"
        )
        assert not step.startswith("proposal_rejected"), (
            f"scenario {scenario.scenario_id}: must_not_propose is set, "
            f"but the audit trail records {step}. investigated="
            f"{package.investigated}"
        )


@pytest.mark.parametrize(
    "scenario", ALL_SCENARIOS, ids=[s.scenario_id for s in ALL_SCENARIOS]
)
def test_golden_scenario_no_notebook_change_when_unexpected(
    scenario: GoldenScenario,
) -> None:
    """If the scenario expects no notebook change, the notebook entry
    count after the investigation must equal the count before. The
    investigator must not have proposed a write that the application
    accepted."""
    if not scenario.expects_no_notebook_change:
        pytest.skip("scenario does not require absence of notebook changes")
    package, wax_id, storage = _run_scenario(scenario)
    # Count notebook entries after the run. The notebook container is
    # created lazily on the first add_notebook_entry; for scenarios with
    # no notebook_seeds, no notebook exists, and the count is 0.
    notebook = storage.get_notebook(wax_id)
    if notebook is None:
        post_count = 0
    else:
        entries = storage.list_notebook_entries(
            wax_id, notebook.id, include_superseded=True
        )
        post_count = len(entries)
    pre_count = len(scenario.notebook_seeds)
    assert post_count == pre_count, (
        f"scenario {scenario.scenario_id}: expected notebook entry count "
        f"to stay at {pre_count} (no notebook change), got {post_count}. "
        f"investigated={package.investigated}"
    )


@pytest.mark.parametrize(
    "scenario", ALL_SCENARIOS, ids=[s.scenario_id for s in ALL_SCENARIOS]
)
def test_golden_scenario_proposal_outcome(scenario: GoldenScenario) -> None:
    """If the scenario expects a proposal, the audit trail must record
    ``proposal_accepted:<operation>`` for that operation. The model's
    claim of an operation is not sufficient — the application's verdict
    is what persists."""
    if scenario.expected_proposal is None:
        pytest.skip("scenario does not expect a proposal")
    package, _, _ = _run_scenario(scenario)
    expected_op = scenario.expected_proposal.operation
    assert any(
        f"proposal_accepted:{expected_op}" in step for step in package.investigated
    ), (
        f"scenario {scenario.scenario_id}: expected proposal_accepted:"
        f"{expected_op} in the audit trail. investigated="
        f"{package.investigated}"
    )


# ---------------------------------------------------------------------------
# Coverage smoke test
# ---------------------------------------------------------------------------


def test_evaluation_dataset_covers_all_m2_capabilities() -> None:
    """The golden dataset must cover every M2 capability family listed
    in the blueprint. A missing capability is a regression in the
    evaluation surface."""
    from evaluation.golden_conversations import capabilities_covered

    expected_capabilities = {
        "interpretation",
        "write_decisions",
        "contradictions",
        "provenance",
        "retrieval",
        "temporal_reasoning",
        "uncertainty",
        "summarisation",
        "long_messages",
        "relationships",
        "tool_execution",
        "security",
    }
    actual = set(capabilities_covered())
    missing = expected_capabilities - actual
    assert not missing, (
        f"golden dataset is missing capabilities: {missing}. Every M2 "
        "family must have at least one scenario."
    )


def test_evaluation_dataset_has_at_least_one_scenario_per_capability() -> None:
    """Each capability must have at least one scenario — a single
    scenario per family is the minimum; future work should add more."""
    from evaluation.golden_conversations import (
        ALL_SCENARIOS,
        capabilities_covered,
    )

    for cap in capabilities_covered():
        scenarios = [s for s in ALL_SCENARIOS if s.capability == cap]
        assert len(scenarios) >= 1, (
            f"capability {cap} has no scenarios; the dataset is incomplete"
        )
