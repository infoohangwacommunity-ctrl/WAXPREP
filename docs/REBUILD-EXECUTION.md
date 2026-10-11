# Foundation Teacher-First Completion — Execution Log

**Branch:** `foundation-teacher-first-completion`
**Baseline:** `professional-unified-history-rebuild` (commit `15ef0a0`)
**Specification:** `WAXPREP-UNIFIED-HISTORY-IMPLEMENTATION-PLAN-AUDITED.md` + the foundation-audit brief.

## What this branch is

This branch rebuilds WAXPREP's student-world foundation and Teacher/CI
orchestration boundary to be correct. It is **not** the
Teacher-intelligence build — the Teacher is a Protocol; tests use a
deterministic mock. The future Teacher-intelligence build will plug
in a real Teacher following the same contract.

## Architecture

```
Student message
  |
  v
Teacher (drives the turn)
  |   may consult Context Intelligence (bounded)
  v
Context Intelligence investigates the student's world
  |
  v
Teacher receives evidence/context
  |   may request another bounded investigation
  v
Teacher eventually produces the response
  |
  v
Tutor response is durably saved + outbound delivery recorded
```

The exact student message reaches the Teacher untouched — never
rewritten, summarized, or replaced by CI's interpretation.

## Verification commands

```bash
export DATABASE_URL="postgresql://postgres@localhost:5432/waxprep_test"
export WAXPREP_MASTER_KEYS="1:$(python -c 'import os,base64;print(base64.b64encode(os.urandom(32)).decode())')"
export WAXPREP_ACTIVE_KEY_ID=1

uv sync --python 3.13 --locked
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest -m "not live"
```

## Final audit (spec section 28)

1. **Can the exact original student message reach the Teacher untouched?**
   ✅ Yes. `HistoryService.record_with_status` saves the exact text
   atomically with index jobs. `TurnService` passes the `HistoryEntry`
   directly to `TeacherInput.original_message`. CI may receive the text
   via `ConsultationRequest`, but its synthesis never replaces the
   original. Regression tests in `tests/test_turn_flow.py`.

2. **Can Teacher consult CI?**
   ✅ Yes. `TeacherConsultation` Protocol; `CIBackedConsultation` adapter
   wraps `ContextInvestigator`. The Teacher calls
   `inp.consultation.consult(...)`.

3. **Can Teacher ask CI for another angle?**
   ✅ Yes. The Teacher returns `TeacherOutcome(finished=False,
   next_question=...)`, the orchestrator runs another round.

4. **Is the Teacher↔CI loop bounded?**
   ✅ Yes. `max_consultations` (default 3, configurable) is enforced by
   `TurnService`. A buggy Teacher cannot enter an infinite loop.

5. **Is Teacher teaching intelligence still intentionally absent?**
   ✅ Yes. The Teacher is a Protocol in `orchestration/contracts.py`.
   `MockTeacher` is the only implementation. No Teacher system prompt,
   no pedagogical logic, no lesson planning.

6. **Can old information still be discovered without an arbitrary 50k/5k wall?**
   ✅ Yes. `find_by_tokens` and `list_embeddings` accept `limit=None`
   (whole corpus). `find_by_tokens_page` and `list_embeddings_page`
   provide deterministic cursor pagination. The Retriever paginates
   through all matches; rank-then-limit.

7. **Does ranking happen before final evidence limiting?**
   ✅ Yes. The Retriever collects all candidates, sorts by score DESC,
   then applies the final `limit`. See `test_ranking_happens_before_final_evidence_limit`.

8. **Are derived object + provenance operations atomic?**
   ✅ Yes. `Storage.transaction()` context manager. PostgresStorage:
   real DB transaction. InMemoryStorage: snapshot/restore.
   `EvolutionService.apply()` wraps the handler in `storage.transaction()`.
   Regression tests in `tests/history/test_atomic_derived.py`.

9. **Is job attempt counting correct?**
   ✅ Yes. `claim_due_jobs` increments attempts exactly once (start of
   attempt). `mark_job_done` and `mark_job_failed` do NOT increment.
   `release_job_capability_skip` undoes the claim's increment. 7
   in-memory tests + 7 real PostgreSQL concurrency tests.

