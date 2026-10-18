-- Image cleanup producer updates. Keep previous migrations immutable.
-- These routines run in the caller's transaction and never perform object IO.

CREATE OR REPLACE FUNCTION aap_assets_v1._queue_pyramid_deletions(
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
    -- Keep the existing whole-prefix wire. Retained rows are swept again after
    -- not_before so an expired writer cannot leave a late PUT behind forever.
    INSERT INTO public.asset_storage_deletions(
        operation_id, asset_id, bucket, prefix, not_before, retained
    )
    SELECT p_operation_id, asset.id, p_bucket,
        'image-pyramids/' || asset.id::text || '/',
        GREATEST(pg_catalog.statement_timestamp(), COALESCE(
            (SELECT pg_catalog.max(generation.lease_expires_at)
             FROM public.image_pyramid_generations AS generation
             WHERE generation.asset_id = asset.id AND generation.status = 'building'),
            pg_catalog.statement_timestamp()
        )),
        true
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
DECLARE
    v_ids uuid[];
    v_task_ids uuid[];
    v_task_id uuid;
    v_deadline timestamptz := pg_catalog.statement_timestamp();
    v_owner_deadline timestamptz;
    v_queued bigint;
BEGIN
    IF p_dataset_id IS NULL OR p_project_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'dataset and project ids are required';
    END IF;
    v_task_ids := ARRAY(
        SELECT reference.task_id
        FROM aap_labeling_v1.dataset_task_references(p_dataset_id, p_project_id) AS reference
        ORDER BY reference.task_id
    );
    -- Fence direct Task image writers before locking pyramid Asset/Generation
    -- rows. DatasetItem-owned work is shared and remains live while its source exists.
    FOREACH v_task_id IN ARRAY v_task_ids LOOP
        v_owner_deadline := aap_assets_v1._cancel_image_source_work(
            'task', v_task_id, 'task_unlinked'
        );
        v_deadline := GREATEST(v_deadline, COALESCE(v_owner_deadline, v_deadline));
    END LOOP;
    v_ids := ARRAY(
        SELECT asset.id FROM public.image_pyramid_assets AS asset
        WHERE asset.task_id = ANY(v_task_ids) ORDER BY asset.id
    );
    v_queued := aap_assets_v1._queue_pyramid_deletions(
        p_operation_id, v_ids, p_bucket
    );
    UPDATE public.asset_storage_deletions AS deletion
    SET not_before = GREATEST(deletion.not_before, v_deadline), retained = true
    WHERE deletion.operation_id = p_operation_id
      AND deletion.asset_id = ANY(v_ids);
    RETURN v_queued;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.prepare_dataset_unlink_cleanup(uuid, uuid, uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1._delete_dataset_source_records(
    p_operation_id uuid, p_dataset_id uuid, p_item_id uuid, p_bucket text
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_assets uuid[];
    v_items bigint := 0;
    v_scenes bigint := 0;
    v_associations bigint := 0;
    v_uploads bigint := 0;
    v_queued bigint := 0;
    v_item_id uuid;
    v_item_ids uuid[];
    v_owner_deadline timestamptz;
    v_deadline timestamptz := pg_catalog.statement_timestamp();
BEGIN
    v_item_ids := ARRAY(
        SELECT item.id FROM public.dataset_items AS item
        WHERE item.dataset_id = p_dataset_id
          AND (p_item_id IS NULL OR item.id = p_item_id)
        ORDER BY item.id
    );
    -- Dataset source is locked by the enclosing deletion workflow; cancel its
    -- image work before acquiring the older pyramid Asset/Generation lock order.
    FOREACH v_item_id IN ARRAY v_item_ids LOOP
        v_owner_deadline := aap_assets_v1._cancel_image_source_work(
            'dataset_item', v_item_id, 'source_deleted'
        );
        v_deadline := GREATEST(v_deadline, COALESCE(v_owner_deadline, v_deadline));
    END LOOP;
    v_assets := ARRAY(
        SELECT asset.id FROM public.image_pyramid_assets AS asset
        JOIN public.dataset_items AS item ON item.id = asset.dataset_item_id
        WHERE item.dataset_id = p_dataset_id
          AND (p_item_id IS NULL OR item.id = p_item_id) ORDER BY asset.id
    );
    v_queued := aap_assets_v1._queue_pyramid_deletions(
        p_operation_id, v_assets, p_bucket
    );
    UPDATE public.asset_storage_deletions AS deletion
    SET not_before = GREATEST(deletion.not_before, v_deadline), retained = true
    WHERE deletion.operation_id = p_operation_id
      AND deletion.asset_id = ANY(v_assets);
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
CREATE OR REPLACE FUNCTION aap_assets_v1.delete_project_owned_records(p_project_id uuid)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
STRICT
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_deleted bigint := 0;
    v_rows bigint;
    v_task_ids uuid[];
    v_task_id uuid;
    v_asset_id uuid;
    v_owner_deadline timestamptz;
    v_deadline timestamptz := pg_catalog.statement_timestamp();
    v_generation_deadline timestamptz;
BEGIN
    v_task_ids := ARRAY(
        SELECT reference.task_id
        FROM aap_labeling_v1.project_task_references(p_project_id) AS reference
        ORDER BY reference.task_id
    );
    -- Project deletion has no cleanup-bucket argument. The standalone image
    -- garbage protocol uses its fixed bucket and exact whole-asset prefixes.
    FOREACH v_task_id IN ARRAY v_task_ids LOOP
        v_owner_deadline := aap_assets_v1._cancel_image_source_work(
            'task', v_task_id, 'project_deleted'
        );
        v_deadline := GREATEST(v_deadline, COALESCE(v_owner_deadline, v_deadline));
    END LOOP;
    PERFORM asset.id FROM public.image_pyramid_assets AS asset
    WHERE asset.task_id = ANY(v_task_ids) ORDER BY asset.id FOR UPDATE NOWAIT;
    PERFORM generation.asset_id FROM public.image_pyramid_generations AS generation
    JOIN public.image_pyramid_assets AS asset ON asset.id = generation.asset_id
    WHERE asset.task_id = ANY(v_task_ids)
    ORDER BY generation.asset_id, generation.generation FOR UPDATE OF generation NOWAIT;
    IF EXISTS (
        SELECT 1 FROM public.image_pyramid_generations AS generation
        JOIN public.image_pyramid_assets AS asset ON asset.id = generation.asset_id
        WHERE asset.task_id = ANY(v_task_ids)
          AND generation.status = 'building' AND generation.lease_expires_at IS NULL
    ) THEN
        RAISE lock_not_available USING MESSAGE = 'pyramid writer lease is unproven';
    END IF;
    FOR v_asset_id IN
        SELECT asset.id FROM public.image_pyramid_assets AS asset
        WHERE asset.task_id = ANY(v_task_ids) ORDER BY asset.id
    LOOP
        SELECT pg_catalog.max(generation.lease_expires_at)
        INTO v_generation_deadline
        FROM public.image_pyramid_generations AS generation
        WHERE generation.asset_id = v_asset_id
          AND generation.status = 'building';
        PERFORM aap_assets_v1._queue_image_garbage(
            'image-pyramids/' || v_asset_id::text || '/',
            'project_deleted',
            GREATEST(v_deadline, COALESCE(v_generation_deadline, v_deadline))
        );
    END LOOP;

    DELETE FROM public.project_datasets AS association
    WHERE association.project_id = p_project_id;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_deleted := v_deleted + v_rows;

    DELETE FROM public.storage_connections AS connection
    WHERE connection.project_id = p_project_id;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_deleted := v_deleted + v_rows;

    DELETE FROM public.image_pyramid_assets AS asset
    WHERE asset.task_id = ANY(v_task_ids);
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_deleted := v_deleted + v_rows;

    RETURN v_deleted;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.delete_project_owned_records(uuid) FROM PUBLIC;
