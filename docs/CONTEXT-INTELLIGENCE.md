# Context Intelligence

CI is a **small AI model** that turns "the student just said X, at time T" into a compact,
dated, traceable context package for the Teacher. CI does **not** teach.

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

## Evidence always carries

time, age relative to now, author kind (student/tutor/system), whether it is superseded,
and a plain-language provenance note.

## Write side

CI **proposes**; the application validates, checks the evidence exists for the same student,
persists, links provenance, and indexes. Operations: create_node, create_edge, supersede_node,
attach_evidence, add_notebook_entry, supersede_notebook_entry, supersede_summary, link_artifact.
Every operation requires evidence. Support that is only tutor-authored is flagged
`needs_student_confirmation`.

## Retrieval of old history

Recency is a gentle tie-breaker (between 0.5x and 1x), never a filter. CI can set
`ignore_recency`. Old items are always reachable.

## Failure behaviour

If CI fails, the turn continues without context (`context_unavailable`).
If embeddings, summaries, or artifact reading fail, the history entry is unaffected and the job retries.

## Configuration

```text
WAXPREP_MASTER_KEYS=1:<base64 32 bytes>[,2:...]   # required (encryption)
WAXPREP_ACTIVE_KEY_ID=...                          # optional
WAXPREP_CONTEXT_MODEL=...                          # logical registry name
WAXPREP_EMBEDDING_API_KEY / _BASE_URL / _MODEL     # optional live embeddings
```
