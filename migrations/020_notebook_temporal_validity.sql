-- Temporal validity on notebook_entries — Founder's vision §7-8:
-- "Time must also be truthful. Never assume that a deadline is still
-- upcoming simply because the notebook says it is. CI should check the
-- date against the current clock."
--
-- A notebook entry can now carry an explicit temporal meaning:
--   * valid_from   — when this fact became true (e.g. "since 2026-11-15")
--   * valid_until  — when this fact stops being true (e.g. an exam date)
--   * temporal_kind — what kind of temporal fact it is:
--       one_time_event — happened on a date, no longer current after
--       recurring      — happens every week / every term
--       deadline       — a specific future date that will pass
--       indefinite     — true until explicitly superseded (default
--                       behaviour for facts that don't carry time)
--
-- All three columns are NULLABLE. Existing notebook entries keep the
-- pre-migration behaviour: NULL valid_until means "still current
-- unless explicitly superseded"; NULL temporal_kind means "no
-- temporal meaning asserted". CI's `check_currency` tool reads these
-- columns and the runtime's authoritative clock to decide whether a
-- fact is still current — never the notebook's own timestamp alone.
--
-- This migration is additive: every statement uses IF NOT EXISTS.
-- No data is rewritten.

ALTER TABLE notebook_entries
    ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ;
ALTER TABLE notebook_entries
    ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ;
ALTER TABLE notebook_entries
    ADD COLUMN IF NOT EXISTS temporal_kind TEXT;

-- CHECK constraint on temporal_kind. NULL is permitted (legacy rows +
-- entries with no temporal meaning). WaxPrep convention: TEXT + CHECK,
-- not CREATE TYPE.
ALTER TABLE notebook_entries
    DROP CONSTRAINT IF EXISTS notebook_entries_temporal_kind_check;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'notebook_entries_temporal_kind_check'
          AND conrelid = 'notebook_entries'::regclass
    ) THEN
        ALTER TABLE notebook_entries
            ADD CONSTRAINT notebook_entries_temporal_kind_check
            CHECK (temporal_kind IS NULL
                OR temporal_kind IN (
                    'one_time_event',
                    'recurring',
                    'deadline',
                    'indefinite'
                ));
    END IF;
END
$$;

-- Sanity constraint: valid_until, when set, must be after valid_from.
-- A row with valid_from > valid_until is logically empty (nothing is
-- ever true) and almost always a bug.
ALTER TABLE notebook_entries
    DROP CONSTRAINT IF EXISTS notebook_entries_valid_range_check;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'notebook_entries_valid_range_check'
          AND conrelid = 'notebook_entries'::regclass
    ) THEN
        ALTER TABLE notebook_entries
            ADD CONSTRAINT notebook_entries_valid_range_check
            CHECK (valid_from IS NULL
                OR valid_until IS NULL
                OR valid_until >= valid_from);
    END IF;
END
$$;

-- Index for "find every notebook entry whose deadline is still in the
-- future" — the canonical CI tool query when it wants to surface what
-- is coming up for the student. Partial on temporal_kind='deadline'
-- AND valid_until IS NOT NULL so only rows that are actually deadlines
-- with a real date participate in the index.
CREATE INDEX IF NOT EXISTS notebook_entries_deadline_idx
    ON notebook_entries (wax_id, valid_until)
    WHERE temporal_kind = 'deadline' AND valid_until IS NOT NULL;

-- No RLS or GRANT changes: the columns live on an existing RLS-protected
-- table that already grants SELECT/INSERT/UPDATE/DELETE to waxprep_app
-- (migrations 002 + 005).
