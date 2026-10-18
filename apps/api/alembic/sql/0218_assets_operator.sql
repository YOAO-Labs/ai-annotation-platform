-- Trusted operator registration is an Assets-owned caller-transactional boundary.
CREATE TABLE public.dataset_operator_operations (
    operation_id uuid PRIMARY KEY,
    action text NOT NULL CONSTRAINT ck_dataset_operator_operations_action CHECK (
        action IN (
            'create_image_dataset', 'ensure_nuscenes_dataset', 'append_items',
            'patch_items', 'recount_items'
        )
    ),
    intent jsonb NOT NULL CONSTRAINT ck_dataset_operator_operations_intent
        CHECK (pg_catalog.jsonb_typeof(intent) = 'object'),
    result jsonb NOT NULL CONSTRAINT ck_dataset_operator_operations_result CHECK (
        pg_catalog.jsonb_typeof(result) = 'object'
        AND result ->> 'schema' = 'aap-assets-operator/v1'
        AND result -> 'ok' = 'true'::jsonb
        AND pg_catalog.jsonb_typeof(result -> 'data') = 'object'
    ),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now()
);
-- aap-statement-break
REVOKE ALL ON TABLE public.dataset_operator_operations FROM PUBLIC;
-- aap-statement-break
COMMENT ON TABLE public.dataset_operator_operations IS
    'Assets-owned immutable successful trusted operator registration receipts; deliberately has no live-resource foreign keys.';
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._operator_reply(p_data jsonb, p_code text)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_code IS NOT NULL THEN
        IF p_code NOT IN (
            'invalid_request', 'invalid_response', 'storage_unavailable',
            'dataset_missing', 'item_missing', 'user_missing',
            'account_not_active', 'account_busy', 'storage_root_conflict',
            'axis_conflict', 'source_changed',
            'operation_conflict', 'operation_busy', 'resource_budget_exceeded'
        ) THEN
            p_code := 'invalid_response';
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-assets-operator/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', p_code)
        );
    END IF;
    IF p_data IS NULL OR pg_catalog.jsonb_typeof(p_data) IS DISTINCT FROM 'object' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-assets-operator/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_response')
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-operator/v1', 'ok', true, 'data', p_data
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._operator_reply(jsonb, text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._operator_operation_lock(p_operation_id uuid)
RETURNS boolean
LANGUAGE sql
VOLATILE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT p_operation_id IS NOT NULL AND pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'aap-assets-operator:' || p_operation_id::text, 0
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._operator_operation_lock(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._operator_read_receipt(
    p_operation_id uuid,
    p_action text,
    p_intent jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_saved public.dataset_operator_operations%ROWTYPE;
BEGIN
    IF NOT aap_assets_v1._operator_operation_lock(p_operation_id) THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'operation_busy');
    END IF;
    SELECT receipt.* INTO v_saved
    FROM public.dataset_operator_operations AS receipt
    WHERE receipt.operation_id = p_operation_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF v_saved.action IS DISTINCT FROM p_action
       OR v_saved.intent IS DISTINCT FROM p_intent THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'operation_conflict');
    END IF;
    RETURN v_saved.result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._operator_read_receipt(uuid, text, jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._operator_write_receipt(
    p_operation_id uuid,
    p_action text,
    p_intent jsonb,
    p_result jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    INSERT INTO public.dataset_operator_operations (
        operation_id, action, intent, result
    ) VALUES (
        p_operation_id, p_action, p_intent, p_result
    ) ON CONFLICT (operation_id) DO NOTHING;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'P0002' USING DETAIL = 'operation_conflict';
    END IF;
    RETURN p_result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._operator_write_receipt(uuid, text, jsonb, jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._operator_user_exists(p_user_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT p_user_id IS NOT NULL AND EXISTS (
        SELECT 1
        FROM aap_identity_v1.read_actor_facts(p_user_id) AS facts
        WHERE facts.actor_id = p_user_id
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._operator_user_exists(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._operator_uuid_text_valid(p_value text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT p_value IS NOT NULL
       AND p_value ~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._operator_uuid_text_valid(text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._operator_source_entry_valid(p_entry jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_number numeric;
    v_value jsonb;
BEGIN
    IF p_entry IS NULL OR pg_catalog.jsonb_typeof(p_entry) IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(p_entry) AS key(name)
        WHERE key.name <> ALL (ARRAY[
            'id', 'file_name', 'file_path', 'file_type', 'file_size',
            'content_hash', 'width', 'height', 'metadata'
        ])
    ) OR NOT (p_entry ?& ARRAY['id', 'file_name', 'file_path', 'file_type']) THEN
        RETURN false;
    END IF;
    IF pg_catalog.jsonb_typeof(p_entry -> 'id') IS DISTINCT FROM 'string'
       OR NOT aap_assets_v1._operator_uuid_text_valid(p_entry ->> 'id') THEN
        RETURN false;
    END IF;
    IF pg_catalog.jsonb_typeof(p_entry -> 'file_name') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_entry ->> 'file_name') NOT BETWEEN 1 AND 500
       OR pg_catalog.jsonb_typeof(p_entry -> 'file_path') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_entry ->> 'file_path') NOT BETWEEN 1 AND 1000
       OR pg_catalog.jsonb_typeof(p_entry -> 'file_type') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_entry ->> 'file_type') NOT BETWEEN 1 AND 20 THEN
        RETURN false;
    END IF;
    IF p_entry ? 'metadata'
       AND pg_catalog.jsonb_typeof(p_entry -> 'metadata') IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    v_value := p_entry -> 'file_size';
    IF v_value IS NOT NULL AND v_value <> 'null'::jsonb THEN
        IF pg_catalog.jsonb_typeof(v_value) IS DISTINCT FROM 'number'
           OR (v_value #>> '{}') !~ '^(0|[1-9][0-9]*)$' THEN
            RETURN false;
        END IF;
        v_number := (v_value #>> '{}')::numeric;
        IF v_number > 9223372036854775807 THEN RETURN false; END IF;
    END IF;
    v_value := p_entry -> 'content_hash';
    IF v_value IS NOT NULL AND v_value <> 'null'::jsonb
       AND (pg_catalog.jsonb_typeof(v_value) IS DISTINCT FROM 'string'
            OR pg_catalog.char_length(v_value #>> '{}') > 64) THEN
        RETURN false;
    END IF;
    FOREACH v_value IN ARRAY ARRAY[p_entry -> 'width', p_entry -> 'height'] LOOP
        IF v_value IS NULL OR v_value = 'null'::jsonb THEN
            CONTINUE;
        END IF;
        IF pg_catalog.jsonb_typeof(v_value) IS DISTINCT FROM 'number'
           OR (v_value #>> '{}') !~ '^-?[0-9]+$' THEN
            RETURN false;
        END IF;
        v_number := (v_value #>> '{}')::numeric;
        IF v_number < -2147483648 OR v_number > 2147483647 THEN
            RETURN false;
        END IF;
    END LOOP;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._operator_source_entry_valid(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._operator_source_patch_valid(p_entry jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_value jsonb;
    v_number numeric;
BEGIN
    IF p_entry IS NULL OR pg_catalog.jsonb_typeof(p_entry) IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_object_keys(p_entry) AS key(name)
        WHERE key.name <> ALL (ARRAY[
            'id', 'source_snapshot', 'nuscenes_export', 'dimensions',
            'file_size', 'content_hash'
        ])
    ) OR NOT (p_entry ?& ARRAY['id', 'source_snapshot']) THEN
        RETURN false;
    END IF;
    IF pg_catalog.jsonb_typeof(p_entry -> 'id') IS DISTINCT FROM 'string'
       OR NOT aap_assets_v1._operator_uuid_text_valid(p_entry ->> 'id')
       OR pg_catalog.jsonb_typeof(p_entry -> 'source_snapshot') IS DISTINCT FROM 'string'
       OR p_entry ->> 'source_snapshot' !~ '^[0-9a-f]{64}$' THEN
        RETURN false;
    END IF;
    IF p_entry ? 'nuscenes_export'
       AND p_entry -> 'nuscenes_export' <> 'null'::jsonb
       AND pg_catalog.jsonb_typeof(p_entry -> 'nuscenes_export') IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    IF p_entry ? 'dimensions'
       AND p_entry -> 'dimensions' <> 'null'::jsonb THEN
        IF pg_catalog.jsonb_typeof(p_entry -> 'dimensions') IS DISTINCT FROM 'object'
           OR (p_entry -> 'dimensions') - ARRAY['width', 'height'] <> '{}'::jsonb
           OR NOT ((p_entry -> 'dimensions') ?& ARRAY['width', 'height']) THEN
            RETURN false;
        END IF;
        FOREACH v_value IN ARRAY ARRAY[
            p_entry #> '{dimensions,width}', p_entry #> '{dimensions,height}'
        ] LOOP
            IF pg_catalog.jsonb_typeof(v_value) IS DISTINCT FROM 'number'
               OR (v_value #>> '{}') !~ '^[0-9]+$' THEN
                RETURN false;
            END IF;
            v_number := (v_value #>> '{}')::numeric;
            IF v_number < 1 OR v_number > 2147483647 THEN RETURN false; END IF;
        END LOOP;
    END IF;
    IF p_entry ? 'file_size'
       AND p_entry -> 'file_size' <> 'null'::jsonb THEN
        v_value := p_entry -> 'file_size';
        IF pg_catalog.jsonb_typeof(v_value) IS DISTINCT FROM 'number'
           OR (v_value #>> '{}') !~ '^(0|[1-9][0-9]*)$' THEN
            RETURN false;
        END IF;
        v_number := (v_value #>> '{}')::numeric;
        IF v_number > 9223372036854775807 THEN RETURN false; END IF;
    END IF;
    IF p_entry ? 'content_hash'
       AND p_entry -> 'content_hash' <> 'null'::jsonb
       AND (pg_catalog.jsonb_typeof(p_entry -> 'content_hash') IS DISTINCT FROM 'string'
            OR pg_catalog.char_length(p_entry ->> 'content_hash') > 64) THEN
        RETURN false;
    END IF;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._operator_source_patch_valid(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._operator_dataset_json(p_dataset public.datasets)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', (p_dataset).id,
        'display_id', (p_dataset).display_id,
        'name', (p_dataset).name,
        'description', (p_dataset).description,
        'data_type', (p_dataset).data_type,
        'is_temporal', (p_dataset).is_temporal,
        'file_count', (p_dataset).file_count,
        'created_by', (p_dataset).created_by,
        'metadata', (p_dataset).metadata,
        'created_at', (p_dataset).created_at,
        'updated_at', (p_dataset).updated_at
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._operator_dataset_json(public.datasets) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._operator_source_snapshot(p_item public.dataset_items)
RETURNS text
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.encode(
        public.digest(
            pg_catalog.convert_to(
                pg_catalog.jsonb_build_object(
                    'id', (p_item).id,
                    'dataset_id', (p_item).dataset_id,
                    'file_name', (p_item).file_name,
                    'file_path', (p_item).file_path,
                    'file_type', (p_item).file_type,
                    'file_size', (p_item).file_size,
                    'content_hash', (p_item).content_hash,
                    'width', (p_item).width,
                    'height', (p_item).height,
                    'metadata', (p_item).metadata,
                    'scene_id', (p_item).scene_id,
                    'frame_index', (p_item).frame_index
                )::text,
                'UTF8'
            ),
            'sha256'
        ),
        'hex'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._operator_source_snapshot(public.dataset_items) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._operator_item_json(
    p_item public.dataset_items,
    p_snapshot text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', (p_item).id,
        'dataset_id', (p_item).dataset_id,
        'file_name', (p_item).file_name,
        'file_path', (p_item).file_path,
        'file_type', (p_item).file_type,
        'file_size', (p_item).file_size,
        'content_hash', (p_item).content_hash,
        'width', (p_item).width,
        'height', (p_item).height,
        'metadata', (p_item).metadata,
        'scene_id', (p_item).scene_id,
        'frame_index', (p_item).frame_index,
        'source_snapshot', COALESCE(
            p_snapshot, aap_assets_v1._operator_source_snapshot(p_item)
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._operator_item_json(public.dataset_items, text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._operator_path_valid(p_path text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT p_path IS NOT NULL
       AND p_path <> ''
       AND pg_catalog.left(p_path, 1) <> '/'
       AND pg_catalog.right(p_path, 1) <> '/'
       AND pg_catalog.strpos(p_path, '//') = 0
       AND pg_catalog.strpos(p_path, pg_catalog.chr(92)) = 0
       AND pg_catalog.strpos(p_path, ':') = 0
       AND p_path !~ '(^|/)\.\.?(/|$)'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._operator_path_valid(text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.create_operator_image_dataset(
    operation_id uuid,
    created_by uuid,
    name text,
    description text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_request jsonb;
    v_intent jsonb;
    v_reply jsonb;
    v_data jsonb;
    v_dataset public.datasets%ROWTYPE;
    v_code text;
BEGIN
    IF operation_id IS NULL OR created_by IS NULL OR name IS NULL
       OR description IS NULL
       OR pg_catalog.char_length(name) NOT BETWEEN 1 AND 255 THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'invalid_request');
    END IF;
    v_intent := pg_catalog.jsonb_build_object(
        'created_by', created_by, 'name', name, 'description', description
    );
    v_request := pg_catalog.jsonb_build_object(
        'operation_id', operation_id,
        'created_by', created_by,
        'name', name,
        'description', description
    );
    IF pg_catalog.octet_length(v_request::text) > 8 * 1024 * 1024 THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'resource_budget_exceeded');
    END IF;
    v_reply := aap_assets_v1._operator_read_receipt(
        operation_id, 'create_image_dataset', v_intent
    );
    IF v_reply IS NOT NULL THEN RETURN v_reply; END IF;
    IF NOT aap_assets_v1._operator_user_exists(created_by) THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'user_missing');
    END IF;

    INSERT INTO public.datasets (
        id, display_id, name, description, data_type, is_temporal,
        file_count, created_by, metadata
    ) VALUES (
        operation_id,
        'DS-' || pg_catalog.upper(
            pg_catalog.left(pg_catalog.replace(operation_id::text, '-', ''), 6)
        ),
        name, description, 'image', false, 0, created_by, '{}'::jsonb
    ) RETURNING * INTO v_dataset;

    v_data := pg_catalog.jsonb_build_object(
        'operation_id', operation_id,
        'dataset_id', operation_id,
        'action', 'create_image_dataset',
        'dataset', aap_assets_v1._operator_dataset_json(v_dataset),
        'storage_root', NULL,
        'needs_pcd_upgrade', NULL
    );
    v_reply := aap_assets_v1._operator_reply(v_data, NULL);
    IF pg_catalog.octet_length(v_reply::text) > 16 * 1024 * 1024 THEN
        RAISE SQLSTATE 'P0002' USING DETAIL = 'resource_budget_exceeded';
    END IF;
    RETURN aap_assets_v1._operator_write_receipt(
        operation_id, 'create_image_dataset', v_intent, v_reply
    );
EXCEPTION
    WHEN SQLSTATE 'P0002' THEN
        GET STACKED DIAGNOSTICS v_code = PG_EXCEPTION_DETAIL;
        RETURN aap_assets_v1._operator_reply(NULL, v_code);
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'operation_busy');
    WHEN unique_violation THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'operation_conflict');
    WHEN foreign_key_violation THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'user_missing');
    WHEN OTHERS THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'storage_unavailable');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.create_operator_image_dataset(uuid, uuid, text, text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.ensure_operator_nuscenes_dataset(
    operation_id uuid,
    owner_id uuid,
    spec jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_spec jsonb;
    v_scene_names jsonb;
    v_request jsonb;
    v_intent jsonb;
    v_reply jsonb;
    v_data jsonb;
    v_dataset public.datasets%ROWTYPE;
    v_dataset_id uuid;
    v_catalog jsonb;
    v_catalog_dataset jsonb;
    v_catalog_axis text;
    v_existing_axis text;
    v_existing_source jsonb;
    v_source jsonb;
    v_metadata jsonb;
    v_stored_root text;
    v_storage_layout text;
    v_storage_root text;
    v_expected_root text;
    v_uses_legacy_root boolean;
    v_needs_pcd_upgrade boolean;
    v_axis_reply jsonb;
    v_code text;
BEGIN
    IF operation_id IS NULL OR owner_id IS NULL OR spec IS NULL THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'invalid_request');
    END IF;
    IF pg_catalog.jsonb_typeof(spec) IS DISTINCT FROM 'object' THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'invalid_request');
    END IF;
    IF (spec - ARRAY[
            'display_id', 'name', 'version', 'scene_names',
            'axis_convention', 'frame', 'pcd_encoding'
       ]) <> '{}'::jsonb
       OR NOT (spec ?& ARRAY[
            'display_id', 'name', 'version', 'scene_names',
            'axis_convention', 'frame', 'pcd_encoding'
       ]) THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'invalid_request');
    END IF;
    IF pg_catalog.octet_length(spec::text) > 8 * 1024 * 1024 THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'resource_budget_exceeded');
    END IF;
    IF pg_catalog.jsonb_typeof(spec -> 'display_id') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(spec ->> 'display_id') NOT BETWEEN 1 AND 20
       OR pg_catalog.jsonb_typeof(spec -> 'name') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(spec ->> 'name') NOT BETWEEN 1 AND 255
       OR pg_catalog.jsonb_typeof(spec -> 'version') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(spec ->> 'version') < 1
       OR pg_catalog.jsonb_typeof(spec -> 'pcd_encoding') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(spec ->> 'pcd_encoding') < 1
       OR pg_catalog.jsonb_typeof(spec -> 'axis_convention') IS DISTINCT FROM 'string'
       OR spec ->> 'axis_convention' NOT IN ('iso_8855', 'apollo')
       OR pg_catalog.jsonb_typeof(spec -> 'frame') IS DISTINCT FROM 'string'
       OR spec ->> 'frame' NOT IN ('sensor', 'ego')
       OR spec ->> 'axis_convention' IS DISTINCT FROM CASE
            WHEN spec ->> 'frame' = 'ego' THEN 'iso_8855'
            ELSE 'apollo'
       END
       OR pg_catalog.jsonb_typeof(spec -> 'scene_names') IS DISTINCT FROM 'array' THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'invalid_request');
    END IF;
    IF pg_catalog.jsonb_array_length(spec -> 'scene_names') > 10000
       OR EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(spec -> 'scene_names') AS scene(value)
            WHERE pg_catalog.jsonb_typeof(scene.value) IS DISTINCT FROM 'string'
       ) THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'invalid_request');
    END IF;
    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.to_jsonb(scene.value)
            ORDER BY scene.value COLLATE "C"
        ), '[]'::jsonb
    )
    INTO v_scene_names
    FROM pg_catalog.jsonb_array_elements_text(spec -> 'scene_names') AS scene(value);
    v_spec := spec || pg_catalog.jsonb_build_object('scene_names', v_scene_names);
    v_request := pg_catalog.jsonb_build_object(
        'operation_id', operation_id, 'owner_id', owner_id, 'spec', v_spec
    );
    v_intent := pg_catalog.jsonb_build_object('owner_id', owner_id, 'spec', v_spec);
    IF pg_catalog.octet_length(v_request::text) > 8 * 1024 * 1024 THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'resource_budget_exceeded');
    END IF;
    v_reply := aap_assets_v1._operator_read_receipt(
        operation_id, 'ensure_nuscenes_dataset', v_intent
    );
    IF v_reply IS NOT NULL THEN RETURN v_reply; END IF;

    -- Preserve the legacy actor-before-Dataset read-eligibility fence.
    SELECT dataset.id INTO v_dataset_id
    FROM public.datasets AS dataset
    WHERE dataset.display_id = v_spec ->> 'display_id';
    IF FOUND THEN
        v_catalog := aap_assets_v1.get_catalog_dataset(owner_id, v_dataset_id);
        IF v_catalog IS NULL
           OR pg_catalog.jsonb_typeof(v_catalog) IS DISTINCT FROM 'object'
           OR v_catalog ->> 'schema' IS DISTINCT FROM 'aap-assets-catalog/v1'
           OR pg_catalog.jsonb_typeof(v_catalog -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._operator_reply(NULL, 'invalid_response');
        END IF;
        IF v_catalog -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            IF (v_catalog - ARRAY['schema', 'ok', 'error']) <> '{}'::jsonb
               OR pg_catalog.jsonb_typeof(v_catalog -> 'error') IS DISTINCT FROM 'object'
               OR (v_catalog -> 'error') - ARRAY['code'] <> '{}'::jsonb THEN
                RETURN aap_assets_v1._operator_reply(NULL, 'invalid_response');
            END IF;
            v_code := v_catalog #>> '{error,code}';
            IF v_code IN ('account_not_active', 'account_busy', 'dataset_missing') THEN
                RETURN aap_assets_v1._operator_reply(NULL, v_code);
            END IF;
            RETURN aap_assets_v1._operator_reply(NULL, 'invalid_response');
        END IF;
        IF (v_catalog - ARRAY['schema', 'ok', 'data']) <> '{}'::jsonb
           OR pg_catalog.jsonb_typeof(v_catalog -> 'data') IS DISTINCT FROM 'object'
           OR (v_catalog -> 'data') - ARRAY['dataset'] <> '{}'::jsonb
           OR pg_catalog.jsonb_typeof(v_catalog #> '{data,dataset}') IS DISTINCT FROM 'object'
           OR v_catalog #>> '{data,dataset,id}' IS DISTINCT FROM v_dataset_id::text THEN
            RETURN aap_assets_v1._operator_reply(NULL, 'invalid_response');
        END IF;
        v_catalog_dataset := v_catalog #> '{data,dataset}';
        IF v_catalog_dataset -> 'axis_convention' IS DISTINCT FROM 'null'::jsonb THEN
            IF pg_catalog.jsonb_typeof(v_catalog_dataset -> 'axis_convention') IS DISTINCT FROM 'string' THEN
                RETURN aap_assets_v1._operator_reply(NULL, 'invalid_response');
            END IF;
            v_catalog_axis := v_catalog_dataset ->> 'axis_convention';
            IF v_catalog_axis NOT IN (
                'iso_8855', 'ros_rep103', 'kitti_camera', 'opencv_camera',
                'apollo', 'y_forward', 'sustechpoints_demo', 'raw'
            ) THEN
                RETURN aap_assets_v1._operator_reply(NULL, 'invalid_response');
            END IF;
            IF v_catalog_axis IS DISTINCT FROM v_spec ->> 'axis_convention' THEN
                RETURN aap_assets_v1._operator_reply(NULL, 'axis_conflict');
            END IF;
        END IF;
        SELECT dataset.* INTO v_dataset
        FROM public.datasets AS dataset
        WHERE dataset.id = v_dataset_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            RETURN aap_assets_v1._operator_reply(NULL, 'dataset_missing');
        END IF;
        IF pg_catalog.jsonb_typeof(v_dataset.metadata) IS DISTINCT FROM 'object' THEN
            RETURN aap_assets_v1._operator_reply(NULL, 'invalid_response');
        END IF;
        v_existing_axis := v_dataset.metadata ->> 'axis_convention';
        IF v_existing_axis IS NOT NULL THEN
            IF v_existing_axis NOT IN (
                'iso_8855', 'ros_rep103', 'kitti_camera', 'opencv_camera',
                'apollo', 'y_forward', 'sustechpoints_demo', 'raw'
            ) THEN
                RETURN aap_assets_v1._operator_reply(NULL, 'invalid_response');
            END IF;
            IF v_existing_axis IS DISTINCT FROM v_spec ->> 'axis_convention' THEN
                RETURN aap_assets_v1._operator_reply(NULL, 'axis_conflict');
            END IF;
        END IF;

        v_metadata := v_dataset.metadata;
        v_existing_source := v_metadata -> 'source';
        IF pg_catalog.jsonb_typeof(v_existing_source) IS DISTINCT FROM 'object' THEN
            v_existing_source := NULL;
        END IF;
        v_stored_root := NULL;
        IF v_existing_source ? 'storage_root'
           AND v_existing_source -> 'storage_root' <> 'null'::jsonb THEN
            IF pg_catalog.jsonb_typeof(v_existing_source -> 'storage_root') = 'string' THEN
                IF v_existing_source ->> 'storage_root' <> '' THEN
                    v_stored_root := v_existing_source ->> 'storage_root';
                END IF;
            ELSIF (v_existing_source -> 'storage_root') NOT IN (
                'false'::jsonb, '0'::jsonb, '""'::jsonb, '{}'::jsonb, '[]'::jsonb
            ) THEN
                IF pg_catalog.jsonb_typeof(v_existing_source -> 'storage_root') = 'number' THEN
                    v_stored_root := v_existing_source ->> 'storage_root';
                ELSE
                    RETURN aap_assets_v1._operator_reply(NULL, 'storage_root_conflict');
                END IF;
            END IF;
        END IF;
        v_storage_layout := v_existing_source ->> 'storage_layout';
        v_expected_root := '__aap_trusted_nuscenes__-' ||
            pg_catalog.replace(v_dataset.id::text, '-', '');
        v_uses_legacy_root := v_stored_root IS NULL OR (
            v_storage_layout = 'legacy_name'
            AND v_stored_root = v_spec ->> 'name'
        );
        IF v_stored_root IS NOT NULL
           AND v_stored_root IS DISTINCT FROM v_expected_root
           AND NOT v_uses_legacy_root THEN
            RETURN aap_assets_v1._operator_reply(NULL, 'storage_root_conflict');
        END IF;
        v_storage_root := COALESCE(v_stored_root, v_spec ->> 'name');
        IF NOT aap_assets_v1._operator_path_valid(v_storage_root) THEN
            RETURN aap_assets_v1._operator_reply(NULL, 'storage_root_conflict');
        END IF;
        IF v_uses_legacy_root AND EXISTS (
            SELECT 1
            FROM public.dataset_items AS item
            WHERE item.dataset_id <> v_dataset.id
              AND pg_catalog.left(
                    item.file_path, pg_catalog.char_length(v_storage_root || '/')
                  ) = v_storage_root || '/'
        ) THEN
            RETURN aap_assets_v1._operator_reply(NULL, 'storage_root_conflict');
        END IF;
        v_needs_pcd_upgrade := v_existing_source IS NULL
            OR v_existing_source -> 'pcd_encoding'
               IS DISTINCT FROM pg_catalog.to_jsonb(v_spec ->> 'pcd_encoding');
        v_source := pg_catalog.jsonb_build_object(
            'format', 'nuscenes',
            'version', v_spec ->> 'version',
            'scenes', v_scene_names,
            'pcd_encoding', v_spec ->> 'pcd_encoding',
            'storage_root', v_storage_root,
            'storage_layout', CASE WHEN v_uses_legacy_root THEN 'legacy_name' ELSE 'uuid_v1' END
        );
        v_metadata := v_metadata || pg_catalog.jsonb_build_object('source', v_source);
        IF v_metadata IS DISTINCT FROM v_dataset.metadata OR NOT v_dataset.is_temporal THEN
            UPDATE public.datasets AS dataset
            SET metadata = v_metadata,
                is_temporal = true,
                updated_at = pg_catalog.clock_timestamp()
            WHERE dataset.id = v_dataset.id;
        END IF;
    ELSE
        IF NOT aap_assets_v1._operator_user_exists(owner_id) THEN
            RETURN aap_assets_v1._operator_reply(NULL, 'user_missing');
        END IF;
        v_dataset_id := pg_catalog.gen_random_uuid();
        v_storage_root := '__aap_trusted_nuscenes__-' ||
            pg_catalog.replace(v_dataset_id::text, '-', '');
        v_source := pg_catalog.jsonb_build_object(
            'format', 'nuscenes',
            'version', v_spec ->> 'version',
            'scenes', v_scene_names,
            'pcd_encoding', v_spec ->> 'pcd_encoding',
            'storage_root', v_storage_root,
            'storage_layout', 'uuid_v1'
        );
        INSERT INTO public.datasets (
            id, display_id, name, description, data_type, is_temporal,
            file_count, created_by, metadata
        ) VALUES (
            v_dataset_id, v_spec ->> 'display_id', v_spec ->> 'name', '',
            'point_cloud', true, 0, owner_id,
            pg_catalog.jsonb_build_object('source', v_source)
        ) RETURNING * INTO v_dataset;
        v_needs_pcd_upgrade := false;
    END IF;

    v_axis_reply := aap_assets_v1.set_spatial_dataset_axis(
        pg_catalog.gen_random_uuid(), v_dataset_id, v_spec ->> 'axis_convention'
    );
    IF v_axis_reply IS NULL
       OR pg_catalog.jsonb_typeof(v_axis_reply) IS DISTINCT FROM 'object'
       OR v_axis_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-spatial/v1'
       OR v_axis_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR (v_axis_reply - ARRAY['schema', 'ok', 'data']) <> '{}'::jsonb
       OR pg_catalog.jsonb_typeof(v_axis_reply -> 'data') IS DISTINCT FROM 'object'
       OR v_axis_reply #>> '{data,dataset_id}' IS DISTINCT FROM v_dataset_id::text
       OR v_axis_reply #>> '{data,axis_convention}' IS DISTINCT FROM v_spec ->> 'axis_convention' THEN
        v_code := CASE
            WHEN v_axis_reply -> 'ok' = 'false'::jsonb
                 AND v_axis_reply #>> '{error,code}' = 'dataset_busy'
                THEN 'operation_busy'
            WHEN v_axis_reply -> 'ok' = 'false'::jsonb
                 AND v_axis_reply #>> '{error,code}' = 'operation_conflict'
                THEN 'operation_conflict'
            ELSE 'invalid_response'
        END;
        RAISE SQLSTATE 'P0002' USING DETAIL = v_code;
    END IF;
    SELECT dataset.* INTO v_dataset
    FROM public.datasets AS dataset
    WHERE dataset.id = v_dataset_id;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'P0002' USING DETAIL = 'dataset_missing';
    END IF;
    v_data := pg_catalog.jsonb_build_object(
        'operation_id', operation_id,
        'dataset_id', v_dataset_id,
        'action', 'ensure_nuscenes_dataset',
        'dataset', aap_assets_v1._operator_dataset_json(v_dataset),
        'storage_root', v_storage_root,
        'needs_pcd_upgrade', v_needs_pcd_upgrade
    );
    v_reply := aap_assets_v1._operator_reply(v_data, NULL);
    IF pg_catalog.octet_length(v_reply::text) > 16 * 1024 * 1024 THEN
        RAISE SQLSTATE 'P0002' USING DETAIL = 'resource_budget_exceeded';
    END IF;
    RETURN aap_assets_v1._operator_write_receipt(
        operation_id, 'ensure_nuscenes_dataset', v_intent, v_reply
    );
EXCEPTION
    WHEN SQLSTATE 'P0002' THEN
        GET STACKED DIAGNOSTICS v_code = PG_EXCEPTION_DETAIL;
        RETURN aap_assets_v1._operator_reply(NULL, v_code);
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'operation_busy');
    WHEN unique_violation THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'storage_root_conflict');
    WHEN foreign_key_violation THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'user_missing');
    WHEN OTHERS THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'storage_unavailable');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.ensure_operator_nuscenes_dataset(uuid, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.append_operator_items(
    operation_id uuid,
    dataset_id uuid,
    entries jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_request jsonb;
    v_intent jsonb;
    v_entries jsonb;
    v_entry jsonb;
    v_item public.dataset_items%ROWTYPE;
    v_dataset public.datasets%ROWTYPE;
    v_items jsonb := '[]'::jsonb;
    v_data jsonb;
    v_reply jsonb;
    v_new_count bigint;
    v_code text;
BEGIN
    IF operation_id IS NULL OR dataset_id IS NULL OR entries IS NULL
       OR pg_catalog.jsonb_typeof(entries) IS DISTINCT FROM 'array' THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'invalid_request');
    END IF;
    IF pg_catalog.jsonb_array_length(entries) NOT BETWEEN 1 AND 500 THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'invalid_request');
    END IF;
    IF pg_catalog.octet_length(entries::text) > 8 * 1024 * 1024 THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'resource_budget_exceeded');
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(entries) AS entry(value)
        WHERE NOT aap_assets_v1._operator_source_entry_valid(entry.value)
    ) THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'invalid_request');
    END IF;
    IF (
        SELECT pg_catalog.count(DISTINCT (entry.value ->> 'id')::uuid)
        FROM pg_catalog.jsonb_array_elements(entries) AS entry(value)
    ) <> pg_catalog.jsonb_array_length(entries) THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'invalid_request');
    END IF;
    SELECT pg_catalog.jsonb_agg(
        pg_catalog.jsonb_build_object(
            'id', (entry.value ->> 'id')::uuid,
            'file_name', entry.value ->> 'file_name',
            'file_path', entry.value ->> 'file_path',
            'file_type', entry.value ->> 'file_type',
            'file_size', COALESCE(entry.value -> 'file_size', 'null'::jsonb),
            'content_hash', COALESCE(entry.value -> 'content_hash', 'null'::jsonb),
            'width', COALESCE(entry.value -> 'width', 'null'::jsonb),
            'height', COALESCE(entry.value -> 'height', 'null'::jsonb),
            'metadata', COALESCE(entry.value -> 'metadata', '{}'::jsonb)
        ) ORDER BY entry.ordinality
    ) INTO v_entries
    FROM pg_catalog.jsonb_array_elements(entries) WITH ORDINALITY AS entry(value, ordinality);
    v_intent := pg_catalog.jsonb_build_object(
        'dataset_id', dataset_id, 'entries', v_entries
    );
    v_request := pg_catalog.jsonb_build_object(
        'operation_id', operation_id,
        'dataset_id', dataset_id,
        'entries', v_entries
    );
    IF pg_catalog.octet_length(v_request::text) > 8 * 1024 * 1024 THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'resource_budget_exceeded');
    END IF;
    v_reply := aap_assets_v1._operator_read_receipt(
        operation_id, 'append_items', v_intent
    );
    IF v_reply IS NOT NULL THEN RETURN v_reply; END IF;

    SELECT dataset.* INTO v_dataset
    FROM public.datasets AS dataset
    WHERE dataset.id = dataset_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'dataset_missing');
    END IF;
    SELECT COALESCE(v_dataset.file_count, 0)::bigint
        + pg_catalog.jsonb_array_length(v_entries)
    INTO v_new_count;
    IF v_new_count > 2147483647 THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'resource_budget_exceeded');
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(v_entries) AS entry(value)
        JOIN public.dataset_items AS item
          ON item.id = (entry.value ->> 'id')::uuid
    ) THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'source_changed');
    END IF;

    FOR v_entry IN
        SELECT entry.value
        FROM pg_catalog.jsonb_array_elements(v_entries) WITH ORDINALITY AS entry(value, ordinality)
        ORDER BY entry.ordinality
    LOOP
        INSERT INTO public.dataset_items (
            id, dataset_id, file_name, file_path, file_type,
            file_size, content_hash, width, height, metadata
        ) VALUES (
            (v_entry ->> 'id')::uuid,
            dataset_id,
            v_entry ->> 'file_name',
            v_entry ->> 'file_path',
            v_entry ->> 'file_type',
            (v_entry ->> 'file_size')::bigint,
            v_entry ->> 'content_hash',
            (v_entry ->> 'width')::integer,
            (v_entry ->> 'height')::integer,
            v_entry -> 'metadata'
        ) RETURNING * INTO v_item;
        IF pg_catalog.jsonb_typeof(v_item.metadata) IS DISTINCT FROM 'object' THEN
            RAISE SQLSTATE 'P0002' USING DETAIL = 'invalid_response';
        END IF;
        v_items := v_items || pg_catalog.jsonb_build_array(
            aap_assets_v1._operator_item_json(v_item)
        );
    END LOOP;
    UPDATE public.datasets AS dataset
    SET file_count = v_new_count::integer,
        updated_at = pg_catalog.clock_timestamp()
    WHERE dataset.id = dataset_id;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'P0002' USING DETAIL = 'dataset_missing';
    END IF;
    v_data := pg_catalog.jsonb_build_object(
        'operation_id', operation_id,
        'dataset_id', dataset_id,
        'action', 'append_items',
        'items', v_items,
        'file_count', v_new_count,
        'dimensions_backfilled', 0
    );
    v_reply := aap_assets_v1._operator_reply(v_data, NULL);
    IF pg_catalog.octet_length(v_reply::text) > 16 * 1024 * 1024 THEN
        RAISE SQLSTATE 'P0002' USING DETAIL = 'resource_budget_exceeded';
    END IF;
    RETURN aap_assets_v1._operator_write_receipt(
        operation_id, 'append_items', v_intent, v_reply
    );
