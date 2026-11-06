-- Fixture retention remains separate from the canonical Export execution owner.
CREATE OR REPLACE FUNCTION public.guard_fixture_export_execution()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_OP = 'DELETE' OR NEW.job_id IS DISTINCT FROM OLD.job_id
       OR (NEW.run_id IS DISTINCT FROM OLD.run_id AND NOT (OLD.run_id IS NULL AND NEW.run_id IS NOT NULL AND pg_catalog.pg_trigger_depth() >= 2))
       OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR OLD.state = 'retired' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture export execution evidence is immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION public.record_fixture_export_execution()
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
        UPDATE aap_export_v1.executions SET run_id = NEW.fixture_run_id
        WHERE job_id = NEW.job_id AND project_id = NEW.project_id AND run_id IS NULL AND state = 'pending';
        PERFORM 1 FROM aap_export_v1.executions
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
CREATE OR REPLACE FUNCTION public.guard_fixture_export_job_identity()
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
    PERFORM 1 FROM aap_export_v1.executions
    WHERE job_id = NEW.id FOR SHARE;
    IF FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture export Job identity cannot be reused';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION public.retire_fixture_export_execution(p_run_id uuid, p_project_id uuid, p_job_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_execution aap_export_v1.executions%ROWTYPE;
BEGIN
    IF p_run_id IS NULL OR p_project_id IS NULL OR p_job_id IS NULL THEN RETURN false; END IF;
    IF NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_runs AS run
        JOIN yoao_fixture_v1.fixture_run_resources AS project ON project.run_id = run.id
        JOIN public.async_jobs AS job ON job.id = p_job_id AND job.project_id = project.resource_id
        WHERE run.id = p_run_id AND run.state = 'sealing'
          AND project.kind = 'project' AND project.resource_id = p_project_id
          AND project.state = 'published' AND job.kind = 'export'
    ) THEN RETURN false; END IF;
    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-export:job:' || p_job_id::text, 0)
    ) THEN RETURN false; END IF;
    SELECT * INTO v_execution FROM aap_export_v1.executions
    WHERE job_id = p_job_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_execution.run_id IS DISTINCT FROM p_run_id
       OR v_execution.project_id IS DISTINCT FROM p_project_id
       OR v_execution.checkpoint IS DISTINCT FROM 'settled'
       OR v_execution.state NOT IN ('settled', 'retired')
       OR NOT aap_export_v1.publications_retired(p_job_id) THEN RETURN false; END IF;
    IF v_execution.state <> 'retired' THEN
        UPDATE aap_export_v1.executions SET state = 'retired'
        WHERE job_id = p_job_id;
    END IF;
    RETURN true;
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION public.fixture_export_execution_retired(p_run_id uuid, p_project_id uuid, p_job_id uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM aap_export_v1.executions
        WHERE run_id = p_run_id AND project_id = p_project_id AND job_id = p_job_id
          AND state = 'retired' AND checkpoint = 'settled'
          AND aap_export_v1.publications_retired(p_job_id)
    );
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION public.capture_fixture_native_export()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run_id uuid;
    v_root_id uuid;
    v_root yoao_fixture_v1.fixture_runs%ROWTYPE;
    v_run yoao_fixture_v1.fixture_runs%ROWTYPE;
    v_created timestamptz;
