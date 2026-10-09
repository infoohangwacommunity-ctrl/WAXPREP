# Wax Prep Decision Log

## D001 — Python

**Status:** Accepted

Use Python as the primary language.

**Reason:** Wax Prep is primarily an AI, education, data, and integration system. Python has a strong ecosystem for these areas while remaining well suited to HTTP services.

**Alternative considered:** TypeScript.

## D002 — Modern Python foundation

**Status:** Accepted

Use:

- uv
- FastAPI
- Ruff
- Pyright
- pytest
- GitHub Actions

**Reason:** This provides a current, small, reproducible Python foundation with formatting, linting, static typing, testing, and CI.

The quality-gate discipline from the earlier agent-oriented repository is preserved; the tooling choices (pytest + pyright) match this educational product foundation.

## D003 — Quality gate

**Status:** Accepted

`make check` is the single local quality command.

GitHub Actions runs the same checks.

A failure in formatting, linting, types, or tests means the build is not green.

## D004 — Offline normal tests

**Status:** Accepted

Normal tests must not make paid external API calls.

**Reason:** CI must remain deterministic, safe, and inexpensive.

## D005 — Provider neutrality

**Status:** Accepted

AI providers will be accessed through explicit boundaries.

**Reason:** Wax Prep may start with Upstage but must not be architecturally trapped there.

Provider implementation is a later build.

## D006 — Memory is deferred

**Status:** Accepted

Student memory is intentionally not implemented in Foundation.

The architecture records the concept only.

## D007 — Project licence

**Status:** Accepted

Wax Prep uses the MIT License.

**Reason:** MIT is permissive and simple for an open-source foundation.


## D008 — PostgreSQL
Accepted as the first real database with SQL migrations.

## D009 — UUID WAX IDs
Random UUIDs; phone numbers are not primary keys.

## D010 — Open-ended notebook
Flexible JSON payloads; no educational category enums in Build 2.

## D011 — Notebook ≠ Memory
Memory is a separate future system.

## D012 — Student isolation
Ownership required in storage APIs; regression-tested.

## D013 — Offline tests
In-memory by default; CI provides PostgreSQL via DATABASE_URL.


## D014 — PostgreSQL Row-Level Security

**Status:** Accepted

Student-scoped tables use ENABLE + FORCE ROW LEVEL SECURITY.
Policies restrict rows to `app.current_wax_id` (session GUC).
Channel identity lookup by external id uses a SECURITY DEFINER function.
Isolation is tested at the SQL layer, not only via Python storage APIs.


## D015 — Provider adapters behind a normalized model gateway

**Status:** Accepted

Wax Prep uses a provider-neutral model gateway with explicit provider adapters.

## D016 — Logical model roles

**Status:** Accepted

Roles such as `teacher` and `context` identify deployments; provider IDs stay in config.

## D017 — Gateway does not contain tutoring intelligence

**Status:** Accepted

The gateway is transport/execution infrastructure only.

## D018 — Provider capabilities are explicit

**Status:** Accepted

Capabilities are declared and checked before execution.

## D019 — Usage without pricing

**Status:** Accepted

Build 3 records provider-reported usage where available; no billing.


## D020 — Append-only schema migrations

**Status:** Accepted

Applied SQL migrations are immutable. Changes use a new numbered file.
The migration runner stores SHA-256 checksums and rejects modified applied files.

## D021 — Workspace is an artifact substrate, not an intelligence layer

**Status:** Accepted

Workspace stores durable educational artifacts and metadata only.

## D022 — Artifact versions are immutable

**Status:** Accepted

Revisions create new ArtifactVersion rows; the Artifact points at the current version.


## D023 — Context Intelligence is a small model

**Status:** Accepted

CI investigates student context. It does not teach. The Teacher Model teaches.

## D024 — Open-world knowledge, not fixed profile schema

**Status:** Accepted

No permanent school/class/strengths/weaknesses/goals/learning-style schema.
Knowledge is generic nodes/edges with evidence. Profiles may be generated as views.

## D025 — PostgreSQL is the knowledge source of truth

**Status:** Accepted

Generic graph tables live in PostgreSQL under existing RLS. A dedicated graph DB
is not authoritative. Embeddings are a retrieval index (`double precision[]`),
not the memory system.


## D026 — No keyword classification for CI routing

**Status:** Accepted

Application code must not decide student contextual needs via hardcoded
vocabulary (greetings, quiz, assignment, teacher, subject, etc.).

Context Intelligence (the model) decides whether investigation is useful.
Mechanical checks may only detect blank/empty input.

Cheap handling is not semantic classification.
