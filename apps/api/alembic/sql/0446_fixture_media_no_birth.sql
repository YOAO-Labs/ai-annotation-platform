-- A root with no native participants is eligible only on positive sealed,
-- run-scoped absence of every relevant admission, producer and reader.
CREATE FUNCTION aap_assets_v1.fixture_run_no_media_births(p_run uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_root uuid;
BEGIN
    SELECT COALESCE(parent_run_id,id) INTO v_root FROM yoao_fixture_v1.fixture_runs WHERE id=p_run;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs WHERE id=v_root AND parent_run_id IS NULL
      AND state IN ('open','sealing') FOR SHARE;
    IF NOT FOUND THEN RETURN false; END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs AS run
    WHERE run.id=p_run AND run.state='sealing' AND (
        (run.family='owned' AND run.parent_run_id=v_root) OR
        (run.family IN ('screenshot','scale') AND run.parent_run_id IS NULL AND run.id=v_root)
    ) FOR SHARE;
    IF NOT FOUND THEN RETURN false; END IF;
    RETURN NOT EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_run_admissions WHERE run_id=p_run AND state<>'settled')
       AND NOT EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_image_work_intents WHERE run_id=p_run)
       AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_cache_targets WHERE run_id=p_run)
       AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_resolver_principals WHERE suite_id=p_run)
       AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_read_principals WHERE run_id=p_run)
       AND NOT EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_media_executor_principals WHERE run_id=p_run)
       AND NOT EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_media_backfills WHERE run_id=p_run)
       AND NOT EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_media_children WHERE run_id=p_run)
       AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_media_births WHERE run_id=p_run)
       AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_media_jobs WHERE run_id=p_run)
       AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_independent_media_births WHERE run_id=p_run)
       AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_legacy_birth_admissions WHERE run_id=p_run)
       AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_screenshot_producer_admissions WHERE run_id=p_run)
       AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_media_executions WHERE run_id=p_run)
       AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_scan_admissions WHERE run_id=p_run)
       AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_media_admissions WHERE run_id=p_run)
       AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_media_sources WHERE run_id=p_run)
       AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_cache_reads WHERE run_id=p_run)
       AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_cache_writes WHERE run_id=p_run)
       AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_media_versions WHERE run_id=p_run)
       AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_external_media_sources
                       WHERE run_id=p_run AND state<>'settled')
       AND NOT EXISTS (
           SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
           LEFT JOIN public.managed_original_creations AS creation ON creation.id=resource.creation_id
           LEFT JOIN public.storage_operations AS operation ON operation.location_id=creation.location_id
             AND operation.node_id='aap-assets-media'
             AND operation.purpose='media_original_' || pg_catalog.replace(creation.id::text,'-','')
           WHERE resource.run_id=p_run AND resource.kind='creation'
             AND (creation.id IS NULL OR operation.id IS NOT NULL)
       )
       AND NOT EXISTS (
           SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS dataset
           WHERE dataset.run_id=p_run AND dataset.kind='dataset' AND (
               dataset.state<>'published' OR NOT EXISTS (SELECT 1 FROM public.datasets WHERE id=dataset.resource_id)
               OR aap_assets_v1.fixture_dataset_legacy_original_readers_retired(p_run,dataset.resource_id) IS DISTINCT FROM true
               OR EXISTS (SELECT 1 FROM public.dataset_items AS item
                          JOIN public.asset_media_jobs AS job ON job.dataset_item_id=item.id
                          WHERE item.dataset_id=dataset.resource_id AND NOT EXISTS (
                              SELECT 1 FROM aap_assets_v1.fixture_external_media_jobs AS external
                              WHERE external.family='media' AND external.job_id=job.job_id
                                AND external.run_id=p_run AND external.dataset_id=dataset.resource_id))
               OR EXISTS (SELECT 1 FROM public.asset_media_jobs AS job
                          WHERE job.kind='video_backfill' AND job.backfill_selection ->> 'dataset_id'=dataset.resource_id::text
                            AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_external_media_jobs AS external
                                WHERE external.family='media' AND external.job_id=job.job_id AND external.run_id=p_run))
               OR EXISTS (SELECT 1 FROM public.dataset_items AS item
                          JOIN public.asset_image_jobs AS job ON job.owner_kind='dataset_item' AND job.owner_id=item.id
                          WHERE item.dataset_id=dataset.resource_id AND NOT EXISTS (
                              SELECT 1 FROM aap_assets_v1.fixture_external_media_jobs AS external
                              WHERE external.family='image' AND external.job_id=job.job_id
                                AND external.run_id=p_run AND external.dataset_id=dataset.resource_id))
               OR EXISTS (SELECT 1 FROM public.asset_image_jobs AS job
                          WHERE job.kind='image_backfill' AND job.spec #>> '{selection,dataset_id}'=dataset.resource_id::text
                            AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_external_media_jobs AS external
                                WHERE external.family='image' AND external.job_id=job.job_id AND external.run_id=p_run))
           )
       )
       AND NOT EXISTS (
           SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
           LEFT JOIN aap_labeling_v1.task_rows AS task ON task.id=resource.resource_id
           WHERE resource.run_id=p_run AND resource.kind='task' AND (
               resource.state<>'published' OR task.id IS NULL
               OR EXISTS (SELECT 1 FROM public.asset_image_jobs AS job
                          WHERE job.owner_kind='task' AND job.owner_id=task.id AND NOT EXISTS (
                              SELECT 1 FROM aap_assets_v1.fixture_external_media_jobs AS external
                              WHERE external.family='image' AND external.job_id=job.job_id AND external.run_id=p_run))
               OR aap_assets_v1.fixture_task_legacy_original_readers_retired(p_run,task.id) IS DISTINCT FROM true
           )
       );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_run_no_media_births(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_assets_v1.fixture_run_no_media_birth_certificates (
    run_id uuid PRIMARY KEY,
    root_id uuid NOT NULL,
    family text NOT NULL,
    resources jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(resources)='array')
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_run_no_media_birth_certificates FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_run_no_media_birth_certificate_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_run_no_media_birth_certificates FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_birth_evidence();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.seal_fixture_run_without_media(p_run uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_root uuid; v_run yoao_fixture_v1.fixture_runs%ROWTYPE; v_resources jsonb;
BEGIN
    IF NOT COALESCE((SELECT pg_catalog.pg_has_role(SESSION_USER,pg_catalog.pg_get_userbyid(c.relowner),'MEMBER')
        FROM pg_catalog.pg_class AS c WHERE c.oid=pg_catalog.to_regclass('public.fixture_export_zip_retirements')),false)
    THEN RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_private_owner_required'; END IF;
    SELECT COALESCE(parent_run_id,id) INTO v_root FROM yoao_fixture_v1.fixture_runs WHERE id=p_run;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs WHERE id=v_root AND parent_run_id IS NULL FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT * INTO v_run FROM yoao_fixture_v1.fixture_runs WHERE id=p_run AND COALESCE(parent_run_id,id)=v_root FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    IF EXISTS (SELECT 1 FROM aap_assets_v1.fixture_run_no_media_birth_certificates AS certificate
               WHERE certificate.run_id=p_run AND certificate.root_id=v_root AND certificate.family=v_run.family)
       THEN RETURN v_run.state IN ('sealing','retired'); END IF;
    IF aap_assets_v1.fixture_run_no_media_births(p_run) IS DISTINCT FROM true THEN RETURN false; END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
        'kind',resource.kind,'id',resource.resource_id,'state',resource.state
    ) ORDER BY resource.kind,resource.resource_id),'[]'::jsonb)
    INTO v_resources FROM yoao_fixture_v1.fixture_run_resources AS resource
    WHERE resource.run_id=p_run AND resource.kind IN ('dataset','item','task');
    INSERT INTO aap_assets_v1.fixture_run_no_media_birth_certificates(run_id,root_id,family,resources)
    VALUES(p_run,v_root,v_run.family,v_resources);
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.seal_fixture_run_without_media(uuid) FROM PUBLIC;
