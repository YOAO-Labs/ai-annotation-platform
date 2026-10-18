ALTER TABLE public.asset_storage_deletions
ADD COLUMN not_before timestamptz NOT NULL DEFAULT pg_catalog.now();
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.complete_storage_deletion(p_operation_id uuid, p_asset_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_updated bigint;
BEGIN
    IF p_operation_id IS NULL OR p_asset_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid storage deletion completion request', DETAIL = '{"code":"invalid_request"}';
    END IF;
    UPDATE public.asset_storage_deletions AS deletion
    SET completed_at = COALESCE(deletion.completed_at, pg_catalog.now())
    WHERE deletion.operation_id = p_operation_id AND deletion.asset_id = p_asset_id
      AND deletion.not_before <= pg_catalog.statement_timestamp();
    GET DIAGNOSTICS v_updated = ROW_COUNT;
    RETURN v_updated > 0;
END;
$function$;
-- aap-statement-break
CREATE TABLE public.dataset_source_deletion_operations (
    operation_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    item_id uuid NULL,
    request jsonb NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now()
);
-- aap-statement-break
REVOKE ALL ON TABLE public.dataset_source_deletion_operations FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._source_delete_failure(p_code text)
RETURNS jsonb LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-dataset-source-delete/v1', 'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._source_delete_failure(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.dataset_association_references(p_dataset_id uuid)
RETURNS TABLE(association_id uuid, project_id uuid)
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_dataset_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'dataset id is required';
    END IF;
    RETURN QUERY SELECT association.id, association.project_id
    FROM public.project_datasets AS association
    WHERE association.dataset_id = p_dataset_id ORDER BY association.id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.dataset_association_references(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.lock_scene_source_references(p_scene_ids uuid[])
RETURNS boolean LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_count bigint; v_expected bigint;
BEGIN
    IF p_scene_ids IS NULL OR pg_catalog.array_position(p_scene_ids, NULL) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid scene source references';
    END IF;
    SELECT pg_catalog.count(DISTINCT id) INTO v_expected FROM pg_catalog.unnest(p_scene_ids) AS ids(id);
    PERFORM scene.id FROM public.scenes AS scene WHERE scene.id = ANY(p_scene_ids)
    ORDER BY scene.id FOR KEY SHARE NOWAIT;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    IF v_count <> v_expected THEN
        RAISE serialization_failure USING MESSAGE = 'scene source changed';
    END IF;
    RETURN true;
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.lock_scene_source_references(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._lock_dataset_source_deletion(
    p_dataset_id uuid, p_item_id uuid
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_id uuid; v_pending boolean := false;
BEGIN
    IF p_dataset_id IS NULL THEN
        RETURN aap_assets_v1._source_delete_failure('invalid_request');
    END IF;
    IF p_item_id IS NOT NULL THEN
        -- Preserve cooperative append serialization before the stronger FK fence.
        v_id := aap_assets_v1.lock_dataset_reference(p_dataset_id);
        IF v_id IS NULL THEN
            RETURN aap_assets_v1._source_delete_failure('item_missing');
        END IF;
    END IF;
    SELECT dataset.id INTO v_id FROM public.datasets AS dataset
    WHERE dataset.id = p_dataset_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._source_delete_failure('dataset_missing');
    END IF;
    IF p_item_id IS NULL THEN
        PERFORM scene.id FROM public.scenes AS scene
        WHERE scene.dataset_id = p_dataset_id ORDER BY scene.id FOR UPDATE NOWAIT;
        IF EXISTS (
            SELECT 1 FROM public.dataset_items AS item
            JOIN public.scenes AS scene ON scene.id = item.scene_id
            WHERE scene.dataset_id = p_dataset_id
              AND item.dataset_id IS DISTINCT FROM p_dataset_id
        ) THEN
            RETURN aap_assets_v1._source_delete_failure('source_in_use');
        END IF;
        PERFORM item.id FROM public.dataset_items AS item
        WHERE item.dataset_id = p_dataset_id ORDER BY item.id FOR UPDATE NOWAIT;
        PERFORM association.id FROM public.project_datasets AS association
        WHERE association.dataset_id = p_dataset_id
        ORDER BY association.id FOR UPDATE NOWAIT;
        PERFORM intent.item_id FROM public.dataset_upload_intents AS intent
        WHERE intent.dataset_id = p_dataset_id
        ORDER BY intent.item_id FOR UPDATE NOWAIT;
    ELSE
        SELECT item.id INTO v_id FROM public.dataset_items AS item
        WHERE item.id = p_item_id AND item.dataset_id = p_dataset_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            SELECT intent.item_id INTO v_id FROM public.dataset_upload_intents AS intent
            WHERE intent.item_id = p_item_id AND intent.dataset_id = p_dataset_id
              AND intent.state = 'pending' FOR UPDATE NOWAIT;
            IF NOT FOUND THEN
                RETURN aap_assets_v1._source_delete_failure('item_missing');
            END IF;
            v_pending := true;
        END IF;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-dataset-source-delete-scope/v1', 'ok', true,
        'data', pg_catalog.jsonb_build_object('pending_upload', v_pending)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._lock_dataset_source_deletion(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._queue_pyramid_deletions(
    p_operation_id uuid, p_asset_ids uuid[], p_bucket text
)
RETURNS bigint LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_count bigint;
BEGIN
    IF p_operation_id IS NULL OR p_asset_ids IS NULL OR p_bucket IS NULL
       OR pg_catalog.array_position(p_asset_ids, NULL) IS NOT NULL
       OR pg_catalog.char_length(p_bucket) NOT BETWEEN 3 AND 63
       OR p_bucket !~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$'
       OR p_bucket ~ '^[0-9]+(\.[0-9]+){3}$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid pyramid cleanup scope';
    END IF;
    PERFORM asset.id FROM public.image_pyramid_assets AS asset
    WHERE asset.id = ANY(p_asset_ids) ORDER BY asset.id FOR UPDATE NOWAIT;
    PERFORM generation.asset_id FROM public.image_pyramid_generations AS generation
    WHERE generation.asset_id = ANY(p_asset_ids)
    ORDER BY generation.asset_id, generation.generation FOR UPDATE NOWAIT;
    IF EXISTS (
        SELECT 1 FROM public.image_pyramid_generations AS generation
        WHERE generation.asset_id = ANY(p_asset_ids)
          AND generation.status = 'building' AND generation.lease_expires_at IS NULL
    ) THEN
        RAISE lock_not_available USING MESSAGE = 'pyramid writer lease is unproven';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.asset_storage_deletions AS deletion
        JOIN public.image_pyramid_assets AS asset ON asset.id = deletion.asset_id
        WHERE deletion.operation_id = p_operation_id AND asset.id = ANY(p_asset_ids)
          AND (deletion.bucket IS DISTINCT FROM p_bucket
            OR deletion.prefix IS DISTINCT FROM ('image-pyramids/' || asset.id::text || '/')
            OR deletion.completed_at IS NOT NULL)
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'pyramid cleanup request conflicts with saved work';
    END IF;
    -- Wait out every captured writer lease before acknowledging physical cleanup.
    INSERT INTO public.asset_storage_deletions(operation_id, asset_id, bucket, prefix, not_before)
    SELECT p_operation_id, asset.id, p_bucket, 'image-pyramids/' || asset.id::text || '/',
        GREATEST(pg_catalog.statement_timestamp(), COALESCE(
            (SELECT pg_catalog.max(generation.lease_expires_at)
             FROM public.image_pyramid_generations AS generation
             WHERE generation.asset_id = asset.id AND generation.status = 'building'),
            pg_catalog.statement_timestamp()
        ))
    FROM public.image_pyramid_assets AS asset WHERE asset.id = ANY(p_asset_ids)
    ON CONFLICT (operation_id, asset_id) DO NOTHING;
    DELETE FROM public.image_pyramid_assets AS asset WHERE asset.id = ANY(p_asset_ids);
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._queue_pyramid_deletions(uuid, uuid[], text) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.prepare_dataset_unlink_cleanup(
    p_operation_id uuid, p_dataset_id uuid, p_project_id uuid, p_bucket text
)
RETURNS bigint LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_ids uuid[];
BEGIN
    IF p_dataset_id IS NULL OR p_project_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'dataset and project ids are required';
    END IF;
    v_ids := ARRAY(
        SELECT asset.id FROM public.image_pyramid_assets AS asset
        WHERE asset.task_id IN (
            SELECT reference.task_id
            FROM aap_labeling_v1.dataset_task_references(p_dataset_id, p_project_id) AS reference
        ) ORDER BY asset.id
    );
    RETURN aap_assets_v1._queue_pyramid_deletions(p_operation_id, v_ids, p_bucket);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._delete_dataset_source_records(
    p_operation_id uuid, p_dataset_id uuid, p_item_id uuid, p_bucket text
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_assets uuid[]; v_items bigint := 0; v_scenes bigint := 0;
    v_associations bigint := 0; v_uploads bigint := 0; v_queued bigint := 0;
BEGIN
    v_assets := ARRAY(
        SELECT asset.id FROM public.image_pyramid_assets AS asset
        JOIN public.dataset_items AS item ON item.id = asset.dataset_item_id
        WHERE item.dataset_id = p_dataset_id
          AND (p_item_id IS NULL OR item.id = p_item_id) ORDER BY asset.id
    );
    v_queued := aap_assets_v1._queue_pyramid_deletions(p_operation_id, v_assets, p_bucket);
    UPDATE public.dataset_upload_intents AS intent
    SET state = 'discarded', result = '{"reason":"source_deleted"}'::jsonb,
        updated_at = pg_catalog.now()
    WHERE intent.dataset_id = p_dataset_id AND intent.state = 'pending'
      AND (p_item_id IS NULL OR intent.item_id = p_item_id);
    GET DIAGNOSTICS v_uploads = ROW_COUNT;
    DELETE FROM public.dataset_items AS item WHERE item.dataset_id = p_dataset_id
      AND (p_item_id IS NULL OR item.id = p_item_id);
    GET DIAGNOSTICS v_items = ROW_COUNT;
    IF p_item_id IS NULL THEN
        DELETE FROM public.scenes AS scene WHERE scene.dataset_id = p_dataset_id;
        GET DIAGNOSTICS v_scenes = ROW_COUNT;
        DELETE FROM public.project_datasets AS association WHERE association.dataset_id = p_dataset_id;
        GET DIAGNOSTICS v_associations = ROW_COUNT;
        DELETE FROM public.datasets AS dataset WHERE dataset.id = p_dataset_id;
    ELSE
        UPDATE public.datasets AS dataset
        SET file_count = (SELECT pg_catalog.count(*)::integer FROM public.dataset_items AS item WHERE item.dataset_id = p_dataset_id),
            updated_at = pg_catalog.now()
        WHERE dataset.id = p_dataset_id;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'deleted_items', v_items, 'deleted_scenes', v_scenes,
        'deleted_associations', v_associations, 'discarded_uploads', v_uploads,
        'queued_storage_deletions', v_queued
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._delete_dataset_source_records(uuid, uuid, uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._delete_dataset_source(
    p_operation_id uuid, p_actor_id uuid, p_dataset_id uuid, p_item_id uuid,
    p_bucket text, p_audit_context jsonb
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb; v_role text; v_request jsonb;
    v_saved public.dataset_source_deletion_operations%ROWTYPE;
    v_scope jsonb; v_guard jsonb; v_pending boolean; v_counts jsonb;
    v_association record; v_jobs bigint := 0; v_audit bigint; v_result jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_dataset_id IS NULL
       OR p_bucket IS NULL OR pg_catalog.char_length(p_bucket) NOT BETWEEN 3 AND 63
       OR p_bucket !~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$'
       OR p_bucket ~ '^[0-9]+(\.[0-9]+){3}$'
       OR aap_audit_v1.valid_request_context(p_audit_context) IS DISTINCT FROM true THEN
        RETURN aap_assets_v1._source_delete_failure('invalid_request');
    END IF;
    v_actor := aap_identity_v1.lock_active_actor(p_actor_id);
    IF v_actor ->> 'schema' IS DISTINCT FROM 'aap-active-actor/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'source deletion identity contract mismatch';
    END IF;
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object('schema', 'aap-dataset-source-delete/v1', 'ok', false, 'error', v_actor -> 'error');
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('aap-dataset-source-delete:' || p_operation_id::text, 0));
    v_request := pg_catalog.jsonb_build_object(
        'operation_id', p_operation_id, 'actor_id', p_actor_id,
        'dataset_id', p_dataset_id, 'item_id', p_item_id, 'cleanup_bucket', p_bucket
    );
    SELECT receipt.* INTO v_saved FROM public.dataset_source_deletion_operations AS receipt
    WHERE receipt.operation_id = p_operation_id FOR UPDATE;
    IF FOUND THEN
        IF v_saved.request IS DISTINCT FROM v_request THEN
            RETURN aap_assets_v1._source_delete_failure('operation_conflict');
        END IF;
        RETURN v_saved.result;
    END IF;
    v_role := v_actor #>> '{data,platform_role}';
    IF v_role IS NULL OR v_role NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
        RETURN aap_assets_v1._source_delete_failure('invalid_account_role');
    END IF;
    IF v_role NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_assets_v1._source_delete_failure('catalog_manager_required');
    END IF;
    v_scope := aap_assets_v1._lock_dataset_source_deletion(p_dataset_id, p_item_id);
    IF v_scope -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_scope; END IF;
    IF v_scope ->> 'schema' IS DISTINCT FROM 'aap-dataset-source-delete-scope/v1'
       OR pg_catalog.jsonb_typeof(v_scope #> '{data,pending_upload}') IS DISTINCT FROM 'boolean' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'source deletion lock contract mismatch';
    END IF;
    v_pending := (v_scope #>> '{data,pending_upload}')::boolean;
    IF NOT v_pending THEN
        v_guard := aap_learning_v1.guard_dataset_source_deletion(p_dataset_id, p_item_id);
        IF v_guard ->> 'schema' IS DISTINCT FROM 'aap-source-retention/v1' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'source Learning contract mismatch';
        END IF;
        IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('schema', 'aap-dataset-source-delete/v1', 'ok', false, 'error', v_guard -> 'error');
        END IF;
        v_guard := aap_labeling_v1.guard_dataset_source_deletion(p_dataset_id, p_item_id);
        IF v_guard ->> 'schema' IS DISTINCT FROM 'aap-source-retention/v1' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'source Labeling contract mismatch';
        END IF;
        IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('schema', 'aap-dataset-source-delete/v1', 'ok', false, 'error', v_guard -> 'error');
        END IF;
    END IF;
    v_guard := aap_jobs_v1.guard_dataset_source_deletion(p_dataset_id);
    IF v_guard ->> 'schema' IS DISTINCT FROM 'aap-source-retention/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'source Jobs contract mismatch';
    END IF;
    IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object('schema', 'aap-dataset-source-delete/v1', 'ok', false, 'error', v_guard -> 'error');
    END IF;
    IF p_item_id IS NULL THEN
        FOR v_association IN SELECT * FROM aap_assets_v1.dataset_association_references(p_dataset_id) LOOP
            v_jobs := v_jobs + aap_jobs_v1.cancel_dataset_task_creation(p_dataset_id, v_association.project_id, v_association.association_id);
        END LOOP;
        PERFORM aap_labeling_v1.detach_deleted_dataset_attribution(p_dataset_id);
    END IF;
    v_counts := aap_assets_v1._delete_dataset_source_records(p_operation_id, p_dataset_id, p_item_id, p_bucket);
    v_audit := aap_audit_v1.record_dataset_source_deletion(
        p_operation_id, p_actor_id, p_dataset_id, p_item_id,
        (v_counts ->> 'deleted_items')::bigint, (v_counts ->> 'deleted_scenes')::bigint,
        (v_counts ->> 'deleted_associations')::bigint, (v_counts ->> 'discarded_uploads')::bigint,
        v_jobs, (v_counts ->> 'queued_storage_deletions')::bigint, p_audit_context
    );
    IF v_audit IS NULL OR v_audit <= 0 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid source deletion audit receipt';
    END IF;
    v_result := pg_catalog.jsonb_build_object(
        'schema', 'aap-dataset-source-delete/v1', 'ok', true,
        'data', v_counts || pg_catalog.jsonb_build_object(
            'operation_id', p_operation_id, 'actor_id', p_actor_id, 'dataset_id', p_dataset_id,
            'item_id', p_item_id, 'cleanup_bucket', p_bucket, 'audit_id', v_audit,
            'deleted_datasets', CASE WHEN p_item_id IS NULL THEN 1 ELSE 0 END,
            'cancelled_creation_jobs', v_jobs
        )
    );
    INSERT INTO public.dataset_source_deletion_operations(operation_id, actor_id, dataset_id, item_id, request, result)
    VALUES (p_operation_id, p_actor_id, p_dataset_id, p_item_id, v_request, v_result);
    RETURN v_result;
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._source_delete_failure('source_busy');
    WHEN foreign_key_violation THEN RETURN aap_assets_v1._source_delete_failure('source_in_use');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._delete_dataset_source(uuid, uuid, uuid, uuid, text, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.delete_dataset(
    p_operation_id uuid, p_actor_id uuid, p_dataset_id uuid, p_bucket text, p_audit_context jsonb DEFAULT NULL
)
RETURNS jsonb LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT aap_assets_v1._delete_dataset_source(p_operation_id, p_actor_id, p_dataset_id, NULL::uuid, p_bucket, p_audit_context)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.delete_dataset(uuid, uuid, uuid, text, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.delete_dataset_item(
    p_operation_id uuid, p_actor_id uuid, p_dataset_id uuid, p_item_id uuid, p_bucket text, p_audit_context jsonb DEFAULT NULL
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_item_id IS NULL THEN RETURN aap_assets_v1._source_delete_failure('invalid_request'); END IF;
    RETURN aap_assets_v1._delete_dataset_source(p_operation_id, p_actor_id, p_dataset_id, p_item_id, p_bucket, p_audit_context);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.delete_dataset_item(uuid, uuid, uuid, uuid, text, jsonb) FROM PUBLIC;
