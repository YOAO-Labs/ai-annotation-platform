-- Accepted input workers receive only the original bound to their live claim.
CREATE FUNCTION aap_assets_v1.begin_accepted_image_original(
    p_input_id uuid, p_generation bigint, p_lease_token uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_input public.asset_accepted_image_inputs%ROWTYPE;
    v_current jsonb;
    v_data jsonb;
    v_result jsonb;
    v_message text;
BEGIN
    SELECT input.* INTO v_input FROM public.asset_accepted_image_inputs AS input
    WHERE input.input_id = p_input_id FOR SHARE NOWAIT;
    IF NOT FOUND OR p_generation IS NULL OR p_lease_token IS NULL
       OR v_input.status <> 'materializing' OR v_input.generation <> p_generation
       OR v_input.lease_token IS DISTINCT FROM p_lease_token
       OR v_input.lease_expires_at IS NULL OR v_input.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'lease_lost');
    END IF;
    v_current := aap_assets_v1._accepted_image_input_job_scope(
        v_input.job_id, v_input.task_id, v_input.actor_id, true
    );
    IF v_current -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_current; END IF;
    v_current := aap_assets_v1._accepted_image_input_source_scope(NULL, v_input.task_id, false);
    IF v_current -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_current; END IF;
    v_data := v_current -> 'data';
    IF v_data ->> 'source_snapshot' IS DISTINCT FROM v_input.source_snapshot
       OR v_data ->> 'source_generation' IS DISTINCT FROM v_input.source_generation::text
       OR v_data #>> '{owner,kind}' IS DISTINCT FROM v_input.source_owner_kind
       OR v_data #>> '{owner,id}' IS DISTINCT FROM v_input.source_owner_id::text
       OR v_data ->> 'file_path' IS DISTINCT FROM v_input.source_facts ->> 'file_path'
       OR v_data -> 'source_drifted' IS DISTINCT FROM 'false'::jsonb
       OR v_data -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb
       OR aap_assets_v1._image_normalize_identity(v_data -> 'object_identity') IS DISTINCT FROM v_input.source_identity THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'source_changed');
    END IF;
    v_result := aap_assets_v1._begin_media_original_read(
        CASE WHEN v_input.source_owner_kind = 'dataset_item' THEN v_input.source_owner_id ELSE NULL END,
        CASE WHEN v_input.source_owner_kind = 'task' THEN v_input.source_owner_id ELSE NULL END,
        v_data ->> 'file_path'
    );
    v_current := aap_assets_v1._accepted_image_input_source_scope(NULL, v_input.task_id, false);
    IF v_current -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'input_source_authority_changed';
    END IF;
    IF v_current #>> '{data,source_snapshot}' IS DISTINCT FROM v_input.source_snapshot
       OR v_current #> '{data,object_identity}' IS DISTINCT FROM v_data -> 'object_identity' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'media_original_unverified';
    END IF;
    RETURN aap_assets_v1._accepted_image_input_reply(v_result, NULL);
EXCEPTION
    WHEN raise_exception THEN
        IF SQLERRM = 'input_source_authority_changed' THEN RETURN v_current; END IF;
        RAISE;
    WHEN lock_not_available THEN RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'source_busy');
    WHEN SQLSTATE '55006' THEN RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'source_busy');
    WHEN check_violation THEN
        GET STACKED DIAGNOSTICS v_message = MESSAGE_TEXT;
        IF v_message = 'media_original_unverified' THEN
            RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'source_changed');
        ELSIF v_message IN ('fixture_run_closed', 'media_original_reclaim_claimed') THEN
            RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'source_busy');
        END IF;
        RAISE;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_accepted_image_original(uuid,bigint,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_accepted_image_original(p_creation_id uuid, p_operation_id uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT aap_assets_v1._finish_media_original_read(p_creation_id, p_operation_id) THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'source_changed');
    END IF;
    RETURN aap_assets_v1._accepted_image_input_reply(
        pg_catalog.jsonb_build_object('creation_id', p_creation_id, 'operation_id', p_operation_id), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_accepted_image_original(uuid,uuid) FROM PUBLIC;
