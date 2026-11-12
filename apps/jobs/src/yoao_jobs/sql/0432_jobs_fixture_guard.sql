-- Keep fixture-owned Jobs anchors until their physical work is proved retired.
CREATE FUNCTION aap_jobs_v1.guard_fixture_project_jobs(p_project_ids uuid[])
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_guard jsonb;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Jobs fixture cleanup requires a test database';
    END IF;
    IF p_project_ids IS NULL OR pg_catalog.array_position(p_project_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Jobs fixture cleanup scope';
    END IF;
    v_guard := aap_workspace_fixture_v1.guard_project_cleanup(p_project_ids);
    IF v_guard IS NULL OR v_guard ->> 'schema' IS DISTINCT FROM 'aap-workspace-e2e-fixtures/v1'
       OR v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR v_guard -> 'reason' IS DISTINCT FROM 'null'::jsonb
       OR (v_guard - ARRAY['schema', 'ok', 'reason']) <> '{}'::jsonb THEN
        IF v_guard ->> 'schema' = 'aap-workspace-e2e-fixtures/v1'
           AND v_guard -> 'ok' = 'false'::jsonb
           AND v_guard ->> 'reason' = 'video_execution_not_retired'
           AND (v_guard - ARRAY['schema', 'ok', 'reason']) = '{}'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'video_execution_not_retired';
        END IF;
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Jobs fixture cleanup guard contract mismatch';
    END IF;
    PERFORM aap_labeling_v1.guard_fixture_project_retry_anchors(p_project_ids);
    IF EXISTS (
        SELECT 1 FROM public.prediction_retry_deliveries AS delivery
        WHERE delivery.project_id=ANY(p_project_ids)
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.async_jobs AS job
        WHERE (job.project_id = ANY(p_project_ids)
               OR job.retained_project_id = ANY(p_project_ids))
          AND NOT EXISTS (
              SELECT 1 FROM yoao_fixture_v1.fixture_presentation_jobs AS proof
              JOIN yoao_fixture_v1.fixture_runs AS run ON run.id = proof.run_id
              WHERE proof.job_id = job.id AND proof.project_id = job.project_id
                AND proof.kind = job.kind AND proof.state = 'admitted' AND run.state = 'sealing'
          )
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_job_execution_unsettled';
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_fixture_project_jobs(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.delete_fixture_presentation_jobs(
    p_run_id uuid, p_project_id uuid, p_job_ids uuid[]
)
RETURNS bigint LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_state text;
    v_count bigint;
    v_failed_ids uuid[];
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='Jobs fixture cleanup requires a test database';
    END IF;
    IF p_run_id IS NULL OR p_project_id IS NULL OR p_job_ids IS NULL
       OR pg_catalog.array_position(p_job_ids, NULL::uuid) IS NOT NULL
       OR (SELECT pg_catalog.count(DISTINCT value) FROM pg_catalog.unnest(p_job_ids) AS ids(value))
          <> pg_catalog.cardinality(p_job_ids) THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid fixture presentation scope';
    END IF;
    BEGIN
        SELECT run.state INTO v_state FROM yoao_fixture_v1.fixture_runs AS run
        WHERE run.id=p_run_id FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
    END;
    IF NOT FOUND OR v_state NOT IN ('open','sealing') OR NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.run_id=p_run_id AND resource.kind='project'
          AND resource.resource_id=p_project_id AND resource.state='published'
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_presentation_scope_changed';
    END IF;
    PERFORM job.id FROM public.async_jobs AS job
    JOIN yoao_fixture_v1.fixture_presentation_jobs AS proof ON proof.job_id=job.id
    WHERE job.id=ANY(p_job_ids) ORDER BY job.id FOR UPDATE OF job, proof;
    SELECT pg_catalog.count(*) INTO v_count FROM public.async_jobs AS job
    JOIN yoao_fixture_v1.fixture_presentation_jobs AS proof ON proof.job_id=job.id
    WHERE job.id=ANY(p_job_ids) AND proof.run_id=p_run_id
      AND proof.project_id=p_project_id AND proof.kind=job.kind
      AND proof.state='admitted' AND job.project_id=p_project_id
      AND job.status <> 'running' AND job.celery_task_id IS NULL AND job.started_at IS NULL
      AND NOT EXISTS (SELECT 1 FROM aap_jobs_v1.dispatch_intents AS intent WHERE intent.job_id=job.id);
    IF v_count <> pg_catalog.cardinality(p_job_ids) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
    END IF;
    SELECT COALESCE(pg_catalog.array_agg(DISTINCT value::uuid), ARRAY[]::uuid[])
    INTO v_failed_ids
    FROM public.async_jobs AS job
    CROSS JOIN LATERAL pg_catalog.jsonb_array_elements_text(
        CASE WHEN pg_catalog.jsonb_typeof(job.result -> 'failed_prediction_ids')='array'
             THEN job.result -> 'failed_prediction_ids' ELSE '[]'::jsonb END
    ) AS failures(value)
    WHERE job.id=ANY(p_job_ids);
    -- The Labeling owner locks failures NOWAIT and rejects even orphaned retry
    -- attempts; the root lock prevents a new fixture Job from being born.
    PERFORM aap_labeling_v1.guard_fixture_presentation_failures(
        p_run_id, p_project_id, v_failed_ids
    );
    IF EXISTS (
        SELECT 1 FROM public.async_jobs AS job
        WHERE (job.project_id=p_project_id OR job.retained_project_id=p_project_id)
          AND NOT EXISTS (
              SELECT 1 FROM yoao_fixture_v1.fixture_presentation_jobs AS proof
              WHERE proof.job_id=job.id AND proof.run_id=p_run_id
                AND proof.project_id=p_project_id AND proof.kind=job.kind
                AND proof.state='admitted'
          )
    ) OR EXISTS (
        SELECT 1 FROM public.prediction_retry_deliveries AS delivery
        WHERE delivery.project_id=p_project_id
           OR delivery.failed_prediction_id=ANY(v_failed_ids)
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
    END IF;
    UPDATE yoao_fixture_v1.fixture_presentation_jobs
    SET state='deleting' WHERE job_id=ANY(p_job_ids);
    DELETE FROM public.async_jobs WHERE id=ANY(p_job_ids);
    GET DIAGNOSTICS v_count = ROW_COUNT;
    IF v_count <> pg_catalog.cardinality(p_job_ids) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_presentation_scope_changed';
    END IF;
    UPDATE yoao_fixture_v1.fixture_presentation_jobs
    SET state='retired' WHERE job_id=ANY(p_job_ids);
    RETURN v_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.delete_fixture_presentation_jobs(uuid,uuid,uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1.delete_fixture_project_jobs(p_project_ids uuid[])
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
    v_row_count bigint := 0;
    v_scope record;
BEGIN
    PERFORM aap_jobs_v1.guard_fixture_project_jobs(p_project_ids);
    FOR v_scope IN
        SELECT proof.run_id, proof.project_id, pg_catalog.array_agg(proof.job_id ORDER BY proof.job_id) AS ids
        FROM yoao_fixture_v1.fixture_presentation_jobs AS proof
        JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=proof.run_id
        WHERE run.state='sealing' AND proof.project_id=ANY(p_project_ids)
          AND proof.state='admitted'
        GROUP BY proof.run_id, proof.project_id ORDER BY proof.run_id, proof.project_id
    LOOP
        v_row_count := v_row_count + aap_jobs_v1.delete_fixture_presentation_jobs(
            v_scope.run_id, v_scope.project_id, v_scope.ids
        );
    END LOOP;
    RETURN v_row_count;
END;
$function$;
