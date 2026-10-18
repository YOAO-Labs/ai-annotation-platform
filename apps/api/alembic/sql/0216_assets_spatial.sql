-- Assets-owned immutable receipts. This table has no live-resource foreign keys.
CREATE TABLE public.dataset_spatial_operations (
    operation_id uuid PRIMARY KEY,
    actor_id uuid,
    action varchar(32) NOT NULL,
    scope jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(scope) = 'object'),
    request jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(request) = 'object'),
    result jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(result) = 'object'),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    CONSTRAINT ck_dataset_spatial_operations_action CHECK (
        action IN (
            'scene.patch', 'layout.apply', 'pose.upsert', 'scene.create',
            'items.assign', 'scene.source.set', 'calibration.update',
            'pointcloud.prepare', 'calibration.provenance', 'dataset.axis.set'
        )
    )
);
-- aap-statement-break
REVOKE ALL ON public.dataset_spatial_operations FROM PUBLIC;
-- aap-statement-break
COMMENT ON TABLE public.dataset_spatial_operations IS
    'Assets-owned immutable spatial operation receipts; retained beyond live-resource deletion.';
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._spatial_success(p_data jsonb)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_data IS NULL OR pg_catalog.jsonb_typeof(p_data) IS DISTINCT FROM 'object' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-assets-spatial/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_response')
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-spatial/v1', 'ok', true, 'data', p_data
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._spatial_success(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._spatial_failure(p_code text)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_code IS NULL OR p_code NOT IN (
        'invalid_request', 'account_not_active', 'account_busy',
        'invalid_account_role', 'catalog_manager_required', 'dataset_missing',
        'scene_missing', 'source_missing', 'source_busy', 'dataset_busy',
        'scene_conflict', 'source_changed', 'operation_conflict',
        'operation_missing', 'association_changed', 'operation_cancelled',
        'project_missing', 'project_manager_required', 'task_missing',
        'task_not_visible', 'task_busy', 'annotation_write_forbidden',
        'camera_role_not_linked', 'camera_dataset_item_missing',
        'camera_calibration_missing', 'camera_calibration_invalid',
        'calibration_revision_conflict', 'camera_shared_across_projects',
        'storage_unavailable', 'resource_budget_exceeded', 'service_unavailable',
        'invalid_response'
    ) THEN
        p_code := 'invalid_response';
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-spatial/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._spatial_failure(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._lock_spatial_operation(p_operation_id uuid)
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_operation_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'spatial operation id is required';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-dataset-spatial:' || p_operation_id::text, 0)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._lock_spatial_operation(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._read_spatial_receipt(
    p_operation_id uuid,
    p_actor_id uuid,
    p_action text,
    p_scope jsonb,
    p_request jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_saved public.dataset_spatial_operations%ROWTYPE;
BEGIN
    PERFORM aap_assets_v1._lock_spatial_operation(p_operation_id);
    SELECT operation.*
    INTO v_saved
    FROM public.dataset_spatial_operations AS operation
    WHERE operation.operation_id = p_operation_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF v_saved.actor_id IS DISTINCT FROM p_actor_id
       OR v_saved.action IS DISTINCT FROM p_action
       OR v_saved.scope IS DISTINCT FROM p_scope
       OR v_saved.request IS DISTINCT FROM p_request THEN
        RETURN aap_assets_v1._spatial_failure('operation_conflict');
    END IF;
    RETURN v_saved.result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._read_spatial_receipt(uuid, uuid, text, jsonb, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._write_spatial_receipt(
    p_operation_id uuid,
    p_actor_id uuid,
    p_action text,
    p_scope jsonb,
    p_request jsonb,
    p_result jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_stored_actor_id uuid;
    v_stored_action text;
    v_stored_scope jsonb;
    v_stored_request jsonb;
    v_stored_result jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_action IS NULL
       OR pg_catalog.jsonb_typeof(p_scope) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_result) IS DISTINCT FROM 'object' THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    PERFORM aap_assets_v1._lock_spatial_operation(p_operation_id);
    INSERT INTO public.dataset_spatial_operations (
        operation_id, actor_id, action, scope, request, result
    ) VALUES (
        p_operation_id, p_actor_id, p_action, p_scope, p_request, p_result
    ) ON CONFLICT (operation_id) DO NOTHING;
    IF FOUND THEN
        RETURN p_result;
    END IF;
    SELECT operation.actor_id, operation.action, operation.scope,
           operation.request, operation.result
    INTO v_stored_actor_id, v_stored_action, v_stored_scope,
         v_stored_request, v_stored_result
    FROM public.dataset_spatial_operations AS operation
    WHERE operation.operation_id = p_operation_id;
    IF NOT FOUND
       OR v_stored_actor_id IS DISTINCT FROM p_actor_id
       OR v_stored_action IS DISTINCT FROM p_action
       OR v_stored_scope IS DISTINCT FROM p_scope
       OR v_stored_request IS DISTINCT FROM p_request THEN
        RETURN aap_assets_v1._spatial_failure('operation_conflict');
    END IF;
    RETURN v_stored_result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._write_spatial_receipt(uuid, uuid, text, jsonb, jsonb, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._spatial_item_source_snapshot(p_item_id uuid)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'dataset_item_id', item.id,
        'dataset_id', item.dataset_id,
        'file_path', item.file_path,
        'file_size', item.file_size,
        'content_hash', item.content_hash,
        'scene_id', item.scene_id,
        'frame_index', item.frame_index,
        'updated_at', item.updated_at,
        'calibration', COALESCE(latest.calibration, item.metadata -> 'calibration'),
        'revision', CASE
            WHEN latest.revision IS NOT NULL THEN latest.revision
            WHEN item.metadata ? 'calibration' THEN 1
            ELSE NULL
        END,
        'digest', latest.digest
    )
    FROM public.dataset_items AS item
    LEFT JOIN LATERAL (
        SELECT revision.revision, revision.calibration, revision.digest
        FROM public.sensor_calibration_revisions AS revision
        WHERE revision.dataset_item_id = item.id
        ORDER BY revision.revision DESC
        LIMIT 1
    ) AS latest ON true
    WHERE item.id = p_item_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._spatial_item_source_snapshot(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_spatial_actor(
    p_actor_id uuid,
    p_access text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_role text;
BEGIN
    IF p_actor_id IS NULL OR p_access IS NULL
       OR p_access NOT IN ('read', 'manager_read', 'manager_write') THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    v_actor := aap_identity_v1.lock_active_actor(p_actor_id);
    IF v_actor ->> 'schema' IS DISTINCT FROM 'aap-active-actor/v1'
       OR pg_catalog.jsonb_typeof(v_actor -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._spatial_failure('invalid_response');
    END IF;
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        IF v_actor #>> '{error,code}' IN ('account_not_active', 'account_busy') THEN
            RETURN aap_assets_v1._spatial_failure(v_actor #>> '{error,code}');
        END IF;
        RETURN aap_assets_v1._spatial_failure('invalid_account_role');
    END IF;
    v_role := v_actor #>> '{data,platform_role}';
    IF v_role NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
        RETURN aap_assets_v1._spatial_failure('invalid_account_role');
    END IF;
    IF p_access IN ('manager_read', 'manager_write')
       AND v_role NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_assets_v1._spatial_failure('catalog_manager_required');
    END IF;
    RETURN aap_assets_v1._spatial_success(
        pg_catalog.jsonb_build_object('actor_id', p_actor_id, 'access', p_access)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_spatial_actor(uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._spatial_dataset_snapshot(p_dataset_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_dataset record;
    v_item_ids uuid[];
    v_roles jsonb;
    v_rows jsonb;
    v_items jsonb;
    v_scenes jsonb;
    v_digest text;
    v_axis text;
BEGIN
    SELECT
        dataset.id,
        dataset.name,
        dataset.is_temporal,
        dataset.metadata ->> 'axis_convention' AS axis_convention,
        (SELECT pg_catalog.count(*) FROM public.scenes AS scene
         WHERE scene.dataset_id = dataset.id) AS scene_count,
        (SELECT pg_catalog.count(*) FROM public.dataset_items AS item
         WHERE item.dataset_id = dataset.id) AS item_count,
        (SELECT pg_catalog.count(*) FROM public.dataset_items AS item
         WHERE item.dataset_id = dataset.id AND item.scene_id IS NOT NULL) AS assigned_count
    INTO v_dataset
    FROM public.datasets AS dataset
    WHERE dataset.id = p_dataset_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._spatial_failure('dataset_missing');
    END IF;
    v_axis := CASE
        WHEN v_dataset.axis_convention IN (
            'iso_8855', 'ros_rep103', 'kitti_camera', 'opencv_camera',
            'apollo', 'y_forward', 'sustechpoints_demo', 'raw'
        ) THEN v_dataset.axis_convention
        ELSE NULL
    END;

    SELECT COALESCE(
        pg_catalog.array_agg(item.id ORDER BY item.id), ARRAY[]::uuid[]
    ) INTO v_item_ids
    FROM public.dataset_items AS item
    WHERE item.dataset_id = p_dataset_id;
    v_roles := aap_labeling_v1.read_spatial_camera_roles(v_item_ids);
    IF v_roles ->> 'schema' IS DISTINCT FROM 'aap-labeling-spatial-camera-roles/v1'
       OR v_roles -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_roles #> '{data,items}') IS DISTINCT FROM 'array'
       OR NOT aap_assets_v1._spatial_camera_roles_valid(v_roles, v_item_ids) THEN
        RETURN aap_assets_v1._spatial_failure('invalid_response');
    END IF;

    WITH role_items AS MATERIALIZED (
        SELECT
            (role_item.value ->> 'item_id')::uuid AS item_id,
            role_item.value -> 'roles' AS roles
        FROM pg_catalog.jsonb_array_elements(v_roles #> '{data,items}') AS role_item(value)
    ),
    row_snapshots AS (
        SELECT
            0 AS row_kind,
            v_dataset.id::text AS row_key,
            pg_catalog.jsonb_build_object(
                'kind', 'dataset',
                'id', v_dataset.id,
                'name', v_dataset.name,
                'is_temporal', v_dataset.is_temporal,
                'axis_convention', v_dataset.axis_convention,
                'scene_count', v_dataset.scene_count,
                'assigned_count', v_dataset.assigned_count,
                'item_count', v_dataset.item_count
            ) AS row_snapshot
        UNION ALL
        SELECT
            1 AS row_kind,
            scene.id::text AS row_key,
            pg_catalog.jsonb_build_object(
                'kind', 'scene',
                'id', scene.id,
                'display_id', scene.display_id,
                'dataset_id', scene.dataset_id,
                'name', scene.name,
                'source_format', scene.source_format,
                'source_metadata', scene.source_metadata,
                'created_by', scene.created_by,
                'created_at', scene.created_at,
                'updated_at', scene.updated_at
            ) AS row_snapshot
        FROM public.scenes AS scene
        WHERE scene.dataset_id = p_dataset_id
        UNION ALL
        SELECT
            2 AS row_kind,
            item.id::text AS row_key,
            pg_catalog.jsonb_build_object(
                'kind', 'item',
                'id', item.id,
                'dataset_id', item.dataset_id,
                'file_name', item.file_name,
                'file_path', item.file_path,
                'file_type', item.file_type,
                'scene_id', item.scene_id,
                'frame_index', item.frame_index,
                'metadata', item.metadata,
                'created_at', item.created_at,
                'updated_at', item.updated_at,
                'width', item.width,
                'height', item.height,
                'file_size', item.file_size,
                'content_hash', item.content_hash,
                'thumbnail_path', item.thumbnail_path,
                'axis_convention', v_axis,
                'calibration_revision', CASE
                    WHEN latest.revision IS NOT NULL THEN latest.revision
                    WHEN item.metadata ? 'calibration' THEN 1
                    ELSE NULL
                END,
                'source_snapshot', aap_assets_v1._spatial_item_source_snapshot(item.id),
                'camera_roles', COALESCE(role_items.roles, '[]'::jsonb)
            ) AS row_snapshot
        FROM public.dataset_items AS item
        LEFT JOIN role_items ON role_items.item_id = item.id
        LEFT JOIN LATERAL (
            SELECT revision.revision
            FROM public.sensor_calibration_revisions AS revision
            WHERE revision.dataset_item_id = item.id
            ORDER BY revision.revision DESC
            LIMIT 1
        ) AS latest ON true
        WHERE item.dataset_id = p_dataset_id
    )
    SELECT pg_catalog.jsonb_agg(row_snapshot ORDER BY row_kind, row_key)
    INTO v_rows
    FROM row_snapshots;
    IF v_rows IS NULL THEN
        RETURN aap_assets_v1._spatial_failure('invalid_response');
    END IF;
    SELECT COALESCE(jsonb_agg(value - ARRAY['kind', 'updated_at']
        ORDER BY (value ->> 'id')::uuid), '[]'::jsonb)
    INTO v_items
    FROM jsonb_array_elements(v_rows) AS row(value)
    WHERE value ->> 'kind' = 'item';
    SELECT COALESCE(jsonb_agg(value - 'kind'
        ORDER BY (value ->> 'created_at')::timestamptz, (value ->> 'id')::uuid), '[]'::jsonb)
    INTO v_scenes
    FROM jsonb_array_elements(v_rows) AS row(value)
    WHERE value ->> 'kind' = 'scene';
    v_digest := pg_catalog.encode(
        public.digest(pg_catalog.convert_to(v_rows::text, 'UTF8'), 'sha256'),
        'hex'
    );
    RETURN aap_assets_v1._spatial_success(
        pg_catalog.jsonb_build_object(
            'dataset_id', v_dataset.id,
            'dataset_name', v_dataset.name,
            'is_temporal', v_dataset.is_temporal,
            'axis_convention', v_axis,
            'scene_count', v_dataset.scene_count,
            'assigned_count', v_dataset.assigned_count,
            'total', v_dataset.item_count,
            'snapshot_digest', v_digest,
            'items_snapshot', v_items,
            'scenes_snapshot', v_scenes
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._spatial_dataset_snapshot(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._spatial_scene_json(p_scene_id uuid)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', scene.id,
        'display_id', scene.display_id,
        'dataset_id', scene.dataset_id,
        'name', scene.name,
        'source_format', scene.source_format,
        'source_metadata', scene.source_metadata,
        'created_by', scene.created_by,
        'created_at', scene.created_at,
        'updated_at', scene.updated_at
    )
    FROM public.scenes AS scene
    WHERE scene.id = p_scene_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._spatial_scene_json(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._spatial_item_json(
    p_item_id uuid,
    p_axis_convention text,
    p_camera_roles jsonb
)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', item.id,
        'dataset_id', item.dataset_id,
        'file_name', item.file_name,
        'file_path', item.file_path,
        'file_type', item.file_type,
        'scene_id', item.scene_id,
        'frame_index', item.frame_index,
        'metadata', item.metadata,
        'created_at', item.created_at,
        'width', item.width,
        'height', item.height,
        'file_size', item.file_size,
        'content_hash', item.content_hash,
        'thumbnail_path', item.thumbnail_path,
        'axis_convention', p_axis_convention,
        'calibration_revision', CASE
            WHEN latest.revision IS NOT NULL THEN latest.revision
            WHEN item.metadata ? 'calibration' THEN 1
            ELSE NULL
        END,
        'source_snapshot', aap_assets_v1._spatial_item_source_snapshot(item.id),
        'camera_roles', COALESCE(p_camera_roles, '[]'::jsonb)
    )
    FROM public.dataset_items AS item
    LEFT JOIN LATERAL (
        SELECT revision.revision
        FROM public.sensor_calibration_revisions AS revision
        WHERE revision.dataset_item_id = item.id
        ORDER BY revision.revision DESC
        LIMIT 1
    ) AS latest ON true
    WHERE item.id = p_item_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._spatial_item_json(uuid, text, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._spatial_pose_json(
    p_scene_id uuid,
    p_frame_index integer
)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'frame_index', pose.frame_index,
        'timestamp_us', pose.timestamp_us,
        'ego_translation', pose.ego_translation,
        'ego_rotation', pose.ego_rotation,
        'source_metadata', pose.source_metadata
    )
    FROM public.scene_frame_poses AS pose
    WHERE pose.scene_id = p_scene_id
      AND pose.frame_index = p_frame_index
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._spatial_pose_json(uuid, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._spatial_camera_roles_valid(
    p_reply jsonb,
    p_expected_ids uuid[]
)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item jsonb;
    v_role jsonb;
    v_item_id uuid;
    v_seen uuid[] := ARRAY[]::uuid[];
BEGIN
    IF p_reply IS NULL
       OR pg_catalog.jsonb_typeof(p_reply) IS DISTINCT FROM 'object'
       OR (p_reply - ARRAY['schema', 'ok', 'data']) IS DISTINCT FROM '{}'::jsonb
       OR p_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-spatial-camera-roles/v1'
       OR p_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(p_reply -> 'data') IS DISTINCT FROM 'object'
       OR ((p_reply -> 'data') - ARRAY['items']) IS DISTINCT FROM '{}'::jsonb
       OR pg_catalog.jsonb_typeof(p_reply #> '{data,items}') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(p_reply #> '{data,items}') <> pg_catalog.cardinality(p_expected_ids) THEN
        RETURN false;
    END IF;
    FOR v_item IN SELECT value FROM pg_catalog.jsonb_array_elements(p_reply #> '{data,items}')
    LOOP
        IF pg_catalog.jsonb_typeof(v_item) IS DISTINCT FROM 'object'
           OR (v_item - ARRAY['item_id', 'roles']) IS DISTINCT FROM '{}'::jsonb
           OR pg_catalog.jsonb_typeof(v_item -> 'item_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_item -> 'roles') IS DISTINCT FROM 'array' THEN
            RETURN false;
        END IF;
        BEGIN
            v_item_id := (v_item ->> 'item_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN false;
        END;
        IF NOT (v_item_id = ANY(p_expected_ids))
           OR v_item_id = ANY(v_seen) THEN
            RETURN false;
        END IF;
        v_seen := pg_catalog.array_append(v_seen, v_item_id);
        FOR v_role IN SELECT value FROM pg_catalog.jsonb_array_elements(v_item -> 'roles')
        LOOP
            IF pg_catalog.jsonb_typeof(v_role) IS DISTINCT FROM 'string'
               OR pg_catalog.left(v_role #>> '{}', 7) <> 'camera_' THEN
                RETURN false;
            END IF;
        END LOOP;
    END LOOP;
    RETURN pg_catalog.cardinality(v_seen) = pg_catalog.cardinality(p_expected_ids);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._spatial_camera_roles_valid(jsonb, uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._spatial_roles_for_item(
    p_reply jsonb,
    p_item_id uuid
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT role_item.value -> 'roles'
    FROM pg_catalog.jsonb_array_elements(p_reply #> '{data,items}') AS role_item(value)
    WHERE role_item.value ->> 'item_id' = p_item_id::text
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._spatial_roles_for_item(jsonb, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._spatial_item_ids_snapshot(p_item_ids uuid[])
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_roles jsonb;
    v_rows jsonb;
    v_items jsonb;
    v_scenes jsonb;
    v_digest text;
    v_count bigint;
BEGIN
    IF p_item_ids IS NULL OR pg_catalog.array_ndims(p_item_ids) IS DISTINCT FROM 1
       OR pg_catalog.cardinality(p_item_ids) < 1
       OR pg_catalog.array_position(p_item_ids, NULL) IS NOT NULL
       OR (SELECT pg_catalog.count(DISTINCT id)
           FROM pg_catalog.unnest(p_item_ids) AS requested(id))
          <> pg_catalog.cardinality(p_item_ids) THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    v_roles := aap_labeling_v1.read_spatial_camera_roles(p_item_ids);
    IF NOT aap_assets_v1._spatial_camera_roles_valid(v_roles, p_item_ids) THEN
        RETURN aap_assets_v1._spatial_failure('invalid_response');
    END IF;
    SELECT pg_catalog.count(*) INTO v_count
    FROM pg_catalog.unnest(p_item_ids) AS requested(id)
    JOIN public.dataset_items AS item ON item.id = requested.id;
    WITH requested AS MATERIALIZED (
        SELECT unnest.id
        FROM pg_catalog.unnest(p_item_ids) AS unnest(id)
    ),
    item_rows AS (
        SELECT
            0 AS row_kind,
            requested.id::text AS row_key,
            pg_catalog.jsonb_build_object(
                'kind', 'item',
                'item', aap_assets_v1._spatial_item_json(
                    item.id,
                    CASE WHEN dataset.metadata ->> 'axis_convention' IN (
                        'iso_8855', 'ros_rep103', 'kitti_camera', 'opencv_camera',
                        'apollo', 'y_forward', 'sustechpoints_demo', 'raw'
                    ) THEN dataset.metadata ->> 'axis_convention' ELSE NULL END,
                    aap_assets_v1._spatial_roles_for_item(v_roles, item.id)
                )
            ) AS row_snapshot
        FROM requested
        JOIN public.dataset_items AS item ON item.id = requested.id
        JOIN public.datasets AS dataset ON dataset.id = item.dataset_id
        UNION ALL
        SELECT
            0 AS row_kind,
            requested.id::text AS row_key,
            pg_catalog.jsonb_build_object('kind', 'missing_item', 'item_id', requested.id)
        FROM requested
        WHERE NOT EXISTS (
            SELECT 1 FROM public.dataset_items AS item WHERE item.id = requested.id
        )
    ),
    scene_rows AS (
        SELECT DISTINCT
            1 AS row_kind,
            scene.id::text AS row_key,
            pg_catalog.jsonb_build_object(
                'kind', 'scene',
                'id', scene.id,
                'display_id', scene.display_id,
                'dataset_id', scene.dataset_id,
                'name', scene.name,
                'source_format', scene.source_format,
                'source_metadata', scene.source_metadata,
                'created_by', scene.created_by,
                'created_at', scene.created_at,
                'updated_at', scene.updated_at
            ) AS row_snapshot
        FROM requested
        JOIN public.dataset_items AS item ON item.id = requested.id
        JOIN public.scenes AS scene ON scene.id = item.scene_id
    ),
    snapshot_rows AS (
        SELECT * FROM item_rows
        UNION ALL
        SELECT * FROM scene_rows
    )
    SELECT COALESCE(
        pg_catalog.jsonb_agg(row_snapshot ORDER BY row_kind, row_key), '[]'::jsonb
    ) INTO v_rows
    FROM snapshot_rows;
    SELECT COALESCE(jsonb_agg(value -> 'item'
        ORDER BY ((value -> 'item') ->> 'id')::uuid), '[]'::jsonb)
    INTO v_items FROM jsonb_array_elements(v_rows) AS row(value)
    WHERE value ->> 'kind' = 'item';
    SELECT COALESCE(jsonb_agg(value - 'kind'
        ORDER BY (value ->> 'id')::uuid), '[]'::jsonb)
    INTO v_scenes FROM jsonb_array_elements(v_rows) AS row(value)
    WHERE value ->> 'kind' = 'scene';
    v_digest := pg_catalog.encode(
        public.digest(pg_catalog.convert_to(v_rows::text, 'UTF8'), 'sha256'),
        'hex'
    );
    RETURN aap_assets_v1._spatial_success(
        pg_catalog.jsonb_build_object(
            'total', v_count,
            'snapshot_digest', v_digest,
            'items_snapshot', v_items,
            'scenes_snapshot', v_scenes
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._spatial_item_ids_snapshot(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.list_spatial_scenes(
    p_actor_id uuid,
    p_dataset_id uuid,
    p_after_created_at timestamptz,
    p_after_scene_id uuid,
    p_page_size integer
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_guard jsonb;
    v_rows jsonb;
    v_page_rows jsonb[];
    v_scene_rows jsonb;
    v_after jsonb;
    v_next_cursor jsonb;
    v_total bigint;
    v_more boolean;
    v_digest text;
BEGIN
    IF p_dataset_id IS NULL OR p_page_size IS NULL
       OR p_page_size NOT BETWEEN 1 AND 500
       OR ((p_after_created_at IS NULL) <> (p_after_scene_id IS NULL)) THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    IF p_actor_id IS NOT NULL THEN
        v_guard := aap_assets_v1.guard_spatial_actor(p_actor_id, 'read');
        IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN v_guard;
        END IF;
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(aap_assets_v1._spatial_scene_json(scene.id)
        ORDER BY scene.created_at, scene.id), '[]'::jsonb)
    INTO v_rows
    FROM public.scenes AS scene WHERE scene.dataset_id = p_dataset_id;
    v_total := jsonb_array_length(v_rows);
    v_digest := pg_catalog.encode(
        public.digest(pg_catalog.convert_to(v_rows::text, 'UTF8'), 'sha256'), 'hex'
    );
    SELECT COALESCE(pg_catalog.array_agg(page.row ORDER BY page.created_at, page.scene_id), ARRAY[]::jsonb[]),
           COALESCE(pg_catalog.bool_or(page.ordinality > p_page_size), false)
    INTO v_page_rows, v_more
    FROM (
        SELECT value AS row, (value ->> 'id')::uuid AS scene_id,
            (value ->> 'created_at')::timestamptz AS created_at,
            pg_catalog.row_number() OVER (
                ORDER BY (value ->> 'created_at')::timestamptz, (value ->> 'id')::uuid
            ) AS ordinality
        FROM pg_catalog.jsonb_array_elements(v_rows) AS source(value)
        WHERE p_after_created_at IS NULL
           OR ((value ->> 'created_at')::timestamptz, (value ->> 'id')::uuid)
              > (p_after_created_at, p_after_scene_id)
        ORDER BY (value ->> 'created_at')::timestamptz, (value ->> 'id')::uuid
        LIMIT p_page_size + 1
    ) AS page;
    IF v_more THEN
        v_page_rows := v_page_rows[1:p_page_size];
    END IF;
    v_scene_rows := to_jsonb(v_page_rows);
    IF p_after_created_at IS NOT NULL THEN
        v_after := pg_catalog.jsonb_build_object(
            'created_at', p_after_created_at, 'scene_id', p_after_scene_id
        );
    END IF;
    IF v_more AND cardinality(v_page_rows) > 0 THEN
        v_next_cursor := jsonb_build_object(
            'created_at', v_page_rows[cardinality(v_page_rows)] -> 'created_at',
            'scene_id', v_page_rows[cardinality(v_page_rows)] -> 'id'
        );
    END IF;
    RETURN aap_assets_v1._spatial_success(
        pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'dataset_id', p_dataset_id,
            'after', v_after,
            'next_cursor', v_next_cursor,
            'snapshot_digest', v_digest,
            'total', v_total,
            'complete', NOT v_more,
            'scenes', v_scene_rows
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.list_spatial_scenes(uuid, uuid, timestamptz, uuid, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.get_spatial_scene(
    p_actor_id uuid,
    p_scene_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_guard jsonb;
    v_scene jsonb;
BEGIN
    IF p_scene_id IS NULL THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    IF p_actor_id IS NOT NULL THEN
        v_guard := aap_assets_v1.guard_spatial_actor(p_actor_id, 'read');
        IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN v_guard;
        END IF;
    END IF;
    v_scene := aap_assets_v1._spatial_scene_json(p_scene_id);
    IF v_scene IS NULL THEN
        RETURN aap_assets_v1._spatial_failure('scene_missing');
    END IF;
    RETURN aap_assets_v1._spatial_success(
        pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'scene_id', p_scene_id,
            'scene', v_scene
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.get_spatial_scene(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_spatial_layout(
    p_actor_id uuid,
    p_dataset_id uuid,
    p_creation_operation_id uuid,
    p_after_item_id uuid,
    p_page_size integer
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_guard jsonb;
    v_prep jsonb;
    v_snapshot jsonb;
    v_data jsonb;
    v_page_rows jsonb[];
    v_item_ids uuid[];
    v_items jsonb;
    v_scenes jsonb;
    v_after uuid;
    v_next uuid;
    v_more boolean;
BEGIN
    IF p_dataset_id IS NULL OR p_page_size IS NULL
       OR p_page_size NOT BETWEEN 1 AND 500 THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    IF p_actor_id IS NOT NULL THEN
        v_guard := aap_assets_v1.guard_spatial_actor(p_actor_id, 'manager_read');
        IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN v_guard;
        END IF;
    END IF;
    IF p_creation_operation_id IS NOT NULL THEN
        v_prep := aap_labeling_v1.read_spatial_preparation_scope(p_creation_operation_id);
        IF v_prep ->> 'schema' IS DISTINCT FROM 'aap-labeling-spatial-preparation/v1'
           OR pg_catalog.jsonb_typeof(v_prep -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._spatial_failure('invalid_response');
        END IF;
        IF v_prep -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN aap_assets_v1._spatial_failure(
                CASE v_prep #>> '{error,code}'
                    WHEN 'operation_missing' THEN 'operation_missing'
                    WHEN 'operation_cancelled' THEN 'operation_cancelled'
                    WHEN 'account_not_active' THEN 'account_not_active'
                    WHEN 'account_busy' THEN 'account_busy'
                    ELSE 'invalid_response'
                END
            );
        END IF;
        IF (v_prep #>> '{data,dataset_id}')::uuid IS DISTINCT FROM p_dataset_id THEN
            RETURN aap_assets_v1._spatial_failure('operation_conflict');
        END IF;
    END IF;
    v_snapshot := aap_assets_v1._spatial_dataset_snapshot(p_dataset_id);
    IF v_snapshot -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_snapshot;
    END IF;
    v_data := v_snapshot -> 'data';
    IF p_after_item_id IS NOT NULL THEN
        v_after := p_after_item_id;
    END IF;
    SELECT COALESCE(pg_catalog.array_agg(page.item ORDER BY page.id), ARRAY[]::jsonb[]),
           COALESCE(pg_catalog.bool_or(page.ordinality > p_page_size), false)
    INTO v_page_rows, v_more
    FROM (
        SELECT value AS item, (value ->> 'id')::uuid AS id,
            pg_catalog.row_number() OVER (ORDER BY (value ->> 'id')::uuid) AS ordinality
        FROM pg_catalog.jsonb_array_elements(v_data -> 'items_snapshot') AS source(value)
        WHERE p_after_item_id IS NULL OR (value ->> 'id')::uuid > p_after_item_id
        ORDER BY (value ->> 'id')::uuid
        LIMIT p_page_size + 1
    ) AS page;
    IF v_more THEN
        v_page_rows := v_page_rows[1:p_page_size];
    END IF;
    v_items := to_jsonb(v_page_rows);
    SELECT COALESCE(array_agg((item ->> 'id')::uuid ORDER BY (item ->> 'id')::uuid), ARRAY[]::uuid[])
    INTO v_item_ids FROM unnest(v_page_rows) AS rows(item);
    SELECT COALESCE(pg_catalog.jsonb_agg(
        scene.value ORDER BY (scene.value ->> 'created_at')::timestamptz,
        (scene.value ->> 'id')::uuid
    ), '[]'::jsonb)
    INTO v_scenes
    FROM pg_catalog.jsonb_array_elements(v_data -> 'scenes_snapshot') AS scene(value)
    WHERE EXISTS (
        SELECT 1 FROM unnest(v_page_rows) AS page(item)
        WHERE page.item ->> 'scene_id' = scene.value ->> 'id'
    );
    IF v_more AND cardinality(v_item_ids) > 0 THEN
        v_next := v_item_ids[pg_catalog.cardinality(v_item_ids)];
    END IF;
    RETURN aap_assets_v1._spatial_success(pg_catalog.jsonb_build_object(
        'actor_id', p_actor_id,
        'dataset_id', p_dataset_id,
        'creation_operation_id', p_creation_operation_id,
        'dataset_name', v_data -> 'dataset_name',
        'is_temporal', v_data -> 'is_temporal',
        'axis_convention', v_data -> 'axis_convention',
        'scene_count', v_data -> 'scene_count',
        'assigned_count', v_data -> 'assigned_count',
        'after_item_id', v_after,
        'next_item_id', v_next,
        'snapshot_digest', v_data -> 'snapshot_digest',
        'total', v_data -> 'total',
        'complete', NOT v_more,
        'items', v_items,
        'scenes', v_scenes
    ));
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_spatial_layout(uuid, uuid, uuid, uuid, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_spatial_items(
    p_dataset_id uuid,
    p_item_ids uuid[],
    p_after_item_id uuid,
    p_page_size integer
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_snapshot jsonb;
    v_data jsonb;
    v_page_rows jsonb[];
    v_item_ids uuid[];
    v_items jsonb;
    v_scenes jsonb;
    v_total bigint;
    v_more boolean;
    v_next uuid;
    v_digest text;
BEGIN
    IF p_page_size IS NULL OR p_page_size NOT BETWEEN 1 AND 500
       OR ((p_dataset_id IS NULL) = (p_item_ids IS NULL)) THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    IF p_item_ids IS NOT NULL AND (
        pg_catalog.array_ndims(p_item_ids) IS DISTINCT FROM 1
        OR pg_catalog.cardinality(p_item_ids) < 1
        OR pg_catalog.cardinality(p_item_ids) > 5000
        OR pg_catalog.array_position(p_item_ids, NULL) IS NOT NULL
        OR (SELECT pg_catalog.count(DISTINCT id)
            FROM pg_catalog.unnest(p_item_ids) AS requested(id))
           <> pg_catalog.cardinality(p_item_ids)
    ) THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    IF p_dataset_id IS NOT NULL THEN
        v_snapshot := aap_assets_v1._spatial_dataset_snapshot(p_dataset_id);
        IF v_snapshot -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN v_snapshot;
        END IF;
        v_data := v_snapshot -> 'data';
        v_total := (v_data ->> 'total')::bigint;
        v_digest := v_data ->> 'snapshot_digest';
        SELECT COALESCE(pg_catalog.array_agg(page.item ORDER BY page.id), ARRAY[]::jsonb[]),
               COALESCE(pg_catalog.bool_or(page.ordinality > p_page_size), false)
        INTO v_page_rows, v_more
        FROM (
            SELECT value AS item, (value ->> 'id')::uuid AS id,
                pg_catalog.row_number() OVER (ORDER BY (value ->> 'id')::uuid) AS ordinality
            FROM pg_catalog.jsonb_array_elements(v_data -> 'items_snapshot') AS source(value)
            WHERE p_after_item_id IS NULL OR (value ->> 'id')::uuid > p_after_item_id
            ORDER BY (value ->> 'id')::uuid
            LIMIT p_page_size + 1
        ) AS page;
    ELSE
        v_snapshot := aap_assets_v1._spatial_item_ids_snapshot(p_item_ids);
        IF v_snapshot -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN v_snapshot;
        END IF;
        v_data := v_snapshot -> 'data';
        v_total := (v_data ->> 'total')::bigint;
        v_digest := v_data ->> 'snapshot_digest';
        IF v_total <> pg_catalog.cardinality(p_item_ids) THEN
            RETURN aap_assets_v1._spatial_failure('source_missing');
        END IF;
        SELECT COALESCE(pg_catalog.array_agg(page.item ORDER BY page.id), ARRAY[]::jsonb[]),
               COALESCE(pg_catalog.bool_or(page.ordinality > p_page_size), false)
        INTO v_page_rows, v_more
        FROM (
            SELECT value AS item, (value ->> 'id')::uuid AS id,
                pg_catalog.row_number() OVER (ORDER BY (value ->> 'id')::uuid) AS ordinality
            FROM pg_catalog.jsonb_array_elements(v_data -> 'items_snapshot') AS source(value)
            WHERE p_after_item_id IS NULL OR (value ->> 'id')::uuid > p_after_item_id
            ORDER BY (value ->> 'id')::uuid
            LIMIT p_page_size + 1
        ) AS page;
    END IF;
    IF v_more THEN
        v_page_rows := v_page_rows[1:p_page_size];
    END IF;
    v_items := to_jsonb(v_page_rows);
    SELECT COALESCE(array_agg((item ->> 'id')::uuid ORDER BY (item ->> 'id')::uuid), ARRAY[]::uuid[])
    INTO v_item_ids FROM unnest(v_page_rows) AS rows(item);
    SELECT COALESCE(pg_catalog.jsonb_agg(
        scene.value ORDER BY (scene.value ->> 'created_at')::timestamptz,
        (scene.value ->> 'id')::uuid
    ), '[]'::jsonb)
    INTO v_scenes
    FROM pg_catalog.jsonb_array_elements(v_data -> 'scenes_snapshot') AS scene(value)
    WHERE EXISTS (
        SELECT 1 FROM unnest(v_page_rows) AS page(item)
        WHERE page.item ->> 'scene_id' = scene.value ->> 'id'
    );
    IF v_more AND cardinality(v_item_ids) > 0 THEN
        v_next := v_item_ids[pg_catalog.cardinality(v_item_ids)];
    END IF;
    RETURN aap_assets_v1._spatial_success(pg_catalog.jsonb_build_object(
        'dataset_id', p_dataset_id,
        'item_ids', CASE WHEN p_item_ids IS NULL THEN NULL ELSE to_jsonb(p_item_ids) END,
        'after_item_id', p_after_item_id,
        'next_item_id', v_next,
        'snapshot_digest', v_digest,
        'total', v_total,
        'complete', NOT v_more,
        'items', v_items,
        'scenes', v_scenes
    ));
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_spatial_items(uuid, uuid[], uuid, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_spatial_poses(
    p_actor_id uuid,
    p_scene_id uuid,
    p_after_frame_index integer,
    p_page_size integer
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_guard jsonb;
    v_scene public.scenes%ROWTYPE;
    v_rows jsonb;
    v_page_rows jsonb[];
    v_page jsonb;
    v_total bigint;
    v_more boolean;
    v_next integer;
    v_digest text;
BEGIN
    IF p_scene_id IS NULL OR p_page_size IS NULL OR p_page_size NOT BETWEEN 1 AND 500
       OR (p_after_frame_index IS NOT NULL AND p_after_frame_index < 0) THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    IF p_actor_id IS NOT NULL THEN
        v_guard := aap_assets_v1.guard_spatial_actor(p_actor_id, 'read');
        IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_guard; END IF;
    END IF;
    SELECT scene.* INTO v_scene FROM public.scenes AS scene WHERE scene.id = p_scene_id;
    IF NOT FOUND THEN RETURN aap_assets_v1._spatial_failure('scene_missing'); END IF;
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'frame_index', pose.frame_index, 'timestamp_us', pose.timestamp_us,
        'ego_translation', pose.ego_translation, 'ego_rotation', pose.ego_rotation,
        'source_metadata', pose.source_metadata
    ) ORDER BY pose.frame_index), '[]'::jsonb)
    INTO v_rows FROM public.scene_frame_poses AS pose WHERE pose.scene_id = p_scene_id;
    v_total := jsonb_array_length(v_rows);
    v_digest := encode(public.digest(convert_to(v_rows::text, 'UTF8'), 'sha256'), 'hex');
    SELECT COALESCE(array_agg(page.row ORDER BY page.frame_index), ARRAY[]::jsonb[]),
           COALESCE(bool_or(page.ordinality > p_page_size), false)
    INTO v_page_rows, v_more
    FROM (
        SELECT value AS row, (value ->> 'frame_index')::integer AS frame_index,
            row_number() OVER (ORDER BY (value ->> 'frame_index')::integer) AS ordinality
        FROM jsonb_array_elements(v_rows) AS source(value)
        WHERE p_after_frame_index IS NULL
           OR (value ->> 'frame_index')::integer > p_after_frame_index
        ORDER BY (value ->> 'frame_index')::integer LIMIT p_page_size + 1
    ) AS page;
    v_more := COALESCE(v_more, false);
    IF v_more THEN
        v_page_rows := v_page_rows[1:p_page_size];
    END IF;
    v_page := to_jsonb(v_page_rows);
    IF cardinality(v_page_rows) > 0 THEN
        v_next := (v_page_rows[cardinality(v_page_rows)] ->> 'frame_index')::integer;
    END IF;
    RETURN aap_assets_v1._spatial_success(jsonb_build_object(
        'actor_id', p_actor_id, 'scene_id', p_scene_id,
        'after_frame_index', p_after_frame_index, 'next_frame_index',
        CASE WHEN v_more THEN v_next ELSE NULL END,
        'snapshot_digest', v_digest, 'total', v_total, 'complete', NOT v_more,
        'poses', v_page
    ));
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_spatial_poses(uuid, uuid, integer, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_spatial_pose_pairs(p_pairs jsonb)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_pair jsonb;
    v_scene_id uuid;
    v_scene_text text;
    v_frame_value jsonb;
    v_frame_numeric numeric;
    v_pairs jsonb;
    v_unique_count integer;
    v_poses jsonb;
    v_snapshot_digest text;
BEGIN
    IF jsonb_typeof(p_pairs) IS DISTINCT FROM 'array' THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    IF jsonb_array_length(p_pairs) NOT BETWEEN 1 AND 5000 THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;

    -- Validate every supplied pair, including canonical UUID layout (either
    -- hex case) and integral frame bounds, before reading any pose rows.
    FOR v_pair IN
        SELECT value FROM jsonb_array_elements(p_pairs) AS requested(value)
    LOOP
        IF jsonb_typeof(v_pair) IS DISTINCT FROM 'object'
           OR (v_pair - ARRAY['scene_id', 'frame_index']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (v_pair ?& ARRAY['scene_id', 'frame_index'])
           OR jsonb_typeof(v_pair -> 'scene_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(v_pair -> 'frame_index') IS DISTINCT FROM 'number' THEN
            RETURN aap_assets_v1._spatial_failure('invalid_request');
        END IF;

        v_scene_text := v_pair ->> 'scene_id';
        v_scene_id := v_scene_text::uuid;
        IF lower(v_scene_id::text) IS DISTINCT FROM lower(v_scene_text) THEN
            RETURN aap_assets_v1._spatial_failure('invalid_request');
        END IF;

        v_frame_value := v_pair -> 'frame_index';
        v_frame_numeric := (v_frame_value #>> '{}')::numeric;
        IF v_frame_numeric <> trunc(v_frame_numeric)
           OR v_frame_numeric < 0
           OR v_frame_numeric > 2147483647 THEN
            RETURN aap_assets_v1._spatial_failure('invalid_request');
        END IF;
    END LOOP;

    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'scene_id', (value ->> 'scene_id')::uuid,
        'frame_index', trunc((value ->> 'frame_index')::numeric)::integer
    ) ORDER BY ordinality), '[]'::jsonb)
    INTO v_pairs
    FROM jsonb_array_elements(p_pairs) WITH ORDINALITY AS requested(value, ordinality);

    SELECT count(*) INTO v_unique_count
    FROM (
        SELECT (value ->> 'scene_id')::uuid AS scene_id,
               (value ->> 'frame_index')::integer AS frame_index
        FROM jsonb_array_elements(v_pairs) AS requested(value)
        GROUP BY 1, 2
    ) AS unique_pairs;
    IF v_unique_count <> jsonb_array_length(v_pairs) THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;

    WITH requested AS MATERIALIZED (
        SELECT (value ->> 'scene_id')::uuid AS scene_id,
               (value ->> 'frame_index')::integer AS frame_index
        FROM jsonb_array_elements(v_pairs) AS input(value)
    )
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'scene_id', pose.scene_id,
        'frame_index', pose.frame_index,
        'timestamp_us', pose.timestamp_us,
        'ego_translation', pose.ego_translation,
        'ego_rotation', pose.ego_rotation,
        'source_metadata', pose.source_metadata
    ) ORDER BY pose.scene_id, pose.frame_index), '[]'::jsonb)
    INTO v_poses
    FROM requested
    JOIN public.scene_frame_poses AS pose
      ON pose.scene_id = requested.scene_id
     AND pose.frame_index = requested.frame_index;

    v_snapshot_digest := encode(public.digest(convert_to(jsonb_build_object(
        'pairs', v_pairs, 'poses', v_poses
    )::text, 'UTF8'), 'sha256'), 'hex');
    RETURN aap_assets_v1._spatial_success(jsonb_build_object(
        'pairs', v_pairs,
        'poses', v_poses,
        'snapshot_digest', v_snapshot_digest
    ));
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN aap_assets_v1._spatial_failure('invalid_request');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_spatial_pose_pairs(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_spatial_calibrations(
    p_dataset_item_ids uuid[],
    p_include_history boolean,
    p_after_item_id uuid,
    p_after_revision integer,
    p_page_size integer
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_records jsonb;
    v_page jsonb;
    v_total bigint;
    v_more boolean;
    v_next_item uuid;
    v_next_revision integer;
    v_digest text;
BEGIN
    IF p_dataset_item_ids IS NULL OR p_include_history IS NULL
       OR array_ndims(p_dataset_item_ids) IS DISTINCT FROM 1
       OR cardinality(p_dataset_item_ids) < 1 OR cardinality(p_dataset_item_ids) > 5000
       OR array_position(p_dataset_item_ids, NULL) IS NOT NULL
       OR p_page_size IS NULL OR p_page_size NOT BETWEEN 1 AND 500
       OR ((p_after_item_id IS NULL) <> (p_after_revision IS NULL))
       OR (p_after_revision IS NOT NULL AND p_after_revision < 1)
       OR (SELECT count(DISTINCT item_id) FROM unnest(p_dataset_item_ids) AS requested(item_id))
          <> cardinality(p_dataset_item_ids) THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    WITH requested AS MATERIALIZED (
        SELECT item_id FROM unnest(p_dataset_item_ids) AS input(item_id)
    ),
    records AS (
        SELECT item.id AS dataset_item_id, revision.revision,
            revision.calibration, revision.digest, revision.created_at,
            aap_assets_v1._spatial_item_source_snapshot(item.id) AS source_snapshot,
            NULL::text AS error
        FROM requested
        JOIN public.dataset_items AS item ON item.id = requested.item_id
        JOIN public.sensor_calibration_revisions AS revision
          ON revision.dataset_item_id = item.id
        WHERE p_include_history
        UNION ALL
        SELECT item.id, revision.revision, revision.calibration,
            revision.digest, revision.created_at,
            aap_assets_v1._spatial_item_source_snapshot(item.id), NULL::text
        FROM requested
        JOIN public.dataset_items AS item ON item.id = requested.item_id
        JOIN LATERAL (
            SELECT history.revision, history.calibration, history.digest, history.created_at
            FROM public.sensor_calibration_revisions AS history
            WHERE history.dataset_item_id = item.id
            ORDER BY history.revision DESC LIMIT 1
        ) AS revision ON true
        WHERE NOT p_include_history
        UNION ALL
        SELECT item.id, 1, item.metadata -> 'calibration', NULL::text, NULL::timestamptz,
            aap_assets_v1._spatial_item_source_snapshot(item.id),
            CASE WHEN item.metadata ? 'calibration' THEN NULL::text
                 ELSE 'camera_calibration_missing' END
        FROM requested
        JOIN public.dataset_items AS item ON item.id = requested.item_id
        WHERE NOT EXISTS (
            SELECT 1 FROM public.sensor_calibration_revisions AS history
            WHERE history.dataset_item_id = item.id
        )
        UNION ALL
        SELECT requested.item_id, 1, NULL::jsonb, NULL::text, NULL::timestamptz,
            '{}'::jsonb, 'camera_dataset_item_missing'::text
        FROM requested
        WHERE NOT EXISTS (
            SELECT 1 FROM public.dataset_items AS item WHERE item.id = requested.item_id
        )
    )
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'dataset_item_id', dataset_item_id, 'revision', revision,
        'calibration', calibration, 'digest', digest, 'created_at', created_at,
        'source_snapshot', source_snapshot, 'error', error
    ) ORDER BY dataset_item_id, revision DESC), '[]'::jsonb), count(*)
    INTO v_records, v_total FROM records;
    v_digest := encode(public.digest(convert_to(v_records::text, 'UTF8'), 'sha256'), 'hex');
    WITH all_rows AS (
        SELECT value AS row,
            (value ->> 'dataset_item_id')::uuid AS item_id,
            (value ->> 'revision')::integer AS revision
        FROM jsonb_array_elements(v_records)
    ), page AS (
        SELECT all_rows.*,
            row_number() OVER (ORDER BY item_id, revision DESC) AS ordinality
        FROM all_rows
        WHERE p_after_item_id IS NULL
           OR item_id > p_after_item_id
           OR (item_id = p_after_item_id AND revision < p_after_revision)
        ORDER BY item_id, revision DESC
        LIMIT p_page_size + 1
    )
    SELECT COALESCE(jsonb_agg(row ORDER BY item_id, revision DESC), '[]'::jsonb),
           bool_or(ordinality > p_page_size)
    INTO v_page, v_more FROM page;
    v_more := COALESCE(v_more, false);
    IF v_more THEN
        SELECT COALESCE(jsonb_agg(row ORDER BY item_id, revision DESC), '[]'::jsonb),
               (array_agg(item_id ORDER BY item_id, revision DESC))[p_page_size],
               (array_agg(revision ORDER BY item_id, revision DESC))[p_page_size]
        INTO v_page, v_next_item, v_next_revision
        FROM (
            SELECT value AS row,
                (value ->> 'dataset_item_id')::uuid AS item_id,
                (value ->> 'revision')::integer AS revision
            FROM jsonb_array_elements(v_records)
            WHERE p_after_item_id IS NULL
               OR (value ->> 'dataset_item_id')::uuid > p_after_item_id
               OR ((value ->> 'dataset_item_id')::uuid = p_after_item_id
                   AND (value ->> 'revision')::integer < p_after_revision)
            ORDER BY (value ->> 'dataset_item_id')::uuid, (value ->> 'revision')::integer DESC
            LIMIT p_page_size
        ) AS page;
    END IF;
    IF NOT v_more AND jsonb_array_length(v_page) > 0 THEN
        v_next_item := (v_page -> (jsonb_array_length(v_page) - 1) ->> 'dataset_item_id')::uuid;
        v_next_revision := (v_page -> (jsonb_array_length(v_page) - 1) ->> 'revision')::integer;
    END IF;
    RETURN aap_assets_v1._spatial_success(jsonb_build_object(
        'dataset_item_ids', to_jsonb(p_dataset_item_ids),
        'include_history', p_include_history,
        'after', CASE WHEN p_after_item_id IS NULL THEN NULL ELSE
            jsonb_build_object('dataset_item_id', p_after_item_id, 'revision', p_after_revision) END,
        'next_cursor', CASE WHEN v_more THEN
            jsonb_build_object('dataset_item_id', v_next_item, 'revision', v_next_revision)
            ELSE NULL END,
        'snapshot_digest', v_digest, 'total', v_total, 'complete', NOT v_more,
        'records', v_page
    ));
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_spatial_calibrations(uuid[], boolean, uuid, integer, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.lock_spatial_calibration_facts(
    p_actor_id uuid,
    p_task_id uuid,
    p_camera_role text,
    p_dataset_item_id uuid,
    p_expected_source_snapshot jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item public.dataset_items%ROWTYPE;
    v_authority jsonb;
    v_snapshot jsonb;
    v_record jsonb;
    v_revision integer;
    v_calibration jsonb;
    v_digest text;
    v_created_at timestamptz;
BEGIN
    IF p_dataset_item_id IS NULL OR jsonb_typeof(p_expected_source_snapshot) IS DISTINCT FROM 'object'
       OR ((p_actor_id IS NULL OR p_task_id IS NULL OR p_camera_role IS NULL)
           AND NOT (p_actor_id IS NULL AND p_task_id IS NULL AND p_camera_role IS NULL))
       OR (p_actor_id IS NOT NULL AND (
           char_length(p_camera_role) NOT BETWEEN 8 AND 50
           OR left(p_camera_role, 7) <> 'camera_'
       )) THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    BEGIN
        SELECT item.* INTO v_item
        FROM public.dataset_items AS item
        WHERE item.id = p_dataset_item_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._spatial_failure('source_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._spatial_failure('camera_dataset_item_missing');
    END IF;
    IF p_actor_id IS NOT NULL THEN
        v_authority := aap_labeling_v1.lock_spatial_camera_authority(
            p_actor_id, p_task_id, p_camera_role, p_dataset_item_id
        );
        IF v_authority ->> 'schema' IS DISTINCT FROM 'aap-labeling-spatial-camera-authority/v1'
           OR jsonb_typeof(v_authority -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._spatial_failure('invalid_response');
        END IF;
        IF v_authority -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN aap_assets_v1._spatial_failure(CASE v_authority #>> '{error,code}'
                WHEN 'account_not_active' THEN 'account_not_active'
                WHEN 'account_busy' THEN 'account_busy'
                WHEN 'task_missing' THEN 'task_missing'
                WHEN 'task_busy' THEN 'task_busy'
                WHEN 'project_missing' THEN 'project_missing'
                WHEN 'project_manager_required' THEN 'project_manager_required'
                WHEN 'camera_role_not_linked' THEN 'camera_role_not_linked'
                WHEN 'camera_shared_across_projects' THEN 'camera_shared_across_projects'
                ELSE 'invalid_response' END);
        END IF;
    END IF;
    v_snapshot := aap_assets_v1._spatial_item_source_snapshot(p_dataset_item_id);
    IF v_snapshot IS NULL THEN
        RETURN aap_assets_v1._spatial_failure('camera_dataset_item_missing');
    END IF;
    IF v_snapshot IS DISTINCT FROM p_expected_source_snapshot THEN
        RETURN aap_assets_v1._spatial_failure('source_changed');
    END IF;
    SELECT revision.revision, revision.calibration, revision.digest, revision.created_at
    INTO v_revision, v_calibration, v_digest, v_created_at
    FROM public.sensor_calibration_revisions AS revision
    WHERE revision.dataset_item_id = p_dataset_item_id
    ORDER BY revision.revision DESC LIMIT 1;
    IF NOT FOUND THEN
        v_revision := 1;
        v_calibration := v_item.metadata -> 'calibration';
        v_digest := NULL;
        v_created_at := NULL;
    END IF;
    IF v_calibration IS NULL THEN
        v_record := jsonb_build_object(
            'dataset_item_id', p_dataset_item_id, 'revision', v_revision,
            'calibration', NULL, 'digest', v_digest, 'created_at', v_created_at,
            'source_snapshot', v_snapshot, 'error', 'camera_calibration_missing'
        );
    ELSE
        v_record := jsonb_build_object(
            'dataset_item_id', p_dataset_item_id, 'revision', v_revision,
            'calibration', v_calibration, 'digest', v_digest, 'created_at', v_created_at,
            'source_snapshot', v_snapshot, 'error', NULL
        );
    END IF;
    RETURN aap_assets_v1._spatial_success(v_record);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.lock_spatial_calibration_facts(uuid, uuid, text, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.mutate_spatial_scene(
    p_operation_id uuid,
    p_actor_id uuid,
    p_scene_id uuid,
    p_patch jsonb,
    p_audit_context jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_guard jsonb;
    v_receipt jsonb;
    v_scene public.scenes%ROWTYPE;
    v_result jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_scene_id IS NULL
       OR jsonb_typeof(p_patch) IS DISTINCT FROM 'object'
       OR jsonb_typeof(p_audit_context) IS DISTINCT FROM 'object'
       OR (p_patch - ARRAY['name', 'source_format', 'source_metadata']) IS DISTINCT FROM '{}'::jsonb
       OR (p_patch ? 'name' AND (jsonb_typeof(p_patch -> 'name') IS DISTINCT FROM 'string'
            OR char_length(p_patch ->> 'name') NOT BETWEEN 1 AND 255))
       OR (p_patch ? 'source_format' AND (jsonb_typeof(p_patch -> 'source_format') IS DISTINCT FROM 'string'
            OR char_length(p_patch ->> 'source_format') > 50))
       OR (p_patch ? 'source_metadata' AND jsonb_typeof(p_patch -> 'source_metadata') IS DISTINCT FROM 'object') THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    v_guard := aap_assets_v1.guard_spatial_actor(p_actor_id, 'manager_write');
    IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_guard; END IF;
    v_receipt := aap_assets_v1._read_spatial_receipt(
        p_operation_id, p_actor_id, 'scene.patch',
        jsonb_build_object('scene_id', p_scene_id), p_patch
    );
    IF v_receipt IS NOT NULL THEN RETURN v_receipt; END IF;
    BEGIN
        SELECT scene.* INTO v_scene FROM public.scenes AS scene
        WHERE scene.id = p_scene_id FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._spatial_failure('scene_conflict');
    END;
    IF NOT FOUND THEN RETURN aap_assets_v1._spatial_failure('scene_missing'); END IF;
    UPDATE public.scenes AS scene SET
        name = CASE WHEN p_patch ? 'name' THEN p_patch ->> 'name' ELSE scene.name END,
        source_format = CASE WHEN p_patch ? 'source_format' THEN p_patch ->> 'source_format' ELSE scene.source_format END,
        source_metadata = CASE WHEN p_patch ? 'source_metadata' THEN p_patch -> 'source_metadata' ELSE scene.source_metadata END,
        updated_at = now()
    WHERE scene.id = p_scene_id
      AND (
          (p_patch ? 'name' AND scene.name IS DISTINCT FROM p_patch ->> 'name')
          OR (p_patch ? 'source_format' AND scene.source_format IS DISTINCT FROM p_patch ->> 'source_format')
          OR (p_patch ? 'source_metadata' AND scene.source_metadata IS DISTINCT FROM p_patch -> 'source_metadata')
      );
    SELECT aap_assets_v1._spatial_scene_json(p_scene_id) INTO v_result;
    v_result := aap_assets_v1._spatial_success(jsonb_build_object(
        'operation_id', p_operation_id, 'actor_id', p_actor_id,
        'scene_id', p_scene_id, 'scene', v_result
    ));
    RETURN aap_assets_v1._write_spatial_receipt(
        p_operation_id, p_actor_id, 'scene.patch',
        jsonb_build_object('scene_id', p_scene_id), p_patch, v_result
    );
EXCEPTION WHEN unique_violation THEN
    RETURN aap_assets_v1._spatial_failure('scene_conflict');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.mutate_spatial_scene(uuid, uuid, uuid, jsonb, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.create_spatial_scene(
    p_operation_id uuid,
    p_scene_id uuid,
    p_dataset_id uuid,
    p_name text,
    p_source_format text,
    p_source_metadata jsonb,
    p_created_by uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_receipt jsonb;
    v_scene_id uuid := COALESCE(p_scene_id, pg_catalog.gen_random_uuid());
    v_result jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_dataset_id IS NULL
       OR p_name IS NULL OR char_length(p_name) > 255
       OR (p_source_format IS NOT NULL AND char_length(p_source_format) > 50)
       OR jsonb_typeof(p_source_metadata) IS DISTINCT FROM 'object' THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    v_receipt := aap_assets_v1._read_spatial_receipt(
        p_operation_id, NULL, 'scene.create',
        jsonb_build_object('dataset_id', p_dataset_id),
        jsonb_build_object('name', p_name, 'source_format', p_source_format,
            'source_metadata', p_source_metadata, 'scene_id', p_scene_id,
            'created_by', p_created_by)
    );
    IF v_receipt IS NOT NULL THEN RETURN v_receipt; END IF;
    PERFORM 1 FROM public.datasets AS dataset WHERE dataset.id = p_dataset_id FOR KEY SHARE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._spatial_failure('dataset_missing'); END IF;
    INSERT INTO public.scenes (
        id, display_id, dataset_id, name, source_format, source_metadata, created_by
    ) VALUES (
        v_scene_id, 'SCN-' || nextval('public.display_seq_scenes')::text, p_dataset_id,
        p_name, p_source_format, p_source_metadata, p_created_by
    );
    v_result := aap_assets_v1._spatial_success(jsonb_build_object(
        'operation_id', p_operation_id, 'actor_id', NULL,
        'scene_id', v_scene_id, 'scene', aap_assets_v1._spatial_scene_json(v_scene_id)
    ));
    RETURN aap_assets_v1._write_spatial_receipt(
        p_operation_id, NULL, 'scene.create',
        jsonb_build_object('dataset_id', p_dataset_id),
        jsonb_build_object('name', p_name, 'source_format', p_source_format,
            'source_metadata', p_source_metadata, 'scene_id', p_scene_id,
            'created_by', p_created_by), v_result
    );
EXCEPTION WHEN unique_violation THEN
    RETURN aap_assets_v1._spatial_failure('scene_conflict');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.create_spatial_scene(uuid, uuid, uuid, text, text, jsonb, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.set_spatial_scene_source(
    p_operation_id uuid,
    p_scene_id uuid,
    p_source_format text,
    p_source_metadata jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_receipt jsonb;
    v_result jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_scene_id IS NULL
       OR (p_source_format IS NOT NULL AND char_length(p_source_format) > 50)
       OR jsonb_typeof(p_source_metadata) IS DISTINCT FROM 'object' THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    v_receipt := aap_assets_v1._read_spatial_receipt(
        p_operation_id, NULL, 'scene.source.set', jsonb_build_object('scene_id', p_scene_id),
        jsonb_build_object('source_format', p_source_format, 'source_metadata', p_source_metadata)
    );
    IF v_receipt IS NOT NULL THEN RETURN v_receipt; END IF;
    BEGIN
        PERFORM scene.id FROM public.scenes AS scene
        WHERE scene.id = p_scene_id FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._spatial_failure('scene_conflict');
    END;
    IF NOT FOUND THEN RETURN aap_assets_v1._spatial_failure('scene_missing'); END IF;
    BEGIN
        UPDATE public.scenes AS scene SET
            source_format = p_source_format,
            source_metadata = p_source_metadata,
            updated_at = now()
        WHERE scene.id = p_scene_id
          AND (scene.source_format IS DISTINCT FROM p_source_format
               OR scene.source_metadata IS DISTINCT FROM p_source_metadata);
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._spatial_failure('scene_conflict');
    END;
    v_result := aap_assets_v1._spatial_success(jsonb_build_object(
        'operation_id', p_operation_id, 'actor_id', NULL,
        'scene_id', p_scene_id, 'scene', aap_assets_v1._spatial_scene_json(p_scene_id)
    ));
    RETURN aap_assets_v1._write_spatial_receipt(
        p_operation_id, NULL, 'scene.source.set', jsonb_build_object('scene_id', p_scene_id),
        jsonb_build_object('source_format', p_source_format, 'source_metadata', p_source_metadata), v_result
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.set_spatial_scene_source(uuid, uuid, text, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.assign_spatial_items(
    p_operation_id uuid,
    p_dataset_id uuid,
    p_assignments jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_receipt jsonb;
    v_assigned integer;
    v_item_id uuid;
    v_scene_id uuid;
    v_frame_index integer;
    v_scene_dataset uuid;
    v_result jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_dataset_id IS NULL
       OR jsonb_typeof(p_assignments) IS DISTINCT FROM 'array'
       THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_assignments) AS entry(value)
        WHERE jsonb_typeof(value) IS DISTINCT FROM 'object'
           OR (value - ARRAY['item_id', 'scene_id', 'frame_index']) IS DISTINCT FROM '{}'::jsonb
           OR jsonb_typeof(value -> 'item_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(value -> 'scene_id') IS DISTINCT FROM 'string'
           OR (value ? 'frame_index' AND value -> 'frame_index' <> 'null'::jsonb
               AND (jsonb_typeof(value -> 'frame_index') IS DISTINCT FROM 'number'
                    OR (value ->> 'frame_index')::numeric < 0
                    OR (value ->> 'frame_index')::numeric <> trunc((value ->> 'frame_index')::numeric)))
    ) OR (SELECT count(DISTINCT value ->> 'item_id') FROM jsonb_array_elements(p_assignments))
         <> jsonb_array_length(p_assignments) THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    v_receipt := aap_assets_v1._read_spatial_receipt(
        p_operation_id, NULL, 'items.assign', jsonb_build_object('dataset_id', p_dataset_id),
        jsonb_build_object('assignments', p_assignments)
    );
    IF v_receipt IS NOT NULL THEN RETURN v_receipt; END IF;
    IF NOT EXISTS (SELECT 1 FROM public.datasets AS dataset WHERE dataset.id = p_dataset_id FOR KEY SHARE NOWAIT) THEN
        RETURN aap_assets_v1._spatial_failure('dataset_missing');
    END IF;
    FOR v_item_id, v_scene_id, v_frame_index IN
        SELECT (value ->> 'item_id')::uuid, (value ->> 'scene_id')::uuid,
               CASE WHEN value -> 'frame_index' IS NULL OR value -> 'frame_index' = 'null'::jsonb
                    THEN NULL ELSE (value ->> 'frame_index')::integer END
        FROM jsonb_array_elements(p_assignments) AS entry(value)
        ORDER BY (value ->> 'item_id')::uuid
    LOOP
        PERFORM 1 FROM public.dataset_items AS item
        WHERE item.id = v_item_id AND item.dataset_id = p_dataset_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN RETURN aap_assets_v1._spatial_failure('source_missing'); END IF;
        SELECT scene.dataset_id INTO v_scene_dataset
        FROM public.scenes AS scene WHERE scene.id = v_scene_id FOR KEY SHARE NOWAIT;
        IF NOT FOUND OR v_scene_dataset IS DISTINCT FROM p_dataset_id THEN
            RETURN aap_assets_v1._spatial_failure('scene_conflict');
        END IF;
    END LOOP;
    UPDATE public.dataset_items AS item SET
        scene_id = (entry.value ->> 'scene_id')::uuid,
        frame_index = CASE WHEN entry.value -> 'frame_index' IS NULL OR entry.value -> 'frame_index' = 'null'::jsonb
                           THEN NULL ELSE (entry.value ->> 'frame_index')::integer END,
        updated_at = now()
    FROM jsonb_array_elements(p_assignments) AS entry(value)
    WHERE item.id = (entry.value ->> 'item_id')::uuid AND item.dataset_id = p_dataset_id;
    GET DIAGNOSTICS v_assigned = ROW_COUNT;
    v_result := aap_assets_v1._spatial_success(jsonb_build_object(
        'operation_id', p_operation_id, 'dataset_id', p_dataset_id,
        'assigned_items', v_assigned
    ));
    RETURN aap_assets_v1._write_spatial_receipt(
        p_operation_id, NULL, 'items.assign', jsonb_build_object('dataset_id', p_dataset_id),
        jsonb_build_object('assignments', p_assignments), v_result
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._spatial_failure('source_busy');
WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN aap_assets_v1._spatial_failure('invalid_request');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.assign_spatial_items(uuid, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.upsert_spatial_poses(
    p_operation_id uuid,
    p_scene_id uuid,
    p_poses jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_receipt jsonb;
    v_pose jsonb;
    v_count integer := 0;
    v_result jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_scene_id IS NULL
       OR jsonb_typeof(p_poses) IS DISTINCT FROM 'array'
       THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_poses) AS input(value)
        WHERE jsonb_typeof(value) IS DISTINCT FROM 'object'
           OR (value - ARRAY['frame_index', 'timestamp_us', 'ego_translation', 'ego_rotation', 'source_metadata']) IS DISTINCT FROM '{}'::jsonb
           OR jsonb_typeof(value -> 'frame_index') IS DISTINCT FROM 'number'
           OR (value ->> 'frame_index')::numeric < 0
           OR (value ->> 'frame_index')::numeric <> trunc((value ->> 'frame_index')::numeric)
           OR jsonb_typeof(value -> 'ego_translation') IS DISTINCT FROM 'array'
           OR jsonb_array_length(value -> 'ego_translation') <> 3
           OR jsonb_typeof(value -> 'ego_rotation') IS DISTINCT FROM 'array'
           OR jsonb_array_length(value -> 'ego_rotation') <> 4
           OR (value ? 'timestamp_us' AND value -> 'timestamp_us' <> 'null'::jsonb
               AND jsonb_typeof(value -> 'timestamp_us') IS DISTINCT FROM 'number')
           OR (value ? 'source_metadata' AND jsonb_typeof(value -> 'source_metadata') IS DISTINCT FROM 'object')
    ) OR (SELECT count(DISTINCT value ->> 'frame_index') FROM jsonb_array_elements(p_poses))
         <> jsonb_array_length(p_poses) THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    v_receipt := aap_assets_v1._read_spatial_receipt(
        p_operation_id, NULL, 'pose.upsert', jsonb_build_object('scene_id', p_scene_id),
        jsonb_build_object('poses', p_poses)
    );
    IF v_receipt IS NOT NULL THEN RETURN v_receipt; END IF;
    PERFORM 1 FROM public.scenes AS scene WHERE scene.id = p_scene_id FOR KEY SHARE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._spatial_failure('scene_missing'); END IF;
    FOR v_pose IN SELECT value FROM jsonb_array_elements(p_poses)
    LOOP
        INSERT INTO public.scene_frame_poses (
            id, scene_id, frame_index, timestamp_us,
            ego_translation, ego_rotation, source_metadata
        ) VALUES (
            pg_catalog.gen_random_uuid(), p_scene_id, (v_pose ->> 'frame_index')::integer,
            CASE WHEN v_pose -> 'timestamp_us' IS NULL OR v_pose -> 'timestamp_us' = 'null'::jsonb
                 THEN NULL ELSE (v_pose ->> 'timestamp_us')::bigint END,
            v_pose -> 'ego_translation', v_pose -> 'ego_rotation',
            COALESCE(v_pose -> 'source_metadata', '{}'::jsonb)
        ) ON CONFLICT ON CONSTRAINT uq_scene_frame_pose DO UPDATE SET
            timestamp_us = excluded.timestamp_us,
            ego_translation = excluded.ego_translation,
            ego_rotation = excluded.ego_rotation,
            source_metadata = excluded.source_metadata;
        v_count := v_count + 1;
    END LOOP;
    v_result := aap_assets_v1._spatial_success(jsonb_build_object(
        'operation_id', p_operation_id, 'scene_id', p_scene_id, 'upserted', v_count
    ));
    RETURN aap_assets_v1._write_spatial_receipt(
        p_operation_id, NULL, 'pose.upsert', jsonb_build_object('scene_id', p_scene_id),
        jsonb_build_object('poses', p_poses), v_result
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._spatial_failure('scene_conflict');
WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN aap_assets_v1._spatial_failure('invalid_request');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.upsert_spatial_poses(uuid, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.apply_spatial_layout(
    p_operation_id uuid,
    p_actor_id uuid,
    p_dataset_id uuid,
    p_mode text,
    p_dry_run boolean,
    p_plan jsonb,
    p_snapshot jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_guard jsonb;
    v_receipt jsonb;
    v_snapshot jsonb;
    v_expected_digest text;
    v_scene jsonb;
    v_assignment jsonb;
    v_item_id uuid;
    v_scene_id uuid;
    v_frame_index integer;
    v_scene_dataset uuid;
    v_result jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_dataset_id IS NULL OR p_mode IS NULL
       OR p_mode NOT IN ('single', 'per_subdirectory', 'auto') OR p_dry_run IS NULL
       OR jsonb_typeof(p_plan) IS DISTINCT FROM 'object'
       OR (p_plan - ARRAY['scenes', 'assignments', 'created_scenes', 'assigned_items',
           'skipped_items', 'dry_run', 'notes']) IS DISTINCT FROM '{}'::jsonb
       OR jsonb_typeof(p_plan -> 'scenes') IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_plan -> 'assignments') IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_plan -> 'notes') IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_plan -> 'dry_run') IS DISTINCT FROM 'boolean'
       OR (p_plan ->> 'dry_run')::boolean IS DISTINCT FROM p_dry_run
       OR jsonb_typeof(p_plan -> 'created_scenes') IS DISTINCT FROM 'number'
       OR (p_plan ->> 'created_scenes')::numeric < 0
       OR (p_plan ->> 'created_scenes')::numeric <> trunc((p_plan ->> 'created_scenes')::numeric)
       OR jsonb_typeof(p_plan -> 'assigned_items') IS DISTINCT FROM 'number'
       OR (p_plan ->> 'assigned_items')::numeric < 0
       OR (p_plan ->> 'assigned_items')::numeric <> trunc((p_plan ->> 'assigned_items')::numeric)
       OR jsonb_typeof(p_plan -> 'skipped_items') IS DISTINCT FROM 'number'
       OR (p_plan ->> 'skipped_items')::numeric < 0
       OR (p_plan ->> 'skipped_items')::numeric <> trunc((p_plan ->> 'skipped_items')::numeric)
       OR EXISTS (SELECT 1 FROM jsonb_array_elements(p_plan -> 'notes') AS note(value)
                  WHERE jsonb_typeof(value) IS DISTINCT FROM 'string')
       OR jsonb_typeof(p_snapshot) IS DISTINCT FROM 'object'
       OR (p_snapshot - ARRAY['snapshot_digest']) IS DISTINCT FROM '{}'::jsonb
       OR jsonb_typeof(p_snapshot -> 'snapshot_digest') IS DISTINCT FROM 'string'
       OR (p_snapshot ->> 'snapshot_digest') !~ '^[0-9a-f]{64}$' THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    IF p_actor_id IS NOT NULL THEN
        v_guard := aap_assets_v1.guard_spatial_actor(p_actor_id, 'manager_write');
        IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_guard; END IF;
    END IF;
    v_receipt := aap_assets_v1._read_spatial_receipt(
        p_operation_id, p_actor_id, 'layout.apply',
        jsonb_build_object('dataset_id', p_dataset_id),
        jsonb_build_object('mode', p_mode, 'dry_run', p_dry_run)
    );
    IF v_receipt IS NOT NULL THEN RETURN v_receipt; END IF;
    BEGIN
        PERFORM dataset.id FROM public.datasets AS dataset
        WHERE dataset.id = p_dataset_id FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._spatial_failure('dataset_busy');
    END;
    IF NOT FOUND THEN RETURN aap_assets_v1._spatial_failure('dataset_missing'); END IF;
    BEGIN
        PERFORM item.id FROM public.dataset_items AS item
        WHERE item.dataset_id = p_dataset_id ORDER BY item.id FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._spatial_failure('source_busy');
    END;
    v_snapshot := aap_assets_v1._spatial_dataset_snapshot(p_dataset_id);
    IF v_snapshot -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_snapshot; END IF;
    v_expected_digest := v_snapshot #>> '{data,snapshot_digest}';
    IF v_expected_digest IS DISTINCT FROM p_snapshot ->> 'snapshot_digest' THEN
        RETURN aap_assets_v1._spatial_failure('source_changed');
    END IF;
    IF jsonb_array_length(p_plan -> 'scenes') <> COALESCE((p_plan ->> 'created_scenes')::integer, -1)
       OR (NOT p_dry_run AND jsonb_array_length(p_plan -> 'assignments')
           <> COALESCE((p_plan ->> 'assigned_items')::integer, -1)) THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_plan -> 'scenes') AS planned(value)
        WHERE jsonb_typeof(value) IS DISTINCT FROM 'object'
           OR (value - ARRAY['id', 'name', 'source_format', 'source_metadata']) IS DISTINCT FROM '{}'::jsonb
           OR jsonb_typeof(value -> 'id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(value -> 'name') IS DISTINCT FROM 'string'
           OR char_length(value ->> 'name') > 255
           OR (value -> 'source_format' IS NOT NULL AND value -> 'source_format' <> 'null'::jsonb
               AND (jsonb_typeof(value -> 'source_format') IS DISTINCT FROM 'string'
                    OR char_length(value ->> 'source_format') > 50))
           OR NOT (value ? 'source_metadata')
           OR jsonb_typeof(value -> 'source_metadata') IS DISTINCT FROM 'object'
    ) OR EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_plan -> 'assignments') AS planned(value)
        WHERE jsonb_typeof(value) IS DISTINCT FROM 'object'
           OR (value - ARRAY['item_id', 'scene_id', 'frame_index']) IS DISTINCT FROM '{}'::jsonb
           OR jsonb_typeof(value -> 'item_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(value -> 'scene_id') IS DISTINCT FROM 'string'
           OR (value ? 'frame_index' AND value -> 'frame_index' <> 'null'::jsonb
               AND ((value ->> 'frame_index')::numeric < 0
                    OR (value ->> 'frame_index')::numeric <> trunc((value ->> 'frame_index')::numeric)))
    ) THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    IF (SELECT count(DISTINCT value ->> 'id')
        FROM jsonb_array_elements(p_plan -> 'scenes') AS planned(value))
       <> jsonb_array_length(p_plan -> 'scenes')
       OR (SELECT count(DISTINCT value ->> 'item_id')
           FROM jsonb_array_elements(p_plan -> 'assignments') AS planned(value))
          <> jsonb_array_length(p_plan -> 'assignments')
       OR EXISTS (
            SELECT 1 FROM jsonb_array_elements(p_plan -> 'assignments') AS planned(value)
            WHERE NOT EXISTS (
                SELECT 1 FROM jsonb_array_elements(p_plan -> 'scenes') AS planned_scene(value)
                WHERE planned_scene.value ->> 'id' = planned.value ->> 'scene_id'
            ) OR NOT EXISTS (
                SELECT 1 FROM public.dataset_items AS item
                WHERE item.id = (planned.value ->> 'item_id')::uuid
                  AND item.dataset_id = p_dataset_id
            )
       ) THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    IF p_dry_run THEN
        v_result := aap_assets_v1._spatial_success(jsonb_build_object(
            'operation_id', p_operation_id, 'actor_id', p_actor_id,
            'dataset_id', p_dataset_id,
            'created_scenes', p_plan -> 'created_scenes',
            'assigned_items', p_plan -> 'assigned_items',
            'skipped_items', p_plan -> 'skipped_items',
            'dry_run', true, 'notes', p_plan -> 'notes'
        ));
        RETURN aap_assets_v1._write_spatial_receipt(
            p_operation_id, p_actor_id, 'layout.apply',
            jsonb_build_object('dataset_id', p_dataset_id),
            jsonb_build_object('mode', p_mode, 'dry_run', p_dry_run), v_result
        );
    END IF;
    FOR v_scene IN SELECT value FROM jsonb_array_elements(p_plan -> 'scenes')
    LOOP
        INSERT INTO public.scenes (
            id, display_id, dataset_id, name, source_format, source_metadata, created_by
        ) VALUES (
            (v_scene ->> 'id')::uuid, 'SCN-' || nextval('public.display_seq_scenes')::text,
            p_dataset_id, v_scene ->> 'name',
            NULLIF(v_scene ->> 'source_format', 'null'),
            COALESCE(v_scene -> 'source_metadata', '{}'::jsonb), p_actor_id
        );
    END LOOP;
    FOR v_assignment IN SELECT value FROM jsonb_array_elements(p_plan -> 'assignments')
    LOOP
        v_item_id := (v_assignment ->> 'item_id')::uuid;
        v_scene_id := (v_assignment ->> 'scene_id')::uuid;
        v_frame_index := CASE WHEN v_assignment -> 'frame_index' IS NULL
                                   OR v_assignment -> 'frame_index' = 'null'::jsonb
                              THEN NULL ELSE (v_assignment ->> 'frame_index')::integer END;
        UPDATE public.dataset_items SET scene_id = v_scene_id,
            frame_index = v_frame_index, updated_at = now()
        WHERE id = v_item_id;
    END LOOP;
    v_result := aap_assets_v1._spatial_success(jsonb_build_object(
        'operation_id', p_operation_id, 'actor_id', p_actor_id,
        'dataset_id', p_dataset_id,
        'created_scenes', p_plan -> 'created_scenes',
        'assigned_items', p_plan -> 'assigned_items',
        'skipped_items', p_plan -> 'skipped_items',
        'dry_run', false, 'notes', p_plan -> 'notes'
    ));
    RETURN aap_assets_v1._write_spatial_receipt(
        p_operation_id, p_actor_id, 'layout.apply',
        jsonb_build_object('dataset_id', p_dataset_id),
        jsonb_build_object('mode', p_mode, 'dry_run', p_dry_run), v_result
    );
EXCEPTION WHEN unique_violation THEN
    RETURN aap_assets_v1._spatial_failure('scene_conflict');
WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._spatial_failure('source_busy');
WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN aap_assets_v1._spatial_failure('invalid_request');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.apply_spatial_layout(uuid, uuid, uuid, text, boolean, jsonb, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._spatial_calibration_valid(p_value jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_key text;
    v_values jsonb;
    v_value jsonb;
BEGIN
    IF p_value IS NULL OR jsonb_typeof(p_value) IS DISTINCT FROM 'object'
       OR (p_value - ARRAY['extrinsic', 'intrinsic', 'rect']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_value ? 'extrinsic') OR NOT (p_value ? 'intrinsic')
       OR NOT (p_value ? 'rect') THEN
        RETURN false;
    END IF;
    FOREACH v_key IN ARRAY ARRAY['extrinsic', 'intrinsic'] LOOP
        v_values := p_value -> v_key;
        IF jsonb_typeof(v_values) IS DISTINCT FROM 'array' THEN
            RETURN false;
        END IF;
        IF jsonb_array_length(v_values) <> (CASE WHEN v_key = 'extrinsic' THEN 16 ELSE 9 END) THEN
            RETURN false;
        END IF;
        FOR v_value IN SELECT value FROM jsonb_array_elements(v_values)
        LOOP
            IF jsonb_typeof(v_value) IS DISTINCT FROM 'number' THEN RETURN false; END IF;
        END LOOP;
    END LOOP;
    v_values := p_value -> 'rect';
    IF v_values <> 'null'::jsonb THEN
        IF jsonb_typeof(v_values) IS DISTINCT FROM 'array' THEN
            RETURN false;
        END IF;
        IF jsonb_array_length(v_values) <> 16 THEN
            RETURN false;
        END IF;
        FOR v_value IN SELECT value FROM jsonb_array_elements(v_values)
        LOOP
            IF jsonb_typeof(v_value) IS DISTINCT FROM 'number' THEN RETURN false; END IF;
        END LOOP;
    END IF;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._spatial_calibration_valid(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._spatial_normalize_calibration(p_raw jsonb)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_extrinsic jsonb;
    v_intrinsic jsonb;
    v_rect jsonb;
    v_result jsonb;
    v_key text;
    v_values jsonb;
    v_entry jsonb;
    v_number double precision;
    v_ordinal bigint;
BEGIN
    IF p_raw IS NULL OR jsonb_typeof(p_raw) IS DISTINCT FROM 'object'
       OR (p_raw - ARRAY['extrinsic', 'intrinsic', 'rect']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_raw ? 'extrinsic') OR NOT (p_raw ? 'intrinsic') THEN
        RETURN NULL;
    END IF;
    FOREACH v_key IN ARRAY ARRAY['extrinsic', 'intrinsic'] LOOP
        v_values := p_raw -> v_key;
        IF jsonb_typeof(v_values) IS DISTINCT FROM 'array'
           OR jsonb_array_length(v_values) <> (CASE WHEN v_key = 'extrinsic' THEN 16 ELSE 9 END) THEN
            RETURN NULL;
        END IF;
        v_result := '[]'::jsonb;
        FOR v_entry, v_ordinal IN
            SELECT value, ordinality
            FROM jsonb_array_elements(v_values) WITH ORDINALITY AS entries(value, ordinality)
            ORDER BY ordinality
        LOOP
            IF jsonb_typeof(v_entry) = 'boolean' THEN
                v_number := CASE WHEN v_entry = 'true'::jsonb THEN 1.0 ELSE 0.0 END;
            ELSIF jsonb_typeof(v_entry) IN ('number', 'string') THEN
                v_number := (v_entry #>> '{}')::double precision;
            ELSE
                RETURN NULL;
            END IF;
            IF v_number::text IN ('NaN', 'Infinity', '-Infinity') THEN RETURN NULL; END IF;
            v_result := v_result || jsonb_build_array(to_jsonb(v_number));
        END LOOP;
        IF v_key = 'extrinsic' THEN
            v_extrinsic := v_result;
        ELSE
            v_intrinsic := v_result;
        END IF;
    END LOOP;
    IF p_raw ? 'rect' THEN
        v_rect := p_raw -> 'rect';
        IF v_rect <> 'null'::jsonb THEN
            IF jsonb_typeof(v_rect) IS DISTINCT FROM 'array'
               OR jsonb_array_length(v_rect) <> 16 THEN
                RETURN NULL;
            END IF;
            v_result := '[]'::jsonb;
            FOR v_entry, v_ordinal IN
                SELECT value, ordinality
                FROM jsonb_array_elements(v_rect) WITH ORDINALITY AS entries(value, ordinality)
                ORDER BY ordinality
            LOOP
                IF jsonb_typeof(v_entry) = 'boolean' THEN
                    v_number := CASE WHEN v_entry = 'true'::jsonb THEN 1.0 ELSE 0.0 END;
                ELSIF jsonb_typeof(v_entry) IN ('number', 'string') THEN
                    v_number := (v_entry #>> '{}')::double precision;
                ELSE
                    RETURN NULL;
                END IF;
                IF v_number::text IN ('NaN', 'Infinity', '-Infinity') THEN RETURN NULL; END IF;
                v_result := v_result || jsonb_build_array(to_jsonb(v_number));
            END LOOP;
            v_rect := v_result;
        END IF;
    ELSE
        v_rect := 'null'::jsonb;
    END IF;
    RETURN jsonb_build_object(
        'extrinsic', v_extrinsic, 'intrinsic', v_intrinsic, 'rect', v_rect
    );
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN NULL;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._spatial_normalize_calibration(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_spatial_camera_target(
    p_actor_id uuid,
    p_task_id uuid,
    p_camera_role text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_target jsonb;
    v_dataset_item_id uuid;
BEGIN
    IF p_actor_id IS NULL OR p_task_id IS NULL OR p_camera_role IS NULL
       OR char_length(p_camera_role) NOT BETWEEN 8 AND 50
       OR left(p_camera_role, 7) <> 'camera_' THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    v_target := aap_labeling_v1.read_spatial_camera_target(p_actor_id, p_task_id, p_camera_role);
    IF v_target ->> 'schema' IS DISTINCT FROM 'aap-labeling-spatial-camera-target/v1'
       OR jsonb_typeof(v_target -> 'ok') IS DISTINCT FROM 'boolean'
       OR (v_target - ARRAY['schema', 'ok', 'data', 'error']) IS DISTINCT FROM '{}'::jsonb THEN
        RETURN aap_assets_v1._spatial_failure('invalid_response');
    END IF;
    IF v_target -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        IF (v_target - ARRAY['schema', 'ok', 'error']) IS DISTINCT FROM '{}'::jsonb
           OR jsonb_typeof(v_target -> 'error') IS DISTINCT FROM 'object'
           OR ((v_target -> 'error') - ARRAY['code']) IS DISTINCT FROM '{}'::jsonb
           OR jsonb_typeof(v_target #> '{error,code}') IS DISTINCT FROM 'string' THEN
            RETURN aap_assets_v1._spatial_failure('invalid_response');
        END IF;
        RETURN aap_assets_v1._spatial_failure(CASE v_target #>> '{error,code}'
            WHEN 'invalid_request' THEN 'invalid_request'
            WHEN 'account_not_active' THEN 'account_not_active'
            WHEN 'account_busy' THEN 'account_busy'
            WHEN 'invalid_account_role' THEN 'invalid_account_role'
            WHEN 'task_missing' THEN 'task_missing'
            WHEN 'task_busy' THEN 'task_busy'
            WHEN 'camera_role_not_linked' THEN 'camera_role_not_linked'
            WHEN 'task_not_visible' THEN 'task_not_visible'
            ELSE 'invalid_response' END);
    END IF;
    IF (v_target - ARRAY['schema', 'ok', 'data']) IS DISTINCT FROM '{}'::jsonb
       OR jsonb_typeof(v_target -> 'data') IS DISTINCT FROM 'object'
       OR ((v_target -> 'data') - ARRAY['actor_id', 'task_id', 'camera_role', 'dataset_item_id']) IS DISTINCT FROM '{}'::jsonb
       OR jsonb_typeof(v_target #> '{data,actor_id}') IS DISTINCT FROM 'string'
       OR jsonb_typeof(v_target #> '{data,task_id}') IS DISTINCT FROM 'string'
       OR jsonb_typeof(v_target #> '{data,camera_role}') IS DISTINCT FROM 'string'
       OR jsonb_typeof(v_target #> '{data,dataset_item_id}') IS DISTINCT FROM 'string'
       OR v_target #>> '{data,actor_id}' IS DISTINCT FROM p_actor_id::text
       OR v_target #>> '{data,task_id}' IS DISTINCT FROM p_task_id::text
       OR v_target #>> '{data,camera_role}' IS DISTINCT FROM p_camera_role THEN
        RETURN aap_assets_v1._spatial_failure('invalid_response');
    END IF;
    BEGIN
        v_dataset_item_id := (v_target #>> '{data,dataset_item_id}')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_assets_v1._spatial_failure('invalid_response');
    END;
    RETURN aap_assets_v1._spatial_success(v_target -> 'data');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_spatial_camera_target(uuid, uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.update_spatial_calibration(
    p_operation_id uuid,
    p_actor_id uuid,
    p_task_id uuid,
    p_camera_role text,
    p_expected_revision integer,
    p_expected_digest text,
    p_calibration jsonb,
    p_canonical_document text,
    p_current_canonical_document text,
    p_source_snapshot jsonb,
    p_audit_context jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item public.dataset_items%ROWTYPE;
    v_authority jsonb;
    v_receipt jsonb;
    v_current jsonb;
    v_normalized_current jsonb;
    v_current_text jsonb;
    v_new_text jsonb;
    v_snapshot jsonb;
    v_current_revision integer;
    v_current_digest text;
    v_current_created_at timestamptz;
    v_new_digest text;
    v_new_revision integer;
    v_new_created_at timestamptz;
    v_has_history boolean := false;
    v_result jsonb;
    v_audit_id bigint;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_task_id IS NULL
       OR p_camera_role IS NULL OR char_length(p_camera_role) NOT BETWEEN 8 AND 50
       OR left(p_camera_role, 7) <> 'camera_'
       OR p_expected_revision IS NULL OR p_expected_revision < 1
       OR p_expected_digest IS NULL OR p_expected_digest !~ '^[0-9a-f]{64}$'
       OR NOT aap_assets_v1._spatial_calibration_valid(p_calibration)
       OR p_canonical_document IS NULL OR p_current_canonical_document IS NULL
       OR jsonb_typeof(p_source_snapshot) IS DISTINCT FROM 'object'
       OR NOT aap_audit_v1.valid_request_context(p_audit_context) THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    BEGIN
        SELECT item.* INTO v_item FROM public.dataset_items AS item
        WHERE item.id = (p_source_snapshot ->> 'dataset_item_id')::uuid
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._spatial_failure('source_busy');
    WHEN invalid_text_representation THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END;
    IF NOT FOUND
       OR v_item.id IS DISTINCT FROM (p_source_snapshot ->> 'dataset_item_id')::uuid THEN
        RETURN aap_assets_v1._spatial_failure('camera_dataset_item_missing');
    END IF;
    v_authority := aap_labeling_v1.lock_spatial_camera_authority(
        p_actor_id, p_task_id, p_camera_role, v_item.id
    );
    IF v_authority ->> 'schema' IS DISTINCT FROM 'aap-labeling-spatial-camera-authority/v1'
       OR jsonb_typeof(v_authority -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._spatial_failure('invalid_response');
    END IF;
    IF v_authority -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._spatial_failure(CASE v_authority #>> '{error,code}'
            WHEN 'account_not_active' THEN 'account_not_active'
            WHEN 'account_busy' THEN 'account_busy'
            WHEN 'task_missing' THEN 'task_missing'
            WHEN 'task_busy' THEN 'task_busy'
            WHEN 'project_missing' THEN 'project_missing'
            WHEN 'project_manager_required' THEN 'project_manager_required'
            WHEN 'camera_role_not_linked' THEN 'camera_role_not_linked'
            WHEN 'camera_shared_across_projects' THEN 'camera_shared_across_projects'
            ELSE 'invalid_response' END);
    END IF;
    v_receipt := aap_assets_v1._read_spatial_receipt(
        p_operation_id, p_actor_id, 'calibration.update',
        jsonb_build_object('dataset_item_id', v_item.id, 'task_id', p_task_id,
            'camera_role', p_camera_role),
        jsonb_build_object('expected_revision', p_expected_revision,
            'expected_digest', p_expected_digest, 'calibration', p_calibration,
            'canonical_document', p_canonical_document)
    );
    IF v_receipt IS NOT NULL THEN RETURN v_receipt; END IF;
    v_snapshot := aap_assets_v1._spatial_item_source_snapshot(v_item.id);
    IF v_snapshot IS DISTINCT FROM p_source_snapshot THEN
        RETURN aap_assets_v1._spatial_failure('source_changed');
    END IF;
    SELECT revision.revision, revision.calibration, revision.digest, revision.created_at
    INTO v_current_revision, v_current, v_current_digest, v_current_created_at
    FROM public.sensor_calibration_revisions AS revision
    WHERE revision.dataset_item_id = v_item.id
    ORDER BY revision.revision DESC LIMIT 1;
    IF FOUND THEN
        v_has_history := true;
    ELSE
        v_current_revision := 1;
        v_current := v_item.metadata -> 'calibration';
        v_current_digest := NULL;
        v_current_created_at := NULL;
    END IF;
    IF v_current IS NULL THEN
        RETURN aap_assets_v1._spatial_failure('camera_calibration_missing');
    END IF;
    v_normalized_current := aap_assets_v1._spatial_normalize_calibration(v_current);
    IF v_normalized_current IS NULL THEN
        RETURN aap_assets_v1._spatial_failure('camera_calibration_invalid');
    END IF;
    BEGIN
        v_current_text := p_current_canonical_document::jsonb;
        v_new_text := p_canonical_document::jsonb;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_assets_v1._spatial_failure('camera_calibration_invalid');
    END;
    IF v_current_text IS DISTINCT FROM v_normalized_current
       OR NOT aap_assets_v1._spatial_calibration_valid(v_new_text)
       OR v_new_text IS DISTINCT FROM p_calibration THEN
        RETURN aap_assets_v1._spatial_failure('camera_calibration_invalid');
    END IF;
    v_new_digest := encode(public.digest(convert_to(p_canonical_document, 'UTF8'), 'sha256'), 'hex');
    IF v_new_digest !~ '^[0-9a-f]{64}$' THEN
        RETURN aap_assets_v1._spatial_failure('camera_calibration_invalid');
    END IF;
    IF v_has_history THEN
        IF v_current_revision IS DISTINCT FROM p_expected_revision
           OR v_current_digest IS DISTINCT FROM p_expected_digest THEN
            RETURN aap_assets_v1._spatial_failure('calibration_revision_conflict');
        END IF;
    ELSE
        v_current_digest := encode(public.digest(
            convert_to(p_current_canonical_document, 'UTF8'), 'sha256'), 'hex');
        IF v_current_revision IS DISTINCT FROM p_expected_revision
           OR v_current_digest IS DISTINCT FROM p_expected_digest THEN
            RETURN aap_assets_v1._spatial_failure('calibration_revision_conflict');
        END IF;
    END IF;
    IF v_new_digest = v_current_digest THEN
        v_new_revision := v_current_revision;
        v_new_digest := v_current_digest;
        v_new_created_at := v_current_created_at;
    ELSE
        IF NOT v_has_history THEN
            INSERT INTO public.sensor_calibration_revisions (
                id, dataset_item_id, revision, digest, calibration, created_by
            ) VALUES (
                pg_catalog.gen_random_uuid(), v_item.id, 1, v_current_digest,
                v_normalized_current, p_actor_id
            );
        END IF;
        v_new_revision := v_current_revision + 1;
        INSERT INTO public.sensor_calibration_revisions (
            id, dataset_item_id, revision, digest, calibration, created_by
        ) VALUES (
            pg_catalog.gen_random_uuid(), v_item.id, v_new_revision, v_new_digest,
            v_new_text, p_actor_id
        ) RETURNING created_at INTO v_new_created_at;
        UPDATE public.dataset_items AS item SET
            metadata = COALESCE(item.metadata, '{}'::jsonb)
                || jsonb_build_object('calibration', v_new_text),
            updated_at = now()
        WHERE item.id = v_item.id;
    END IF;
    v_audit_id := aap_audit_v1.record_sensor_calibration_update(
        p_operation_id, p_actor_id, v_item.id, p_task_id, p_camera_role,
        v_new_revision, v_new_digest, p_audit_context
    );
    IF v_audit_id IS NULL OR v_audit_id < 1 THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Audit sensor calibration event contract mismatch';
    END IF;
    v_snapshot := aap_assets_v1._spatial_item_source_snapshot(v_item.id);
    v_result := aap_assets_v1._spatial_success(jsonb_build_object(
        'operation_id', p_operation_id, 'actor_id', p_actor_id,
        'task_id', p_task_id, 'camera_role', p_camera_role,
        'state', jsonb_build_object(
            'dataset_item_id', v_item.id, 'revision', v_new_revision,
            'calibration', CASE WHEN v_new_digest = v_current_digest THEN v_normalized_current ELSE v_new_text END,
            'digest', v_new_digest, 'created_at', v_new_created_at,
            'source_snapshot', v_snapshot
        )
    ));
    RETURN aap_assets_v1._write_spatial_receipt(
        p_operation_id, p_actor_id, 'calibration.update',
        jsonb_build_object('dataset_item_id', v_item.id, 'task_id', p_task_id,
            'camera_role', p_camera_role),
        jsonb_build_object('expected_revision', p_expected_revision,
            'expected_digest', p_expected_digest, 'calibration', p_calibration,
            'canonical_document', p_canonical_document), v_result
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.update_spatial_calibration(uuid, uuid, uuid, text, integer, text, jsonb, text, text, jsonb, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_pointcloud_preparation(
    p_creation_operation_id uuid,
    p_after_item_id uuid,
    p_page_size integer
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_scope jsonb;
    v_page jsonb;
BEGIN
    IF p_creation_operation_id IS NULL OR p_page_size IS NULL
       OR p_page_size NOT BETWEEN 1 AND 500 THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    v_scope := aap_labeling_v1.read_spatial_preparation_scope(p_creation_operation_id);
    IF v_scope ->> 'schema' IS DISTINCT FROM 'aap-labeling-spatial-preparation/v1'
       OR jsonb_typeof(v_scope -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._spatial_failure('invalid_response');
    END IF;
    IF v_scope -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._spatial_failure(CASE v_scope #>> '{error,code}'
            WHEN 'operation_missing' THEN 'operation_missing'
            WHEN 'operation_cancelled' THEN 'operation_cancelled'
            WHEN 'account_not_active' THEN 'account_not_active'
            WHEN 'account_busy' THEN 'account_busy'
            ELSE 'invalid_response' END);
    END IF;
    v_page := aap_assets_v1.read_spatial_layout(
        NULL, (v_scope #>> '{data,dataset_id}')::uuid,
        p_creation_operation_id, p_after_item_id, p_page_size
    );
    IF v_page -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_page;
    END IF;
    RETURN aap_assets_v1._spatial_success(v_page -> 'data');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_pointcloud_preparation(uuid, uuid, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.apply_pointcloud_preparation(
    p_creation_operation_id uuid,
    p_snapshot jsonb,
    p_calibration_updates jsonb,
    p_frame_plan jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_receipt jsonb;
    v_scope jsonb;
    v_scope_data jsonb;
    v_dataset_id uuid;
    v_creator uuid;
    v_snapshot jsonb;
    v_digest text;
    v_update jsonb;
    v_source_id uuid;
    v_item_id uuid;
    v_source_item public.dataset_items%ROWTYPE;
    v_item public.dataset_items%ROWTYPE;
    v_calibration jsonb;
    v_scene jsonb;
    v_assignment jsonb;
    v_scene_id uuid;
    v_target_scene uuid;
    v_frame integer;
    v_calibrated integer := 0;
    v_created integer := 0;
    v_assigned integer := 0;
    v_result jsonb;
BEGIN
    IF p_creation_operation_id IS NULL
       OR jsonb_typeof(p_snapshot) IS DISTINCT FROM 'object'
       OR (p_snapshot - ARRAY['snapshot_digest']) IS DISTINCT FROM '{}'::jsonb
       OR jsonb_typeof(p_snapshot -> 'snapshot_digest') IS DISTINCT FROM 'string'
       OR (p_snapshot ->> 'snapshot_digest') !~ '^[0-9a-f]{64}$'
       OR jsonb_typeof(p_calibration_updates) IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_frame_plan) IS DISTINCT FROM 'object'
       OR (p_frame_plan - ARRAY['scenes', 'assignments', 'created_scenes',
           'assigned_items', 'skipped_items', 'dry_run', 'notes']) IS DISTINCT FROM '{}'::jsonb
       OR jsonb_typeof(p_frame_plan -> 'scenes') IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_frame_plan -> 'assignments') IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_frame_plan -> 'notes') IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_frame_plan -> 'dry_run') IS DISTINCT FROM 'boolean'
       OR (p_frame_plan ->> 'dry_run')::boolean IS DISTINCT FROM false
       OR jsonb_array_length(p_frame_plan -> 'scenes')
          <> COALESCE((p_frame_plan ->> 'created_scenes')::integer, -1)
       OR jsonb_array_length(p_frame_plan -> 'assignments')
          <> COALESCE((p_frame_plan ->> 'assigned_items')::integer, -1)
       OR COALESCE((p_frame_plan ->> 'skipped_items')::integer, -1) < 0
       OR EXISTS (SELECT 1 FROM jsonb_array_elements(p_frame_plan -> 'notes') AS note(value)
                  WHERE jsonb_typeof(value) IS DISTINCT FROM 'string')
       OR COALESCE((p_frame_plan ->> 'skipped_items')::integer, -1) < 0
       OR EXISTS (SELECT 1 FROM jsonb_array_elements(p_frame_plan -> 'notes') AS note(value)
                  WHERE jsonb_typeof(value) IS DISTINCT FROM 'string')
       OR (SELECT count(DISTINCT value ->> 'dataset_item_id')
           FROM jsonb_array_elements(p_calibration_updates) AS update_entry(value))
          <> jsonb_array_length(p_calibration_updates) THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    v_scope := aap_labeling_v1.lock_spatial_preparation_scope(p_creation_operation_id);
    IF v_scope ->> 'schema' IS DISTINCT FROM 'aap-labeling-spatial-preparation/v1'
       OR jsonb_typeof(v_scope -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._spatial_failure('invalid_response');
    END IF;
    IF v_scope -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._spatial_failure(CASE v_scope #>> '{error,code}'
            WHEN 'operation_missing' THEN 'operation_missing'
            WHEN 'operation_cancelled' THEN 'operation_cancelled'
            WHEN 'association_changed' THEN 'association_changed'
            WHEN 'job_cancelled' THEN 'operation_cancelled'
            WHEN 'job_not_active' THEN 'operation_cancelled'
            WHEN 'source_busy' THEN 'source_busy'
            WHEN 'dataset_busy' THEN 'dataset_busy'
            ELSE 'invalid_response' END);
    END IF;
    v_scope_data := v_scope -> 'data';
    v_dataset_id := (v_scope_data ->> 'dataset_id')::uuid;
    v_creator := NULLIF(v_scope_data ->> 'actor_id', '')::uuid;
    v_receipt := aap_assets_v1._read_spatial_receipt(
        p_creation_operation_id, NULL, 'pointcloud.prepare',
        jsonb_build_object('creation_operation_id', p_creation_operation_id),
        jsonb_build_object('creation_operation_id', p_creation_operation_id)
    );
    IF v_receipt IS NOT NULL THEN RETURN v_receipt; END IF;
    BEGIN
        PERFORM item.id FROM public.dataset_items AS item
        WHERE item.dataset_id = v_dataset_id ORDER BY item.id FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._spatial_failure('source_busy');
    END;
    v_snapshot := aap_assets_v1._spatial_dataset_snapshot(v_dataset_id);
    IF v_snapshot -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_snapshot; END IF;
    v_digest := v_snapshot #>> '{data,snapshot_digest}';
    IF v_digest IS DISTINCT FROM p_snapshot ->> 'snapshot_digest' THEN
        RETURN aap_assets_v1._spatial_failure('source_changed');
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_frame_plan -> 'scenes') AS planned(value)
        WHERE jsonb_typeof(value) IS DISTINCT FROM 'object'
           OR (value - ARRAY['id', 'name', 'source_format', 'source_metadata']) IS DISTINCT FROM '{}'::jsonb
           OR jsonb_typeof(value -> 'id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(value -> 'name') IS DISTINCT FROM 'string'
           OR char_length(value ->> 'name') > 255
           OR NOT (value ? 'source_metadata')
           OR jsonb_typeof(value -> 'source_metadata') IS DISTINCT FROM 'object'
           OR (value -> 'source_format' IS NOT NULL AND value -> 'source_format' <> 'null'::jsonb
               AND (jsonb_typeof(value -> 'source_format') IS DISTINCT FROM 'string'
                    OR char_length(value ->> 'source_format') > 50))
    ) OR EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_frame_plan -> 'assignments') AS planned(value)
        WHERE jsonb_typeof(value) IS DISTINCT FROM 'object'
           OR (value - ARRAY['item_id', 'scene_id', 'frame_index']) IS DISTINCT FROM '{}'::jsonb
           OR jsonb_typeof(value -> 'item_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(value -> 'scene_id') IS DISTINCT FROM 'string'
           OR (value ? 'frame_index' AND value -> 'frame_index' <> 'null'::jsonb
               AND ((value ->> 'frame_index')::numeric < 0
                    OR (value ->> 'frame_index')::numeric <> trunc((value ->> 'frame_index')::numeric)))
    ) OR (SELECT count(DISTINCT value ->> 'id')
          FROM jsonb_array_elements(p_frame_plan -> 'scenes') AS planned(value))
         <> jsonb_array_length(p_frame_plan -> 'scenes')
      OR (SELECT count(DISTINCT value ->> 'item_id')
          FROM jsonb_array_elements(p_frame_plan -> 'assignments') AS planned(value))
         <> jsonb_array_length(p_frame_plan -> 'assignments')
      OR EXISTS (
          SELECT 1 FROM jsonb_array_elements(p_frame_plan -> 'assignments') AS planned(value)
          WHERE NOT EXISTS (
              SELECT 1 FROM jsonb_array_elements(p_frame_plan -> 'scenes') AS planned_scene(scene)
              WHERE planned_scene.scene ->> 'id' = planned.value ->> 'scene_id'
          ) OR NOT EXISTS (
              SELECT 1 FROM public.dataset_items AS item
              WHERE item.id = (planned.value ->> 'item_id')::uuid
                AND item.dataset_id = v_dataset_id
          )
      ) THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    FOR v_update IN SELECT value FROM jsonb_array_elements(p_calibration_updates)
    LOOP
        IF jsonb_typeof(v_update) IS DISTINCT FROM 'object'
           OR (v_update - ARRAY['dataset_item_id', 'source_item_id', 'source_snapshot',
                'source_etag', 'source_size', 'calibration']) IS DISTINCT FROM '{}'::jsonb
           OR jsonb_typeof(v_update -> 'source_snapshot') IS DISTINCT FROM 'object'
           OR jsonb_typeof(v_update -> 'source_etag') IS DISTINCT FROM 'string'
           OR jsonb_typeof(v_update -> 'source_size') IS DISTINCT FROM 'number'
           OR (v_update ->> 'source_size')::numeric < 0
           OR (v_update ->> 'source_size')::numeric <> trunc((v_update ->> 'source_size')::numeric)
           OR NOT aap_assets_v1._spatial_calibration_valid(v_update -> 'calibration') THEN
            RETURN aap_assets_v1._spatial_failure('invalid_request');
        END IF;
        v_source_id := (v_update ->> 'source_item_id')::uuid;
        v_item_id := (v_update ->> 'dataset_item_id')::uuid;
        SELECT item.* INTO v_source_item FROM public.dataset_items AS item
        WHERE item.id = v_source_id AND item.dataset_id = v_dataset_id;
        IF NOT FOUND OR (v_source_item.file_size IS NOT NULL
            AND v_source_item.file_size IS DISTINCT FROM (v_update ->> 'source_size')::bigint)
           OR aap_assets_v1._spatial_item_source_snapshot(v_source_id)
              IS DISTINCT FROM v_update -> 'source_snapshot' THEN
            RETURN aap_assets_v1._spatial_failure('source_changed');
        END IF;
        SELECT item.* INTO v_item FROM public.dataset_items AS item
        WHERE item.id = v_item_id AND item.dataset_id = v_dataset_id;
        IF NOT FOUND THEN RETURN aap_assets_v1._spatial_failure('camera_dataset_item_missing'); END IF;
        IF EXISTS (SELECT 1 FROM public.sensor_calibration_revisions AS revision
                   WHERE revision.dataset_item_id = v_item_id) THEN
            RETURN aap_assets_v1._spatial_failure('calibration_revision_conflict');
        END IF;
    END LOOP;
    FOR v_update IN SELECT value FROM jsonb_array_elements(p_calibration_updates)
    LOOP
        v_item_id := (v_update ->> 'dataset_item_id')::uuid;
        v_calibration := v_update -> 'calibration';
        UPDATE public.dataset_items AS item SET
            metadata = COALESCE(item.metadata, '{}'::jsonb)
                || jsonb_build_object('calibration', jsonb_strip_nulls(v_calibration)),
            updated_at = now()
        WHERE item.id = v_item_id AND item.dataset_id = v_dataset_id;
        v_calibrated := v_calibrated + 1;
    END LOOP;
    FOR v_scene IN SELECT value FROM jsonb_array_elements(p_frame_plan -> 'scenes')
    LOOP
        INSERT INTO public.scenes (
            id, display_id, dataset_id, name, source_format, source_metadata, created_by
        ) VALUES (
            (v_scene ->> 'id')::uuid, 'SCN-' || nextval('public.display_seq_scenes')::text,
            v_dataset_id, v_scene ->> 'name', NULLIF(v_scene ->> 'source_format', 'null'),
            v_scene -> 'source_metadata', v_creator
        );
        v_created := v_created + 1;
    END LOOP;
    FOR v_assignment IN SELECT value FROM jsonb_array_elements(p_frame_plan -> 'assignments')
    LOOP
        v_item_id := (v_assignment ->> 'item_id')::uuid;
        v_target_scene := (v_assignment ->> 'scene_id')::uuid;
        v_frame := CASE WHEN v_assignment -> 'frame_index' IS NULL
                              OR v_assignment -> 'frame_index' = 'null'::jsonb
                        THEN NULL ELSE (v_assignment ->> 'frame_index')::integer END;
        UPDATE public.dataset_items SET scene_id = v_target_scene,
            frame_index = v_frame, updated_at = now() WHERE id = v_item_id;
        v_assigned := v_assigned + 1;
    END LOOP;
    v_result := aap_assets_v1._spatial_success(jsonb_build_object(
        'creation_operation_id', p_creation_operation_id,
        'dataset_id', v_dataset_id, 'calibrated_items', v_calibrated,
        'created_scenes', v_created, 'assigned_items', v_assigned
    ));
    RETURN aap_assets_v1._write_spatial_receipt(
        p_creation_operation_id, NULL, 'pointcloud.prepare',
        jsonb_build_object('creation_operation_id', p_creation_operation_id),
        jsonb_build_object('creation_operation_id', p_creation_operation_id), v_result
    );
EXCEPTION WHEN unique_violation THEN
    RETURN aap_assets_v1._spatial_failure('scene_conflict');
WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._spatial_failure('source_busy');
WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN aap_assets_v1._spatial_failure('invalid_request');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.apply_pointcloud_preparation(uuid, jsonb, jsonb, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.record_spatial_calibration_provenance(
    p_operation_id uuid,
    p_dataset_id uuid,
    p_entries jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_identity jsonb;
    v_receipt jsonb;
    v_entry jsonb;
    v_item public.dataset_items%ROWTYPE;
    v_item_id uuid;
    v_document jsonb;
    v_current_calibration jsonb;
    v_digest text;
    v_source jsonb;
    v_recorded integer := 0;
    v_result jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_dataset_id IS NULL
       OR jsonb_typeof(p_entries) IS DISTINCT FROM 'array'
       THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    IF EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_entries) AS input(value)
        WHERE jsonb_typeof(value) IS DISTINCT FROM 'object'
           OR (value - ARRAY['dataset_item_id', 'calibration', 'digest', 'canonical_document', 'source_snapshot']) IS DISTINCT FROM '{}'::jsonb
           OR jsonb_typeof(value -> 'dataset_item_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(value -> 'source_snapshot') IS DISTINCT FROM 'object'
           OR jsonb_typeof(value -> 'digest') IS DISTINCT FROM 'string'
           OR (value ->> 'digest') !~ '^[0-9a-f]{64}$'
           OR jsonb_typeof(value -> 'canonical_document') IS DISTINCT FROM 'string'
           OR NOT aap_assets_v1._spatial_calibration_valid(value -> 'calibration')
    ) OR (SELECT count(DISTINCT value ->> 'dataset_item_id') FROM jsonb_array_elements(p_entries))
         <> jsonb_array_length(p_entries) THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'dataset_item_id', value -> 'dataset_item_id',
        'calibration', value -> 'calibration',
        'digest', value -> 'digest',
        'canonical_document', value -> 'canonical_document'
    ) ORDER BY value ->> 'dataset_item_id'), '[]'::jsonb)
    INTO v_identity FROM jsonb_array_elements(p_entries) AS entries(value);
    v_receipt := aap_assets_v1._read_spatial_receipt(
        p_operation_id, NULL, 'calibration.provenance',
        jsonb_build_object('dataset_id', p_dataset_id),
        jsonb_build_object('entries', v_identity)
    );
    IF v_receipt IS NOT NULL THEN RETURN v_receipt; END IF;
    FOR v_entry IN SELECT value FROM jsonb_array_elements(p_entries)
        ORDER BY (value ->> 'dataset_item_id')::uuid
    LOOP
        v_item_id := (v_entry ->> 'dataset_item_id')::uuid;
        SELECT item.* INTO v_item FROM public.dataset_items AS item
        WHERE item.id = v_item_id AND item.dataset_id = p_dataset_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN RETURN aap_assets_v1._spatial_failure('source_missing'); END IF;
        IF aap_assets_v1._spatial_item_source_snapshot(v_item_id)
           IS DISTINCT FROM v_entry -> 'source_snapshot' THEN
            RETURN aap_assets_v1._spatial_failure('source_changed');
        END IF;
        v_document := (v_entry ->> 'canonical_document')::jsonb;
        v_digest := encode(public.digest(
            convert_to(v_entry ->> 'canonical_document', 'UTF8'), 'sha256'), 'hex');
        IF v_digest IS DISTINCT FROM v_entry ->> 'digest'
           OR v_document IS DISTINCT FROM v_entry -> 'calibration' THEN
            RETURN aap_assets_v1._spatial_failure('camera_calibration_invalid');
        END IF;
        SELECT COALESCE(latest.calibration, v_item.metadata -> 'calibration')
        INTO v_current_calibration
        FROM (SELECT 1) AS singleton
        LEFT JOIN LATERAL (
            SELECT revision.calibration
            FROM public.sensor_calibration_revisions AS revision
            WHERE revision.dataset_item_id = v_item_id
            ORDER BY revision.revision DESC LIMIT 1
        ) AS latest ON true;
        IF v_current_calibration IS NULL THEN
            RETURN aap_assets_v1._spatial_failure('camera_calibration_missing');
        END IF;
        v_current_calibration := aap_assets_v1._spatial_normalize_calibration(v_current_calibration);
        IF v_current_calibration IS NULL THEN
            RETURN aap_assets_v1._spatial_failure('camera_calibration_invalid');
        END IF;
        IF v_current_calibration IS DISTINCT FROM v_document THEN
            RETURN aap_assets_v1._spatial_failure('source_changed');
        END IF;
    END LOOP;
    FOR v_entry IN SELECT value FROM jsonb_array_elements(p_entries)
        ORDER BY (value ->> 'dataset_item_id')::uuid
    LOOP
        v_item_id := (v_entry ->> 'dataset_item_id')::uuid;
        SELECT item.* INTO v_item FROM public.dataset_items AS item
        WHERE item.id = v_item_id AND item.dataset_id = p_dataset_id;
        v_digest := v_entry ->> 'digest';
        IF v_item.file_type <> 'image'
           OR jsonb_typeof(v_item.metadata -> 'nuscenes_export') IS DISTINCT FROM 'object'
           OR (v_item.metadata -> 'nuscenes_export') ? 'platform_calibration_digest' THEN
            CONTINUE;
        END IF;
        v_source := (v_item.metadata -> 'nuscenes_export')
            || jsonb_build_object('platform_calibration_digest', v_digest);
        UPDATE public.dataset_items AS item SET
            metadata = COALESCE(item.metadata, '{}'::jsonb)
                || jsonb_build_object('nuscenes_export', v_source),
            updated_at = now()
        WHERE item.id = v_item_id;
        v_recorded := v_recorded + 1;
    END LOOP;
    v_result := aap_assets_v1._spatial_success(jsonb_build_object(
        'operation_id', p_operation_id, 'dataset_id', p_dataset_id,
        'recorded_items', v_recorded
    ));
    RETURN aap_assets_v1._write_spatial_receipt(
        p_operation_id, NULL, 'calibration.provenance',
        jsonb_build_object('dataset_id', p_dataset_id),
        jsonb_build_object('entries', v_identity), v_result
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._spatial_failure('source_busy');
WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN aap_assets_v1._spatial_failure('camera_calibration_invalid');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.record_spatial_calibration_provenance(uuid, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.set_spatial_dataset_axis(
    p_operation_id uuid,
    p_dataset_id uuid,
    p_axis_convention text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_receipt jsonb;
    v_dataset public.datasets%ROWTYPE;
    v_result jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_dataset_id IS NULL
       OR (p_axis_convention IS NOT NULL AND p_axis_convention NOT IN (
           'iso_8855', 'ros_rep103', 'kitti_camera', 'opencv_camera',
           'apollo', 'y_forward', 'sustechpoints_demo', 'raw'
       )) THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    v_receipt := aap_assets_v1._read_spatial_receipt(
        p_operation_id, NULL, 'dataset.axis.set',
        jsonb_build_object('dataset_id', p_dataset_id),
        jsonb_build_object('axis_convention', p_axis_convention)
    );
    IF v_receipt IS NOT NULL THEN RETURN v_receipt; END IF;
    BEGIN
        SELECT dataset.* INTO v_dataset
        FROM public.datasets AS dataset
        WHERE dataset.id = p_dataset_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._spatial_failure('dataset_busy');
    END;
    IF NOT FOUND THEN RETURN aap_assets_v1._spatial_failure('dataset_missing'); END IF;
    IF pg_catalog.jsonb_typeof(v_dataset.metadata) IS DISTINCT FROM 'object' THEN
        RETURN aap_assets_v1._spatial_failure('invalid_response');
    END IF;

    IF p_axis_convention IS NULL THEN
        IF v_dataset.metadata ? 'axis_convention' THEN
            UPDATE public.datasets AS dataset SET
                metadata = COALESCE(dataset.metadata, '{}'::jsonb) - 'axis_convention',
                updated_at = now()
            WHERE dataset.id = p_dataset_id;
        END IF;
    ELSIF jsonb_typeof(v_dataset.metadata -> 'axis_convention') IS DISTINCT FROM 'string'
       OR v_dataset.metadata ->> 'axis_convention' IS DISTINCT FROM p_axis_convention THEN
        UPDATE public.datasets AS dataset SET
            metadata = COALESCE(dataset.metadata, '{}'::jsonb)
                || jsonb_build_object('axis_convention', p_axis_convention),
            updated_at = now()
        WHERE dataset.id = p_dataset_id;
    END IF;

    v_result := aap_assets_v1._spatial_success(jsonb_build_object(
        'operation_id', p_operation_id,
        'dataset_id', p_dataset_id,
        'axis_convention', p_axis_convention
    ));
    RETURN aap_assets_v1._write_spatial_receipt(
        p_operation_id, NULL, 'dataset.axis.set',
        jsonb_build_object('dataset_id', p_dataset_id),
        jsonb_build_object('axis_convention', p_axis_convention), v_result
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.set_spatial_dataset_axis(uuid, uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_spatial_layout_receipt(
    p_operation_id uuid,
    p_actor_id uuid,
    p_dataset_id uuid,
    p_mode text,
    p_dry_run boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_guard jsonb;
    v_receipt jsonb;
    v_layout_result jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_dataset_id IS NULL OR p_mode IS NULL
       OR p_mode NOT IN ('single', 'per_subdirectory', 'auto')
       OR p_dry_run IS NULL THEN
        RETURN aap_assets_v1._spatial_failure('invalid_request');
    END IF;
    IF p_actor_id IS NOT NULL THEN
        v_guard := aap_assets_v1.guard_spatial_actor(p_actor_id, 'manager_write');
        IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_guard; END IF;
    END IF;
    v_receipt := aap_assets_v1._read_spatial_receipt(
        p_operation_id, p_actor_id, 'layout.apply',
        jsonb_build_object('dataset_id', p_dataset_id),
        jsonb_build_object('mode', p_mode, 'dry_run', p_dry_run)
    );
    IF v_receipt IS NOT NULL THEN
        IF v_receipt -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN v_receipt;
        END IF;
        v_layout_result := v_receipt -> 'data';
        IF v_receipt ->> 'schema' IS DISTINCT FROM 'aap-assets-spatial/v1'
           OR (v_receipt - ARRAY['schema', 'ok', 'data']) IS DISTINCT FROM '{}'::jsonb
           OR pg_catalog.jsonb_typeof(v_layout_result) IS DISTINCT FROM 'object'
           OR (v_layout_result - ARRAY[
                'operation_id', 'actor_id', 'dataset_id', 'created_scenes',
                'assigned_items', 'skipped_items', 'dry_run', 'notes'
           ]) IS DISTINCT FROM '{}'::jsonb
           OR v_layout_result ->> 'operation_id' IS DISTINCT FROM p_operation_id::text
           OR v_layout_result ->> 'dataset_id' IS DISTINCT FROM p_dataset_id::text
           OR (v_layout_result -> 'actor_id') IS DISTINCT FROM
              COALESCE(to_jsonb(p_actor_id), 'null'::jsonb)
           OR (v_layout_result -> 'dry_run') IS DISTINCT FROM to_jsonb(p_dry_run)
           OR pg_catalog.jsonb_typeof(v_layout_result -> 'created_scenes') IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(v_layout_result -> 'assigned_items') IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(v_layout_result -> 'skipped_items') IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(v_layout_result -> 'notes') IS DISTINCT FROM 'array'
           OR EXISTS (
                SELECT 1 FROM pg_catalog.jsonb_array_elements(v_layout_result -> 'notes') AS note(value)
                WHERE pg_catalog.jsonb_typeof(value) IS DISTINCT FROM 'string'
           ) THEN
            RETURN aap_assets_v1._spatial_failure('invalid_response');
        END IF;
        IF (v_layout_result ->> 'created_scenes')::numeric < 0
           OR (v_layout_result ->> 'created_scenes')::numeric <> pg_catalog.trunc((v_layout_result ->> 'created_scenes')::numeric)
           OR (v_layout_result ->> 'assigned_items')::numeric < 0
           OR (v_layout_result ->> 'assigned_items')::numeric <> pg_catalog.trunc((v_layout_result ->> 'assigned_items')::numeric)
           OR (v_layout_result ->> 'skipped_items')::numeric < 0
           OR (v_layout_result ->> 'skipped_items')::numeric <> pg_catalog.trunc((v_layout_result ->> 'skipped_items')::numeric) THEN
            RETURN aap_assets_v1._spatial_failure('invalid_response');
        END IF;
    END IF;
    RETURN aap_assets_v1._spatial_success(pg_catalog.jsonb_build_object(
        'operation_id', p_operation_id,
        'actor_id', p_actor_id,
        'dataset_id', p_dataset_id,
        'mode', p_mode,
        'dry_run', p_dry_run,
        'result', v_layout_result
    ));
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_spatial_layout_receipt(uuid, uuid, uuid, text, boolean) FROM PUBLIC;
