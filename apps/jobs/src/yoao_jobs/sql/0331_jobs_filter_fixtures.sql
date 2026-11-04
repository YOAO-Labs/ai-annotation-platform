CREATE FUNCTION aap_jobs_v1.set_filter_fixture_job_outcomes(
    p_completed_job_id uuid, p_failed_job_id uuid
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_id uuid;
    v_count bigint;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE = '42501',
            MESSAGE = 'Jobs fixture initialization requires a test database';
    END IF;
    IF p_completed_job_id IS NULL OR p_failed_job_id IS NULL
       OR p_completed_job_id = p_failed_job_id THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Jobs filter fixture IDs';
    END IF;
    -- Preserve the mapper's primary-key order and original UPDATE OF masks.
    FOR v_id IN
        SELECT item.id FROM pg_catalog.unnest(ARRAY[p_completed_job_id, p_failed_job_id]) AS item(id)
        ORDER BY item.id
    LOOP
        IF v_id = p_completed_job_id THEN
            UPDATE public.async_jobs
            SET status = 'completed', progress_pct = 100,
                result = '{"success_count":2}'::jsonb, updated_at = pg_catalog.now()
            WHERE id = v_id;
        ELSE
            UPDATE public.async_jobs
            SET status = 'failed', progress_pct = 40,
                error_message = 'fixture failure', updated_at = pg_catalog.now()
            WHERE id = v_id;
        END IF;
        GET DIAGNOSTICS v_count = ROW_COUNT;
        IF v_count <> 1 THEN
            RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'Jobs filter fixture row not found';
        END IF;
    END LOOP;
    RETURN 2::bigint;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.set_filter_fixture_job_outcomes(uuid,uuid) FROM PUBLIC;