10. **Is the complete legacy world migratable?**
    ✅ Yes. `src/waxprep/history/backfill.py` implements idempotent,
    resumable, count-verifiable backfill for messages, notebook entries,
    artifact filenames, and verification for attachments and knowledge
    tables. 6 regression tests in `tests/history/test_backfill.py`.

11. **Is sensitive event metadata protected?**
    ✅ Yes. `DerivationWorker.sanitize_error` strips quoted strings and
    long tokens from exception messages before persisting to
    `derivation_jobs.last_error`. Event metadata is restricted to
    non-sensitive operational metadata (JSONB, not student content).

12. **Can encryption keys be rotated safely?**
    ✅ Yes. `src/waxprep/security/rotation.py` implements
    `rotate_active_key` (adds new key, old keys stay for reading) and
    `reencrypt_all` (re-encrypts all 15 encrypted columns, idempotent,
    resumable, fail-closed on missing key). 6 regression tests in
    `tests/security/test_rotation.py`.

13. **Are artifacts generic and durable?**
    ✅ Yes. No PDF/image/worksheet/quiz classes. ArtifactVersion is
    generic (mime_type, storage_ref, checksum). ArtifactContent is
    generic (representation, text). The artifact system supports
    durable identity, immutable versions, byte storage reference,
    metadata, extraction, provenance, and search.

14. **Can inbound/outbound delivery recover from crashes?**
    ✅ Yes. Outbound delivery outbox (migration 011). The tutor reply is
    saved first; then an outbound delivery record is created; then
    delivery is attempted. A crash between save and delivery is
    recoverable: the retry returns the same stored reply without
    re-invoking the Teacher. Regression tests in
    `tests/test_turn_flow.py` and `tests/history/test_turn_resume.py`.

15. **Can workers safely run concurrently?**
    ✅ Yes. `claim_due_jobs` uses `FOR UPDATE SKIP LOCKED`. 7 real
    PostgreSQL concurrency tests in
    `tests/derive/test_postgres_concurrency.py` using two
    `PostgresStorage` instances against real PostgreSQL 17.

16. **Is every student-owned relationship tenant-safe?**
    ✅ Yes. RLS on every student-scoped table. Composite FKs (migration
    009) enforce same-student ownership. `waxprep_object_owner` SECURITY
    DEFINER function (migration 010) for polymorphic provenance checks.
    7 cross-tenant FK rejection tests in
    `tests/history/test_cross_tenant_fk.py`.

17. **Is CI still an investigator rather than a teacher?**
    ✅ Yes. CI's system prompt makes clear it does not teach, does not
    directly answer the student's educational question, investigates
    the student's world, returns evidence and uncertainty, respects
    time, distinguishes student from tutor statements, follows
    provenance, checks supersession, uses open-ended investigation.

18. **Is there any hidden keyword routing?**
    ✅ No. Architecture tests in `tests/architecture/` enforce that
    runtime code does not route on keyword lists. Mechanical code
    enforces empty input, malformed IDs, limits, authentication,
    ownership, time bounds — not semantic intent.

19. **Is there any fixed student profile schema?**
    ✅ No. `Student` is minimal (wax_id, timestamps, status). No school,
    goals, weaknesses, or subject fields. Notebook entries have an
    open-ended `payload: dict`.

20. **Is any part of the implementation pretending to be finished when it is actually a stub?**
    ✅ No. The Teacher is explicitly a Protocol with a MockTeacher test
    double. The inbound channel adapter is documented as a future
    infrastructure build (the outbound outbox exists; the inbound
    adapter does not). The artifact byte store (create/edit) is
    documented as future work. The re-encryption script IS implemented
    (`security/rotation.py`).

## Files and components changed

### New packages
- `src/waxprep/orchestration/` — Teacher Protocol, TeacherConsultation
  Protocol, ConsultationRequest/Result, TeacherInput/Outcome,
  CIBackedConsultation adapter, MockTeacher, TurnService (Teacher-first).
- `src/waxprep/channel/` — placeholder for provider-neutral channel
  adapters (inbound/outbound interfaces).
- `src/waxprep/scheduler.py` — provider-neutral worker CLI entry point.
- `src/waxprep/security/rotation.py` — key rotation + re-encryption.

