-- Each native Job is retired by its Assets owner only after every born output
-- scope, source operation and real execution has settled.  The immutable birth
-- and this tombstone survive removal of the business anchor.
CREATE TABLE aap_assets_v1.fixture_native_job_retirements (
    family text NOT NULL CHECK (family IN ('image','media')),
    job_id uuid NOT NULL,
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    PRIMARY KEY (family,job_id)
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_native_job_retirements FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_native_job_retirement()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_job_retirement_immutable';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_job_retirement() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_job_retirement_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_native_job_retirements FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_job_retirement();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_native_job_inventory(p_run uuid,p_dataset uuid,p_token uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_jobs jsonb;
BEGIN
    IF aap_jobs_v1.fixture_media_retirement_authorized(p_run,p_dataset,p_token) IS DISTINCT FROM true
       OR aap_assets_v1.fixture_native_outputs_retired(p_run,p_dataset) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_retirement_unsettled';
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object('family',birth.family,'job_id',birth.job_id)
        ORDER BY birth.family,birth.job_id),'[]'::jsonb) INTO v_jobs
    FROM aap_assets_v1.fixture_native_media_jobs AS birth
    WHERE birth.run_id=p_run AND birth.dataset_id=p_dataset;
    RETURN v_jobs;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_native_job_inventory(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.retire_fixture_native_job(
    p_run uuid,p_dataset uuid,p_token uuid,p_family text,p_job uuid
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_birth aap_assets_v1.fixture_native_media_jobs%ROWTYPE; v_job record;
BEGIN
    IF p_family NOT IN ('image','media') OR p_job IS NULL
       OR aap_jobs_v1.fixture_media_retirement_authorized(p_run,p_dataset,p_token) IS DISTINCT FROM true
       OR aap_assets_v1.fixture_native_outputs_retired(p_run,p_dataset) IS DISTINCT FROM true THEN RETURN false; END IF;
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_native_media_jobs
    WHERE family=p_family AND job_id=p_job AND run_id=p_run AND dataset_id=p_dataset;
    IF NOT FOUND THEN RETURN false; END IF;
    IF EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_job_retirements
               WHERE family=p_family AND job_id=p_job AND run_id=p_run AND dataset_id=p_dataset) THEN
        IF p_family='image' THEN
            RETURN NOT EXISTS (SELECT 1 FROM public.asset_image_jobs WHERE job_id=p_job);
        ELSE
            RETURN NOT EXISTS (SELECT 1 FROM public.asset_media_jobs WHERE job_id=p_job);
        END IF;
    END IF;
    IF p_family='image' THEN
        SELECT job.kind,job.owner_kind,job.owner_id,job.spec -> 'selection' AS selection,
               job.generation,job.lease_token INTO v_job
        FROM public.asset_image_jobs AS job WHERE job.job_id=p_job FOR UPDATE;
        IF NOT FOUND OR v_job.kind IS DISTINCT FROM v_birth.job_kind
           OR (v_birth.item_id IS NOT NULL AND (v_job.owner_kind,v_job.owner_id)
               IS DISTINCT FROM (v_birth.owner_kind,v_birth.owner_id))
           OR (v_birth.item_id IS NULL AND (v_job.owner_kind IS NOT NULL OR v_job.owner_id IS NOT NULL
               OR v_job.selection IS DISTINCT FROM v_birth.selection)) THEN RETURN false; END IF;
    ELSE
        SELECT job.kind,job.dataset_item_id,job.backfill_selection AS selection,
               job.generation,job.lease_token INTO v_job
        FROM public.asset_media_jobs AS job WHERE job.job_id=p_job FOR UPDATE;
        IF NOT FOUND OR v_job.kind IS DISTINCT FROM v_birth.job_kind
           OR v_job.dataset_item_id IS DISTINCT FROM v_birth.item_id
           OR (v_birth.item_id IS NULL AND v_job.selection IS DISTINCT FROM v_birth.selection) THEN RETURN false; END IF;
    END IF;
    IF v_job.lease_token IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_media_claims AS claim
        JOIN aap_assets_v1.fixture_native_media_executions AS execution ON execution.id=claim.execution_id
        WHERE claim.family=p_family AND claim.job_id=p_job AND claim.state='claimed'
          AND claim.job_generation=v_job.generation AND claim.lease_token=v_job.lease_token
          AND execution.run_id=p_run AND execution.dataset_id=p_dataset AND execution.state='settled'
    ) THEN RETURN false; END IF;
    IF p_family='image' AND EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_pyramid_generations AS binding
        WHERE binding.job_id=p_job AND binding.run_id=p_run AND binding.dataset_id=p_dataset
          AND aap_assets_v1.fixture_native_pyramid_generation_retired(
              p_run,p_dataset,binding.asset_id,binding.generation
          ) IS DISTINCT FROM true
    ) THEN RETURN false; END IF;
    INSERT INTO aap_assets_v1.fixture_native_job_retirements(family,job_id,run_id,dataset_id)
    VALUES (p_family,p_job,p_run,p_dataset);
    IF p_family='image' THEN DELETE FROM public.asset_image_jobs WHERE job_id=p_job;
    ELSE DELETE FROM public.asset_media_jobs WHERE job_id=p_job; END IF;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.retire_fixture_native_job(uuid,uuid,uuid,text,uuid) FROM PUBLIC;
