-- Publish a verified operator original onto an existing DatasetItem locator.
-- The caller must present the exact locator/content it captured before the
-- replacement; a changed item is fenced and never overwritten. Business rows
-- owned by Labeling are deliberately untouched.
CREATE OR REPLACE FUNCTION aap_assets_v1.publish_operator_item_original(
    p_request jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_dataset_id uuid;
    v_item_id uuid;
    v_creation_id uuid;
    v_expected_path text;
    v_expected_size bigint;
    v_expected_hash text;
    v_creation public.managed_original_creations%ROWTYPE;
    v_item public.dataset_items%ROWTYPE;
    v_ref jsonb;
    v_asset public.dataset_assets%ROWTYPE;
    v_asset_id uuid;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR NOT (p_request ?& ARRAY[
           'dataset_id', 'item_id', 'creation_id',
           'expected_file_path', 'expected_file_size', 'expected_content_hash'
       ])
       OR (p_request - ARRAY[
           'dataset_id', 'item_id', 'creation_id',
           'expected_file_path', 'expected_file_size', 'expected_content_hash'
       ]) <> '{}'::jsonb THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        v_dataset_id := (p_request ->> 'dataset_id')::uuid;
        v_item_id := (p_request ->> 'item_id')::uuid;
        v_creation_id := (p_request ->> 'creation_id')::uuid;
        v_expected_size := CASE
            WHEN pg_catalog.jsonb_typeof(p_request -> 'expected_file_size') = 'null'
                THEN NULL
            ELSE (p_request ->> 'expected_file_size')::bigint
        END;
    EXCEPTION
        WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END;
    v_expected_path := p_request ->> 'expected_file_path';
    v_expected_hash := CASE
        WHEN pg_catalog.jsonb_typeof(p_request -> 'expected_content_hash') = 'null'
            THEN NULL
        ELSE pg_catalog.lower(p_request ->> 'expected_content_hash')
    END;
    IF v_expected_path IS NULL OR pg_catalog.char_length(v_expected_path) NOT BETWEEN 1 AND 1000
       OR (v_expected_hash IS NOT NULL AND v_expected_hash !~ '^([0-9a-f]{32}|[0-9a-f]{64})$') THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END IF;

    SELECT creation.* INTO v_creation
    FROM public.managed_original_creations AS creation
    WHERE creation.id = v_creation_id
      AND creation.owner_kind = 'dataset'
      AND creation.dataset_id = v_dataset_id
      AND creation.status = 'verified'
      AND creation.authority ->> 'item_id' = v_item_id::text
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_missing');
    END IF;

    SELECT item.* INTO v_item
    FROM public.dataset_items AS item
    WHERE item.id = v_item_id
      AND item.dataset_id = v_dataset_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
    END IF;
    IF v_item.file_path IS DISTINCT FROM v_expected_path
       OR v_item.file_size IS DISTINCT FROM v_expected_size
       OR pg_catalog.lower(v_item.content_hash) IS DISTINCT FROM v_expected_hash THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
    END IF;

    SELECT asset.* INTO v_asset
    FROM public.dataset_assets AS asset
    WHERE asset.dataset_item_id = v_item_id
    FOR UPDATE;
    IF FOUND AND v_asset.verification_state = 'verified'
       AND (v_asset.checksum_value IS DISTINCT FROM v_creation.sha256
            OR v_asset.size_bytes IS DISTINCT FROM v_creation.size_bytes) THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_evidence_mismatch');
    END IF;

    v_ref := v_creation.configuration -> 'original_ref';
    IF v_ref IS NULL OR pg_catalog.jsonb_typeof(v_ref) IS DISTINCT FROM 'object' THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_evidence_mismatch');
    END IF;

    UPDATE public.dataset_items AS item
    SET file_path = v_creation.object_key,
        file_size = v_creation.size_bytes,
        content_hash = v_creation.sha256,
        updated_at = pg_catalog.clock_timestamp()
    WHERE item.id = v_item_id;

    IF v_asset.id IS NULL THEN
        INSERT INTO public.dataset_assets (
            id, dataset_item_id, media_type, size_bytes,
            checksum_algorithm, checksum_value, verification_state
        ) VALUES (
            pg_catalog.gen_random_uuid(), v_item_id, v_item.file_type,
            v_creation.size_bytes, 'sha256', v_creation.sha256, 'verified'
        ) RETURNING id INTO v_asset_id;
    ELSE
        UPDATE public.dataset_assets AS asset
        SET media_type = v_item.file_type,
            size_bytes = v_creation.size_bytes,
            checksum_algorithm = 'sha256',
            checksum_value = v_creation.sha256,
            verification_state = 'verified'
        WHERE asset.id = v_asset.id
        RETURNING id INTO v_asset_id;
    END IF;

    DELETE FROM public.dataset_asset_locations AS replica
    WHERE replica.asset_id = v_asset_id
      AND replica.role = 'managed_original'
      AND replica.creation_id IS DISTINCT FROM v_creation.id;
    UPDATE public.dataset_asset_locations AS replica
    SET storage_ref = v_ref,
        etag = v_creation.etag,
        reference_state = 'present',
        verified_at = pg_catalog.clock_timestamp()
    WHERE replica.asset_id = v_asset_id
      AND replica.location_id = v_creation.location_id
      AND replica.bucket = v_creation.bucket
      AND replica.object_key = v_creation.object_key
      AND replica.role = 'managed_original'
      AND replica.creation_id = v_creation.id;
    IF NOT FOUND THEN
        INSERT INTO public.dataset_asset_locations (
            asset_id, location_id, bucket, object_key, role,
            reference_state, creation_id, storage_ref, etag, verified_at
        ) VALUES (
            v_asset_id, v_creation.location_id, v_creation.bucket,
            v_creation.object_key, 'managed_original', 'present',
            v_creation.id, v_ref, v_creation.etag, pg_catalog.clock_timestamp()
        )
        ON CONFLICT (asset_id, location_id, bucket, object_key) DO NOTHING;
    END IF;

    RETURN aap_assets_v1._managed_original_reply(
        pg_catalog.jsonb_build_object(
            'item_id', v_item_id,
            'file_path', v_creation.object_key,
            'file_size', v_creation.size_bytes,
            'content_hash', v_creation.sha256
        ),
        NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.publish_operator_item_original(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1.operator_item_original_facts(
    p_dataset_id uuid,
    p_item_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item public.dataset_items%ROWTYPE;
    v_asset public.dataset_assets%ROWTYPE;
    v_replica public.dataset_asset_locations%ROWTYPE;
    v_creation public.managed_original_creations%ROWTYPE;
    v_ref jsonb;
BEGIN
    SELECT item.* INTO v_item
    FROM public.dataset_items AS item
    WHERE item.id = p_item_id AND item.dataset_id = p_dataset_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(
            pg_catalog.jsonb_build_object('found', false), NULL
        );
    END IF;
    SELECT asset.* INTO v_asset
    FROM public.dataset_assets AS asset
    WHERE asset.dataset_item_id = p_item_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(
            pg_catalog.jsonb_build_object(
                'found', true,
                'item', pg_catalog.jsonb_build_object(
                    'file_path', v_item.file_path,
                    'file_size', v_item.file_size,
                    'content_hash', v_item.content_hash,
                    'file_type', v_item.file_type
                ),
                'source', NULL
            ),
            NULL
        );
    END IF;
    SELECT replica.* INTO v_replica
    FROM public.dataset_asset_locations AS replica
    WHERE replica.asset_id = v_asset.id
      AND replica.role = 'managed_original'
      AND replica.creation_id IS NOT NULL
    ORDER BY replica.verified_at DESC NULLS LAST
    LIMIT 1;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(
            pg_catalog.jsonb_build_object(
                'found', true,
                'item', pg_catalog.jsonb_build_object(
                    'file_path', v_item.file_path,
                    'file_size', v_item.file_size,
                    'content_hash', v_item.content_hash,
                    'file_type', v_item.file_type
                ),
                'source', NULL
            ),
            NULL
        );
    END IF;
    SELECT creation.* INTO v_creation
    FROM public.managed_original_creations AS creation
    WHERE creation.id = v_replica.creation_id
      AND creation.owner_kind = 'dataset'
      AND creation.dataset_id = p_dataset_id
      AND creation.status = 'verified';
    IF NOT FOUND
       OR v_asset.verification_state <> 'verified'
       OR v_asset.checksum_value IS DISTINCT FROM v_creation.sha256
       OR v_asset.size_bytes IS DISTINCT FROM v_creation.size_bytes
       OR v_replica.object_key IS DISTINCT FROM v_item.file_path THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_evidence_mismatch');
    END IF;
    v_ref := v_creation.configuration -> 'original_ref';
    RETURN aap_assets_v1._managed_original_reply(
        pg_catalog.jsonb_build_object(
            'found', true,
            'item', pg_catalog.jsonb_build_object(
                'file_path', v_item.file_path,
                'file_size', v_item.file_size,
                'content_hash', v_item.content_hash,
                'file_type', v_item.file_type
            ),
            'source', pg_catalog.jsonb_build_object(
                'creation_id', v_creation.id,
                'sha256', v_creation.sha256,
                'size_bytes', v_creation.size_bytes,
                'object_key', v_creation.object_key,
                'bucket', v_creation.bucket,
                'location_id', v_creation.location_id,
                'etag', v_creation.etag,
                'version_id', v_creation.version_id,
                'storage_ref', v_ref,
                'endpoint_url', v_ref ->> 'endpoint_url',
                'key_prefix', v_ref ->> 'key_prefix',
                'storage_identity', v_ref ->> 'storage_identity'
            )
        ),
        NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.operator_item_original_facts(uuid,uuid) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1.operator_auxiliary_pointer(
    p_dataset_id uuid,
    p_item_id uuid,
    p_scene_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item uuid;
    v_scene uuid;
BEGIN
    IF p_item_id IS NOT NULL THEN
        SELECT item.nuscenes_raw_original_id INTO v_item
        FROM public.dataset_items AS item
        WHERE item.id = p_item_id AND item.dataset_id = p_dataset_id;
        IF NOT FOUND THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
        END IF;
    END IF;
    IF p_scene_id IS NOT NULL THEN
        SELECT scene.nuscenes_map_original_id INTO v_scene
        FROM public.scenes AS scene
        WHERE scene.id = p_scene_id AND scene.dataset_id = p_dataset_id;
        IF NOT FOUND THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
        END IF;
    END IF;
    RETURN aap_assets_v1._managed_original_reply(
        pg_catalog.jsonb_build_object('item', v_item, 'scene', v_scene), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.operator_auxiliary_pointer(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