BEGIN
    IF NEW.kind <> 'export' OR NEW.status <> 'pending' OR NEW.user_id IS NULL
       OR NEW.payload->'targets' IS DISTINCT FROM '["native_json"]'::jsonb
       OR NOT EXISTS (SELECT 1 FROM public.projects WHERE id = NEW.project_id AND data_type = 'lidar') THEN
        RETURN NEW;
    END IF;
    SELECT resource.run_id INTO v_run_id FROM yoao_fixture_v1.fixture_run_resources AS resource
    WHERE resource.kind = 'project' AND resource.resource_id = NEW.project_id AND resource.state = 'published';
    IF NOT FOUND THEN RETURN NEW; END IF;
    SELECT COALESCE(run.parent_run_id, run.id) INTO v_root_id
    FROM yoao_fixture_v1.fixture_runs AS run WHERE run.id = v_run_id;
    IF NOT FOUND THEN RETURN NEW; END IF;
    SELECT root.* INTO v_root FROM yoao_fixture_v1.fixture_runs AS root WHERE root.id = v_root_id FOR SHARE;
    IF NOT FOUND OR v_root.parent_run_id IS NOT NULL OR v_root.state <> 'open'
       OR v_root.original_target_id IS NULL OR v_root.original_storage_identity IS NULL
       OR v_root.original_store_receipt_sha256 IS NULL THEN RETURN NEW; END IF;
    SELECT run.* INTO v_run FROM yoao_fixture_v1.fixture_runs AS run WHERE run.id = v_run_id FOR SHARE;
    IF NOT FOUND OR COALESCE(v_run.parent_run_id, v_run.id) <> v_root.id
       OR v_run.state <> 'open' OR v_run.family <> v_root.family THEN RETURN NEW; END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_run_resources AS project
    JOIN yoao_fixture_v1.fixture_run_resources AS actor
      ON actor.run_id = project.run_id AND actor.kind = 'user'
     AND actor.resource_id = NEW.user_id AND actor.state = 'published'
    JOIN public.users AS account ON account.id = actor.resource_id AND account.is_active
    JOIN yoao_fixture_v1.fixture_original_targets AS target
      ON target.id = v_root.original_target_id AND target.storage_identity = v_root.original_storage_identity
     AND target.store_receipt_sha256 = v_root.original_store_receipt_sha256
    WHERE project.run_id = v_run_id AND project.kind = 'project'
      AND project.resource_id = NEW.project_id AND project.state = 'published'
    FOR SHARE OF project, actor, account, target;
    IF NOT FOUND THEN RETURN NEW; END IF;
    v_created := pg_catalog.clock_timestamp();
    INSERT INTO yoao_fixture_v1.fixture_native_exports (job_id, run_id, project_id, created_at, expires_at)
    VALUES (NEW.id, v_run_id, NEW.project_id, v_created, v_created + INTERVAL '15 minutes');
    UPDATE aap_export_v1.executions SET run_id = v_run_id
    WHERE job_id = NEW.id AND project_id = NEW.project_id AND run_id IS NULL AND state = 'pending';
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture export execution identity changed';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION public.capture_fixture_native_export_output()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_owner yoao_fixture_v1.fixture_native_exports%ROWTYPE;
BEGIN
    SELECT * INTO v_owner FROM yoao_fixture_v1.fixture_native_exports WHERE job_id = NEW.job_id;
    IF NOT FOUND THEN RETURN NEW; END IF;
    IF NEW.project_id <> v_owner.project_id OR NEW.media_bundle_id IS NOT NULL
       OR NEW.created_at <> v_owner.created_at OR NEW.expires_at <> v_owner.expires_at
       OR NEW.expires_at <= pg_catalog.clock_timestamp()
       OR NOT public.fixture_export_session_owns_job(NEW.job_id)
       OR NOT EXISTS (SELECT 1 FROM aap_export_v1.executions AS execution
           WHERE execution.job_id = NEW.job_id AND execution.run_id = v_owner.run_id
             AND execution.project_id = v_owner.project_id AND execution.state = 'running'
             AND execution.business_backend_pid = pg_catalog.pg_backend_pid()) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture native export output authority changed';
    END IF;
    INSERT INTO yoao_fixture_v1.fixture_native_export_outputs
        (output_id, job_id, run_id, project_id, sha256, size_bytes)
    VALUES (NEW.id, NEW.job_id, v_owner.run_id, NEW.project_id, NEW.sha256, NEW.size_bytes);
    RETURN NEW;
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION public.fixture_native_export_cleanup_eligible(p_output_id uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_native_export_outputs AS output
        JOIN yoao_fixture_v1.fixture_native_exports AS owner ON owner.job_id = output.job_id
          AND owner.run_id = output.run_id AND owner.project_id = output.project_id
        JOIN aap_export_v1.executions AS execution ON execution.job_id = owner.job_id
          AND execution.run_id = owner.run_id AND execution.project_id = owner.project_id
        WHERE output.output_id = p_output_id AND output.retired_at IS NULL
          AND owner.expires_at <= pg_catalog.clock_timestamp()
          AND execution.state = 'retired' AND execution.checkpoint = 'settled'
          AND aap_export_v1.publications_retired(owner.job_id)
    );
$function$;
