-- evidence_strength on provenance_links — Founder's vision §5:
-- "A student's answer is not automatically proof of mastery."
--
-- The Founder distinguishes several kinds of evidence:
--   * self_reported  — student said "I understand"
--   * hint_assisted  — student solved the problem only after a hint
--   * independent    — student solved the problem with no help
--   * inferred       — CI inferred this from indirect signals (default)
--
-- Without this column CI cannot tell "the student said they understand"
-- from "the student solved five problems independently". Both might
-- produce the same notebook entry, but they have very different
-- teaching implications.
--
-- This migration adds the column as nullable with a default of
-- 'inferred' so existing rows keep a sensible interpretation: a
-- pre-existing provenance link is treated as inferred evidence (the
-- safest reading — we don't know what the student actually did).
--
-- WaxPrep convention: TEXT columns with a CHECK constraint, not a
-- CREATE TYPE enum (matches `history_author_kind_check`,
-- `summaries_status_check`, `notebook_entries_status_check`).
-- Idempotent and append-only.

ALTER TABLE provenance_links
    ADD COLUMN IF NOT EXISTS evidence_strength TEXT;

-- Backfill default for legacy rows. Using UPDATE (not ALTER ... SET
-- DEFAULT) keeps the migration safe to re-run on a database that
-- already has rows; existing NULLs become 'inferred'. New writes
-- pick up the DEFAULT clause below.
UPDATE provenance_links
   SET evidence_strength = 'inferred'
 WHERE evidence_strength IS NULL;

ALTER TABLE provenance_links
    ALTER COLUMN evidence_strength SET DEFAULT 'inferred';

ALTER TABLE provenance_links
    DROP CONSTRAINT IF EXISTS provenance_links_evidence_strength_check;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'provenance_links_evidence_strength_check'
          AND conrelid = 'provenance_links'::regclass
    ) THEN
        ALTER TABLE provenance_links
            ADD CONSTRAINT provenance_links_evidence_strength_check
            CHECK (evidence_strength IS NULL
                OR evidence_strength IN (
                    'self_reported',
                    'hint_assisted',
                    'independent',
                    'inferred'
                ));
    END IF;
END
$$;

-- Index for "give me every independent-evidence link supporting this
-- notebook entry" — the canonical CI query when it wants to know
-- whether a notebook entry has demonstration backing, not just a
-- self-report.
CREATE INDEX IF NOT EXISTS provenance_links_evidence_strength_idx
    ON provenance_links (wax_id, derived_type, derived_id, evidence_strength);

-- No RLS or GRANT changes: the column lives on an existing RLS-protected
-- table that already grants SELECT/INSERT/UPDATE/DELETE to waxprep_app
-- (migration 005).
