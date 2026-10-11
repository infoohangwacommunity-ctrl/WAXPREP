-- Outbound delivery outbox (Part 8 Blocker I — delivery foundation).
--
-- The tutor reply is saved to history_entries first (author_kind='tutor',
-- reply_to_id set to the student entry). This table tracks the channel
-- delivery of that reply. A crash between saving the reply and marking
-- delivered is recoverable: the retry sends the same stored reply
-- without invoking the Teacher again.

CREATE TABLE IF NOT EXISTS outbound_deliveries (
    id UUID PRIMARY KEY,
    wax_id UUID NOT NULL REFERENCES students (wax_id),
    tutor_entry_id UUID NOT NULL,
    channel TEXT,
    channel_ref TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    delivered_at TIMESTAMPTZ,
    run_after TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT outbound_status_check
        CHECK (status IN ('pending', 'in_progress', 'delivered', 'failed'))
);

-- Composite FK: the tutor entry must belong to the same student as the
-- outbound delivery. Blocker F: cross-tenant reference protection.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'outbound_deliveries_tutor_owner_fk'
          AND conrelid = 'outbound_deliveries'::regclass
    ) THEN
        ALTER TABLE outbound_deliveries
            ADD CONSTRAINT outbound_deliveries_tutor_owner_fk
            FOREIGN KEY (wax_id, tutor_entry_id)
            REFERENCES history_entries (wax_id, id)
            DEFERRABLE INITIALLY IMMEDIATE;
    END IF;
END
$$;

-- Unique constraint: at most one outbound delivery per (tutor_entry_id, channel).
-- A duplicate provider delivery of the same student message triggers a
-- duplicate of the stored tutor reply; the outbox must not create a
-- second outbound row for the same (tutor_entry_id, channel) pair.
CREATE UNIQUE INDEX IF NOT EXISTS outbound_deliveries_one_per_channel_idx
    ON outbound_deliveries (wax_id, tutor_entry_id, channel)
    WHERE channel IS NOT NULL;

CREATE INDEX IF NOT EXISTS outbound_deliveries_due_idx
    ON outbound_deliveries (wax_id, status, run_after);

ALTER TABLE outbound_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbound_deliveries FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS outbound_deliveries_isolation ON outbound_deliveries;
CREATE POLICY outbound_deliveries_isolation ON outbound_deliveries
    FOR ALL USING (wax_id = waxprep_current_wax_id())
    WITH CHECK (wax_id = waxprep_current_wax_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON outbound_deliveries TO waxprep_app;
