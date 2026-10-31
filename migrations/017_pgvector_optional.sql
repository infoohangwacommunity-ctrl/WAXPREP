-- Optional pgvector acceleration for semantic search.
--
-- Background:
--   Migration 005 stores each embedding as `vector_encrypted BYTEA`
--   (AES-GCM ciphertext). Semantic search today paginates through
--   every row for one student, decrypts each blob in Python, and
--   computes cosine similarity in-process. This is correct but
--   O(N) per query in Python — slow once a student has thousands of
--   indexed objects.
--
--   pgvector moves the comparison into the database with the `<=>`
--   cosine-distance operator and an HNSW approximate-nearest-neighbour
--   index. The catch: pgvector is not available on every Postgres
--   hosting tier (notably Render's free tier). The founder's brief is
--   explicit — pgvector must remain OPTIONAL with a Python fallback.
--
-- Design:
--   1. CREATE EXTENSION IF NOT EXISTS vector — wrapped in a DO
--      block with EXCEPTION WHEN OTHERS THEN. If the extension's
--      control file is missing (Render free tier, stock Postgres),
--      the migration succeeds and no vector column is added.
--   2. If the extension loaded, add a nullable `vector_plain vector`
--      column to `search_embeddings`. The column is variable-dim so
--      the migration is independent of the active embedding model.
--      Rows are populated lazily by the storage layer only when
--      `has_pgvector()` is true at write time.
--   3. Create an HNSW index for the cosine-distance operator. Wrapped
--      in its own DO block because variable-dim HNSW is not supported
--      on every pgvector release; the migration succeeds even if the
--      index creation is skipped. The `<=>` operator still works
--      without the index (sequential scan inside the RLS-scoped
--      per-student row set), so semantic search degrades gracefully.
--
-- Security:
--   `search_embeddings` already has FORCE/ENABLE ROW LEVEL SECURITY
--   (migration 005) with the policy `search_embeddings_isolation`
--   that scopes every row by `wax_id = waxprep_current_wax_id()`.
--   The plaintext vector column inherits the same RLS protection —
--   a student can only read their own vectors, regardless of whether
--   they are stored as ciphertext or plaintext. The plaintext vector
--   is a numeric array with no semantic content on its own; only
--   the encrypted source text + a model can recover the original
--   words. (See migration 002 and 005 for the RLS substrate.)
--
-- Append-only and idempotent: every statement uses IF NOT EXISTS or
-- is wrapped in a DO block that swallows "already exists" errors.
-- No existing migration is modified.

-- 1. Load the extension. Swallow failure if the control file is
--    missing (Render free tier, stock Postgres without pgvector
--    installed by the cloud operator).
DO $pgvector_load$
BEGIN
    CREATE EXTENSION IF NOT EXISTS vector;
EXCEPTION
    WHEN OTHERS THEN
        RAISE NOTICE 'pgvector extension not available; semantic search stays on the Python-side cosine fallback. Error: %', SQLERRM;
END
$pgvector_load$;

-- 2. Add the plaintext vector column ONLY if the extension loaded.
--    The check uses pg_extension (not pg_available_extensions) so a
--    loaded-but-uncommitted extension counts as missing — this is
--    what we want: the column is added only when `vector` is usable
--    right now.
DO $pgvector_column$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        ALTER TABLE search_embeddings
            ADD COLUMN IF NOT EXISTS vector_plain vector;
        RAISE NOTICE 'pgvector: search_embeddings.vector_plain column is live';
    ELSE
        RAISE NOTICE 'pgvector: extension missing; vector_plain column not added';
    END IF;
EXCEPTION
    WHEN OTHERS THEN
        RAISE NOTICE 'pgvector: vector_plain column add skipped: %', SQLERRM;
END
$pgvector_column$;

-- 3. Create the HNSW index ONLY if both the extension and the
--    column exist. Variable-dim HNSW is not supported on every
--    pgvector release; if CREATE INDEX fails, the migration still
--    succeeds — `<=>` will do an index-free sequential scan that
--    is still faster than Python-side pagination + decryption.
DO $pgvector_index$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'search_embeddings'
          AND column_name = 'vector_plain'
    ) THEN
        CREATE INDEX IF NOT EXISTS search_embeddings_hnsw_idx
            ON search_embeddings
            USING hnsw (vector_plain vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
        RAISE NOTICE 'pgvector: HNSW index created on search_embeddings.vector_plain';
    ELSE
        RAISE NOTICE 'pgvector: vector_plain column missing; HNSW index not created';
    END IF;
EXCEPTION
    WHEN OTHERS THEN
        RAISE NOTICE 'pgvector: HNSW index creation skipped (falling back to sequential scan): %', SQLERRM;
END
$pgvector_index$;

-- 4. No new RLS policy or GRANT is required:
--    - The `search_embeddings_isolation` policy from migration 005
--      scopes every row by `wax_id = waxprep_current_wax_id()`.
--      Adding a column does not change the policy; the column is
--      visible to the waxprep_app role under the same rule.
--    - Indexes are not subject to GRANT; the existing SELECT/INSERT/
--      UPDATE/DELETE on search_embeddings is sufficient.
