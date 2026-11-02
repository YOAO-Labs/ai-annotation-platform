CREATE OR REPLACE FUNCTION aap_assets_v1._image_accept_source(
    p_operation_id uuid,
    p_actor_id uuid,
    p_task_id uuid,
    p_dataset_item_id uuid,
    p_purpose text,
    p_expected_source_snapshot text,
    p_policy jsonb,
    p_force boolean,
    p_action text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_policy jsonb;
    v_source_reply jsonb;
    v_source jsonb;
    v_access jsonb;
    v_spec jsonb;
    v_scope jsonb;
    v_intent jsonb;
    v_saved jsonb;
    v_result jsonb;
    v_data jsonb;
    v_job public.asset_image_jobs%ROWTYPE;
    v_projection jsonb;
    v_owner_kind text;
    v_owner_id uuid;
    v_slot text;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_force IS NULL
       OR p_action NOT IN ('request', 'retry')
       OR p_purpose NOT IN ('dimensions', 'thumbnail', 'pyramid')
       OR ((p_task_id IS NULL) = (p_dataset_item_id IS NULL))
       OR (p_expected_source_snapshot IS NOT NULL
           AND p_expected_source_snapshot !~ '^[0-9a-f]{64}$') THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_policy := aap_assets_v1._image_policy(COALESCE(p_policy, '{}'::jsonb));
    IF v_policy IS NULL THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    IF p_task_id IS NULL THEN
        v_access := aap_assets_v1._image_manager_item(p_actor_id, p_dataset_item_id, true);
        IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_access; END IF;
    ELSE
        v_access := aap_assets_v1._image_actor(p_actor_id, true);
        IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_access; END IF;
    END IF;
    v_source_reply := aap_assets_v1._image_owner_source(
        p_actor_id, p_task_id, p_dataset_item_id, true
    );
    IF v_source_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_source_reply; END IF;
    v_source := v_source_reply -> 'data';
    IF NOT aap_assets_v1._image_source_data_valid(v_source) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    v_spec := pg_catalog.jsonb_build_object(
        'kind', p_purpose,
        'expected_source_snapshot', v_source -> 'source_snapshot',
        'policy', v_policy
    );
    v_spec := aap_assets_v1._image_spec(v_spec);
    IF v_spec IS NULL THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_response'); END IF;
    v_scope := pg_catalog.jsonb_build_object(
        'task_id', p_task_id,
        'dataset_item_id', p_dataset_item_id,
        'purpose', p_purpose
    );
    v_intent := pg_catalog.jsonb_build_object(
        'expected_source_snapshot', p_expected_source_snapshot,
        'policy', v_policy,
        'force', p_force
    );
    v_saved := aap_assets_v1._image_read_operation(
        p_operation_id, p_actor_id, p_action, v_scope, v_intent
    );
    IF v_saved IS NOT NULL THEN
        IF v_saved ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
           OR pg_catalog.jsonb_typeof(v_saved -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_saved -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_saved; END IF;
        v_data := v_saved -> 'data';
        IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
           OR (v_data - ARRAY['operation_id', 'source', 'work', 'spec']) IS DISTINCT FROM '{}'::jsonb
           OR v_data ->> 'operation_id' IS DISTINCT FROM p_operation_id::text
           OR NOT aap_assets_v1._image_source_data_valid(v_data -> 'source')
           OR aap_assets_v1._image_spec(v_data -> 'spec') IS DISTINCT FROM v_data -> 'spec'
           OR pg_catalog.jsonb_typeof(v_data -> 'work') IS DISTINCT FROM 'object'
           OR v_data #>> '{spec,kind}' IS DISTINCT FROM p_purpose
           OR v_data #>> '{work,kind}' IS DISTINCT FROM p_purpose
           OR v_data #>> '{spec,expected_source_snapshot}' IS DISTINCT FROM v_data #>> '{source,source_snapshot}'
           OR v_data #> '{spec,policy}' IS DISTINCT FROM v_policy
           OR v_data #>> '{work,owner,kind}' IS DISTINCT FROM v_data #>> '{source,owner,kind}'
           OR v_data #>> '{work,owner,id}' IS DISTINCT FROM v_data #>> '{source,owner,id}'
           OR v_data #>> '{work,source_generation}' IS DISTINCT FROM v_data #>> '{source,source_generation}' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_data #>> '{source,owner,kind}' IS DISTINCT FROM v_source #>> '{owner,kind}'
           OR v_data #>> '{source,owner,id}' IS DISTINCT FROM v_source #>> '{owner,id}'
           OR v_data #>> '{source,source_snapshot}' IS DISTINCT FROM v_source ->> 'source_snapshot'
           OR v_data #>> '{source,source_generation}' IS DISTINCT FROM v_source ->> 'source_generation' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
        END IF;
        RETURN v_saved;
    END IF;
    IF v_source -> 'source_drifted' = 'true'::jsonb THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    IF v_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb
       OR v_source -> 'object_identity' = 'null'::jsonb THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_not_ready');
    END IF;
    IF p_expected_source_snapshot IS NOT NULL
       AND v_source ->> 'source_snapshot' IS DISTINCT FROM p_expected_source_snapshot THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    IF p_purpose = 'pyramid'
       AND v_source -> 'width' <> 'null'::jsonb
       AND v_source -> 'height' <> 'null'::jsonb
       AND (v_source ->> 'width')::numeric * (v_source ->> 'height')::numeric
            < (v_policy ->> 'optional_pixels')::numeric THEN
        RETURN aap_assets_v1._image_reply(NULL, 'resource_limit');
    END IF;
    IF p_action = 'retry' THEN
        v_owner_kind := v_source #>> '{owner,kind}';
        v_owner_id := (v_source #>> '{owner,id}')::uuid;
        v_slot := CASE WHEN p_purpose = 'pyramid'
            THEN 'pyramid:' || (v_policy ->> 'profile_version') ELSE p_purpose END;
        IF p_purpose = 'dimensions'
           AND v_source -> 'width' <> 'null'::jsonb
           AND v_source -> 'height' <> 'null'::jsonb THEN
            RETURN aap_assets_v1._image_reply(NULL, 'already_ready');
        ELSIF p_purpose = 'thumbnail'
           AND v_source -> 'thumbnail_path' <> 'null'::jsonb
           AND v_source -> 'thumbnail_source_generation' = v_source -> 'source_generation' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'already_ready');
        ELSIF p_purpose = 'pyramid' THEN
            v_projection := aap_assets_v1._image_pyramid_projection(
                v_source, NULL, v_policy ->> 'profile_version'
            );
            IF v_projection IS NULL THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            IF v_projection ->> 'status' = 'ready' THEN
                RETURN aap_assets_v1._image_reply(NULL, 'already_ready');
            END IF;
            IF v_source -> 'width' <> 'null'::jsonb
               AND v_source -> 'height' <> 'null'::jsonb
               AND (v_source ->> 'width')::numeric * (v_source ->> 'height')::numeric
                    < (v_policy ->> 'optional_pixels')::numeric THEN
                RETURN aap_assets_v1._image_reply(NULL, 'not_eligible');
            END IF;
        END IF;
        SELECT job.* INTO v_job
        FROM public.asset_image_jobs AS job
        WHERE job.owner_kind = v_owner_kind
          AND job.owner_id = v_owner_id
          AND job.slot_key = v_slot;
        IF FOUND AND v_job.status = 'failed'
           AND v_job.source_generation = (v_source ->> 'source_generation')::bigint
           AND v_job.source_snapshot = v_source ->> 'source_snapshot'
           AND v_job.spec = v_spec
           AND pg_catalog.clock_timestamp() < COALESCE(v_job.completed_at, v_job.updated_at)
               + pg_catalog.make_interval(secs => (v_policy ->> 'retry_cooldown_seconds')::integer) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'retry_cooldown');
        END IF;
    END IF;
    v_result := aap_assets_v1._image_enqueue(
        p_operation_id, p_actor_id, v_source, v_spec, p_force
    );
    IF v_result ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
       OR pg_catalog.jsonb_typeof(v_result -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'image enqueue envelope mismatch';
    END IF;
    IF v_result -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_result; END IF;
    v_data := v_result -> 'data';
    IF v_data ->> 'operation_id' IS DISTINCT FROM p_operation_id::text
       OR NOT aap_assets_v1._image_source_data_valid(v_data -> 'source')
       OR v_data #>> '{source,owner,kind}' IS DISTINCT FROM v_source #>> '{owner,kind}'
       OR v_data #>> '{source,owner,id}' IS DISTINCT FROM v_source #>> '{owner,id}'
       OR v_data #>> '{source,source_snapshot}' IS DISTINCT FROM v_source ->> 'source_snapshot'
       OR v_data #>> '{spec,kind}' IS DISTINCT FROM p_purpose
       OR v_data #>> '{work,kind}' IS DISTINCT FROM p_purpose
       OR v_data #>> '{spec,expected_source_snapshot}' IS DISTINCT FROM v_source ->> 'source_snapshot'
       OR v_data #>> '{work,owner,kind}' IS DISTINCT FROM v_source #>> '{owner,kind}'
       OR v_data #>> '{work,owner,id}' IS DISTINCT FROM v_source #>> '{owner,id}'
       OR v_data #>> '{work,source_generation}' IS DISTINCT FROM v_source ->> 'source_generation' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'image enqueue result escaped request';
    END IF;
    v_saved := aap_assets_v1._image_write_operation(
        p_operation_id, p_actor_id, p_action, v_scope, v_intent, v_result
    );
    IF v_saved IS NULL OR v_saved IS DISTINCT FROM v_result THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'image operation receipt mismatch';
    END IF;
    RETURN v_saved;
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
WHEN unique_violation THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
DROP FUNCTION aap_assets_v1._image_owner_source_share(uuid, uuid, uuid, boolean);
-- aap-statement-break
DROP FUNCTION aap_assets_v1._image_labeling_task_share(uuid, uuid, boolean);
-- aap-statement-break
DROP FUNCTION aap_labeling_v1.read_image_task_source_share(uuid, uuid, boolean);
