-- Connection durability hardening (live incident 2026-10-26).
--
-- Symptom in production: the worker crashed with
--   invalid input syntax for type uuid: ""
-- on SELECT wax_id FROM waxprep_wax_ids_with_pending_deletions(...),
-- and every WhatsApp webhook returned 500, so Meta retried and each
-- retry created another orphan student (no channel identity).
--
-- Root cause: the interactive_offers and deletion_requests policies
-- cast current_setting('app.current_wax_id', true) DIRECTLY to uuid.
-- After set_config(..., local => true) ends with its transaction, the
-- session-level value of a custom GUC reads back as the EMPTY STRING,
-- and ''::uuid raises. Every other table uses the NULLIF-safe
-- waxprep_current_wax_id() helper (migration 002); these two did not.
--
-- The crash also proves RLS policies are evaluated INSIDE the
-- SECURITY DEFINER helper functions on this deployment (the tables
-- use FORCE ROW LEVEL SECURITY and the function owner is not a
-- superuser), which additionally means those helpers silently
-- returned ZERO rows whenever the session GUC was unset or '' —
-- including waxprep_lookup_channel, so provisioning could not find
-- existing identities and retried webhooks kept creating duplicate
-- students.
--
-- Fix, three parts:
--   1. Recreate both raw-cast policies with the NULLIF-safe helper.
--   2. Stop forcing RLS on tables read by the SECURITY DEFINER
--      helpers, so the function owner (the role that owns the tables)
--      sees every row again, as in standard RLS deployments. ENABLE
--      stays on: the application role (waxprep_app) remains fully
--      policy-bound.
--   3. Add waxprep_orphan_student_ids(older_than) so application
--      startup can prune students that failed provisioning (created,
--      no channel identity, no data of any kind) — the live incident
--      left three of them behind.

-- 1. NULLIF-safe policies for the two tables that cast raw.
DROP POLICY IF EXISTS interactive_offers_isolation ON interactive_offers;
CREATE POLICY interactive_offers_isolation ON interactive_offers
    FOR ALL
    USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

DROP POLICY IF EXISTS deletion_requests_isolation ON deletion_requests;
CREATE POLICY deletion_requests_isolation ON deletion_requests
    FOR ALL
    USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

-- 2. The table owner must see all rows inside the SECURITY DEFINER
--    helpers (waxprep_lookup_channel, waxprep_students_with_due_jobs,
--    waxprep_wax_ids_with_pending_deletions, and the orphan scan
--    below). FORCE makes even the owner policy-bound, which silently
--    blinds those helpers; dropping FORCE restores the standard
--    behaviour while waxprep_app keeps full RLS protection.
ALTER TABLE students NO FORCE ROW LEVEL SECURITY;
ALTER TABLE channel_identities NO FORCE ROW LEVEL SECURITY;
ALTER TABLE history_entries NO FORCE ROW LEVEL SECURITY;
ALTER TABLE events NO FORCE ROW LEVEL SECURITY;
ALTER TABLE derivation_jobs NO FORCE ROW LEVEL SECURITY;
ALTER TABLE outbound_deliveries NO FORCE ROW LEVEL SECURITY;
ALTER TABLE notebooks NO FORCE ROW LEVEL SECURITY;
ALTER TABLE notebook_entries NO FORCE ROW LEVEL SECURITY;
ALTER TABLE deletion_requests NO FORCE ROW LEVEL SECURITY;
ALTER TABLE interactive_offers NO FORCE ROW LEVEL SECURITY;

-- 3. Orphan student detection for the startup prune. A student is an
--    orphan when it has no channel identity and no data of any kind:
--    it was created by a provisioning attempt whose identity write
--    failed, so nothing can ever reach it again.
CREATE OR REPLACE FUNCTION waxprep_orphan_student_ids(
    p_older_than TIMESTAMPTZ
) RETURNS TABLE (wax_id UUID)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT s.wax_id
    FROM public.students AS s
    WHERE s.created_at < p_older_than
      AND NOT EXISTS (
          SELECT 1 FROM public.channel_identities c WHERE c.wax_id = s.wax_id)
      AND NOT EXISTS (
          SELECT 1 FROM public.history_entries h WHERE h.wax_id = s.wax_id)
      AND NOT EXISTS (
          SELECT 1 FROM public.events e WHERE e.wax_id = s.wax_id)
      AND NOT EXISTS (
          SELECT 1 FROM public.derivation_jobs j WHERE j.wax_id = s.wax_id)
      AND NOT EXISTS (
          SELECT 1 FROM public.outbound_deliveries o WHERE o.wax_id = s.wax_id)
      AND NOT EXISTS (
          SELECT 1 FROM public.notebook_entries n WHERE n.wax_id = s.wax_id)
      AND NOT EXISTS (
          SELECT 1 FROM public.notebooks nb WHERE nb.wax_id = s.wax_id)
      AND NOT EXISTS (
          SELECT 1 FROM public.deletion_requests d WHERE d.wax_id = s.wax_id)
      AND NOT EXISTS (
          SELECT 1 FROM public.interactive_offers io WHERE io.wax_id = s.wax_id)
    ORDER BY s.created_at
$$;

REVOKE ALL ON FUNCTION waxprep_orphan_student_ids(TIMESTAMPTZ) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION waxprep_orphan_student_ids(TIMESTAMPTZ) TO waxprep_app;
