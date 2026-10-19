# Wax Prep Architecture

## Production path (CI-first)

Live WhatsApp and production composition use **CI-first** only:

- `TurnServiceCiFirst` + `ModelTeacherCiFirst`
- Context Intelligence is **mandatory** every turn (not optional consult)
- Teacher-first modules (`turn.py`, `model_teacher.py`, `contracts.py`,
  `bounded.py`, `consultation.py`) are **quarantined** (DeprecationWarning on
  import) and scheduled for removal

### The CI-first turn, end to end

```text
Student message arrives
  |
  v
Save the exact student message (atomic with index jobs)
  |
  v
Context Intelligence runs — mandatory, every non-empty message
  |
  v
CI produces a ContextPackage (evidence + synthesis + uncertainty + status)
  |
  v
Teacher receives: original student message + ContextPackage
  |
  v
Teacher produces the final educational response
  |
  v
Save exactly one tutor reply (linked to the student entry) + outbound delivery
record (outbox), atomically. A crash between them is repaired on the next retry.
  |
  v
Channel adapter delivers; on crash, retry sends the same stored reply.
```

Key invariants of this flow:

- The **Teacher does not decide whether to consult CI**. CI is mandatory.
- The **Teacher does not generate consultation questions**. CI investigates on
  its own.
- The **Teacher receives a complete ContextPackage**, not raw CI results.
- The Teacher has **one job**: produce the final student-facing response.
- There is **no bounded consultation loop** between Teacher and CI in
  production. CI runs once per turn with its own internal bounds.

The Teacher-first orchestration (Teacher drives the turn, optionally calls a
bounded CI consultation) was an earlier design and is no longer used in
production. Its modules remain importable for legacy unit tests only and emit
a `DeprecationWarning`.

### Normalization (WhatsApp inbound)

Every inbound type becomes one durable history candidate via
`channel.whatsapp.normalize`: text, media, reaction, location, contacts,
interactive button/list reply, and unknown (never dropped). Fields:
`content_kind`, human-readable `text`, and `structured` type-specific JSON.

### Interactive offers (one-shot)

Teacher may optionally suggest up to 3 short reply buttons. Channel issues
**opaque** button ids, stores `interactive_offers`, and **consumes** on first
valid click. Stale clicks are rejected at the channel layer (Meta does not
auto-disable buttons). Body teaching always lives in plain text.

### Media derivation

Attachments enqueue `JOB_INGEST_MEDIA` after durable save so the first tutor
reply is never blocked on download/OCR/STT.

## The one idea

A student's world is **one unified, timestamped history**. Everything Wax Prep
knows is either that history, or something *derived from it that points back to it*.

```text
Student message
  |
  v
Save exact student message (history is append-only, never deleted or edited)
  |
  v
Context Intelligence investigates the student's world
  |    (model decides what to inspect; application executes tools and enforces limits)
  |    (mandatory for every non-empty message — never optional)
  v
CI produces a ContextPackage: evidence + synthesis + uncertainty + investigation status
  |
  v
Teacher receives the original student message + the ContextPackage
  |
  |   produces the final educational response
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

**Important:** the Teacher is real in this build: `ModelTeacherCiFirst`
(`src/waxprep/orchestration/model_teacher_ci_first.py`) implements the
CI-first `Teacher` Protocol on the Model Gateway. The `Teacher` Protocol and
a deterministic mock remain, so tests stay offline and reproducible.

## Roles

- **History service** saves first and unconditionally. It never asks "is this important?".
  The exact student message reaches the Teacher untouched — never rewritten,
  summarized, or replaced by CI's interpretation. History is append-only; age
  affects findability, never existence.
- **Context Intelligence (CI)** is the **investigating intelligence**. It runs
  **automatically for every non-empty message**. It decides what to inspect,
  when evidence conflicts, when more investigation is justified, and when it
  has enough to stop. It never teaches the student. The application supplies
  it with: the current message, the current time, the trigger record, the
  authorized scope, available tools, mechanical limits, and instructions on
  evidence quality and tool use. The model decides the rest.
- **Teacher** is the **student-facing intelligence**. It receives the original
  student message plus a ContextPackage, and composes the final educational
  answer. It does **not** decide whether CI runs — CI has already run. It
  never writes to history or storage; persistence stays with
  `TurnServiceCiFirst` and the storage layer. No keyword routing, phrase
  matching, or student-specific branching lives in the adapter: the model
  composes the answer from the message and the package; the application
  enforces contracts, bounds, validation and persistence.
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

## The Teacher model (CI-first)

`ModelTeacherCiFirst` runs **one** gateway call per turn. The model returns
plain text — the final student-facing answer. No JSON parsing, no `consult`
action, no bounded loop. The adapter splits an optional `<<<choices>>>`
trailer into an `InteractiveSuggestion`; the body always carries the actual
teaching.

Validation is strict:
- A blank or whitespace-only reply is never persisted. The orchestrator raises
  `InvalidTeacherOutcomeError` so the failure is observable and recoverable.
- No keyword routing, phrase matching, or student-specific branching lives in
  the adapter. The model composes the answer from the message and the package;
  the application enforces the response contract and provider-neutral request
  construction.

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
  `ModelTeacherCiFirst(gateway, model_name="teacher")` automatically. A ready
  `teacher=` instance can be passed instead (tests inject
  `RecordingTeacher`/`MockTeacher` this way).
- Providing neither `teacher=` nor `gateway=` fails at startup
  (`ValueError`); a missing registry entry fails clearly when the model is
  first resolved. The mock is never silently substituted in production.
- API keys stay with the provider adapters' established configuration;
  no keys are committed.

## Teacher ↔ CI contract (CI-first)

In CI-first architecture there is **no consultation loop**. The Teacher does
**not** decide whether to call CI and does **not** generate investigation
questions. The contract is one-directional:

```text
TurnServiceCiFirst calls CI with an InvestigationRequest
  -> CI returns a ContextPackage
