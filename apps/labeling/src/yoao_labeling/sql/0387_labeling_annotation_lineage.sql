CREATE OR REPLACE FUNCTION aap_labeling_v1.annotation_bulk_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_keys text[];
    v_ids uuid[] := ARRAY[]::uuid[];
    v_task_ids uuid[];
    v_track_ids uuid[] := ARRAY[]::uuid[];
    v_expected_task_id uuid;
    v_expected_project_id uuid;
    v_task_project_id uuid;
    v_project_row text;
    v_item jsonb;
    v_missing_text text;
    v_found_count bigint;
    v_task_count bigint;
    v_track_count bigint;
    v_selected_count bigint;
    v_active_count bigint;
    v_annotation record;
    v_pair record;
    v_track_id uuid;
    v_binding jsonb;
    v_allowed_count bigint;
    v_camera record;
    v_reply_annotations jsonb := '[]'::jsonb;
    v_reply_cameras jsonb := '[]'::jsonb;
    v_reply_tracks jsonb := '[]'::jsonb;
    v_reply_row jsonb;
    v_numeric_value numeric;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-annotation-bulk/v1'
       OR p_request ->> 'operation' IS DISTINCT FROM 'update' THEN
        RAISE EXCEPTION 'Invalid Labeling Annotation bulk request'
            USING ERRCODE = '22023';
    END IF;
    SELECT array_agg(key ORDER BY key) INTO v_keys
    FROM jsonb_object_keys(p_request) AS request_keys(key);
    IF v_keys IS DISTINCT FROM ARRAY[
        'actor_id', 'ids', 'operation', 'patch', 'project_id', 'schema', 'task_id'
    ]::text[] THEN
        RAISE EXCEPTION 'Invalid Labeling Annotation bulk request fields'
            USING ERRCODE = '22023';
    END IF;

    IF jsonb_typeof(p_request -> 'actor_id') NOT IN ('string', 'null')
       OR jsonb_typeof(p_request -> 'task_id') NOT IN ('string', 'null')
       OR jsonb_typeof(p_request -> 'project_id') NOT IN ('string', 'null')
       OR jsonb_typeof(p_request -> 'ids') IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_request -> 'patch') IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'Invalid Labeling Annotation bulk identity or patch'
            USING ERRCODE = '22023';
    END IF;
    IF (jsonb_typeof(p_request -> 'task_id') = 'null')
       IS DISTINCT FROM (jsonb_typeof(p_request -> 'project_id') = 'null') THEN
        RAISE EXCEPTION 'Invalid Labeling Annotation bulk target'
            USING ERRCODE = '22023';
    END IF;

    IF jsonb_typeof(p_request -> 'actor_id') = 'string' THEN
        IF p_request ->> 'actor_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RAISE EXCEPTION 'Invalid Labeling Annotation bulk actor'
                USING ERRCODE = '22023';
        END IF;
    END IF;
    IF jsonb_typeof(p_request -> 'task_id') = 'string' THEN
        IF p_request ->> 'task_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR p_request ->> 'project_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RAISE EXCEPTION 'Invalid Labeling Annotation bulk target'
                USING ERRCODE = '22023';
        END IF;
        v_expected_task_id := (p_request ->> 'task_id')::uuid;
        v_expected_project_id := (p_request ->> 'project_id')::uuid;
    END IF;

    SELECT array_agg(key ORDER BY key) INTO v_keys
    FROM jsonb_object_keys(p_request -> 'patch') AS patch_keys(key);
    IF EXISTS (
        SELECT 1
        FROM unnest(COALESCE(v_keys, ARRAY[]::text[])) AS patch_key(key)
        WHERE patch_key.key NOT IN (
            'class_name', 'attributes', 'z_order', 'is_locked', 'is_hidden'
        )
    ) THEN
        RAISE EXCEPTION 'Invalid Labeling Annotation bulk patch fields'
            USING ERRCODE = '22023';
    END IF;
    IF (p_request -> 'patch') ? 'class_name'
       AND jsonb_typeof(p_request #> '{patch,class_name}') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Labeling Annotation bulk class name'
            USING ERRCODE = '22023';
    END IF;
    IF (p_request -> 'patch') ? 'attributes'
       AND jsonb_typeof(p_request #> '{patch,attributes}') IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'Invalid Labeling Annotation bulk attributes'
            USING ERRCODE = '22023';
    END IF;
    IF (p_request -> 'patch') ? 'z_order' THEN
        IF jsonb_typeof(p_request #> '{patch,z_order}') IS DISTINCT FROM 'number'
           OR p_request #>> '{patch,z_order}' !~ '^-?(0|[1-9][0-9]*)$' THEN
            RAISE EXCEPTION 'Invalid Labeling Annotation bulk z-order'
                USING ERRCODE = '22023';
        END IF;
        v_numeric_value := (p_request #>> '{patch,z_order}')::numeric;
        IF v_numeric_value < -2147483648 OR v_numeric_value > 2147483647 THEN
            RAISE EXCEPTION 'Invalid Labeling Annotation bulk z-order'
                USING ERRCODE = '22023';
        END IF;
    END IF;
    FOREACH v_item IN ARRAY ARRAY[
        p_request #> '{patch,is_locked}', p_request #> '{patch,is_hidden}'
    ]::jsonb[] LOOP
        IF v_item IS NOT NULL AND jsonb_typeof(v_item) IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION 'Invalid Labeling Annotation bulk status flag'
                USING ERRCODE = '22023';
        END IF;
    END LOOP;

    FOR v_item IN
        SELECT value
        FROM jsonb_array_elements(p_request -> 'ids') AS requested(value)
    LOOP
        IF jsonb_typeof(v_item) IS DISTINCT FROM 'string'
           OR v_item #>> '{}' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RAISE EXCEPTION 'Invalid Labeling Annotation bulk identifier'
                USING ERRCODE = '22023';
        END IF;
        v_ids := array_append(v_ids, (v_item #>> '{}')::uuid);
    END LOOP;
    IF cardinality(v_ids) = 0 THEN
        RAISE EXCEPTION 'Labeling Annotation bulk request requires identifiers'
            USING ERRCODE = '22023';
    END IF;

    SELECT count(DISTINCT annotation.id),
           array_agg(DISTINCT annotation.task_id ORDER BY annotation.task_id)
    INTO v_found_count, v_task_ids
    FROM public.annotations AS annotation
    WHERE annotation.id = ANY(v_ids);
    IF v_found_count <> cardinality(v_ids) THEN
        SELECT string_agg(quote_literal(missing.id::text), ', ' ORDER BY missing.id)
        INTO v_missing_text
        FROM (
            SELECT DISTINCT requested.id
            FROM unnest(v_ids) AS requested(id)
            LEFT JOIN public.annotations AS annotation ON annotation.id = requested.id
            WHERE annotation.id IS NULL
        ) AS missing;
        RAISE EXCEPTION USING ERRCODE = 'P0001',
            MESSAGE = 'aap_annotation_bulk_error',
            DETAIL = jsonb_build_object(
                'status_code', 404,
                'detail', 'annotations not found: [' || COALESCE(v_missing_text, '') || ']'
            )::text;
    END IF;
    IF v_expected_task_id IS NOT NULL
       AND (cardinality(v_task_ids) <> 1 OR v_task_ids[1] IS DISTINCT FROM v_expected_task_id) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001',
            MESSAGE = 'aap_annotation_bulk_error',
            DETAIL = jsonb_build_object(
                'status_code', 409,
                'detail', jsonb_build_object('reason', 'annotation_target_changed')
            )::text;
    END IF;

    PERFORM task.id
    FROM public.tasks AS task
    WHERE task.id = ANY(v_task_ids)
    ORDER BY task.id
    FOR UPDATE OF task;
    SELECT count(*) INTO v_task_count
    FROM public.tasks AS task
    WHERE task.id = ANY(v_task_ids);
    IF v_task_count <> cardinality(v_task_ids) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001',
            MESSAGE = 'aap_annotation_bulk_error',
            DETAIL = jsonb_build_object('status_code', 404, 'detail', 'Task not found')::text;
    END IF;
    IF v_expected_task_id IS NOT NULL THEN
        SELECT task.project_id INTO v_task_project_id
        FROM public.tasks AS task
        WHERE task.id = v_expected_task_id;
        IF v_task_project_id IS DISTINCT FROM v_expected_project_id THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_annotation_bulk_error',
                DETAIL = jsonb_build_object(
                    'status_code', 409,
                    'detail', jsonb_build_object('reason', 'task_target_changed')
                )::text;
        END IF;
    END IF;

    PERFORM annotation.id
    FROM public.annotations AS annotation
    WHERE annotation.id = ANY(v_ids)
    ORDER BY annotation.id
    FOR UPDATE OF annotation;
    SELECT count(DISTINCT annotation.id)
    INTO v_found_count
    FROM public.annotations AS annotation
    WHERE annotation.id = ANY(v_ids);
    IF v_found_count <> cardinality(v_ids) THEN
        SELECT string_agg(quote_literal(missing.id::text), ', ' ORDER BY missing.id)
        INTO v_missing_text
        FROM (
            SELECT DISTINCT requested.id
            FROM unnest(v_ids) AS requested(id)
            LEFT JOIN public.annotations AS annotation ON annotation.id = requested.id
            WHERE annotation.id IS NULL
        ) AS missing;
        RAISE EXCEPTION USING ERRCODE = 'P0001',
            MESSAGE = 'aap_annotation_bulk_error',
            DETAIL = jsonb_build_object(
                'status_code', 404,
                'detail', 'annotations not found: [' || COALESCE(v_missing_text, '') || ']'
            )::text;
    END IF;

    FOR v_annotation IN
        SELECT annotation.id, annotation.task_id, annotation.project_id,
               annotation.tool_unit_id, annotation.sensor_role, annotation.is_active,
               annotation.is_locked, annotation.scene_track_id,
               annotation.was_cancelled
        FROM public.annotations AS annotation
        WHERE annotation.id = ANY(v_ids)
        ORDER BY annotation.id
        FOR UPDATE OF annotation
    LOOP
        IF v_annotation.task_id <> ALL(v_task_ids)
           OR (v_expected_task_id IS NOT NULL
               AND v_annotation.task_id IS DISTINCT FROM v_expected_task_id)
           OR (v_expected_project_id IS NOT NULL
               AND v_annotation.project_id IS NOT NULL
               AND v_annotation.project_id IS DISTINCT FROM v_expected_project_id) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_annotation_bulk_error',
                DETAIL = jsonb_build_object(
                    'status_code', 409,
                    'detail', jsonb_build_object('reason', 'annotation_target_changed')
                )::text;
        END IF;
        IF v_annotation.sensor_role IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_annotation_bulk_error',
                DETAIL = jsonb_build_object(
                    'status_code', 422,
                    'detail', 'camera members must be edited through the point-cloud camera member API'
                )::text;
        END IF;
        IF v_annotation.is_active IS NOT TRUE THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_annotation_bulk_error',
                DETAIL = jsonb_build_object(
                    'status_code', 422,
                    'detail', format('annotation %s is not active', v_annotation.id)
                )::text;
        END IF;
        IF v_annotation.is_locked
           AND ((p_request -> 'patch') ? 'class_name'
                OR (p_request -> 'patch') ? 'attributes'
                OR (p_request -> 'patch') ? 'z_order') THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_annotation_bulk_error',
                DETAIL = jsonb_build_object(
                    'status_code', 422,
                    'detail', format('annotation %s is locked; unlock it first', v_annotation.id)
                )::text;
        END IF;
    END LOOP;

    IF (p_request -> 'patch') ? 'class_name'
       AND p_request #>> '{patch,class_name}' IS DISTINCT FROM '__unknown' THEN
        FOR v_pair IN
            SELECT DISTINCT annotation.project_id, annotation.tool_unit_id
            FROM public.annotations AS annotation
            WHERE annotation.id = ANY(v_ids)
            ORDER BY annotation.project_id, annotation.tool_unit_id
        LOOP
            IF v_pair.project_id IS NULL THEN
                CONTINUE;
            END IF;
            v_project_row := aap_workspace_v1.read_project_row(v_pair.project_id);
            IF v_project_row IS NULL THEN
                CONTINUE;
            END IF;
            v_binding := v_project_row::jsonb -> 'tool_bindings' -> v_pair.tool_unit_id;
            IF jsonb_typeof(v_binding) IS DISTINCT FROM 'object'
               OR v_binding -> 'enabled' IS DISTINCT FROM 'true'::jsonb
               OR jsonb_typeof(v_binding -> 'classes') IS DISTINCT FROM 'array' THEN
                CONTINUE;
            END IF;
            SELECT count(*) INTO v_allowed_count
            FROM jsonb_array_elements(v_binding -> 'classes') AS entry(value)
            WHERE jsonb_typeof(entry.value -> 'name') = 'string'
              AND entry.value ->> 'name' <> '';
            IF v_allowed_count > 0 AND NOT EXISTS (
                SELECT 1
                FROM jsonb_array_elements(v_binding -> 'classes') AS entry(value)
                WHERE jsonb_typeof(entry.value -> 'name') = 'string'
                  AND entry.value ->> 'name' <> ''
                  AND entry.value ->> 'name' = p_request #>> '{patch,class_name}'
            ) THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001',
                    MESSAGE = 'aap_annotation_bulk_error',
                    DETAIL = jsonb_build_object(
                        'status_code', 422,
                        'detail', format(
                            'class_name ''%s'' 不在工具单位 ''%s'' 的类别集合内',
                            p_request #>> '{patch,class_name}', v_pair.tool_unit_id
                        )
                    )::text;
            END IF;
        END LOOP;
    END IF;

    SELECT COALESCE(
        array_agg(DISTINCT annotation.scene_track_id ORDER BY annotation.scene_track_id),
        ARRAY[]::uuid[]
    ) INTO v_track_ids
    FROM public.annotations AS annotation
    WHERE annotation.id = ANY(v_ids)
      AND annotation.scene_track_id IS NOT NULL;
    IF cardinality(v_track_ids) > 0 THEN
        PERFORM track.id
        FROM public.scene_tracks AS track
        WHERE track.id = ANY(v_track_ids)
        ORDER BY track.id
        FOR UPDATE OF track;
        SELECT count(*) INTO v_track_count
        FROM public.scene_tracks AS track
        WHERE track.id = ANY(v_track_ids);
        IF v_track_count <> cardinality(v_track_ids) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_annotation_bulk_error',
                DETAIL = jsonb_build_object(
                    'status_code', 409,
                    'detail', jsonb_build_object(
                        'reason', 'track_missing',
                        'message', 'one or more annotations reference a missing Scene Track'
                    )
                )::text;
        END IF;

        IF (p_request -> 'patch') ? 'class_name' THEN
            FOREACH v_track_id IN ARRAY v_track_ids LOOP
                SELECT count(*) INTO v_selected_count
                FROM public.annotations AS annotation
                WHERE annotation.id = ANY(v_ids)
                  AND annotation.scene_track_id = v_track_id;
                SELECT count(*) INTO v_active_count
                FROM public.annotations AS annotation
                WHERE annotation.scene_track_id = v_track_id
                  AND annotation.is_active IS TRUE
                  AND annotation.was_cancelled IS FALSE
                  AND annotation.sensor_role IS NULL;
                IF v_active_count <> v_selected_count THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001',
                        MESSAGE = 'aap_annotation_bulk_error',
                        DETAIL = jsonb_build_object(
                            'status_code', 409,
                            'detail', jsonb_build_object(
                                'reason', 'track_class_change_requires_scope',
                                'message', 'select every active Scene Track member to change its class'
                            )
                        )::text;
                END IF;
            END LOOP;

            PERFORM camera.id
            FROM public.annotations AS camera
            WHERE camera.scene_track_id = ANY(v_track_ids)
              AND camera.sensor_role IS NOT NULL
              AND camera.was_cancelled IS FALSE
            ORDER BY camera.id
            FOR UPDATE OF camera;

            FOR v_camera IN
                SELECT camera.id
                FROM public.annotations AS camera
                WHERE camera.scene_track_id = ANY(v_track_ids)
                  AND camera.sensor_role IS NOT NULL
                  AND camera.was_cancelled IS FALSE
                ORDER BY camera.id
            LOOP
                UPDATE public.annotations AS camera
                SET class_name = p_request #>> '{patch,class_name}',
                    version = COALESCE(NULLIF(camera.version, 0), 1) + 1,
                    updated_at = transaction_timestamp()
                WHERE camera.id = v_camera.id
                RETURNING jsonb_build_object(
                    'id', camera.id,
                    'class_name', camera.class_name,
                    'attributes', camera.attributes,
                    'attributes_meta', camera.attributes_meta,
                    'z_order', camera.z_order,
                    'is_locked', camera.is_locked,
                    'is_hidden', camera.is_hidden,
                    'version', camera.version,
                    'updated_at', camera.updated_at
                ) INTO v_reply_row;
                v_reply_cameras := v_reply_cameras || jsonb_build_array(v_reply_row);
            END LOOP;
        END IF;
    END IF;

    FOR v_annotation IN
        SELECT annotation.id
        FROM public.annotations AS annotation
        WHERE annotation.id = ANY(v_ids)
        ORDER BY annotation.id
    LOOP
        UPDATE public.annotations AS annotation
        SET class_name = CASE WHEN (p_request -> 'patch') ? 'class_name'
                THEN p_request #>> '{patch,class_name}' ELSE annotation.class_name END,
            attributes = CASE WHEN (p_request -> 'patch') ? 'attributes'
                THEN p_request #> '{patch,attributes}' ELSE annotation.attributes END,
            attributes_meta = CASE WHEN (p_request -> 'patch') ? 'attributes' THEN
                (CASE WHEN annotation.attributes_meta ? '_video_prediction'
                    THEN jsonb_build_object(
                        '_video_prediction', annotation.attributes_meta -> '_video_prediction'
                    ) ELSE '{}'::jsonb END) ||
                COALESCE((
                    SELECT jsonb_object_agg(new_attribute.key, annotation.attributes_meta -> new_attribute.key)
                    FROM jsonb_each(p_request #> '{patch,attributes}') AS new_attribute(key, value)
                    WHERE left(new_attribute.key, 1) <> '_'
                      AND annotation.attributes_meta ? new_attribute.key
                      AND annotation.attributes_meta -> new_attribute.key IS DISTINCT FROM 'null'::jsonb
                      AND annotation.attributes ? new_attribute.key
                      AND (
                          WITH RECURSIVE json_values(old_value, new_value) AS (
                              SELECT annotation.attributes -> new_attribute.key,
                                     new_attribute.value
                              UNION ALL
                              SELECT child.old_value, child.new_value
                              FROM json_values AS parent
                              CROSS JOIN LATERAL (
                                  SELECT old_object.value AS old_value,
                                         new_object.value AS new_value
                                  FROM jsonb_each(CASE
                                      WHEN jsonb_typeof(parent.old_value) = 'object'
                                      THEN parent.old_value ELSE '{}'::jsonb END
                                  ) AS old_object(key, value)
                                  JOIN jsonb_each(CASE
                                      WHEN jsonb_typeof(parent.new_value) = 'object'
                                      THEN parent.new_value ELSE '{}'::jsonb END
                                  ) AS new_object(key, value) USING (key)
                                  WHERE jsonb_typeof(parent.old_value) = 'object'
                                    AND jsonb_typeof(parent.new_value) = 'object'
                                  UNION ALL
                                  SELECT old_array.value AS old_value,
                                         new_array.value AS new_value
                                  FROM jsonb_array_elements(CASE
                                      WHEN jsonb_typeof(parent.old_value) = 'array'
                                      THEN parent.old_value ELSE '[]'::jsonb END
                                  ) WITH ORDINALITY AS old_array(value, ordinal)
                                  JOIN jsonb_array_elements(CASE
                                      WHEN jsonb_typeof(parent.new_value) = 'array'
                                      THEN parent.new_value ELSE '[]'::jsonb END
                                  ) WITH ORDINALITY AS new_array(value, ordinal)
                                      USING (ordinal)
                                  WHERE jsonb_typeof(parent.old_value) = 'array'
                                    AND jsonb_typeof(parent.new_value) = 'array'
                              ) AS child
                          )
                          SELECT bool_and(CASE
                              WHEN jsonb_typeof(old_value) = 'object'
                               AND jsonb_typeof(new_value) = 'object' THEN
                                  old_value - ARRAY(SELECT jsonb_object_keys(new_value))
                                      = '{}'::jsonb
                                  AND new_value - ARRAY(SELECT jsonb_object_keys(old_value))
                                      = '{}'::jsonb
                              WHEN jsonb_typeof(old_value) = 'array'
                               AND jsonb_typeof(new_value) = 'array' THEN
                                  jsonb_array_length(old_value) = jsonb_array_length(new_value)
                              WHEN jsonb_typeof(old_value) = 'boolean'
                               AND jsonb_typeof(new_value) = 'number' THEN
                                  new_value = CASE WHEN old_value = 'true'::jsonb
                                      THEN '1'::jsonb ELSE '0'::jsonb END
                              WHEN jsonb_typeof(old_value) = 'number'
                               AND jsonb_typeof(new_value) = 'boolean' THEN
                                  old_value = CASE WHEN new_value = 'true'::jsonb
                                      THEN '1'::jsonb ELSE '0'::jsonb END
                              WHEN jsonb_typeof(old_value) = jsonb_typeof(new_value) THEN
                                  old_value = new_value
                              ELSE false
                          END)
                          FROM json_values
                      )
                ), '{}'::jsonb)
                ELSE annotation.attributes_meta END,
            z_order = CASE WHEN (p_request -> 'patch') ? 'z_order'
                THEN (p_request #>> '{patch,z_order}')::integer ELSE annotation.z_order END,
            is_locked = CASE WHEN (p_request -> 'patch') ? 'is_locked'
                THEN (p_request #>> '{patch,is_locked}')::boolean ELSE annotation.is_locked END,
            is_hidden = CASE WHEN (p_request -> 'patch') ? 'is_hidden'
                THEN (p_request #>> '{patch,is_hidden}')::boolean ELSE annotation.is_hidden END,
            version = annotation.version + 1,
            updated_at = transaction_timestamp()
        WHERE annotation.id = v_annotation.id
        RETURNING jsonb_build_object(
            'id', annotation.id,
            'class_name', annotation.class_name,
            'attributes', annotation.attributes,
            'attributes_meta', annotation.attributes_meta,
            'z_order', annotation.z_order,
            'is_locked', annotation.is_locked,
            'is_hidden', annotation.is_hidden,
            'version', annotation.version,
            'updated_at', annotation.updated_at
        ) INTO v_reply_row;
        v_reply_annotations := v_reply_annotations || jsonb_build_array(v_reply_row);
    END LOOP;

    IF cardinality(v_track_ids) > 0 THEN
        FOREACH v_track_id IN ARRAY v_track_ids LOOP
            UPDATE public.scene_tracks AS track
            SET class_name = CASE WHEN (p_request -> 'patch') ? 'class_name'
                    THEN p_request #>> '{patch,class_name}' ELSE track.class_name END,
                revision = COALESCE(NULLIF(track.revision, 0), 1) + 1,
                updated_at = transaction_timestamp()
            WHERE track.id = v_track_id
            RETURNING jsonb_build_object(
                'id', track.id,
                'class_name', track.class_name,
                'revision', track.revision,
                'updated_at', track.updated_at
            ) INTO v_reply_row;
            v_reply_tracks := v_reply_tracks || jsonb_build_array(v_reply_row);
        END LOOP;
    END IF;

    RETURN jsonb_build_object(
        'schema', 'aap-labeling-annotation-bulk/v1',
        'ok', true,
        'request', p_request,
        'data', jsonb_build_object(
            'annotations', v_reply_annotations,
            'camera_members', v_reply_cameras,
            'tracks', v_reply_tracks
        )
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.annotation_bulk_command(jsonb) FROM PUBLIC;
