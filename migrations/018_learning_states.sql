-- learning_states — first-class home for the "current learning state" (Founder's vision §6).
--
-- The Founder's brief distinguishes three layers:
--   * history_entries        — the original message history (ground truth)
--   * notebook_entries        — durable, derived understanding (long-term)
--   * learning_states (NEW)  — what is happening in the lesson RIGHT NOW
--
-- A learning state is the live "where is the student in this subject"
-- snapshot: the current topic, current stage, demonstrated vs. developing
-- vs. not-assessed, the teaching observation, and the next sensible step.
-- It is replaced (superseded) every time the student advances; it is
-- never edited. The previous state stays for audit and provenance.
--
-- This table mirrors the notebook_entries pattern (migration 005/006):
--   * RLS enabled + forced (per migration 002 + 015 NULLIF-safe helper)
--   * payload_encrypted BYTEA — AES-GCM ciphertext, per-student derived key
--   * subject_hash BYTEA — blind HMAC index so the unique constraint on
--     (wax_id, subject_hash) does not leak the subject through the index
--   * composite-FK self-reference for supersession (per migration 009)
--   * partial unique index: one ACTIVE state per subject per student
--
-- Append-only and idempotent: every statement uses IF NOT EXISTS or is
-- wrapped in a DO block that swallows "already exists" errors. No
-- existing migration is modified.

-- 1. Table -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS learning_states (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    -- Blind HMAC index over the subject string. The cipher's blind_index
    -- produces 16-byte HMAC-SHA256 truncations (see security/cipher.py).
    -- Storing the hash (not the subject) keeps the unique constraint
    -- from leaking the subject through the index leafs.
    subject_hash BYTEA NOT NULL,
    -- AES-GCM ciphertext. The blob header carries [format_version, key_id,
    -- nonce, ciphertext+tag] (see security/cipher.py). payload_key_id is
    -- a redundant queryable copy of the key id at write time — useful for
    -- rotation audits that want to find rows encrypted under a specific
    -- key without parsing the blob header.
    payload_encrypted BYTEA NOT NULL,
    payload_key_id INT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    paused_at TIMESTAMPTZ,
    last_active_at TIMESTAMPTZ NOT NULL,
    superseded_by UUID,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT learning_states_status_check
        CHECK (status IN ('active', 'superseded'))
);

-- Composite unique key so the self-FK on (wax_id, superseded_by) can
-- enforce same-owner supersession (per migration 009 pattern).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'learning_states_wax_id_id_key'
          AND conrelid = 'learning_states'::regclass
    ) THEN
        ALTER TABLE learning_states
            ADD CONSTRAINT learning_states_wax_id_id_key UNIQUE (wax_id, id);
    END IF;
END
$$;

-- Same-owner self-FK: a learning state can only be superseded by another
-- learning state owned by the same student. DEFERRABLE INITIALLY
-- IMMEDIATE so the storage layer can UPDATE the old row then INSERT the
-- new one in the same transaction without ordering pain.
ALTER TABLE learning_states
    DROP CONSTRAINT IF EXISTS learning_states_supersession_owner_fk;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'learning_states_supersession_owner_fk'
          AND conrelid = 'learning_states'::regclass
    ) THEN
        ALTER TABLE learning_states
            ADD CONSTRAINT learning_states_supersession_owner_fk
            FOREIGN KEY (wax_id, superseded_by)
            REFERENCES learning_states (wax_id, id)
            DEFERRABLE INITIALLY IMMEDIATE;
    END IF;
END
$$;

-- 2. Indexes -----------------------------------------------------------------
-- One current (active) state per subject per student. The partial
-- predicate keeps the index compact: only one row per (wax_id,
-- subject_hash) can have status='active'. A second write for the same
-- subject must first supersede the existing active row.
CREATE UNIQUE INDEX IF NOT EXISTS learning_states_active_per_subject_idx
    ON learning_states (wax_id, subject_hash)
    WHERE status = 'active';

-- "What was the student working on most recently?" Equality on wax_id
-- (RLS policy column) + range on last_active_at DESC. Canonical
-- equality-first-then-range composite index order from the
-- database-reviewer checklist.
CREATE INDEX IF NOT EXISTS learning_states_recent_activity_idx
    ON learning_states (wax_id, last_active_at DESC);

-- 3. RLS ----------------------------------------------------------------------
-- NULLIF-safe pattern from migration 002 + reaffirmed in migration 015.
-- `waxprep_current_wax_id()` returns NULL when the GUC is unset or '',
-- which fails the equality test (no rows match — fail closed).
ALTER TABLE learning_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE learning_states FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS learning_states_isolation ON learning_states;
CREATE POLICY learning_states_isolation ON learning_states
    FOR ALL
    USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

-- 4. Grants ------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON learning_states TO waxprep_app;

-- 5. Column comment for rotation introspection -------------------------------
-- The cipher binds a `purpose` string into the AES-GCM AAD so a value
-- copied into another column or another student fails to decrypt. The
-- rotation code (security/rotation.py::discover_encrypted_columns)
-- introspects every `*_encrypted` column via this comment to discover
-- the purpose — no Python-side hardcoded list to maintain. New
-- migrations that add an `*_encrypted` column MUST set the same kind
-- of comment. See migration 022 for the backfill on every legacy
-- encrypted column.
COMMENT ON COLUMN learning_states.payload_encrypted IS
    'purpose=learning_state.payload';
