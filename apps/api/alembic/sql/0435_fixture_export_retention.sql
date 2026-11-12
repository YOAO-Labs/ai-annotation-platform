ALTER TABLE public.export_media_bundles
    ADD COLUMN fixture_run_id uuid;
-- aap-statement-break
ALTER TABLE public.export_media_bundles DROP CONSTRAINT ck_export_media_expiry;
-- aap-statement-break
ALTER TABLE public.export_media_bundles ADD CONSTRAINT ck_export_media_expiry CHECK (
    (fixture_run_id IS NULL AND expires_at = created_at + INTERVAL '168 hours')
    OR (fixture_run_id IS NOT NULL AND expires_at = created_at + INTERVAL '15 minutes')
);
-- aap-statement-break
CREATE FUNCTION public.guard_fixture_export_retention()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run_id uuid;
    v_root_id uuid;
    v_actor_id uuid;
    v_root yoao_fixture_v1.fixture_runs%ROWTYPE;
    v_run yoao_fixture_v1.fixture_runs%ROWTYPE;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.fixture_run_id IS DISTINCT FROM OLD.fixture_run_id THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'export fixture retention is immutable';
        END IF;
        RETURN NEW;
    END IF;
    -- The caller cannot choose an owner or shorten a delivery via row metadata.
    IF NEW.fixture_run_id IS NOT NULL
       OR NEW.expires_at IS DISTINCT FROM NEW.created_at + INTERVAL '168 hours' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'invalid export retention request';
    END IF;
    SELECT job.user_id INTO v_actor_id FROM public.async_jobs AS job
    WHERE job.id = NEW.job_id AND job.project_id = NEW.project_id
      AND job.kind = 'export' AND job.status = 'pending';
    IF NOT FOUND OR v_actor_id IS NULL THEN RETURN NEW; END IF;
    SELECT resource.run_id INTO v_run_id
    FROM yoao_fixture_v1.fixture_run_resources AS resource
    WHERE resource.kind = 'project' AND resource.resource_id = NEW.project_id
      AND resource.state = 'published';
    IF NOT FOUND THEN RETURN NEW; END IF;
    SELECT COALESCE(run.parent_run_id, run.id) INTO v_root_id
    FROM yoao_fixture_v1.fixture_runs AS run WHERE run.id = v_run_id;
    IF NOT FOUND THEN RETURN NEW; END IF;
    SELECT root.* INTO v_root FROM yoao_fixture_v1.fixture_runs AS root
    WHERE root.id = v_root_id FOR SHARE;
    IF NOT FOUND OR v_root.parent_run_id IS NOT NULL OR v_root.state <> 'open'
       OR v_root.original_target_id IS NULL OR v_root.original_storage_identity IS NULL
       OR v_root.original_store_receipt_sha256 IS NULL THEN RETURN NEW; END IF;
    SELECT run.* INTO v_run FROM yoao_fixture_v1.fixture_runs AS run
    WHERE run.id = v_run_id FOR SHARE;
    IF NOT FOUND OR COALESCE(v_run.parent_run_id, v_run.id) <> v_root.id
       OR v_run.state <> 'open' OR v_run.family <> v_root.family THEN RETURN NEW; END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_run_resources AS project
    JOIN yoao_fixture_v1.fixture_run_resources AS actor
      ON actor.run_id = project.run_id AND actor.kind = 'user'
     AND actor.resource_id = v_actor_id AND actor.state = 'published'
    JOIN public.users AS account ON account.id = actor.resource_id AND account.is_active
    JOIN yoao_fixture_v1.fixture_original_targets AS target
      ON target.id = v_root.original_target_id
     AND target.storage_identity = v_root.original_storage_identity
     AND target.store_receipt_sha256 = v_root.original_store_receipt_sha256
    WHERE project.run_id = v_run_id AND project.kind = 'project'
      AND project.resource_id = NEW.project_id AND project.state = 'published'
    FOR SHARE OF project, actor, account, target;
    IF NOT FOUND THEN RETURN NEW; END IF;
    NEW.fixture_run_id := v_run_id;
    NEW.created_at := pg_catalog.clock_timestamp();
    NEW.expires_at := NEW.created_at + INTERVAL '15 minutes';
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.guard_fixture_export_retention() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_export_media_fixture_retention
BEFORE INSERT OR UPDATE OF fixture_run_id ON public.export_media_bundles
FOR EACH ROW EXECUTE FUNCTION public.guard_fixture_export_retention();
