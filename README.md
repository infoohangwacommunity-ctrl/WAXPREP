# Wax Prep

 The Tutor That Actually Knows You.

Wax Prep is a personalised educational tutor for Nigerian students, initially delivered through WhatsApp.

The goal is not simply to answer questions. Wax Prep should adapt its teaching to the individual learner.

## Status

The student-world architecture is in place: one unified encrypted history, Context
Intelligence, derived notebook/summaries/knowledge with provenance, and artifact understanding.
The real model-backed Teacher (`ModelTeacher`) is implemented and integrated with
Context Intelligence, unified history and the turn orchestrator — see
docs/ARCHITECTURE.md ("The Teacher model"). The WhatsApp channel is a separate build.

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

## Documents

- docs/PHILOSOPHY.md
- docs/ARCHITECTURE.md
- docs/CONTRIBUTING.md
- docs/DECISIONS.md
- docs/LICENCE-POLICY.md
- docs/DATA-MAP.md
- docs/CONTEXT-INTELLIGENCE.md
- docs/IMPLEMENTATION-PLAN.md

## Deploy (Railway)

See [docs/RAILWAY.md](docs/RAILWAY.md) for web + worker + Postgres setup.
Repository files: `railway.toml` (web), `railway.worker.toml` (worker reference).

