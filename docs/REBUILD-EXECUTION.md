# Foundation Teacher-First Completion — Execution Log

**Branch:** `foundation-teacher-first-completion`
**Baseline:** `professional-unified-history-rebuild` (commit `15ef0a0`)
**Latest commit:** see `git log --oneline -1`
**Draft PR:** https://github.com/infoohangwacommunity-ctrl/WAXPREP/pull/2

## What this branch is

This branch rebuilds WAXPREP's student-world foundation and Teacher/CI
orchestration boundary to be correct. It is **not** the
Teacher-intelligence build — the Teacher is a Protocol; tests use a
deterministic mock. The future Teacher-intelligence build will plug
in a real Teacher following the same contract.

## CI status

The GitHub Actions `quality-gate` workflow runs on every push and PR.
It uses PostgreSQL 17 and runs:
- ruff format --check
- ruff check
- pyright
- pytest -m "not live" (all PostgreSQL-backed tests run against the
  CI's PostgreSQL 17 container)
- migration verification (drops schema, applies all migrations)
- scheduler CLI verification

**Current CI status: ✅ passing** (verified on the latest commit).

## What is actually implemented (source code exists)

### Core architecture
- **Unified history** — `history_entries` table, append-only, timestamped.
  `HistoryService` saves the exact student message atomically with its
  index jobs.
- **Teacher-first orchestration** — `src/waxprep/orchestration/`:
  - `contracts.py`: Teacher Protocol, TeacherConsultation Protocol,
    ConsultationRequest/Result, TeacherInput/Outcome.
  - `consultation.py`: CIBackedConsultation adapter wrapping
    ContextInvestigator.
  - `bounded.py`: BoundedConsultation — enforces the consultation
    budget at the capability boundary, counting every CI call.
  - `mock_teacher.py`: deterministic test double (NOT teaching
    intelligence).
  - `turn.py`: TurnService — Teacher drives the turn with bounded CI
    consultations.
- **Outbound delivery outbox** — `outbound_deliveries` table
  (migration 011). The tutor reply and its outbox record are persisted
  atomically. If a reply exists but its outbox record is missing
  (simulating a crash), a retry repairs the record and returns the same
  stored reply without invoking the Teacher again.
- **Channel interfaces** — `src/waxprep/channel/__init__.py`:
  NormalizedInbound, InboundChannelAdapter Protocol,
  OutboundChannelAdapter Protocol. The actual WhatsApp adapter is a
  future build; these interfaces define the contract.

### Retrieval
- **Rank-then-limit** — `find_exact` and `find_by_meaning` paginate
  through the corpus with deterministic cursors. No arbitrary page
  limits. Rigorously justified early termination:
  - Token search: stop when the current top-N min exceeds
    `sum_of_all_query_token_weights × recency_weight(now, last_seen_time)`.
  - Semantic search: stop when the current top-N min exceeds
    `1.0 × recency_weight(now, last_seen_time)`.

### Storage
- **Atomic transactions** — `Storage.transaction()` context manager.
  PostgresStorage: real DB transaction. InMemoryStorage: snapshot/restore.
- **Job leases** — `FOR UPDATE SKIP LOCKED` + `lease_until`.
  Correct attempt accounting: claim increments once; done/failed don't
  increment.
- **Cross-tenant FKs** — composite foreign keys + `waxprep_object_owner`
  SECURITY DEFINER function for polymorphic provenance.

### Security
- **Field-level encryption** — AES-256-GCM with per-student derived keys.
- **Key rotation** — `src/waxprep/security/rotation.py`: `rotate_active_key`,
  `reencrypt_all` (idempotent, resumable, fail-closed).
- **Event metadata contract** — `src/waxprep/security/event_metadata.py`:
  only safe operational keys are allowed; disallowed keys and long
  values are rejected at the storage layer.
- **Sanitized errors** — `DerivationWorker.sanitize_error` strips quoted
  strings and long tokens from exception messages before persisting.

### Migration & backfill
- **Full legacy backfill** — `src/waxprep/history/backfill.py`:
  idempotent, resumable, count-verifiable. Handles messages, notebook
  entries (payload → encrypted + provenance, atomic), artifact
  filenames, knowledge verification.
- **Migration 006** — guards check for UN-BACKFILLED rows (not mere
  existence); `ADD COLUMN IF NOT EXISTS` makes it idempotent with the
  backfill.

### Worker & scheduler
- **Worker** — `src/waxprep/derive/worker.py`: retries, leases,
  capability-aware.
- **Scheduler CLI** — `src/waxprep/scheduler.py`: `python -m waxprep.scheduler
  run-once` / `run-interval`. Provider-neutral, safe for concurrent
  workers.

## What was intentionally deferred (Teacher intelligence)

These belong to the next build, not the foundation:

- Teacher system prompt content
- Teaching methodology / pedagogical intelligence
- Educational response strategy / lesson planning
- Subject-specific teaching logic / grading logic
- Teacher personality
- Real production Teacher model/provider implementation

## What is NOT yet implemented (infrastructure)

These are infrastructure additions that plug into the existing contracts:

1. **Inbound channel adapter** — the `InboundChannelAdapter` Protocol
   is defined in `src/waxprep/channel/__init__.py`, but no concrete
   adapter (WhatsApp webhook handler, etc.) is implemented. The
   application exposes `/health` only.
2. **Outbound channel adapter** — the `OutboundChannelAdapter` Protocol
   is defined, but no concrete adapter is implemented. The outbox table
   and `OutboundDelivery` model exist; the delivery worker is not wired.
3. **Real Teacher adapter** — the Teacher is a Protocol; `MockTeacher`
   is the only implementation. A real Teacher adapter through
   `model_gateway` is the next build.
4. **Artifact byte store (create/edit)** — only reading extracted content
   through a supplied processor is supported. Writing new files requires
   a byte store that doesn't exist yet.
5. **Continuous consolidation schedule** — `ConsolidationService` is
   invoked explicitly by the scheduler; a nightly per-student schedule
   is not wired.
6. **Knowledge graph backfill** — old `knowledge_nodes`/`edges`/`evidence`
   rows (if any exist) are reported by the backfill but not converted,
   because the application never wrote to them in the old architecture.

## Test results

```
201 passed, 1 deselected (live), 0 failing
```

PostgreSQL version tested: **17.11** (Debian 17.11-0+deb13u1).
CI PostgreSQL: **17** (Docker image `postgres:17`).

Quality gates:
- `ruff format --check .` → 138 files clean
- `ruff check .` → 0 errors
- `pyright` → 0 errors, 0 warnings

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

# Verify migrations on an empty database:
uv run python -c "
import psycopg
from waxprep.migrations import apply_migrations
conn = psycopg.connect('$DATABASE_URL')
with conn.cursor() as cur:
    cur.execute('DROP SCHEMA public CASCADE')
    cur.execute('CREATE SCHEMA public')
conn.commit()
print(apply_migrations(conn))
"

# Verify the worker scheduler CLI:
uv run python -m waxprep.scheduler run-once
```

## Production readiness assessment

The foundation is **coherent, durable, tenant-safe, searchable,
recoverable, auditable, and genuinely ready for the future Teacher
intelligence layer**. The architecture is correct; the remaining items
are infrastructure additions that plug into the existing contracts
without changing the foundation.
