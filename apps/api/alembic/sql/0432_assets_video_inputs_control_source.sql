CREATE FUNCTION aap_assets_v1.begin_video_member_original(
    p_job_id uuid, p_actor_id uuid, p_project_id uuid, p_selection_digest text,
    p_ordinal bigint, p_task_id uuid, p_source_snapshot text, p_object_key text
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_current jsonb; v_member jsonb; v_result jsonb; v_message text; v_code text; v_source jsonb;
    v_manifest public.asset_accepted_video_manifests%ROWTYPE;
BEGIN
    IF p_ordinal IS NULL OR p_ordinal < 1 THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'invalid_request');
    END IF;
    SELECT manifest.* INTO v_manifest FROM public.asset_accepted_video_manifests AS manifest
    WHERE manifest.job_id = p_job_id FOR SHARE NOWAIT;
    v_code := aap_assets_v1._video_input_manifest_scope(v_manifest, p_actor_id, p_project_id, true);
    IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL, v_code); END IF;
    IF v_manifest.status <> 'preparing' THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'manifest_not_ready');
    END IF;
    v_code := aap_assets_v1._video_input_code(
        aap_assets_v1._video_input_job(v_manifest, p_task_id, true),
        'aap-assets-video-input-execution/v1'
    );
    IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL, v_code); END IF;
    v_current := aap_assets_v1.read_accepted_video_manifest_page(
        p_job_id, p_actor_id, p_project_id, p_selection_digest, NULL, p_ordinal - 1, 1
    );
    IF v_current -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_current; END IF;
    v_member := v_current #> '{data,members,0}';
    IF v_member ->> 'ordinal' IS DISTINCT FROM p_ordinal::text
       OR v_member #>> '{task,task_id}' IS DISTINCT FROM p_task_id::text
       OR v_member ->> 'state' IS DISTINCT FROM 'unprepared'
       OR v_member #>> '{source,source_snapshot}' IS DISTINCT FROM p_source_snapshot
       OR v_member #>> '{source,file_path}' IS DISTINCT FROM p_object_key THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'manifest_scope_mismatch');
    END IF;
    v_source := aap_assets_v1.read_media_source(
        p_actor_id, (v_member #>> '{source,dataset_item_id}')::uuid, p_task_id
    );
    v_code := aap_assets_v1._video_input_code(v_source, 'aap-assets-media/v1');
    IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL, v_code); END IF;
    IF v_source #>> '{data,source_snapshot}' IS DISTINCT FROM p_source_snapshot
       OR v_source #>> '{data,file_path}' IS DISTINCT FROM p_object_key THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'source_changed');
    END IF;
    v_result := aap_assets_v1._begin_media_original_read(
        (v_member #>> '{source,dataset_item_id}')::uuid, NULL, p_object_key
    );
    v_current := aap_assets_v1.read_accepted_video_manifest_page(
        p_job_id, p_actor_id, p_project_id, p_selection_digest, NULL, p_ordinal - 1, 1
    );
    IF v_current -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'video_source_authority_changed';
    END IF;
    IF v_current #> '{data,members,0}' IS DISTINCT FROM v_member THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'media_original_unverified';
    END IF;
    RETURN aap_assets_v1._video_input_reply(v_result, NULL);
EXCEPTION
    WHEN raise_exception THEN
        IF SQLERRM = 'video_source_authority_changed' THEN RETURN v_current; END IF;
        RAISE;
    WHEN lock_not_available THEN RETURN aap_assets_v1._video_input_reply(NULL, 'source_busy');
    WHEN SQLSTATE '55006' THEN RETURN aap_assets_v1._video_input_reply(NULL, 'source_busy');
    WHEN check_violation THEN
        GET STACKED DIAGNOSTICS v_message = MESSAGE_TEXT;
        IF v_message = 'media_original_unverified' THEN RETURN aap_assets_v1._video_input_reply(NULL, 'source_changed');
        ELSIF v_message IN ('fixture_run_closed', 'media_original_reclaim_claimed') THEN RETURN aap_assets_v1._video_input_reply(NULL, 'source_busy'); END IF;
        RAISE;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_video_member_original(uuid,uuid,uuid,text,bigint,uuid,text,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_video_control_original(
    p_input_id uuid, p_job_id uuid, p_task_id uuid, p_actor_id uuid,
    p_plan_digest text, p_source_snapshot text, p_object_key text
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_current jsonb; v_data jsonb; v_result jsonb; v_message text;
BEGIN
    v_current := aap_assets_v1.read_accepted_video_input_work(p_input_id, p_job_id, p_task_id, p_actor_id);
    IF v_current -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_current; END IF;
    v_data := v_current -> 'data';
    IF v_data #>> '{input,status}' IS DISTINCT FROM 'ready'
       OR v_data #>> '{input,plan,plan_digest}' IS DISTINCT FROM p_plan_digest
       OR v_data #>> '{source,source_snapshot}' IS DISTINCT FROM p_source_snapshot
       OR v_data #>> '{source,file_path}' IS DISTINCT FROM p_object_key THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'input_scope_mismatch');
    END IF;
    v_result := aap_assets_v1._begin_media_original_read(
        (v_data #>> '{source,dataset_item_id}')::uuid, NULL, p_object_key
    );
    v_current := aap_assets_v1.read_accepted_video_input_work(p_input_id, p_job_id, p_task_id, p_actor_id);
    IF v_current -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'video_source_authority_changed';
    END IF;
    IF v_current -> 'data' IS DISTINCT FROM v_data THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'media_original_unverified';
    END IF;
    RETURN aap_assets_v1._video_input_reply(v_result, NULL);
EXCEPTION
    WHEN raise_exception THEN
        IF SQLERRM = 'video_source_authority_changed' THEN RETURN v_current; END IF;
        RAISE;
    WHEN lock_not_available THEN RETURN aap_assets_v1._video_input_reply(NULL, 'source_busy');
    WHEN SQLSTATE '55006' THEN RETURN aap_assets_v1._video_input_reply(NULL, 'source_busy');
    WHEN check_violation THEN
        GET STACKED DIAGNOSTICS v_message = MESSAGE_TEXT;
        IF v_message = 'media_original_unverified' THEN RETURN aap_assets_v1._video_input_reply(NULL, 'source_changed');
        ELSIF v_message IN ('fixture_run_closed', 'media_original_reclaim_claimed') THEN RETURN aap_assets_v1._video_input_reply(NULL, 'source_busy'); END IF;
        RAISE;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_video_control_original(uuid,uuid,uuid,uuid,text,text,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_video_control_original(p_creation_id uuid, p_operation_id uuid)
RETURNS jsonb LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT aap_assets_v1.finish_accepted_video_original(p_creation_id, p_operation_id)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_video_control_original(uuid,uuid) FROM PUBLIC;
