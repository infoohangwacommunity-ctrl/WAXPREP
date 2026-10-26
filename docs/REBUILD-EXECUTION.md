# Execution Log — CI-first Wax Prep

**Repository:** https://github.com/infoohangwacommunity-ctrl/WAXPREP
**Branch:** `main`
**Status:** green — `ruff format`, `ruff check`, `pyright`, `pytest` all clean

> This document was rewritten. The previous version described the
> Teacher-first foundation branch and is a rear-view mirror from a
> different car: it claimed no real Teacher adapter and no channel
> adapters existed, and quoted a test count from a different codebase.
> What follows describes the repository as it actually is.

## The architecture as it stands

```text
Student message
  |
  v
Save the exact student message (history is append-only, never deleted or edited)
  |
  v
The application assembles the raw exchange material (newest history entries,
character-capped) — supply only, no judgement about what matters
  |
  v
Context Intelligence investigates (mandatory for every non-empty message)
  |    model decides what to inspect, whether to carry the exchange,
  |    and when it has enough
  v
ContextPackage: evidence + the exchange CI chose to carry + synthesis
                + uncertainty + investigation status
  |
  v
Teacher receives: the original message + the package
  |
  v
Tutor response saved + outbound delivery recorded
```

CI is the investigating intelligence. The Teacher is the student-facing
intelligence. The application enforces contracts, bounds, validation and
persistence — and nothing else.

## What is implemented

### Orchestration (CI-first)
- `orchestration/contracts_ci_first.py` — the `Teacher` Protocol,
  `TeacherInput`, `TeacherOutcome`. `TeacherInput` carries the message, the
  package, the time and one channel. No consultation capability: CI has
  already run.
- `orchestration/model_teacher_ci_first.py` — the real, model-backed
  Teacher on the Model Gateway. Renders brief → exchange → current message →
  evidence, in that order, so "that" has something to point at.
- `orchestration/turn_ci_first.py` — the turn orchestrator. Saves first,
  runs CI, teaches once, saves exactly one reply and its outbound delivery
  record atomically, repairs a missing outbox row on retry, and takes a
  cross-replica turn lock so a webhook and a worker cannot both teach.
- `orchestration/post_turn.py` — the post-turn quality event, and the
  optional CI notebook stewardship pass.

### Context Intelligence
- `context/investigator.py` — the model decides, the application executes
  tools and enforces `InvestigationLimits`. The `investigation_status` is
  computed by the runtime from what actually happened, never taken from the
  model's own claim.
- `context/exchange.py` — the mechanical rules for the exchange material:
  how much may be supplied, and how supplied turns are rendered.
- `context/system_prompt.py` — the CI prompt: evidence-led investigation,
  notebook stewardship, the exchange contract, and the safety rules.
- `context/gateway_model.py` — CI on the Model Gateway. Parses the
  structured final answer, including `include_exchange`.

### Continuity through CI (D041)
The application supplies the raw exchange material on every turn and never
judges which turns matter. CI decides whether the Teacher receives it; the
default is *include*, because the Teacher cannot see the thread any other
way. A drop is explicit, reasoned, and recorded in the audit trail as
`exchange:dropped:<reason>`; an include is recorded as
`exchange:included:<n>`.

This closes the hole left by removing the fixed recent-turn window: without
it, continuity was *possible* but not *guaranteed*.

### Channels
- **WhatsApp** — Cloud API handler, security (signature verification),
  normalisation, progressive delivery, outbox.
- **Telegram** — bot API routes, normalisation, native drafts, outbox.
- **Streaming** — a shared contract, an accumulator, formatters, renderers
  and observability, so a reply can be delivered progressively without the
  educational branching moving into infrastructure.

### Storage, security, operations
- Append-only unified history, blind word index, embeddings, provenance
  links, notebook, summaries, knowledge graph, artifacts, workspace.
- PostgreSQL Row-Level Security for tenant isolation; composite
  cross-tenant foreign keys.
- AES-256-GCM field encryption with per-student keys, blind HMAC word
  hashes for exact search, and resumable key rotation.
- An event-metadata contract: only safe operational keys are accepted, and
  long values are rejected at the storage layer.
- 14 migrations; the worker applies pending migrations on boot.

## What is NOT yet implemented

These are the real remaining pieces, in the order they should be built:

1. **The judge** — a background evaluation pass, after the outbox, on the
   small model, writing structured scores into the `turn_quality` event log
   that `post_turn.py` already owns. Continuity is already recorded there
   (`exchange_included`, `exchange_turns`), so the judge has something
   concrete to score.
2. **The synthetic preference pipeline** — turning real student exchanges
   into preference data for future fine-tuning. This is the automation the
   product needs so that understanding improves without a human in the loop.
3. **Continuous consolidation schedule** — `ConsolidationService` is
   invoked explicitly by the scheduler; a nightly per-student schedule is
   not wired.

## Live testing

`scripts/live_e2e_upstage.py` runs ten multi-turn students and forty
single questions against real Upstage models.

- The Teacher and CI are registered as **different models**: Solar Pro 4
  teaches, Mini investigates. Judging the product's voice on the small
  model tells you about the small model, not about the product. Override
  with `WAXPREP_LIVE_TEACHER_MODEL` and `WAXPREP_LIVE_CI_MODEL`.
- The script reports the CI investigation status and how many exchange
  turns reached the Teacher, so continuity is visible per turn rather than
  inferred from the prose.
- `scripts/` is now covered by `pyright` and `ruff`. It used to be
  excluded, which is how two live scripts kept calling
  `build_services(max_consultations=...)` — a parameter removed in the
  CI-first migration — and kept reading `result.consultations_made`, a
  field the CI-first result never had. Both scripts crashed on the first
  line of the run. They were instruments that had never played a note.

```bash
export UPSTAGE_API_KEY=...
uv run python scripts/live_e2e_upstage.py
```

## Test results

```
458 passed, 95 skipped (no DATABASE_URL), 5 deselected (live)
```

PostgreSQL version tested: **17.11** (Debian 17.11-0+deb13u1).
CI PostgreSQL: **17** (Docker image `postgres:17`).

Quality gates:
- `ruff format --check .` → 228 files clean
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

## Where the project stands

The foundation is coherent and the architecture is honest: one repository,
one history, one place where intelligence lives, and an application that
enforces contracts without pretending to understand students.

Two precise moves remain, and they are the ones that matter:

1. **The judge** — so quality is measured by the system, not by you.
2. **The synthetic preference pipeline** — so the system improves from real
   conversations without anyone hand-labelling them.

Both build on contracts that already exist. Neither requires a new
architecture.
