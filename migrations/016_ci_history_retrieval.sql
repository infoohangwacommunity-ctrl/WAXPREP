-- CI history retrieval — fast cursor pagination for history_entries.
--
-- Background:
--   The unified history table is the ground truth for every student
--   interaction. Existing migration 005 created `history_time_idx
--   (wax_id, occurred_at, seq)` which serves ASC scans efficiently and
--   can be walked backwards for DESC scans. In practice the dominant
--   access pattern is "newest first, page by cursor" — used by the
--   paginated reader (`list_history_paginated`) and by the
--   time-window context browser (`Retriever.browse_window`).
--
--   A dedicated `(wax_id, occurred_at DESC, seq DESC)` index gives
--   the planner a guaranteed forward scan for the DESC direction, so
--   cursor pagination never pays for a backwards B-tree walk that
--   must visit every leaf in the wax_id partition.
--
--   A partial index on `(wax_id, thread_ref, occurred_at DESC)` is
--   added for thread-scoped retrieval — the previous full-table
--   `(wax_id, thread_ref, occurred_at)` would also have indexed rows
--   where thread_ref IS NULL, which never participate in thread
--   queries. The partial index is smaller and stays cache-hot.
--
-- Append-only and idempotent: `CREATE INDEX IF NOT EXISTS` makes the
-- migration safe to re-run, and nothing here touches data or existing
-- schema. RLS policies from migration 005 remain in force — indexes
-- are storage-level structures and have no effect on policy
-- evaluation.

-- 1. Cursor-pagination index for "newest first" history reads.
--    Equality on wax_id (RLS policy column) + range on occurred_at +
--    tiebreaker on seq. This is the canonical "equality first, then
--    range" composite index order from the database-reviewer
--    checklist.
CREATE INDEX IF NOT EXISTS idx_history_wax_occurred_desc
    ON history_entries (wax_id, occurred_at DESC, seq DESC);

-- 2. Thread-scoped retrieval: only rows that belong to a thread
--    participate in thread queries. The partial predicate keeps the
--    index compact and the wax_id+thread_ref equality prefix is
--    exactly the access path Retriever.follow_relationships and the
--    thread browser use.
CREATE INDEX IF NOT EXISTS idx_history_wax_thread
    ON history_entries (wax_id, thread_ref, occurred_at DESC)
    WHERE thread_ref IS NOT NULL;

-- 3. Time-range search support. `list_history(since, until)` and the
--    new `search_history_by_time_range` both filter on (wax_id,
--    occurred_at) with two range bounds. The existing history_time_idx
--    already serves this, but adding a covering index that includes
--    `id` lets the executor answer a "list IDs in range" query as an
--    index-only scan.
CREATE INDEX IF NOT EXISTS idx_history_wax_time_range
    ON history_entries (wax_id, occurred_at, seq)
    INCLUDE (id);

-- The waxprep_app role already has SELECT on history_entries
-- (migration 002 + 005). Indexes are world-readable by every role
-- that can SELECT the table; no further GRANT is needed.
