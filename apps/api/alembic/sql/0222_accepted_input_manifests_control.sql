-- Validate the Jobs projection through one Assets-owned boundary.
CREATE FUNCTION aap_assets_v1._accepted_image_input_manifest_job_scope(
    p_job_id uuid,
    p_actor_id uuid,
    p_project_id uuid,
    p_task_id uuid,
    p_selection jsonb,
    p_selection_digest text,
    p_require_active boolean
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_data jsonb;
    v_code text;
    v_operation text;
BEGIN
    IF p_require_active THEN
        v_operation := 'project_accepted_image_input_manifest_job';
        v_reply := aap_jobs_v1.project_accepted_image_input_manifest_job(
            p_job_id, p_task_id, p_actor_id, p_project_id,
            p_selection, p_selection_digest, true
        );
    ELSE
        v_operation := 'accepted_image_input_manifest_job_consistency';
        v_reply := aap_jobs_v1.accepted_image_input_manifest_job_consistency(
            p_job_id, p_task_id, p_actor_id, p_project_id,
            p_selection, p_selection_digest
        );
    END IF;
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-jobs-accepted-image-input-manifest/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_reply #>> '{error,code}';
        IF v_code IN ('invalid_request', 'manifest_scope_mismatch', 'job_not_active', 'job_busy') THEN
            RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, v_code);
        END IF;
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
    END IF;
    v_data := v_reply -> 'data';
    IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
       OR v_operation = 'project_accepted_image_input_manifest_job'
          AND ((v_data - ARRAY[
                'job_id', 'task_id', 'actor_id', 'project_id', 'status', 'active',
                'cancel_requested'
              ]) IS DISTINCT FROM '{}'
               OR NOT (v_data ?& ARRAY[
                    'job_id', 'task_id', 'actor_id', 'project_id', 'status', 'active',
                    'cancel_requested'
               ])
               OR v_data ->> 'job_id' IS DISTINCT FROM p_job_id::text
               OR v_data ->> 'task_id' IS DISTINCT FROM p_task_id::text
               OR v_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
               OR v_data ->> 'project_id' IS DISTINCT FROM p_project_id::text
               OR pg_catalog.jsonb_typeof(v_data -> 'status') IS DISTINCT FROM 'string'
               OR v_data ->> 'status' NOT IN ('pending', 'running', 'completed', 'failed', 'cancelled')
               OR pg_catalog.jsonb_typeof(v_data -> 'active') IS DISTINCT FROM 'boolean'
               OR pg_catalog.jsonb_typeof(v_data -> 'cancel_requested') IS DISTINCT FROM 'boolean'
               OR ((v_data ->> 'status') IN ('pending', 'running')
                   AND v_data -> 'cancel_requested' = 'false'::jsonb)
                    IS DISTINCT FROM (v_data -> 'active' = 'true'::jsonb))
       OR v_operation = 'accepted_image_input_manifest_job_consistency'
          AND ((v_data - ARRAY[
                'job_id', 'task_id', 'actor_id', 'project_id', 'exists',
                'consistent', 'active'
              ]) IS DISTINCT FROM '{}'
               OR NOT (v_data ?& ARRAY[
                    'job_id', 'task_id', 'actor_id', 'project_id', 'exists',
                    'consistent', 'active'
               ])
               OR v_data ->> 'job_id' IS DISTINCT FROM p_job_id::text
               OR v_data ->> 'task_id' IS DISTINCT FROM p_task_id::text
               OR v_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
               OR v_data ->> 'project_id' IS DISTINCT FROM p_project_id::text
               OR pg_catalog.jsonb_typeof(v_data -> 'exists') IS DISTINCT FROM 'boolean'
               OR pg_catalog.jsonb_typeof(v_data -> 'consistent') IS DISTINCT FROM 'boolean'
               OR pg_catalog.jsonb_typeof(v_data -> 'active') IS DISTINCT FROM 'boolean'
               OR (v_data -> 'active' = 'true'::jsonb
                   AND v_data -> 'exists' IS DISTINCT FROM 'true'::jsonb)) THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
    END IF;
    IF v_operation = 'accepted_image_input_manifest_job_consistency'
       AND v_data -> 'consistent' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
    END IF;
    IF p_require_active AND v_data -> 'active' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'job_not_active');
    END IF;
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(v_data, NULL);
EXCEPTION WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accepted_image_input_manifest_job_scope(
    uuid, uuid, uuid, uuid, jsonb, text, boolean
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._accepted_image_input_manifest_header(
    p_manifest public.asset_accepted_image_input_manifests
)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'job_id', (p_manifest).job_id,
        'actor_id', (p_manifest).actor_id,
        'project_id', (p_manifest).project_id,
        'selection', (p_manifest).selection,
        'selection_digest', (p_manifest).selection_digest,
        'candidate_count', (p_manifest).candidate_count,
        'selected_count', (p_manifest).selected_count,
        'prepared_count', (p_manifest).prepared_count,
        'status', (p_manifest).status,
        'ready_count', (p_manifest).ready_count,
        'failed_count', (p_manifest).failed_count,
        'sealed_digest', (p_manifest).sealed_digest,
        'created_at', (p_manifest).created_at,
        'sealed_at', (p_manifest).sealed_at
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accepted_image_input_manifest_header(
    public.asset_accepted_image_input_manifests
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._accepted_image_input_manifest_member(
    p_member public.asset_accepted_image_input_manifest_members,
    p_manifest public.asset_accepted_image_input_manifests
)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_input public.asset_accepted_image_inputs%ROWTYPE;
    v_state text := (p_member).state;
    v_error text := (p_member).error_code;
BEGIN
    IF (p_member).input_id IS NOT NULL THEN
        SELECT input.* INTO v_input
        FROM public.asset_accepted_image_inputs AS input
        WHERE input.input_id = (p_member).input_id;
        IF NOT FOUND OR (v_input.job_id, v_input.task_id, v_input.project_id,
                         v_input.actor_id)
            IS DISTINCT FROM ((p_member).job_id, (p_member).task_id,
                              (p_member).project_id, (p_manifest).actor_id) THEN
            RETURN NULL;
        END IF;
        IF (p_manifest).status = 'preparing' AND v_state = 'preparing' THEN
            IF v_input.status = 'ready' THEN
                v_state := 'ready';
                v_error := NULL;
            ELSIF v_input.status IN ('failed', 'expired') THEN
                v_state := 'failed';
                v_error := COALESCE(v_input.error_code, 'input_expired');
            ELSE
                v_state := 'preparing';
                v_error := NULL;
            END IF;
        END IF;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'ordinal', (p_member).ordinal,
        'task_id', (p_member).task_id,
        'project_id', (p_member).project_id,
        'task_version', (p_member).task_version,
        'task_source_generation', (p_member).task_source_generation,
        'dataset_item_id', (p_member).dataset_item_id,
        'task_source_snapshot', (p_member).task_source_snapshot,
        'file_path', (p_member).file_path,
        'file_type', (p_member).file_type,
        'source', (p_member).source,
        'state', v_state,
        'input', CASE WHEN (p_member).input_id IS NULL THEN NULL
                      ELSE aap_assets_v1._accepted_image_input_record(v_input) END,
        'error', v_error
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accepted_image_input_manifest_member(
    public.asset_accepted_image_input_manifest_members,
    public.asset_accepted_image_input_manifests
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._accepted_image_input_manifest_current_scope(
    p_actor_id uuid, p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_access jsonb;
    v_code text;
    v_project jsonb;
BEGIN
    IF p_actor_id IS NULL OR p_project_id IS NULL THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_request');
    END IF;
    v_reply := aap_assets_v1._image_actor(p_actor_id, false);
    IF v_reply IS NULL OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
       OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(
            NULL, COALESCE(v_reply #>> '{error,code}', 'invalid_response')
        );
    END IF;
    v_access := aap_workspace_v1.read_media_project_access(p_actor_id, p_project_id);
    IF v_access IS NULL
       OR v_access ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
       OR pg_catalog.jsonb_typeof(v_access -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
    END IF;
    IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_access #>> '{error,code}';
        IF v_code = 'project_missing' THEN
            v_code := 'task_not_visible';
        ELSIF v_code IN ('project_busy', 'membership_busy') THEN
            v_code := 'source_busy';
        END IF;
        IF v_code IN ('account_not_active', 'account_busy', 'invalid_account_role',
                      'task_not_visible', 'source_busy') THEN
            RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, v_code);
        END IF;
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
    END IF;
    SELECT view.project_config INTO v_project
    FROM aap_workspace_v1.project_catalog_configs AS view
    WHERE view.project_id = p_project_id;
    IF NOT FOUND OR pg_catalog.jsonb_typeof(v_project) IS DISTINCT FROM 'object' THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'task_not_visible');
    END IF;
    IF v_project ->> 'data_type' IS DISTINCT FROM 'image'
       OR v_project -> 'scene_mode' IS DISTINCT FROM 'false'::jsonb THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'unsupported_media');
    END IF;
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(
        pg_catalog.jsonb_build_object('actor_id', p_actor_id, 'project_id', p_project_id), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accepted_image_input_manifest_current_scope(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.capture_accepted_image_input_manifest(
    p_job_id uuid, p_actor_id uuid, p_project_id uuid, p_selection jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_job jsonb;
    v_existing public.asset_accepted_image_input_manifests%ROWTYPE;
    v_cursor_name text;
    v_cursor refcursor;
    v_row jsonb;
    v_source_reply jsonb;
    v_task_reply jsonb;
    v_source jsonb;
    v_task_source jsonb;
    v_kind text;
    v_error text;
    v_expected_ordinal bigint := 1;
    v_candidate_count bigint;
    v_selected_count bigint;
    v_prepared bigint := 0;
    v_digest text;
    v_hash_input text;
    v_path text;
    v_task_id uuid;
    v_item_id uuid;
    v_source_error text;
    v_task_scope_changed boolean;
    v_message text;
BEGIN
    IF p_job_id IS NULL OR p_actor_id IS NULL OR p_project_id IS NULL
       OR NOT aap_assets_v1._accepted_image_input_manifest_selection_valid(p_selection) THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_request');
    END IF;
    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-accepted-image-manifest:' || p_job_id::text, 0)
    ) THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_busy');
    END IF;
    v_reply := aap_assets_v1._accepted_image_input_manifest_current_scope(
        p_actor_id, p_project_id
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    SELECT manifest.* INTO v_existing
    FROM public.asset_accepted_image_input_manifests AS manifest
    WHERE manifest.job_id = p_job_id;
    IF FOUND THEN
        IF (v_existing.actor_id, v_existing.project_id, v_existing.selection)
            IS DISTINCT FROM (p_actor_id, p_project_id, p_selection) THEN
            RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
        END IF;
        v_reply := aap_assets_v1._accepted_image_input_manifest_job_scope(
            p_job_id, p_actor_id, p_project_id, NULL,
            v_existing.selection, v_existing.selection_digest, false
        );
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(
            aap_assets_v1._accepted_image_input_manifest_header(v_existing), NULL
        );
    END IF;
    v_reply := aap_assets_v1._accepted_image_input_manifest_job_scope(
        p_job_id, p_actor_id, p_project_id, NULL, p_selection, NULL, true
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_job := v_reply -> 'data';

    v_reply := aap_labeling_v1.open_accepted_image_input_manifest_members(
        p_actor_id, p_project_id, p_selection
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-accepted-image-input-manifest/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_error := v_reply #>> '{error,code}';
        IF v_error IN (
            'invalid_request', 'account_not_active', 'account_busy', 'invalid_account_role',
            'task_not_visible', 'source_busy', 'source_missing', 'unsupported_media'
        ) THEN
            RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, v_error);
        END IF;
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
    END IF;
    IF pg_catalog.jsonb_typeof(v_reply #> '{data,cursor}') IS DISTINCT FROM 'string' THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
    END IF;
    v_cursor_name := v_reply #>> '{data,cursor}';
    v_cursor := v_cursor_name::refcursor;
    FETCH v_cursor INTO v_row;
    IF v_row IS NULL OR pg_catalog.jsonb_typeof(v_row) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
    END IF;
    v_kind := v_row ->> 'kind';
    IF v_kind = 'error' THEN
        v_error := v_row ->> 'code';
        CLOSE v_cursor;
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, v_error);
    END IF;
    IF v_kind IS DISTINCT FROM 'header'
       OR pg_catalog.jsonb_typeof(v_row -> 'candidate_count') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_row -> 'selected_count') IS DISTINCT FROM 'number' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
    END IF;
    BEGIN
        v_candidate_count := (v_row ->> 'candidate_count')::bigint;
        v_selected_count := (v_row ->> 'selected_count')::bigint;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
    END;
    IF v_candidate_count < 0 OR v_selected_count < 0
       OR v_selected_count > v_candidate_count THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
    END IF;
    v_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
        pg_catalog.jsonb_build_object(
            'schema', 'aap-assets-input-manifest/v1',
            'job_id', p_job_id,
            'actor_id', p_actor_id,
            'project_id', p_project_id,
            'selection', p_selection,
            'candidate_count', v_candidate_count,
            'selected_count', v_selected_count
        )::text, 'UTF8'
    )), 'hex');

    LOOP
        FETCH v_cursor INTO v_row;
        EXIT WHEN NOT FOUND;
        IF pg_catalog.jsonb_typeof(v_row) IS DISTINCT FROM 'object'
           OR v_row ->> 'kind' IS DISTINCT FROM 'member'
           OR pg_catalog.jsonb_typeof(v_row -> 'ordinal') IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(v_row -> 'task_id') IS DISTINCT FROM 'string'
           OR v_row ->> 'project_id' IS DISTINCT FROM p_project_id::text
           OR v_row ->> 'file_type' IS DISTINCT FROM 'image'
           OR pg_catalog.jsonb_typeof(v_row -> 'file_path') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_row ->> 'file_path') > 1024 THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
        END IF;
        BEGIN
            v_task_id := (v_row ->> 'task_id')::uuid;
            v_item_id := CASE WHEN v_row -> 'dataset_item_id' = 'null'::jsonb
                THEN NULL ELSE (v_row ->> 'dataset_item_id')::uuid END;
        EXCEPTION WHEN invalid_text_representation THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
        END;
        IF (v_row ->> 'ordinal')::bigint IS DISTINCT FROM v_expected_ordinal THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
        END IF;
        v_source := NULL;
        v_source_error := NULL;
        v_task_scope_changed := false;
        v_task_source := NULL;
        IF v_item_id IS NULL
           AND NOT aap_assets_v1._accepted_image_input_manifest_path_valid(v_row ->> 'file_path') THEN
            v_source_error := 'source_missing';
        ELSE
            v_task_reply := aap_labeling_v1.read_image_task_source(v_task_id, p_actor_id, false);
        END IF;
        IF v_source_error IS NOT NULL THEN
            NULL;
        ELSIF v_task_reply IS NULL
           OR v_task_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-image-source/v1'
           OR v_task_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_error := COALESCE(v_task_reply #>> '{error,code}', 'invalid_response');
            IF v_error IN ('source_missing', 'source_changed', 'not_image') THEN
                v_source_error := CASE WHEN v_error = 'source_missing'
                    THEN 'source_missing' ELSE 'source_changed' END;
            ELSIF v_error = 'source_not_ready' THEN
                v_source_error := 'source_not_ready';
            ELSE
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:' || v_error;
            END IF;
        ELSE
            v_task_source := v_task_reply -> 'data';
            IF pg_catalog.jsonb_typeof(v_task_source) IS DISTINCT FROM 'object'
               OR v_task_source ->> 'task_id' IS DISTINCT FROM v_task_id::text
               OR v_task_source ->> 'project_id' IS DISTINCT FROM p_project_id::text THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
            END IF;
            IF v_task_source ->> 'source_snapshot' IS DISTINCT FROM v_row ->> 'task_source_snapshot'
               OR v_task_source ->> 'source_generation' IS DISTINCT FROM v_row ->> 'task_source_generation'
               OR v_task_source -> 'dataset_item_id' IS DISTINCT FROM v_row -> 'dataset_item_id'
               OR v_task_source ->> 'file_path' IS DISTINCT FROM v_row ->> 'file_path'
               OR v_task_source ->> 'file_type' IS DISTINCT FROM v_row ->> 'file_type' THEN
                v_source_error := 'task_scope_changed';
                v_task_scope_changed := true;
            ELSIF v_task_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb THEN
                v_source_error := 'source_not_ready';
            END IF;
            IF NOT v_task_scope_changed THEN
                v_source_reply := aap_assets_v1._image_owner_source(
                    p_actor_id, v_task_id, NULL, false
                );
                IF v_source_reply IS NULL
                   OR v_source_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1' THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
                END IF;
                IF v_source_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                    v_error := v_source_reply #>> '{error,code}';
                    IF v_error IN ('source_missing', 'source_changed', 'not_image', 'owner_changed') THEN
                        v_source_error := CASE WHEN v_error = 'source_missing'
                            THEN 'source_missing' ELSE 'source_changed' END;
                    ELSIF v_error = 'source_not_ready' THEN
                        v_source_error := 'source_not_ready';
                    ELSIF v_error = 'source_busy' THEN
                        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:source_busy';
                    ELSE
                        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:' || COALESCE(v_error, 'invalid_response');
                    END IF;
                ELSE
                    v_source := v_source_reply -> 'data';
                    IF NOT aap_assets_v1._image_source_data_valid(v_source)
                       OR v_source ->> 'task_id' IS DISTINCT FROM v_task_id::text
                       OR v_source ->> 'project_id' IS DISTINCT FROM p_project_id::text
                       OR v_source -> 'item_id' IS DISTINCT FROM v_row -> 'dataset_item_id'
                       OR (v_item_id IS NULL
                           AND v_source ->> 'file_path' IS DISTINCT FROM v_row ->> 'file_path')
                       OR v_source ->> 'file_type' IS DISTINCT FROM 'image' THEN
                        v_source_error := 'task_scope_changed';
                        v_source := NULL;
                    ELSIF v_source -> 'source_drifted' IS DISTINCT FROM 'false'::jsonb THEN
                        v_source_error := 'source_changed';
                    ELSIF v_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb THEN
                        v_source_error := 'source_not_ready';
                    END IF;
                END IF;
            END IF;
        END IF;
        IF v_task_source IS NOT NULL AND v_task_source -> 'dataset_item_id' <> 'null'::jsonb
           AND v_source IS NULL AND v_source_error IS NULL THEN
            v_source_error := 'source_missing';
        END IF;
        IF v_source_error IS NULL AND v_source IS NULL THEN
            v_source_error := 'source_changed';
        END IF;
        IF v_source_error IS NOT NULL THEN
            v_source_error := CASE
                WHEN v_source_error IN ('source_missing', 'source_changed', 'source_not_ready', 'task_scope_changed')
                    THEN v_source_error
                ELSE 'source_changed' END;
            v_prepared := v_prepared + 1;
        END IF;
        v_hash_input := pg_catalog.jsonb_build_object(
            'ordinal', v_expected_ordinal,
            'task_id', v_task_id,
            'project_id', p_project_id,
            'batch_id', v_row -> 'batch_id',
            'task_version', v_row -> 'task_version',
            'task_source_generation', v_row -> 'task_source_generation',
            'dataset_item_id', v_row -> 'dataset_item_id',
            'task_source_snapshot', v_row ->> 'task_source_snapshot',
            'file_path', v_row ->> 'file_path',
            'file_type', v_row ->> 'file_type',
            'source', v_source,
            'error', v_source_error
        )::text;
        v_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
            v_digest || ':' || v_hash_input, 'UTF8'
        )), 'hex');
        INSERT INTO public.asset_accepted_image_input_manifest_members (
            job_id, ordinal, task_id, project_id, batch_id, task_version,
            task_source_generation, dataset_item_id, task_source_snapshot,
            file_path, file_type, source, state, error_code
        ) VALUES (
            p_job_id, v_expected_ordinal, v_task_id, p_project_id,
            CASE WHEN v_row -> 'batch_id' = 'null'::jsonb THEN NULL
                 ELSE (v_row ->> 'batch_id')::uuid END,
            (v_row ->> 'task_version')::integer,
            (v_row ->> 'task_source_generation')::bigint,
            v_item_id,
            v_row ->> 'task_source_snapshot',
            v_row ->> 'file_path', 'image', v_source,
            CASE WHEN v_source_error IS NULL THEN 'unprepared' ELSE 'failed' END,
            v_source_error
        );
        v_expected_ordinal := v_expected_ordinal + 1;
    END LOOP;
    CLOSE v_cursor;
    IF v_expected_ordinal - 1 <> v_selected_count THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
    END IF;
    INSERT INTO public.asset_accepted_image_input_manifests (
        job_id, actor_id, project_id, selection, selection_digest,
        candidate_count, selected_count, prepared_count
    ) VALUES (
        p_job_id, p_actor_id, p_project_id, p_selection, v_digest,
        v_candidate_count, v_selected_count, v_prepared
    ) RETURNING * INTO v_existing;
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(
        aap_assets_v1._accepted_image_input_manifest_header(v_existing), NULL
    );
