# Wax Prep Architecture

## The one idea

A student's world is **one unified, timestamped history**. Everything Wax Prep
knows is either that history, or something *derived from it that points back to it*.

```text
Student message
  |
  v
Teacher (drives the turn)
  |
  |   may consult Context Intelligence (bounded)
  |   may request another investigation (bounded)
  v
Context Intelligence investigates the student's world
  |
  v
Teacher receives evidence/context
  |
  |   eventually produces the response
  v
Tutor response is durably saved + outbound delivery recorded
  |
  +-- Unified history   what happened (every student AND tutor message, with time, who, never deleted)
  +-- Derived, always linked back to history:
  |     Notebook         durable understanding worth keeping handy
  |     Summaries        compressed stretches of history (a faster path to the evidence)
  |     Knowledge graph  relationships CI discovered
  |     Search indexes   blind word hashes + embeddings (ways of finding things)
  |     Artifact contents what could be read from a student's files
  +-- Workspace          durable objects the student owns (artifacts, versions)
  |
  v
Model Gateway (provider-neutral) -> Upstage / OpenAI / Anthropic / mock
```

**Important:** the Teacher is real in this build: `ModelTeacher`
(`src/waxprep/orchestration/model_teacher.py`) implements the `Teacher`
Protocol on the Model Gateway. The `Teacher` Protocol and the
deterministic mock remain, so tests stay offline and reproducible.

## Roles

- **History service** saves first and unconditionally. It never asks "is this important?".
  The exact student message reaches the Teacher untouched — never rewritten,
  summarized, or replaced by CI's interpretation.
- **Teacher** drives the turn. It decides whether to consult CI, what to ask,
  and whether the result is sufficient. The real Teacher (`ModelTeacher`)
  gets its teaching intelligence from a logical model executed through the
  Model Gateway; it composes the final educational answer itself and never
  writes to history or storage.
- **Context Intelligence (CI)** is a research/investigation system. It
  investigates the student's world and returns evidence and uncertainty.
  It never directly answers the student's educational question. It is a
  capability the Teacher uses, not a gatekeeper that sits in front of
  the Teacher.
- **Services** own *execution*: validated, student-scoped reads and writes.
  Derived-object + provenance + index writes are atomic (spec section 9).
- **Storage** owns *persistence*. PostgreSQL Row-Level Security owns *isolation*.
  The storage layer provides a `transaction()` context manager for atomic
  derived operations.
- **Background worker** owns *derived work* (embeddings, artifact reading) and retries it.
  A failure there can never lose or block a saved message. Job attempt
  accounting is correct: one real execution attempt counts exactly once.
- **Scheduler** provides a provider-neutral CLI entry point
  (`python -m waxprep.scheduler run-once` or `run-interval`) that is safe
  to run concurrently across multiple processes.

## Rules

1. History is append-only. There is no delete/edit method for it.
2. Age changes how easy something is to find, never whether it exists.
3. Every derived object has provenance links to its sources, and the application
   records who authored each source. What the tutor said is never a student fact.
4. Everything the model sees carries the exact time and its age relative to *now*.
5. No keyword routing, no fixed profile fields, no media-type handling in intelligence code.
6. Sensitive text is encrypted by the application (AES-256-GCM, per-student keys).
   Exact-word search uses blind (HMAC) hashes so encryption and search coexist.
   Keys can be rotated safely (spec section 14).
7. Normal tests make no paid AI calls.

## The Teacher model

`ModelTeacher` runs one gateway call per orchestration round. The model
returns a strictly validated JSON decision:

```json
{"action": "reply", "reply_text": "<final student-facing answer>"}
{"action": "consult", "question": "<free-form investigation request>"}
```

Any other shape — unknown action, missing fields, blank text, non-JSON
output, or a consultation request after the budget is exhausted — fails
the turn with a typed error (`TeacherModelError`). Nothing invalid is ever
persisted as a tutor reply. There is no keyword routing, phrase matching,
or student-specific branching anywhere in the adapter: the model decides
from the actual message and evidence; the application enforces contracts,
bounds, validation and persistence.

### Configuration

The Teacher model is a **logical model** in the `ModelRegistry`, exactly
like the CI model:

- Register the provider adapter (`OpenAIResponsesProvider`,
  `UpstageResponsesProvider`, `AnthropicMessagesProvider`, or
  `MockProvider`) with the registry.
- Register a `ModelConfig` with `name="teacher"` (the default logical
  name; `DEFAULT_TEACHER_MODEL_NAME`), the provider's name, the real
  provider model id, `role=ModelRole.TEACHER`, and the model's declared
  capabilities. The basic Teacher path needs text only.
- Build a `ModelGateway` over the registry and pass it to
  `build_services(..., gateway=gateway)` — the composition root composes
  `ModelTeacher(gateway, model_name="teacher")` automatically. A ready
  `teacher=` instance can be passed instead (tests inject
  `RecordingTeacher`/`MockTeacher` this way).
- Providing neither `teacher=` nor `gateway=` fails at startup
  (`ValueError`); a missing registry entry fails clearly when the model is
  first resolved. The mock is never silently substituted in production.
- API keys stay with the provider adapters' established configuration;
  no keys are committed.

## Teacher ↔ CI consultation contract

The Teacher receives:
- the exact student `HistoryEntry` (untouched)
- the current time
- the WAX ID
- a `TeacherConsultation` capability
- any already-returned CI context from earlier rounds

The Teacher can:
- respond without consulting CI
- ask CI a targeted question
- receive CI context (evidence + synthesis + uncertainty + provenance)
- ask CI for another bounded investigation
- eventually return reply text

The orchestration enforces a configurable hard upper bound on
consultations per student turn (default 3) so a buggy Teacher cannot
enter an infinite CI loop. When the budget is exhausted the Teacher is
told clearly that no consultations remain and must finish with the
evidence it already has; if it cannot produce a valid final reply, the
turn fails in a controlled way with the student message preserved.

## Retrieval architecture

Candidate generation and ranking are **separate concepts**:
- `find_exact` paginates through **all** matching token rows (no arbitrary
  wall), scoring each page and keeping a running top-N.
- `find_by_meaning` paginates through embeddings with a configurable page
  size, scoring each page. Early termination when the top-N is provably stable.
- Pagination is deterministic (cursor = last row's `(object_time, object_id, [token_hash])`).
- Ranking happens before the final evidence limit.
- The design is replaceable: a real vector index (pgvector, dedicated
  service) can later be introduced without changing the memory architecture.

## Outbound delivery (outbox)

The tutor reply is saved first; then an outbound delivery record is
created; then delivery is attempted. A crash between saving the reply
and marking delivered is recoverable: the retry sends the same stored
reply without invoking the Teacher again.

## Cross-tenant security

- RLS is database-enforced on every student-scoped table.
- Composite foreign keys enforce same-student ownership for every
  reference that can be structurally protected.
- A `waxprep_object_owner` SECURITY DEFINER function lets the application
  check ownership of polymorphic provenance references that cannot be
  protected by an ordinary FK.
