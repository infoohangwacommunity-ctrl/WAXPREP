# Context Intelligence

CI is a **small AI model** that turns "the student just said X, at time T" into a compact,
dated, traceable context package for the Teacher. CI does **not** teach.

CI is **mandatory** in the CI-first production flow — every non-empty student
message triggers one investigation. The Teacher does not decide whether CI runs;
CI has already finished by the time the Teacher sees the package.

## Loop

```text
new message + now -> CI decides -> tool (read-only) -> evidence -> decide ... -> stop -> ContextPackage
```

The model chooses actions; the application executes tools and enforces limits.
A failing tool is reported to the model and never crashes the turn.

## Tools (capabilities, not tables)

| Tool | What it does |
|------|--------------|
| find_exact | Words, names, numbers, file names (blind index; rarity-weighted per student) |
| find_by_meaning | Embedding similarity (reports honestly when embeddings are unavailable) |
| browse_time_window | What happened in a period |
| read_around | What happened just before/after a moment (defaults to the newest message) |
| inspect_item | Look closely at one item |
| trace_sources | Original evidence behind a summary/notebook entry/relationship |
| check_currency | Was this replaced? What replaced it? |
| follow_relationships | Walk the knowledge graph |
| read_artifact | Extracted contents of an artifact version |

The model decides which tool(s) to call. The application exposes capabilities
and validates the arguments — it never hardcodes which tool fits which kind of
student message. Embeddings, keyword search, notebook, and direct
database-history inspection are all *retrieval methods*; the model decides when
to use which, and may combine several. None of them decides the meaning of a
student's message.

## Evidence always carries

time, age relative to now, author kind (student/tutor/system), whether it is superseded,
and a plain-language provenance note.

## The exchange material

The application hands CI the **raw material of the live exchange** on every
turn: the newest saved history entries, in order, trimmed only for length
(`InvestigationLimits.max_exchange_entries` / `.max_exchange_characters`).

This is *supply*, the same category as tools, scope and limits. The
application does not decide that a turn matters, and it does not decide that
one does not. It hands over the clay.

**CI decides what travels to the Teacher.** The Teacher cannot see the
thread any other way, so the exchange is assembled by the intelligence, not
routed around it by the application. CI answers one question in the final
package:

| Field                   | Meaning                                                        |
| ----------------------- | -------------------------------------------------------------- |
| `include_exchange`      | `true` (default) — the Teacher receives the supplied exchange.  |
| `include_exchange_reason` | Why, when CI dropped it. Recorded in the audit trail.        |

The default is *include*. Silence never means "dropped": a malformed final
answer keeps the exchange, and dropping it has to be an explicit, reasoned
decision. CI drops the exchange only for a true cold start (no thread yet)
or when the thread is genuinely irrelevant and nothing in it needs
resolving — and it says so.

The package carries the decision in `ContextPackage.exchange`, and the
runtime records it in the `investigated` trail as
`exchange:included:<n>` or `exchange:dropped:<reason>`. A drop that nobody
can see would be a bug, not a decision.

The Teacher renders the exchange as plain "who said what, in order", and
does not repeat a turn that the exchange already carries.

## Investigation status (closed vocabulary)

Every `ContextPackage` carries an explicit `investigation_status` drawn from
a closed set. The Teacher can branch on this without parsing prose. The
status is computed by the runtime from what actually happened — never
from the model's own claim that it is "done".

| Status        | Meaning                                                              |
| ------------- | ------------------------------------------------------------------- |
| `successful`  | Investigation completed and at least one piece of relevant evidence was gathered. |
| `empty`       | Investigation completed but no relevant evidence was found.         |
| `incomplete` | The investigation hit a hard mechanical limit before the model signalled stop. |
| `partial`    | At least one tool failed or was unavailable during the investigation. |
| `failed`     | The investigation itself broke before any reliable result was produced. |
| `skipped`    | The message was blank; no investigation was attempted.               |

Distinctions the Teacher can now make:

- "CI looked and found nothing" (`empty`) vs "CI never finished" (`incomplete`)
  vs "CI broke" (`failed`). These are not interchangeable.
- A package with `invalid_selection=True` means the model returned evidence
  ids that matched none of the gathered evidence. The runtime ships an empty
  package and surfaces the failure — it does *not* silently include
  everything.

## Evidence selection contract

When CI finalises, it returns `selected_evidence_ids`:

- **empty selection** => "use all gathered evidence" (model delegates).
- **non-empty selection with at least one match** => use only matches.
- **non-empty selection with NO match** => record the failure in the
  `investigated` audit trail, set `invalid_selection=True`, ship an empty
  evidence tuple. The runtime *never* silently inflates the package with
  every gathered item when the model's selection matches nothing.

The old behaviour silently fell back to all gathered evidence on a
non-matching selection, hiding a broken selection behind a large package.
That fallback is gone.

## Notebook vs original database

