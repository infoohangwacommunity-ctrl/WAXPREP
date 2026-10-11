-- At-most-one tutor reply per trigger (Part 8 Blocker D).
--
-- A duplicate student message delivery must resume the saved tutor reply
-- (if one exists) rather than silently declaring the turn finished. The
-- cheapest durable way to enforce "at most one tutor reply for a given
-- trigger entry" is a partial unique index on (wax_id, reply_to_id) for
-- tutor-authored rows.

-- Clean up any pre-existing duplicates so the unique index can be created.
-- In a guarded deployment, this is a no-op because no two tutor rows
-- share the same reply_to_id. If duplicates exist, the cutover must
-- resolve them by hand before this migration is applied.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM (
            SELECT wax_id, reply_to_id
            FROM history_entries
            WHERE author_kind = 'tutor' AND reply_to_id IS NOT NULL
            GROUP BY wax_id, reply_to_id
            HAVING COUNT(*) > 1
        ) dupes
    ) THEN
        RAISE EXCEPTION
            'cannot create unique tutor-reply index: duplicate tutor replies exist; resolve before cutover';
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS history_entries_one_tutor_reply_per_trigger_idx
    ON history_entries (wax_id, reply_to_id)
    WHERE author_kind = 'tutor' AND reply_to_id IS NOT NULL;
