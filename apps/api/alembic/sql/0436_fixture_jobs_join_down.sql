DO $block$
BEGIN
    IF EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_export_dispatch_receipts) THEN
        RAISE EXCEPTION 'cannot downgrade while fixture export dispatch receipts exist';
    END IF;
END;
$block$;
-- aap-statement-break
DROP TRIGGER aap_fixture_export_preserve_dispatch ON public.async_jobs;
-- aap-statement-break
DROP FUNCTION aap_jobs_v1.preserve_fixture_export_dispatch();
-- aap-statement-break
DROP TABLE aap_jobs_v1.fixture_export_dispatch_receipts;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1.guard_fixture_project_jobs(p_project_ids uuid[])
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
CREATE OR REPLACE FUNCTION yoao_fixture_v1.guard_jobs_anchor()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run record;
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.kind = 'project' AND resource.resource_id = OLD.project_id;
        IF FOUND AND NOT EXISTS (
            SELECT 1 FROM yoao_fixture_v1.fixture_presentation_jobs AS proof
            JOIN yoao_fixture_v1.fixture_runs AS run ON run.id = proof.run_id
            WHERE proof.job_id = OLD.id AND proof.project_id = OLD.project_id
              AND proof.kind = OLD.kind AND proof.state = 'deleting'
              AND (run.state = 'sealing' OR (
                  run.state = 'open' AND OLD.celery_task_id IS NULL AND OLD.started_at IS NULL
              ))
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_job_execution_unsettled';
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.project_id IS NOT DISTINCT FROM OLD.project_id THEN
            RETURN NEW;
        END IF;
        PERFORM 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.kind = 'project' AND resource.resource_id = OLD.project_id;
        IF FOUND THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_job_execution_unsettled';
        END IF;
    END IF;
    FOR v_run IN
        SELECT run.state FROM yoao_fixture_v1.fixture_runs AS run
        JOIN yoao_fixture_v1.fixture_run_resources AS resource ON resource.run_id = run.id
        WHERE resource.kind = 'project' AND resource.resource_id = NEW.project_id
        FOR SHARE OF run
    LOOP
        IF v_run.state <> 'open' THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_run_closed';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$function$;
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
-- aap-statement-break
DROP FUNCTION aap_jobs_v1.fixture_real_job_retired(uuid,uuid,uuid);
