-- Manifest and tracker input workers share the retained input row but never each
-- other's claim capability. The caller's wrapper fixes p_owner_kind.
CREATE FUNCTION aap_assets_v1._begin_video_input_original(
    p_input_id uuid, p_generation bigint, p_lease_token uuid, p_owner_kind text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_input public.asset_accepted_video_inputs%ROWTYPE;
    v_manifest public.asset_accepted_video_manifests%ROWTYPE;
    v_reply jsonb;
    v_code text;
    v_result jsonb;
    v_message text;
BEGIN
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
    WHERE input.input_id = p_input_id FOR SHARE NOWAIT;
    IF NOT FOUND OR p_generation IS NULL OR p_lease_token IS NULL
       OR v_input.input_owner IS DISTINCT FROM p_owner_kind
       OR v_input.status <> 'materializing' OR v_input.generation <> p_generation
       OR v_input.lease_token IS DISTINCT FROM p_lease_token
       OR v_input.lease_expires_at IS NULL OR v_input.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'lease_lost');
    END IF;
    IF p_owner_kind = 'manifest' THEN
        SELECT manifest.* INTO v_manifest FROM public.asset_accepted_video_manifests AS manifest
        WHERE manifest.job_id = v_input.job_id;
        IF NOT FOUND OR v_manifest.status <> 'preparing' THEN
            RETURN aap_assets_v1._video_input_reply(NULL, 'manifest_not_ready');
        END IF;
        v_reply := aap_assets_v1._video_input_job(v_manifest, v_input.task_id, true);
        v_code := aap_assets_v1._video_input_code(v_reply, 'aap-assets-video-input-execution/v1');
        IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL, v_code); END IF;
        v_code := aap_assets_v1._video_input_current_source(v_input, v_input.request_actor_id, true, true);
    ELSIF p_owner_kind = 'tracker' THEN
        v_code := aap_assets_v1._video_tracker_input_current_source(v_input, true, true);
    ELSE
        RETURN aap_assets_v1._video_input_reply(NULL, 'invalid_request');
    END IF;
    IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL, v_code); END IF;
    v_result := aap_assets_v1._begin_media_original_read(
        (v_input.source_facts ->> 'dataset_item_id')::uuid, NULL, v_input.source_facts ->> 'file_path'
    );
    IF p_owner_kind = 'manifest' THEN
        v_code := aap_assets_v1._video_input_current_source(v_input, v_input.request_actor_id, true, true);
    ELSE
        v_code := aap_assets_v1._video_tracker_input_current_source(v_input, true, true);
    END IF;
    IF v_code IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'video_input_source_authority_changed';
    END IF;
    RETURN aap_assets_v1._video_input_reply(v_result, NULL);
EXCEPTION
    WHEN raise_exception THEN
        IF SQLERRM = 'video_input_source_authority_changed' THEN
            RETURN aap_assets_v1._video_input_reply(NULL, v_code);
        END IF;
        RAISE;
    WHEN lock_not_available THEN RETURN aap_assets_v1._video_input_reply(NULL, 'source_busy');
    WHEN SQLSTATE '55006' THEN RETURN aap_assets_v1._video_input_reply(NULL, 'source_busy');
    WHEN check_violation THEN
        GET STACKED DIAGNOSTICS v_message = MESSAGE_TEXT;
        IF v_message = 'media_original_unverified' THEN
            RETURN aap_assets_v1._video_input_reply(NULL, 'source_changed');
        ELSIF v_message IN ('fixture_run_closed', 'media_original_reclaim_claimed') THEN
            RETURN aap_assets_v1._video_input_reply(NULL, 'source_busy');
        END IF;
        RAISE;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._begin_video_input_original(uuid,bigint,uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_accepted_video_original(p_input_id uuid, p_generation bigint, p_lease_token uuid)
RETURNS jsonb LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT aap_assets_v1._begin_video_input_original(p_input_id, p_generation, p_lease_token, 'manifest')
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_accepted_video_original(uuid,bigint,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_tracker_video_original(p_input_id uuid, p_generation bigint, p_lease_token uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_reply jsonb;
BEGIN
    v_reply := aap_assets_v1._begin_video_input_original(p_input_id, p_generation, p_lease_token, 'tracker');
    RETURN aap_assets_v1._video_tracker_input_reply(v_reply -> 'data', v_reply #>> '{error,code}');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_tracker_video_original(uuid,bigint,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_accepted_video_original(p_creation_id uuid, p_operation_id uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT aap_assets_v1._finish_media_original_read(p_creation_id, p_operation_id) THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'source_changed');
    END IF;
    RETURN aap_assets_v1._video_input_reply(
        pg_catalog.jsonb_build_object('creation_id', p_creation_id, 'operation_id', p_operation_id), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_accepted_video_original(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_tracker_video_original(p_creation_id uuid, p_operation_id uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT aap_assets_v1._finish_media_original_read(p_creation_id, p_operation_id) THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL, 'source_changed');
    END IF;
    RETURN aap_assets_v1._video_tracker_input_reply(
        pg_catalog.jsonb_build_object('creation_id', p_creation_id, 'operation_id', p_operation_id), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_tracker_video_original(uuid,uuid) FROM PUBLIC;
