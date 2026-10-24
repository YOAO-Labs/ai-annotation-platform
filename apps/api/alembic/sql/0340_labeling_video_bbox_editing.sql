CREATE FUNCTION aap_labeling_v1.video_bbox_editing_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_operation text;
    v_keys text[];
    v_expected_keys text[];
    v_task_id uuid;
    v_project_id uuid;
    v_actor_id uuid;
    v_batch_id uuid;
    v_review_round_id uuid;
    v_expected_status text;
    v_actor_reply jsonb;
    v_actor jsonb;
    v_task record;
    v_fence jsonb;
    v_source public.annotations%ROWTYPE;
    v_first_source public.annotations%ROWTYPE;
    v_source_ids uuid[] := ARRAY[]::uuid[];
    v_source_count integer;
    v_locked_count integer;
    v_created_id uuid;
    v_created_ids jsonb := '[]'::jsonb;
    v_updated_ids jsonb := '[]'::jsonb;
    v_deleted_ids jsonb := '[]'::jsonb;
    v_removed_frames jsonb := '[]'::jsonb;
    v_frame jsonb;
    v_frame_ids uuid[] := ARRAY[]::uuid[];
    v_frame_index integer;
    v_bbox_frame_count integer;
    v_max_created integer;
    v_deleted_source boolean := false;
    v_remaining_keyframes jsonb;
    v_expected_keyframes jsonb;
    v_geometry jsonb;
    v_source_geometry jsonb;
    v_created_geometry jsonb;
    v_created_track_id text;
    v_source_fence_id uuid;
    v_expected_version integer;
    v_class_count integer;
    v_frame_count integer;
    v_exact_keyframe jsonb;
    v_is_outside boolean;
    v_source_index integer := 0;
    v_first_geometry jsonb;
    v_second_geometry jsonb;
    v_first_id uuid;
    v_second_id uuid;
    v_first_class text;
    v_second_class text;
    v_first_start integer;
    v_first_end integer;
    v_second_start integer;
    v_second_end integer;
    v_chrono_first_end integer;
    v_chrono_second_start integer;
    v_range record;
    v_outside_input jsonb := '[]'::jsonb;
    v_normalized_outside jsonb := '[]'::jsonb;
    v_current_range jsonb;
    v_range_from integer;
    v_range_to integer;
    v_range_source text;
    v_current_from integer;
    v_current_to integer;
    v_current_source text;
    v_gap_mode text;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-bbox-editing/v1'
       OR jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Labeling video-bbox editing request'
            USING ERRCODE = '22023';
    END IF;

    v_operation := p_request ->> 'operation';
    v_expected_keys := ARRAY[
        'actor_id', 'batch_id', 'expected_review_round_id', 'expected_status',
        'operation', 'project_id', 'schema', 'source_fences', 'task_id'
    ]::text[];
    CASE v_operation
        WHEN 'convert_track' THEN
            v_expected_keys := v_expected_keys || ARRAY[
                'conversion_operation', 'created_frames', 'frame_count', 'frame_index',
                'frame_mode', 'max_created', 'removed_frame_indexes', 'scope'
            ]::text[];
        WHEN 'aggregate_bboxes' THEN
            v_expected_keys := v_expected_keys || ARRAY[
                'created_annotation_id', 'created_geometry', 'created_track_id',
                'delete_sources'
            ]::text[];
        WHEN 'split_track' THEN
            v_expected_keys := v_expected_keys || ARRAY[
                'created_annotation_id', 'created_geometry', 'created_track_id',
                'frame_index', 'updated_geometry'
            ]::text[];
        WHEN 'merge_tracks' THEN
            v_expected_keys := v_expected_keys || ARRAY[
                'survivor_geometry', 'survivor_id', 'survivor_track_id'
            ]::text[];
        WHEN 'join_tracks' THEN
            v_expected_keys := v_expected_keys || ARRAY[
                'gap_mode', 'survivor_geometry', 'survivor_id', 'survivor_track_id'
            ]::text[];
        ELSE
            RAISE EXCEPTION 'Invalid Labeling video-bbox editing operation'
                USING ERRCODE = '22023';
    END CASE;
    SELECT array_agg(key ORDER BY key) INTO v_keys
    FROM jsonb_object_keys(p_request) AS request_keys(key);
    SELECT array_agg(key ORDER BY key) INTO v_expected_keys
    FROM unnest(v_expected_keys) AS expected(key);
    IF v_keys IS DISTINCT FROM v_expected_keys THEN
        RAISE EXCEPTION 'Invalid Labeling video-bbox editing fields'
            USING ERRCODE = '22023';
    END IF;

    IF jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
       OR p_request ->> 'task_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
       OR p_request ->> 'project_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR jsonb_typeof(p_request -> 'actor_id') IS DISTINCT FROM 'string'
       OR p_request ->> 'actor_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR jsonb_typeof(p_request -> 'batch_id') NOT IN ('string', 'null')
       OR (jsonb_typeof(p_request -> 'batch_id') = 'string'
           AND p_request ->> 'batch_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
       OR jsonb_typeof(p_request -> 'expected_review_round_id') NOT IN ('string', 'null')
       OR (jsonb_typeof(p_request -> 'expected_review_round_id') = 'string'
           AND p_request ->> 'expected_review_round_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
       OR jsonb_typeof(p_request -> 'expected_status') IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_request -> 'source_fences') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'Invalid Labeling video-bbox editing identity'
            USING ERRCODE = '22023';
    END IF;
    v_task_id := (p_request ->> 'task_id')::uuid;
    v_project_id := (p_request ->> 'project_id')::uuid;
    v_actor_id := (p_request ->> 'actor_id')::uuid;
    v_batch_id := (p_request ->> 'batch_id')::uuid;
    v_review_round_id := (p_request ->> 'expected_review_round_id')::uuid;
    v_expected_status := p_request ->> 'expected_status';
    v_source_count := jsonb_array_length(p_request -> 'source_fences');
    IF v_source_count < 1 THEN
        RAISE EXCEPTION 'Invalid Labeling video-bbox editing source count'
            USING ERRCODE = '22023';
    END IF;

    v_actor_reply := aap_identity_v1.lock_accounts_share(ARRAY[v_actor_id], true);
    SELECT account.value INTO v_actor
    FROM jsonb_array_elements(COALESCE(v_actor_reply -> 'accounts', '[]'::jsonb)) AS account(value)
    WHERE account.value ->> 'id' = v_actor_id::text;
    IF v_actor IS NULL OR v_actor ->> 'is_active' IS DISTINCT FROM 'true' THEN
        RETURN jsonb_build_object(
            'schema', 'aap-labeling-video-bbox-editing/v1', 'ok', false,
            'request', p_request,
            'error', jsonb_build_object(
                'code', 'actor_inactive', 'status_code', 403,
                'detail', jsonb_build_object('reason', 'actor_inactive')
            )
        );
    END IF;

    SELECT task.id, task.project_id, task.batch_id, task.status,
           task.review_round_id
    INTO v_task
    FROM public.tasks AS task
    WHERE task.id = v_task_id
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'schema', 'aap-labeling-video-bbox-editing/v1', 'ok', false,
            'request', p_request,
            'error', jsonb_build_object(
                'code', 'task_target_changed', 'status_code', 409,
                'detail', jsonb_build_object('reason', 'task_target_changed')
            )
        );
    END IF;
    IF v_task.project_id IS DISTINCT FROM v_project_id
       OR v_task.batch_id IS DISTINCT FROM v_batch_id THEN
        RETURN jsonb_build_object(
            'schema', 'aap-labeling-video-bbox-editing/v1', 'ok', false,
            'request', p_request,
            'error', jsonb_build_object(
                'code', 'task_target_changed', 'status_code', 409,
                'detail', jsonb_build_object('reason', 'task_target_changed')
            )
        );
    END IF;
    IF v_task.status IS DISTINCT FROM v_expected_status
       OR v_task.review_round_id IS DISTINCT FROM v_review_round_id THEN
        RETURN jsonb_build_object(
            'schema', 'aap-labeling-video-bbox-editing/v1', 'ok', false,
            'request', p_request,
            'error', jsonb_build_object(
                'code', 'task_state_changed', 'status_code', 409,
                'detail', jsonb_build_object('reason', 'task_state_changed',
                    'status', v_task.status)
            )
        );
    END IF;

    FOR v_fence IN
        SELECT item.value
        FROM jsonb_array_elements(p_request -> 'source_fences') AS item(value)
    LOOP
        SELECT array_agg(key ORDER BY key) INTO v_keys
        FROM jsonb_object_keys(v_fence) AS fence_keys(key);
        IF v_keys IS DISTINCT FROM ARRAY['annotation_id', 'expected_version']::text[]
           OR jsonb_typeof(v_fence -> 'annotation_id') IS DISTINCT FROM 'string'
           OR v_fence ->> 'annotation_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(v_fence -> 'expected_version') IS DISTINCT FROM 'number'
           OR v_fence ->> 'expected_version' !~ '^[1-9][0-9]*$' THEN
            RAISE EXCEPTION 'Invalid Labeling video-bbox annotation fence'
                USING ERRCODE = '22023';
        END IF;
        v_source_fence_id := (v_fence ->> 'annotation_id')::uuid;
        IF v_source_fence_id = ANY(v_source_ids) THEN
            RAISE EXCEPTION 'Duplicate Labeling video-bbox annotation fence'
                USING ERRCODE = '22023';
        END IF;
        v_source_ids := array_append(v_source_ids, v_source_fence_id);
    END LOOP;
    PERFORM annotation.id
    FROM public.annotations AS annotation
    WHERE annotation.id = ANY(v_source_ids)
    ORDER BY annotation.id
    FOR UPDATE OF annotation NOWAIT;
    GET DIAGNOSTICS v_locked_count = ROW_COUNT;
    IF v_locked_count <> v_source_count THEN
        RETURN jsonb_build_object(
            'schema', 'aap-labeling-video-bbox-editing/v1', 'ok', false,
            'request', p_request,
            'error', jsonb_build_object(
                'code', 'source_changed', 'status_code', 409,
                'detail', jsonb_build_object('reason', 'annotation_not_found')
            )
        );
    END IF;

    v_source_index := 0;
    FOR v_fence IN
        SELECT item.value
        FROM jsonb_array_elements(p_request -> 'source_fences') AS item(value)
    LOOP
        v_source_index := v_source_index + 1;
        v_source_fence_id := (v_fence ->> 'annotation_id')::uuid;
        v_expected_version := (v_fence ->> 'expected_version')::integer;
        SELECT annotation.* INTO v_source
        FROM public.annotations AS annotation
        WHERE annotation.id = v_source_fence_id;
        IF NOT FOUND
           OR v_source.task_id IS DISTINCT FROM v_task_id
           OR v_source.project_id IS DISTINCT FROM v_project_id
           OR v_source.version IS DISTINCT FROM v_expected_version
           OR v_source.is_active IS NOT TRUE
           OR v_source.was_cancelled IS TRUE
           OR v_source.sensor_role IS NOT NULL THEN
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-video-bbox-editing/v1', 'ok', false,
                'request', p_request,
                'error', jsonb_build_object(
                    'code', 'source_changed', 'status_code', 409,
                    'detail', jsonb_build_object('reason', 'annotation_changed',
                        'annotation_id', v_source_fence_id)
                )
            );
        END IF;
        IF v_operation = 'aggregate_bboxes'
           AND (v_source.annotation_type IS DISTINCT FROM 'video_bbox'
                OR v_source.geometry ->> 'type' IS DISTINCT FROM 'video_bbox') THEN
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-video-bbox-editing/v1', 'ok', false,
                'request', p_request,
                'error', jsonb_build_object(
                    'code', 'invalid_source', 'status_code', 400,
                    'detail', 'aggregate_bboxes only accepts video_bbox annotations'
                )
            );
        ELSIF v_operation <> 'aggregate_bboxes'
           AND (v_source.annotation_type IS DISTINCT FROM 'video_track_bbox'
                OR v_source.geometry ->> 'type' IS DISTINCT FROM 'video_track_bbox') THEN
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-video-bbox-editing/v1', 'ok', false,
                'request', p_request,
                'error', jsonb_build_object(
                    'code', 'invalid_source', 'status_code', 400,
                    'detail', CASE v_operation
                        WHEN 'convert_track' THEN 'annotation must be a video_track_bbox'
                        WHEN 'split_track' THEN 'split_track only accepts a video_track_bbox annotation'
                        ELSE format('%s only accepts video_track_bbox annotations', v_operation)
                    END
                )
            );
        END IF;
        IF v_source_index = 1 THEN
            v_first_source := v_source;
            v_first_geometry := v_source.geometry;
            v_first_id := v_source.id;
            v_first_class := v_source.class_name;
        ELSIF v_source_index = 2 THEN
            v_second_geometry := v_source.geometry;
            v_second_id := v_source.id;
            v_second_class := v_source.class_name;
        END IF;
    END LOOP;

    IF v_operation = 'convert_track' THEN
        IF v_source_count <> 1
           OR p_request ->> 'conversion_operation' NOT IN ('copy', 'split')
           OR p_request ->> 'scope' NOT IN ('frame', 'track')
           OR p_request ->> 'frame_mode' NOT IN ('keyframes', 'all_frames')
           OR jsonb_typeof(p_request -> 'max_created') IS DISTINCT FROM 'number'
           OR p_request ->> 'max_created' !~ '^[1-9][0-9]*$'
           OR (p_request ->> 'max_created')::numeric > 5000
           OR jsonb_typeof(p_request -> 'frame_count') NOT IN ('number', 'null')
           OR (jsonb_typeof(p_request -> 'frame_count') = 'number'
               AND p_request ->> 'frame_count' !~ '^-?[0-9]+$')
           OR jsonb_typeof(p_request -> 'created_frames') IS DISTINCT FROM 'array'
           OR jsonb_typeof(p_request -> 'removed_frame_indexes') IS DISTINCT FROM 'array'
           OR jsonb_array_length(p_request -> 'created_frames') = 0 THEN
            RAISE EXCEPTION 'Invalid Labeling video-bbox conversion fields'
                USING ERRCODE = '22023';
        END IF;
        v_max_created := (p_request ->> 'max_created')::integer;
        v_bbox_frame_count := jsonb_array_length(p_request -> 'created_frames');
        FOR v_fence IN
            SELECT item.value
            FROM jsonb_array_elements(p_request -> 'removed_frame_indexes') AS item(value)
        LOOP
            IF jsonb_typeof(v_fence) IS DISTINCT FROM 'number'
               OR v_fence #>> '{}' !~ '^-?[0-9]+$' THEN
                RAISE EXCEPTION 'Invalid removed video-bbox frame index'
                    USING ERRCODE = '22023';
            END IF;
        END LOOP;
        IF v_bbox_frame_count > v_max_created THEN
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-video-bbox-editing/v1', 'ok', false,
                'request', p_request,
                'error', jsonb_build_object(
                    'code', 'invalid_conversion', 'status_code', 400,
                    'detail', format('track conversion would create more than %s bboxes', v_max_created)
                )
            );
        END IF;
        IF p_request ->> 'scope' = 'frame'
           AND (jsonb_typeof(p_request -> 'frame_index') IS DISTINCT FROM 'number'
                OR p_request ->> 'frame_index' !~ '^-?[0-9]+$') THEN
            RAISE EXCEPTION 'frame_index is required for frame scope'
                USING ERRCODE = '22023';
        END IF;
        IF p_request ->> 'scope' = 'track'
           AND jsonb_typeof(p_request -> 'frame_index') NOT IN ('number', 'null') THEN
            RAISE EXCEPTION 'Invalid frame_index for track scope'
                USING ERRCODE = '22023';
        END IF;
        v_frame_index := (p_request ->> 'frame_index')::integer;
        IF p_request ->> 'conversion_operation' = 'copy'
           AND jsonb_array_length(p_request -> 'removed_frame_indexes') <> 0 THEN
            RAISE EXCEPTION 'copy must not remove source frames' USING ERRCODE = '22023';
        END IF;
        IF p_request ->> 'conversion_operation' = 'split'
           AND p_request ->> 'scope' = 'frame'
           AND (jsonb_array_length(p_request -> 'removed_frame_indexes') <> 1
                OR (p_request -> 'removed_frame_indexes' ->> 0)::integer <> v_frame_index
                OR v_bbox_frame_count <> 1
                OR ((p_request -> 'created_frames' -> 0 ->> 'frame_index')::integer <> v_frame_index)) THEN
            RAISE EXCEPTION 'Invalid frame split conversion plan'
                USING ERRCODE = '22023';
        END IF;
        IF p_request ->> 'conversion_operation' = 'split'
           AND p_request ->> 'scope' = 'frame' THEN
            SELECT keyframe.value INTO v_exact_keyframe
            FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(v_first_geometry -> 'keyframes') = 'array'
                     THEN v_first_geometry -> 'keyframes' ELSE '[]'::jsonb END
            ) AS keyframe(value)
            WHERE jsonb_typeof(keyframe.value) = 'object'
              AND COALESCE((keyframe.value ->> 'frame_index')::integer, 0) = v_frame_index
            ORDER BY COALESCE((keyframe.value ->> 'frame_index')::integer, 0)
            LIMIT 1;
            SELECT EXISTS (
                SELECT 1
                FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof(v_first_geometry -> 'outside') = 'array'
                         THEN v_first_geometry -> 'outside' ELSE '[]'::jsonb END
                ) AS outside_range(value)
                WHERE v_frame_index BETWEEN
                    GREATEST(0, LEAST(
                        COALESCE((outside_range.value ->> 'from')::integer, 0),
                        COALESCE((outside_range.value ->> 'to')::integer, 0)
                    )) AND
                    GREATEST(0, GREATEST(
                        COALESCE((outside_range.value ->> 'from')::integer, 0),
                        COALESCE((outside_range.value ->> 'to')::integer, 0)
                    ))
            ) INTO v_is_outside;
            IF v_exact_keyframe IS NULL OR v_is_outside THEN
                RETURN jsonb_build_object(
                    'schema', 'aap-labeling-video-bbox-editing/v1', 'ok', false,
                    'request', p_request,
                    'error', jsonb_build_object(
                        'code', 'invalid_conversion', 'status_code', 400,
                        'detail', 'frame split requires an exact visible keyframe'
                    )
                );
            END IF;
        END IF;
        IF p_request ->> 'conversion_operation' = 'split'
           AND p_request ->> 'scope' = 'track' THEN
            SELECT COALESCE(jsonb_agg(frame_index ORDER BY ordinal), '[]'::jsonb)
            INTO v_removed_frames
            FROM jsonb_array_elements(p_request -> 'created_frames') WITH ORDINALITY AS frame(value, ordinal)
            CROSS JOIN LATERAL (SELECT (frame.value ->> 'frame_index')::integer AS frame_index) AS parsed;
            IF p_request -> 'removed_frame_indexes' <> v_removed_frames THEN
                RAISE EXCEPTION 'Invalid track split conversion plan'
                    USING ERRCODE = '22023';
            END IF;
        END IF;

        FOR v_frame IN
            SELECT item.value
            FROM jsonb_array_elements(p_request -> 'created_frames') WITH ORDINALITY AS item(value, ordinal)
            ORDER BY item.ordinal
        LOOP
            SELECT array_agg(key ORDER BY key) INTO v_keys
            FROM jsonb_object_keys(v_frame) AS frame_keys(key);
            IF v_keys IS DISTINCT FROM ARRAY['annotation_id', 'frame_index', 'h', 'w', 'x', 'y']::text[]
               OR jsonb_typeof(v_frame -> 'annotation_id') IS DISTINCT FROM 'string'
               OR v_frame ->> 'annotation_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR jsonb_typeof(v_frame -> 'frame_index') IS DISTINCT FROM 'number'
               OR v_frame ->> 'frame_index' !~ '^-?[0-9]+$'
               OR jsonb_typeof(v_frame -> 'x') IS DISTINCT FROM 'number'
               OR jsonb_typeof(v_frame -> 'y') IS DISTINCT FROM 'number'
               OR jsonb_typeof(v_frame -> 'w') IS DISTINCT FROM 'number'
               OR jsonb_typeof(v_frame -> 'h') IS DISTINCT FROM 'number' THEN
                RAISE EXCEPTION 'Invalid converted video-bbox frame'
                    USING ERRCODE = '22023';
            END IF;
            v_created_id := (v_frame ->> 'annotation_id')::uuid;
            v_frame_index := (v_frame ->> 'frame_index')::integer;
            IF v_created_id = ANY(v_frame_ids) THEN
                RAISE EXCEPTION 'Duplicate converted video-bbox frame'
                    USING ERRCODE = '22023';
            END IF;
            v_frame_ids := array_append(v_frame_ids, v_created_id);
            INSERT INTO public.annotations (
                id, task_id, project_id, video_segment_id, user_id, source,
                annotation_type, class_name, geometry, confidence,
                parent_annotation_id, attributes
            ) VALUES (
                v_created_id, v_task_id, v_project_id, v_first_source.video_segment_id,
                v_actor_id, v_first_source.source, 'video_bbox', v_first_source.class_name,
                jsonb_build_object(
                    'type', 'video_bbox', 'frame_index', v_frame_index,
                    'x', v_frame -> 'x', 'y', v_frame -> 'y',
                    'w', v_frame -> 'w', 'h', v_frame -> 'h'
                ),
                v_first_source.confidence, v_first_source.id,
                COALESCE(v_first_source.attributes, '{}'::jsonb)
            );
            v_created_ids := v_created_ids || jsonb_build_array(v_created_id);
        END LOOP;

        IF p_request ->> 'conversion_operation' = 'split'
           AND p_request ->> 'scope' = 'track' THEN
            UPDATE public.annotations AS annotation
            SET is_active = false, updated_at = transaction_timestamp()
            WHERE annotation.id = v_first_source.id;
            v_deleted_ids := jsonb_build_array(v_first_source.id);
            v_deleted_source := true;
        ELSIF p_request ->> 'conversion_operation' = 'split'
           AND p_request ->> 'scope' = 'frame' THEN
            SELECT COALESCE(
                jsonb_agg(keyframe.value ORDER BY COALESCE((keyframe.value ->> 'frame_index')::integer, 0)),
                '[]'::jsonb
            ) INTO v_remaining_keyframes
            FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(v_first_geometry -> 'keyframes') = 'array'
                     THEN v_first_geometry -> 'keyframes' ELSE '[]'::jsonb END
            ) AS keyframe(value)
            WHERE jsonb_typeof(keyframe.value) = 'object'
              AND COALESCE((keyframe.value ->> 'frame_index')::integer, 0) <> v_frame_index;
            IF jsonb_array_length(v_remaining_keyframes) > 0 THEN
                UPDATE public.annotations AS annotation
                SET geometry = jsonb_set(annotation.geometry, '{keyframes}', v_remaining_keyframes, true),
                    version = annotation.version + 1,
                    updated_at = transaction_timestamp()
                WHERE annotation.id = v_first_source.id;
                v_updated_ids := jsonb_build_array(v_first_source.id);
            ELSE
                UPDATE public.annotations AS annotation
                SET is_active = false, updated_at = transaction_timestamp()
                WHERE annotation.id = v_first_source.id;
                v_deleted_ids := jsonb_build_array(v_first_source.id);
                v_deleted_source := true;
            END IF;
        END IF;
        IF p_request ->> 'conversion_operation' = 'split' THEN
            v_removed_frames := p_request -> 'removed_frame_indexes';
        END IF;
    ELSIF v_operation = 'aggregate_bboxes' THEN
        SELECT count(DISTINCT annotation.class_name),
               count(DISTINCT COALESCE((annotation.geometry ->> 'frame_index')::integer, 0))
        INTO v_class_count, v_frame_count
        FROM public.annotations AS annotation
        WHERE annotation.id = ANY(v_source_ids);
        IF v_class_count <> 1 THEN
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-video-bbox-editing/v1', 'ok', false,
                'request', p_request,
                'error', jsonb_build_object(
                    'code', 'invalid_composition', 'status_code', 400,
                    'detail', 'video_bbox annotations must share one class'
                )
            );
        END IF;
        IF v_frame_count <> v_source_count THEN
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-video-bbox-editing/v1', 'ok', false,
                'request', p_request,
                'error', jsonb_build_object(
                    'code', 'invalid_composition', 'status_code', 400,
                    'detail', 'video_bbox annotations must not share a frame_index'
                )
            );
        END IF;
        IF jsonb_typeof(p_request -> 'created_annotation_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'created_annotation_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(p_request -> 'created_track_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'created_track_id' !~ '^trk_[0-9a-f]{32}$'
           OR jsonb_typeof(p_request -> 'delete_sources') IS DISTINCT FROM 'boolean'
           OR jsonb_typeof(p_request -> 'created_geometry') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'Invalid aggregate video-bbox fields'
                USING ERRCODE = '22023';
        END IF;
        v_created_id := (p_request ->> 'created_annotation_id')::uuid;
        v_created_track_id := p_request ->> 'created_track_id';
        v_created_geometry := p_request -> 'created_geometry';
        IF v_created_geometry ->> 'type' IS DISTINCT FROM 'video_track_bbox'
           OR v_created_geometry ->> 'track_id' IS DISTINCT FROM v_created_track_id
           OR jsonb_typeof(v_created_geometry -> 'keyframes') IS DISTINCT FROM 'array'
           OR v_created_geometry -> 'outside' IS DISTINCT FROM '[]'::jsonb THEN
            RAISE EXCEPTION 'Invalid aggregate video-track geometry'
                USING ERRCODE = '22023';
        END IF;
        SELECT array_agg(key ORDER BY key) INTO v_keys
        FROM jsonb_object_keys(v_created_geometry) AS geometry_keys(key);
        IF v_keys IS DISTINCT FROM ARRAY['keyframes', 'outside', 'track_id', 'type']::text[] THEN
            RAISE EXCEPTION 'Invalid aggregate video-track geometry fields'
                USING ERRCODE = '22023';
        END IF;
        SELECT COALESCE(
            jsonb_agg(jsonb_build_object(
                'frame_index', COALESCE((annotation.geometry ->> 'frame_index')::integer, 0),
                'bbox', jsonb_build_object(
                    'x', COALESCE((annotation.geometry ->> 'x')::double precision, 0),
                    'y', COALESCE((annotation.geometry ->> 'y')::double precision, 0),
                    'w', COALESCE((annotation.geometry ->> 'w')::double precision, 0),
                    'h', COALESCE((annotation.geometry ->> 'h')::double precision, 0)
                ),
                'source', 'manual', 'occluded', false
            ) ORDER BY COALESCE((annotation.geometry ->> 'frame_index')::integer, 0)),
            '[]'::jsonb
        ) INTO v_expected_keyframes
        FROM public.annotations AS annotation
        WHERE annotation.id = ANY(v_source_ids);
        IF v_created_geometry -> 'keyframes' IS DISTINCT FROM v_expected_keyframes THEN
            RAISE EXCEPTION 'Aggregate video-track frames do not match locked annotations'
                USING ERRCODE = '22023';
        END IF;
        SELECT annotation.* INTO v_first_source
        FROM public.annotations AS annotation
        WHERE annotation.id = ANY(v_source_ids)
        ORDER BY COALESCE((annotation.geometry ->> 'frame_index')::integer, 0), annotation.id
        LIMIT 1;
        INSERT INTO public.annotations (
            id, task_id, project_id, video_segment_id, user_id, source,
            annotation_type, class_name, geometry, confidence, track_id, attributes
        ) VALUES (
            v_created_id, v_task_id, v_project_id, v_first_source.video_segment_id,
            v_actor_id, v_first_source.source, 'video_track_bbox',
            v_first_source.class_name, v_created_geometry, v_first_source.confidence,
            v_created_track_id, COALESCE(v_first_source.attributes, '{}'::jsonb)
        );
        v_created_ids := jsonb_build_array(v_created_id);
        IF (p_request ->> 'delete_sources')::boolean THEN
            UPDATE public.annotations AS annotation
            SET is_active = false, updated_at = transaction_timestamp()
            WHERE annotation.id = ANY(v_source_ids);
            v_deleted_ids := (
                SELECT COALESCE(jsonb_agg((item.value ->> 'annotation_id')::uuid ORDER BY item.ordinality), '[]'::jsonb)
                FROM jsonb_array_elements(p_request -> 'source_fences') WITH ORDINALITY AS item(value, ordinality)
            );
        END IF;
    ELSIF v_operation = 'split_track' THEN
        IF v_source_count <> 1
           OR jsonb_typeof(p_request -> 'frame_index') IS DISTINCT FROM 'number'
           OR p_request ->> 'frame_index' !~ '^-?[0-9]+$'
           OR jsonb_typeof(p_request -> 'created_annotation_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'created_annotation_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(p_request -> 'created_track_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'created_track_id' !~ '^trk_[0-9a-f]{32}$'
           OR jsonb_typeof(p_request -> 'updated_geometry') IS DISTINCT FROM 'object'
           OR jsonb_typeof(p_request -> 'created_geometry') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'Invalid split video-track fields'
                USING ERRCODE = '22023';
        END IF;
        v_frame_index := (p_request ->> 'frame_index')::integer;
        v_created_id := (p_request ->> 'created_annotation_id')::uuid;
        v_created_track_id := p_request ->> 'created_track_id';
        v_source_geometry := p_request -> 'updated_geometry';
        v_created_geometry := p_request -> 'created_geometry';
        IF v_created_geometry ->> 'type' IS DISTINCT FROM 'video_track_bbox'
           OR v_created_geometry ->> 'track_id' IS DISTINCT FROM v_created_track_id
           OR jsonb_typeof(v_created_geometry -> 'keyframes') IS DISTINCT FROM 'array'
           OR jsonb_typeof(v_created_geometry -> 'outside') IS DISTINCT FROM 'array'
           OR v_source_geometry ->> 'type' IS DISTINCT FROM 'video_track_bbox'
           OR jsonb_typeof(v_source_geometry -> 'keyframes') IS DISTINCT FROM 'array'
           OR jsonb_typeof(v_source_geometry -> 'outside') IS DISTINCT FROM 'array'
           OR (v_source_geometry - 'keyframes' - 'outside') IS DISTINCT FROM
              (v_first_geometry - 'keyframes' - 'outside') THEN
            RAISE EXCEPTION 'Invalid split video-track geometry'
                USING ERRCODE = '22023';
        END IF;
        IF v_source_geometry ->> 'track_id' IS DISTINCT FROM v_first_geometry ->> 'track_id' THEN
            RAISE EXCEPTION 'Split video-track source identity changed'
                USING ERRCODE = '22023';
        END IF;
        UPDATE public.annotations AS annotation
        SET geometry = v_source_geometry,
            version = annotation.version + 1,
            updated_at = transaction_timestamp()
        WHERE annotation.id = v_first_source.id;
        INSERT INTO public.annotations (
            id, task_id, project_id, video_segment_id, user_id, source,
            annotation_type, class_name, geometry, confidence,
            parent_annotation_id, track_id, attributes
        ) VALUES (
            v_created_id, v_task_id, v_project_id, v_first_source.video_segment_id,
            v_actor_id, v_first_source.source, 'video_track_bbox',
            v_first_source.class_name, v_created_geometry, v_first_source.confidence,
            v_first_source.id, v_created_track_id,
            COALESCE(v_first_source.attributes, '{}'::jsonb)
        );
        v_updated_ids := jsonb_build_array(v_first_source.id);
        v_created_ids := jsonb_build_array(v_created_id);
    ELSIF v_operation IN ('merge_tracks', 'join_tracks') THEN
        IF v_source_count <> 2
           OR jsonb_typeof(p_request -> 'survivor_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'survivor_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR (p_request ->> 'survivor_id')::uuid IS DISTINCT FROM v_first_id
           OR jsonb_typeof(p_request -> 'survivor_track_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'survivor_geometry') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'Invalid two-track composition fields'
                USING ERRCODE = '22023';
        END IF;
        IF v_operation = 'join_tracks'
           AND p_request ->> 'gap_mode' NOT IN ('interpolate', 'outside') THEN
            RAISE EXCEPTION 'Invalid join_tracks gap_mode' USING ERRCODE = '22023';
        END IF;
        IF v_first_class IS DISTINCT FROM v_second_class THEN
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-video-bbox-editing/v1', 'ok', false,
                'request', p_request,
                'error', jsonb_build_object(
                    'code', 'invalid_composition', 'status_code', 400,
                    'detail', format('%s requires tracks with the same class', v_operation)
                )
            );
        END IF;
        SELECT min(frame_index), max(frame_index) INTO v_first_start, v_first_end
        FROM (
            SELECT COALESCE((keyframe.value ->> 'frame_index')::integer, 0) AS frame_index
            FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(v_first_geometry -> 'keyframes') = 'array'
                     THEN v_first_geometry -> 'keyframes' ELSE '[]'::jsonb END
            ) AS keyframe(value)
            WHERE jsonb_typeof(keyframe.value) = 'object'
              AND NOT EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements(
                      CASE WHEN jsonb_typeof(v_first_geometry -> 'outside') = 'array'
                           THEN v_first_geometry -> 'outside' ELSE '[]'::jsonb END
                  ) AS outside_range(value)
                  WHERE COALESCE((keyframe.value ->> 'frame_index')::integer, 0)
                    BETWEEN GREATEST(0, LEAST(
                        COALESCE((outside_range.value ->> 'from')::integer, 0),
                        COALESCE((outside_range.value ->> 'to')::integer, 0)
                    )) AND GREATEST(0, GREATEST(
                        COALESCE((outside_range.value ->> 'from')::integer, 0),
                        COALESCE((outside_range.value ->> 'to')::integer, 0)
                    ))
              )
        ) AS visible;
        IF v_first_start IS NULL THEN
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-video-bbox-editing/v1', 'ok', false,
                'request', p_request,
                'error', jsonb_build_object(
                    'code', 'invalid_composition', 'status_code', 400,
                    'detail', format('%s requires visible keyframes', v_operation)
                )
            );
        END IF;
        SELECT min(frame_index), max(frame_index) INTO v_second_start, v_second_end
        FROM (
            SELECT COALESCE((keyframe.value ->> 'frame_index')::integer, 0) AS frame_index
            FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(v_second_geometry -> 'keyframes') = 'array'
                     THEN v_second_geometry -> 'keyframes' ELSE '[]'::jsonb END
            ) AS keyframe(value)
            WHERE jsonb_typeof(keyframe.value) = 'object'
              AND NOT EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements(
                      CASE WHEN jsonb_typeof(v_second_geometry -> 'outside') = 'array'
                           THEN v_second_geometry -> 'outside' ELSE '[]'::jsonb END
                  ) AS outside_range(value)
                  WHERE COALESCE((keyframe.value ->> 'frame_index')::integer, 0)
                    BETWEEN GREATEST(0, LEAST(
                        COALESCE((outside_range.value ->> 'from')::integer, 0),
                        COALESCE((outside_range.value ->> 'to')::integer, 0)
                    )) AND GREATEST(0, GREATEST(
                        COALESCE((outside_range.value ->> 'from')::integer, 0),
                        COALESCE((outside_range.value ->> 'to')::integer, 0)
                    ))
              )
        ) AS visible;
        IF v_second_start IS NULL THEN
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-video-bbox-editing/v1', 'ok', false,
                'request', p_request,
                'error', jsonb_build_object(
                    'code', 'invalid_composition', 'status_code', 400,
                    'detail', format('%s requires visible keyframes', v_operation)
                )
            );
        END IF;
        IF (v_first_end >= v_second_start AND v_second_end >= v_first_start) THEN
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-video-bbox-editing/v1', 'ok', false,
                'request', p_request,
                'error', jsonb_build_object(
                    'code', 'invalid_composition', 'status_code', 400,
                    'detail', format('%s requires non-overlapping tracks', v_operation)
                )
            );
        END IF;
        IF EXISTS (
            SELECT frame_index
            FROM (
                SELECT COALESCE((keyframe.value ->> 'frame_index')::integer, 0) AS frame_index
                FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof(v_first_geometry -> 'keyframes') = 'array'
                         THEN v_first_geometry -> 'keyframes' ELSE '[]'::jsonb END
                ) AS keyframe(value)
                WHERE jsonb_typeof(keyframe.value) = 'object'
                UNION ALL
                SELECT COALESCE((keyframe.value ->> 'frame_index')::integer, 0) AS frame_index
                FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof(v_second_geometry -> 'keyframes') = 'array'
                         THEN v_second_geometry -> 'keyframes' ELSE '[]'::jsonb END
                ) AS keyframe(value)
                WHERE jsonb_typeof(keyframe.value) = 'object'
            ) AS frames
            GROUP BY frame_index HAVING count(*) > 1
        ) THEN
            RETURN jsonb_build_object(
                'schema', 'aap-labeling-video-bbox-editing/v1', 'ok', false,
                'request', p_request,
                'error', jsonb_build_object(
                    'code', 'invalid_composition', 'status_code', 400,
                    'detail', format('%s requires unique keyframe frame_index values', v_operation)
                )
            );
        END IF;
        SELECT COALESCE(jsonb_agg(keyframe.value ORDER BY COALESCE((keyframe.value ->> 'frame_index')::integer, 0)), '[]'::jsonb)
        INTO v_expected_keyframes
        FROM (
            SELECT item.value AS value
            FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(v_first_geometry -> 'keyframes') = 'array'
                     THEN v_first_geometry -> 'keyframes' ELSE '[]'::jsonb END
            ) AS item(value)
            WHERE jsonb_typeof(item.value) = 'object'
            UNION ALL
            SELECT item.value AS value
            FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(v_second_geometry -> 'keyframes') = 'array'
                     THEN v_second_geometry -> 'keyframes' ELSE '[]'::jsonb END
            ) AS item(value)
            WHERE jsonb_typeof(item.value) = 'object'
        ) AS keyframe;
        v_created_geometry := p_request -> 'survivor_geometry';
        v_created_track_id := p_request ->> 'survivor_track_id';
        IF v_created_geometry ->> 'type' IS DISTINCT FROM 'video_track_bbox'
           OR v_created_geometry ->> 'track_id' IS DISTINCT FROM v_created_track_id
           OR jsonb_typeof(v_created_geometry -> 'keyframes') IS DISTINCT FROM 'array'
           OR jsonb_typeof(v_created_geometry -> 'outside') IS DISTINCT FROM 'array'
           OR v_created_geometry -> 'keyframes' IS DISTINCT FROM v_expected_keyframes
           OR (v_created_geometry - 'keyframes' - 'outside' - 'track_id') IS DISTINCT FROM
              (v_first_geometry - 'keyframes' - 'outside' - 'track_id') THEN
            RAISE EXCEPTION 'Invalid merged video-track geometry'
                USING ERRCODE = '22023';
        END IF;
        IF v_first_start < v_second_start THEN
            v_chrono_first_end := v_first_end;
            v_chrono_second_start := v_second_start;
        ELSE
            v_chrono_first_end := v_second_end;
            v_chrono_second_start := v_first_start;
        END IF;
        SELECT COALESCE(jsonb_agg(outside_range.value), '[]'::jsonb)
        INTO v_outside_input
        FROM (
            SELECT item.value AS value
            FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(v_first_geometry -> 'outside') = 'array'
                     THEN v_first_geometry -> 'outside' ELSE '[]'::jsonb END
            ) AS item(value)
            UNION ALL
            SELECT item.value AS value
            FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(v_second_geometry -> 'outside') = 'array'
                     THEN v_second_geometry -> 'outside' ELSE '[]'::jsonb END
            ) AS item(value)
        ) AS outside_range;
        v_gap_mode := CASE
            WHEN v_operation = 'merge_tracks' THEN 'outside'
            ELSE p_request ->> 'gap_mode'
        END;
        IF v_gap_mode = 'outside' AND v_chrono_first_end + 1 <= v_chrono_second_start - 1 THEN
            v_outside_input := v_outside_input || jsonb_build_array(jsonb_build_object(
                'from', v_chrono_first_end + 1,
                'to', v_chrono_second_start - 1,
                'source', 'manual'
            ));
        END IF;
        v_current_range := NULL;
        FOR v_range IN
            SELECT
                GREATEST(0, LEAST(
                    COALESCE((item.value ->> 'from')::integer, 0),
                    COALESCE((item.value ->> 'to')::integer, 0)
                )) AS from_frame,
                GREATEST(0, GREATEST(
                    COALESCE((item.value ->> 'from')::integer, 0),
                    COALESCE((item.value ->> 'to')::integer, 0)
                )) AS to_frame,
                CASE WHEN item.value ->> 'source' = 'prediction'
                     THEN 'prediction' ELSE 'manual' END AS source
            FROM jsonb_array_elements(v_outside_input) AS item(value)
            ORDER BY 1, 2, 3
        LOOP
            IF v_current_range IS NULL THEN
                v_current_range := jsonb_build_object(
                    'from', v_range.from_frame, 'to', v_range.to_frame,
                    'source', v_range.source
                );
            ELSE
                v_current_from := (v_current_range ->> 'from')::integer;
                v_current_to := (v_current_range ->> 'to')::integer;
                v_current_source := v_current_range ->> 'source';
                IF v_range.source = v_current_source
                   AND v_range.from_frame <= v_current_to + 1 THEN
                    v_current_range := jsonb_build_object(
                        'from', v_current_from,
                        'to', GREATEST(v_current_to, v_range.to_frame),
                        'source', v_current_source
                    );
                ELSE
                    v_normalized_outside := v_normalized_outside || jsonb_build_array(v_current_range);
                    v_current_range := jsonb_build_object(
                        'from', v_range.from_frame, 'to', v_range.to_frame,
                        'source', v_range.source
                    );
                END IF;
            END IF;
        END LOOP;
        IF v_current_range IS NOT NULL THEN
            v_normalized_outside := v_normalized_outside || jsonb_build_array(v_current_range);
        END IF;
        IF v_created_geometry -> 'outside' IS DISTINCT FROM v_normalized_outside THEN
            RAISE EXCEPTION 'Merged video-track outside ranges do not match locked annotations'
                USING ERRCODE = '22023';
        END IF;
        UPDATE public.annotations AS annotation
        SET geometry = v_created_geometry,
            track_id = v_created_track_id,
            version = annotation.version + 1,
            updated_at = transaction_timestamp()
        WHERE annotation.id = v_first_id;
        UPDATE public.annotations AS annotation
        SET is_active = false, updated_at = transaction_timestamp()
        WHERE annotation.id = v_second_id;
        v_updated_ids := jsonb_build_array(v_first_id);
        v_deleted_ids := jsonb_build_array(v_second_id);
    END IF;

    RETURN jsonb_build_object(
        'schema', 'aap-labeling-video-bbox-editing/v1',
        'ok', true,
        'request', p_request,
        'data', jsonb_build_object(
            'updated_annotation_ids', v_updated_ids,
            'created_annotation_ids', v_created_ids,
            'deleted_annotation_ids', v_deleted_ids,
            'removed_frame_indexes', v_removed_frames,
            'deleted_source', v_deleted_source
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.video_bbox_editing_command(jsonb) FROM PUBLIC;