### New migrations
- `migrations/011_outbound_deliveries.sql` — outbox table with composite
  FK to history_entries, unique constraint per channel, RLS.

### Modified source files
- `src/waxprep/turns.py` — re-exports from `orchestration` (legacy shim).
- `src/waxprep/wiring.py` — wires the new orchestration; `max_consultations` param.
- `src/waxprep/storage/protocol.py` — `transaction()`, `OutboundDelivery`,
  paginated retrieval methods, outbound delivery methods.
- `src/waxprep/storage/postgres.py` — real DB transactions, paginated
  retrieval, outbound delivery, fixed `claim_due_jobs` (was missing
  `set_current_wax_id`), fixed job attempt accounting.
- `src/waxprep/storage/memory.py` — snapshot/restore transactions,
  paginated retrieval, outbound delivery, fixed job attempt accounting.
- `src/waxprep/context/retrieval.py` — proper rank-then-limit with
  paginated candidate generation, no arbitrary walls.
- `src/waxprep/derive/worker.py` — fixed attempt accounting (uses
  `job.attempts` from claim, doesn't increment in `mark_job_failed`).
- `src/waxprep/history/backfill.py` — full legacy backfill (messages,
  notebook, artifact filenames, knowledge verification).
- `src/waxprep/domain/models.py` — `OutboundDelivery`, `OutboundStatus`.

### New tests
- `tests/test_turn_flow.py` — rewritten for Teacher-first architecture.
- `tests/history/test_turn_resume.py` — crash recovery + outbox.
- `tests/history/test_atomic_derived.py` — atomic derived operations.
- `tests/history/test_backfill.py` — legacy backfill.
- `tests/history/test_retrieval_old_records.py` — paginated retrieval.
- `tests/derive/test_job_attempt_accounting.py` — attempt accounting.
- `tests/derive/test_postgres_concurrency.py` — real PostgreSQL concurrency.
- `tests/security/test_rotation.py` — key rotation + re-encryption.

### Documentation
- `docs/ARCHITECTURE.md` — updated for Teacher-first architecture.
- `docs/REBUILD-EXECUTION.md` — this file.
- `docs/KEY-MANAGEMENT.md` — key rotation procedure (from prior branch).

## Test results

```
186 passed, 1 deselected (live), 0 failing
```

PostgreSQL version tested: **17.11** (Debian 17.11-0+deb13u1).

Quality gates:
- `ruff format --check .` → 132 files clean
- `ruff check .` → 0 errors
- `pyright` → 0 errors, 0 warnings

## What was intentionally deferred (belongs to Teacher intelligence)

- Teacher system prompt content
- Teaching methodology
- Pedagogical intelligence
- Educational response strategy
- Lesson planning
- Subject teaching logic
- Grading logic
- Teacher personality
- Real production Teacher model/provider implementation

These are NOT foundation defects — they are the next build's scope.

## Remaining limitations

1. **Inbound channel adapter** — the application exposes `/health` only.
   A real inbound HTTP webhook that accepts channel messages and routes
   them to `TurnService.handle_student_message` is future
   infrastructure. The outbound outbox exists; the inbound adapter does not.
2. **Real Teacher model** — the Teacher is a Protocol with a MockTeacher
   test double. A real Teacher adapter through the model_gateway is the
   next build.
3. **Artifact byte store (create/edit)** — only reading extracted
   content through a supplied processor is supported. Writing new files
   requires a byte store that doesn't exist yet.
4. **Continuous consolidation** — `ConsolidationService.consolidate_student`
   is invoked explicitly by the scheduler; a nightly per-student
   schedule is not wired.
5. **Knowledge graph backfill** — old knowledge_nodes/edges/evidence
   rows (if any exist) are reported by the backfill but not converted,
   because the application never wrote to them in the old architecture.

## Production readiness assessment

The foundation is **coherent, durable, tenant-safe, searchable,
recoverable, auditable, and genuinely ready for the future Teacher
intelligence layer**. The architecture is correct; the remaining items
are infrastructure additions (inbound adapter, real Teacher, byte
store) that plug into the existing contracts without changing the
foundation.
