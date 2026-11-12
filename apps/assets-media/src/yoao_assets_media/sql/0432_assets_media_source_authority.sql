-- Separate control and worker capabilities preserve the existing Media role
-- boundary; each admitted operation uses the durable storage-operation ledger.
CREATE FUNCTION aap_assets_v1.begin_media_original_actor(
    p_actor_id uuid, p_dataset_item_id uuid, p_task_id uuid,
    p_source_snapshot text, p_object_key text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_current jsonb;
    v_item public.dataset_items%ROWTYPE;
    v_source jsonb;
    v_message text;
BEGIN
    v_reply := aap_assets_v1.read_media_source(p_actor_id, p_dataset_item_id, p_task_id);
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    SELECT item.* INTO v_item FROM public.dataset_items AS item
    WHERE item.id = p_dataset_item_id FOR SHARE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._media_reply(NULL, 'source_missing'); END IF;
    v_source := aap_assets_v1._media_source_json(v_item);
    IF v_source ->> 'source_snapshot' IS DISTINCT FROM p_source_snapshot
       OR v_source ->> 'file_path' IS DISTINCT FROM p_object_key THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
    END IF;
    v_reply := aap_assets_v1._media_reply(
        aap_assets_v1._begin_media_original_read(p_dataset_item_id, NULL, p_object_key), NULL
    );
    v_current := aap_assets_v1.read_media_source(p_actor_id, p_dataset_item_id, p_task_id);
    IF v_current -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'media_source_authority_changed';
    END IF;
    IF v_current #>> '{data,source_snapshot}' IS DISTINCT FROM p_source_snapshot THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'media_original_unverified';
    END IF;
    RETURN v_reply;
EXCEPTION
    WHEN raise_exception THEN
        IF SQLERRM = 'media_source_authority_changed' THEN RETURN v_current; END IF;
        RAISE;
    WHEN lock_not_available THEN RETURN aap_assets_v1._media_reply(NULL, 'source_busy');
    WHEN SQLSTATE '55006' THEN RETURN aap_assets_v1._media_reply(NULL, 'source_busy');
    WHEN check_violation THEN
        GET STACKED DIAGNOSTICS v_message = MESSAGE_TEXT;
        IF v_message = 'media_original_unverified' THEN
            RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
        ELSIF v_message IN ('fixture_run_closed', 'media_original_reclaim_claimed') THEN
            RETURN aap_assets_v1._media_reply(NULL, 'source_busy');
        END IF;
        RAISE;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_media_original_actor(uuid,uuid,uuid,text,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_media_original_worker(
    p_job_id uuid, p_generation bigint, p_lease_token uuid,
    p_source_snapshot text, p_object_key text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.asset_media_jobs%ROWTYPE;
    v_item public.dataset_items%ROWTYPE;
    v_source jsonb;
    v_message text;
BEGIN
    SELECT job.* INTO v_job FROM public.asset_media_jobs AS job
    WHERE job.job_id = p_job_id FOR SHARE NOWAIT;
    IF NOT FOUND OR p_generation IS NULL OR p_lease_token IS NULL
       OR v_job.status <> 'running' OR v_job.generation <> p_generation
       OR v_job.lease_token IS DISTINCT FROM p_lease_token
       OR v_job.lease_expires_at IS NULL OR v_job.lease_expires_at <= pg_catalog.clock_timestamp()
       OR v_job.dataset_item_id IS NULL THEN
        RETURN aap_assets_v1._media_reply(NULL, 'lease_lost');
    END IF;
    SELECT item.* INTO v_item FROM public.dataset_items AS item
    WHERE item.id = v_job.dataset_item_id FOR SHARE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._media_reply(NULL, 'source_missing'); END IF;
    v_source := aap_assets_v1._media_source_json(v_item);
    IF v_source ->> 'source_snapshot' IS DISTINCT FROM p_source_snapshot
       OR v_source ->> 'file_path' IS DISTINCT FROM p_object_key
       OR v_item.media_source_generation IS DISTINCT FROM v_job.source_generation THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
    END IF;
    RETURN aap_assets_v1._media_reply(
        aap_assets_v1._begin_media_original_read(v_item.id, NULL, p_object_key), NULL
    );
EXCEPTION
    WHEN lock_not_available THEN RETURN aap_assets_v1._media_reply(NULL, 'source_busy');
    WHEN SQLSTATE '55006' THEN RETURN aap_assets_v1._media_reply(NULL, 'source_busy');
    WHEN check_violation THEN
        GET STACKED DIAGNOSTICS v_message = MESSAGE_TEXT;
        IF v_message = 'media_original_unverified' THEN
            RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
        ELSIF v_message IN ('fixture_run_closed', 'media_original_reclaim_claimed') THEN
            RETURN aap_assets_v1._media_reply(NULL, 'source_busy');
        END IF;
        RAISE;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_media_original_worker(uuid,bigint,uuid,text,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_media_original_read(p_creation_id uuid, p_operation_id uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT aap_assets_v1._finish_media_original_read(p_creation_id, p_operation_id) THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
    END IF;
    RETURN aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object('creation_id', p_creation_id, 'operation_id', p_operation_id), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_media_original_read(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_media_original_worker(p_creation_id uuid, p_operation_id uuid)
RETURNS jsonb LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT aap_assets_v1.finish_media_original_read(p_creation_id, p_operation_id)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_media_original_worker(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_media_original_system(
    p_dataset_item_id uuid, p_source_snapshot text, p_object_key text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item public.dataset_items%ROWTYPE;
    v_source jsonb;
    v_message text;
BEGIN
    SELECT item.* INTO v_item FROM public.dataset_items AS item
    WHERE item.id = p_dataset_item_id FOR SHARE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._media_reply(NULL, 'source_missing'); END IF;
    v_source := aap_assets_v1._media_source_json(v_item);
    IF v_source ->> 'source_snapshot' IS DISTINCT FROM p_source_snapshot
       OR v_source ->> 'file_path' IS DISTINCT FROM p_object_key THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
    END IF;
    RETURN aap_assets_v1._media_reply(
        aap_assets_v1._begin_media_original_read(v_item.id, NULL, p_object_key), NULL
    );
EXCEPTION
    WHEN lock_not_available THEN RETURN aap_assets_v1._media_reply(NULL, 'source_busy');
    WHEN SQLSTATE '55006' THEN RETURN aap_assets_v1._media_reply(NULL, 'source_busy');
    WHEN check_violation THEN
        GET STACKED DIAGNOSTICS v_message = MESSAGE_TEXT;
        IF v_message = 'media_original_unverified' THEN
            RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
        ELSIF v_message IN ('fixture_run_closed', 'media_original_reclaim_claimed') THEN
            RETURN aap_assets_v1._media_reply(NULL, 'source_busy');
        END IF;
        RAISE;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_media_original_system(uuid,text,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_media_original_system(p_creation_id uuid, p_operation_id uuid)
RETURNS jsonb LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT aap_assets_v1.finish_media_original_read(p_creation_id, p_operation_id)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_media_original_system(uuid,uuid) FROM PUBLIC;