EXCEPTION
    WHEN SQLSTATE 'P0002' THEN
        GET STACKED DIAGNOSTICS v_code = PG_EXCEPTION_DETAIL;
        RETURN aap_assets_v1._operator_reply(NULL, v_code);
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'operation_busy');
    WHEN unique_violation THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'source_changed');
    WHEN foreign_key_violation THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'dataset_missing');
    WHEN OTHERS THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'storage_unavailable');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.append_operator_items(uuid, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.patch_operator_items(
    operation_id uuid,
    dataset_id uuid,
    entries jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_request jsonb;
    v_intent jsonb;
    v_entries jsonb;
    v_entry jsonb;
    v_item public.dataset_items%ROWTYPE;
    v_dataset public.datasets%ROWTYPE;
    v_item_ids uuid[];
    v_locked_count integer := 0;
    v_items jsonb := '[]'::jsonb;
    v_metadata jsonb;
    v_width integer;
    v_height integer;
    v_file_size bigint;
    v_content_hash text;
    v_dimensions_backfilled integer := 0;
    v_data jsonb;
    v_reply jsonb;
    v_code text;
BEGIN
    IF operation_id IS NULL OR dataset_id IS NULL OR entries IS NULL
       OR pg_catalog.jsonb_typeof(entries) IS DISTINCT FROM 'array' THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'invalid_request');
    END IF;
    IF pg_catalog.jsonb_array_length(entries) NOT BETWEEN 1 AND 500 THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'invalid_request');
    END IF;
    IF pg_catalog.octet_length(entries::text) > 8 * 1024 * 1024 THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'resource_budget_exceeded');
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(entries) AS entry(value)
        WHERE NOT aap_assets_v1._operator_source_patch_valid(entry.value)
    ) THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'invalid_request');
    END IF;
    IF (
        SELECT pg_catalog.count(DISTINCT (entry.value ->> 'id')::uuid)
        FROM pg_catalog.jsonb_array_elements(entries) AS entry(value)
    ) <> pg_catalog.jsonb_array_length(entries) THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'invalid_request');
    END IF;
    SELECT pg_catalog.jsonb_agg(
        pg_catalog.jsonb_build_object(
            'id', (entry.value ->> 'id')::uuid,
            'source_snapshot', entry.value ->> 'source_snapshot',
            'nuscenes_export', COALESCE(entry.value -> 'nuscenes_export', 'null'::jsonb),
            'dimensions', COALESCE(entry.value -> 'dimensions', 'null'::jsonb),
            'file_size', COALESCE(entry.value -> 'file_size', 'null'::jsonb),
            'content_hash', COALESCE(entry.value -> 'content_hash', 'null'::jsonb)
        ) ORDER BY entry.ordinality
    ) INTO v_entries
    FROM pg_catalog.jsonb_array_elements(entries) WITH ORDINALITY AS entry(value, ordinality);
    v_intent := pg_catalog.jsonb_build_object(
        'dataset_id', dataset_id, 'entries', v_entries
    );
    v_request := pg_catalog.jsonb_build_object(
        'operation_id', operation_id,
        'dataset_id', dataset_id,
        'entries', v_entries
    );
    IF pg_catalog.octet_length(v_request::text) > 8 * 1024 * 1024 THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'resource_budget_exceeded');
    END IF;
    v_reply := aap_assets_v1._operator_read_receipt(
        operation_id, 'patch_items', v_intent
    );
    IF v_reply IS NOT NULL THEN RETURN v_reply; END IF;

    SELECT dataset.* INTO v_dataset
    FROM public.datasets AS dataset
    WHERE dataset.id = dataset_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'dataset_missing');
    END IF;
    SELECT pg_catalog.array_agg((entry.value ->> 'id')::uuid ORDER BY entry.ordinality)
    INTO v_item_ids
    FROM pg_catalog.jsonb_array_elements(v_entries) WITH ORDINALITY AS entry(value, ordinality);
    FOR v_item IN
        SELECT item.*
        FROM public.dataset_items AS item
        WHERE item.dataset_id = dataset_id AND item.id = ANY(v_item_ids)
        ORDER BY item.id
        FOR UPDATE NOWAIT
    LOOP
        v_locked_count := v_locked_count + 1;
    END LOOP;
    IF v_locked_count <> pg_catalog.cardinality(v_item_ids) THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'item_missing');
    END IF;

    -- Verify every opaque snapshot before making the first row change.
    FOR v_entry IN
        SELECT entry.value
        FROM pg_catalog.jsonb_array_elements(v_entries) WITH ORDINALITY AS entry(value, ordinality)
        ORDER BY entry.ordinality
    LOOP
        SELECT item.* INTO v_item
        FROM public.dataset_items AS item
        WHERE item.dataset_id = dataset_id
          AND item.id = (v_entry ->> 'id')::uuid;
        IF NOT FOUND THEN
            RETURN aap_assets_v1._operator_reply(NULL, 'item_missing');
        END IF;
        IF pg_catalog.jsonb_typeof(v_item.metadata) IS DISTINCT FROM 'object' THEN
            RETURN aap_assets_v1._operator_reply(NULL, 'invalid_response');
        END IF;
        IF aap_assets_v1._operator_source_snapshot(v_item)
           IS DISTINCT FROM v_entry ->> 'source_snapshot' THEN
            RETURN aap_assets_v1._operator_reply(NULL, 'source_changed');
        END IF;
    END LOOP;

    FOR v_entry IN
        SELECT entry.value
        FROM pg_catalog.jsonb_array_elements(v_entries) WITH ORDINALITY AS entry(value, ordinality)
        ORDER BY entry.ordinality
    LOOP
        SELECT item.* INTO v_item
        FROM public.dataset_items AS item
        WHERE item.dataset_id = dataset_id
          AND item.id = (v_entry ->> 'id')::uuid;
        v_metadata := v_item.metadata;
        v_width := v_item.width;
        v_height := v_item.height;
        v_file_size := v_item.file_size;
        v_content_hash := v_item.content_hash;
        IF v_entry -> 'nuscenes_export' <> 'null'::jsonb THEN
            v_metadata := v_metadata || pg_catalog.jsonb_build_object(
                'nuscenes_export', v_entry -> 'nuscenes_export'
            );
        END IF;
        IF v_entry -> 'dimensions' <> 'null'::jsonb
           AND (COALESCE(v_width, 0) = 0 OR COALESCE(v_height, 0) = 0) THEN
            v_width := (v_entry #>> '{dimensions,width}')::integer;
            v_height := (v_entry #>> '{dimensions,height}')::integer;
            v_dimensions_backfilled := v_dimensions_backfilled + 1;
        END IF;
        IF v_entry -> 'file_size' <> 'null'::jsonb THEN
            v_file_size := (v_entry ->> 'file_size')::bigint;
        END IF;
        IF v_entry -> 'content_hash' <> 'null'::jsonb THEN
            v_content_hash := v_entry ->> 'content_hash';
        END IF;
        IF v_metadata IS DISTINCT FROM v_item.metadata
           OR v_width IS DISTINCT FROM v_item.width
           OR v_height IS DISTINCT FROM v_item.height
           OR v_file_size IS DISTINCT FROM v_item.file_size
           OR v_content_hash IS DISTINCT FROM v_item.content_hash THEN
            UPDATE public.dataset_items AS item
            SET metadata = v_metadata,
                width = v_width,
                height = v_height,
                file_size = v_file_size,
                content_hash = v_content_hash,
                updated_at = pg_catalog.clock_timestamp()
            WHERE item.id = v_item.id
            RETURNING * INTO v_item;
        END IF;
        v_items := v_items || pg_catalog.jsonb_build_array(
            aap_assets_v1._operator_item_json(v_item)
        );
    END LOOP;
    v_data := pg_catalog.jsonb_build_object(
        'operation_id', operation_id,
        'dataset_id', dataset_id,
        'action', 'patch_items',
        'items', v_items,
        'file_count', COALESCE(v_dataset.file_count, 0),
        'dimensions_backfilled', v_dimensions_backfilled
    );
    v_reply := aap_assets_v1._operator_reply(v_data, NULL);
    IF pg_catalog.octet_length(v_reply::text) > 16 * 1024 * 1024 THEN
        RAISE SQLSTATE 'P0002' USING DETAIL = 'resource_budget_exceeded';
    END IF;
    RETURN aap_assets_v1._operator_write_receipt(
        operation_id, 'patch_items', v_intent, v_reply
    );
EXCEPTION
    WHEN SQLSTATE 'P0002' THEN
        GET STACKED DIAGNOSTICS v_code = PG_EXCEPTION_DETAIL;
        RETURN aap_assets_v1._operator_reply(NULL, v_code);
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'operation_busy');
    WHEN unique_violation THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'source_changed');
    WHEN foreign_key_violation THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'dataset_missing');
    WHEN OTHERS THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'storage_unavailable');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.patch_operator_items(uuid, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.recount_operator_items(
    operation_id uuid,
    dataset_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_request jsonb;
    v_intent jsonb;
    v_reply jsonb;
    v_data jsonb;
    v_dataset public.datasets%ROWTYPE;
    v_count bigint;
    v_code text;
BEGIN
    IF operation_id IS NULL OR dataset_id IS NULL THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'invalid_request');
    END IF;
    v_request := pg_catalog.jsonb_build_object(
        'operation_id', operation_id, 'dataset_id', dataset_id
    );
    IF pg_catalog.octet_length(v_request::text) > 8 * 1024 * 1024 THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'resource_budget_exceeded');
    END IF;
    v_intent := pg_catalog.jsonb_build_object('dataset_id', dataset_id);
    v_reply := aap_assets_v1._operator_read_receipt(
        operation_id, 'recount_items', v_intent
    );
    IF v_reply IS NOT NULL THEN RETURN v_reply; END IF;
    SELECT dataset.* INTO v_dataset
    FROM public.datasets AS dataset
    WHERE dataset.id = dataset_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'dataset_missing');
    END IF;
    SELECT pg_catalog.count(*) INTO v_count
    FROM public.dataset_items AS item
    WHERE item.dataset_id = dataset_id;
    IF v_count > 2147483647 THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'resource_budget_exceeded');
    END IF;
    IF v_dataset.file_count IS DISTINCT FROM v_count::integer THEN
        UPDATE public.datasets AS dataset
        SET file_count = v_count::integer,
            updated_at = pg_catalog.clock_timestamp()
        WHERE dataset.id = dataset_id;
        IF NOT FOUND THEN
            RAISE SQLSTATE 'P0002' USING DETAIL = 'dataset_missing';
        END IF;
    END IF;
    v_data := pg_catalog.jsonb_build_object(
        'operation_id', operation_id,
        'dataset_id', dataset_id,
        'action', 'recount_items',
        'file_count', v_count
    );
    v_reply := aap_assets_v1._operator_reply(v_data, NULL);
    IF pg_catalog.octet_length(v_reply::text) > 16 * 1024 * 1024 THEN
        RAISE SQLSTATE 'P0002' USING DETAIL = 'resource_budget_exceeded';
    END IF;
    RETURN aap_assets_v1._operator_write_receipt(
        operation_id, 'recount_items', v_intent, v_reply
    );
