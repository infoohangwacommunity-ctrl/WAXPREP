-- Build 2.5 — Workspace and Artifact substrate.
-- Append-only: never edit 001/002; future changes are 004+.

-- Composite uniqueness for (wax_id, id) FKs from child tables.
ALTER TABLE workspaces DROP CONSTRAINT IF EXISTS workspaces_wax_id_id_key;
-- workspaces may not exist yet:

CREATE TABLE IF NOT EXISTS workspaces (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL UNIQUE REFERENCES students (wax_id),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

-- Ensure composite unique for FK from artifacts
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'workspaces_wax_id_id_key'
    ) THEN
        ALTER TABLE workspaces ADD CONSTRAINT workspaces_wax_id_id_key UNIQUE (wax_id, id);
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS artifacts (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    workspace_id UUID NOT NULL REFERENCES workspaces (id),
    status TEXT NOT NULL DEFAULT 'active',
    current_version_id UUID,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT artifacts_status_check CHECK (
        status IN ('active', 'superseded', 'pending_deletion', 'deleted')
    ),
    CONSTRAINT artifacts_workspace_owner_check
        FOREIGN KEY (wax_id, workspace_id)
        REFERENCES workspaces (wax_id, id)
);

CREATE INDEX IF NOT EXISTS artifacts_workspace_idx
    ON artifacts (wax_id, workspace_id);

CREATE INDEX IF NOT EXISTS artifacts_status_idx
    ON artifacts (wax_id, status);

-- Composite unique for version FK
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'artifacts_wax_id_id_key'
    ) THEN
        ALTER TABLE artifacts ADD CONSTRAINT artifacts_wax_id_id_key UNIQUE (wax_id, id);
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS artifact_versions (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    artifact_id UUID NOT NULL REFERENCES artifacts (id),
    version_number INTEGER NOT NULL,
    storage_ref TEXT NOT NULL,
    original_filename TEXT,
    mime_type TEXT,
    size_bytes BIGINT,
    checksum_sha256 TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT artifact_versions_number_check CHECK (version_number >= 1),
    CONSTRAINT artifact_versions_artifact_owner_check
        FOREIGN KEY (wax_id, artifact_id)
        REFERENCES artifacts (wax_id, id),
    CONSTRAINT artifact_versions_unique_number
        UNIQUE (artifact_id, version_number)
);

CREATE INDEX IF NOT EXISTS artifact_versions_artifact_idx
    ON artifact_versions (wax_id, artifact_id);

CREATE TABLE IF NOT EXISTS artifact_references (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    artifact_id UUID NOT NULL REFERENCES artifacts (id),
    reference_type TEXT NOT NULL,
    reference_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT artifact_references_artifact_owner_check
        FOREIGN KEY (wax_id, artifact_id)
        REFERENCES artifacts (wax_id, id),
    CONSTRAINT artifact_references_unique
        UNIQUE (artifact_id, reference_type, reference_id)
);

CREATE INDEX IF NOT EXISTS artifact_references_artifact_idx
    ON artifact_references (wax_id, artifact_id);

-- Optional FK from artifacts.current_version_id to versions
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'artifacts_current_version_fk'
    ) THEN
        ALTER TABLE artifacts
            ADD CONSTRAINT artifacts_current_version_fk
            FOREIGN KEY (current_version_id)
            REFERENCES artifact_versions (id);
    END IF;
END
$$;

-- RLS for new student-scoped tables (FORCE so owners cannot bypass)
ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspaces FORCE ROW LEVEL SECURITY;
ALTER TABLE artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE artifacts FORCE ROW LEVEL SECURITY;
ALTER TABLE artifact_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE artifact_versions FORCE ROW LEVEL SECURITY;
ALTER TABLE artifact_references ENABLE ROW LEVEL SECURITY;
ALTER TABLE artifact_references FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS workspaces_isolation ON workspaces;
CREATE POLICY workspaces_isolation ON workspaces
    FOR ALL
    USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

DROP POLICY IF EXISTS artifacts_isolation ON artifacts;
CREATE POLICY artifacts_isolation ON artifacts
    FOR ALL
    USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

DROP POLICY IF EXISTS artifact_versions_isolation ON artifact_versions;
CREATE POLICY artifact_versions_isolation ON artifact_versions
    FOR ALL
    USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

DROP POLICY IF EXISTS artifact_references_isolation ON artifact_references;
CREATE POLICY artifact_references_isolation ON artifact_references
    FOR ALL
    USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

-- Grants for app role
GRANT SELECT, INSERT, UPDATE, DELETE ON
    workspaces, artifacts, artifact_versions, artifact_references
    TO waxprep_app;
