-- Join the existing execution owners only for an exact sealed fixture Job.
CREATE FUNCTION aap_jobs_v1.fixture_real_job_retired(
    p_run_id uuid, p_project_id uuid, p_job_id uuid
)
RETURNS boolean LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_project_id uuid;
    v_kind text;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='Jobs fixture cleanup requires a test database';
    END IF;
    IF p_run_id IS NULL OR p_project_id IS NULL OR p_job_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid fixture Job scope';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_runs AS run
        JOIN yoao_fixture_v1.fixture_run_resources AS resource ON resource.run_id=run.id
        WHERE run.id=p_run_id AND run.state='sealing' AND resource.kind='project'
          AND resource.resource_id=p_project_id AND resource.state='published'
    ) THEN RETURN false; END IF;
    SELECT job.project_id,job.kind INTO v_project_id,v_kind FROM public.async_jobs AS job
    WHERE job.id=p_job_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_project_id IS DISTINCT FROM p_project_id THEN RETURN false; END IF;
    IF v_kind IN ('batch_predict','prediction_retry') THEN
        RETURN aap_jobs_v1.fixture_prediction_retired(p_run_id,p_project_id,p_job_id) IS TRUE;
    ELSIF v_kind='export' THEN
        RETURN public.fixture_export_execution_retired(p_run_id,p_project_id,p_job_id) IS TRUE
           AND public.fixture_export_retired(p_run_id,p_project_id,p_job_id) IS TRUE;
    END IF;
    RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_real_job_retired(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
-- Export retirement fences delayed workers, but the Jobs foreign key would
-- otherwise cascade away the separate sender/dispatch receipt.
CREATE TABLE aap_jobs_v1.fixture_export_dispatch_receipts (
    intent_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    job_id uuid NOT NULL,
    snapshot jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(snapshot)='object'),
    captured_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    CHECK (snapshot->>'intent_id' IS NOT DISTINCT FROM intent_id::text
       AND snapshot->>'job_id' IS NOT DISTINCT FROM job_id::text)
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.fixture_export_dispatch_receipts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.preserve_fixture_export_dispatch()
RETURNS trigger LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_run_id uuid;
BEGIN
    IF OLD.kind <> 'export' THEN RETURN OLD; END IF;
    SELECT resource.run_id INTO v_run_id
    FROM yoao_fixture_v1.fixture_run_resources AS resource
    WHERE resource.kind='project' AND resource.resource_id=OLD.project_id
      AND resource.state='published';
    IF NOT FOUND THEN RETURN OLD; END IF;
    PERFORM intent.intent_id FROM aap_jobs_v1.dispatch_intents AS intent
    WHERE intent.job_id=OLD.id ORDER BY intent.intent_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN OLD; END IF;
    IF aap_jobs_v1.fixture_real_job_retired(v_run_id,OLD.project_id,OLD.id) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
    END IF;
    INSERT INTO aap_jobs_v1.fixture_export_dispatch_receipts(run_id,project_id,job_id,intent_id,snapshot)
    SELECT v_run_id,OLD.project_id,OLD.id,intent.intent_id,pg_catalog.to_jsonb(intent)
    FROM aap_jobs_v1.dispatch_intents AS intent WHERE intent.job_id=OLD.id;
    RETURN OLD;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.preserve_fixture_export_dispatch() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER aap_fixture_export_preserve_dispatch
BEFORE DELETE ON public.async_jobs
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1.preserve_fixture_export_dispatch();
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1.guard_fixture_project_jobs(p_project_ids uuid[])
RETURNS void LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_guard jsonb;
    v_project uuid;
    v_run_id uuid;
    v_parent_id uuid;
    v_root_state text;
    v_run_state text;
    v_tasks uuid[];
    v_run_projects uuid[];
    v_checked_runs uuid[] := ARRAY[]::uuid[];
    v_job record;
    v_presentation yoao_fixture_v1.fixture_presentation_jobs%ROWTYPE;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='Jobs fixture cleanup requires a test database';
    END IF;
    IF p_project_ids IS NULL OR pg_catalog.array_position(p_project_ids,NULL::uuid) IS NOT NULL
       OR (SELECT pg_catalog.count(DISTINCT id) FROM pg_catalog.unnest(p_project_ids) AS items(id))
          <> pg_catalog.cardinality(p_project_ids) THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid Jobs fixture cleanup scope';
    END IF;
    v_guard := aap_workspace_fixture_v1.guard_project_cleanup(p_project_ids);
    IF v_guard IS NULL OR v_guard ->> 'schema' IS DISTINCT FROM 'aap-workspace-e2e-fixtures/v1'
       OR v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR v_guard -> 'reason' IS DISTINCT FROM 'null'::jsonb
       OR (v_guard - ARRAY['schema','ok','reason']) <> '{}'::jsonb THEN
        IF v_guard ->> 'schema' = 'aap-workspace-e2e-fixtures/v1'
           AND v_guard -> 'ok' = 'false'::jsonb
           AND v_guard ->> 'reason' = 'video_execution_not_retired'
           AND (v_guard - ARRAY['schema','ok','reason']) = '{}'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='video_execution_not_retired';
        END IF;
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='Jobs fixture cleanup guard contract mismatch';
    END IF;
    FOR v_project IN SELECT id FROM pg_catalog.unnest(p_project_ids) AS items(id) ORDER BY id LOOP
        SELECT run.id, run.parent_run_id INTO v_run_id, v_parent_id
        FROM yoao_fixture_v1.fixture_runs AS run
        JOIN yoao_fixture_v1.fixture_run_resources AS resource ON resource.run_id=run.id
        WHERE resource.kind='project' AND resource.resource_id=v_project
          AND resource.state='published';
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
        END IF;
        SELECT root.state INTO v_root_state FROM yoao_fixture_v1.fixture_runs AS root
        WHERE root.id=COALESCE(v_parent_id,v_run_id) AND root.parent_run_id IS NULL
        FOR SHARE NOWAIT;
        IF NOT FOUND OR v_root_state NOT IN ('open','sealing') THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
        END IF;
        SELECT run.state INTO v_run_state FROM yoao_fixture_v1.fixture_runs AS run
        JOIN yoao_fixture_v1.fixture_run_resources AS resource ON resource.run_id=run.id
        WHERE run.id=v_run_id AND run.parent_run_id IS NOT DISTINCT FROM v_parent_id
          AND resource.kind='project' AND resource.resource_id=v_project
          AND resource.state='published' FOR UPDATE OF run NOWAIT;
        IF NOT FOUND OR v_run_state <> 'sealing' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
        END IF;
        PERFORM job.id FROM public.async_jobs AS job
        WHERE job.project_id=v_project OR job.retained_project_id=v_project
        ORDER BY job.id FOR UPDATE NOWAIT;
        IF aap_jobs_v1.guard_fixture_prediction_ledgers(v_run_id,v_project) IS DISTINCT FROM TRUE THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
        END IF;
        IF NOT (v_run_id=ANY(v_checked_runs)) THEN
            SELECT COALESCE(pg_catalog.array_agg(resource.resource_id ORDER BY resource.resource_id),
                            ARRAY[]::uuid[]) INTO v_run_projects
            FROM yoao_fixture_v1.fixture_run_resources AS resource
            WHERE resource.run_id=v_run_id AND resource.kind='project' AND resource.state='published';
            IF EXISTS (
                SELECT 1 FROM pg_catalog.unnest(v_run_projects) AS ids(id)
                WHERE NOT (ids.id=ANY(p_project_ids))
            ) THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
            END IF;
            SELECT COALESCE(pg_catalog.array_agg(task.id ORDER BY task.id),ARRAY[]::uuid[])
            INTO v_tasks FROM aap_labeling_v1.task_rows AS task
            WHERE task.project_id=ANY(v_run_projects);
            IF aap_inference_routing_v1.fixture_prediction_tests_retired(
                v_run_id,v_run_projects,v_tasks
            ) IS DISTINCT FROM TRUE THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
            END IF;
            v_checked_runs := pg_catalog.array_append(v_checked_runs,v_run_id);
        END IF;
        IF EXISTS (
            SELECT 1 FROM yoao_fixture_v1.fixture_presentation_jobs AS proof
            WHERE proof.run_id=v_run_id AND proof.project_id=v_project AND proof.state='admitted'
              AND NOT EXISTS (SELECT 1 FROM public.async_jobs AS job
                  WHERE job.id=proof.job_id AND job.project_id=v_project AND job.kind=proof.kind)
        ) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
        END IF;
        FOR v_job IN SELECT job.id,job.project_id,job.kind,job.status,
            job.celery_task_id,job.started_at FROM public.async_jobs AS job
            WHERE job.project_id=v_project OR job.retained_project_id=v_project ORDER BY job.id
        LOOP
            IF v_job.project_id IS DISTINCT FROM v_project THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
            END IF;
            SELECT proof.* INTO v_presentation FROM yoao_fixture_v1.fixture_presentation_jobs AS proof
            WHERE proof.job_id=v_job.id FOR UPDATE NOWAIT;
            IF FOUND THEN
                IF v_presentation.run_id <> v_run_id OR v_presentation.project_id <> v_project
                   OR v_presentation.kind <> v_job.kind OR v_presentation.state <> 'admitted'
                   OR v_job.status='running' OR v_job.celery_task_id IS NOT NULL
                   OR v_job.started_at IS NOT NULL OR EXISTS (
                       SELECT 1 FROM aap_jobs_v1.dispatch_intents AS intent WHERE intent.job_id=v_job.id
                   ) THEN
                    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
                END IF;
            ELSIF aap_jobs_v1.fixture_real_job_retired(v_run_id,v_project,v_job.id) IS DISTINCT FROM TRUE THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
            END IF;
        END LOOP;
    END LOOP;
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_fixture_v1.guard_jobs_anchor()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run record;
    v_root_id uuid;
    v_root_state text;
    v_state text;
    v_parent_id uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        SELECT resource.run_id INTO v_root_id FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.kind='project' AND resource.resource_id=OLD.project_id;
        IF FOUND AND NOT EXISTS (
            SELECT 1 FROM yoao_fixture_v1.fixture_presentation_jobs AS proof
            JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=proof.run_id
            WHERE proof.job_id=OLD.id AND proof.project_id=OLD.project_id
              AND proof.kind=OLD.kind AND proof.state='deleting'
              AND (run.state='sealing' OR (run.state='open'
                   AND OLD.celery_task_id IS NULL AND OLD.started_at IS NULL))
        ) AND aap_jobs_v1.fixture_real_job_retired(v_root_id,OLD.project_id,OLD.id) IS DISTINCT FROM TRUE THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP='UPDATE' THEN
        IF NEW.project_id IS NOT DISTINCT FROM OLD.project_id THEN RETURN NEW; END IF;
        PERFORM 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.kind='project' AND resource.resource_id=OLD.project_id;
        IF FOUND THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
        END IF;
    END IF;
    FOR v_run IN
        SELECT run.id, run.parent_run_id FROM yoao_fixture_v1.fixture_runs AS run
        JOIN yoao_fixture_v1.fixture_run_resources AS resource ON resource.run_id=run.id
        WHERE resource.kind='project' AND resource.resource_id=NEW.project_id
    LOOP
        v_root_id := COALESCE(v_run.parent_run_id,v_run.id);
        SELECT root.state INTO v_root_state FROM yoao_fixture_v1.fixture_runs AS root
        WHERE root.id=v_root_id AND root.parent_run_id IS NULL FOR SHARE;
        IF NOT FOUND OR v_root_state <> 'open' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_run_closed';
        END IF;
        SELECT run.state,run.parent_run_id INTO v_state,v_parent_id
        FROM yoao_fixture_v1.fixture_runs AS run
        JOIN yoao_fixture_v1.fixture_run_resources AS resource ON resource.run_id=run.id
        WHERE run.id=v_run.id AND resource.kind='project' AND resource.resource_id=NEW.project_id
        FOR SHARE OF run;
        IF NOT FOUND OR v_state <> 'open' OR COALESCE(v_parent_id,v_run.id) <> v_root_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_run_closed';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1.delete_fixture_project_jobs(p_project_ids uuid[])
RETURNS bigint LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_count bigint := 0;
    v_deleted bigint;
    v_job record;
    v_presentation boolean;
BEGIN
    PERFORM aap_jobs_v1.guard_fixture_project_jobs(p_project_ids);
    FOR v_job IN
        SELECT job.id,job.project_id FROM public.async_jobs AS job
        WHERE job.project_id=ANY(p_project_ids) ORDER BY job.project_id,job.id
    LOOP
        UPDATE yoao_fixture_v1.fixture_presentation_jobs AS proof SET state='deleting'
        WHERE proof.job_id=v_job.id AND proof.project_id=v_job.project_id AND proof.state='admitted';
        v_presentation := FOUND;
        DELETE FROM public.async_jobs AS job WHERE job.id=v_job.id AND job.project_id=v_job.project_id;
        GET DIAGNOSTICS v_deleted = ROW_COUNT;
        IF v_deleted <> 1 THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
        END IF;
        IF v_presentation THEN
            UPDATE yoao_fixture_v1.fixture_presentation_jobs SET state='retired'
            WHERE job_id=v_job.id AND state='deleting';
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
            END IF;
        END IF;
        v_count := v_count + 1;
    END LOOP;
    RETURN v_count;
END;
$function$;
