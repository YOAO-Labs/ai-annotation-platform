-- The shared source reader uses the existing durable storage-operation ledger,
-- current maintenance gate and exact bucket/key reclaim fence. Caller-specific
-- wrappers validate actor or claim authority before invoking this helper.
CREATE FUNCTION aap_assets_v1._begin_media_original_read(
    p_item_id uuid, p_task_id uuid, p_object_key text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_creation public.managed_original_creations%ROWTYPE;
    v_operation_id uuid;
    v_revision integer;
    v_run record;
    v_original_id uuid;
    v_blocked jsonb;
    v_match text[];
    v_namespace_identity text;
    v_source_identity text;
    v_qualified boolean := false;
BEGIN
    IF (p_item_id IS NULL) = (p_task_id IS NULL) OR p_object_key IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid media original scope';
    END IF;
    IF p_item_id IS NOT NULL THEN
        SELECT CASE WHEN pg_catalog.count(*) = 1
                    THEN pg_catalog.min(location.storage_ref ->> 'storage_identity') END
        INTO v_source_identity
        FROM public.dataset_assets AS asset
        JOIN public.dataset_asset_locations AS location ON location.asset_id = asset.id
        WHERE asset.dataset_item_id = p_item_id
          AND location.role = 'managed_original' AND location.reference_state = 'present'
          AND location.object_key = p_object_key;
    ELSE
        v_original_id := aap_labeling_v1.lock_task_original_pointer(p_task_id, p_object_key);
        SELECT original.storage_identity INTO v_source_identity
        FROM public.task_originals AS original
        WHERE original.id = v_original_id AND original.task_id = p_task_id
          AND original.object_key = p_object_key;
    END IF;
    -- Fence the reserved namespace before an individual Creation exists.
    -- Media raw references can sign without probing the object first.
    v_match := pg_catalog.regexp_match(
        p_object_key,
        '(^|/)__aap_managed_datasets__/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/_objects/'
    );
    IF v_match IS NOT NULL THEN
        SELECT resource.storage_identity INTO v_namespace_identity
        FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.kind = 'dataset' AND resource.resource_id = v_match[2]::uuid;
        IF FOUND AND v_namespace_identity IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'media_original_unverified';
        END IF;
    END IF;
    -- The trusted storage adapter compares owner exclusions with its actual
    -- endpoint and bucket, so a different physical store stays ordinary.
    SELECT COALESCE(pg_catalog.jsonb_agg(DISTINCT collisions.storage_identity), '[]'::jsonb)
    INTO v_blocked
    FROM (
        SELECT creation.storage_identity
        FROM yoao_fixture_v1.fixture_run_resources AS resource
        JOIN public.managed_original_creations AS creation ON creation.id = resource.creation_id
        WHERE resource.kind = 'creation' AND creation.object_key = p_object_key
        UNION ALL
        SELECT v_namespace_identity WHERE v_namespace_identity IS NOT NULL
        UNION ALL
        SELECT root.original_storage_identity
        FROM yoao_fixture_v1.fixture_runs AS root
        WHERE root.parent_run_id IS NULL
          AND root.original_storage_identity IS NOT NULL
          AND root.original_store_receipt_sha256 IS NOT NULL
    ) AS collisions;
    IF v_blocked = '[]'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'ownership', 'ordinary', 'object_key', p_object_key,
            'blocked_storage_identities', v_blocked, 'source_storage_identity', v_source_identity
        );
    END IF;
    BEGIN
        IF p_item_id IS NOT NULL THEN
            SELECT creation.* INTO STRICT v_creation
            FROM public.dataset_items AS item
            JOIN public.dataset_assets AS asset ON asset.dataset_item_id = item.id
            JOIN public.dataset_asset_locations AS location ON location.asset_id = asset.id
            JOIN public.managed_original_creations AS creation ON creation.id = location.creation_id
            JOIN yoao_fixture_v1.fixture_run_resources AS resource
              ON resource.kind = 'creation' AND resource.creation_id = creation.id
            WHERE item.id = p_item_id AND item.file_path = p_object_key
              AND location.role = 'managed_original' AND location.reference_state = 'present'
              AND location.object_key = p_object_key AND creation.object_key = location.object_key
              AND creation.bucket = location.bucket AND creation.location_id = location.location_id
              AND creation.dataset_id = item.dataset_id
              AND asset.verification_state = 'verified' AND asset.checksum_value = creation.sha256
              AND asset.size_bytes = creation.size_bytes;
        ELSE
            SELECT creation.* INTO STRICT v_creation
            FROM public.task_originals AS original
            JOIN public.managed_original_creations AS creation ON creation.id = original.creation_id
            JOIN yoao_fixture_v1.fixture_run_resources AS resource
              ON resource.kind = 'creation' AND resource.creation_id = creation.id
            WHERE original.id = v_original_id AND original.task_id = p_task_id
              AND original.object_key = p_object_key
              AND creation.object_key = original.object_key AND creation.bucket = original.bucket
              AND creation.location_id = original.location_id AND creation.sha256 = original.sha256
              AND creation.size_bytes = original.size_bytes AND creation.version_id = original.version_id;
        END IF;
    EXCEPTION WHEN no_data_found THEN
        RETURN pg_catalog.jsonb_build_object(
            'ownership', 'ordinary', 'object_key', p_object_key,
            'blocked_storage_identities', v_blocked, 'source_storage_identity', v_source_identity
        );
    END;
    IF v_creation.status <> 'verified' OR v_creation.settled_at IS NULL
       OR v_creation.version_id IS NULL OR v_creation.version_id = 'null' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'media_original_unverified';
    END IF;
    IF v_creation.configuration #>> '{original_ref,endpoint_url}' IS NULL
       OR v_creation.configuration #>> '{original_ref,location_id}' IS DISTINCT FROM v_creation.location_id::text
       OR v_creation.configuration #>> '{original_ref,bucket}' IS DISTINCT FROM v_creation.bucket
       OR v_creation.configuration #>> '{original_ref,object_key}' IS DISTINCT FROM v_creation.object_key
       OR v_creation.configuration #>> '{original_ref,storage_identity}' IS DISTINCT FROM v_creation.storage_identity
       OR v_creation.storage_identity IS DISTINCT FROM aap_assets_v1._managed_original_identity(
           v_creation.configuration #>> '{original_ref,endpoint_url}', v_creation.bucket
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'media_original_unverified';
    END IF;
    FOR v_run IN
        SELECT run.state, root.state AS root_state
        FROM yoao_fixture_v1.fixture_runs AS run
        JOIN yoao_fixture_v1.fixture_run_resources AS resource ON resource.run_id = run.id
        JOIN yoao_fixture_v1.fixture_runs AS root ON root.id = COALESCE(run.parent_run_id, run.id)
        JOIN yoao_fixture_v1.fixture_original_targets AS target ON target.id = root.original_target_id
        WHERE resource.kind = 'creation' AND resource.creation_id = v_creation.id
          AND target.location_id = v_creation.location_id AND target.bucket = v_creation.bucket
          AND target.storage_identity = v_creation.storage_identity
          AND target.storage_identity = root.original_storage_identity
          AND target.store_receipt_sha256 = root.original_store_receipt_sha256
          AND v_creation.configuration #>> '{fixture_original_target,id}' = target.id::text
          AND v_creation.configuration #>> '{fixture_original_target,generation}' = target.generation::text
          AND v_creation.configuration #>> '{fixture_original_target,store_receipt_sha256}' = target.store_receipt_sha256
          AND v_creation.configuration #>> '{original_ref,binding_generation}' = target.generation::text
        FOR SHARE OF run, root, target
    LOOP
        IF v_run.state <> 'open' OR v_run.root_state <> 'open' THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_run_closed';
        END IF;
        v_qualified := true;
    END LOOP;
    IF NOT v_qualified THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'media_original_unverified';
    END IF;
    -- Like storage_access.acquire, admit at the active current Gate revision.
    -- A creation's original maintenance revision is historical and must not
    -- invalidate reads after a legitimate maintenance cycle.
    v_revision := aap_assets_v1._managed_original_gate_revision(v_creation.location_id);
    v_operation_id := pg_catalog.gen_random_uuid();
    INSERT INTO public.storage_operations (id, location_id, revision, purpose, node_id)
    VALUES (
        v_operation_id, v_creation.location_id, v_revision,
        'media_original_' || pg_catalog.replace(v_creation.id::text, '-', ''),
        'aap-assets-media'
    );
    IF NOT pg_catalog.pg_try_advisory_xact_lock_shared(
        pg_catalog.hashtextextended(
            aap_assets_v1._managed_reclaim_lock_identity(v_creation.bucket, v_creation.object_key), 0
        )
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55P03', MESSAGE = 'media_original_reclaim_busy';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.managed_original_reclaim_claims AS claim
        WHERE claim.bucket = v_creation.bucket AND claim.object_key = v_creation.object_key
          AND claim.state <> 'released'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'media_original_reclaim_claimed';
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'ownership', 'fixture',
        'creation_id', v_creation.id,
        'operation_id', v_operation_id,
        'location_id', v_creation.location_id,
        'endpoint_url', v_creation.configuration #>> '{original_ref,endpoint_url}',
        'storage_identity', v_creation.storage_identity,
        'bucket', v_creation.bucket,
        'object_key', v_creation.object_key,
        'version_id', v_creation.version_id,
        'etag', v_creation.etag,
        'size_bytes', v_creation.size_bytes,
        'sha256', v_creation.sha256
    );
EXCEPTION
    WHEN no_data_found OR too_many_rows THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'media_original_unverified';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._begin_media_original_read(uuid,uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._finish_media_original_read(p_creation_id uuid, p_operation_id uuid)
RETURNS boolean
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    DELETE FROM public.storage_operations AS operation
    USING public.managed_original_creations AS creation
    WHERE creation.id = p_creation_id AND operation.id = p_operation_id
      AND operation.location_id = creation.location_id
      AND operation.purpose = 'media_original_' || pg_catalog.replace(creation.id::text, '-', '')
      AND operation.node_id = 'aap-assets-media';
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._finish_media_original_read(uuid,uuid) FROM PUBLIC;