EXCEPTION WHEN SQLSTATE 'P0001' THEN
    GET STACKED DIAGNOSTICS v_message = MESSAGE_TEXT;
    IF v_cursor IS NOT NULL THEN
        BEGIN
            CLOSE v_cursor;
        EXCEPTION WHEN invalid_cursor_name THEN
            NULL;
        END;
    END IF;
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(
        NULL, COALESCE(NULLIF(pg_catalog.split_part(v_message, ':', 2), ''), 'invalid_response')
    );
WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.capture_accepted_image_input_manifest(uuid, uuid, uuid, jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._accepted_image_input_manifest_member_bound(
    p_job_id uuid, p_task_id uuid, p_actor_id uuid, p_project_id uuid,
    p_input_id uuid
)
RETURNS boolean
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1
        FROM public.asset_accepted_image_input_manifests AS manifest
        JOIN public.asset_accepted_image_input_manifest_members AS member
          ON member.job_id = manifest.job_id
        JOIN public.asset_accepted_image_inputs AS input
          ON input.input_id = member.input_id
        WHERE manifest.job_id = p_job_id
          AND manifest.actor_id = p_actor_id
          AND manifest.project_id = p_project_id
          AND member.task_id = p_task_id
          AND member.input_id = p_input_id
          AND member.state IN ('preparing', 'ready', 'failed')
          AND input.job_id = p_job_id
          AND input.task_id = p_task_id
          AND input.actor_id = p_actor_id
          AND input.project_id = p_project_id
          AND input.task_version = member.task_version
          AND input.task_source_generation = member.task_source_generation
          AND input.dataset_item_id IS NOT DISTINCT FROM member.dataset_item_id
          AND input.task_source_snapshot = member.task_source_snapshot
          AND input.source_owner_kind = member.source #>> '{owner,kind}'
          AND input.source_owner_id::text = member.source #>> '{owner,id}'
          AND input.source_generation::text = member.source ->> 'source_generation'
          AND input.source_snapshot = member.source ->> 'source_snapshot'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accepted_image_input_manifest_member_bound(
    uuid, uuid, uuid, uuid, uuid
) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1._accepted_image_input_job_scope(
    p_job_id uuid, p_task_id uuid, p_actor_id uuid, p_require_active boolean
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_data jsonb;
    v_code text;
    v_bulk boolean := false;
    v_selection jsonb;
    v_digest text;
BEGIN
    v_reply := aap_jobs_v1.project_accepted_image_input_job(
        p_job_id, p_task_id, p_actor_id, p_require_active
    );
    IF v_reply IS NOT NULL
       AND v_reply ->> 'schema' = 'aap-jobs-accepted-image-input/v1'
       AND v_reply -> 'ok' = 'false'::jsonb
       AND v_reply #>> '{error,code}' = 'input_scope_mismatch'
       AND EXISTS (
           SELECT 1 FROM public.asset_accepted_image_inputs AS input
           WHERE input.job_id = p_job_id AND input.task_id = p_task_id
             AND input.actor_id = p_actor_id
             AND aap_assets_v1._accepted_image_input_manifest_member_bound(
                 p_job_id, p_task_id, p_actor_id, input.project_id, input.input_id
             )
       ) THEN
        SELECT manifest.selection, manifest.selection_digest
        INTO v_selection, v_digest
        FROM public.asset_accepted_image_input_manifests AS manifest
        JOIN public.asset_accepted_image_inputs AS input
          ON input.job_id = manifest.job_id
         AND input.project_id = manifest.project_id
         AND input.actor_id = manifest.actor_id
        WHERE input.job_id = p_job_id AND input.task_id = p_task_id
          AND input.actor_id = p_actor_id;
        v_reply := aap_jobs_v1.project_accepted_image_input_manifest_job(
            p_job_id, p_task_id, p_actor_id,
            (SELECT input.project_id FROM public.asset_accepted_image_inputs AS input
             WHERE input.job_id = p_job_id AND input.task_id = p_task_id),
            v_selection, v_digest, p_require_active
        );
        v_bulk := true;
    END IF;
    IF v_reply IS NULL
       OR (v_bulk AND v_reply ->> 'schema' IS DISTINCT FROM 'aap-jobs-accepted-image-input-manifest/v1')
       OR (NOT v_bulk AND v_reply ->> 'schema' IS DISTINCT FROM 'aap-jobs-accepted-image-input/v1')
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_reply #>> '{error,code}';
        IF v_code IN ('invalid_request', 'input_scope_mismatch', 'manifest_scope_mismatch', 'job_not_active', 'job_busy') THEN
            RETURN aap_assets_v1._accepted_image_input_reply(
                NULL, CASE WHEN v_code = 'manifest_scope_mismatch'
                    THEN 'input_scope_mismatch' ELSE v_code END
            );
        END IF;
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
    END IF;
    v_data := v_reply -> 'data';
    IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
       OR (v_data - ARRAY[
            'job_id', 'task_id', 'actor_id', 'project_id', 'status', 'active',
            'cancel_requested'
       ]) IS DISTINCT FROM '{}'
       OR NOT (v_data ?& ARRAY[
            'job_id', 'task_id', 'actor_id', 'project_id', 'status', 'active',
            'cancel_requested'
       ])
       OR v_data ->> 'job_id' IS DISTINCT FROM p_job_id::text
       OR v_data ->> 'task_id' IS DISTINCT FROM p_task_id::text
       OR v_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR pg_catalog.jsonb_typeof(v_data -> 'project_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_data -> 'status') IS DISTINCT FROM 'string'
       OR v_data ->> 'status' NOT IN ('pending', 'running', 'completed', 'failed', 'cancelled')
       OR pg_catalog.jsonb_typeof(v_data -> 'active') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_data -> 'cancel_requested') IS DISTINCT FROM 'boolean'
       OR ((v_data ->> 'status') IN ('pending', 'running')
           AND v_data -> 'cancel_requested' = 'false'::jsonb)
             IS DISTINCT FROM (v_data -> 'active' = 'true'::jsonb) THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
    END IF;
    IF p_require_active AND v_data -> 'active' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'job_not_active');
    END IF;
    RETURN aap_assets_v1._accepted_image_input_reply(v_data, NULL);
EXCEPTION WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accepted_image_input_job_scope(uuid, uuid, uuid, boolean)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1._accepted_image_input_job_consistency(
    p_job_id uuid, p_task_id uuid, p_actor_id uuid, p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_data jsonb;
    v_code text;
    v_bulk boolean := false;
    v_selection jsonb;
    v_digest text;
    v_input_id uuid;
BEGIN
    SELECT input.input_id INTO v_input_id
    FROM public.asset_accepted_image_inputs AS input
    WHERE input.job_id = p_job_id AND input.task_id = p_task_id
      AND input.actor_id = p_actor_id AND input.project_id = p_project_id;
    IF FOUND AND aap_assets_v1._accepted_image_input_manifest_member_bound(
        p_job_id, p_task_id, p_actor_id, p_project_id, v_input_id
    ) THEN
        SELECT manifest.selection, manifest.selection_digest
        INTO v_selection, v_digest
        FROM public.asset_accepted_image_input_manifests AS manifest
        WHERE manifest.job_id = p_job_id
          AND manifest.actor_id = p_actor_id
          AND manifest.project_id = p_project_id;
        v_reply := aap_jobs_v1.accepted_image_input_manifest_job_consistency(
            p_job_id, p_task_id, p_actor_id, p_project_id, v_selection, v_digest
        );
        v_bulk := true;
    ELSE
        v_reply := aap_jobs_v1.accepted_image_input_job_consistency(
            p_job_id, p_task_id, p_actor_id, p_project_id
        );
    END IF;
    IF v_reply IS NULL
       OR (v_bulk AND v_reply ->> 'schema' IS DISTINCT FROM 'aap-jobs-accepted-image-input-manifest/v1')
       OR (NOT v_bulk AND v_reply ->> 'schema' IS DISTINCT FROM 'aap-jobs-accepted-image-input/v1')
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_reply #>> '{error,code}';
        IF v_code = 'invalid_request' THEN
            RETURN aap_assets_v1._accepted_image_input_reply(NULL, v_code);
        END IF;
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
    END IF;
    v_data := v_reply -> 'data';
    IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
       OR (v_data - ARRAY[
            'job_id', 'task_id', 'actor_id', 'project_id', 'exists',
            'consistent', 'active'
       ]) IS DISTINCT FROM '{}'
       OR NOT (v_data ?& ARRAY[
            'job_id', 'task_id', 'actor_id', 'project_id', 'exists',
            'consistent', 'active'
       ])
       OR v_data ->> 'job_id' IS DISTINCT FROM p_job_id::text
       OR v_data ->> 'task_id' IS DISTINCT FROM p_task_id::text
       OR v_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR v_data ->> 'project_id' IS DISTINCT FROM p_project_id::text
       OR pg_catalog.jsonb_typeof(v_data -> 'exists') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_data -> 'consistent') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_data -> 'active') IS DISTINCT FROM 'boolean'
       OR (v_data -> 'active' = 'true'::jsonb
           AND (v_data -> 'exists' IS DISTINCT FROM 'true'::jsonb
                OR v_data -> 'consistent' IS DISTINCT FROM 'true'::jsonb)) THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
    END IF;
    RETURN aap_assets_v1._accepted_image_input_reply(v_data, NULL);