EXCEPTION
    WHEN SQLSTATE 'P0002' THEN
        GET STACKED DIAGNOSTICS v_code = PG_EXCEPTION_DETAIL;
        RETURN aap_assets_v1._operator_reply(NULL, v_code);
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'operation_busy');
    WHEN OTHERS THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'storage_unavailable');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.recount_operator_items(uuid, uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.read_operator_items(
    dataset_id uuid,
    after_item_id uuid,
    page_size integer
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_request jsonb;
    v_reply jsonb;
    v_data jsonb;
    v_row record;
    v_total bigint := 0;
    v_snapshot_digest text;
    v_items jsonb := '[]'::jsonb;
    v_item_count integer := 0;
    v_record_bytes integer;
    v_page_bytes bigint := 2;
    v_next_item_id uuid;
    v_has_more boolean := false;
    v_complete boolean := true;
BEGIN
    IF dataset_id IS NULL OR page_size IS NULL OR page_size < 1 OR page_size > 500 THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'invalid_request');
    END IF;
    v_request := pg_catalog.jsonb_build_object(
        'dataset_id', dataset_id,
        'after_item_id', after_item_id,
        'page_size', page_size
    );
    IF pg_catalog.octet_length(v_request::text) > 8 * 1024 * 1024 THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'resource_budget_exceeded');
    END IF;

    FOR v_row IN
        WITH target AS MATERIALIZED (
            SELECT dataset.id
            FROM public.datasets AS dataset
            WHERE dataset.id = $1
        ),
        snapshots AS MATERIALIZED (
            SELECT item.id,
                   aap_assets_v1._operator_source_snapshot(item) AS source_snapshot
            FROM public.dataset_items AS item
            JOIN target ON target.id = item.dataset_id
        ),
        summary AS MATERIALIZED (
            SELECT EXISTS (SELECT 1 FROM target) AS dataset_exists,
                   pg_catalog.count(*)::bigint AS total,
                   (
                       SELECT last_snapshot.id
                       FROM snapshots AS last_snapshot
                       ORDER BY last_snapshot.id DESC
                       LIMIT 1
                   ) AS last_item_id,
                   pg_catalog.encode(
                       public.digest(
                           pg_catalog.convert_to(
                               COALESCE(
                                   pg_catalog.string_agg(
                                       snapshot.id::text || ':' || snapshot.source_snapshot,
                                       E'\n' ORDER BY snapshot.id
                                   ),
                                   ''
                               ),
                               'UTF8'
                           ),
                           'sha256'
                       ),
                       'hex'
                   ) AS snapshot_digest
            FROM snapshots AS snapshot
        ),
        page_items AS MATERIALIZED (
            SELECT item AS item_row,
                   item.id,
                   snapshot.source_snapshot
            FROM public.dataset_items AS item
            JOIN snapshots AS snapshot ON snapshot.id = item.id
            WHERE item.dataset_id = $1
              AND (
                  $2 IS NULL
                  OR item.id > $2
              )
            ORDER BY item.id
            LIMIT $3
        )
        SELECT summary.dataset_exists,
               summary.total,
               summary.snapshot_digest,
               page_items.id AS item_id,
               CASE WHEN page_items.id IS NULL THEN NULL
                    ELSE aap_assets_v1._operator_item_json(
                        page_items.item_row, page_items.source_snapshot
                    )
               END AS item_json,
               (page_items.id IS NOT NULL
                   AND page_items.id < summary.last_item_id) AS has_more
        FROM summary
        LEFT JOIN page_items ON true
        ORDER BY page_items.id
    LOOP
        IF NOT v_row.dataset_exists THEN
            RETURN aap_assets_v1._operator_reply(NULL, 'dataset_missing');
        END IF;
        v_total := v_row.total;
        v_snapshot_digest := v_row.snapshot_digest;
        IF v_row.item_id IS NULL THEN
            CONTINUE;
        END IF;
        IF pg_catalog.jsonb_typeof(v_row.item_json -> 'metadata') IS DISTINCT FROM 'object' THEN
            RETURN aap_assets_v1._operator_reply(NULL, 'invalid_response');
        END IF;
        v_record_bytes := pg_catalog.octet_length(v_row.item_json::text);
        IF v_record_bytes + 2 > 15 * 1024 * 1024 THEN
            IF v_item_count = 0 THEN
                RETURN aap_assets_v1._operator_reply(NULL, 'resource_budget_exceeded');
            END IF;
            EXIT;
        END IF;
        IF v_page_bytes + v_record_bytes
           + (CASE WHEN v_item_count > 0 THEN 2 ELSE 0 END)
           > 15 * 1024 * 1024 THEN
            EXIT;
        END IF;
        v_items := v_items || pg_catalog.jsonb_build_array(v_row.item_json);
        v_item_count := v_item_count + 1;
        v_page_bytes := v_page_bytes + v_record_bytes
            + CASE WHEN v_item_count > 1 THEN 2 ELSE 0 END;
        v_next_item_id := v_row.item_id;
        v_has_more := v_row.has_more;
    END LOOP;
    v_complete := NOT v_has_more;
    v_data := pg_catalog.jsonb_build_object(
        'dataset_id', dataset_id,
        'after_item_id', after_item_id,
        'next_item_id', CASE WHEN v_complete THEN NULL ELSE v_next_item_id END,
        'snapshot_digest', v_snapshot_digest,
        'total', v_total,
        'complete', v_complete,
        'items', v_items
    );
    v_reply := aap_assets_v1._operator_reply(v_data, NULL);
    IF pg_catalog.octet_length(v_reply::text) > 16 * 1024 * 1024 THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'resource_budget_exceeded');
    END IF;
    RETURN v_reply;
EXCEPTION
    WHEN OTHERS THEN
        RETURN aap_assets_v1._operator_reply(NULL, 'storage_unavailable');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_operator_items(uuid, uuid, integer) FROM PUBLIC;
-- aap-statement-break
