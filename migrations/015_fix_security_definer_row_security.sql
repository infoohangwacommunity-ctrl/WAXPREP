-- FORCE ROW LEVEL SECURITY applies even to the table owner and to
-- SECURITY DEFINER functions unless row_security is disabled in the
-- function. Without this, waxprep_lookup_channel always returns no rows
-- (current_wax_id is unset at login), so every webhook re-provisions and
-- hits channel_identities_channel_external_id_key → HTTP 500.

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
SET row_security = off
AS $$
    SELECT id, wax_id, channel, external_id, created_at
    FROM channel_identities
    WHERE channel = p_channel AND external_id = p_external_id
$$;

REVOKE ALL ON FUNCTION waxprep_lookup_channel(TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION waxprep_lookup_channel(TEXT, TEXT) TO waxprep_app;

CREATE OR REPLACE FUNCTION waxprep_wax_ids_with_pending_deletions(
    p_limit INTEGER
) RETURNS TABLE (wax_id UUID)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
SET row_security = off
AS $$
    SELECT d.wax_id
    FROM public.deletion_requests AS d
    WHERE d.status = 'pending'
    GROUP BY d.wax_id
    ORDER BY MIN(d.requested_at)
    LIMIT p_limit
$$;

REVOKE ALL ON FUNCTION waxprep_wax_ids_with_pending_deletions(INTEGER)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION waxprep_wax_ids_with_pending_deletions(INTEGER)
    TO waxprep_app;
