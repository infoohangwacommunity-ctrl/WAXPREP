-- FORCE ROW LEVEL SECURITY applies even to the table owner. A SECURITY
-- DEFINER function only bypasses RLS if its owner has BYPASSRLS, or if
-- FORCE is turned off (ENABLE RLS can remain so waxprep_app is still
-- isolated). SET row_security=off alone is NOT enough without BYPASSRLS.
--
-- Symptom without this fix:
--   every Telegram webhook re-INSERT into channel_identities → UniqueViolation → HTTP 500
--   and migration 015 previously failed with InsufficientPrivilege.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'waxprep_bypass') THEN
    CREATE ROLE waxprep_bypass NOLOGIN NOSUPERUSER BYPASSRLS;
  ELSE
    ALTER ROLE waxprep_bypass WITH BYPASSRLS;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO waxprep_bypass;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO waxprep_bypass;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO waxprep_bypass;

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

ALTER FUNCTION waxprep_lookup_channel(TEXT, TEXT) OWNER TO waxprep_bypass;
REVOKE ALL ON FUNCTION waxprep_lookup_channel(TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION waxprep_lookup_channel(TEXT, TEXT) TO waxprep_app;
GRANT EXECUTE ON FUNCTION waxprep_lookup_channel(TEXT, TEXT) TO CURRENT_USER;

CREATE OR REPLACE FUNCTION waxprep_wax_ids_with_pending_deletions(
    p_limit INTEGER
) RETURNS TABLE (wax_id UUID)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT d.wax_id
    FROM public.deletion_requests AS d
    WHERE d.status = 'pending'
      AND d.wax_id IS NOT NULL
    GROUP BY d.wax_id
    ORDER BY MIN(d.requested_at)
    LIMIT GREATEST(COALESCE(p_limit, 0), 0)
$$;

ALTER FUNCTION waxprep_wax_ids_with_pending_deletions(INTEGER) OWNER TO waxprep_bypass;
REVOKE ALL ON FUNCTION waxprep_wax_ids_with_pending_deletions(INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION waxprep_wax_ids_with_pending_deletions(INTEGER) TO waxprep_app;
GRANT EXECUTE ON FUNCTION waxprep_wax_ids_with_pending_deletions(INTEGER) TO CURRENT_USER;
