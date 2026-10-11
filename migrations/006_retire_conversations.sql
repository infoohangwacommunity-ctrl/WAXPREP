-- Retire the conversation-based model.
-- Safe by construction: it REFUSES to run if any old message has not been
-- copied into history_entries first (see waxprep.history.backfill).
-- On an empty/pre-launch database it simply cleans up.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM messages m
        WHERE NOT EXISTS (SELECT 1 FROM history_entries h WHERE h.id = m.id)
    ) THEN
        RAISE EXCEPTION
            'messages not yet copied to history_entries: run the backfill first';
    END IF;
    IF EXISTS (SELECT 1 FROM notebook_entries) THEN
        RAISE EXCEPTION
            'notebook_entries has rows: re-save them through the new notebook first';
    END IF;
    IF EXISTS (SELECT 1 FROM knowledge_nodes)
       OR EXISTS (SELECT 1 FROM knowledge_edges)
       OR EXISTS (SELECT 1 FROM knowledge_evidence)
       OR EXISTS (SELECT 1 FROM knowledge_embeddings)
       OR EXISTS (SELECT 1 FROM context_investigations) THEN
        RAISE EXCEPTION
            'old knowledge/investigation tables have rows; they were never written by the app';
    END IF;
    IF EXISTS (SELECT 1 FROM artifact_versions WHERE original_filename IS NOT NULL) THEN
        RAISE EXCEPTION
            'artifact filenames must be re-saved encrypted before this migration';
    END IF;
END
$$;

-- Attachments now hang off history entries.
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
ALTER TABLE notebook_entries
    ADD CONSTRAINT notebook_entries_status_check
    CHECK (status IN ('active', 'superseded'));

-- Artifact filenames are student information: store them encrypted.
ALTER TABLE artifact_versions DROP COLUMN IF EXISTS original_filename;
ALTER TABLE artifact_versions ADD COLUMN filename_encrypted BYTEA;

-- Knowledge graph: recreate with encrypted text. Evidence now lives in
-- provenance_links, and vectors live in search_embeddings.
DROP TABLE IF EXISTS context_investigations CASCADE;
DROP TABLE IF EXISTS knowledge_embeddings CASCADE;
DROP TABLE IF EXISTS knowledge_evidence CASCADE;
DROP TABLE IF EXISTS knowledge_edges CASCADE;
DROP TABLE IF EXISTS knowledge_nodes CASCADE;

CREATE TABLE knowledge_nodes (
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

CREATE INDEX knowledge_nodes_wax_idx ON knowledge_nodes (wax_id);

CREATE TABLE knowledge_edges (
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

CREATE INDEX knowledge_edges_source_idx ON knowledge_edges (wax_id, source_node_id);
CREATE INDEX knowledge_edges_target_idx ON knowledge_edges (wax_id, target_node_id);

CREATE TABLE context_investigations (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    trigger_entry_id UUID REFERENCES history_entries (id),
    input_encrypted BYTEA NOT NULL,
    result_encrypted BYTEA NOT NULL,
    model TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX context_investigations_wax_idx
    ON context_investigations (wax_id, created_at);

ALTER TABLE knowledge_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_nodes FORCE ROW LEVEL SECURITY;
ALTER TABLE knowledge_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_edges FORCE ROW LEVEL SECURITY;
ALTER TABLE context_investigations ENABLE ROW LEVEL SECURITY;
ALTER TABLE context_investigations FORCE ROW LEVEL SECURITY;

CREATE POLICY knowledge_nodes_isolation ON knowledge_nodes
    FOR ALL USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());
CREATE POLICY knowledge_edges_isolation ON knowledge_edges
    FOR ALL USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());
CREATE POLICY context_investigations_isolation ON context_investigations
    FOR ALL USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON
    knowledge_nodes, knowledge_edges, context_investigations
    TO waxprep_app;
