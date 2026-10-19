-- Build 4 foundation — open-world knowledge + Context Intelligence substrate.
-- Append-only. No fixed student-profile columns (school/class/strengths/etc.).
-- Embeddings stored as double precision[] so CI does not require pgvector.

CREATE TABLE IF NOT EXISTS knowledge_nodes (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    node_type TEXT NOT NULL,
    label TEXT NOT NULL,
    description TEXT,
    properties JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT knowledge_nodes_status_check
        CHECK (status IN ('active', 'superseded', 'dormant', 'deleted'))
);

CREATE INDEX IF NOT EXISTS knowledge_nodes_wax_idx
    ON knowledge_nodes (wax_id);
CREATE INDEX IF NOT EXISTS knowledge_nodes_type_idx
    ON knowledge_nodes (wax_id, node_type);

CREATE TABLE IF NOT EXISTS knowledge_edges (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    source_node_id UUID NOT NULL REFERENCES knowledge_nodes (id) ON DELETE CASCADE,
    target_node_id UUID NOT NULL REFERENCES knowledge_nodes (id) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    properties JSONB NOT NULL DEFAULT '{}'::jsonb,
    valid_from TIMESTAMPTZ,
    valid_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS knowledge_edges_source_idx
    ON knowledge_edges (wax_id, source_node_id);
CREATE INDEX IF NOT EXISTS knowledge_edges_target_idx
    ON knowledge_edges (wax_id, target_node_id);
CREATE INDEX IF NOT EXISTS knowledge_edges_relation_idx
    ON knowledge_edges (wax_id, relation);

CREATE TABLE IF NOT EXISTS knowledge_evidence (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    node_id UUID REFERENCES knowledge_nodes (id) ON DELETE CASCADE,
    edge_id UUID REFERENCES knowledge_edges (id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    source_id UUID,
    quote TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS knowledge_evidence_node_idx
    ON knowledge_evidence (wax_id, node_id);
CREATE INDEX IF NOT EXISTS knowledge_evidence_edge_idx
    ON knowledge_evidence (wax_id, edge_id);
CREATE INDEX IF NOT EXISTS knowledge_evidence_source_idx
    ON knowledge_evidence (wax_id, source_type, source_id);

CREATE TABLE IF NOT EXISTS knowledge_embeddings (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    object_type TEXT NOT NULL,
    object_id UUID NOT NULL,
    content TEXT NOT NULL,
    embedding DOUBLE PRECISION[] NOT NULL,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS knowledge_embeddings_wax_idx
    ON knowledge_embeddings (wax_id);
CREATE INDEX IF NOT EXISTS knowledge_embeddings_object_idx
    ON knowledge_embeddings (wax_id, object_type, object_id);

CREATE TABLE IF NOT EXISTS context_investigations (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    conversation_id UUID,
    input_text TEXT NOT NULL,
    result JSONB NOT NULL,
    model TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS context_investigations_wax_idx
    ON context_investigations (wax_id, created_at);

ALTER TABLE knowledge_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_nodes FORCE ROW LEVEL SECURITY;
ALTER TABLE knowledge_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_edges FORCE ROW LEVEL SECURITY;
ALTER TABLE knowledge_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_evidence FORCE ROW LEVEL SECURITY;
ALTER TABLE knowledge_embeddings ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_embeddings FORCE ROW LEVEL SECURITY;
ALTER TABLE context_investigations ENABLE ROW LEVEL SECURITY;
ALTER TABLE context_investigations FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS knowledge_nodes_isolation ON knowledge_nodes;
CREATE POLICY knowledge_nodes_isolation ON knowledge_nodes
    FOR ALL
    USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

DROP POLICY IF EXISTS knowledge_edges_isolation ON knowledge_edges;
CREATE POLICY knowledge_edges_isolation ON knowledge_edges
    FOR ALL
    USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

DROP POLICY IF EXISTS knowledge_evidence_isolation ON knowledge_evidence;
CREATE POLICY knowledge_evidence_isolation ON knowledge_evidence
    FOR ALL
    USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

DROP POLICY IF EXISTS knowledge_embeddings_isolation ON knowledge_embeddings;
CREATE POLICY knowledge_embeddings_isolation ON knowledge_embeddings
    FOR ALL
    USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

DROP POLICY IF EXISTS context_investigations_isolation ON context_investigations;
CREATE POLICY context_investigations_isolation ON context_investigations
    FOR ALL
    USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON
    knowledge_nodes, knowledge_edges, knowledge_evidence,
    knowledge_embeddings, context_investigations
    TO waxprep_app;
