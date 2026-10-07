-- Database-enforced student isolation via Row-Level Security.
-- Application sets: SET LOCAL app.current_wax_id = '<uuid>'
-- FORCE ROW LEVEL SECURITY so even table owners cannot bypass policies.

-- Application role used by the storage layer (non-superuser).
-- Non-superuser application role (superusers always bypass RLS).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waxprep_app') THEN
        CREATE ROLE waxprep_app NOINHERIT NOSUPERUSER NOBYPASSRLS
            LOGIN PASSWORD 'waxprep_app';
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO waxprep_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    students, channel_identities, conversations, messages,
    attachments, notebooks, notebook_entries, events
    TO waxprep_app;
-- Allow migrator/superuser to SET ROLE waxprep_app in tests/CI.
DO $$
BEGIN
    -- Grant membership so the bootstrap role can SET ROLE waxprep_app.
    EXECUTE format(
        'GRANT waxprep_app TO %I',
        current_user
    );
EXCEPTION WHEN OTHERS THEN
    NULL; -- membership may already exist
END
$$;

-- Enable + force RLS on every student-scoped table.
ALTER TABLE students ENABLE ROW LEVEL SECURITY;
ALTER TABLE students FORCE ROW LEVEL SECURITY;

ALTER TABLE channel_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE channel_identities FORCE ROW LEVEL SECURITY;

ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations FORCE ROW LEVEL SECURITY;

ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages FORCE ROW LEVEL SECURITY;

ALTER TABLE attachments ENABLE ROW LEVEL SECURITY;
ALTER TABLE attachments FORCE ROW LEVEL SECURITY;

ALTER TABLE notebooks ENABLE ROW LEVEL SECURITY;
ALTER TABLE notebooks FORCE ROW LEVEL SECURITY;

ALTER TABLE notebook_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE notebook_entries FORCE ROW LEVEL SECURITY;

ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE events FORCE ROW LEVEL SECURITY;

-- Helper expression: current request WAX ID from session GUC.
-- empty string / missing => no rows match (fail closed).

CREATE OR REPLACE FUNCTION waxprep_current_wax_id() RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.current_wax_id', true), '')::uuid
$$;

-- Policies: only the current WAX ID's rows.
DROP POLICY IF EXISTS students_isolation ON students;
CREATE POLICY students_isolation ON students
    FOR ALL
    USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

DROP POLICY IF EXISTS channel_identities_isolation ON channel_identities;
CREATE POLICY channel_identities_isolation ON channel_identities
    FOR ALL
    USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

DROP POLICY IF EXISTS conversations_isolation ON conversations;
CREATE POLICY conversations_isolation ON conversations
    FOR ALL
    USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

DROP POLICY IF EXISTS messages_isolation ON messages;
CREATE POLICY messages_isolation ON messages
    FOR ALL
    USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

DROP POLICY IF EXISTS attachments_isolation ON attachments;
CREATE POLICY attachments_isolation ON attachments
    FOR ALL
    USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

DROP POLICY IF EXISTS notebooks_isolation ON notebooks;
CREATE POLICY notebooks_isolation ON notebooks
    FOR ALL
    USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

DROP POLICY IF EXISTS notebook_entries_isolation ON notebook_entries;
CREATE POLICY notebook_entries_isolation ON notebook_entries
    FOR ALL
    USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

DROP POLICY IF EXISTS events_isolation ON events;
CREATE POLICY events_isolation ON events
    FOR ALL
    USING (wax_id IS NOT DISTINCT FROM waxprep_current_wax_id())
    WITH CHECK (wax_id IS NOT DISTINCT FROM waxprep_current_wax_id());


-- Channel identity lookup by external id (bootstrap path; not student-scoped query).
CREATE OR REPLACE FUNCTION waxprep_lookup_channel(
    p_channel TEXT,
    p_external_id TEXT
) RETURNS TABLE (
    id UUID,
    wax_id UUID,
    channel TEXT,
    external_id TEXT,
    created_at TIMESTAMPTZ
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT id, wax_id, channel, external_id, created_at
    FROM channel_identities
    WHERE channel = p_channel AND external_id = p_external_id
$$;

REVOKE ALL ON FUNCTION waxprep_lookup_channel(TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION waxprep_lookup_channel(TEXT, TEXT) TO waxprep_app;
