-- Persist the outbound provider message ID so delivery-status callbacks
-- can be matched to the correct outbound record (Part 8 Blocker I).
--
-- WhatsApp returns a message ID when it accepts an outbound message.
-- Without storing it, WAXPREP cannot associate a delivery receipt with
-- the outbound record that produced it.

ALTER TABLE outbound_deliveries
    ADD COLUMN IF NOT EXISTS provider_message_id TEXT;

CREATE INDEX IF NOT EXISTS outbound_deliveries_provider_msg_idx
    ON outbound_deliveries (wax_id, provider_message_id)
    WHERE provider_message_id IS NOT NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON outbound_deliveries TO waxprep_app;
