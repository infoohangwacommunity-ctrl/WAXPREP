-- Interactive button/list offer registry for one-shot product rule.
-- Meta does not disable buttons after click; we must enforce one-shot.
-- Opaque button ids only — never educational ontology.

CREATE TABLE IF NOT EXISTS interactive_offers (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    tutor_entry_id UUID,
    provider_outbound_message_id TEXT,
    buttons JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    consumed_button_id TEXT,
    consumed_at TIMESTAMPTZ,
    CONSTRAINT interactive_offers_status_check
        CHECK (status IN ('open', 'consumed', 'expired')),
    CONSTRAINT interactive_offers_wax_id_id_key UNIQUE (wax_id, id)
);

-- Fast lookup by opaque button id inside the buttons JSON array.
CREATE INDEX IF NOT EXISTS interactive_offers_button_id_idx
    ON interactive_offers USING gin ((buttons));

CREATE INDEX IF NOT EXISTS interactive_offers_wax_status_idx
    ON interactive_offers (wax_id, status);

ALTER TABLE interactive_offers ENABLE ROW LEVEL SECURITY;
ALTER TABLE interactive_offers FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS interactive_offers_isolation ON interactive_offers;
CREATE POLICY interactive_offers_isolation ON interactive_offers
    USING (wax_id = current_setting('app.current_wax_id', true)::uuid)
    WITH CHECK (wax_id = current_setting('app.current_wax_id', true)::uuid);
