CREATE FUNCTION aap_assets_v1.begin_image_original_actor(
    p_actor_id uuid, p_task_id uuid, p_dataset_item_id uuid,
    p_source_snapshot text, p_object_key text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_current jsonb;
    v_source jsonb;
    v_item_id uuid;
    v_task_id uuid;
    v_message text;
BEGIN
    v_reply := aap_assets_v1.read_image_source(p_actor_id, p_task_id, p_dataset_item_id);
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_source := v_reply -> 'data';
    IF v_source ->> 'source_snapshot' IS DISTINCT FROM p_source_snapshot
       OR v_source ->> 'file_path' IS DISTINCT FROM p_object_key THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    v_item_id := (v_source ->> 'item_id')::uuid;
    v_task_id := CASE WHEN v_item_id IS NULL THEN (v_source ->> 'task_id')::uuid ELSE NULL END;
    IF v_item_id IS NOT NULL THEN
        PERFORM 1 FROM public.dataset_items AS item WHERE item.id = v_item_id FOR SHARE NOWAIT;
        IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'source_missing'); END IF;
    END IF;
    -- The item or Labeling pointer remains locked until admission commits.
    v_reply := aap_assets_v1._image_reply(
        aap_assets_v1._begin_media_original_read(v_item_id, v_task_id, p_object_key), NULL
    );
    v_current := aap_assets_v1.read_image_source(p_actor_id, p_task_id, p_dataset_item_id);
    IF v_current -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_source_authority_changed';
    END IF;
    IF v_current #>> '{data,source_snapshot}' IS DISTINCT FROM p_source_snapshot THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'media_original_unverified';
    END IF;
    RETURN v_reply;
