-- Preserve every existing unknown-scope, presentation, prediction and export
-- denial; only the exact retired dataset backfill joins the existing proofs.
CREATE OR REPLACE FUNCTION yoao_fixture_v1.guard_dataset_job_anchor()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_old jsonb; v_new jsonb; v_run record;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        v_old := aap_jobs_v1.fixture_job_dataset_scope(
            OLD.id,OLD.kind,OLD.payload,OLD.project_id,OLD.user_id);
    END IF;
    IF TG_OP <> 'DELETE' THEN
        v_new := aap_jobs_v1.fixture_job_dataset_scope(
            NEW.id,NEW.kind,NEW.payload,NEW.project_id,NEW.user_id);
    END IF;
    IF TG_OP='DELETE' AND EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_presentation_jobs AS proof
        JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=proof.run_id
        WHERE proof.job_id=OLD.id AND proof.project_id=OLD.project_id
          AND proof.kind=OLD.kind AND proof.state='deleting'
          AND (run.state='sealing' OR (
              run.state='open' AND OLD.celery_task_id IS NULL AND OLD.started_at IS NULL
          ))
    ) THEN RETURN OLD; END IF;
    IF TG_OP='DELETE' AND EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.kind='project' AND resource.resource_id=OLD.project_id
          AND resource.state='published'
          AND aap_jobs_v1.fixture_real_job_retired(resource.run_id,resource.resource_id,OLD.id) IS TRUE
    ) THEN RETURN OLD; END IF;
    IF TG_OP='DELETE' AND EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_backfills AS binding
        WHERE binding.job_id=OLD.id AND OLD.kind='media_backfill' AND OLD.project_id IS NULL
          AND aap_jobs_v1.fixture_media_backfill_retired(binding.run_id,binding.dataset_id,OLD.id) IS TRUE
    ) THEN RETURN OLD; END IF;
    IF TG_OP='INSERT' AND EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_presentation_jobs AS proof
        JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=proof.run_id
        WHERE proof.job_id=NEW.id AND proof.project_id=NEW.project_id
          AND proof.kind=NEW.kind AND proof.state='admitted' AND run.state='open'
    ) THEN RETURN NEW; END IF;
    IF TG_OP='UPDATE' AND v_old=v_new THEN RETURN NEW; END IF;
    IF TG_OP <> 'INSERT' THEN
        IF v_old ->> 'kind'='unknown' THEN
            PERFORM 1 FROM yoao_fixture_v1.fixture_runs AS run
            WHERE run.state <> 'retired' AND EXISTS (
                SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
                WHERE resource.run_id=run.id AND resource.kind='dataset'
            );
        ELSIF v_old ->> 'kind'='dataset' THEN
            PERFORM 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
            JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=resource.run_id
            WHERE resource.kind='dataset' AND run.state <> 'retired'
              AND resource.resource_id::text=v_old ->> 'dataset_id';
        ELSIF v_old ->> 'kind'='datasets' THEN
            PERFORM 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
            JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=resource.run_id
            WHERE resource.kind='dataset' AND run.state <> 'retired'
              AND (v_old -> 'dataset_ids') ? resource.resource_id::text;
        END IF;
        IF v_old ->> 'kind' IN ('unknown','dataset','datasets') AND FOUND THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_dataset_job_unsettled';
        END IF;
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    IF v_new ->> 'kind' IN ('dataset','datasets') THEN
        FOR v_run IN
            SELECT run.state FROM yoao_fixture_v1.fixture_runs AS run
            JOIN yoao_fixture_v1.fixture_run_resources AS resource ON resource.run_id=run.id
            WHERE resource.kind='dataset'
              AND (resource.resource_id::text=v_new ->> 'dataset_id'
                   OR (v_new -> 'dataset_ids') ? resource.resource_id::text)
            FOR SHARE OF run
        LOOP
            IF v_run.state <> 'open' THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_run_closed';
            END IF;
        END LOOP;
    ELSIF v_new ->> 'kind'='unknown' THEN
        FOR v_run IN
            SELECT run.state FROM yoao_fixture_v1.fixture_runs AS run
            WHERE run.state <> 'retired' AND EXISTS (
                SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
                WHERE resource.run_id=run.id AND resource.kind='dataset'
            ) FOR SHARE OF run
        LOOP
            IF v_run.state <> 'open' THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_dataset_job_scope_unknown';
            END IF;
        END LOOP;
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_fixture_v1.guard_dataset_job_anchor() FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1.guard_fixture_dataset_jobs(p_dataset_ids uuid[])
RETURNS void LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='0A000', MESSAGE='fixture_dataset_isolation_unsupported';
    END IF;
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()),5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()),4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='Jobs fixture cleanup requires a test database';
    END IF;
    IF p_dataset_ids IS NULL OR pg_catalog.array_position(p_dataset_ids,NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid Jobs fixture dataset scope';
    END IF;
    IF pg_catalog.cardinality(p_dataset_ids)=0 THEN RETURN; END IF;
    IF EXISTS (
        SELECT 1 FROM public.async_jobs AS job
        CROSS JOIN LATERAL (
            SELECT aap_jobs_v1.fixture_job_dataset_scope(
                job.id,job.kind,job.payload,job.project_id,job.user_id) AS value
        ) AS scope
        WHERE (scope.value ->> 'kind'='unknown'
           OR (scope.value ->> 'kind' IN ('dataset','datasets') AND EXISTS (
                SELECT 1 FROM pg_catalog.unnest(p_dataset_ids) AS dataset(id)
                WHERE dataset.id::text=scope.value ->> 'dataset_id'
                   OR (scope.value -> 'dataset_ids') ? dataset.id::text
           )))
          AND NOT EXISTS (
              SELECT 1 FROM yoao_fixture_v1.fixture_presentation_jobs AS proof
              WHERE proof.job_id=job.id AND proof.project_id=job.project_id
                AND proof.kind=job.kind AND proof.state='admitted'
          )
          AND NOT EXISTS (
              SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
              WHERE resource.kind='project' AND resource.resource_id=job.project_id
                AND resource.state='published'
                AND aap_jobs_v1.fixture_real_job_retired(resource.run_id,resource.resource_id,job.id) IS TRUE
          )
          AND NOT EXISTS (
              SELECT 1 FROM aap_jobs_v1.fixture_media_backfills AS binding
              WHERE binding.job_id=job.id AND job.kind='media_backfill' AND job.project_id IS NULL
                AND binding.dataset_id=ANY(p_dataset_ids)
                AND aap_jobs_v1.fixture_media_backfill_retired(binding.run_id,binding.dataset_id,job.id) IS TRUE
          )
    ) OR EXISTS (
        SELECT 1 FROM public.image_export_scopes AS scope
        WHERE scope.dataset_ids && p_dataset_ids
          AND NOT EXISTS (SELECT 1 FROM public.async_jobs AS job WHERE job.id=scope.job_id)
          AND NOT EXISTS (
              SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS project
              WHERE project.kind='project' AND project.resource_id=scope.project_id
                AND public.fixture_export_execution_retired(project.run_id,project.resource_id,scope.job_id)
          )
    ) OR EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_backfills AS binding
        WHERE binding.dataset_id=ANY(p_dataset_ids)
          AND NOT EXISTS (SELECT 1 FROM public.async_jobs AS job WHERE job.id=binding.job_id)
          AND aap_jobs_v1.fixture_media_backfill_retired(binding.run_id,binding.dataset_id,binding.job_id)
              IS DISTINCT FROM true
    ) OR EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.kind='dataset' AND resource.state='published'
          AND resource.resource_id=ANY(p_dataset_ids)
          AND aap_assets_v1.fixture_dataset_media_retired(resource.run_id,resource.resource_id)
              IS DISTINCT FROM true
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_dataset_job_unsettled';
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_fixture_dataset_jobs(uuid[]) FROM PUBLIC;