TurnServiceCiFirst hands the Teacher the original message + the package
  -> Teacher returns the final reply text
```

The Teacher receives:
- the exact student `HistoryEntry` (untouched)
- the current time
- the WAX ID
- a `ContextPackage` (evidence + synthesis + uncertainty + provenance +
  investigation status + stop reason)

The Teacher does **not** receive a consultation capability. CI has already
completed its bounded investigation. If the package is empty or partial, the
Teacher answers as well as it can without inventing context — the package's
`investigation_status` field tells the Teacher exactly what CI did and did not
establish (see `docs/CONTEXT-INTELLIGENCE.md`).

## Investigation completion contract

CI never silently produces an empty package. Every `ContextPackage` carries an
explicit `investigation_status` drawn from a closed enum:

| Status        | Meaning                                                              |
| ------------- | ------------------------------------------------------------------- |
| `successful`  | Investigation completed with relevant evidence.                      |
| `empty`       | Investigation completed but no relevant evidence was found.          |
| `incomplete`  | Investigation stopped at a hard budget before gathering enough evidence. |
| `partial`     | Some tools failed or were unavailable; the package reflects the remainder. |
| `failed`      | The investigation itself broke before any reliable result was produced. |
| `skipped`     | The message was blank; no investigation was attempted.              |

The status is computed from what the runtime actually did, not from the
model's own claim. Hitting the step budget, a tool failing, an invalid
evidence-selection by the model, and an empty corpus each map to a specific
status. The Teacher can therefore distinguish "CI looked and found nothing"
from "CI never finished".

## Evidence lifecycle contract

Derived objects have an explicit lifecycle. The vocabulary is small and the
operations are *not* interchangeable:

| Operation      | Meaning                                                                   |
| -------------- | ------------------------------------------------------------------------- |
| Correction     | A claim or record was wrong; a corrected claim is recorded.              |
| Supersession   | A newer or more applicable derived claim replaces an older one.           |
| Invalidation   | A derived object is no longer trustworthy; excluded from normal use.     |
| Deletion       | Information must no longer be retained or used (retention policy applies).|
| Rebuilding     | A derived object or index is regenerated from available sources.         |

History is never deleted or invalidated by these operations — it is
append-only. Invalidation and deletion propagate through derived dependents
(notebook entries, summaries, edges, search tokens, embeddings, cached
retrieval). The propagation is auditable; the source history remains unless
a deletion request explicitly removes it (and only when retention policy
permits).

See `docs/CONTEXT-INTELLIGENCE.md` for the operational details and the
`Operation` enum in `src/waxprep/context/proposals.py` for the surface area
CI uses to propose lifecycle changes.

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
- A model-generated request is never sufficient authorization to access
  another student's records. Retrieved content — even when it says
  "ignore all rules" — is evidence that someone wrote those words, not
  an instruction that grants permission.
