-- The Jobs owner joins its immutable dispatch/execution history with the
-- independently computed Assets source/output retirement proof.
CREATE FUNCTION aap_jobs_v1.fixture_media_backfill_retired(p_run uuid,p_dataset uuid,p_job uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_backfills AS binding
        JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=binding.run_id
        JOIN yoao_fixture_v1.fixture_run_resources AS dataset
          ON dataset.run_id=binding.run_id AND dataset.kind='dataset'
         AND dataset.resource_id=binding.dataset_id AND dataset.state='published'
        CROSS JOIN LATERAL (
            SELECT ARRAY(
                SELECT DISTINCT dispatch.envelope ->> 'execution_id'
                FROM aap_jobs_v1.fixture_media_dispatches AS dispatch
                WHERE dispatch.job_id=binding.job_id AND dispatch.run_id=p_run AND dispatch.dataset_id=p_dataset
                ORDER BY dispatch.envelope ->> 'execution_id'
            ) AS execution_ids
        ) AS publication
        WHERE binding.job_id=p_job AND binding.run_id=p_run AND binding.dataset_id=p_dataset
          AND binding.state='retired' AND run.state='sealing'
          AND aap_jobs_v1.job_publications_retired(binding.job_id,publication.execution_ids)
          AND ((binding.retirement_outcome='not_sent' AND binding.generation=0
                AND aap_jobs_v1.job_publications_not_sent(binding.job_id,publication.execution_ids))
            OR (binding.retirement_outcome='executed' AND binding.generation>0
                AND NOT aap_jobs_v1.job_publications_not_sent(binding.job_id,publication.execution_ids)))
          AND NOT EXISTS (
              SELECT 1 FROM public.async_jobs AS job WHERE job.id=p_job
                AND (job.kind IS DISTINCT FROM 'media_backfill' OR job.project_id IS NOT NULL
                  OR job.user_id IS DISTINCT FROM binding.actor_id
                  OR job.payload IS DISTINCT FROM binding.payload)
          )
          AND (SELECT pg_catalog.count(*) FROM aap_jobs_v1.fixture_media_dispatches AS dispatch
               WHERE dispatch.job_id=p_job AND dispatch.run_id=p_run AND dispatch.dataset_id=p_dataset
                 AND dispatch.envelope ->> 'execution_id'=p_job::text) = 1
          AND NOT EXISTS (
              SELECT 1 FROM aap_jobs_v1.fixture_media_children AS child
              WHERE child.job_id=p_job AND (child.run_id<>p_run OR child.dataset_id<>p_dataset
                  OR child.state<>'retired' OR child.retirement_outcome IS DISTINCT FROM binding.retirement_outcome)
          )
          AND NOT EXISTS (
              SELECT 1 FROM aap_jobs_v1.fixture_media_children AS child
              WHERE child.job_id=p_job AND
                (SELECT pg_catalog.count(*) FROM aap_jobs_v1.fixture_media_dispatches AS dispatch
                 WHERE dispatch.job_id=p_job AND dispatch.run_id=p_run
                   AND dispatch.dataset_id=p_dataset
                   AND dispatch.envelope ->> 'execution_id'=child.child_id::text) <> 1
          )
          AND NOT EXISTS (
              SELECT 1 FROM aap_jobs_v1.fixture_media_dispatches AS dispatch
              WHERE dispatch.job_id=p_job AND (dispatch.run_id<>p_run OR dispatch.dataset_id<>p_dataset
                OR (dispatch.envelope ->> 'execution_id' <> p_job::text AND NOT EXISTS (
                    SELECT 1 FROM aap_jobs_v1.fixture_media_children AS child
                    WHERE child.child_id::text=dispatch.envelope ->> 'execution_id'
                      AND child.job_id=p_job AND child.run_id=p_run AND child.dataset_id=p_dataset
                      AND child.state='retired'
                )))
          )
          AND aap_assets_v1.fixture_dataset_media_retired(p_run,p_dataset) IS TRUE
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_media_backfill_retired(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.retire_fixture_media_executor(p_run uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_principal aap_jobs_v1.fixture_media_executor_principals%ROWTYPE;
BEGIN
    IF NOT COALESCE((
        SELECT pg_catalog.pg_has_role(SESSION_USER,pg_catalog.pg_get_userbyid(c.relowner),'MEMBER')
        FROM pg_catalog.pg_class AS c WHERE c.oid=pg_catalog.to_regclass('public.fixture_export_zip_retirements')
    ),false) THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture executor requires private owner';
    END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs
    WHERE id=p_run AND state='sealing' AND
      ((parent_run_id IS NOT NULL AND family='owned') OR (parent_run_id IS NULL AND family='screenshot')) FOR SHARE;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT * INTO v_principal FROM aap_jobs_v1.fixture_media_executor_principals
    WHERE run_id=p_run FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    IF v_principal.state='retired' THEN RETURN true; END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_stat_activity WHERE usesysid=v_principal.principal_oid)
       OR EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_media_backfills WHERE run_id=p_run AND state<>'retired')
       OR EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_media_children WHERE run_id=p_run AND state<>'retired')
       OR EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_media_retirement_sessions WHERE run_id=p_run AND state<>'settled')
       OR EXISTS (
           SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
           WHERE resource.run_id=p_run AND resource.kind='dataset'
             AND (resource.state<>'published' OR aap_assets_v1.fixture_dataset_media_retired(
                 p_run,resource.resource_id) IS DISTINCT FROM true)
       ) THEN RETURN false; END IF;
    UPDATE aap_jobs_v1.fixture_media_executor_principals SET state='retired'
    WHERE run_id=p_run AND principal_oid=v_principal.principal_oid AND state='active';
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.retire_fixture_media_executor(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.fixture_media_executor_identity(p_run uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_principal aap_jobs_v1.fixture_media_executor_principals%ROWTYPE;
BEGIN
    IF NOT COALESCE((
        SELECT pg_catalog.pg_has_role(SESSION_USER,pg_catalog.pg_get_userbyid(c.relowner),'MEMBER')
        FROM pg_catalog.pg_class AS c WHERE c.oid=pg_catalog.to_regclass('public.fixture_export_zip_retirements')
    ),false) THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture executor requires private owner';
    END IF;
    SELECT * INTO v_principal FROM aap_jobs_v1.fixture_media_executor_principals WHERE run_id=p_run;
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN pg_catalog.jsonb_build_object('oid',v_principal.principal_oid,
        'name',v_principal.principal_name,'state',v_principal.state);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_media_executor_identity(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.fixture_media_run_retired(p_run uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT NOT EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_backfills AS binding
        WHERE binding.run_id=p_run AND aap_jobs_v1.fixture_media_backfill_retired(
            p_run,binding.dataset_id,binding.job_id
        ) IS DISTINCT FROM true
    ) AND NOT EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_children WHERE run_id=p_run AND state<>'retired'
    ) AND NOT EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_retirement_sessions WHERE run_id=p_run AND state<>'settled'
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_media_run_retired(uuid) FROM PUBLIC;
