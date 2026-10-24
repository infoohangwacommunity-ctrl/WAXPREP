-- Table owners are subject to RLS when FORCE is on, so SECURITY DEFINER
-- owned by the migration user still cannot see rows without current_wax_id.
-- Keep RLS enabled for waxprep_app; drop FORCE so the owner (and
-- RESET ROLE lookup paths) can resolve channel → wax_id at login.

ALTER TABLE channel_identities NO FORCE ROW LEVEL SECURITY;
ALTER TABLE students NO FORCE ROW LEVEL SECURITY;

-- Keep policies for waxprep_app; owner bypasses without FORCE.

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
