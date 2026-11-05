-- This owner evidence intentionally has no foreign keys to business Jobs or
-- fixture roots: an old broker message must never acquire a reused identity.
CREATE TABLE yoao_fixture_v1.fixture_export_executions (
    job_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    state text NOT NULL CHECK (state IN ('pending', 'running', 'settled', 'retired')),
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    token uuid,
    backend_pid integer,
    CHECK ((state = 'running') = (token IS NOT NULL AND backend_pid IS NOT NULL)),
    CHECK (state = 'running' OR (token IS NULL AND backend_pid IS NULL))
);
-- aap-statement-break
REVOKE ALL ON yoao_fixture_v1.fixture_export_executions FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION public.guard_fixture_export_execution()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_OP = 'DELETE' OR NEW.job_id IS DISTINCT FROM OLD.job_id
       OR NEW.run_id IS DISTINCT FROM OLD.run_id
       OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR OLD.state = 'retired' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture export execution evidence is immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.guard_fixture_export_execution() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_fixture_export_execution_immutable
BEFORE UPDATE OR DELETE ON yoao_fixture_v1.fixture_export_executions
FOR EACH ROW EXECUTE FUNCTION public.guard_fixture_export_execution();
-- aap-statement-break
CREATE FUNCTION public.record_fixture_export_execution()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NEW.fixture_run_id IS NOT NULL THEN
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended('aap-export:job:' || NEW.job_id::text, 0)
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '55P03', MESSAGE = 'fixture export execution is busy';
        END IF;
        INSERT INTO yoao_fixture_v1.fixture_export_executions (job_id, run_id, project_id, state)
        VALUES (NEW.job_id, NEW.fixture_run_id, NEW.project_id, 'pending')
        ON CONFLICT (job_id) DO NOTHING;
        PERFORM 1 FROM yoao_fixture_v1.fixture_export_executions
        WHERE job_id = NEW.job_id AND run_id = NEW.fixture_run_id
          AND project_id = NEW.project_id AND state IN ('pending', 'settled');
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture export execution identity changed';
        END IF;
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.record_fixture_export_execution() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_fixture_export_execution_birth
AFTER INSERT ON public.export_media_bundles
FOR EACH ROW EXECUTE FUNCTION public.record_fixture_export_execution();
-- aap-statement-break
CREATE FUNCTION public.guard_fixture_export_job_identity()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.id IS NOT DISTINCT FROM OLD.id THEN RETURN NEW; END IF;
    -- Serialize against both Job birth and fixture-owner birth, including a
    -- concurrent transaction whose anchor was not visible at statement start.
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-export:job:' || NEW.id::text, 0)
    );
    -- Lock even an unretired anchor: an INSERT that waits for the original Job
    -- to disappear must not pass an uncommitted concurrent retirement.
    PERFORM 1 FROM yoao_fixture_v1.fixture_export_executions
    WHERE job_id = NEW.id FOR SHARE;
    IF FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture export Job identity cannot be reused';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.guard_fixture_export_job_identity() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_fixture_export_job_identity
BEFORE INSERT OR UPDATE OF id ON public.async_jobs
FOR EACH ROW EXECUTE FUNCTION public.guard_fixture_export_job_identity();
-- aap-statement-break
CREATE FUNCTION public.fixture_export_session_owns_job(p_job_id uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_locks AS held
        CROSS JOIN LATERAL (SELECT pg_catalog.hashtextextended('aap-export:job:' || p_job_id::text, 0) AS key) AS lock_key
        WHERE held.locktype = 'advisory' AND held.pid = pg_catalog.pg_backend_pid()
          AND held.mode = 'ExclusiveLock' AND held.granted AND held.objsubid = 1
          AND held.classid::bigint = ((lock_key.key >> 32) & 4294967295::bigint)
          AND held.objid::bigint = (lock_key.key & 4294967295::bigint)
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.fixture_export_session_owns_job(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION public.fixture_export_execution_admitted(p_job_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_state text;
BEGIN
    IF p_job_id IS NULL OR NOT public.fixture_export_session_owns_job(p_job_id) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture export execution lock required';
    END IF;
    SELECT state INTO v_state FROM yoao_fixture_v1.fixture_export_executions
    WHERE job_id = p_job_id FOR SHARE;
    RETURN NOT FOUND OR v_state IN ('pending', 'settled');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.fixture_export_execution_admitted(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION public.begin_fixture_export_execution(p_job_id uuid, p_project_id uuid, p_token uuid)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_execution yoao_fixture_v1.fixture_export_executions%ROWTYPE;
BEGIN
    IF p_job_id IS NULL OR p_project_id IS NULL OR p_token IS NULL
       OR NOT public.fixture_export_session_owns_job(p_job_id) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture export execution lock required';
    END IF;
    SELECT * INTO v_execution FROM yoao_fixture_v1.fixture_export_executions
    WHERE job_id = p_job_id FOR UPDATE;
    IF NOT FOUND THEN RETURN 'ordinary'; END IF;
    IF v_execution.project_id <> p_project_id OR v_execution.state IN ('running', 'retired') THEN
        RETURN 'blocked';
    END IF;
    UPDATE yoao_fixture_v1.fixture_export_executions
    SET state = 'running', generation = generation + 1, token = p_token,
        backend_pid = pg_catalog.pg_backend_pid()
    WHERE job_id = p_job_id;
    RETURN 'started';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.begin_fixture_export_execution(uuid, uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION public.finish_fixture_export_execution(p_job_id uuid, p_token uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT public.fixture_export_session_owns_job(p_job_id) THEN RETURN false; END IF;
    UPDATE yoao_fixture_v1.fixture_export_executions
    SET state = 'settled', token = NULL, backend_pid = NULL
    WHERE job_id = p_job_id AND state = 'running' AND token = p_token
      AND backend_pid = pg_catalog.pg_backend_pid();
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.finish_fixture_export_execution(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION public.retire_fixture_export_execution(p_run_id uuid, p_project_id uuid, p_job_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_execution yoao_fixture_v1.fixture_export_executions%ROWTYPE;
BEGIN
    IF p_run_id IS NULL OR p_project_id IS NULL OR p_job_id IS NULL THEN RETURN false; END IF;
    IF NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_runs AS run
        JOIN yoao_fixture_v1.fixture_run_resources AS project ON project.run_id = run.id
        JOIN public.async_jobs AS job ON job.id = p_job_id AND job.project_id = project.resource_id
        WHERE run.id = p_run_id AND run.state = 'sealing'
          AND project.kind = 'project' AND project.resource_id = p_project_id
          AND project.state = 'published' AND job.kind = 'export'
          AND job.status IN ('completed', 'failed', 'cancelled')
    ) THEN RETURN false; END IF;
    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-export:job:' || p_job_id::text, 0)
    ) THEN RETURN false; END IF;
    SELECT * INTO v_execution FROM yoao_fixture_v1.fixture_export_executions
    WHERE job_id = p_job_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_execution.run_id <> p_run_id OR v_execution.project_id <> p_project_id
       OR v_execution.state = 'running' THEN RETURN false; END IF;
    IF v_execution.state <> 'retired' THEN
        UPDATE yoao_fixture_v1.fixture_export_executions SET state = 'retired'
        WHERE job_id = p_job_id;
    END IF;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.retire_fixture_export_execution(uuid, uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION public.fixture_export_execution_retired(p_run_id uuid, p_project_id uuid, p_job_id uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_export_executions
        WHERE run_id = p_run_id AND project_id = p_project_id AND job_id = p_job_id
          AND state = 'retired'
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.fixture_export_execution_retired(uuid, uuid, uuid) FROM PUBLIC;
