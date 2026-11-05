-- Native LiDAR ZIP-only deliveries have no media bundle.  Their creation-time
-- owner and each physical output remain independent of the business Job.
CREATE TABLE yoao_fixture_v1.fixture_native_exports (
    job_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    CHECK (expires_at = created_at + INTERVAL '15 minutes')
);
-- aap-statement-break
REVOKE ALL ON yoao_fixture_v1.fixture_native_exports FROM PUBLIC;
-- aap-statement-break
CREATE TABLE yoao_fixture_v1.fixture_native_export_outputs (
    output_id uuid PRIMARY KEY,
    job_id uuid NOT NULL,
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    sha256 varchar(64) NOT NULL,
    size_bytes bigint NOT NULL,
    retired_at timestamptz
);
-- aap-statement-break
REVOKE ALL ON yoao_fixture_v1.fixture_native_export_outputs FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION public.guard_fixture_native_export_evidence()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_TABLE_NAME = 'fixture_native_exports' OR TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture native export evidence is immutable';
    END IF;
    IF OLD.retired_at IS NOT NULL OR NEW.retired_at IS NULL
       OR NEW.output_id IS DISTINCT FROM OLD.output_id OR NEW.job_id IS DISTINCT FROM OLD.job_id
       OR NEW.run_id IS DISTINCT FROM OLD.run_id OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.sha256 IS DISTINCT FROM OLD.sha256 OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture native export receipt is immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.guard_fixture_native_export_evidence() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_fixture_native_export_immutable
