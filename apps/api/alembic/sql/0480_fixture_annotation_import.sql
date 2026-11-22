CREATE TABLE yoao_jobs_v1.fixture_annotation_imports (
    job_id uuid PRIMARY KEY REFERENCES public.async_jobs(id) ON DELETE CASCADE,
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    started_at timestamptz,
    completed_at timestamptz,
    CHECK ((started_at IS NULL AND completed_at IS NULL)
        OR (started_at IS NOT NULL AND completed_at IS NOT NULL AND completed_at >= started_at))
);
-- aap-statement-break
REVOKE ALL ON yoao_jobs_v1.fixture_annotation_imports FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_jobs_v1.observe_fixture_annotation_import()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RETURN NEW;
    END IF;
    IF NEW.project_id IS NULL OR NEW.user_id IS NULL OR NEW.retained_project_id IS NOT NULL
       OR NEW.celery_task_id IS NOT NULL OR NEW.execution_snapshot_id IS NOT NULL THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status = 'pending' AND NEW.started_at IS NULL AND NEW.completed_at IS NULL THEN
            INSERT INTO yoao_jobs_v1.fixture_annotation_imports (job_id, run_id, project_id, actor_id)
            SELECT NEW.id, project.run_id, project.resource_id, actor.resource_id
            FROM yoao_fixture_v1.fixture_run_resources AS project
            JOIN yoao_fixture_v1.fixture_run_resources AS actor
                ON actor.run_id = project.run_id AND actor.kind = 'user'
                AND actor.resource_id = NEW.user_id AND actor.state = 'published'
            JOIN yoao_fixture_v1.fixture_runs AS run ON run.id = project.run_id
            JOIN yoao_fixture_v1.fixture_runs AS root ON root.id = COALESCE(run.parent_run_id, run.id)
            WHERE project.kind = 'project' AND project.resource_id = NEW.project_id
                AND project.state = 'published' AND run.state = 'open' AND root.state = 'open';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status = 'running' AND NEW.status = 'completed'
       AND NEW.started_at IS NOT NULL AND NEW.started_at = OLD.started_at
       AND NEW.completed_at >= NEW.started_at AND OLD.completed_at IS NULL
       AND NEW.result ?& ARRAY['imported', 'skipped', 'error_count']
       AND NOT EXISTS (SELECT FROM yoao_jobs_v1.dispatch_intents WHERE job_id = NEW.id) THEN
        UPDATE yoao_jobs_v1.fixture_annotation_imports AS proof
        SET started_at = NEW.started_at, completed_at = NEW.completed_at
        WHERE proof.job_id = NEW.id AND proof.project_id = NEW.project_id AND proof.actor_id = NEW.user_id
          AND proof.completed_at IS NULL
          AND EXISTS (
              SELECT FROM public.audit_logs AS entry
              JOIN public.async_jobs AS job ON job.id = NEW.id AND job.xmin = entry.xmin
              WHERE entry.action = 'annotation.import' AND entry.target_type = 'project'
                AND entry.target_id = NEW.project_id::text AND entry.actor_id = NEW.user_id
                AND entry.method = 'POST' AND entry.status_code = 200
                AND entry.detail_json @> (NEW.result || pg_catalog.jsonb_build_object('job_id', NEW.id))
          );
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_jobs_v1.observe_fixture_annotation_import() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_annotation_import_owner
AFTER INSERT OR UPDATE OF status ON public.async_jobs
FOR EACH ROW WHEN (NEW.kind = 'annotations_import')
EXECUTE FUNCTION yoao_jobs_v1.observe_fixture_annotation_import();
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
    ELSIF v_kind='annotations_import' THEN
        RETURN EXISTS (
            SELECT FROM yoao_jobs_v1.fixture_annotation_imports AS proof
            JOIN public.async_jobs AS job ON job.id=proof.job_id AND job.user_id=proof.actor_id
            JOIN yoao_fixture_v1.fixture_run_resources AS actor
                ON actor.run_id=proof.run_id AND actor.kind='user'
                AND actor.resource_id=proof.actor_id AND actor.state='published'
            WHERE proof.job_id=p_job_id AND proof.run_id=p_run_id AND proof.project_id=p_project_id
                AND job.status='completed' AND job.started_at=proof.started_at
                AND job.completed_at=proof.completed_at AND job.retained_project_id IS NULL
                AND job.celery_task_id IS NULL AND job.execution_snapshot_id IS NULL
                AND NOT EXISTS (SELECT FROM yoao_jobs_v1.dispatch_intents WHERE job_id=p_job_id)
        );
    END IF;
    RETURN false;
END;
$function$;
