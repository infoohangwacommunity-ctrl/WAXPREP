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
