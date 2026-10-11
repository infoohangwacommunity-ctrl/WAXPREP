-- Unified history architecture (additive only; nothing is dropped here).
-- History is the ground truth. Everything else is derived from it and
-- points back to it through provenance_links.
-- Sensitive text is stored as ciphertext (BYTEA) produced by the application.

CREATE TABLE IF NOT EXISTS history_entries (
    id UUID PRIMARY KEY,
    seq BIGINT GENERATED ALWAYS AS IDENTITY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    author_kind TEXT NOT NULL,
    content_kind TEXT NOT NULL,
    text_encrypted BYTEA,
    occurred_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    channel TEXT,
    channel_ref TEXT,
    thread_ref TEXT,
    reply_to_id UUID REFERENCES history_entries (id),
    CONSTRAINT history_author_kind_check
        CHECK (author_kind IN ('student', 'tutor', 'system')),
    CONSTRAINT history_wax_id_id_key UNIQUE (wax_id, id)
);

CREATE INDEX IF NOT EXISTS history_time_idx
    ON history_entries (wax_id, occurred_at, seq);

-- A channel may deliver the same message twice; saving must be idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS history_channel_ref_idx
    ON history_entries (wax_id, channel, channel_ref)
    WHERE channel IS NOT NULL AND channel_ref IS NOT NULL;

-- Blind word index: HMAC hashes of words, so exact search works on
-- encrypted data without the database ever seeing a word.
CREATE TABLE IF NOT EXISTS search_tokens (
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    object_type TEXT NOT NULL,
    object_id UUID NOT NULL,
    object_time TIMESTAMPTZ NOT NULL,
    token_hash BYTEA NOT NULL,
    PRIMARY KEY (wax_id, object_type, object_id, token_hash)
);

CREATE INDEX IF NOT EXISTS search_tokens_lookup_idx
    ON search_tokens (wax_id, token_hash, object_type, object_time);

CREATE TABLE IF NOT EXISTS search_embeddings (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    object_type TEXT NOT NULL,
    object_id UUID NOT NULL,
    object_time TIMESTAMPTZ NOT NULL,
    model TEXT NOT NULL,
    vector_encrypted BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT search_embeddings_unique
        UNIQUE (wax_id, object_type, object_id, model)
);

CREATE INDEX IF NOT EXISTS search_embeddings_time_idx
    ON search_embeddings (wax_id, object_time DESC);

CREATE TABLE IF NOT EXISTS summaries (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    text_encrypted BYTEA NOT NULL,
    covers_from TIMESTAMPTZ NOT NULL,
    covers_until TIMESTAMPTZ NOT NULL,
    level INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    produced_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT summaries_status_check CHECK (status IN ('active', 'superseded'))
);

CREATE INDEX IF NOT EXISTS summaries_wax_idx ON summaries (wax_id, covers_until DESC);

CREATE TABLE IF NOT EXISTS provenance_links (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    derived_type TEXT NOT NULL,
    derived_id UUID NOT NULL,
    source_type TEXT NOT NULL,
    source_id UUID NOT NULL,
    source_author_kind TEXT,
    quote_encrypted BYTEA,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS provenance_derived_idx
    ON provenance_links (wax_id, derived_type, derived_id);
CREATE INDEX IF NOT EXISTS provenance_source_idx
    ON provenance_links (wax_id, source_type, source_id);

CREATE TABLE IF NOT EXISTS artifact_contents (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    artifact_version_id UUID NOT NULL REFERENCES artifact_versions (id),
    representation TEXT NOT NULL,
    text_encrypted BYTEA NOT NULL,
    produced_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS artifact_contents_version_idx
    ON artifact_contents (wax_id, artifact_version_id);

CREATE TABLE IF NOT EXISTS derivation_jobs (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    kind TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id UUID NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    run_after TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT derivation_jobs_status_check
        CHECK (status IN ('pending', 'done', 'failed'))
);

CREATE INDEX IF NOT EXISTS derivation_jobs_due_idx
    ON derivation_jobs (wax_id, status, run_after);

-- Background workers must find which students have work without being
-- able to read any student data (same pattern as waxprep_lookup_channel).
CREATE OR REPLACE FUNCTION waxprep_students_with_due_jobs(
    p_now TIMESTAMPTZ,
    p_limit INTEGER
) RETURNS TABLE (wax_id UUID)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT DISTINCT j.wax_id
    FROM derivation_jobs j
    WHERE j.status = 'pending' AND j.run_after <= p_now
    LIMIT p_limit
$$;

REVOKE ALL ON FUNCTION waxprep_students_with_due_jobs(TIMESTAMPTZ, INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION waxprep_students_with_due_jobs(TIMESTAMPTZ, INTEGER)
    TO waxprep_app;

-- Notebook entries become derived understanding that can be replaced
-- (never deleted). Existing columns stay until 006.
ALTER TABLE notebook_entries
    ADD COLUMN IF NOT EXISTS payload_encrypted BYTEA,
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS superseded_by_id UUID;

-- Row-Level Security for every new student-scoped table.
ALTER TABLE history_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE history_entries FORCE ROW LEVEL SECURITY;
ALTER TABLE search_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE search_tokens FORCE ROW LEVEL SECURITY;
ALTER TABLE search_embeddings ENABLE ROW LEVEL SECURITY;
ALTER TABLE search_embeddings FORCE ROW LEVEL SECURITY;
ALTER TABLE summaries ENABLE ROW LEVEL SECURITY;
ALTER TABLE summaries FORCE ROW LEVEL SECURITY;
ALTER TABLE provenance_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE provenance_links FORCE ROW LEVEL SECURITY;
ALTER TABLE artifact_contents ENABLE ROW LEVEL SECURITY;
ALTER TABLE artifact_contents FORCE ROW LEVEL SECURITY;
ALTER TABLE derivation_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE derivation_jobs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS history_entries_isolation ON history_entries;
CREATE POLICY history_entries_isolation ON history_entries
    FOR ALL USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

DROP POLICY IF EXISTS search_tokens_isolation ON search_tokens;
CREATE POLICY search_tokens_isolation ON search_tokens
    FOR ALL USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

DROP POLICY IF EXISTS search_embeddings_isolation ON search_embeddings;
CREATE POLICY search_embeddings_isolation ON search_embeddings
    FOR ALL USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

DROP POLICY IF EXISTS summaries_isolation ON summaries;
CREATE POLICY summaries_isolation ON summaries
    FOR ALL USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

DROP POLICY IF EXISTS provenance_links_isolation ON provenance_links;
CREATE POLICY provenance_links_isolation ON provenance_links
    FOR ALL USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

DROP POLICY IF EXISTS artifact_contents_isolation ON artifact_contents;
CREATE POLICY artifact_contents_isolation ON artifact_contents
    FOR ALL USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

DROP POLICY IF EXISTS derivation_jobs_isolation ON derivation_jobs;
CREATE POLICY derivation_jobs_isolation ON derivation_jobs
    FOR ALL USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON
    history_entries, search_tokens, search_embeddings, summaries,
    provenance_links, artifact_contents, derivation_jobs
    TO waxprep_app;
