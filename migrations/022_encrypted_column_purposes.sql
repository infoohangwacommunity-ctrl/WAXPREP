-- Backfill column comments on every existing `*_encrypted` column so
-- `waxprep_rotation_targets()` (defined in migration 018) can discover
-- the cipher purpose automatically — the structural fix to the
-- `security/rotation.py` hardcoded-column-list issue.
--
-- Background:
--   The AES-GCM cipher binds a `purpose` string into the Additional
--   Authenticated Data of every ciphertext (security/cipher.py). Without
--   the right purpose, decryption fails closed. rotation.py therefore
--   needs the (table, column, purpose) triple for every encrypted column.
--
--   Before migration 018, rotation.py had a hardcoded list of these
--   triples. Any new `*_encrypted` column added by a future migration
--   was silently orphaned during key rotation — its rows would never
--   be re-encrypted with the new active key, leaving them readable
--   only via the old key. If the old key was later removed from the
--   ring, those rows became permanently unreadable.
--
--   This migration backfills the column comments on every existing
--   `*_encrypted` column with the exact purpose string that
--   rotation.py + cipher.py expect. From now on, `waxprep_rotation_targets()`
--   (migration 018) returns the full inventory; rotation.py no longer
--   needs a hardcoded list, and future migrations that add an
--   `*_encrypted` column MUST set the same `purpose=...` comment.
--
-- Idempotent: every `COMMENT ON COLUMN` is a replace-if-exists operation
-- by definition, so this migration is safe to re-apply. No data is
-- touched; only column comments (pg_description entries).

COMMENT ON COLUMN history_entries.text_encrypted IS
    'purpose=history.text';
COMMENT ON COLUMN search_embeddings.vector_encrypted IS
    'purpose=embedding.vector';
COMMENT ON COLUMN summaries.text_encrypted IS
    'purpose=summary.text';
COMMENT ON COLUMN provenance_links.quote_encrypted IS
    'purpose=provenance.quote';
COMMENT ON COLUMN artifact_contents.text_encrypted IS
    'purpose=artifact_content.text';
COMMENT ON COLUMN artifact_versions.filename_encrypted IS
    'purpose=artifact.filename';
COMMENT ON COLUMN notebook_entries.payload_encrypted IS
    'purpose=notebook.payload';
COMMENT ON COLUMN knowledge_nodes.node_type_encrypted IS
    'purpose=knowledge.node_type';
COMMENT ON COLUMN knowledge_nodes.label_encrypted IS
    'purpose=knowledge.label';
COMMENT ON COLUMN knowledge_nodes.description_encrypted IS
    'purpose=knowledge.description';
COMMENT ON COLUMN knowledge_nodes.properties_encrypted IS
    'purpose=knowledge.properties';
COMMENT ON COLUMN knowledge_edges.relation_encrypted IS
    'purpose=knowledge.relation';
-- FIX (vision-security Finding 1 + correctness audit): the prior
-- hardcoded list in security/rotation.py declared purpose
-- "knowledge.properties" for knowledge_edges.properties_encrypted.
-- The write path (storage/postgres.py:1827) uses
-- "knowledge.edge_properties" — distinct from knowledge_nodes'
-- "knowledge.properties". AES-GCM AAD binds the purpose, so the
-- prior rotation would have raised DecryptionError on every row of
-- this column. Storing the purpose in the column comment makes the
-- mismatch impossible to ship: the migration declares the exact
-- purpose the write path uses.
COMMENT ON COLUMN knowledge_edges.properties_encrypted IS
    'purpose=knowledge.edge_properties';
COMMENT ON COLUMN context_investigations.input_encrypted IS
    'purpose=investigation.input';
COMMENT ON COLUMN context_investigations.result_encrypted IS
    'purpose=investigation.result';

-- learning_states.payload_encrypted and student_calendar.notes_encrypted
-- were given their purpose=... comments by migrations 018 and 021
-- respectively, so they are already discoverable. We list them here
-- only for documentation completeness — the COMMENT statements are
-- idempotent so re-applying is a no-op.
COMMENT ON COLUMN learning_states.payload_encrypted IS
    'purpose=learning_state.payload';
COMMENT ON COLUMN student_calendar.notes_encrypted IS
    'purpose=student_calendar.notes';
