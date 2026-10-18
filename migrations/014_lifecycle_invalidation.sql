-- Evidence lifecycle operations (Blueprint Part IV §13-17).
--
-- Adds an explicit `invalidated` status to notebook_entries, summaries,
-- and knowledge_nodes. Supersession is *not* invalidation: a superseded
-- object is replaced by a newer one; an invalidated object is no longer
-- trustworthy but is retained for audit and excluded from ordinary
-- retrieval. The history table is untouched — history is append-only.
--
-- This migration is additive: it widens the CHECK constraints; no data
-- is rewritten. Existing rows keep their existing status.

-- 1. notebook_entries: allow 'invalidated' status.
ALTER TABLE notebook_entries DROP CONSTRAINT IF EXISTS notebook_entries_status_check;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'notebook_entries_status_check'
          AND conrelid = 'notebook_entries'::regclass
    ) THEN
        ALTER TABLE notebook_entries
            ADD CONSTRAINT notebook_entries_status_check
            CHECK (status IN ('active', 'superseded', 'invalidated'));
    END IF;
END
$$;

-- 2. summaries: allow 'invalidated' status.
ALTER TABLE summaries DROP CONSTRAINT IF EXISTS summaries_status_check;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'summaries_status_check'
          AND conrelid = 'summaries'::regclass
    ) THEN
        ALTER TABLE summaries
            ADD CONSTRAINT summaries_status_check
            CHECK (status IN ('active', 'superseded', 'invalidated'));
    END IF;
END
$$;

-- 3. knowledge_nodes: allow 'invalidated' status (alongside existing
-- active/superseded/dormant/deleted).
ALTER TABLE knowledge_nodes DROP CONSTRAINT IF EXISTS knowledge_nodes_status_check;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'knowledge_nodes_status_check'
          AND conrelid = 'knowledge_nodes'::regclass
    ) THEN
        ALTER TABLE knowledge_nodes
            ADD CONSTRAINT knowledge_nodes_status_check
            CHECK (status IN ('active', 'superseded', 'dormant', 'deleted', 'invalidated'));
    END IF;
END
$$;

-- 4. Audit table for deletion requests. The actual deletion workflow
-- is owned by the application — this table records that CI (or another
-- upstream component) requested deletion, and lets the workflow pick it
-- up later. Retention policy is enforced downstream; nothing here
-- stores the deleted content itself.
CREATE TABLE IF NOT EXISTS deletion_requests (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    object_type TEXT NOT NULL,
    object_id UUID NOT NULL,
    scope TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    propagation_log JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT deletion_requests_status_check
        CHECK (status IN ('pending', 'in_progress', 'completed', 'rejected'))
);

CREATE INDEX IF NOT EXISTS deletion_requests_wax_idx
    ON deletion_requests (wax_id, status);
CREATE INDEX IF NOT EXISTS deletion_requests_target_idx
    ON deletion_requests (wax_id, object_type, object_id);

ALTER TABLE deletion_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE deletion_requests FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS deletion_requests_isolation ON deletion_requests;
CREATE POLICY deletion_requests_isolation ON deletion_requests
    USING (wax_id = current_setting('app.current_wax_id', true)::uuid)
    WITH CHECK (wax_id = current_setting('app.current_wax_id', true)::uuid);
