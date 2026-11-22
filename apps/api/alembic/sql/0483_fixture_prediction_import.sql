ALTER TABLE yoao_jobs_v1.fixture_annotation_imports
    ADD COLUMN kind text NOT NULL DEFAULT 'annotations_import'
        CHECK (kind IN ('annotations_import', 'predictions_import')),
    ADD COLUMN running_at timestamptz,
    ADD COLUMN result jsonb;
-- aap-statement-break
CREATE FUNCTION yoao_jobs_v1.observe_fixture_prediction_import()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run_id uuid;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND
        (NEW.kind, NEW.project_id, NEW.user_id, NEW.status, NEW.started_at, NEW.completed_at,
         NEW.result, NEW.error_message, NEW.retained_project_id, NEW.celery_task_id, NEW.execution_snapshot_id)
        IS NOT DISTINCT FROM
        (OLD.kind, OLD.project_id, OLD.user_id, OLD.status, OLD.started_at, OLD.completed_at,
         OLD.result, OLD.error_message, OLD.retained_project_id, OLD.celery_task_id, OLD.execution_snapshot_id) THEN
        RETURN NEW;
    END IF;
    IF NEW.project_id IS NOT NULL AND NEW.user_id IS NOT NULL AND NEW.retained_project_id IS NULL
       AND NEW.celery_task_id IS NULL AND NEW.execution_snapshot_id IS NULL AND NEW.error_message IS NULL
       AND NOT EXISTS (SELECT FROM yoao_jobs_v1.dispatch_intents WHERE job_id = NEW.id) THEN
        SELECT project.run_id INTO v_run_id
        FROM yoao_fixture_v1.fixture_run_resources AS project
        JOIN yoao_fixture_v1.fixture_run_resources AS actor
            ON actor.run_id = project.run_id AND actor.kind = 'user'
            AND actor.resource_id = NEW.user_id AND actor.state = 'published'
        JOIN yoao_fixture_v1.fixture_runs AS run ON run.id = project.run_id
        JOIN yoao_fixture_v1.fixture_runs AS root ON root.id = COALESCE(run.parent_run_id, run.id)
        WHERE project.kind = 'project' AND project.resource_id = NEW.project_id
            AND project.state = 'published' AND run.state = 'open' AND root.state = 'open'
            AND root.parent_run_id IS NULL
        FOR SHARE OF root, run, project, actor;
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF v_run_id IS NOT NULL AND NEW.status = 'pending'
           AND NEW.started_at IS NULL AND NEW.completed_at IS NULL THEN
            INSERT INTO yoao_jobs_v1.fixture_annotation_imports (job_id, run_id, project_id, actor_id, kind)
            VALUES (NEW.id, v_run_id, NEW.project_id, NEW.user_id, NEW.kind);
        END IF;
        RETURN NEW;
    END IF;

    IF v_run_id IS NOT NULL AND OLD.kind = NEW.kind
       AND OLD.project_id = NEW.project_id AND OLD.user_id = NEW.user_id THEN
        IF OLD.status = 'pending' AND NEW.status = 'running' AND OLD.started_at IS NULL
           AND NEW.started_at IS NOT NULL AND OLD.completed_at IS NULL AND NEW.completed_at IS NULL THEN
            UPDATE yoao_jobs_v1.fixture_annotation_imports AS proof
            SET running_at = NEW.started_at
            WHERE proof.job_id = NEW.id AND proof.kind = NEW.kind AND proof.run_id = v_run_id
                AND proof.project_id = NEW.project_id AND proof.actor_id = NEW.user_id
                AND proof.running_at IS NULL;
            RETURN NEW;
        ELSIF OLD.status = 'running' AND NEW.status = 'completed' AND NEW.started_at = OLD.started_at
           AND NEW.completed_at >= NEW.started_at AND OLD.completed_at IS NULL
           AND pg_catalog.jsonb_typeof(NEW.result->'imported') = 'number'
           AND pg_catalog.jsonb_typeof(NEW.result->'skipped') = 'number'
           AND pg_catalog.jsonb_typeof(NEW.result->'error_count') = 'number' THEN
            UPDATE yoao_jobs_v1.fixture_annotation_imports AS proof
            SET started_at = NEW.started_at, completed_at = NEW.completed_at, result = NEW.result
            WHERE proof.job_id = NEW.id AND proof.kind = NEW.kind AND proof.run_id = v_run_id
                AND proof.project_id = NEW.project_id AND proof.actor_id = NEW.user_id
                AND proof.running_at = NEW.started_at AND proof.completed_at IS NULL
                AND EXISTS (
                    SELECT FROM public.audit_logs AS entry
                    JOIN public.async_jobs AS job ON job.id = NEW.id AND job.xmin = entry.xmin
                    WHERE entry.action = 'predictions.import' AND entry.target_type = 'project'
                        AND entry.target_id = NEW.project_id::text AND entry.actor_id = NEW.user_id
                        AND entry.method = 'POST' AND entry.status_code = 200
                        AND entry.detail_json @> pg_catalog.jsonb_build_object('job_id', NEW.id)
                        AND NEW.result = pg_catalog.jsonb_build_object(
                            'imported', entry.detail_json->'imported',
                            'skipped', entry.detail_json->'skipped',
                            'error_count', entry.detail_json->'error_count'
                        )
                );
            IF FOUND THEN RETURN NEW; END IF;
        END IF;
    END IF;

    DELETE FROM yoao_jobs_v1.fixture_annotation_imports AS proof
    WHERE proof.job_id = NEW.id AND proof.kind = NEW.kind;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_jobs_v1.observe_fixture_prediction_import() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_prediction_import_owner
AFTER INSERT OR UPDATE OF kind, project_id, user_id, status, started_at, completed_at,
    result, error_message, retained_project_id, celery_task_id, execution_snapshot_id ON public.async_jobs
FOR EACH ROW WHEN (NEW.kind = 'predictions_import')
EXECUTE FUNCTION yoao_jobs_v1.observe_fixture_prediction_import();
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_jobs_v1.fixture_real_job_retired(
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
        RETURN yoao_jobs_v1.fixture_prediction_retired(p_run_id,p_project_id,p_job_id) IS TRUE;
    ELSIF v_kind='export' THEN
        RETURN public.fixture_export_execution_retired(p_run_id,p_project_id,p_job_id) IS TRUE
           AND public.fixture_export_retired(p_run_id,p_project_id,p_job_id) IS TRUE;
    ELSIF v_kind IN ('annotations_import','predictions_import') THEN
        RETURN EXISTS (
            SELECT FROM yoao_jobs_v1.fixture_annotation_imports AS proof
            JOIN public.async_jobs AS job ON job.id=proof.job_id AND job.user_id=proof.actor_id
            JOIN yoao_fixture_v1.fixture_run_resources AS actor
                ON actor.run_id=proof.run_id AND actor.kind='user'
                AND actor.resource_id=proof.actor_id AND actor.state='published'
            WHERE proof.job_id=p_job_id AND proof.run_id=p_run_id AND proof.project_id=p_project_id
                AND proof.kind=v_kind AND job.status='completed' AND job.started_at=proof.started_at
                AND job.completed_at=proof.completed_at AND job.retained_project_id IS NULL
                AND job.celery_task_id IS NULL AND job.execution_snapshot_id IS NULL
                AND (v_kind='annotations_import' OR (job.result=proof.result AND job.error_message IS NULL))
                AND NOT EXISTS (SELECT FROM yoao_jobs_v1.dispatch_intents WHERE job_id=p_job_id)
        );
    END IF;
    RETURN false;
END;
$function$;
