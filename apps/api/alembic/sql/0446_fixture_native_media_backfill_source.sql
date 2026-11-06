CREATE OR REPLACE FUNCTION aap_assets_v1.begin_image_original_backfill(
    p_job_id uuid, p_generation bigint, p_lease_token uuid, p_batch_token uuid,
    p_owner_kind text, p_owner_id uuid, p_source_snapshot text, p_object_key text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.asset_image_jobs%ROWTYPE;
    v_run uuid;
    v_dataset uuid;
BEGIN
    SELECT job.* INTO v_job FROM public.asset_image_jobs AS job
    WHERE job.job_id = p_job_id FOR SHARE NOWAIT;
    IF NOT FOUND OR p_generation IS NULL OR p_lease_token IS NULL OR p_batch_token IS NULL
       OR v_job.kind <> 'image_backfill' OR v_job.status <> 'running'
       OR v_job.generation <> p_generation OR v_job.lease_token IS DISTINCT FROM p_lease_token
       OR v_job.lease_expires_at IS NULL OR v_job.lease_expires_at <= pg_catalog.clock_timestamp()
       OR v_job.progress #>> '{pending_batch,batch_token}' IS DISTINCT FROM p_batch_token::text
       OR v_job.progress #>> '{pending_batch,lease_token}' IS DISTINCT FROM p_lease_token::text THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    IF pg_catalog.jsonb_typeof(v_job.progress #> '{pending_batch,items}') IS DISTINCT FROM 'array'
       OR NOT EXISTS (
           SELECT 1 FROM pg_catalog.jsonb_array_elements(v_job.progress #> '{pending_batch,items}') AS item
           WHERE item #>> '{owner,kind}' = p_owner_kind AND item #>> '{owner,id}' = p_owner_id::text
             AND item ->> 'source_snapshot' = p_source_snapshot AND item ->> 'file_path' = p_object_key
       ) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    IF p_owner_kind='dataset_item' THEN
        SELECT item.dataset_id, resource.run_id INTO v_dataset,v_run
        FROM public.dataset_items AS item
        JOIN yoao_fixture_v1.fixture_run_resources AS resource
          ON resource.kind='dataset' AND resource.resource_id=item.dataset_id
        WHERE item.id=p_owner_id;
        IF FOUND THEN
            PERFORM 1 FROM yoao_fixture_v1.fixture_runs AS run
            JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=COALESCE(run.parent_run_id,run.id)
            WHERE run.id=v_run AND run.state='open' AND root.state='open' FOR SHARE OF run,root;
            IF NOT FOUND OR NOT EXISTS (
                SELECT 1 FROM aap_assets_v1.fixture_native_media_jobs AS birth
                JOIN aap_assets_v1.fixture_native_media_executions AS execution
                  ON execution.family=birth.family AND execution.job_id=birth.job_id
                 AND execution.run_id=birth.run_id AND execution.dataset_id=birth.dataset_id
                JOIN aap_assets_v1.fixture_native_media_claims AS claim
                  ON claim.execution_id=execution.id AND claim.state='claimed'
                WHERE birth.family='image' AND birth.job_id=p_job_id AND birth.item_id IS NULL
                  AND birth.run_id=v_run AND birth.dataset_id=v_dataset AND execution.state='running'
                  AND claim.job_generation=p_generation AND claim.lease_token=p_lease_token
                  AND aap_assets_v1.fixture_native_execution_lock_held(execution.backend_pid,execution.id)
            ) THEN RETURN aap_assets_v1._image_reply(NULL,'source_changed'); END IF;
        END IF;
    END IF;
    RETURN aap_assets_v1.begin_image_original_system(
        p_owner_kind, p_owner_id, p_source_snapshot, p_object_key
    );
EXCEPTION
    WHEN lock_not_available THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
END;
$function$;
