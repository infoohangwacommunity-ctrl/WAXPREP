-- Student timezone + calendar — Founder's vision §7:
-- "A learning companion needs an understanding of time, not merely
-- timestamps attached to database records."
--
-- Two changes:
--
-- 1. `students.timezone` (TEXT, nullable) — so clock.py#student_local_now
--    can convert the runtime's UTC now() into the student's wall clock.
--    Nullable because we may not know the timezone yet (never ask for it
--    upfront — let it surface from context, e.g. "my exam is at 9am
--    tomorrow"). Stored as plain text per the IANA timezone database
--    (e.g. 'Africa/Lagos'); no CHECK constraint because the set of
--    valid IANA names is open-ended.
--
-- 2. `student_calendar` table — exam dates, deadlines, school sessions.
--    RLS-enabled + per-student encrypted (same pattern as
--    notebook_entries, migration 005). The event_date is plaintext so
--    range queries (what's coming up) work server-side. The notes are
--    encrypted because they may contain student-derived context.
--
-- Append-only and idempotent throughout.

-- 1. students.timezone ------------------------------------------------------
ALTER TABLE students
    ADD COLUMN IF NOT EXISTS timezone TEXT;

-- 2. student_calendar table -------------------------------------------------
CREATE TABLE IF NOT EXISTS student_calendar (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    -- event_kind: the Founder's brief lists exam / deadline / session / other.
    -- Stored as TEXT + CHECK (WaxPrep convention, not CREATE TYPE).
    event_kind TEXT NOT NULL,
    -- The wall-clock date of the event. Plaintext so range queries
    -- (from_date / to_date filters) run server-side as index scans.
    -- Stored as TIMESTAMPTZ so the runtime always works in UTC; the
    -- student's local timezone is applied at presentation time using
    -- students.timezone.
    event_date TIMESTAMPTZ NOT NULL,
    -- Optional subject — plaintext because it is a coarse label, not
    -- sensitive content. CI uses this to group "every Chemistry exam"
    -- or "every Mathematics deadline".
    subject TEXT,
    -- Free-form notes (e.g. "covers chapters 4-6"). AES-GCM ciphertext.
    notes_encrypted BYTEA,
    -- Sweep marker: expire_past_deadlines(now) sets this when a deadline
    -- event_date has passed. The row stays for audit; ordinary retrieval
    -- should exclude expired deadlines from "upcoming" lists.
    expired_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT student_calendar_event_kind_check
        CHECK (event_kind IN ('exam', 'deadline', 'session', 'other'))
);

-- Composite unique key so the table can be referenced from same-owner
-- FKs if a future migration needs to (mirrors migration 009 pattern).
-- Also a deterministic uniqueness anchor for tests.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'student_calendar_wax_id_id_key'
          AND conrelid = 'student_calendar'::regclass
    ) THEN
        ALTER TABLE student_calendar
            ADD CONSTRAINT student_calendar_wax_id_id_key UNIQUE (wax_id, id);
    END IF;
END
$$;

-- Indexes --------------------------------------------------------------------
-- Range query: "what's coming up for this student between from_date
-- and to_date?" Equality on wax_id (RLS policy column), range on
-- event_date. Canonical equality-first-then-range composite index
-- order from the database-reviewer checklist.
CREATE INDEX IF NOT EXISTS student_calendar_date_range_idx
    ON student_calendar (wax_id, event_date);

-- Sweep query: "give me every deadline that has not yet been expired
-- AND whose event_date is in the past". Partial on event_kind='deadline'
-- AND expired_at IS NULL so the index stays compact (only deadlines
-- that are still owed a sweep step participate).
CREATE INDEX IF NOT EXISTS student_calendar_due_sweep_idx
    ON student_calendar (wax_id, event_date)
    WHERE event_kind = 'deadline' AND expired_at IS NULL;

-- RLS -------------------------------------------------------------------------
-- NULLIF-safe pattern from migrations 002 + 015.
ALTER TABLE student_calendar ENABLE ROW LEVEL SECURITY;
ALTER TABLE student_calendar FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS student_calendar_isolation ON student_calendar;
CREATE POLICY student_calendar_isolation ON student_calendar
    FOR ALL
    USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON student_calendar TO waxprep_app;

-- Column comment for rotation introspection (per migration 018 pattern).
-- The cipher's AAD binds this purpose string into the ciphertext, so a
-- value copied into another column or another student fails to decrypt.
-- rotation.py introspects every `*_encrypted` column via this comment.
COMMENT ON COLUMN student_calendar.notes_encrypted IS
    'purpose=student_calendar.notes';
