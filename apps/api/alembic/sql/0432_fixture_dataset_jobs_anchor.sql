-- Serialize Dataset Job admission with the exact fixture run seal and retain
-- unknown or owned Job anchors until their physical work is proven retired.
CREATE FUNCTION yoao_fixture_v1.guard_dataset_job_anchor()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_old jsonb;
    v_new jsonb;
    v_run record;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        v_old := aap_jobs_v1.fixture_dataset_job_scope(
            OLD.id, OLD.kind, OLD.payload, OLD.project_id, OLD.user_id
        );
    END IF;
    IF TG_OP <> 'DELETE' THEN
        v_new := aap_jobs_v1.fixture_dataset_job_scope(
            NEW.id, NEW.kind, NEW.payload, NEW.project_id, NEW.user_id
        );
    END IF;
    IF TG_OP = 'DELETE' AND EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_presentation_jobs AS proof
        JOIN yoao_fixture_v1.fixture_runs AS run ON run.id = proof.run_id
        WHERE proof.job_id = OLD.id AND proof.project_id = OLD.project_id
          AND proof.kind = OLD.kind AND proof.state = 'deleting' AND run.state = 'sealing'
    ) THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' AND EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_presentation_jobs AS proof
        JOIN yoao_fixture_v1.fixture_runs AS run ON run.id = proof.run_id
        WHERE proof.job_id = NEW.id AND proof.project_id = NEW.project_id
          AND proof.kind = NEW.kind AND proof.state = 'admitted' AND run.state = 'open'
    ) THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND v_old = v_new THEN
        RETURN NEW;
    END IF;
    IF TG_OP <> 'INSERT' THEN
        IF v_old ->> 'kind' = 'unknown' THEN
            PERFORM 1 FROM yoao_fixture_v1.fixture_runs AS run
            WHERE run.state <> 'retired' AND EXISTS (
                SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
                WHERE resource.run_id = run.id AND resource.kind = 'dataset'
            );
        ELSIF v_old ->> 'kind' = 'dataset' THEN
            PERFORM 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
            JOIN yoao_fixture_v1.fixture_runs AS run ON run.id = resource.run_id
            WHERE resource.kind = 'dataset' AND run.state <> 'retired'
              AND resource.resource_id::text = v_old ->> 'dataset_id';
        END IF;
        IF v_old ->> 'kind' IN ('unknown', 'dataset') AND FOUND THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_dataset_job_unsettled';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    IF v_new ->> 'kind' = 'dataset' THEN
        FOR v_run IN
            SELECT run.state FROM yoao_fixture_v1.fixture_runs AS run
            JOIN yoao_fixture_v1.fixture_run_resources AS resource ON resource.run_id = run.id
            WHERE resource.kind = 'dataset'
              AND resource.resource_id::text = v_new ->> 'dataset_id'
            FOR SHARE OF run
        LOOP
            IF v_run.state <> 'open' THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_run_closed';
            END IF;
        END LOOP;
    ELSIF v_new ->> 'kind' = 'unknown' THEN
        FOR v_run IN
            SELECT run.state FROM yoao_fixture_v1.fixture_runs AS run
            WHERE run.state <> 'retired' AND EXISTS (
                SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
                WHERE resource.run_id = run.id AND resource.kind = 'dataset'
            )
            FOR SHARE OF run
        LOOP
            IF v_run.state <> 'open' THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_dataset_job_scope_unknown';
            END IF;
        END LOOP;
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_fixture_v1.guard_dataset_job_anchor() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_dataset_job_anchor_guard
BEFORE INSERT OR UPDATE OF id, kind, project_id, user_id, payload OR DELETE ON public.async_jobs
FOR EACH ROW EXECUTE FUNCTION yoao_fixture_v1.guard_dataset_job_anchor();
