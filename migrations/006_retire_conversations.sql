-- Retire the conversation-based model.
-- Safe by construction: it REFUSES to run if the backfill
-- (waxprep.history.backfill) has not converted every legacy row.
-- On an empty/pre-launch database it simply cleans up.
--
-- The guards check for UN-BACKFILLED rows, not for the mere existence of
-- rows. After the backfill:
-- - every notebook_entries row has payload_encrypted set
-- - every artifact_versions row has filename_encrypted set
-- - every messages row exists in history_entries
-- The guards pass, and migration 006 drops the old plaintext columns.

-- Step 1: add the new columns idempotently BEFORE the guards so the
-- guards can check them. The backfill may have already added some of
-- these; IF NOT EXISTS makes this safe.
ALTER TABLE artifact_versions ADD COLUMN IF NOT EXISTS filename_encrypted BYTEA;

-- Step 2: guards — refuse to proceed if any legacy data hasn't been
-- backfilled yet.
DO $$
BEGIN
    -- Messages must be copied to history_entries first.
    IF EXISTS (
        SELECT 1 FROM messages m
        WHERE NOT EXISTS (SELECT 1 FROM history_entries h WHERE h.id = m.id)
    ) THEN
        RAISE EXCEPTION
            'messages not yet copied to history_entries: run the backfill first';
    END IF;
    -- Notebook entries must have payload_encrypted set (backfill converts
    -- plaintext payload → encrypted).
    IF EXISTS (SELECT 1 FROM notebook_entries WHERE payload_encrypted IS NULL) THEN
        RAISE EXCEPTION
            'notebook_entries has rows without payload_encrypted: run the backfill first';
    END IF;
    -- Old knowledge/investigation tables must be empty (the app never
    -- wrote to them; if rows exist, they were written by another process
    -- and require manual conversion).
    IF EXISTS (SELECT 1 FROM knowledge_nodes)
       OR EXISTS (SELECT 1 FROM knowledge_edges)
       OR EXISTS (SELECT 1 FROM knowledge_evidence)
       OR EXISTS (SELECT 1 FROM knowledge_embeddings)
       OR EXISTS (SELECT 1 FROM context_investigations) THEN
        RAISE EXCEPTION
            'old knowledge/investigation tables have rows; they were never written by the app';
    END IF;
    -- Artifact versions must have filename_encrypted set for every row
    -- that has a plaintext filename.
    IF EXISTS (
        SELECT 1 FROM artifact_versions
        WHERE original_filename IS NOT NULL
          AND filename_encrypted IS NULL
    ) THEN
        RAISE EXCEPTION
            'artifact_versions has filenames not yet encrypted: run the backfill first';
    END IF;
END
$$;

-- Step 3: retire the old schema.
ALTER TABLE attachments DROP CONSTRAINT IF EXISTS attachments_message_id_fkey;
ALTER TABLE attachments RENAME COLUMN message_id TO history_entry_id;
ALTER TABLE attachments
    ADD CONSTRAINT attachments_history_entry_fkey
    FOREIGN KEY (history_entry_id) REFERENCES history_entries (id);

DROP TABLE IF EXISTS messages CASCADE;
DROP TABLE IF EXISTS conversations CASCADE;

-- Notebook: payload is encrypted; provenance replaces source_type/source_ref.
ALTER TABLE notebook_entries DROP COLUMN IF EXISTS payload;
ALTER TABLE notebook_entries DROP COLUMN IF EXISTS source_type;
ALTER TABLE notebook_entries DROP COLUMN IF EXISTS source_ref;
ALTER TABLE notebook_entries ALTER COLUMN payload_encrypted SET NOT NULL;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'notebook_entries_status_check'
          AND conrelid = 'notebook_entries'::regclass
    ) THEN
        ALTER TABLE notebook_entries
            ADD CONSTRAINT notebook_entries_status_check
            CHECK (status IN ('active', 'superseded'));
    END IF;
END
$$;

-- Artifact filenames are student information: drop the plaintext column.
-- filename_encrypted was added in Step 1.
ALTER TABLE artifact_versions DROP COLUMN IF EXISTS original_filename;

-- Knowledge graph: recreate with encrypted text. Evidence now lives in
-- provenance_links, and vectors live in search_embeddings.
DROP TABLE IF EXISTS context_investigations CASCADE;
DROP TABLE IF EXISTS knowledge_embeddings CASCADE;
DROP TABLE IF EXISTS knowledge_evidence CASCADE;
DROP TABLE IF EXISTS knowledge_edges CASCADE;
DROP TABLE IF EXISTS knowledge_nodes CASCADE;

CREATE TABLE IF NOT EXISTS knowledge_nodes (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    node_type_encrypted BYTEA NOT NULL,
    label_encrypted BYTEA NOT NULL,
    description_encrypted BYTEA,
    properties_encrypted BYTEA NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT knowledge_nodes_status_check
        CHECK (status IN ('active', 'superseded', 'dormant', 'deleted')),
    CONSTRAINT knowledge_nodes_wax_id_id_key UNIQUE (wax_id, id)
);

CREATE INDEX IF NOT EXISTS knowledge_nodes_wax_idx ON knowledge_nodes (wax_id);

CREATE TABLE IF NOT EXISTS knowledge_edges (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    source_node_id UUID NOT NULL,
    target_node_id UUID NOT NULL,
    relation_encrypted BYTEA NOT NULL,
    properties_encrypted BYTEA NOT NULL,
    valid_from TIMESTAMPTZ,
    valid_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT knowledge_edges_source_owner
        FOREIGN KEY (wax_id, source_node_id) REFERENCES knowledge_nodes (wax_id, id),
    CONSTRAINT knowledge_edges_target_owner
        FOREIGN KEY (wax_id, target_node_id) REFERENCES knowledge_nodes (wax_id, id)
);

CREATE INDEX IF NOT EXISTS knowledge_edges_source_idx ON knowledge_edges (wax_id, source_node_id);
CREATE INDEX IF NOT EXISTS knowledge_edges_target_idx ON knowledge_edges (wax_id, target_node_id);

CREATE TABLE IF NOT EXISTS context_investigations (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    trigger_entry_id UUID,
    input_encrypted BYTEA NOT NULL,
    result_encrypted BYTEA NOT NULL,
    model TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS context_investigations_wax_idx
    ON context_investigations (wax_id, created_at);

ALTER TABLE knowledge_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_nodes FORCE ROW LEVEL SECURITY;
ALTER TABLE knowledge_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_edges FORCE ROW LEVEL SECURITY;
ALTER TABLE context_investigations ENABLE ROW LEVEL SECURITY;
ALTER TABLE context_investigations FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS knowledge_nodes_isolation ON knowledge_nodes;
CREATE POLICY knowledge_nodes_isolation ON knowledge_nodes
    FOR ALL USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());
DROP POLICY IF EXISTS knowledge_edges_isolation ON knowledge_edges;
CREATE POLICY knowledge_edges_isolation ON knowledge_edges
    FOR ALL USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());
DROP POLICY IF EXISTS context_investigations_isolation ON context_investigations;
CREATE POLICY context_investigations_isolation ON context_investigations
    FOR ALL USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON
    knowledge_nodes, knowledge_edges, context_investigations
    TO waxprep_app;