EXCEPTION
    WHEN raise_exception THEN
        IF SQLERRM = 'image_source_authority_changed' THEN RETURN v_current; END IF;
        RAISE;
    WHEN lock_not_available THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
    WHEN SQLSTATE '55006' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
    WHEN check_violation THEN
        GET STACKED DIAGNOSTICS v_message = MESSAGE_TEXT;
        IF v_message = 'media_original_unverified' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
        ELSIF v_message IN ('fixture_run_closed', 'media_original_reclaim_claimed') THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
        END IF;
        RAISE;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_image_original_actor(uuid,uuid,uuid,text,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_image_original_worker(
    p_job_id uuid, p_generation bigint, p_lease_token uuid,
    p_source_snapshot text, p_object_key text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.asset_image_jobs%ROWTYPE;
    v_reply jsonb;
    v_current jsonb;
    v_source jsonb;
    v_item_id uuid;
    v_task_id uuid;
    v_message text;
BEGIN
    SELECT job.* INTO v_job FROM public.asset_image_jobs AS job
    WHERE job.job_id = p_job_id FOR SHARE NOWAIT;
    IF NOT FOUND OR p_generation IS NULL OR p_lease_token IS NULL
       OR v_job.status <> 'running' OR v_job.generation <> p_generation
       OR v_job.lease_token IS DISTINCT FROM p_lease_token
       OR v_job.lease_expires_at IS NULL OR v_job.lease_expires_at <= pg_catalog.clock_timestamp()
       OR v_job.owner_id IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    IF v_job.source_snapshot IS DISTINCT FROM p_source_snapshot
       OR v_job.source_facts ->> 'file_path' IS DISTINCT FROM p_object_key THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    v_reply := aap_assets_v1.resolve_internal_image_source(
        v_job.owner_kind, v_job.owner_id, v_job.source_snapshot, v_job.source_facts -> 'object_identity'
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_source := v_reply -> 'data';
    IF v_source ->> 'source_snapshot' IS DISTINCT FROM p_source_snapshot
       OR v_source ->> 'file_path' IS DISTINCT FROM p_object_key THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    v_item_id := (v_source ->> 'item_id')::uuid;
    v_task_id := CASE WHEN v_item_id IS NULL THEN (v_source ->> 'task_id')::uuid ELSE NULL END;
    IF v_item_id IS NOT NULL THEN
        PERFORM 1 FROM public.dataset_items AS item WHERE item.id = v_item_id FOR SHARE NOWAIT;
        IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'source_missing'); END IF;
    END IF;
    v_reply := aap_assets_v1._image_reply(
        aap_assets_v1._begin_media_original_read(v_item_id, v_task_id, p_object_key), NULL
    );
    v_current := aap_assets_v1.resolve_internal_image_source(
        v_job.owner_kind, v_job.owner_id, v_job.source_snapshot, v_job.source_facts -> 'object_identity'
    );
    IF v_current -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_source_authority_changed';
    END IF;
    IF v_current #>> '{data,source_snapshot}' IS DISTINCT FROM p_source_snapshot THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'media_original_unverified';
    END IF;
    RETURN v_reply;
EXCEPTION
    WHEN raise_exception THEN
        IF SQLERRM = 'image_source_authority_changed' THEN RETURN v_current; END IF;
        RAISE;
    WHEN lock_not_available THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
    WHEN SQLSTATE '55006' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
    WHEN check_violation THEN
        GET STACKED DIAGNOSTICS v_message = MESSAGE_TEXT;
        IF v_message = 'media_original_unverified' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
        ELSIF v_message IN ('fixture_run_closed', 'media_original_reclaim_claimed') THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
        END IF;
        RAISE;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_image_original_worker(uuid,bigint,uuid,text,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_image_original_read(p_creation_id uuid, p_operation_id uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT aap_assets_v1._finish_media_original_read(p_creation_id, p_operation_id) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    RETURN aap_assets_v1._image_reply(
        pg_catalog.jsonb_build_object('creation_id', p_creation_id, 'operation_id', p_operation_id), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_image_original_read(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_image_original_worker(p_creation_id uuid, p_operation_id uuid)
RETURNS jsonb LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT aap_assets_v1.finish_image_original_read(p_creation_id, p_operation_id)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_image_original_worker(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_image_original_system(
    p_owner_kind text, p_owner_id uuid, p_source_snapshot text, p_object_key text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_current jsonb;
    v_source jsonb;
    v_item_id uuid;
    v_task_id uuid;
    v_message text;
BEGIN
    v_reply := aap_assets_v1.resolve_internal_image_source(p_owner_kind, p_owner_id, NULL, NULL);
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_source := v_reply -> 'data';
    IF v_source ->> 'source_snapshot' IS DISTINCT FROM p_source_snapshot
       OR v_source ->> 'file_path' IS DISTINCT FROM p_object_key THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    v_item_id := (v_source ->> 'item_id')::uuid;
    v_task_id := CASE WHEN v_item_id IS NULL THEN (v_source ->> 'task_id')::uuid ELSE NULL END;
    IF v_item_id IS NOT NULL THEN
        PERFORM 1 FROM public.dataset_items AS item WHERE item.id = v_item_id FOR SHARE NOWAIT;
        IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'source_missing'); END IF;
    END IF;
    v_reply := aap_assets_v1._image_reply(
        aap_assets_v1._begin_media_original_read(v_item_id, v_task_id, p_object_key), NULL
    );
    v_current := aap_assets_v1.resolve_internal_image_source(p_owner_kind, p_owner_id, NULL, NULL);
    IF v_current -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_source_authority_changed';
    END IF;
    IF v_current #>> '{data,source_snapshot}' IS DISTINCT FROM p_source_snapshot THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'media_original_unverified';
    END IF;
    RETURN v_reply;
EXCEPTION
    WHEN raise_exception THEN
        IF SQLERRM = 'image_source_authority_changed' THEN RETURN v_current; END IF;
        RAISE;
    WHEN lock_not_available THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
    WHEN SQLSTATE '55006' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
    WHEN check_violation THEN
        GET STACKED DIAGNOSTICS v_message = MESSAGE_TEXT;
        IF v_message = 'media_original_unverified' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
        ELSIF v_message IN ('fixture_run_closed', 'media_original_reclaim_claimed') THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
        END IF;
        RAISE;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_image_original_system(text,uuid,text,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_image_original_system(p_creation_id uuid, p_operation_id uuid)
RETURNS jsonb LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT aap_assets_v1.finish_image_original_read(p_creation_id, p_operation_id)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_image_original_system(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_image_original_backfill(
    p_job_id uuid, p_generation bigint, p_lease_token uuid, p_batch_token uuid,
    p_owner_kind text, p_owner_id uuid, p_source_snapshot text, p_object_key text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.asset_image_jobs%ROWTYPE;
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
    RETURN aap_assets_v1.begin_image_original_system(
        p_owner_kind, p_owner_id, p_source_snapshot, p_object_key
    );
EXCEPTION
    WHEN lock_not_available THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_image_original_backfill(uuid,bigint,uuid,uuid,text,uuid,text,text) FROM PUBLIC;
