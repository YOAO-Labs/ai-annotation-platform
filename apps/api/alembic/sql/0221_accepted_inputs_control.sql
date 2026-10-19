-- Strict Assets-native envelope and narrow cross-owner response validators.
CREATE FUNCTION aap_assets_v1._accepted_image_input_reply(p_data jsonb, p_code text)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_code IS NULL THEN pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-inputs/v1', 'ok', true, 'data', p_data
    ) ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-inputs/v1', 'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accepted_image_input_reply(jsonb, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._accepted_image_input_record(
    p_input public.asset_accepted_image_inputs
)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'input_id', (p_input).input_id,
        'job_id', (p_input).job_id,
        'task_id', (p_input).task_id,
        'project_id', (p_input).project_id,
        'actor_id', (p_input).actor_id,
        'status', (p_input).status,
        'task_version', (p_input).task_version,
        'task_source_generation', (p_input).task_source_generation,
        'dataset_item_id', (p_input).dataset_item_id,
        'task_source_snapshot', (p_input).task_source_snapshot,
        'source_owner', pg_catalog.jsonb_build_object(
            'kind', (p_input).source_owner_kind,
            'id', (p_input).source_owner_id
        ),
        'source_generation', (p_input).source_generation,
        'source_snapshot', (p_input).source_snapshot,
        'byte_size', (p_input).byte_size,
        'sha256', (p_input).sha256,
        'error', (p_input).error_code,
        'created_at', (p_input).created_at,
        'retire_after', (p_input).retire_after
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accepted_image_input_record(
    public.asset_accepted_image_inputs
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._accepted_image_input_job_scope(
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
BEGIN
    v_reply := aap_jobs_v1.project_accepted_image_input_job(
        p_job_id, p_task_id, p_actor_id, p_require_active
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-jobs-accepted-image-input/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_reply #>> '{error,code}';
        IF v_code IN ('invalid_request', 'input_scope_mismatch', 'job_not_active') THEN
            RETURN aap_assets_v1._accepted_image_input_reply(NULL, v_code);
        END IF;
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
    END IF;
    v_data := v_reply -> 'data';
    IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
       OR (v_data - ARRAY[
            'job_id', 'task_id', 'actor_id', 'project_id', 'status', 'active',
            'cancel_requested'
       ]) IS DISTINCT FROM '{}'::jsonb
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
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accepted_image_input_job_scope(uuid, uuid, uuid, boolean)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._accepted_image_input_job_consistency(
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
BEGIN
    v_reply := aap_jobs_v1.accepted_image_input_job_consistency(
        p_job_id, p_task_id, p_actor_id, p_project_id
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-jobs-accepted-image-input/v1'
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
       ]) IS DISTINCT FROM '{}'::jsonb
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
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accepted_image_input_job_consistency(uuid, uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._accepted_image_input_task_scope(
    p_task_id uuid, p_actor_id uuid, p_lock_source boolean
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_data jsonb;
    v_code text;
BEGIN
    v_reply := aap_labeling_v1.project_accepted_image_input_task(
        p_task_id, p_actor_id, p_lock_source
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-accepted-image-input/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_reply #>> '{error,code}';
        IF v_code IN (
            'invalid_request', 'account_not_active', 'account_busy',
            'invalid_account_role', 'task_not_visible', 'task_busy',
            'permission_denied', 'source_missing', 'source_changed',
            'source_busy', 'source_not_ready', 'not_image'
        ) THEN
            RETURN aap_assets_v1._accepted_image_input_reply(NULL, v_code);
        END IF;
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
    END IF;
    v_data := v_reply -> 'data';
    IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
       OR (v_data - ARRAY[
            'task_id', 'project_id', 'dataset_item_id', 'task_version',
            'task_source_generation', 'task_source_snapshot'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (v_data ?& ARRAY[
            'task_id', 'project_id', 'dataset_item_id', 'task_version',
            'task_source_generation', 'task_source_snapshot'
       ])
       OR v_data ->> 'task_id' IS DISTINCT FROM p_task_id::text
       OR pg_catalog.jsonb_typeof(v_data -> 'project_id') IS DISTINCT FROM 'string'
       OR (v_data -> 'dataset_item_id' <> 'null'::jsonb
           AND pg_catalog.jsonb_typeof(v_data -> 'dataset_item_id') IS DISTINCT FROM 'string')
       OR pg_catalog.jsonb_typeof(v_data -> 'task_version') IS DISTINCT FROM 'number'
       OR v_data ->> 'task_version' !~ '^[1-9][0-9]*$'
       OR pg_catalog.jsonb_typeof(v_data -> 'task_source_generation') IS DISTINCT FROM 'number'
       OR v_data ->> 'task_source_generation' !~ '^[1-9][0-9]*$'
       OR pg_catalog.jsonb_typeof(v_data -> 'task_source_snapshot') IS DISTINCT FROM 'string'
       OR v_data ->> 'task_source_snapshot' !~ '^[0-9a-f]{64}$' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
    END IF;
    RETURN aap_assets_v1._accepted_image_input_reply(v_data, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accepted_image_input_task_scope(uuid, uuid, boolean)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._accepted_image_input_source_scope(
    p_actor_id uuid, p_task_id uuid, p_lock_source boolean
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_source jsonb;
    v_code text;
BEGIN
    v_reply := aap_assets_v1._image_owner_source(
        p_actor_id, p_task_id, NULL, p_lock_source
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_reply #>> '{error,code}';
        IF v_code IN (
            'invalid_request', 'account_not_active', 'account_busy',
            'invalid_account_role', 'task_not_visible', 'permission_denied',
            'source_missing', 'source_changed', 'source_busy', 'source_not_ready',
            'not_image', 'owner_changed', 'resource_budget_exceeded'
        ) THEN
            RETURN aap_assets_v1._accepted_image_input_reply(NULL, v_code);
        END IF;
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
    END IF;
    v_source := v_reply -> 'data';
    IF NOT aap_assets_v1._image_source_data_valid(v_source)
       OR v_source ->> 'task_id' IS DISTINCT FROM p_task_id::text THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
    END IF;
    RETURN aap_assets_v1._accepted_image_input_reply(v_source, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._accepted_image_input_source_scope(uuid, uuid, boolean)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.find_accepted_image_input(
    p_job_id uuid, p_task_id uuid, p_actor_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_actor jsonb;
    v_job jsonb;
    v_input public.asset_accepted_image_inputs%ROWTYPE;
BEGIN
    IF p_job_id IS NULL OR p_task_id IS NULL OR p_actor_id IS NULL THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_request');
    END IF;
    v_actor := aap_assets_v1._image_actor(p_actor_id, false);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, v_actor #>> '{error,code}');
    END IF;
    v_reply := aap_assets_v1._accepted_image_input_job_scope(
        p_job_id, p_task_id, p_actor_id, false
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_job := v_reply -> 'data';
    SELECT input.* INTO v_input
    FROM public.asset_accepted_image_inputs AS input
    WHERE input.job_id = p_job_id AND input.task_id = p_task_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._accepted_image_input_reply(
            pg_catalog.jsonb_build_object('input', NULL), NULL
        );
    END IF;
    IF v_input.actor_id IS DISTINCT FROM p_actor_id
       OR v_input.project_id::text IS DISTINCT FROM v_job ->> 'project_id' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_scope_mismatch');
    END IF;
    RETURN aap_assets_v1._accepted_image_input_reply(
        pg_catalog.jsonb_build_object(
            'input', aap_assets_v1._accepted_image_input_record(v_input)
        ), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.find_accepted_image_input(uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.reserve_accepted_image_input(
    p_job_id uuid, p_task_id uuid, p_actor_id uuid,
    p_source_snapshot text, p_object_identity jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_actor jsonb;
    v_job jsonb;
    v_task jsonb;
    v_source jsonb;
    v_input public.asset_accepted_image_inputs%ROWTYPE;
    v_task_id uuid;
    v_project_id uuid;
    v_item_id uuid;
    v_task_version integer;
    v_task_generation bigint;
    v_task_snapshot text;
    v_source_kind text;
    v_source_id uuid;
    v_source_generation bigint;
    v_source_identity jsonb;
    v_input_id uuid := pg_catalog.gen_random_uuid();
BEGIN
    IF p_job_id IS NULL OR p_task_id IS NULL OR p_actor_id IS NULL
       OR p_source_snapshot IS NULL OR p_source_snapshot !~ '^[0-9a-f]{64}$'
       OR NOT aap_assets_v1._media_object_identity_valid(p_object_identity) THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_request');
    END IF;
    v_actor := aap_assets_v1._image_actor(p_actor_id, false);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, v_actor #>> '{error,code}');
    END IF;
    v_reply := aap_assets_v1._accepted_image_input_job_scope(
        p_job_id, p_task_id, p_actor_id, false
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_job := v_reply -> 'data';

    -- The first committed reservation wins; a replay never retargets current bytes.
    SELECT input.* INTO v_input
    FROM public.asset_accepted_image_inputs AS input
    WHERE input.job_id = p_job_id AND input.task_id = p_task_id;
    IF FOUND THEN
        IF v_input.actor_id IS DISTINCT FROM p_actor_id
           OR v_input.project_id::text IS DISTINCT FROM v_job ->> 'project_id' THEN
            RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_scope_mismatch');
        END IF;
        RETURN aap_assets_v1._accepted_image_input_reply(
            aap_assets_v1._accepted_image_input_record(v_input), NULL
        );
    END IF;

    v_reply := aap_assets_v1._accepted_image_input_job_scope(
        p_job_id, p_task_id, p_actor_id, true
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_reply := aap_assets_v1._accepted_image_input_task_scope(
        p_task_id, p_actor_id, true
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_task := v_reply -> 'data';
    v_reply := aap_assets_v1._accepted_image_input_source_scope(
        p_actor_id, p_task_id, true
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_source := v_reply -> 'data';
    IF v_task ->> 'project_id' IS DISTINCT FROM v_job ->> 'project_id'
       OR v_source ->> 'project_id' IS DISTINCT FROM v_job ->> 'project_id'
       OR v_source ->> 'source_snapshot' IS DISTINCT FROM p_source_snapshot
       OR v_source -> 'source_drifted' IS DISTINCT FROM 'false'::jsonb
       OR v_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb
       OR v_source -> 'object_identity' = 'null'::jsonb
       OR aap_assets_v1._image_normalize_identity(v_source -> 'object_identity')
            IS DISTINCT FROM aap_assets_v1._image_normalize_identity(p_object_identity)
       OR v_task -> 'dataset_item_id' IS DISTINCT FROM v_source -> 'item_id'
       OR v_source ->> 'task_id' IS DISTINCT FROM p_task_id::text THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'source_changed');
    END IF;
    BEGIN
        v_task_id := (v_task ->> 'task_id')::uuid;
        v_project_id := (v_task ->> 'project_id')::uuid;
        v_item_id := CASE
            WHEN v_task -> 'dataset_item_id' = 'null'::jsonb THEN NULL
            ELSE (v_task ->> 'dataset_item_id')::uuid
        END;
        v_task_version := (v_task ->> 'task_version')::integer;
        v_task_generation := (v_task ->> 'task_source_generation')::bigint;
        v_source_kind := v_source #>> '{owner,kind}';
        v_source_id := (v_source #>> '{owner,id}')::uuid;
        v_source_generation := (v_source ->> 'source_generation')::bigint;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
    END;
    v_task_snapshot := v_task ->> 'task_source_snapshot';
    v_source_identity := aap_assets_v1._image_normalize_identity(
        v_source -> 'object_identity'
    );

    INSERT INTO public.asset_accepted_image_inputs (
        input_id, job_id, task_id, project_id, actor_id,
        task_version, task_source_generation, dataset_item_id,
        task_source_snapshot, source_owner_kind, source_owner_id,
        source_generation, source_snapshot, source_identity, source_facts
    ) VALUES (
        v_input_id, p_job_id, v_task_id, v_project_id, p_actor_id,
        v_task_version, v_task_generation, v_item_id,
        v_task_snapshot, v_source_kind, v_source_id,
        v_source_generation, p_source_snapshot, v_source_identity, v_source
    )
    ON CONFLICT (job_id, task_id) DO NOTHING
    RETURNING * INTO v_input;
    IF NOT FOUND THEN
        SELECT input.* INTO v_input
        FROM public.asset_accepted_image_inputs AS input
        WHERE input.job_id = p_job_id AND input.task_id = p_task_id;
        IF NOT FOUND OR v_input.actor_id IS DISTINCT FROM p_actor_id
           OR v_input.project_id IS DISTINCT FROM v_project_id THEN
            RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_scope_mismatch');
        END IF;
    END IF;
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
REVOKE ALL ON FUNCTION aap_assets_v1.reserve_accepted_image_input(uuid, uuid, uuid, text, jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_accepted_image_input(
    p_input_id uuid, p_job_id uuid, p_task_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_input public.asset_accepted_image_inputs%ROWTYPE;
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
    RETURN aap_assets_v1._accepted_image_input_reply(
        aap_assets_v1._accepted_image_input_record(v_input), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_accepted_image_input(uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_accepted_image_input_work(
    p_input_id uuid, p_job_id uuid, p_task_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_input public.asset_accepted_image_inputs%ROWTYPE;
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
    IF v_input.status = 'expired' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_expired');
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
        'key', v_input.blob_key,
        'version_id', v_input.blob_version_id,
        'etag', v_input.blob_etag,
        'byte_size', v_input.byte_size,
        'sha256', v_input.sha256
    );
    RETURN aap_assets_v1._accepted_image_input_reply(
        pg_catalog.jsonb_build_object(
            'input', aap_assets_v1._accepted_image_input_record(v_input),
            'source', v_input.source_facts,
            'blob', v_blob
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
CREATE FUNCTION aap_assets_v1.lock_accepted_image_input(
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
       OR v_task -> 'dataset_item_id' IS DISTINCT FROM pg_catalog.to_jsonb(v_input.dataset_item_id) THEN
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
REVOKE ALL ON FUNCTION aap_assets_v1.lock_accepted_image_input(uuid, uuid, uuid)
    FROM PUBLIC;
