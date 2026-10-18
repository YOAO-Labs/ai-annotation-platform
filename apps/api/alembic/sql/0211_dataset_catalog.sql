-- Assets catalog queries and writes are versioned capabilities; the LOGIN
-- caller receives no direct table or sequence privileges.
CREATE TABLE public.dataset_catalog_operations (
    operation_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    operation_kind varchar(16) NOT NULL
        CHECK (operation_kind IN ('create', 'update')),
    dataset_id uuid NOT NULL,
    request jsonb NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now()
);
-- aap-statement-break
COMMENT ON TABLE public.dataset_catalog_operations IS
    'Assets-owned immutable create/update replay receipts without live Dataset or User foreign keys.';
-- aap-statement-break
REVOKE ALL ON TABLE public.dataset_catalog_operations FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._catalog_failure(p_code text)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-catalog/v1',
        'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._catalog_failure(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._catalog_actor(
    p_actor_id uuid,
    p_write boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_code text;
    v_role text;
BEGIN
    IF p_actor_id IS NULL OR p_write IS NULL THEN
        RETURN aap_assets_v1._catalog_failure('invalid_request');
    END IF;

    v_actor := aap_identity_v1.lock_active_actor(p_actor_id);
    IF v_actor ->> 'schema' IS DISTINCT FROM 'aap-active-actor/v1'
       OR pg_catalog.jsonb_typeof(v_actor -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'Assets catalog identity contract mismatch';
    END IF;
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_actor #>> '{error,code}';
        IF v_code IN ('account_not_active', 'account_busy') THEN
            RETURN aap_assets_v1._catalog_failure(v_code);
        END IF;
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'Assets catalog identity failure contract mismatch';
    END IF;

    IF pg_catalog.jsonb_typeof(v_actor -> 'data') IS DISTINCT FROM 'object'
       OR ((v_actor -> 'data') - ARRAY['actor_id', 'platform_role']) <> '{}'::jsonb
       OR pg_catalog.jsonb_typeof(v_actor #> '{data,actor_id}') IS DISTINCT FROM 'string'
       OR v_actor #>> '{data,actor_id}' IS DISTINCT FROM p_actor_id::text
       OR pg_catalog.jsonb_typeof(v_actor #> '{data,platform_role}') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'Assets catalog identity facts mismatch';
    END IF;

    v_role := v_actor #>> '{data,platform_role}';
    IF p_write THEN
        IF v_role IS NULL OR v_role NOT IN (
            'super_admin', 'project_admin', 'employee', 'viewer'
        ) THEN
            RETURN aap_assets_v1._catalog_failure('invalid_account_role');
        END IF;
        IF v_role NOT IN ('super_admin', 'project_admin') THEN
            RETURN aap_assets_v1._catalog_failure('catalog_manager_required');
        END IF;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-catalog-actor/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'platform_role', v_role
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._catalog_actor(uuid, boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._catalog_dataset_summary(p_dataset_id uuid)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', dataset.id,
        'display_id', dataset.display_id,
        'name', dataset.name,
        'description', dataset.description,
        'data_type', dataset.data_type,
        'is_temporal', dataset.is_temporal,
        'has_scenes', EXISTS (
            SELECT 1 FROM public.scenes AS scene
            WHERE scene.dataset_id = dataset.id
        ),
        'file_count', dataset.file_count,
        'total_size', COALESCE((
            SELECT pg_catalog.sum(item.file_size)
            FROM public.dataset_items AS item
            WHERE item.dataset_id = dataset.id
        ), 0)::bigint,
        'created_by', dataset.created_by,
        'project_count', (
            SELECT pg_catalog.count(*)
            FROM public.project_datasets AS association
            WHERE association.dataset_id = dataset.id
        ),
        'axis_convention', COALESCE(dataset.metadata, '{}'::jsonb) -> 'axis_convention',
        'created_at', dataset.created_at,
        'updated_at', dataset.updated_at
    )
    FROM public.datasets AS dataset
    WHERE dataset.id = p_dataset_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._catalog_dataset_summary(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.list_catalog_datasets(
    p_actor_id uuid,
    p_search text,
    p_data_type text,
    p_has_scenes boolean,
    p_limit integer,
    p_offset bigint
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
    v_result jsonb;
BEGIN
    IF p_actor_id IS NULL
       OR p_limit IS NULL OR p_limit < 1 OR p_limit > 200
       OR p_offset IS NULL OR p_offset < 0
       OR (p_data_type IS NOT NULL AND pg_catalog.char_length(p_data_type) > 30) THEN
        RETURN aap_assets_v1._catalog_failure('invalid_request');
    END IF;

    v_actor := aap_assets_v1._catalog_actor(p_actor_id, false);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;

    WITH filtered AS MATERIALIZED (
        SELECT
            dataset.id,
            dataset.display_id,
            dataset.name,
            dataset.description,
            dataset.data_type,
            dataset.is_temporal,
            dataset.file_count,
            dataset.created_by,
            dataset.metadata,
            dataset.created_at,
            dataset.updated_at
        FROM public.datasets AS dataset
        WHERE (p_search IS NULL OR p_search = '' OR
               dataset.name ILIKE ('%' || p_search || '%'))
          AND (
              p_data_type IS NULL
              OR p_data_type = ''
              OR CASE
                  WHEN p_data_type IN ('lidar', 'point_cloud', 'pointcloud')
                      THEN dataset.data_type IN ('lidar', 'point_cloud', 'pointcloud')
                  ELSE dataset.data_type = p_data_type
              END
          )
          AND (
              p_has_scenes IS NULL
              OR EXISTS (
                  SELECT 1 FROM public.scenes AS scene
                  WHERE scene.dataset_id = dataset.id
              ) = p_has_scenes
          )
    ),
    page AS MATERIALIZED (
        SELECT
            filtered.id,
            filtered.display_id,
            filtered.name,
            filtered.description,
            filtered.data_type,
            filtered.is_temporal,
            filtered.file_count,
            filtered.created_by,
            filtered.metadata,
            filtered.created_at,
            filtered.updated_at
        FROM filtered
        ORDER BY filtered.created_at DESC, filtered.id DESC
        LIMIT p_limit OFFSET p_offset
    ),
    summarized AS (
        SELECT
            page.id,
            page.display_id,
            page.name,
            page.description,
            page.data_type,
            page.is_temporal,
            page.file_count,
            page.created_by,
            page.metadata,
            page.created_at,
            page.updated_at,
            EXISTS (
                SELECT 1 FROM public.scenes AS scene
                WHERE scene.dataset_id = page.id
            ) AS has_scenes,
            COALESCE((
                SELECT pg_catalog.sum(item.file_size)
                FROM public.dataset_items AS item
                WHERE item.dataset_id = page.id
            ), 0)::bigint AS total_size,
            (
                SELECT pg_catalog.count(*)
                FROM public.project_datasets AS association
                WHERE association.dataset_id = page.id
            ) AS project_count
        FROM page
    )
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-catalog/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'items', COALESCE(
                pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_object(
                        'id', summarized.id,
                        'display_id', summarized.display_id,
                        'name', summarized.name,
                        'description', summarized.description,
                        'data_type', summarized.data_type,
                        'is_temporal', summarized.is_temporal,
                        'has_scenes', summarized.has_scenes,
                        'file_count', summarized.file_count,
                        'total_size', summarized.total_size,
                        'created_by', summarized.created_by,
                        'project_count', summarized.project_count,
                        'axis_convention', COALESCE(
                            summarized.metadata -> 'axis_convention', 'null'::jsonb
                        ),
                        'created_at', summarized.created_at,
                        'updated_at', summarized.updated_at
                    ) ORDER BY summarized.created_at DESC, summarized.id DESC
                ),
                '[]'::jsonb
            ),
            'total', (SELECT pg_catalog.count(*) FROM filtered),
            'limit', p_limit,
            'offset', p_offset
        )
    )
    INTO v_result
    FROM summarized;

    RETURN v_result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.list_catalog_datasets(
    uuid, text, text, boolean, integer, bigint
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.get_catalog_dataset(
    p_actor_id uuid,
    p_dataset_id uuid
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
    v_dataset jsonb;
BEGIN
    IF p_actor_id IS NULL OR p_dataset_id IS NULL THEN
        RETURN aap_assets_v1._catalog_failure('invalid_request');
    END IF;

    v_actor := aap_assets_v1._catalog_actor(p_actor_id, false);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;

    v_dataset := aap_assets_v1._catalog_dataset_summary(p_dataset_id);
    IF v_dataset IS NULL THEN
        RETURN aap_assets_v1._catalog_failure('dataset_missing');
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-catalog/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object('dataset', v_dataset)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.get_catalog_dataset(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.list_catalog_items(
    p_actor_id uuid,
    p_dataset_id uuid,
    p_limit integer,
    p_offset bigint
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
    v_result jsonb;
BEGIN
    IF p_actor_id IS NULL OR p_dataset_id IS NULL
       OR p_limit IS NULL OR p_limit < 1 OR p_limit > 200
       OR p_offset IS NULL OR p_offset < 0 THEN
        RETURN aap_assets_v1._catalog_failure('invalid_request');
    END IF;

    v_actor := aap_assets_v1._catalog_actor(p_actor_id, false);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;

    WITH dataset_ref AS MATERIALIZED (
        SELECT dataset.id
        FROM public.datasets AS dataset
        WHERE dataset.id = p_dataset_id
    ),
    item_total AS (
        SELECT pg_catalog.count(*) AS total
        FROM public.dataset_items AS item
        WHERE item.dataset_id = p_dataset_id
    ),
    page AS MATERIALIZED (
        SELECT
            item.id,
            item.dataset_id,
            item.file_name,
            item.file_path,
            item.file_type,
            item.file_size,
            item.content_hash,
            item.width,
            item.height,
            item.metadata,
            item.thumbnail_path,
            item.blurhash,
            item.created_at
        FROM public.dataset_items AS item
        WHERE item.dataset_id = p_dataset_id
        ORDER BY item.created_at DESC, item.id DESC
        LIMIT p_limit OFFSET p_offset
    )
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-catalog/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'dataset_id', dataset_ref.id,
            'items', COALESCE((
                SELECT pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_object(
                        'id', item.id,
                        'dataset_id', item.dataset_id,
                        'file_name', item.file_name,
                        'file_path', item.file_path,
                        'file_type', item.file_type,
                        'file_size', item.file_size,
                        'content_hash', item.content_hash,
                        'width', item.width,
                        'height', item.height,
                        'metadata', COALESCE(item.metadata, '{}'::jsonb),
                        'file_url', NULL,
                        'thumbnail_path', item.thumbnail_path,
                        'thumbnail_url', NULL,
                        'blurhash', item.blurhash,
                        'created_at', item.created_at
                    ) ORDER BY item.created_at DESC, item.id DESC
                )
                FROM page AS item
            ), '[]'::jsonb),
            'total', item_total.total,
            'limit', p_limit,
            'offset', p_offset
        )
    )
    INTO v_result
    FROM dataset_ref
    CROSS JOIN item_total;

    IF v_result IS NULL THEN
        RETURN aap_assets_v1._catalog_failure('dataset_missing');
    END IF;
    RETURN v_result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.list_catalog_items(
    uuid, uuid, integer, bigint
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.create_catalog_dataset(
    p_operation_id uuid,
    p_actor_id uuid,
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
    v_actor jsonb;
    v_saved public.dataset_catalog_operations%ROWTYPE;
    v_dataset_id uuid;
    v_display_id text;
    v_name text;
    v_description text;
    v_data_type text;
    v_is_temporal boolean;
    v_axis_convention text;
    v_metadata jsonb;
    v_dataset jsonb;
    v_result jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR NOT (p_request ?& ARRAY[
            'name', 'description', 'data_type', 'is_temporal', 'axis_convention'
       ])
       OR (p_request - ARRAY[
            'name', 'description', 'data_type', 'is_temporal', 'axis_convention'
       ]) <> '{}'::jsonb
       OR pg_catalog.jsonb_typeof(p_request -> 'name') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request -> 'description') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request -> 'data_type') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request -> 'is_temporal') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(p_request -> 'axis_convention') NOT IN ('string', 'null') THEN
        RETURN aap_assets_v1._catalog_failure('invalid_request');
    END IF;

    v_name := p_request ->> 'name';
    v_description := p_request ->> 'description';
    v_data_type := p_request ->> 'data_type';
    v_is_temporal := (p_request ->> 'is_temporal')::boolean;
    v_axis_convention := p_request ->> 'axis_convention';
    IF pg_catalog.char_length(v_name) > 255
       OR pg_catalog.char_length(v_data_type) > 30 THEN
        RETURN aap_assets_v1._catalog_failure('invalid_request');
    END IF;
    IF v_axis_convention IS NOT NULL AND v_axis_convention NOT IN (
        'iso_8855', 'ros_rep103', 'kitti_camera', 'opencv_camera',
        'apollo', 'y_forward', 'sustechpoints_demo', 'raw'
    ) THEN
        RETURN aap_assets_v1._catalog_failure('invalid_request');
    END IF;
    IF pg_catalog.left(
        pg_catalog.split_part(pg_catalog.btrim(v_name, '/'), '/', 1),
        pg_catalog.char_length('__aap_trusted_nuscenes__')
    ) = '__aap_trusted_nuscenes__' THEN
        RETURN aap_assets_v1._catalog_failure('reserved_namespace');
    END IF;

    v_actor := aap_assets_v1._catalog_actor(p_actor_id, true);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;

    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-assets-catalog:' || p_operation_id::text, 0)
    ) THEN
        RETURN aap_assets_v1._catalog_failure('source_busy');
    END IF;
    SELECT operation.* INTO v_saved
    FROM public.dataset_catalog_operations AS operation
    WHERE operation.operation_id = p_operation_id;
    IF FOUND THEN
        IF v_saved.actor_id IS DISTINCT FROM p_actor_id
           OR v_saved.operation_kind IS DISTINCT FROM 'create'
           OR v_saved.request IS DISTINCT FROM p_request THEN
            RETURN aap_assets_v1._catalog_failure('operation_conflict');
        END IF;
        RETURN v_saved.result;
    END IF;

    v_dataset_id := pg_catalog.gen_random_uuid();
    v_display_id := 'D-' || pg_catalog.nextval(
        'public.display_seq_datasets'::pg_catalog.regclass
    )::text;
    v_metadata := CASE
        WHEN v_axis_convention IS NULL THEN '{}'::jsonb
        ELSE pg_catalog.jsonb_build_object('axis_convention', v_axis_convention)
    END;
    INSERT INTO public.datasets (
        id, display_id, name, description, data_type, is_temporal,
        file_count, created_by, metadata
    ) VALUES (
        v_dataset_id, v_display_id, v_name, v_description, v_data_type,
        v_is_temporal, 0, p_actor_id, v_metadata
    );

    v_dataset := aap_assets_v1._catalog_dataset_summary(v_dataset_id);
    IF v_dataset IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'Assets catalog create lost its inserted dataset';
    END IF;
    v_result := pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-catalog/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'operation_id', p_operation_id,
            'actor_id', p_actor_id,
            'dataset', v_dataset
        )
    );
    INSERT INTO public.dataset_catalog_operations (
        operation_id, actor_id, operation_kind, dataset_id, request, result
    ) VALUES (
        p_operation_id, p_actor_id, 'create', v_dataset_id, p_request, v_result
    );
    RETURN v_result;
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._catalog_failure('source_busy');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.create_catalog_dataset(uuid, uuid, jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.update_catalog_dataset(
    p_operation_id uuid,
    p_actor_id uuid,
    p_dataset_id uuid,
    p_patch jsonb
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
    v_saved public.dataset_catalog_operations%ROWTYPE;
    v_old_name text;
    v_old_description text;
    v_old_metadata jsonb;
    v_name text;
    v_description text;
    v_metadata jsonb;
    v_changed boolean := false;
    v_dataset_result jsonb;
    v_request jsonb;
    v_result jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_dataset_id IS NULL
       OR p_patch IS NULL
       OR pg_catalog.jsonb_typeof(p_patch) IS DISTINCT FROM 'object'
       OR (p_patch - ARRAY['name', 'description', 'axis_convention']) <> '{}'::jsonb
       OR (p_patch ? 'name' AND pg_catalog.jsonb_typeof(p_patch -> 'name') NOT IN ('string', 'null'))
       OR (p_patch ? 'description' AND pg_catalog.jsonb_typeof(p_patch -> 'description') NOT IN ('string', 'null'))
       OR (p_patch ? 'axis_convention' AND pg_catalog.jsonb_typeof(p_patch -> 'axis_convention') NOT IN ('string', 'null')) THEN
        RETURN aap_assets_v1._catalog_failure('invalid_request');
    END IF;

    IF pg_catalog.jsonb_typeof(p_patch -> 'name') = 'string'
       AND pg_catalog.char_length(p_patch ->> 'name') > 255 THEN
        RETURN aap_assets_v1._catalog_failure('invalid_request');
    END IF;
    IF pg_catalog.jsonb_typeof(p_patch -> 'axis_convention') = 'string'
       AND p_patch ->> 'axis_convention' NOT IN (
           'iso_8855', 'ros_rep103', 'kitti_camera', 'opencv_camera',
           'apollo', 'y_forward', 'sustechpoints_demo', 'raw'
       ) THEN
        RETURN aap_assets_v1._catalog_failure('invalid_request');
    END IF;
    IF pg_catalog.jsonb_typeof(p_patch -> 'name') = 'string'
       AND pg_catalog.left(
           pg_catalog.split_part(pg_catalog.btrim(p_patch ->> 'name', '/'), '/', 1),
           pg_catalog.char_length('__aap_trusted_nuscenes__')
       ) = '__aap_trusted_nuscenes__' THEN
        RETURN aap_assets_v1._catalog_failure('reserved_namespace');
    END IF;

    v_actor := aap_assets_v1._catalog_actor(p_actor_id, true);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;

    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-assets-catalog:' || p_operation_id::text, 0)
    ) THEN
        RETURN aap_assets_v1._catalog_failure('source_busy');
    END IF;
    v_request := pg_catalog.jsonb_build_object('dataset_id', p_dataset_id, 'patch', p_patch);
    SELECT operation.* INTO v_saved
    FROM public.dataset_catalog_operations AS operation
    WHERE operation.operation_id = p_operation_id;
    IF FOUND THEN
        IF v_saved.actor_id IS DISTINCT FROM p_actor_id
           OR v_saved.operation_kind IS DISTINCT FROM 'update'
           OR v_saved.dataset_id IS DISTINCT FROM p_dataset_id
           OR v_saved.request IS DISTINCT FROM v_request THEN
            RETURN aap_assets_v1._catalog_failure('operation_conflict');
        END IF;
        RETURN v_saved.result;
    END IF;

    BEGIN
        SELECT dataset.name, dataset.description, dataset.metadata
        INTO v_old_name, v_old_description, v_old_metadata
        FROM public.datasets AS dataset
        WHERE dataset.id = p_dataset_id
        FOR UPDATE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._catalog_failure('source_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._catalog_failure('dataset_missing');
    END IF;

    v_name := v_old_name;
    IF pg_catalog.jsonb_typeof(p_patch -> 'name') = 'string' THEN
        v_name := p_patch ->> 'name';
    END IF;
    v_description := v_old_description;
    IF pg_catalog.jsonb_typeof(p_patch -> 'description') = 'string' THEN
        v_description := p_patch ->> 'description';
    END IF;
    v_metadata := COALESCE(v_old_metadata, '{}'::jsonb);
    IF p_patch ? 'axis_convention' THEN
        IF pg_catalog.jsonb_typeof(p_patch -> 'axis_convention') = 'null' THEN
            v_metadata := v_metadata - 'axis_convention';
        ELSE
            v_metadata := v_metadata || pg_catalog.jsonb_build_object(
                'axis_convention', p_patch ->> 'axis_convention'
            );
        END IF;
    END IF;
    v_changed := v_name IS DISTINCT FROM v_old_name
        OR v_description IS DISTINCT FROM v_old_description
        OR v_metadata IS DISTINCT FROM COALESCE(v_old_metadata, '{}'::jsonb);
    IF v_changed THEN
        UPDATE public.datasets AS dataset
        SET name = v_name,
            description = v_description,
            metadata = v_metadata,
            updated_at = pg_catalog.now()
        WHERE dataset.id = p_dataset_id;
    END IF;

    v_dataset_result := aap_assets_v1._catalog_dataset_summary(p_dataset_id);
    IF v_dataset_result IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'Assets catalog update lost its locked dataset';
    END IF;
    v_result := pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-catalog/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'operation_id', p_operation_id,
            'actor_id', p_actor_id,
            'dataset', v_dataset_result
        )
    );
    INSERT INTO public.dataset_catalog_operations (
        operation_id, actor_id, operation_kind, dataset_id, request, result
    ) VALUES (
        p_operation_id, p_actor_id, 'update', p_dataset_id, v_request, v_result
    );
    RETURN v_result;
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._catalog_failure('source_busy');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.update_catalog_dataset(uuid, uuid, uuid, jsonb)
    FROM PUBLIC;