BEFORE UPDATE OR DELETE ON yoao_fixture_v1.fixture_native_exports
FOR EACH ROW EXECUTE FUNCTION public.guard_fixture_native_export_evidence();
-- aap-statement-break
CREATE TRIGGER trg_fixture_native_export_output_immutable
BEFORE UPDATE OR DELETE ON yoao_fixture_v1.fixture_native_export_outputs
FOR EACH ROW EXECUTE FUNCTION public.guard_fixture_native_export_evidence();
-- aap-statement-break
CREATE FUNCTION public.capture_fixture_native_export()
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
    INSERT INTO yoao_fixture_v1.fixture_export_executions (job_id, run_id, project_id, state)
    VALUES (NEW.id, v_run_id, NEW.project_id, 'pending');
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.capture_fixture_native_export() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_fixture_native_export_birth
AFTER INSERT ON public.async_jobs
FOR EACH ROW EXECUTE FUNCTION public.capture_fixture_native_export();
-- aap-statement-break
CREATE FUNCTION public.fixture_native_export_lifetime(p_job_id uuid, p_project_id uuid)
RETURNS TABLE (run_id uuid, created_at timestamptz, expires_at timestamptz)
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT owner.run_id, owner.created_at, owner.expires_at
    FROM yoao_fixture_v1.fixture_native_exports AS owner
    WHERE owner.job_id = p_job_id AND owner.project_id = p_project_id;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.fixture_native_export_lifetime(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION public.capture_fixture_native_export_output()
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
       OR NOT EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_export_executions AS execution
           WHERE execution.job_id = NEW.job_id AND execution.run_id = v_owner.run_id
             AND execution.project_id = v_owner.project_id AND execution.state = 'running'
             AND execution.backend_pid = pg_catalog.pg_backend_pid()) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture native export output authority changed';
    END IF;
    INSERT INTO yoao_fixture_v1.fixture_native_export_outputs
        (output_id, job_id, run_id, project_id, sha256, size_bytes)
    VALUES (NEW.id, NEW.job_id, v_owner.run_id, NEW.project_id, NEW.sha256, NEW.size_bytes);
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.capture_fixture_native_export_output() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_fixture_native_export_output_birth
AFTER INSERT ON public.export_outputs
FOR EACH ROW EXECUTE FUNCTION public.capture_fixture_native_export_output();
-- aap-statement-break
CREATE FUNCTION public.fixture_native_export_cleanup_eligible(p_output_id uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_native_export_outputs AS output
        JOIN yoao_fixture_v1.fixture_native_exports AS owner ON owner.job_id = output.job_id
          AND owner.run_id = output.run_id AND owner.project_id = output.project_id
        JOIN yoao_fixture_v1.fixture_export_executions AS execution ON execution.job_id = owner.job_id
          AND execution.run_id = owner.run_id AND execution.project_id = owner.project_id
        WHERE output.output_id = p_output_id AND output.retired_at IS NULL
          AND owner.expires_at <= pg_catalog.clock_timestamp() AND execution.state = 'retired'
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.fixture_native_export_cleanup_eligible(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION public.retire_fixture_native_export_output(p_output_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT public.fixture_native_export_cleanup_eligible(p_output_id) THEN RETURN false; END IF;
    UPDATE yoao_fixture_v1.fixture_native_export_outputs AS receipt SET retired_at = pg_catalog.clock_timestamp()
    FROM public.export_outputs AS output
    WHERE receipt.output_id = p_output_id AND receipt.output_id = output.id
      AND receipt.job_id = output.job_id AND receipt.project_id = output.project_id
      AND receipt.sha256 = output.sha256 AND receipt.size_bytes = output.size_bytes
      AND output.media_bundle_id IS NULL AND output.state = 'retired'
      AND NOT EXISTS (SELECT 1 FROM public.storage_operations AS operation WHERE operation.purpose IN (
          'export_write_' || pg_catalog.replace(output.id::text, '-', ''),
          'export_read_' || pg_catalog.replace(output.id::text, '-', ''),
          'export_delete_' || pg_catalog.replace(output.id::text, '-', '')
      ));
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.retire_fixture_native_export_output(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION public.fixture_export_retired(p_run_id uuid, p_project_id uuid, p_job_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_run_id IS NULL OR p_project_id IS NULL OR p_job_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid fixture export scope';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_runs AS run
        JOIN yoao_fixture_v1.fixture_run_resources AS project ON project.run_id = run.id
        WHERE run.id = p_run_id AND run.state = 'sealing'
          AND project.kind = 'project' AND project.resource_id = p_project_id AND project.state = 'published'
    ) THEN RETURN false; END IF;
    IF EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_native_exports WHERE job_id = p_job_id) THEN
        RETURN EXISTS (
            SELECT 1 FROM yoao_fixture_v1.fixture_native_exports AS owner
            WHERE owner.job_id = p_job_id AND owner.run_id = p_run_id AND owner.project_id = p_project_id
              AND owner.expires_at <= pg_catalog.clock_timestamp()
        ) AND public.fixture_export_execution_retired(p_run_id, p_project_id, p_job_id)
          AND NOT EXISTS (SELECT 1 FROM public.export_media_bundles WHERE job_id = p_job_id)
          AND NOT EXISTS (SELECT 1 FROM public.export_outputs WHERE job_id = p_job_id)
          AND NOT EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_native_export_outputs
              WHERE job_id = p_job_id AND (run_id <> p_run_id OR project_id <> p_project_id OR retired_at IS NULL));
    END IF;
    PERFORM bundle.id FROM public.export_media_bundles AS bundle
    WHERE bundle.job_id = p_job_id ORDER BY bundle.id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN false; END IF;
    RETURN NOT EXISTS (
        SELECT 1 FROM public.export_media_bundles AS bundle
        WHERE bundle.job_id = p_job_id AND (
            bundle.fixture_run_id IS DISTINCT FROM p_run_id OR bundle.project_id IS DISTINCT FROM p_project_id
            OR bundle.state <> 'deleted' OR bundle.expires_at > pg_catalog.clock_timestamp()
            OR (bundle.published_at IS NOT NULL AND NOT bundle.fixture_zip_created)
            OR (bundle.fixture_zip_created AND NOT EXISTS (
                SELECT 1 FROM public.fixture_export_zip_retirements AS receipt
                WHERE receipt.bundle_id = bundle.id AND receipt.output_id = bundle.zip_output_id
                  AND receipt.fixture_run_id = p_run_id AND receipt.project_id = p_project_id AND receipt.job_id = p_job_id
            )) OR EXISTS (
                SELECT 1 FROM public.storage_operations AS operation WHERE operation.purpose IN (
                    'export_media_' || pg_catalog.replace(bundle.id::text, '-', ''),
                    'export_write_' || pg_catalog.replace(bundle.zip_output_id::text, '-', ''),
                    'export_read_' || pg_catalog.replace(bundle.zip_output_id::text, '-', '')
                )
            )
        )
    ) AND NOT EXISTS (SELECT 1 FROM public.export_outputs AS output WHERE output.job_id = p_job_id);
END;
$function$;