EXCEPTION WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accepted_image_input_job_consistency(uuid, uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.read_accepted_image_input_work(
    p_input_id uuid, p_job_id uuid, p_task_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_input public.asset_accepted_image_inputs%ROWTYPE;
    v_manifest_status text;
    v_job_reply jsonb;
    v_job jsonb;
    v_retained_reply jsonb;
    v_retained jsonb;
    v_attempt public.asset_accepted_image_input_attempts%ROWTYPE;
    v_blob jsonb;
BEGIN
    IF p_input_id IS NULL OR p_job_id IS NULL OR p_task_id IS NULL THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_request');
    END IF;
    SELECT input.* INTO v_input
    FROM public.asset_accepted_image_inputs AS input
    WHERE input.input_id = p_input_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_missing');
    END IF;
    IF (v_input.job_id, v_input.task_id) IS DISTINCT FROM (p_job_id, p_task_id) THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_scope_mismatch');
    END IF;
    SELECT manifest.status INTO v_manifest_status
    FROM public.asset_accepted_image_input_manifest_members AS member
    JOIN public.asset_accepted_image_input_manifests AS manifest
      ON manifest.job_id = member.job_id
    WHERE member.input_id = p_input_id
      AND manifest.actor_id = v_input.actor_id
      AND manifest.project_id = v_input.project_id;
    IF FOUND AND v_manifest_status <> 'sealed' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_not_ready');
    END IF;
    IF v_input.status = 'expired' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_expired');
    ELSIF v_input.status = 'failed' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, v_input.error_code);
    ELSIF v_input.status <> 'ready' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_not_ready');
    END IF;
    v_job_reply := aap_assets_v1._accepted_image_input_job_consistency(
        p_job_id, p_task_id, v_input.actor_id, v_input.project_id
    );
    IF v_job_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_job_reply; END IF;
    v_job := v_job_reply -> 'data';
    v_retained_reply := aap_labeling_v1.accepted_image_input_referenced(p_input_id);
    IF v_retained_reply IS NULL
       OR v_retained_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-accepted-input-retention/v1'
       OR v_retained_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_retained_reply #> '{data,referenced}') IS DISTINCT FROM 'boolean'
       OR v_retained_reply #>> '{data,input_id}' IS DISTINCT FROM p_input_id::text THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
    END IF;
    v_retained := v_retained_reply #> '{data,referenced}';
    IF v_job -> 'consistent' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_scope_mismatch');
    END IF;
    IF v_job -> 'active' IS DISTINCT FROM 'true'::jsonb
       AND v_retained IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_scope_mismatch');
    END IF;
    SELECT attempt.* INTO v_attempt
    FROM public.asset_accepted_image_input_attempts AS attempt
    WHERE attempt.input_id = p_input_id
      AND attempt.status = 'ready'
      AND attempt.prefix = pg_catalog.regexp_replace(v_input.blob_key, 'source$', '')
    ORDER BY attempt.generation DESC
    LIMIT 1;
    IF NOT FOUND OR v_input.blob_version_id IS NULL
       OR v_input.blob_version_id = 'null'
       OR v_input.byte_size IS NULL OR v_input.sha256 IS NULL THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
    END IF;
    v_blob := pg_catalog.jsonb_build_object(
        'key', v_input.blob_key, 'version_id', v_input.blob_version_id,
        'etag', v_input.blob_etag, 'byte_size', v_input.byte_size,
        'sha256', v_input.sha256
    );
    RETURN aap_assets_v1._accepted_image_input_reply(
        pg_catalog.jsonb_build_object(
            'input', aap_assets_v1._accepted_image_input_record(v_input),
            'source', v_input.source_facts, 'blob', v_blob
        ), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_accepted_image_input_work(uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_accepted_image_input_manifest(
    p_job_id uuid, p_actor_id uuid, p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_manifest public.asset_accepted_image_input_manifests%ROWTYPE;
    v_reply jsonb;
    v_job jsonb;
BEGIN
    IF p_job_id IS NULL OR p_actor_id IS NULL OR p_project_id IS NULL THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_request');
    END IF;
    v_reply := aap_assets_v1._accepted_image_input_manifest_current_scope(
        p_actor_id, p_project_id
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    SELECT manifest.* INTO v_manifest
    FROM public.asset_accepted_image_input_manifests AS manifest
    WHERE manifest.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_missing');
    END IF;
    IF (v_manifest.actor_id, v_manifest.project_id)
        IS DISTINCT FROM (p_actor_id, p_project_id) THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
    END IF;
    v_reply := aap_assets_v1._accepted_image_input_manifest_job_scope(
        p_job_id, p_actor_id, p_project_id, NULL,
        v_manifest.selection, v_manifest.selection_digest, false
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_job := v_reply -> 'data';
    IF v_job -> 'consistent' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
    END IF;
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(
        aap_assets_v1._accepted_image_input_manifest_header(v_manifest), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_accepted_image_input_manifest(uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_accepted_image_input_manifest_page(
    p_job_id uuid,
    p_actor_id uuid,
    p_project_id uuid,
    p_selection_digest text,
    p_sealed_digest text,
    p_cursor bigint,
    p_page_size integer
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_manifest public.asset_accepted_image_input_manifests%ROWTYPE;
    v_member public.asset_accepted_image_input_manifest_members%ROWTYPE;
    v_member_json jsonb;
    v_members jsonb := '[]'::jsonb;
    v_candidate_members jsonb;
    v_candidate_reply jsonb;
    v_reply jsonb;
    v_job jsonb;
    v_count integer := 0;
    v_last bigint;
    v_next_cursor bigint;
BEGIN
    IF p_job_id IS NULL OR p_actor_id IS NULL OR p_project_id IS NULL
       OR p_selection_digest IS NULL OR p_selection_digest !~ '^[0-9a-f]{64}$'
       OR (p_sealed_digest IS NOT NULL AND p_sealed_digest !~ '^[0-9a-f]{64}$')
       OR p_cursor IS NULL OR p_cursor < 0
       OR p_page_size IS NULL OR p_page_size NOT BETWEEN 1 AND 100 THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_request');
    END IF;
    v_reply := aap_assets_v1._accepted_image_input_manifest_current_scope(
        p_actor_id, p_project_id
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    SELECT manifest.* INTO v_manifest
    FROM public.asset_accepted_image_input_manifests AS manifest
    WHERE manifest.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_missing');
    END IF;
    IF (v_manifest.actor_id, v_manifest.project_id)
        IS DISTINCT FROM (p_actor_id, p_project_id)
       OR v_manifest.selection_digest IS DISTINCT FROM p_selection_digest
       OR p_cursor > v_manifest.selected_count THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(
            NULL, CASE WHEN p_cursor > v_manifest.selected_count
                THEN 'manifest_cursor_invalid' ELSE 'manifest_scope_mismatch' END
        );
    END IF;
    IF p_sealed_digest IS NOT NULL
       AND (v_manifest.status <> 'sealed'
            OR v_manifest.sealed_digest IS DISTINCT FROM p_sealed_digest) THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
    END IF;
    v_reply := aap_assets_v1._accepted_image_input_manifest_job_scope(
        p_job_id, p_actor_id, p_project_id, NULL,
        v_manifest.selection, v_manifest.selection_digest, false
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_job := v_reply -> 'data';
    IF v_job -> 'consistent' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
    END IF;
    IF v_manifest.status = 'preparing'
       AND v_job -> 'active' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'job_not_active');
    END IF;
    IF p_cursor = v_manifest.selected_count THEN
        v_next_cursor := NULL;
    ELSE
        FOR v_member IN
            SELECT member.*
            FROM public.asset_accepted_image_input_manifest_members AS member
            WHERE member.job_id = p_job_id AND member.ordinal > p_cursor
            ORDER BY member.ordinal
            LIMIT p_page_size
        LOOP
            IF v_member.ordinal IS DISTINCT FROM p_cursor + v_count + 1 THEN
                RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
            END IF;
            v_member_json := aap_assets_v1._accepted_image_input_manifest_member(
                v_member, v_manifest
            );
            IF v_member_json IS NULL THEN
                RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
            END IF;
            v_candidate_members := v_members || pg_catalog.jsonb_build_array(v_member_json);
            v_candidate_reply := aap_assets_v1._accepted_image_input_manifest_reply(
                pg_catalog.jsonb_build_object(
                    'manifest', aap_assets_v1._accepted_image_input_manifest_header(v_manifest),
                    'cursor', p_cursor,
                    'next_cursor', CASE
                        WHEN v_member.ordinal < v_manifest.selected_count THEN v_member.ordinal
                        ELSE NULL END,
                    'members', v_candidate_members
                ), NULL
            );
            IF pg_catalog.octet_length(v_candidate_reply::text) > 2097152 THEN
                IF v_count = 0 THEN
                    RETURN aap_assets_v1._accepted_image_input_manifest_reply(
                        NULL, 'resource_budget_exceeded'
                    );
                END IF;
                EXIT;
            END IF;
            v_members := v_candidate_members;
            v_count := v_count + 1;
            v_last := v_member.ordinal;
        END LOOP;
        IF v_count = 0 THEN
            RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
        END IF;
        v_next_cursor := CASE WHEN v_last < v_manifest.selected_count THEN v_last ELSE NULL END;
    END IF;
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(
        pg_catalog.jsonb_build_object(
            'manifest', aap_assets_v1._accepted_image_input_manifest_header(v_manifest),
            'cursor', p_cursor,
            'next_cursor', v_next_cursor,
            'members', v_members
        ), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_accepted_image_input_manifest_page(
    uuid, uuid, uuid, text, text, bigint, integer
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.prepare_accepted_image_input_manifest_member(
    p_job_id uuid,
    p_actor_id uuid,
    p_project_id uuid,
    p_selection_digest text,
    p_ordinal bigint,
    p_task_id uuid,
    p_source_snapshot text,
    p_object_identity jsonb,
    p_terminal_error text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_manifest public.asset_accepted_image_input_manifests%ROWTYPE;
    v_member public.asset_accepted_image_input_manifest_members%ROWTYPE;
    v_current_task jsonb;
    v_source_reply jsonb;
    v_source jsonb;
    v_observed jsonb;
    v_reply jsonb;
    v_job jsonb;
    v_error text;
    v_code text;
    v_identity jsonb;
    v_has_identity boolean;
    v_has_error boolean;
    v_input_id uuid;
    v_owner_kind text;
    v_owner_id uuid;
    v_capture_batch uuid;
    v_select_batch uuid;
BEGIN
    v_has_identity := p_object_identity IS NOT NULL
        AND p_object_identity <> 'null'::jsonb;
    v_has_error := p_terminal_error IS NOT NULL;
    IF p_job_id IS NULL OR p_actor_id IS NULL OR p_project_id IS NULL
       OR p_selection_digest IS NULL OR p_selection_digest !~ '^[0-9a-f]{64}$'
       OR p_ordinal IS NULL OR p_ordinal <= 0 OR p_task_id IS NULL
       OR v_has_identity = v_has_error
       OR (v_has_identity AND (
            p_source_snapshot IS NULL OR p_source_snapshot !~ '^[0-9a-f]{64}$'
            OR NOT aap_assets_v1._media_object_identity_valid(p_object_identity)
       ))
       OR (v_has_error AND p_terminal_error NOT IN (
            'source_missing', 'source_changed', 'source_not_ready', 'not_image',
            'task_scope_changed', 'input_expired', 'input_corrupt',
            'resource_budget_exceeded'
       )) THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_request');
    END IF;
    v_reply := aap_assets_v1._accepted_image_input_manifest_current_scope(
        p_actor_id, p_project_id
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    SELECT manifest.* INTO v_manifest
    FROM public.asset_accepted_image_input_manifests AS manifest
    WHERE manifest.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_missing');
    END IF;
    IF (v_manifest.actor_id, v_manifest.project_id, v_manifest.selection_digest)
        IS DISTINCT FROM (p_actor_id, p_project_id, p_selection_digest) THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
    END IF;
    v_reply := aap_assets_v1._accepted_image_input_manifest_job_scope(
        p_job_id, p_actor_id, p_project_id, p_task_id,
        v_manifest.selection, v_manifest.selection_digest, false
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_job := v_reply -> 'data';
    BEGIN
        SELECT manifest.* INTO v_manifest
        FROM public.asset_accepted_image_input_manifests AS manifest
        WHERE manifest.job_id = p_job_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_busy');
    END;
    IF NOT FOUND
       OR (v_manifest.actor_id, v_manifest.project_id, v_manifest.selection_digest)
            IS DISTINCT FROM (p_actor_id, p_project_id, p_selection_digest) THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
    END IF;
    BEGIN
        SELECT member.* INTO v_member
        FROM public.asset_accepted_image_input_manifest_members AS member
        WHERE member.job_id = p_job_id
          AND member.ordinal = p_ordinal
          AND member.task_id = p_task_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'task_scope_changed');
    END IF;
    IF v_member.state <> 'unprepared' THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(
            pg_catalog.jsonb_build_object(
                'job_id', p_job_id, 'actor_id', p_actor_id, 'project_id', p_project_id,
                'selection_digest', p_selection_digest,
                'member', aap_assets_v1._accepted_image_input_manifest_member(v_member, v_manifest)
            ), NULL
        );
    END IF;
    IF v_manifest.status <> 'preparing' THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
    END IF;
    v_reply := aap_assets_v1._accepted_image_input_manifest_job_scope(
        p_job_id, p_actor_id, p_project_id, p_task_id,
        v_manifest.selection, v_manifest.selection_digest, true
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    IF v_member.source IS NULL THEN
        IF v_has_identity OR p_source_snapshot IS NOT NULL THEN
            RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
        END IF;
    ELSIF p_source_snapshot IS NOT NULL
       AND p_source_snapshot IS DISTINCT FROM v_member.source ->> 'source_snapshot' THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
    ELSIF v_has_identity AND p_source_snapshot IS DISTINCT FROM v_member.source ->> 'source_snapshot' THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
    END IF;

    v_select_batch := CASE WHEN v_manifest.selection -> 'batch_id' = 'null'::jsonb
        THEN NULL ELSE (v_manifest.selection ->> 'batch_id')::uuid END;
    v_reply := aap_labeling_v1.lock_accepted_image_input_manifest_member(
        p_actor_id, p_project_id, p_task_id, v_member.batch_id,
        v_select_batch, v_manifest.selection ->> 'predict_mode'
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-accepted-image-input-manifest/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_reply #>> '{error,code}';
        IF v_code IN ('account_not_active', 'account_busy', 'invalid_account_role',
                      'task_not_visible', 'source_busy') THEN
            RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, v_code);
        ELSIF v_code NOT IN ('source_missing', 'task_scope_changed') THEN
            RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
        END IF;
        v_error := CASE WHEN v_code = 'source_missing'
            THEN 'source_missing' ELSE 'task_scope_changed' END;
    ELSE
        v_current_task := v_reply -> 'data';
        IF pg_catalog.jsonb_typeof(v_current_task) IS DISTINCT FROM 'object'
           OR v_current_task ->> 'task_id' IS DISTINCT FROM p_task_id::text
           OR v_current_task ->> 'project_id' IS DISTINCT FROM p_project_id::text
           OR v_current_task ->> 'task_source_generation' IS DISTINCT FROM v_member.task_source_generation::text
           OR v_current_task ->> 'task_source_snapshot' IS DISTINCT FROM v_member.task_source_snapshot
           OR v_current_task ->> 'dataset_item_id' IS DISTINCT FROM v_member.dataset_item_id::text
           OR v_current_task ->> 'file_path' IS DISTINCT FROM v_member.file_path
           OR v_current_task ->> 'file_type' IS DISTINCT FROM v_member.file_type THEN
            v_error := 'task_scope_changed';
        END IF;
    END IF;

    IF v_error IS NULL AND v_member.source IS NOT NULL THEN
        v_source_reply := aap_assets_v1._image_owner_source(
            p_actor_id, p_task_id, NULL, false
        );
        IF v_source_reply IS NULL
           OR v_source_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
           OR pg_catalog.jsonb_typeof(v_source_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
        END IF;
        IF v_source_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_code := v_source_reply #>> '{error,code}';
            IF v_code IN ('source_busy', 'account_not_active', 'account_busy', 'invalid_account_role', 'task_not_visible') THEN
                RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, v_code);
            ELSIF v_code = 'source_missing' THEN
                v_error := 'source_missing';
            ELSIF v_code IN ('source_changed', 'not_image', 'owner_changed') THEN
                v_error := 'source_changed';
            ELSIF v_code = 'source_not_ready' THEN
                v_error := 'source_not_ready';
            ELSE
                RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
            END IF;
        ELSE
            v_source := v_source_reply -> 'data';
            IF NOT aap_assets_v1._image_source_data_valid(v_source)
               OR v_source #>> '{owner,kind}' IS DISTINCT FROM v_member.source #>> '{owner,kind}'
               OR v_source #>> '{owner,id}' IS DISTINCT FROM v_member.source #>> '{owner,id}'
               OR v_source ->> 'source_generation' IS DISTINCT FROM v_member.source ->> 'source_generation'
               OR v_source ->> 'source_snapshot' IS DISTINCT FROM v_member.source ->> 'source_snapshot'
               OR v_source ->> 'file_path' IS DISTINCT FROM v_member.source ->> 'file_path'
               OR v_source ->> 'project_id' IS DISTINCT FROM p_project_id::text
               OR v_source ->> 'item_id' IS DISTINCT FROM v_member.dataset_item_id::text THEN
                v_error := 'source_changed';
            ELSIF v_source -> 'source_drifted' IS DISTINCT FROM 'false'::jsonb THEN
                v_error := 'source_changed';
            ELSIF v_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb THEN
                v_error := 'source_not_ready';
            END IF;
        END IF;
    END IF;
    IF v_error IS NULL AND v_has_error THEN
        v_error := p_terminal_error;
    END IF;

    IF v_error IS NULL AND v_has_identity THEN
        v_identity := aap_assets_v1._image_normalize_identity(p_object_identity);
        v_owner_kind := v_member.source #>> '{owner,kind}';
        BEGIN
            v_owner_id := (v_member.source #>> '{owner,id}')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
        END;
        v_reply := aap_assets_v1.observe_image_source(
            p_actor_id, p_task_id, NULL,
            v_member.source ->> 'source_snapshot', v_identity
        );
        IF v_reply IS NULL OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
           OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
        END IF;
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_code := v_reply #>> '{error,code}';
            IF v_code IN ('source_busy', 'account_not_active', 'account_busy', 'invalid_account_role', 'task_not_visible') THEN
                RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, v_code);
            ELSIF v_code = 'source_missing' THEN
                v_error := 'source_missing';
            ELSIF v_code IN ('source_changed', 'not_image', 'owner_changed') THEN
                v_error := 'source_changed';
            ELSIF v_code = 'source_not_ready' THEN
                v_error := 'source_not_ready';
            ELSE
                RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
            END IF;
        ELSE
            v_source := v_reply -> 'data';
            IF NOT aap_assets_v1._image_source_data_valid(v_source)
               OR v_source #>> '{owner,kind}' IS DISTINCT FROM v_owner_kind
               OR v_source #>> '{owner,id}' IS DISTINCT FROM v_owner_id::text
               OR v_source ->> 'source_snapshot' IS DISTINCT FROM v_member.source ->> 'source_snapshot'
               OR v_source ->> 'source_generation' IS DISTINCT FROM v_member.source ->> 'source_generation'
               OR v_source -> 'source_drifted' IS DISTINCT FROM 'false'::jsonb
               OR v_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb
               OR aap_assets_v1._image_normalize_identity(v_source -> 'object_identity')
                    IS DISTINCT FROM v_identity THEN
                v_error := 'source_changed';
            END IF;
        END IF;
    END IF;

    IF v_error IS NOT NULL THEN
        UPDATE public.asset_accepted_image_input_manifest_members AS member
        SET state = 'failed', error_code = v_error, updated_at = pg_catalog.clock_timestamp()
        WHERE member.job_id = p_job_id AND member.ordinal = p_ordinal
          AND member.task_id = p_task_id AND member.state = 'unprepared'
        RETURNING * INTO v_member;
        IF NOT FOUND THEN
            RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_busy');
        END IF;
        UPDATE public.asset_accepted_image_input_manifests AS manifest
        SET prepared_count = manifest.prepared_count + 1
        WHERE manifest.job_id = p_job_id
          AND manifest.status = 'preparing'
          AND manifest.selection_digest = p_selection_digest
          AND manifest.prepared_count < manifest.selected_count
        RETURNING * INTO v_manifest;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:manifest_scope_mismatch';
        END IF;
    ELSE
        IF v_source IS NULL OR v_identity IS NULL
           OR v_source -> 'object_identity' = 'null'::jsonb
           OR v_source -> 'source_drifted' IS DISTINCT FROM 'false'::jsonb
           OR v_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
        END IF;
        v_input_id := pg_catalog.gen_random_uuid();
        INSERT INTO public.asset_accepted_image_inputs (
            input_id, job_id, task_id, project_id, actor_id,
            task_version, task_source_generation, dataset_item_id,
            task_source_snapshot, source_owner_kind, source_owner_id,
            source_generation, source_snapshot, source_identity, source_facts
        ) VALUES (
            v_input_id, p_job_id, p_task_id, p_project_id, p_actor_id,
            v_member.task_version, v_member.task_source_generation, v_member.dataset_item_id,
            v_member.task_source_snapshot, v_source #>> '{owner,kind}',
            (v_source #>> '{owner,id}')::uuid,
            (v_source ->> 'source_generation')::bigint,
            v_source ->> 'source_snapshot', v_identity, v_source
        );
        UPDATE public.asset_accepted_image_input_manifest_members AS member
        SET state = 'preparing', input_id = v_input_id,
            error_code = NULL, updated_at = pg_catalog.clock_timestamp()
        WHERE member.job_id = p_job_id AND member.ordinal = p_ordinal
          AND member.task_id = p_task_id AND member.state = 'unprepared'
        RETURNING * INTO v_member;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:manifest_busy';
        END IF;
        UPDATE public.asset_accepted_image_input_manifests AS manifest
        SET prepared_count = manifest.prepared_count + 1
        WHERE manifest.job_id = p_job_id
          AND manifest.status = 'preparing'
          AND manifest.selection_digest = p_selection_digest
          AND manifest.prepared_count < manifest.selected_count
        RETURNING * INTO v_manifest;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:manifest_scope_mismatch';
        END IF;
    END IF;
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(
        pg_catalog.jsonb_build_object(
            'job_id', p_job_id, 'actor_id', p_actor_id, 'project_id', p_project_id,
            'selection_digest', p_selection_digest,
            'member', aap_assets_v1._accepted_image_input_manifest_member(v_member, v_manifest)
        ), NULL
    );
EXCEPTION WHEN SQLSTATE 'P0001' THEN
    GET STACKED DIAGNOSTICS v_code = MESSAGE_TEXT;
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(
        NULL, COALESCE(NULLIF(pg_catalog.split_part(v_code, ':', 2), ''), 'invalid_response')
    );
WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.prepare_accepted_image_input_manifest_member(
    uuid, uuid, uuid, text, bigint, uuid, text, jsonb, text
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.seal_accepted_image_input_manifest(
    p_job_id uuid, p_actor_id uuid, p_project_id uuid, p_selection_digest text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_manifest public.asset_accepted_image_input_manifests%ROWTYPE;
    v_member public.asset_accepted_image_input_manifest_members%ROWTYPE;
    v_input public.asset_accepted_image_inputs%ROWTYPE;
    v_reply jsonb;
    v_job jsonb;
    v_ready bigint := 0;
    v_failed bigint := 0;
    v_count bigint := 0;
    v_expected bigint := 1;
    v_digest text;
    v_state text;
    v_error text;
    v_byte_size bigint;
    v_sha text;
    v_sealed_at timestamptz;
BEGIN
    IF p_job_id IS NULL OR p_actor_id IS NULL OR p_project_id IS NULL
       OR p_selection_digest IS NULL OR p_selection_digest !~ '^[0-9a-f]{64}$' THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_request');
    END IF;
    v_reply := aap_assets_v1._accepted_image_input_manifest_current_scope(
        p_actor_id, p_project_id
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    SELECT manifest.* INTO v_manifest
    FROM public.asset_accepted_image_input_manifests AS manifest
    WHERE manifest.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_missing');
    END IF;
    IF (v_manifest.actor_id, v_manifest.project_id, v_manifest.selection_digest)
        IS DISTINCT FROM (p_actor_id, p_project_id, p_selection_digest) THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
    END IF;
    v_reply := aap_assets_v1._accepted_image_input_manifest_job_scope(
        p_job_id, p_actor_id, p_project_id, NULL,
        v_manifest.selection, v_manifest.selection_digest, false
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_job := v_reply -> 'data';
    IF v_job -> 'consistent' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
    END IF;
    IF v_manifest.status = 'sealed' THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(
            aap_assets_v1._accepted_image_input_manifest_header(v_manifest), NULL
        );
    END IF;
    IF v_job -> 'active' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'job_not_active');
    END IF;
    BEGIN
        SELECT manifest.* INTO v_manifest
        FROM public.asset_accepted_image_input_manifests AS manifest
        WHERE manifest.job_id = p_job_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_busy');
    END;
    IF NOT FOUND OR v_manifest.status <> 'preparing'
       OR (v_manifest.actor_id, v_manifest.project_id, v_manifest.selection_digest)
            IS DISTINCT FROM (p_actor_id, p_project_id, p_selection_digest) THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
    END IF;
    IF v_manifest.prepared_count <> v_manifest.selected_count THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_not_ready');
    END IF;

    -- First pass is read-only. A not-ready member cannot leave a partially sealed
    -- outcome or partially published counters behind.
    FOR v_member IN
        SELECT member.*
        FROM public.asset_accepted_image_input_manifest_members AS member
        WHERE member.job_id = p_job_id
        ORDER BY member.ordinal
    LOOP
        IF v_member.ordinal IS DISTINCT FROM v_expected THEN
            RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
        END IF;
        v_expected := v_expected + 1;
        v_count := v_count + 1;
        IF v_member.state = 'unprepared' THEN
            RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_not_ready');
        ELSIF v_member.state = 'failed' THEN
            v_failed := v_failed + 1;
        ELSIF v_member.state = 'ready' THEN
            IF v_member.outcome_byte_size IS NULL OR v_member.outcome_sha256 IS NULL THEN
                RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
            END IF;
            v_ready := v_ready + 1;
        ELSIF v_member.state = 'preparing' THEN
            SELECT input.* INTO v_input
            FROM public.asset_accepted_image_inputs AS input
            WHERE input.input_id = v_member.input_id;
            IF NOT FOUND THEN
                v_failed := v_failed + 1;
            ELSIF v_input.status = 'ready' THEN
                IF v_input.byte_size IS NULL OR v_input.sha256 IS NULL THEN
                    RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
                END IF;
                v_ready := v_ready + 1;
            ELSIF v_input.status IN ('failed', 'expired') THEN
                v_failed := v_failed + 1;
            ELSE
                RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_not_ready');
            END IF;
        ELSE
            RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
        END IF;
    END LOOP;
    IF v_count <> v_manifest.selected_count THEN
        RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
    END IF;

    v_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
        pg_catalog.jsonb_build_object(
            'schema', 'aap-assets-input-manifest/v1',
            'job_id', p_job_id,
            'actor_id', p_actor_id,
            'project_id', p_project_id,
            'selection_digest', p_selection_digest,
            'selected_count', v_manifest.selected_count
        )::text, 'UTF8'
    )), 'hex');
    v_ready := 0;
    v_failed := 0;
    v_expected := 1;
    FOR v_member IN
        SELECT member.*
        FROM public.asset_accepted_image_input_manifest_members AS member
        WHERE member.job_id = p_job_id
        ORDER BY member.ordinal
    LOOP
        v_state := v_member.state;
        v_error := v_member.error_code;
        v_byte_size := NULL;
        v_sha := NULL;
        IF v_state = 'preparing' THEN
            SELECT input.* INTO v_input
            FROM public.asset_accepted_image_inputs AS input
            WHERE input.input_id = v_member.input_id;
            IF NOT FOUND THEN
                v_state := 'failed';
                v_error := 'input_expired';
                UPDATE public.asset_accepted_image_input_manifest_members AS member
                SET state = 'failed', input_id = NULL, error_code = v_error,
                    outcome_byte_size = NULL, outcome_sha256 = NULL,
                    updated_at = pg_catalog.clock_timestamp()
                WHERE member.job_id = p_job_id AND member.ordinal = v_member.ordinal;
            ELSIF v_input.status = 'ready' THEN
                v_state := 'ready';
                v_error := NULL;
                v_byte_size := v_input.byte_size;
                v_sha := v_input.sha256;
                UPDATE public.asset_accepted_image_input_manifest_members AS member
                SET state = 'ready', error_code = NULL,
                    outcome_byte_size = v_byte_size, outcome_sha256 = v_sha,
                    updated_at = pg_catalog.clock_timestamp()
                WHERE member.job_id = p_job_id AND member.ordinal = v_member.ordinal;
            ELSE
                v_state := 'failed';
                v_error := COALESCE(v_input.error_code, 'input_expired');
                UPDATE public.asset_accepted_image_input_manifest_members AS member
                SET state = 'failed', error_code = v_error,
                    outcome_byte_size = NULL, outcome_sha256 = NULL,
                    updated_at = pg_catalog.clock_timestamp()
                WHERE member.job_id = p_job_id AND member.ordinal = v_member.ordinal;
            END IF;
        ELSIF v_state = 'ready' THEN
            v_byte_size := v_member.outcome_byte_size;
            v_sha := v_member.outcome_sha256;
        END IF;
        IF v_state = 'ready' THEN
            v_ready := v_ready + 1;
        ELSIF v_state = 'failed' THEN
            v_failed := v_failed + 1;
        ELSE
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
        END IF;
        v_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
            v_digest || ':' || pg_catalog.jsonb_build_object(
                'ordinal', v_member.ordinal,
                'task_id', v_member.task_id,
                'state', v_state,
                'input_id', CASE WHEN v_state = 'failed' AND v_error = 'input_expired'
                    AND v_member.input_id IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM public.asset_accepted_image_inputs AS input
                        WHERE input.input_id = v_member.input_id
                    ) THEN NULL ELSE v_member.input_id END,
                'byte_size', v_byte_size,
                'sha256', v_sha,
                'error', v_error
            )::text, 'UTF8'
        )), 'hex');
        v_expected := v_expected + 1;
    END LOOP;
    IF v_ready + v_failed <> v_manifest.selected_count THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
    END IF;
    v_sealed_at := GREATEST(pg_catalog.clock_timestamp(), v_manifest.created_at);
    UPDATE public.asset_accepted_image_input_manifests AS manifest
    SET status = 'sealed', ready_count = v_ready, failed_count = v_failed,
        sealed_digest = v_digest, sealed_at = v_sealed_at
    WHERE manifest.job_id = p_job_id
      AND manifest.status = 'preparing'
      AND manifest.selection_digest = p_selection_digest
    RETURNING * INTO v_manifest;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:manifest_scope_mismatch';
    END IF;
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(
        aap_assets_v1._accepted_image_input_manifest_header(v_manifest), NULL
    );
EXCEPTION WHEN SQLSTATE 'P0001' THEN
    GET STACKED DIAGNOSTICS v_error = MESSAGE_TEXT;
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(
        NULL, COALESCE(NULLIF(pg_catalog.split_part(v_error, ':', 2), ''), 'invalid_response')
    );
WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.seal_accepted_image_input_manifest(uuid, uuid, uuid, text)
    FROM PUBLIC;
-- aap-statement-break
-- JSON null is a valid direct Task source binding.
CREATE OR REPLACE FUNCTION aap_assets_v1.lock_accepted_image_input(
    p_input_id uuid, p_task_id uuid, p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_input public.asset_accepted_image_inputs%ROWTYPE;
    v_reply jsonb;
    v_job jsonb;
    v_task jsonb;
    v_source jsonb;
BEGIN
    IF p_input_id IS NULL OR p_task_id IS NULL OR p_project_id IS NULL THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT input.* INTO v_input
        FROM public.asset_accepted_image_inputs AS input
        WHERE input.input_id = p_input_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_missing');
    END IF;
    IF (v_input.task_id, v_input.project_id) IS DISTINCT FROM (p_task_id, p_project_id) THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_scope_mismatch');
    END IF;
    IF v_input.status = 'expired' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_expired');
    ELSIF v_input.status <> 'ready' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_not_ready');
    END IF;
    v_reply := aap_assets_v1._accepted_image_input_job_consistency(
        v_input.job_id, p_task_id, v_input.actor_id, p_project_id
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_job := v_reply -> 'data';
    IF v_job -> 'consistent' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_scope_mismatch');
    END IF;
    v_reply := aap_assets_v1._accepted_image_input_task_scope(p_task_id, NULL, true);
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_task := v_reply -> 'data';
    IF (v_task ->> 'project_id')::uuid IS DISTINCT FROM p_project_id
       OR (v_task ->> 'task_source_generation')::bigint IS DISTINCT FROM v_input.task_source_generation
       OR v_task ->> 'task_source_snapshot' IS DISTINCT FROM v_input.task_source_snapshot
       OR v_task -> 'dataset_item_id' IS DISTINCT FROM COALESCE(pg_catalog.to_jsonb(v_input.dataset_item_id), 'null'::jsonb) THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'source_changed');
    END IF;
    v_reply := aap_assets_v1._accepted_image_input_source_scope(NULL, p_task_id, true);
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_source := v_reply -> 'data';
    IF NOT aap_assets_v1._image_source_data_valid(v_source)
       OR v_source ->> 'project_id' IS DISTINCT FROM p_project_id::text
       OR v_source #>> '{owner,kind}' IS DISTINCT FROM v_input.source_owner_kind
       OR v_source #>> '{owner,id}' IS DISTINCT FROM v_input.source_owner_id::text
       OR v_source ->> 'source_generation' IS DISTINCT FROM v_input.source_generation::text
       OR v_source ->> 'source_snapshot' IS DISTINCT FROM v_input.source_snapshot
       OR v_source -> 'source_drifted' IS DISTINCT FROM 'false'::jsonb
       OR v_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb
       OR v_source -> 'object_identity' = 'null'::jsonb
       OR aap_assets_v1._image_normalize_identity(v_source -> 'object_identity')
            IS DISTINCT FROM v_input.source_identity THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'source_changed');
    END IF;
    UPDATE public.asset_accepted_image_inputs AS input
    SET retire_after = NULL,
        retention_checked_at = NULL,
        updated_at = pg_catalog.clock_timestamp()
    WHERE input.input_id = p_input_id;
    v_input.retire_after := NULL;
    RETURN aap_assets_v1._accepted_image_input_reply(
        aap_assets_v1._accepted_image_input_record(v_input), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'source_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.lock_accepted_image_input(uuid, uuid, uuid) FROM PUBLIC;
