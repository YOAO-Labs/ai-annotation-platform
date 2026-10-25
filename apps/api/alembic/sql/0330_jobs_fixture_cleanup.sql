CREATE FUNCTION aap_jobs_v1.delete_fixture_project_jobs(p_project_ids uuid[])
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
    v_guard jsonb;
    v_row_count bigint;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE = '42501',
            MESSAGE = 'Jobs fixture cleanup requires a test database';
    END IF;
    IF p_project_ids IS NULL OR pg_catalog.array_position(p_project_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Jobs fixture cleanup scope';
    END IF;
    v_guard := aap_workspace_fixture_v1.guard_project_cleanup(p_project_ids);
    IF v_guard IS NULL
       OR v_guard ->> 'schema' IS DISTINCT FROM 'aap-workspace-e2e-fixtures/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Jobs fixture cleanup guard contract mismatch';
    END IF;
    IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        IF v_guard -> 'ok' IS DISTINCT FROM 'false'::jsonb
           OR v_guard ->> 'reason' IS DISTINCT FROM 'video_execution_not_retired'
           OR (v_guard - ARRAY['schema', 'ok', 'reason']) <> '{}'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Jobs fixture cleanup guard contract mismatch';
        END IF;
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'video_execution_not_retired';
    END IF;
    IF v_guard -> 'reason' IS DISTINCT FROM 'null'::jsonb
       OR (v_guard - ARRAY['schema', 'ok', 'reason']) <> '{}'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Jobs fixture cleanup guard contract mismatch';
    END IF;
    DELETE FROM public.async_jobs WHERE project_id = ANY (p_project_ids);
    GET DIAGNOSTICS v_row_count = ROW_COUNT;
    RETURN v_row_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.delete_fixture_project_jobs(uuid[]) FROM PUBLIC;
