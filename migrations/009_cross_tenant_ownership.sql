-- Cross-tenant ownership constraints (Part 8 Blocker F).
--
-- RLS scopes reads by WAX ID, but RLS alone does not stop a row owned by
-- Student A from referencing a row owned by Student B through a foreign
-- key that checks only a globally unique object ID. These composite FKs
-- enforce that every reference points to a row owned by the same student.
--
-- The migrations are idempotent. If a constraint fails to validate on
-- existing data, the cutover must stop and the violating rows must be
-- resolved by hand; do not add the constraint NOT VALID permanently.

-- 1. A history row can reply only to another row from the same student.
ALTER TABLE history_entries
    DROP CONSTRAINT IF EXISTS history_entries_reply_to_id_fkey;
ALTER TABLE history_entries
    ADD CONSTRAINT history_reply_owner_fk
    FOREIGN KEY (wax_id, reply_to_id)
    REFERENCES history_entries (wax_id, id)
    DEFERRABLE INITIALLY IMMEDIATE;

-- 2. Composite unique key on artifact_versions (wax_id, id) so a
-- same-owner FK can be attached to artifact_contents.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'artifact_versions_wax_id_id_key'
          AND conrelid = 'artifact_versions'::regclass
    ) THEN
        ALTER TABLE artifact_versions
            ADD CONSTRAINT artifact_versions_wax_id_id_key UNIQUE (wax_id, id);
    END IF;
END
$$;

-- 3. Artifact contents must belong to the same student as their version.
ALTER TABLE artifact_contents
    DROP CONSTRAINT IF EXISTS artifact_contents_artifact_version_id_fkey;
ALTER TABLE artifact_contents
    DROP CONSTRAINT IF EXISTS artifact_content_version_owner_fk;
ALTER TABLE artifact_contents
    ADD CONSTRAINT artifact_content_version_owner_fk
    FOREIGN KEY (wax_id, artifact_version_id)
    REFERENCES artifact_versions (wax_id, id);

-- 4. Attachments must reference history belonging to the same WAX ID.
ALTER TABLE attachments
    DROP CONSTRAINT IF EXISTS attachments_history_entry_fkey;
ALTER TABLE attachments
    DROP CONSTRAINT IF EXISTS attachments_history_owner_fk;
ALTER TABLE attachments
    ADD CONSTRAINT attachments_history_owner_fk
    FOREIGN KEY (wax_id, history_entry_id)
    REFERENCES history_entries (wax_id, id);

-- 5. Composite unique key on artifact_versions was added above; the
-- current-version pointer on artifacts also needs to be owner-checked.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'artifacts_wax_id_current_version_id_key'
          AND conrelid = 'artifacts'::regclass
    ) THEN
        ALTER TABLE artifacts
            ADD CONSTRAINT artifacts_wax_id_current_version_id_key
            UNIQUE (wax_id, current_version_id);
    END IF;
END
$$;

ALTER TABLE artifacts
    DROP CONSTRAINT IF EXISTS artifacts_current_version_fk;
ALTER TABLE artifacts
    DROP CONSTRAINT IF EXISTS artifacts_current_version_owner_fk;
ALTER TABLE artifacts
    ADD CONSTRAINT artifacts_current_version_owner_fk
    FOREIGN KEY (wax_id, current_version_id)
    REFERENCES artifact_versions (wax_id, id)
    DEFERRABLE INITIALLY IMMEDIATE;

-- 6. Investigation triggers must belong to the same student as the
-- investigation record. Requires a composite unique key on
-- history_entries (wax_id, id), which migration 005 already creates
-- as `history_wax_id_id_key`. We re-check to be safe.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'history_wax_id_id_key'
          AND conrelid = 'history_entries'::regclass
    ) THEN
        ALTER TABLE history_entries
            ADD CONSTRAINT history_wax_id_id_key UNIQUE (wax_id, id);
    END IF;
END
$$;

ALTER TABLE context_investigations
    DROP CONSTRAINT IF EXISTS context_investigations_trigger_entry_id_fkey;
ALTER TABLE context_investigations
    DROP CONSTRAINT IF EXISTS investigation_trigger_owner_fk;
ALTER TABLE context_investigations
    ADD CONSTRAINT investigation_trigger_owner_fk
    FOREIGN KEY (wax_id, trigger_entry_id)
    REFERENCES history_entries (wax_id, id);

-- 7. Notebook entry supersession: ensure the successor is owned by the
-- same student as the predecessor. Requires a composite unique key on
-- notebook_entries (wax_id, id).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'notebook_entries_wax_id_id_key'
          AND conrelid = 'notebook_entries'::regclass
    ) THEN
        ALTER TABLE notebook_entries
            ADD CONSTRAINT notebook_entries_wax_id_id_key UNIQUE (wax_id, id);
    END IF;
END
$$;

ALTER TABLE notebook_entries
    DROP CONSTRAINT IF EXISTS notebook_entries_superseded_by_id_fkey;
ALTER TABLE notebook_entries
    DROP CONSTRAINT IF EXISTS notebook_entries_supersession_owner_fk;
ALTER TABLE notebook_entries
    ADD CONSTRAINT notebook_entries_supersession_owner_fk
    FOREIGN KEY (wax_id, superseded_by_id)
    REFERENCES notebook_entries (wax_id, id)
    DEFERRABLE INITIALLY IMMEDIATE;

-- 8. Knowledge edges: source and target nodes must be owned by the same
-- student as the edge. Migration 006 already creates these composite FKs
-- via `knowledge_edges_source_owner` and `knowledge_edges_target_owner`.
-- We re-affirm them here (idempotent).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'knowledge_edges_source_owner'
          AND conrelid = 'knowledge_edges'::regclass
    ) THEN
        ALTER TABLE knowledge_edges
            ADD CONSTRAINT knowledge_edges_source_owner
            FOREIGN KEY (wax_id, source_node_id)
            REFERENCES knowledge_nodes (wax_id, id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'knowledge_edges_target_owner'
          AND conrelid = 'knowledge_edges'::regclass
    ) THEN
        ALTER TABLE knowledge_edges
            ADD CONSTRAINT knowledge_edges_target_owner
            FOREIGN KEY (wax_id, target_node_id)
            REFERENCES knowledge_nodes (wax_id, id);
    END IF;
END
$$;
