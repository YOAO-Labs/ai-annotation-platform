-- Legacy completion belongs to Assets; native calls never perform object I/O or commit.
CREATE TABLE public.dataset_legacy_upload_operations (
    operation_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    item_id uuid NOT NULL,
    request jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(request) = 'object'),
    result jsonb NOT NULL CHECK (
        pg_catalog.jsonb_typeof(result) = 'object'
        AND result ->> 'state' = 'completed'
        AND result ->> 'operation_id' = operation_id::text
        AND result ->> 'actor_id' = actor_id::text
        AND result ->> 'dataset_id' = dataset_id::text
        AND result ->> 'item_id' = item_id::text
    ),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now()
);
-- aap-statement-break
REVOKE ALL ON public.dataset_legacy_upload_operations FROM PUBLIC;
-- aap-statement-break
COMMENT ON TABLE public.dataset_legacy_upload_operations IS
    'Assets-owned immutable successful legacy upload completion receipts, without live resource foreign keys.';
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._legacy_upload_reply(p_data jsonb, p_code text)
RETURNS jsonb
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_code IS NOT NULL THEN
        pg_catalog.jsonb_build_object(
            'schema', 'aap-legacy-dataset-upload/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', p_code)
        )
    ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-legacy-dataset-upload/v1', 'ok', true, 'data', p_data
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._legacy_upload_reply(jsonb, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.lock_legacy_dataset_upload(
    p_operation_id uuid, p_actor_id uuid, p_dataset_id uuid, p_item_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_item public.dataset_items%ROWTYPE;
    v_saved public.dataset_legacy_upload_operations%ROWTYPE;
    v_dataset uuid;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL
       OR p_dataset_id IS NULL OR p_item_id IS NULL THEN
        RETURN aap_assets_v1._legacy_upload_reply(NULL, 'invalid_request');
    END IF;
    v_actor := aap_assets_v1._dataset_upload_actor(p_actor_id);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._legacy_upload_reply(NULL, v_actor #>> '{error,code}');
    END IF;
    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-legacy-upload:' || p_operation_id::text, 0)
    ) THEN
        RETURN aap_assets_v1._legacy_upload_reply(NULL, 'dataset_busy');
    END IF;
    SELECT operation.* INTO v_saved
    FROM public.dataset_legacy_upload_operations AS operation
    WHERE operation.operation_id = p_operation_id;
    IF FOUND AND (v_saved.actor_id, v_saved.dataset_id, v_saved.item_id)
       IS DISTINCT FROM (p_actor_id, p_dataset_id, p_item_id) THEN
        RETURN aap_assets_v1._legacy_upload_reply(NULL, 'operation_conflict');
    END IF;
    SELECT dataset.id INTO v_dataset
    FROM public.datasets AS dataset
    WHERE dataset.id = p_dataset_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._legacy_upload_reply(NULL, 'upload_missing');
    END IF;
    SELECT item.* INTO v_item
    FROM public.dataset_items AS item
    WHERE item.id = p_item_id AND item.dataset_id = p_dataset_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._legacy_upload_reply(NULL, 'upload_missing');
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.dataset_upload_intents AS intent
        WHERE intent.item_id = p_item_id
    ) THEN
        RETURN aap_assets_v1._legacy_upload_reply(NULL, 'upload_scope_mismatch');
    END IF;
    IF v_saved.operation_id IS NOT NULL THEN
        IF v_saved.result ->> 'file_path' IS DISTINCT FROM v_item.file_path
           OR v_saved.result ->> 'file_type' IS DISTINCT FROM v_item.file_type THEN
            RETURN aap_assets_v1._legacy_upload_reply(NULL, 'source_changed');
        END IF;
        RETURN aap_assets_v1._legacy_upload_reply(v_saved.result, NULL);
    END IF;
    RETURN aap_assets_v1._legacy_upload_reply(
        pg_catalog.jsonb_build_object(
            'operation_id', p_operation_id, 'actor_id', p_actor_id,
            'dataset_id', p_dataset_id, 'item_id', p_item_id,
            'file_path', v_item.file_path, 'file_type', v_item.file_type,
            'width', v_item.width, 'height', v_item.height,
            'state', 'pending', 'result', NULL, 'duplicate_of', NULL
        ), NULL
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._legacy_upload_reply(NULL, 'dataset_busy');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.lock_legacy_dataset_upload(uuid, uuid, uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.complete_legacy_dataset_upload(
    p_operation_id uuid, p_actor_id uuid, p_dataset_id uuid, p_item_id uuid,
    p_expected_file_path text, p_file_size bigint, p_content_hash text,
    p_width integer, p_height integer
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_locked jsonb;
    v_request jsonb;
    v_saved_request jsonb;
    v_duplicate uuid;
    v_append jsonb;
    v_created bigint;
    v_result jsonb;
    v_item public.dataset_items%ROWTYPE;
BEGIN
    IF p_expected_file_path IS NULL OR p_expected_file_path = ''
       OR p_file_size IS NULL OR p_file_size < 0
       OR (p_content_hash IS NOT NULL AND p_content_hash !~ '^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{64})$')
       OR (p_width IS NULL) <> (p_height IS NULL)
       OR p_width <= 0 OR p_height <= 0 THEN
        RETURN aap_assets_v1._legacy_upload_reply(NULL, 'invalid_request');
    END IF;
    p_content_hash := pg_catalog.lower(p_content_hash);
    v_request := pg_catalog.jsonb_build_object(
        'expected_file_path', p_expected_file_path, 'file_size', p_file_size,
        'content_hash', p_content_hash, 'width', p_width, 'height', p_height
    );
    v_locked := aap_assets_v1.lock_legacy_dataset_upload(
        p_operation_id, p_actor_id, p_dataset_id, p_item_id
    );
    IF v_locked -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_locked;
    END IF;
    IF v_locked #>> '{data,state}' = 'completed' THEN
        SELECT operation.request INTO v_saved_request
        FROM public.dataset_legacy_upload_operations AS operation
        WHERE operation.operation_id = p_operation_id;
        IF v_saved_request IS DISTINCT FROM v_request THEN
            RETURN aap_assets_v1._legacy_upload_reply(NULL, 'operation_conflict');
        END IF;
        RETURN v_locked;
    END IF;
    IF v_locked #>> '{data,file_path}' IS DISTINCT FROM p_expected_file_path THEN
        RETURN aap_assets_v1._legacy_upload_reply(NULL, 'source_changed');
    END IF;
    IF p_content_hash IS NOT NULL THEN
        SELECT item.id INTO v_duplicate
        FROM public.dataset_items AS item
        WHERE item.dataset_id = p_dataset_id AND item.id <> p_item_id
          AND item.content_hash = p_content_hash
        ORDER BY item.id LIMIT 1;
        IF v_duplicate IS NOT NULL THEN
            RETURN aap_assets_v1._legacy_upload_reply(
                (v_locked -> 'data') || pg_catalog.jsonb_build_object(
                    'state', 'duplicate', 'duplicate_of', v_duplicate
                ), NULL
            );
        END IF;
    END IF;
    UPDATE public.dataset_items AS item
    SET file_size = p_file_size,
        content_hash = COALESCE(p_content_hash, item.content_hash),
        width = CASE WHEN p_width IS NOT NULL AND (item.width IS NULL OR item.height IS NULL)
                     THEN p_width ELSE item.width END,
        height = CASE WHEN p_height IS NOT NULL AND (item.width IS NULL OR item.height IS NULL)
                      THEN p_height ELSE item.height END
    WHERE item.id = p_item_id AND item.dataset_id = p_dataset_id
    RETURNING item.* INTO v_item;
    v_append := aap_labeling_v1.append_dataset_items(
        p_dataset_id, ARRAY[p_item_id]::uuid[], p_operation_id
    );
    IF pg_catalog.jsonb_typeof(v_append) IS DISTINCT FROM 'object'
       OR (v_append - ARRAY['schema','operation_id','dataset_id','created','project_totals','associations']) <> '{}'::jsonb
       OR NOT (v_append ?& ARRAY['schema','operation_id','dataset_id','created','project_totals','associations'])
       OR v_append ->> 'schema' IS DISTINCT FROM 'aap-task-append/v1'
       OR v_append ->> 'operation_id' IS DISTINCT FROM p_operation_id::text
       OR v_append ->> 'dataset_id' IS DISTINCT FROM p_dataset_id::text
       OR pg_catalog.jsonb_typeof(v_append -> 'created') IS DISTINCT FROM 'number'
       OR (v_append ->> 'created') !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.jsonb_typeof(v_append -> 'project_totals') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_append -> 'associations') IS DISTINCT FROM 'array'
       OR EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(v_append -> 'project_totals') AS entry(value)
            WHERE pg_catalog.jsonb_typeof(entry.value) IS DISTINCT FROM 'object'
               OR (entry.value - ARRAY['project_id','total_tasks']) <> '{}'::jsonb
               OR NOT (entry.value ?& ARRAY['project_id','total_tasks'])
               OR pg_catalog.jsonb_typeof(entry.value -> 'project_id') IS DISTINCT FROM 'string'
               OR (entry.value ->> 'project_id') !~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
               OR pg_catalog.jsonb_typeof(entry.value -> 'total_tasks') IS DISTINCT FROM 'number'
               OR (entry.value ->> 'total_tasks') !~ '^(0|[1-9][0-9]*)$'
       )
       OR EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(v_append -> 'associations') AS entry(value)
            WHERE pg_catalog.jsonb_typeof(entry.value) IS DISTINCT FROM 'object'
               OR (entry.value - ARRAY['id','project_id']) <> '{}'::jsonb
               OR NOT (entry.value ?& ARRAY['id','project_id'])
               OR pg_catalog.jsonb_typeof(entry.value -> 'id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(entry.value -> 'project_id') IS DISTINCT FROM 'string'
               OR (entry.value ->> 'id') !~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
               OR (entry.value ->> 'project_id') !~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'legacy upload Labeling receipt mismatch';
    END IF;
    v_created := (v_append ->> 'created')::bigint;
    v_result := pg_catalog.jsonb_build_object(
        'operation_id', p_operation_id, 'actor_id', p_actor_id,
        'dataset_id', p_dataset_id, 'item_id', p_item_id,
        'file_path', v_item.file_path, 'file_type', v_item.file_type,
        'width', v_item.width, 'height', v_item.height,
        'state', 'completed', 'duplicate_of', NULL,
        'result', pg_catalog.jsonb_build_object('item_id', p_item_id, 'linked_tasks', v_created)
    );
    INSERT INTO public.dataset_legacy_upload_operations (
        operation_id, actor_id, dataset_id, item_id, request, result
    ) VALUES (p_operation_id, p_actor_id, p_dataset_id, p_item_id, v_request, v_result);
    RETURN aap_assets_v1._legacy_upload_reply(v_result, NULL);
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._legacy_upload_reply(NULL, 'dataset_busy');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.complete_legacy_dataset_upload(uuid, uuid, uuid, uuid, text, bigint, text, integer, integer) FROM PUBLIC;
