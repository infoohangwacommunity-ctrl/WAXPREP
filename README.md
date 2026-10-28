# Wax Prep

 The Tutor That Actually Knows You.

Wax Prep is a personalised educational tutor for Nigerian students, delivered
through WhatsApp and Telegram.

The goal is not simply to answer questions. Wax Prep should adapt its teaching
to the individual learner.

## Status

The student-world architecture is live: one unified encrypted history, Context
Intelligence, derived notebook/summaries/knowledge with provenance, and artifact
understanding. The model-backed Teacher (`ModelTeacherCiFirst`) and the
CI-first turn orchestrator run in production, with both WhatsApp and Telegram
channels mounted through a per-student turn queue. A Quality Judge periodically
reviews tutoring and feeds bounded guidance back into the Teacher's input.

What is implemented and running:

- **CI-first turns.** Context Intelligence investigates every non-empty message
  before the Teacher answers; the Teacher never decides whether to consult it.
- **Turn assembly.** Rapid messages from one student are collected into a
  single turn by a resettable quiet period with a hard collection deadline.
  A small grouping model classifies late arrivals (continuation vs new
  request) — no keyword rules; the time window is always the fallback.
- **Progressive streaming with a truncation guard.** Replies stream early to
  the student, but a stream is accepted as the final answer only when the
  provider emits a completion event. A cut-off stream falls back to one
  complete call, so a mid-sentence fragment is never saved or delivered as
  the tutor's reply.
- **Per-stage latency notes.** Every turn records a `turn_latency` event with
  stage timings (CI seconds, seconds to first streamed delta, first visible,
  reply saved, end-to-end).
- **Interactive buttons.** The Teacher may end a reply with up to three
  one-shot choices; channels issue opaque button ids and consume them on
  first valid click.
- **Quality Judge.** Separate bounded assessments of the context review and
  the delivery; accepted tutor guidance is delivered into the next turns'
  Teacher input with its confidence and freshness.
- **Optional semantic retrieval.** An OpenAI-compatible embedding provider
  (set `WAXPREP_EMBEDDING_API_KEY` + `WAXPREP_EMBEDDING_MODEL`) enables
  `find_by_meaning` and background embedding jobs; unset means lexical-only.
- **CI batched retrievals.** Context Intelligence may select several
  independent lookups in one decision; the runtime executes them
  concurrently within the same mechanical budgets.

## What Wax Prep is NOT

Wax Prep is not a coding agent.

It does not provide students with a terminal, shell, coding workspace, or arbitrary software-development environment.

## Development

The project uses:

- Python 3.13
- FastAPI
- uv
- Ruff
- Pyright
- pytest
- GitHub Actions

The normal quality gate is:

    make check

The normal quality gate must not make paid AI API calls.

Paid provider integration tests are explicitly marked `live` and are excluded from the normal quality gate.

## Operational configuration (environment)

Turn queue (all optional, defaults in parentheses):

- `WAXPREP_QUEUE_COALESCE` — time-window coalescing (on)
- `WAXPREP_QUEUE_WINDOW` — quiet-period seconds (2.0)
- `WAXPREP_QUEUE_MAX_BATCH` — max messages per turn (5)
- `WAXPREP_QUEUE_MAX_COLLECTION` — hard collection deadline seconds (12)
- `WAXPREP_QUEUE_SEMANTIC` — small-model semantic grouping (on)
- `WAXPREP_QUEUE_GROUPING_TIMEOUT` — grouping call budget seconds (1.5)

Embeddings (both key and model must be set together to enable):

- `WAXPREP_EMBEDDING_API_KEY` / `WAXPREP_EMBEDDING_MODEL`
- `WAXPREP_EMBEDDING_BASE_URL` (https://api.openai.com/v1)
- `WAXPREP_EMBEDDING_DIMENSIONS`

Models and channels follow their existing configuration: `UPSTAGE_*`,
`WAXPREP_REASONING_EFFORT`, `WAXPREP_CI_NATIVE_TOOLS`, `WHATSAPP_*`,
`TELEGRAM_*`, `WAXPREP_PUBLIC_BASE_URL`, `DATABASE_URL`, `WAXPREP_MASTER_KEYS`.

## Documents

- docs/PHILOSOPHY.md
- docs/ARCHITECTURE.md
- docs/CONTEXT-INTELLIGENCE.md
- docs/DATA-MAP.md
- docs/DECISIONS.md
- docs/CONTRIBUTING.md
- docs/LICENCE-POLICY.md
- docs/KEY-MANAGEMENT.md
- docs/QUALITY-GATE.md
- docs/PRODUCT-CHANNELS.md
- docs/MEDIA-WHATSAPP.md
- docs/EVALUATION-BEHAVIOUR.md
- docs/BUILD-3-MODEL-GATEWAY.md
- docs/IMPLEMENTATION-MATRIX.md
- docs/IMPLEMENTATION-STAGE-TEACHER-CI-INTEGRATION.md
- docs/REBUILD-EXECUTION.md
