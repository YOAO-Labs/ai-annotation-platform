-- Retained video receipts have no live foreign-resource keys.
CREATE FUNCTION aap_assets_v1._video_input_reply(p_data jsonb, p_code text)
RETURNS jsonb LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT CASE WHEN p_code IS NULL THEN pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-video-input-execution/v1', 'ok', true, 'data', p_data
    ) ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-video-input-execution/v1', 'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_reply(jsonb, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_keys(p_data jsonb, p_keys text[])
RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF pg_catalog.jsonb_typeof(p_data) IS DISTINCT FROM 'object' THEN RETURN false; END IF;
    RETURN (p_data - p_keys) = '{}'::jsonb AND p_data ?& p_keys;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_keys(jsonb, text[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_int(p_data jsonb, p_min bigint, p_max bigint)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF pg_catalog.jsonb_typeof(p_data) IS DISTINCT FROM 'number'
       OR (p_data #>> '{}') !~ '^-?(0|[1-9][0-9]*)$'
       OR pg_catalog.char_length(p_data #>> '{}') > 17 THEN RETURN false; END IF;
    RETURN (p_data #>> '{}')::numeric BETWEEN p_min AND p_max;
EXCEPTION WHEN OTHERS THEN RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_int(jsonb, bigint, bigint) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_uuid(p_data jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT COALESCE(pg_catalog.jsonb_typeof(p_data) = 'string'
        AND p_data #>> '{}' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', false)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_uuid(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_identity_valid(p_data jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT COALESCE(aap_assets_v1._video_input_keys(p_data, ARRAY['etag','byte_size','version_id'])
        AND pg_catalog.jsonb_typeof(p_data -> 'etag') = 'string'
        AND pg_catalog.char_length(p_data ->> 'etag') BETWEEN 1 AND 255
        AND aap_assets_v1._video_input_int(p_data -> 'byte_size', 0, 9007199254740991)
        AND (p_data -> 'version_id' = 'null'::jsonb OR (
            pg_catalog.jsonb_typeof(p_data -> 'version_id') = 'string'
            AND pg_catalog.char_length(p_data ->> 'version_id') <= 255)), false)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_identity_valid(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_normalize_identity(p_data jsonb)
RETURNS jsonb LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT CASE WHEN p_data IS NULL OR p_data = 'null'::jsonb THEN NULL
        ELSE p_data || pg_catalog.jsonb_build_object('version_id', p_data -> 'version_id') END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_normalize_identity(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_sampling_valid(p_data jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT COALESCE(aap_assets_v1._video_input_keys(p_data, ARRAY['max_frames','step','width','format'])
        AND aap_assets_v1._video_input_int(p_data -> 'max_frames', 1, 2147483647)
        AND aap_assets_v1._video_input_int(p_data -> 'step', 1, 2147483647)
        AND aap_assets_v1._video_input_int(p_data -> 'width', 1, 4096)
        AND pg_catalog.jsonb_typeof(p_data -> 'format') = 'string'
        AND p_data ->> 'format' IN ('jpeg','webp'), false)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_sampling_valid(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_selection_valid(p_data jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_id jsonb; v_previous text;
BEGIN
    IF NOT aap_assets_v1._video_input_keys(p_data,
        ARRAY['batch_id','task_ids','predict_mode','execution_scope','sampling'])
       OR (p_data -> 'batch_id' <> 'null'::jsonb AND NOT aap_assets_v1._video_input_uuid(p_data -> 'batch_id'))
       OR pg_catalog.jsonb_typeof(p_data -> 'predict_mode') IS DISTINCT FROM 'string'
       OR p_data ->> 'predict_mode' NOT IN ('skip_predicted','overwrite','append')
       OR pg_catalog.jsonb_typeof(p_data -> 'execution_scope') IS DISTINCT FROM 'string'
       OR p_data ->> 'execution_scope' NOT IN ('bulk','workbench')
       OR NOT aap_assets_v1._video_input_sampling_valid(p_data -> 'sampling') THEN RETURN false; END IF;
    IF p_data -> 'task_ids' <> 'null'::jsonb THEN
        IF pg_catalog.jsonb_typeof(p_data -> 'task_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_data -> 'task_ids') NOT BETWEEN 1 AND 200 THEN RETURN false; END IF;
        FOR v_id IN SELECT value FROM pg_catalog.jsonb_array_elements(p_data -> 'task_ids') LOOP
            IF NOT aap_assets_v1._video_input_uuid(v_id)
               OR (v_previous IS NOT NULL AND v_id #>> '{}' <= v_previous) THEN RETURN false; END IF;
            v_previous := v_id #>> '{}';
        END LOOP;
    END IF;
    IF p_data ->> 'execution_scope' = 'workbench' THEN
        RETURN p_data -> 'batch_id' = 'null'::jsonb
           AND pg_catalog.jsonb_typeof(p_data -> 'task_ids') = 'array'
           AND pg_catalog.jsonb_array_length(p_data -> 'task_ids') = 1;
    END IF;
    RETURN true;
EXCEPTION WHEN OTHERS THEN RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_selection_valid(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_task_valid(p_data jsonb, p_captured boolean)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_keys text[] := ARRAY['task_id','project_id','task_version','task_source_generation',
    'dataset_item_id','task_source_snapshot','file_path','file_type'];
BEGIN
    IF p_captured THEN v_keys := v_keys || ARRAY['batch_id']; END IF;
    RETURN COALESCE(aap_assets_v1._video_input_keys(p_data, v_keys)
        AND aap_assets_v1._video_input_uuid(p_data -> 'task_id')
        AND aap_assets_v1._video_input_uuid(p_data -> 'project_id')
        AND (aap_assets_v1._video_input_uuid(p_data -> 'dataset_item_id')
            OR (p_captured AND p_data -> 'dataset_item_id' = 'null'::jsonb))
        AND (NOT p_captured OR p_data -> 'batch_id' = 'null'::jsonb
            OR aap_assets_v1._video_input_uuid(p_data -> 'batch_id'))
        AND aap_assets_v1._video_input_int(p_data -> 'task_version', 1, 9007199254740991)
        AND aap_assets_v1._video_input_int(p_data -> 'task_source_generation', 1, 9007199254740991)
        AND pg_catalog.jsonb_typeof(p_data -> 'task_source_snapshot') = 'string'
        AND p_data ->> 'task_source_snapshot' ~ '^[0-9a-f]{64}$'
        AND pg_catalog.jsonb_typeof(p_data -> 'file_path') = 'string'
        AND pg_catalog.char_length(p_data ->> 'file_path') <= 1024
        AND p_data ->> 'file_type' = 'video', false);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_task_valid(jsonb, boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_source_valid(p_data jsonb, p_captured boolean)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_keys text[] := ARRAY['dataset_item_id','dataset_id','source_generation',
    'source_snapshot','timetable_revision','object_identity'];
BEGIN
    IF p_captured THEN
        v_keys := v_keys || ARRAY['source_drifted','file_path','file_type','file_size','content_hash'];
        IF pg_catalog.jsonb_typeof(p_data -> 'source_drifted') IS DISTINCT FROM 'boolean'
           OR pg_catalog.jsonb_typeof(p_data -> 'file_path') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(p_data ->> 'file_path') NOT BETWEEN 1 AND 1024
           OR p_data ->> 'file_type' IS DISTINCT FROM 'video'
           OR (p_data -> 'file_size' <> 'null'::jsonb
               AND NOT aap_assets_v1._video_input_int(p_data -> 'file_size', 0, 9007199254740991))
           OR NOT (p_data -> 'content_hash' = 'null'::jsonb OR (
               pg_catalog.jsonb_typeof(p_data -> 'content_hash') = 'string'
               AND pg_catalog.char_length(p_data ->> 'content_hash') <= 255)) THEN RETURN false; END IF;
    END IF;
    RETURN COALESCE(aap_assets_v1._video_input_keys(p_data, v_keys)
        AND aap_assets_v1._video_input_uuid(p_data -> 'dataset_item_id')
        AND aap_assets_v1._video_input_uuid(p_data -> 'dataset_id')
        AND aap_assets_v1._video_input_int(p_data -> 'source_generation', 1, 9007199254740991)
        AND aap_assets_v1._video_input_int(p_data -> 'timetable_revision', 0, 9007199254740991)
        AND pg_catalog.jsonb_typeof(p_data -> 'source_snapshot') = 'string'
        AND p_data ->> 'source_snapshot' ~ '^[0-9a-f]{64}$'
        AND (aap_assets_v1._video_input_identity_valid(p_data -> 'object_identity')
            OR (p_captured AND p_data -> 'object_identity' = 'null'::jsonb)), false);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_source_valid(jsonb, boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_prefix_valid(p_prefix text)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT COALESCE(p_prefix ~ '^accepted-video-inputs/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g[1-9][0-9]*/a[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/$', false)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_prefix_valid(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_blob_valid(p_data jsonb, p_frame boolean)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_key text; v_suffix text;
BEGIN
    IF NOT aap_assets_v1._video_input_keys(p_data, ARRAY['key','version_id','etag','byte_size','sha256']) THEN RETURN false; END IF;
    v_key := p_data ->> 'key';
    v_suffix := CASE WHEN p_frame THEN 'frames/(0|[1-9][0-9]*)/w[1-9][0-9]*-(jpeg|webp)' ELSE 'source' END;
    RETURN COALESCE(pg_catalog.jsonb_typeof(p_data -> 'key') = 'string'
        AND pg_catalog.char_length(v_key) <= 2048
        AND v_key ~ ('^accepted-video-inputs/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g[1-9][0-9]*/a[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/' || v_suffix || '$')
        AND pg_catalog.jsonb_typeof(p_data -> 'version_id') = 'string'
        AND pg_catalog.char_length(p_data ->> 'version_id') BETWEEN 1 AND 255
        AND pg_catalog.btrim(p_data ->> 'version_id',
            U&'\0009\000a\000b\000c\000d\001c\001d\001e\001f\0020\0085\00a0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200a\2028\2029\202f\205f\3000') <> ''
        AND p_data ->> 'version_id' <> 'null'
        AND pg_catalog.jsonb_typeof(p_data -> 'etag') = 'string'
        AND pg_catalog.char_length(p_data ->> 'etag') BETWEEN 1 AND 255
        AND aap_assets_v1._video_input_int(p_data -> 'byte_size', 1, 9007199254740991)
        AND pg_catalog.jsonb_typeof(p_data -> 'sha256') = 'string'
        AND p_data ->> 'sha256' ~ '^[0-9a-f]{64}$', false);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_blob_valid(jsonb, boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_plan_shape_valid(p_plan jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_input jsonb := p_plan -> 'input'; v_video jsonb := v_input -> 'video'; v_field text;
BEGIN
    IF NOT aap_assets_v1._video_input_keys(p_plan, ARRAY['input','sampling','selected_count','timebase_digest','plan_digest'])
       OR NOT aap_assets_v1._video_input_keys(v_input, ARRAY['input_id','job_id','actor_id','task','source','blob','video','created_at'])
       OR NOT aap_assets_v1._video_input_keys(v_video, ARRAY['frame_count','width','height','duration_ms','video_stream'])
       OR NOT aap_assets_v1._video_input_task_valid(v_input -> 'task', false)
       OR NOT aap_assets_v1._video_input_source_valid(v_input -> 'source', false)
       OR NOT aap_assets_v1._video_input_blob_valid(v_input -> 'blob', false)
       OR NOT aap_assets_v1._video_input_sampling_valid(p_plan -> 'sampling')
       OR NOT aap_assets_v1._video_input_int(p_plan -> 'selected_count', 1, 2147483647)
       OR NOT aap_assets_v1._video_input_int(v_video -> 'duration_ms', 0, 9007199254740991)
       OR NOT aap_assets_v1._video_input_int(v_video -> 'video_stream', 0, 0)
       OR pg_catalog.jsonb_typeof(v_input -> 'created_at') IS DISTINCT FROM 'string'
       OR v_input ->> 'created_at' !~ '(Z|[+-][0-9]{2}:[0-9]{2})$'
       OR v_input #> '{task,dataset_item_id}' IS DISTINCT FROM v_input #> '{source,dataset_item_id}'
       OR v_input #> '{blob,byte_size}' IS DISTINCT FROM v_input #> '{source,object_identity,byte_size}'
       OR pg_catalog.strpos(v_input #>> '{blob,key}', 'accepted-video-inputs/' || (v_input ->> 'input_id') || '/') <> 1 THEN RETURN false; END IF;
    FOREACH v_field IN ARRAY ARRAY['input_id','job_id','actor_id'] LOOP
        IF NOT aap_assets_v1._video_input_uuid(v_input -> v_field) THEN RETURN false; END IF;
    END LOOP;
    FOREACH v_field IN ARRAY ARRAY['frame_count','width','height'] LOOP
        IF NOT aap_assets_v1._video_input_int(v_video -> v_field, 1, 9007199254740991) THEN RETURN false; END IF;
    END LOOP;
    FOREACH v_field IN ARRAY ARRAY['timebase_digest','plan_digest'] LOOP
        IF pg_catalog.jsonb_typeof(p_plan -> v_field) IS DISTINCT FROM 'string'
           OR p_plan ->> v_field !~ '^[0-9a-f]{64}$' THEN RETURN false; END IF;
    END LOOP;
    PERFORM (v_input ->> 'created_at')::timestamptz;
    RETURN true;
EXCEPTION WHEN OTHERS THEN RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_plan_shape_valid(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_error_valid(p_code text)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT COALESCE(p_code = ANY(ARRAY[
        'invalid_request','invalid_response','storage_unavailable','account_not_active','account_busy',
        'invalid_account_role','task_not_visible','task_busy','permission_denied','source_missing',
        'source_changed','source_not_ready','not_video','job_busy','job_not_active','manifest_scope_mismatch',
        'source_busy','metadata_not_ready','invalid_frame_range','asset_missing','operation_conflict',
        'operation_busy','lease_lost','dependency_changed','resource_budget_exceeded','object_missing',
        'input_missing','input_scope_mismatch','input_not_ready','input_expired','input_corrupt','input_busy',
        'manifest_missing','manifest_busy','manifest_not_ready','manifest_cursor_invalid','task_scope_changed',
        'unsupported_media','timebase_changed','timebase_incomplete','frame_missing','frame_not_ready',
        'frame_busy','versioning_required','resource_limit','timeout','processing_failed','reference_busy'
    ]),false)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_error_valid(text) FROM PUBLIC;
-- aap-statement-break

CREATE TABLE public.asset_accepted_video_manifests (
    job_id uuid PRIMARY KEY, actor_id uuid NOT NULL, project_id uuid NOT NULL,
    selection jsonb NOT NULL CHECK (aap_assets_v1._video_input_selection_valid(selection)),
    selection_digest text NOT NULL CHECK (selection_digest ~ '^[0-9a-f]{64}$'),
    candidate_count bigint NOT NULL CHECK (candidate_count BETWEEN 0 AND 9007199254740991),
    selected_count bigint NOT NULL CHECK (selected_count BETWEEN 0 AND candidate_count),
    prepared_count bigint NOT NULL DEFAULT 0 CHECK (prepared_count BETWEEN 0 AND selected_count),
    status text NOT NULL DEFAULT 'preparing', ready_count bigint, failed_count bigint,
    sealed_digest text, created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    sealed_at timestamptz, retire_after timestamptz,
    CHECK ((status = 'preparing' AND ready_count IS NULL AND failed_count IS NULL AND sealed_digest IS NULL AND sealed_at IS NULL)
        OR (status = 'sealed' AND prepared_count = selected_count AND ready_count >= 0 AND failed_count >= 0
            AND ready_count + failed_count = selected_count AND sealed_digest ~ '^[0-9a-f]{64}$'
            AND ready_count IS NOT NULL AND failed_count IS NOT NULL AND sealed_digest IS NOT NULL
            AND sealed_at IS NOT NULL AND sealed_at >= created_at))
);
-- aap-statement-break
REVOKE ALL ON public.asset_accepted_video_manifests FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.asset_accepted_video_members (
    job_id uuid NOT NULL, ordinal bigint NOT NULL CHECK (ordinal BETWEEN 1 AND 9007199254740991),
    task_id uuid NOT NULL, project_id uuid NOT NULL, batch_id uuid,
    task jsonb NOT NULL CHECK (aap_assets_v1._video_input_task_valid(task, true)),
    source_facts jsonb CHECK (source_facts IS NULL OR aap_assets_v1._video_input_source_valid(source_facts, true)),
    capture_error text CHECK (capture_error IS NULL OR aap_assets_v1._video_input_error_valid(capture_error)),
    state text NOT NULL, input_id uuid UNIQUE,
    error_code text CHECK (error_code IS NULL OR aap_assets_v1._video_input_error_valid(error_code)),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY(job_id, ordinal), UNIQUE(job_id, task_id),
    CHECK (task ->> 'task_id' = task_id::text AND task ->> 'project_id' = project_id::text
        AND task -> 'batch_id' = COALESCE(pg_catalog.to_jsonb(batch_id), 'null'::jsonb)
        AND (source_facts IS NULL OR source_facts -> 'dataset_item_id' = task -> 'dataset_item_id')),
    CHECK ((state = 'unprepared' AND source_facts IS NOT NULL AND source_facts -> 'source_drifted' = 'false'::jsonb AND input_id IS NULL AND error_code IS NULL AND capture_error IS NULL)
        OR (state = 'preparing' AND source_facts IS NOT NULL AND source_facts -> 'source_drifted' = 'false'::jsonb AND input_id IS NOT NULL AND error_code IS NULL AND capture_error IS NULL)
        OR (state = 'ready' AND source_facts IS NOT NULL AND source_facts -> 'source_drifted' = 'false'::jsonb AND input_id IS NOT NULL AND error_code IS NULL AND capture_error IS NULL)
        OR (state = 'failed' AND error_code IS NOT NULL))
);
-- aap-statement-break
CREATE INDEX ix_asset_accepted_video_members_task ON public.asset_accepted_video_members(task_id, job_id);
-- aap-statement-break
REVOKE ALL ON public.asset_accepted_video_members FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.asset_accepted_video_inputs (
    input_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(), job_id uuid NOT NULL,
    task_id uuid NOT NULL, project_id uuid NOT NULL, actor_id uuid NOT NULL, request_actor_id uuid NOT NULL,
    task jsonb NOT NULL CHECK (aap_assets_v1._video_input_task_valid(task, false)),
    source jsonb NOT NULL CHECK (aap_assets_v1._video_input_source_valid(source, false)),
    source_facts jsonb NOT NULL CHECK (aap_assets_v1._video_input_source_valid(source_facts, true)),
    sampling jsonb NOT NULL CHECK (aap_assets_v1._video_input_sampling_valid(sampling)),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','materializing','ready','failed','expired')),
    plan jsonb CHECK (plan IS NULL OR aap_assets_v1._video_input_plan_shape_valid(plan)),
    error_code text CHECK (error_code IS NULL OR aap_assets_v1._video_input_error_valid(error_code)),
    generation bigint NOT NULL DEFAULT 0 CHECK (generation BETWEEN 0 AND 9007199254740991),
    attempt_count bigint NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 9007199254740991),
    lease_owner uuid, lease_token uuid, lease_expires_at timestamptz,
    last_lease_token uuid, last_outcome text CHECK (last_outcome IN ('ready','failed','lease_expired')),
    staged_count bigint NOT NULL DEFAULT 0 CHECK (staged_count BETWEEN 0 AND 2147483647),
    staged_bytes bigint NOT NULL DEFAULT 0 CHECK (staged_bytes BETWEEN 0 AND 67108864),
    retire_after timestamptz, retention_checked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(), UNIQUE(job_id, task_id),
    CHECK (task ->> 'task_id' = task_id::text AND task ->> 'project_id' = project_id::text
        AND source -> 'dataset_item_id' = task -> 'dataset_item_id'
        AND (source_facts - ARRAY['source_drifted','file_path','file_type','file_size','content_hash']) = source
        AND source_facts -> 'source_drifted' = 'false'::jsonb
        AND (source #>> '{object_identity,byte_size}')::bigint > 0),
    CHECK ((status IN ('ready','expired')) = (plan IS NOT NULL)),
    CHECK (plan IS NULL OR (
        plan #>> '{input,input_id}' = input_id::text AND plan #>> '{input,job_id}' = job_id::text
        AND plan #>> '{input,actor_id}' = actor_id::text AND plan #> '{input,task}' = task
        AND plan #> '{input,source}' = source AND plan -> 'sampling' = sampling)),
    CHECK ((status = 'failed') = (error_code IS NOT NULL)),
    CHECK ((status = 'materializing') = (lease_token IS NOT NULL)
        AND (lease_token IS NULL) = (lease_owner IS NULL)
        AND (lease_token IS NULL) = (lease_expires_at IS NULL)),
    CHECK ((last_lease_token IS NULL) = (last_outcome IS NULL))
);
-- aap-statement-break
CREATE INDEX ix_asset_accepted_video_inputs_claim ON public.asset_accepted_video_inputs(status, lease_expires_at, created_at, input_id);
-- aap-statement-break
CREATE INDEX ix_asset_accepted_video_inputs_job ON public.asset_accepted_video_inputs(job_id, task_id);
-- aap-statement-break
REVOKE ALL ON public.asset_accepted_video_inputs FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.asset_accepted_video_input_attempts (
    input_id uuid NOT NULL REFERENCES public.asset_accepted_video_inputs(input_id) ON DELETE RESTRICT,
    generation bigint NOT NULL CHECK (generation BETWEEN 1 AND 9007199254740991), lease_token uuid NOT NULL,
    prefix text NOT NULL UNIQUE CHECK (aap_assets_v1._video_input_prefix_valid(prefix)),
    status text NOT NULL DEFAULT 'leased' CHECK (status IN ('leased','ready','failed','abandoned','expired')),
    lease_expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    completed_at timestamptz, PRIMARY KEY(input_id, generation)
);
-- aap-statement-break
REVOKE ALL ON public.asset_accepted_video_input_attempts FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.asset_accepted_video_times (
    input_id uuid NOT NULL REFERENCES public.asset_accepted_video_inputs(input_id) ON DELETE RESTRICT,
    generation bigint NOT NULL CHECK (generation BETWEEN 1 AND 9007199254740991), lease_token uuid NOT NULL,
    ordinal bigint NOT NULL CHECK (ordinal BETWEEN 1 AND 2147483647),
    frame_index integer NOT NULL CHECK (frame_index >= 0),
    pts_ms bigint NOT NULL CHECK (pts_ms BETWEEN -9007199254740991 AND 9007199254740991),
    is_keyframe boolean NOT NULL, PRIMARY KEY(input_id, generation, ordinal), UNIQUE(input_id, generation, frame_index)
);
-- aap-statement-break
REVOKE ALL ON public.asset_accepted_video_times FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.asset_accepted_video_frames (
    frame_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
    input_id uuid NOT NULL REFERENCES public.asset_accepted_video_inputs(input_id) ON DELETE RESTRICT,
    request_actor_id uuid NOT NULL, plan_digest text NOT NULL CHECK (plan_digest ~ '^[0-9a-f]{64}$'),
    ordinal bigint NOT NULL CHECK (ordinal BETWEEN 1 AND 2147483647), frame_index integer NOT NULL CHECK (frame_index >= 0),
    pts_ms bigint NOT NULL CHECK (pts_ms BETWEEN -9007199254740991 AND 9007199254740991), is_keyframe boolean NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','materializing','ready','failed','expired')),
    blob jsonb CHECK (blob IS NULL OR aap_assets_v1._video_input_blob_valid(blob, true)),
    error_code text CHECK (error_code IS NULL OR aap_assets_v1._video_input_error_valid(error_code)),
    generation bigint NOT NULL DEFAULT 0 CHECK (generation BETWEEN 0 AND 9007199254740991),
    attempt_count bigint NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 9007199254740991),
    lease_owner uuid, lease_token uuid, lease_expires_at timestamptz, last_lease_token uuid,
    last_outcome text CHECK (last_outcome IN ('ready','failed','lease_expired')),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE(input_id, plan_digest, ordinal),
    CHECK ((status IN ('ready','expired')) = (blob IS NOT NULL)),
    CHECK ((status = 'failed') = (error_code IS NOT NULL)),
    CHECK ((status = 'materializing') = (lease_token IS NOT NULL)
        AND (lease_token IS NULL) = (lease_owner IS NULL)
        AND (lease_token IS NULL) = (lease_expires_at IS NULL)),
    CHECK ((last_lease_token IS NULL) = (last_outcome IS NULL))
);
-- aap-statement-break
CREATE INDEX ix_asset_accepted_video_frames_claim ON public.asset_accepted_video_frames(status, lease_expires_at, created_at, frame_id);
-- aap-statement-break
REVOKE ALL ON public.asset_accepted_video_frames FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.asset_accepted_video_frame_attempts (
    frame_id uuid NOT NULL REFERENCES public.asset_accepted_video_frames(frame_id) ON DELETE RESTRICT,
    input_id uuid NOT NULL REFERENCES public.asset_accepted_video_inputs(input_id) ON DELETE RESTRICT,
    generation bigint NOT NULL CHECK (generation BETWEEN 1 AND 9007199254740991), lease_token uuid NOT NULL,
    prefix text NOT NULL UNIQUE CHECK (aap_assets_v1._video_input_prefix_valid(prefix)),
    status text NOT NULL DEFAULT 'leased' CHECK (status IN ('leased','ready','failed','abandoned','expired')),
    lease_expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    completed_at timestamptz, PRIMARY KEY(frame_id, generation)
);
-- aap-statement-break
CREATE INDEX ix_asset_accepted_video_frame_attempts_input ON public.asset_accepted_video_frame_attempts(input_id);
-- aap-statement-break
REVOKE ALL ON public.asset_accepted_video_frame_attempts FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._video_input_receipt_immutable()
RETURNS trigger LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_old jsonb := pg_catalog.to_jsonb(OLD); v_new jsonb; v_mutable text[];
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='video input receipts are retained'; END IF;
    v_new := pg_catalog.to_jsonb(NEW);
    CASE TG_TABLE_NAME
        WHEN 'asset_accepted_video_manifests' THEN
            v_mutable := ARRAY['prepared_count','status','ready_count','failed_count','sealed_digest','sealed_at','retire_after'];
            IF OLD.status = 'sealed' THEN v_mutable := ARRAY['retire_after']; END IF;
        WHEN 'asset_accepted_video_members' THEN
            v_mutable := ARRAY['state','input_id','error_code','updated_at'];
            IF OLD.state IN ('ready','failed') THEN v_mutable := ARRAY['updated_at']; END IF;
        WHEN 'asset_accepted_video_inputs' THEN
            v_mutable := ARRAY['status','plan','error_code','generation','attempt_count','lease_owner','lease_token',
                'lease_expires_at','last_lease_token','last_outcome','staged_count','staged_bytes',
                'retire_after','retention_checked_at','updated_at'];
            IF OLD.status IN ('ready','expired','failed') THEN
                v_mutable := ARRAY['retire_after','retention_checked_at','updated_at'];
                IF OLD.status = 'ready' AND NEW.status = 'expired' THEN v_mutable := v_mutable || ARRAY['status']; END IF;
            END IF;
        WHEN 'asset_accepted_video_frames' THEN
            v_mutable := ARRAY['status','blob','error_code','generation','attempt_count','lease_owner','lease_token',
                'lease_expires_at','last_lease_token','last_outcome','request_actor_id','updated_at'];
            IF OLD.status IN ('ready','expired') THEN
                v_mutable := ARRAY['updated_at'];
                IF OLD.status = 'ready' AND NEW.status = 'expired' THEN v_mutable := v_mutable || ARRAY['status']; END IF;
            ELSIF OLD.status = 'materializing' AND NEW.request_actor_id IS DISTINCT FROM OLD.request_actor_id THEN
                RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='video frame requester is leased';
            END IF;
        WHEN 'asset_accepted_video_input_attempts', 'asset_accepted_video_frame_attempts' THEN
            v_mutable := ARRAY['status','lease_expires_at','completed_at'];
        ELSE v_mutable := ARRAY[]::text[];
    END CASE;
    IF (v_old - v_mutable) IS DISTINCT FROM (v_new - v_mutable) THEN
        RAISE EXCEPTION USING ERRCODE='55000', MESSAGE='video input source and terminal evidence are immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_receipt_immutable() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_video_manifest_immutable BEFORE UPDATE OR DELETE ON public.asset_accepted_video_manifests
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._video_input_receipt_immutable();
-- aap-statement-break
CREATE TRIGGER trg_video_member_immutable BEFORE UPDATE OR DELETE ON public.asset_accepted_video_members
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._video_input_receipt_immutable();
-- aap-statement-break
CREATE TRIGGER trg_video_input_immutable BEFORE UPDATE OR DELETE ON public.asset_accepted_video_inputs
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._video_input_receipt_immutable();
-- aap-statement-break
CREATE TRIGGER trg_video_input_attempt_immutable BEFORE UPDATE OR DELETE ON public.asset_accepted_video_input_attempts
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._video_input_receipt_immutable();
-- aap-statement-break
CREATE TRIGGER trg_video_times_immutable BEFORE UPDATE OR DELETE ON public.asset_accepted_video_times
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._video_input_receipt_immutable();
-- aap-statement-break
CREATE TRIGGER trg_video_frame_immutable BEFORE UPDATE OR DELETE ON public.asset_accepted_video_frames
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._video_input_receipt_immutable();
-- aap-statement-break
CREATE TRIGGER trg_video_frame_attempt_immutable BEFORE UPDATE OR DELETE ON public.asset_accepted_video_frame_attempts
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._video_input_receipt_immutable();
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._video_input_header(p_row public.asset_accepted_video_manifests)
RETURNS jsonb LANGUAGE sql STABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT pg_catalog.jsonb_build_object('job_id',(p_row).job_id,'actor_id',(p_row).actor_id,'project_id',(p_row).project_id,
        'selection',(p_row).selection,'selection_digest',(p_row).selection_digest,
        'candidate_count',(p_row).candidate_count,'selected_count',(p_row).selected_count,'prepared_count',(p_row).prepared_count,
        'status',(p_row).status,'ready_count',(p_row).ready_count,'failed_count',(p_row).failed_count,
        'sealed_digest',(p_row).sealed_digest,'created_at',(p_row).created_at,'sealed_at',(p_row).sealed_at)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_header(public.asset_accepted_video_manifests) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_record(p_row public.asset_accepted_video_inputs)
RETURNS jsonb LANGUAGE sql STABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT pg_catalog.jsonb_build_object('input_id',(p_row).input_id,'job_id',(p_row).job_id,'actor_id',(p_row).actor_id,
        'task',(p_row).task,'source',(p_row).source,'sampling',(p_row).sampling,'status',(p_row).status,'plan',(p_row).plan,
        'error',(p_row).error_code,'created_at',(p_row).created_at,'retire_after',(p_row).retire_after)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_record(public.asset_accepted_video_inputs) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_member(p_row public.asset_accepted_video_members)
RETURNS jsonb LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT pg_catalog.jsonb_build_object('ordinal',(p_row).ordinal,'task',(p_row).task,'source',(p_row).source_facts,
        'state',(p_row).state,'input',(SELECT aap_assets_v1._video_input_record(input) FROM public.asset_accepted_video_inputs AS input
            WHERE input.input_id=(p_row).input_id),'error',(p_row).error_code)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_member(public.asset_accepted_video_members) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_entry(p_ordinal bigint,p_index integer,p_pts bigint,p_keyframe boolean)
RETURNS jsonb LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT pg_catalog.jsonb_build_object('ordinal',p_ordinal,'frame_index',p_index,'pts_ms',p_pts,'is_keyframe',p_keyframe)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_entry(bigint,integer,bigint,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_entry_text(p_ordinal bigint,p_index integer,p_pts bigint,p_keyframe boolean)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT p_ordinal::text || E'\n' || p_index::text || E'\n' || p_pts::text || E'\n' || CASE WHEN p_keyframe THEN '1' ELSE '0' END || E'\n'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_entry_text(bigint,integer,bigint,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_frame(p_row public.asset_accepted_video_frames)
RETURNS jsonb LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT pg_catalog.jsonb_build_object('frame_id',(p_row).frame_id,
        'plan',(SELECT input.plan FROM public.asset_accepted_video_inputs AS input WHERE input.input_id=(p_row).input_id),
        'entry',aap_assets_v1._video_input_entry((p_row).ordinal,(p_row).frame_index,(p_row).pts_ms,(p_row).is_keyframe),
        'status',(p_row).status,'blob',(p_row).blob,'error',(p_row).error_code,'created_at',(p_row).created_at)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_frame(public.asset_accepted_video_frames) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_hash_fields(p_domain text, p_fields text[])
RETURNS text LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_field text; v_body text := p_domain || E'\n';
BEGIN
    FOREACH v_field IN ARRAY p_fields LOOP
        v_body := v_body || CASE WHEN v_field IS NULL THEN 'N;' ELSE
            'S' || pg_catalog.octet_length(pg_catalog.convert_to(v_field,'UTF8'))::text || ':' || v_field || ';' END;
    END LOOP;
    RETURN pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(v_body,'UTF8')),'hex');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_hash_fields(text,text[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_selection_seed(
    p_job uuid,p_actor uuid,p_project uuid,p_selection jsonb,p_candidates bigint,p_selected bigint
)
RETURNS text LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_fields text[]; v_task jsonb;
BEGIN
    v_fields := ARRAY['aap-assets-video-input-execution/v1',p_job::text,p_actor::text,p_project::text,
        p_selection ->> 'batch_id',p_selection ->> 'execution_scope',p_selection ->> 'predict_mode',
        p_selection #>> '{sampling,max_frames}',p_selection #>> '{sampling,step}',p_selection #>> '{sampling,width}',
        p_selection #>> '{sampling,format}',CASE WHEN p_selection -> 'task_ids' = 'null'::jsonb THEN NULL
            ELSE pg_catalog.jsonb_array_length(p_selection -> 'task_ids')::text END];
    IF p_selection -> 'task_ids' <> 'null'::jsonb THEN
        FOR v_task IN SELECT value FROM pg_catalog.jsonb_array_elements(p_selection -> 'task_ids') LOOP
            v_fields := v_fields || ARRAY[v_task #>> '{}'];
        END LOOP;
    END IF;
    RETURN aap_assets_v1._video_input_hash_fields('selection',v_fields || ARRAY[p_candidates::text,p_selected::text]);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_selection_seed(uuid,uuid,uuid,jsonb,bigint,bigint) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_capture_digest(p_previous text,p_row public.asset_accepted_video_members)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT aap_assets_v1._video_input_hash_fields('member', ARRAY[p_previous,(p_row).ordinal::text,
        (p_row).task ->> 'task_id',(p_row).task ->> 'project_id',(p_row).task ->> 'batch_id',
        (p_row).task ->> 'task_version',(p_row).task ->> 'task_source_generation',(p_row).task ->> 'dataset_item_id',
        (p_row).task ->> 'task_source_snapshot',(p_row).task ->> 'file_path',(p_row).task ->> 'file_type',
        (p_row).source_facts ->> 'dataset_item_id',(p_row).source_facts ->> 'dataset_id',
        (p_row).source_facts ->> 'source_generation',(p_row).source_facts ->> 'source_snapshot',
        (p_row).source_facts ->> 'timetable_revision',(p_row).source_facts #>> '{object_identity,etag}',
        (p_row).source_facts #>> '{object_identity,byte_size}',(p_row).source_facts #>> '{object_identity,version_id}',
        CASE WHEN (p_row).source_facts IS NULL THEN NULL
            WHEN (p_row).source_facts -> 'source_drifted' = 'true'::jsonb THEN '1' ELSE '0' END,
        (p_row).source_facts ->> 'file_path',(p_row).source_facts ->> 'file_type',
        (p_row).source_facts ->> 'file_size',(p_row).source_facts ->> 'content_hash',(p_row).capture_error])
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_capture_digest(text,public.asset_accepted_video_members) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_sealed_seed(p_row public.asset_accepted_video_manifests,p_ready bigint,p_failed bigint)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT aap_assets_v1._video_input_hash_fields('sealed', ARRAY['aap-assets-video-input-execution/v1',
        (p_row).job_id::text,(p_row).actor_id::text,(p_row).project_id::text,(p_row).selection_digest,
        (p_row).selected_count::text,p_ready::text,p_failed::text])
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_sealed_seed(public.asset_accepted_video_manifests,bigint,bigint) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_outcome_digest(p_previous text,p_row public.asset_accepted_video_members)
RETURNS text LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_plan jsonb;
BEGIN
    IF (p_row).state = 'ready' THEN
        SELECT input.plan INTO v_plan FROM public.asset_accepted_video_inputs AS input WHERE input.input_id=(p_row).input_id;
    END IF;
    RETURN aap_assets_v1._video_input_hash_fields('outcome', ARRAY[p_previous,(p_row).ordinal::text,(p_row).task_id::text,
        (p_row).state,(p_row).error_code,(p_row).input_id::text,v_plan ->> 'plan_digest',v_plan ->> 'timebase_digest',
        v_plan #>> '{input,blob,sha256}',v_plan #>> '{input,blob,byte_size}',v_plan ->> 'selected_count']);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_outcome_digest(text,public.asset_accepted_video_members) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_attempt_prefix(p_input uuid,p_generation bigint,p_token uuid)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT 'accepted-video-inputs/' || p_input::text || '/g' || p_generation::text || '/a' || p_token::text || '/'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_attempt_prefix(uuid,bigint,uuid) FROM PUBLIC;
