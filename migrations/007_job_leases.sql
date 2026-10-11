-- Job leases for concurrent worker safety (Part 8 Blocker A).
ALTER TABLE derivation_jobs
    ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ;

ALTER TABLE derivation_jobs
    DROP CONSTRAINT IF EXISTS derivation_jobs_status_check;

ALTER TABLE derivation_jobs
    ADD CONSTRAINT derivation_jobs_status_check
    CHECK (status IN ('pending', 'running', 'done', 'failed'));

CREATE OR REPLACE FUNCTION waxprep_students_with_due_jobs(
    p_now TIMESTAMPTZ,
    p_limit INTEGER
) RETURNS TABLE (wax_id UUID)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT j.wax_id
    FROM public.derivation_jobs AS j
    WHERE (j.status = 'pending' AND j.run_after <= p_now)
       OR (j.status = 'running' AND j.lease_until IS NOT NULL AND j.lease_until <= p_now)
    GROUP BY j.wax_id
    ORDER BY MIN(j.run_after)
    LIMIT p_limit
$$;

REVOKE ALL ON FUNCTION waxprep_students_with_due_jobs(TIMESTAMPTZ, INTEGER)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION waxprep_students_with_due_jobs(TIMESTAMPTZ, INTEGER)
    TO waxprep_app;
