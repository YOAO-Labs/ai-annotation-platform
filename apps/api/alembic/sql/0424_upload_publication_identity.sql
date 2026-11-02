-- Recover the publication already allocated before a presigned upload URL was issued.
-- Ambiguous or mismatched responsibility records remain unchanged and fail closed.
WITH candidates AS (
    SELECT intent.item_id,
        creation.authority ->> 'publication_id' AS publication_id,
        'dataset-sources/' || intent.dataset_id::text || '/'
            || intent.item_id::text || '/'
            || (creation.authority ->> 'publication_id') || '/'
            || intent.file_name AS file_path,
        pg_catalog.count(*) OVER (PARTITION BY intent.item_id) AS candidate_count
    FROM public.dataset_upload_intents AS intent
    JOIN public.managed_original_creations AS creation
      ON creation.dataset_id = intent.dataset_id
     AND creation.owner_kind = 'dataset'
     AND creation.origin = 'direct'
     AND creation.requested_by = intent.actor_id
     AND creation.authority ->> 'kind' = 'dataset_upload'
     AND creation.authority ->> 'item_id' = intent.item_id::text
     AND creation.staging_key = intent.staging_key
     AND creation.authority ->> 'publication_id'
         ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
     AND creation.object_key = COALESCE(
            creation.configuration #>> '{original_ref,key_prefix}', ''
         ) || 'dataset-sources/' || intent.dataset_id::text || '/'
            || intent.item_id::text || '/'
            || (creation.authority ->> 'publication_id') || '/' || intent.file_name
    WHERE intent.state = 'pending'
      AND intent.publication_id IS NULL
      AND intent.file_path IS NULL
)
UPDATE public.dataset_upload_intents AS intent
SET publication_id = candidates.publication_id::uuid,
    file_path = candidates.file_path,
    updated_at = pg_catalog.clock_timestamp()
FROM candidates
WHERE intent.item_id = candidates.item_id
  AND candidates.candidate_count = 1;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.lock_dataset_upload(
    p_actor_id uuid,
    p_dataset_id uuid,
    p_item_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_dataset_name text;
    v_storage_root text;
    v_intent public.dataset_upload_intents%ROWTYPE;
    v_item public.dataset_items%ROWTYPE;
    v_publication_id uuid;
    v_file_path text;
    v_expected_path text;
BEGIN
    IF p_actor_id IS NULL OR p_dataset_id IS NULL OR p_item_id IS NULL THEN
        RETURN aap_assets_v1._dataset_upload_failure('invalid_request');
    END IF;
    v_actor := aap_assets_v1._dataset_upload_actor(p_actor_id);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;

    BEGIN
        SELECT dataset.name INTO v_dataset_name
        FROM public.datasets AS dataset
        WHERE dataset.id = p_dataset_id
        FOR UPDATE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._dataset_upload_failure('dataset_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_upload_failure('dataset_missing');
    END IF;
    BEGIN
        SELECT intent.* INTO v_intent
        FROM public.dataset_upload_intents AS intent
        WHERE intent.item_id = p_item_id
          AND intent.dataset_id = p_dataset_id
        FOR UPDATE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._dataset_upload_failure('dataset_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_upload_failure('upload_missing');
    END IF;
    IF v_intent.actor_id IS DISTINCT FROM p_actor_id THEN
        RETURN aap_assets_v1._dataset_upload_failure('upload_scope_mismatch');
    END IF;
    IF v_intent.publication_id IS NOT NULL THEN
        v_expected_path := 'dataset-sources/' || p_dataset_id::text || '/'
            || p_item_id::text || '/' || v_intent.publication_id::text || '/'
            || v_intent.file_name;
        IF v_intent.file_path IS DISTINCT FROM v_expected_path THEN
            RETURN aap_assets_v1._dataset_upload_failure('source_changed');
        END IF;
    END IF;
    IF v_intent.state <> 'pending' THEN
        IF v_intent.state = 'completed' THEN
            SELECT item.* INTO v_item
            FROM public.dataset_items AS item
            WHERE item.id = p_item_id;
            IF FOUND AND (
                v_item.dataset_id IS DISTINCT FROM p_dataset_id
                OR v_item.file_name IS DISTINCT FROM v_intent.file_name
                OR v_item.file_path IS DISTINCT FROM v_intent.file_path
                OR v_item.file_type IS DISTINCT FROM v_intent.file_type
            ) THEN
                RETURN aap_assets_v1._dataset_upload_failure('source_changed');
            END IF;
        END IF;
        RETURN aap_assets_v1._dataset_upload_success(
            aap_assets_v1._dataset_upload_facts(v_intent)
        );
    END IF;
    v_storage_root := pg_catalog.split_part(
        pg_catalog.btrim(v_dataset_name, '/'), '/', 1
    );
    IF pg_catalog.left(
        v_storage_root, pg_catalog.char_length('__aap_trusted_nuscenes__')
    ) = '__aap_trusted_nuscenes__' THEN
        RETURN aap_assets_v1._dataset_upload_failure('invalid_request');
    END IF;

    IF v_intent.publication_id IS NOT NULL THEN
        SELECT item.* INTO v_item
        FROM public.dataset_items AS item
        WHERE item.id = p_item_id;
        IF FOUND THEN
            IF v_item.dataset_id IS DISTINCT FROM p_dataset_id
               OR v_item.file_name IS DISTINCT FROM v_intent.file_name
               OR v_item.file_path IS DISTINCT FROM v_intent.file_path
               OR v_item.file_type IS DISTINCT FROM v_intent.file_type THEN
                RETURN aap_assets_v1._dataset_upload_failure('source_changed');
            END IF;
            RETURN aap_assets_v1._dataset_upload_failure('operation_conflict');
        END IF;
        RETURN aap_assets_v1._dataset_upload_success(
            aap_assets_v1._dataset_upload_facts(v_intent)
        );
    ELSIF EXISTS (
        SELECT 1 FROM public.dataset_items AS item WHERE item.id = p_item_id
    ) THEN
        RETURN aap_assets_v1._dataset_upload_failure('operation_conflict');
    END IF;

    v_publication_id := pg_catalog.gen_random_uuid();
    v_file_path := 'dataset-sources/' || p_dataset_id::text || '/'
        || p_item_id::text || '/' || v_publication_id::text || '/'
        || v_intent.file_name;
    IF pg_catalog.char_length(v_file_path) > 1000 THEN
        RETURN aap_assets_v1._dataset_upload_failure('invalid_request');
    END IF;
    UPDATE public.dataset_upload_intents AS intent
    SET publication_id = v_publication_id,
        file_path = v_file_path,
        updated_at = pg_catalog.now()
    WHERE intent.item_id = p_item_id
      AND intent.dataset_id = p_dataset_id
      AND intent.actor_id = p_actor_id
      AND intent.state = 'pending'
    RETURNING intent.* INTO v_intent;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_upload_failure('upload_not_pending');
    END IF;
    RETURN aap_assets_v1._dataset_upload_success(
        aap_assets_v1._dataset_upload_facts(v_intent)
    );
END;
$function$;
