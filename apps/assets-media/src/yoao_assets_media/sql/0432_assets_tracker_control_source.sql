CREATE FUNCTION aap_assets_v1.begin_tracker_prepare_original(
    p_actor_id uuid, p_tracker_job_id uuid, p_task_id uuid, p_input_id uuid,
    p_request_digest text, p_source_snapshot text, p_object_key text
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_current jsonb; v_data jsonb; v_result jsonb; v_message text;
BEGIN
    v_current := aap_assets_v1.read_video_tracker_input(p_actor_id, p_tracker_job_id, p_task_id);
    IF v_current -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_current; END IF;
    v_data := v_current -> 'data';
    IF v_data #>> '{record,input_id}' IS DISTINCT FROM p_input_id::text
       OR v_data #>> '{record,scope,request_digest}' IS DISTINCT FROM p_request_digest
       OR v_data #>> '{record,status}' IS DISTINCT FROM 'pending'
       OR v_data #>> '{source_facts,source_snapshot}' IS DISTINCT FROM p_source_snapshot
       OR v_data #>> '{source_facts,file_path}' IS DISTINCT FROM p_object_key THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL, 'input_scope_mismatch');
    END IF;
    v_result := aap_assets_v1._begin_media_original_read(
        (v_data #>> '{source_facts,dataset_item_id}')::uuid, NULL, p_object_key
    );
    v_current := aap_assets_v1.read_video_tracker_input(p_actor_id, p_tracker_job_id, p_task_id);
    IF v_current -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'tracker_source_authority_changed';
    END IF;
    IF v_current -> 'data' IS DISTINCT FROM v_data THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'media_original_unverified';
    END IF;
    RETURN aap_assets_v1._video_tracker_input_reply(v_result, NULL);
EXCEPTION
    WHEN raise_exception THEN
        IF SQLERRM = 'tracker_source_authority_changed' THEN RETURN v_current; END IF;
        RAISE;
    WHEN lock_not_available THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL, 'source_busy');
    WHEN SQLSTATE '55006' THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL, 'source_busy');
    WHEN check_violation THEN
        GET STACKED DIAGNOSTICS v_message = MESSAGE_TEXT;
        IF v_message = 'media_original_unverified' THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL, 'source_changed');
        ELSIF v_message IN ('fixture_run_closed', 'media_original_reclaim_claimed') THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL, 'source_busy'); END IF;
        RAISE;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_tracker_prepare_original(uuid,uuid,uuid,uuid,text,text,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_tracker_control_original(
    p_actor_id uuid, p_tracker_job_id uuid, p_task_id uuid, p_input_id uuid,
    p_request_digest text, p_plan_digest text, p_source_snapshot text, p_object_key text
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_current jsonb; v_data jsonb; v_result jsonb; v_message text;
BEGIN
    v_current := aap_assets_v1.reference_video_tracker_source(
        p_actor_id, p_tracker_job_id, p_task_id, p_input_id, p_request_digest, p_plan_digest
    );
    IF v_current -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_current; END IF;
    v_data := v_current -> 'data';
    IF v_data #>> '{source_facts,source_snapshot}' IS DISTINCT FROM p_source_snapshot
       OR v_data #>> '{source_facts,file_path}' IS DISTINCT FROM p_object_key THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL, 'source_changed');
    END IF;
    v_result := aap_assets_v1._begin_media_original_read(
        (v_data #>> '{source_facts,dataset_item_id}')::uuid, NULL, p_object_key
    );
    v_current := aap_assets_v1.reference_video_tracker_source(
        p_actor_id, p_tracker_job_id, p_task_id, p_input_id, p_request_digest, p_plan_digest
    );
    IF v_current -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'tracker_source_authority_changed';
    END IF;
    IF v_current -> 'data' IS DISTINCT FROM v_data THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'media_original_unverified';
    END IF;
    RETURN aap_assets_v1._video_tracker_input_reply(v_result, NULL);
EXCEPTION
    WHEN raise_exception THEN
        IF SQLERRM = 'tracker_source_authority_changed' THEN RETURN v_current; END IF;
        RAISE;
    WHEN lock_not_available THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL, 'source_busy');
    WHEN SQLSTATE '55006' THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL, 'source_busy');
    WHEN check_violation THEN
        GET STACKED DIAGNOSTICS v_message = MESSAGE_TEXT;
        IF v_message = 'media_original_unverified' THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL, 'source_changed');
        ELSIF v_message IN ('fixture_run_closed', 'media_original_reclaim_claimed') THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL, 'source_busy'); END IF;
        RAISE;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_tracker_control_original(uuid,uuid,uuid,uuid,text,text,text,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_tracker_control_original(p_creation_id uuid, p_operation_id uuid)
RETURNS jsonb LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT aap_assets_v1.finish_tracker_video_original(p_creation_id, p_operation_id)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_tracker_control_original(uuid,uuid) FROM PUBLIC;
