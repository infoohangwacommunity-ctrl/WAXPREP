-- Ownership lookup for polymorphic provenance validation (Part 8 Blocker F).
--
-- Polymorphic provenance_links reference (object_type, object_id) pairs that
-- cannot be protected by an ordinary foreign key. The application must
-- verify that the source and derived objects are owned by the same student
-- before inserting a link. RLS hides rows owned by other students, so the
-- check would always pass ("row not visible, no violation detected") —
-- defeating the protection.
--
-- This SECURITY DEFINER function is owned by the migrator (superuser in
-- tests; a role with BYPASSRLS in production per spec 8.2-A) and returns
-- the actual owner of an object regardless of the caller's current_wax_id.
-- The application calls this function and rejects the link if the returned
-- owner differs from the batch's wax_id.

CREATE OR REPLACE FUNCTION waxprep_object_owner(
    p_object_type TEXT,
    p_object_id UUID
) RETURNS UUID
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT
        CASE
            WHEN p_object_type = 'history' THEN
                (SELECT wax_id FROM public.history_entries WHERE id = p_object_id)
            WHEN p_object_type = 'summary' THEN
                (SELECT wax_id FROM public.summaries WHERE id = p_object_id)
            WHEN p_object_type = 'notebook' THEN
                (SELECT wax_id FROM public.notebook_entries WHERE id = p_object_id)
            WHEN p_object_type = 'knowledge_node' THEN
                (SELECT wax_id FROM public.knowledge_nodes WHERE id = p_object_id)
            WHEN p_object_type = 'knowledge_edge' THEN
                (SELECT wax_id FROM public.knowledge_edges WHERE id = p_object_id)
            WHEN p_object_type = 'artifact_version' THEN
                (SELECT wax_id FROM public.artifact_versions WHERE id = p_object_id)
            WHEN p_object_type = 'artifact_content' THEN
                (SELECT wax_id FROM public.artifact_contents WHERE id = p_object_id)
            ELSE NULL
        END
$$;

REVOKE ALL ON FUNCTION waxprep_object_owner(TEXT, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION waxprep_object_owner(TEXT, UUID) TO waxprep_app;
