-- Allow channel identity lookup when app.current_wax_id is unset (login path).
-- FORCE RLS + isolation policy previously hid all rows from lookup, so every
-- Telegram webhook tried INSERT again → unique_violation → HTTP 500.
-- When current_wax_id is set, isolation remains tenant-scoped.

DROP POLICY IF EXISTS channel_identities_isolation ON channel_identities;
CREATE POLICY channel_identities_isolation ON channel_identities
    FOR ALL
    USING (
        nullif(current_setting('app.current_wax_id', true), '') IS NULL
        OR wax_id::text = current_setting('app.current_wax_id', true)
    )
    WITH CHECK (
        wax_id::text = current_setting('app.current_wax_id', true)
    );

-- Avoid casting empty current_wax_id to uuid inside RLS (uuid: "").
DROP POLICY IF EXISTS deletion_requests_isolation ON deletion_requests;
CREATE POLICY deletion_requests_isolation ON deletion_requests
    FOR ALL
    USING (
        nullif(current_setting('app.current_wax_id', true), '') IS NULL
        OR wax_id::text = current_setting('app.current_wax_id', true)
    )
    WITH CHECK (
        wax_id::text = current_setting('app.current_wax_id', true)
    );
