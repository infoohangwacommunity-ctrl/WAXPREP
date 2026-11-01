# Deployment

This document records the **current** deployment target for WaxPrep.
It is a statement of fact, not a recommendation: it exists so that any
operator looking at the repo can tell, in five seconds, where the live
dyno runs and which config file owns the start command.

> **Do not change the deployment platform without founder approval.**
> The Founder has confirmed Railway is the current target; the
> railway-vs-Render ambiguity flagged in `vision-devops.md` §6 is
> resolved by this document.

## Platform

**Railway.** The repo ships a `railway.toml` (Railway's Nixpacks-based
build config); no `render.yaml` exists. Three comment lines elsewhere
in the tree (`migrations/017_pgvector_optional.sql`,
`tests/architecture/test_every_table_has_rls.py`,
`tests/history/test_ci_history_retrieval_db.py`) reference "Render's
free tier" as the reason pgvector is optional — those describe the
historical reason for the optional-pgvector design, not the current
deploy target. They are left in place because the design decision
("pgvector is optional; the SQL has a Python fallback") is still
correct regardless of which platform hosts it.

## Start command

Defined in `railway.toml`:

```
uv run uvicorn waxprep.app:app --host 0.0.0.0 --port $PORT --timeout-graceful-shutdown 30
```

- `--timeout-graceful-shutdown 30`: gives the lifespan drain (Fix 1) a
  30-second budget to stop accepting new webhooks, wait for in-flight
  turns to finish, cancel the derivation worker task, and close every
  psycopg connection. The app's internal drain defaults to 25s, so 5s
  of headroom remain under uvicorn's own shutdown timeout.
- `--host 0.0.0.0`: Railway routes traffic to the container's external
  interface; binding to `127.0.0.1` would refuse the platform's
  healthcheck.
- `--port $PORT`: Railway injects `PORT` at runtime; the same command
  works for any port assignment.

## Healthcheck

- **Path**: `/health` (set in `railway.toml`).
- **Timeout**: 30s.
- **Behaviour**: returns `{"status": "ok"}` when storage is reachable;
  `"degraded"` when the DB is unreachable; `"draining"` once the
  SIGTERM drain has started (Fix 1). Also surfaces
  `latency_median_ms` + `latency_samples` (the median of the last 100
  turns) once at least one turn has completed — this is a monitoring
  signal, **not** an admin dashboard (the Founder explicitly excluded
  the dashboard).

## Restart policy

`ON_FAILURE`: Railway restarts the service if the process exits
non-zero. Crashes during startup (migration failure, missing env vars)
loop here until the underlying config is fixed.

## What is NOT deployed (yet)

- **No cron service.** The Founder explicitly excluded daily automated
  testing; no `cron` block is wired in `railway.toml` and no scheduled
  workflow runs in `.github/workflows/`. The `scheduler.py` CLI exists
  for manual or future cron use; it is invoked from the
  `database-and-runtime` quality-gate job to verify it boots, but it
  is not deployed as a long-running or scheduled service.
- **No separate worker process.** The derivation worker runs as an
  in-process `asyncio.create_task` inside the web service's lifespan
  (tracked in `_worker_task`, cancelled + awaited on shutdown — Fix 2).
  A separate Railway service for the worker is the natural next step if
  derivation load grows, but it is not needed at current traffic.
- **No admin dashboard.** The Founder explicitly excluded it. The
  `/health` endpoint exposes only process liveness and aggregate
  median latency; nothing per-student, nothing per-turn.

## Environment variables

See `.env.example` for the full list. The operational minimum:

- `DATABASE_URL` — Postgres connection string. Without it the app
  boots in degraded mode (in-memory storage; migrations are skipped).
- `WAXPREP_MASTER_KEYS` + `WAXPREP_ACTIVE_KEY_ID` — AES-GCM field
  cipher keys. Without them the app falls back to a deterministic test
  key (logged loudly; never acceptable in production).
- `WAXPREP_WHATSAPP_*` / `WAXPREP_TELEGRAM_*` — channel credentials.
  When neither channel is enabled, the app still boots and `/health`
  probes the database, but no webhook routes are mounted.
- `WAXPREP_QUEUE_*` — per-student queue knobs (coalesce window, max
  batch, semantic grouping toggle). Defaults are tuned for the
  Founder's latency target (~0.5s queue-induced wait for fast-path
  turns).

## Local parity

The `database-and-runtime` quality-gate job (`.github/workflows/quality-gate.yml`)
runs the same uvicorn start command against a Postgres service
container and polls `/health` for 30s — this is the deploy-parity
smoke test. If it passes in CI, the same command will boot on
Railway.

The `Makefile` exposes the same surface locally:

```
make check   # format + lint + types + tests (excludes live)
make test    # pytest -m "not live"
```
