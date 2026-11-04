CREATE FUNCTION yoao_fixture_v1.guard_jobs_anchor()
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
REVOKE ALL ON FUNCTION yoao_fixture_v1.guard_jobs_anchor() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_jobs_anchor_guard
BEFORE INSERT OR UPDATE OF project_id OR DELETE ON public.async_jobs
FOR EACH ROW EXECUTE FUNCTION yoao_fixture_v1.guard_jobs_anchor();
