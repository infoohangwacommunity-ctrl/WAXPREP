# The Wax Prep Quality Gate

The quality gate is the set of checks every change must pass before it is
considered production-ready. It runs automatically on GitHub Actions
(`.github/workflows/quality-gate.yml`) on every push to `main` and on every
pull request targeting `main`.

## What the gate checks

| Job | Checks | Local equivalent |
| --- | --- | --- |
| **static analysis** | Lockfile freshness (`uv lock --check`), spelling (codespell), formatting (`ruff format --check`), linting (`ruff check`), and typing (`pyright`) | `make check` + `uvx codespell .` |
| **tests + coverage** | Full test suite excluding paid `live` tests, against a real PostgreSQL 17 service container, with a coverage floor (`fail_under` in `pyproject.toml`) | `uv run --with pytest-cov pytest -m "not live" --cov` |
| **migrations, scheduler, boot** | Migrations apply cleanly to a clean database; the worker scheduler CLI runs; the real FastAPI app boots and answers `/health` exactly like the Railway healthcheck | `uv run python -m waxprep.scheduler run-once` + `uvicorn waxprep.app:app` |
| **dependency audit** | Locked production dependencies are scanned for known vulnerabilities with `pip-audit` | `uvx pip-audit` |
| **failure triage** | On a failing direct push, the failed-step logs are published to a GitHub issue titled "quality-gate failed on main" so the failure is diagnosable from the repository itself | — |

## Principles

1. **The gate never makes paid external API calls.** Tests that hit real
   model providers are marked `live` and excluded; run them explicitly with
   `make test-live`.
2. **Deploy parity.** The database job uses the same PostgreSQL major version
   as production, and the boot smoke test hits the same `/health` endpoint
   that `railway.toml` uses for deployment healthchecks.
3. **Least privilege.** Gate jobs get `contents: read` only. Only the
   failure-triage job gets `actions: read` and `issues: write`, and only the
   auto-format and lockfile workflows get `contents: write`.
4. **Supply-chain hardening.** GitHub Actions are pinned to full commit SHAs,
   and Dependabot (`.github/dependabot.yml`) keeps both Python dependencies
   and actions current so the vulnerability audit stays quiet.
5. **Self-healing mechanical checks.** Formatting and safe lint fixes are
   applied automatically by the `format-fix` bot (same pattern as the
   lockfile bot). Semantic checks — types, tests, migrations, audits — are
   never auto-fixed; they fail loudly and are triaged via the issue
   published by the gate.
6. **Fast feedback.** Runs on the same branch cancel each other
   (`concurrency`), jobs run in parallel, and every job has a timeout.

## The coverage floor

`[tool.coverage.report] fail_under` in `pyproject.toml` is a ratchet:

- Raise it over time as tests improve.
- Never lower it to make a red gate green — add or fix tests instead.

## When the gate is red

1. Open the failed workflow run, or read the triage issue
   ("quality-gate failed on main") which contains the failed-step logs.
2. Fix the underlying problem. Formatting-only problems are usually fixed
   automatically by the `format-fix` bot within a minute or two of the push.
3. Push again. The gate re-runs; the triage issue can be closed once the
   gate is green.