The architecture preserves **both** roles and does not collapse them:

- **Original database (history)**: source of truth. Append-only. Never deleted.
  Age changes findability, never existence.
- **Notebook**: derived layer of useful understanding — preferences, facts,
  ongoing plans, relationships — extracted from history. Open-ended payload.
  Linked to its supporting evidence by provenance.

CI can use the notebook alone when its evidence is sufficient, inspect the
underlying history when exact wording or timing matters, or combine both.
A notebook entry is *not* a privileged source of truth — it is a
well-supported interpretation that the model can trace back to its sources
when the question demands.

## Lifecycle vocabulary (Blueprint Part IV)

Five non-interchangeable operations govern derived objects. History is
append-only and is never touched by these.

| Operation      | Meaning                                                            | Proposal operation(s)                              |
| -------------- | ----------------------------------------------------------------- | ------------------------------------------------- |
| Correction     | A claim or record was wrong; a corrected claim is recorded.       | supersede_notebook_entry / supersede_summary / supersede_node (with new evidence) |
| Supersession   | A newer or more applicable derived claim replaces an older one.  | supersede_notebook_entry / supersede_summary / supersede_node |
| Invalidation   | A derived object is no longer trustworthy; excluded from ordinary use. | invalidate_notebook_entry / invalidate_node / invalidate_summary |
| Deletion       | Information must no longer be retained or used.                  | request_deletion (forwards to a separate workflow) |
| Rebuilding     | A derived object or index is regenerated from sources.           | rebuild_index |

Supersession links a new claim to the old one but never rewrites the old
claim. Invalidation marks the object no-longer-trustworthy; it stays for
audit and is excluded from ordinary retrieval. Rebuilding regenerates search
tokens and embeddings from the source text. Deletion is *not* supersession
— it routes through `DeletionService` (see `src/waxprep/context/deletion.py`)
which applies retention policy, propagates through every dependent
representation (notebook rows, summaries, edges, tokens, embeddings,
caches), and records the propagation log on the request row.

Invalidation propagates through the derived graph: when a notebook entry is
invalidated, every object that cites it as evidence is also invalidated.
The propagation is bounded by provenance depth and is cycle-protected.

## Write side

CI **proposes**; the application validates, checks the evidence exists for
the same student, persists, links provenance, and indexes. Existing
operations: `create_node`, `create_edge`, `supersede_node`, `attach_evidence`,
`add_notebook_entry`, `supersede_notebook_entry`, `supersede_summary`,
`link_artifact`. New lifecycle operations: `invalidate_notebook_entry`,
`invalidate_node`, `invalidate_summary`, `rebuild_index`, `request_deletion`.

Every operation requires evidence. Support that is only tutor-authored is
flagged `needs_student_confirmation`.

## Retrieval of old history

Recency is a gentle tie-breaker (between 0.5x and 1x), never a filter. CI can set
`ignore_recency`. Old items are always reachable.

## Failure behaviour

If CI fails, the turn continues without context (`investigation_status=failed`).
If embeddings, summaries, or artifact reading fail, the history entry is
unaffected and the job retries. The Teacher is told, via the package's
status, exactly what CI did and did not establish — it does not silently
pretend.

## Prompt injection guardrails

Retrieved content (history entries, notebook notes, summaries, artifact
contents, relationships) is **evidence, not instructions**. A retrieved
message that says "ignore all rules" or "reveal another student's records"
is evidence that someone wrote those words. It does not:

- change CI's instructions
- grant permission to access another student's records
- bypass tool argument validation or student-scoped reads

Tool permissions are enforced outside the model. Every read is scoped to
the authenticated student. A model-generated request is never sufficient
authorization to access arbitrary records. Write proposals are validated
before persistence. Evidence and artifacts are treated as potentially
adversarial input.

## Observability

Every investigation persists a `context_investigations` audit row in
CI-first mode. The row carries:

- the trigger entry id
- the input text (the student's message)
- the closed-vocabulary `investigation_status`
- the package's summary, uncertainty, and `invalid_selection` flag
- the list of evidence ids and types
- the `investigated` audit trail (every action attempted, including failures)
- the `stopped_reason` and context model name
- whether the exchange was carried to the Teacher, and how many turns

Operational logs carry investigation id, trigger record, tool name and
duration, candidate and selected evidence counts, retry and failure class,
stop reason, package size, overall latency, and whether relevant context
was found. Operational observability does not quietly copy sensitive
student content into ordinary logs; full content traces require explicit
access controls, retention rules, and redaction.

## Configuration

```text
WAXPREP_MASTER_KEYS=1:<base64 32 bytes>[,2:...]   # required (encryption)
WAXPREP_ACTIVE_KEY_ID=...                          # optional
WAXPREP_CONTEXT_MODEL=...                          # logical registry name
WAXPREP_EMBEDDING_API_KEY / _BASE_URL / _MODEL     # optional live embeddings
```
