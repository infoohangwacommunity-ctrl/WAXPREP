# Unified History Rebuild — Execution Log

**Branch:** `unified-history-rebuild`  
**Baseline:** `6b26ce2`  
**Blueprint:** WAXPREP-UNIFIED-HISTORY-IMPLEMENTATION-PLAN-AUDITED.md  

## Offline verification (this session)

```text
uv run ruff check .     → pass (E501/E741 ignored for first pass)
uv run pyright          → 0 errors (basic mode + first-party stubs relaxed)
uv run pytest -m "not live" → 111 passed, 14 skipped (postgres), 1 deselected (live)
```

PostgreSQL / DATABASE_URL tests: **not run** (environment).

## Implemented from Part 4

- 74 file blocks extracted and applied
- 8 obsolete conversation-era files deleted
- migrations 005 (unified history), 006 (retire conversations, guarded), 007 (job leases)
- cryptography dependency + regenerated `uv.lock`
- security/cipher, history service, index, derive worker, turns, wiring
- architecture tests (no keyword routing, no conversation concept)

## Part 8 blockers

| ID | Status |
|----|--------|
| A Job lease/claim | Fixed (memory + postgres SKIP LOCKED + migration 007) |
| B Capability skip | Fixed (return to pending, not DONE/FAILED) |
| C History+outbox txn | Partial — save-first preserved; single DB txn API still soft |
| D Duplicate resume | Fixed (resume if no tutor reply) |
| E Full legacy backfill | Guards kept; notebook/graph conversion incomplete |
| F Cross-tenant FKs | Not fully audited in this pass |
| G Atomic provenance | Not fully hardened |
| H Old-history retrieval bound | Documented limitation remains |
| I End-to-end channel/Teacher | Honest: /health only; Teacher is Protocol |
| J Key rotation tooling | Cipher present; rotation procedure not shipped |

## Do not merge to main until

1. Human review of this branch  
2. Postgres CI green with real DATABASE_URL  
3. Migration 006 validated on empty + representative data  
4. Remaining Part 8 blockers accepted or fixed  

## Next human steps

- Review diff vs `main`  
- Run quality-gate workflow on the branch  
- Decide cutover after blockers C, E, F, G, H risk acceptance  
