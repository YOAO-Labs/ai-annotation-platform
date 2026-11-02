-- Decode the validated role reply once, preserving exact item/scene snapshots.
CREATE OR REPLACE FUNCTION aap_assets_v1._spatial_item_ids_snapshot(p_item_ids uuid[])
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
    role_items AS MATERIALIZED (
        SELECT role_item.value ->> 'item_id' AS item_id,
               role_item.value -> 'roles' AS roles
        FROM pg_catalog.jsonb_array_elements(v_roles #> '{data,items}') AS role_item(value)
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
                    role_item.roles
                )
            ) AS row_snapshot
        FROM requested
        JOIN public.dataset_items AS item ON item.id = requested.id
        JOIN public.datasets AS dataset ON dataset.id = item.dataset_id
        LEFT JOIN role_items AS role_item ON role_item.item_id = item.id::text
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
DROP FUNCTION aap_assets_v1._spatial_roles_for_item(jsonb, uuid);
