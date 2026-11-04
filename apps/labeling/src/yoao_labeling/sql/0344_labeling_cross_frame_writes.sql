CREATE FUNCTION aap_labeling_v1.cross_frame_write_command(p_request jsonb)
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
    v_actor_id uuid;
    v_project_id uuid;
    v_task_ids uuid[] := ARRAY[]::uuid[];
    v_source_ids uuid[] := ARRAY[]::uuid[];
    v_target_ids uuid[] := ARRAY[]::uuid[];
    v_scene_track_ids uuid[] := ARRAY[]::uuid[];
    v_task_count integer := 0;
    v_source_count integer := 0;
    v_target_count integer := 0;
    v_scene_track_count integer := 0;
    v_expected_scene_count integer := 0;
    v_locked_count integer := 0;
    v_ordinal bigint;
    v_scene_ordinal bigint := 0;
    v_task_request jsonb;
    v_source_request jsonb;
    v_target_request jsonb;
    v_track_request jsonb;
    v_scene_reply jsonb;
    v_track_uuid uuid;
    v_source_field_map jsonb := '{}'::jsonb;
    v_source record;
    v_task record;
    v_track record;
    v_inserted public.annotations%ROWTYPE;
    v_created jsonb := '[]'::jsonb;
    v_sources jsonb := '[]'::jsonb;
    v_scene_tracks jsonb := '[]'::jsonb;
    v_expected_version integer;
    v_role text;
    v_written_fields text[];
    v_frames integer[];
    v_frame jsonb;
BEGIN
    IF p_request IS NULL OR jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-cross-frame-writes/v1'
       OR jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_request -> 'actor_id') IS DISTINCT FROM 'string'
       OR p_request ->> 'actor_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR jsonb_typeof(p_request -> 'task_fences') IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_request -> 'source_annotations') IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_request -> 'target_annotations') IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_request -> 'scene_tracks') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'Invalid Labeling cross-frame write request'
            USING ERRCODE = '22023';
    END IF;

    SELECT array_agg(key ORDER BY key) INTO v_keys
    FROM jsonb_object_keys(p_request) AS request_keys(key);
    IF v_keys IS DISTINCT FROM ARRAY[
        'actor_id', 'operation', 'scene_tracks', 'schema', 'source_annotations',
        'target_annotations', 'task_fences'
    ]::text[] THEN
        RAISE EXCEPTION 'Invalid Labeling cross-frame write fields'
            USING ERRCODE = '22023';
    END IF;

    v_operation := p_request ->> 'operation';
    IF v_operation NOT IN ('propagate', 'propagate_batch', 'interpolate_range') THEN
        RAISE EXCEPTION 'Invalid Labeling cross-frame write operation'
            USING ERRCODE = '22023';
    END IF;
    v_actor_id := (p_request ->> 'actor_id')::uuid;

    FOR v_task_request, v_ordinal IN
        SELECT value, ordinality
        FROM jsonb_array_elements(p_request -> 'task_fences') WITH ORDINALITY AS rows(value, ordinality)
        ORDER BY ordinality
    LOOP
        IF jsonb_typeof(v_task_request) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'Invalid Labeling cross-frame task fence'
                USING ERRCODE = '22023';
        END IF;
        SELECT array_agg(key ORDER BY key) INTO v_keys
        FROM jsonb_object_keys(v_task_request) AS request_keys(key);
        IF v_keys IS DISTINCT FROM ARRAY[
            'expected_version', 'project_id', 'task_id'
        ]::text[]
           OR jsonb_typeof(v_task_request -> 'task_id') IS DISTINCT FROM 'string'
           OR v_task_request ->> 'task_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(v_task_request -> 'project_id') IS DISTINCT FROM 'string'
           OR v_task_request ->> 'project_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(v_task_request -> 'expected_version') IS DISTINCT FROM 'number'
           OR v_task_request ->> 'expected_version' !~ '^[1-9][0-9]*$' THEN
            RAISE EXCEPTION 'Invalid Labeling cross-frame task fence fields'
                USING ERRCODE = '22023';
        END IF;
        IF (v_task_request ->> 'expected_version')::bigint > 2147483647 THEN
            RAISE EXCEPTION 'Invalid Labeling cross-frame task version'
                USING ERRCODE = '22023';
        END IF;
        IF (v_task_request ->> 'task_id')::uuid = ANY(v_task_ids) THEN
            RAISE EXCEPTION 'Duplicate Labeling cross-frame task fence'
                USING ERRCODE = '22023';
        END IF;
        v_task_ids := array_append(v_task_ids, (v_task_request ->> 'task_id')::uuid);
        v_task_count := v_task_count + 1;
        IF v_project_id IS NULL THEN
            v_project_id := (v_task_request ->> 'project_id')::uuid;
        ELSIF v_project_id IS DISTINCT FROM (v_task_request ->> 'project_id')::uuid THEN
            RAISE EXCEPTION 'Cross-frame task fences must share a project'
                USING ERRCODE = '22023';
        END IF;
    END LOOP;
    IF v_task_count = 0 THEN
        RAISE EXCEPTION 'Cross-frame task fences cannot be empty'
            USING ERRCODE = '22023';
    END IF;

    FOR v_source_request, v_ordinal IN
        SELECT value, ordinality
        FROM jsonb_array_elements(p_request -> 'source_annotations') WITH ORDINALITY AS rows(value, ordinality)
        ORDER BY ordinality
    LOOP
        IF jsonb_typeof(v_source_request) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'Invalid Labeling cross-frame source fence'
                USING ERRCODE = '22023';
        END IF;
        SELECT array_agg(key ORDER BY key) INTO v_keys
        FROM jsonb_object_keys(v_source_request) AS request_keys(key);
        IF v_keys IS DISTINCT FROM ARRAY[
            'annotation_id', 'class_name', 'expected_scene_track_id',
            'expected_temporal_role', 'expected_track_id', 'expected_version',
            'geometry_type', 'project_id', 'target_track_id', 'task_id'
        ]::text[]
           OR jsonb_typeof(v_source_request -> 'annotation_id') IS DISTINCT FROM 'string'
           OR v_source_request ->> 'annotation_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(v_source_request -> 'task_id') IS DISTINCT FROM 'string'
           OR v_source_request ->> 'task_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(v_source_request -> 'project_id') NOT IN ('string', 'null')
           OR (jsonb_typeof(v_source_request -> 'project_id') = 'string'
               AND v_source_request ->> 'project_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR jsonb_typeof(v_source_request -> 'expected_version') IS DISTINCT FROM 'number'
           OR v_source_request ->> 'expected_version' !~ '^[1-9][0-9]*$'
           OR jsonb_typeof(v_source_request -> 'geometry_type') IS DISTINCT FROM 'string'
           OR jsonb_typeof(v_source_request -> 'class_name') IS DISTINCT FROM 'string'
           OR jsonb_typeof(v_source_request -> 'expected_track_id') NOT IN ('string', 'null')
           OR jsonb_typeof(v_source_request -> 'target_track_id') NOT IN ('string', 'null')
           OR jsonb_typeof(v_source_request -> 'expected_scene_track_id') NOT IN ('string', 'null')
           OR (jsonb_typeof(v_source_request -> 'expected_scene_track_id') = 'string'
               AND v_source_request ->> 'expected_scene_track_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR jsonb_typeof(v_source_request -> 'expected_temporal_role') IS DISTINCT FROM 'string'
           OR v_source_request ->> 'expected_temporal_role' NOT IN ('keyframe', 'derived', 'sample')
           OR v_source_request ->> 'geometry_type' NOT IN (
                'box_3d', 'bbox', 'polygon', 'multi_polygon',
                'rotated_bbox', 'polyline', 'keypoint'
           ) THEN
            RAISE EXCEPTION 'Invalid Labeling cross-frame source fence fields'
                USING ERRCODE = '22023';
        END IF;
        IF (v_source_request ->> 'expected_version')::bigint > 2147483647 THEN
            RAISE EXCEPTION 'Invalid Labeling cross-frame source version'
                USING ERRCODE = '22023';
        END IF;
        IF (v_source_request ->> 'annotation_id')::uuid = ANY(v_source_ids) THEN
            RAISE EXCEPTION 'Duplicate Labeling cross-frame source annotation'
                USING ERRCODE = '22023';
        END IF;
        v_source_ids := array_append(v_source_ids, (v_source_request ->> 'annotation_id')::uuid);
        v_source_count := v_source_count + 1;
        IF NOT ((v_source_request ->> 'task_id')::uuid = ANY(v_task_ids)) THEN
            RAISE EXCEPTION 'Cross-frame source is outside task fences'
                USING ERRCODE = '22023';
        END IF;
        IF v_source_request ->> 'expected_track_id' IS NULL
           AND jsonb_typeof(v_source_request -> 'expected_track_id') IS DISTINCT FROM 'null'
           OR v_source_request ->> 'target_track_id' IS NULL
           OR (jsonb_typeof(v_source_request -> 'expected_track_id') = 'string'
               AND v_source_request ->> 'expected_track_id' IS DISTINCT FROM v_source_request ->> 'target_track_id') THEN
            RAISE EXCEPTION 'Invalid Labeling cross-frame track identity'
                USING ERRCODE = '22023';
        END IF;
        IF jsonb_typeof(v_source_request -> 'expected_scene_track_id') = 'string' THEN
            v_scene_track_ids := array_append(
                v_scene_track_ids,
                (v_source_request ->> 'expected_scene_track_id')::uuid
            );
        END IF;
    END LOOP;
    IF v_source_count = 0 THEN
        RAISE EXCEPTION 'Cross-frame source annotations cannot be empty'
            USING ERRCODE = '22023';
    END IF;

    IF (v_operation = 'propagate' AND (v_source_count <> 1
            OR jsonb_array_length(p_request -> 'target_annotations') <> 1))
       OR (v_operation = 'propagate_batch' AND
            jsonb_array_length(p_request -> 'target_annotations') = 0)
       OR (v_operation = 'interpolate_range' AND v_source_count <> 2) THEN
        RAISE EXCEPTION 'Invalid Labeling cross-frame operation cardinality'
            USING ERRCODE = '22023';
    END IF;

    FOR v_target_request, v_ordinal IN
        SELECT value, ordinality
        FROM jsonb_array_elements(p_request -> 'target_annotations') WITH ORDINALITY AS rows(value, ordinality)
        ORDER BY ordinality
    LOOP
        IF jsonb_typeof(v_target_request) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'Invalid Labeling cross-frame target write'
                USING ERRCODE = '22023';
        END IF;
        SELECT array_agg(key ORDER BY key) INTO v_keys
        FROM jsonb_object_keys(v_target_request) AS request_keys(key);
        IF v_keys IS DISTINCT FROM ARRAY[
            'annotation_id', 'annotation_type', 'attributes', 'class_name',
            'geometry', 'project_id', 'source', 'source_annotation_id',
            'task_id', 'temporal_role', 'tool_unit_id', 'track_id',
            'video_segment_id'
        ]::text[]
           OR jsonb_typeof(v_target_request -> 'annotation_id') IS DISTINCT FROM 'string'
           OR v_target_request ->> 'annotation_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(v_target_request -> 'source_annotation_id') IS DISTINCT FROM 'string'
           OR v_target_request ->> 'source_annotation_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(v_target_request -> 'task_id') IS DISTINCT FROM 'string'
           OR v_target_request ->> 'task_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(v_target_request -> 'project_id') IS DISTINCT FROM 'string'
           OR v_target_request ->> 'project_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(v_target_request -> 'video_segment_id') NOT IN ('string', 'null')
           OR (jsonb_typeof(v_target_request -> 'video_segment_id') = 'string'
               AND v_target_request ->> 'video_segment_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR jsonb_typeof(v_target_request -> 'source') IS DISTINCT FROM 'string'
           OR jsonb_typeof(v_target_request -> 'annotation_type') IS DISTINCT FROM 'string'
           OR jsonb_typeof(v_target_request -> 'tool_unit_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(v_target_request -> 'class_name') IS DISTINCT FROM 'string'
           OR jsonb_typeof(v_target_request -> 'geometry') IS DISTINCT FROM 'object'
           OR jsonb_typeof(v_target_request -> 'track_id') NOT IN ('string', 'null')
           OR jsonb_typeof(v_target_request -> 'attributes') IS DISTINCT FROM 'object'
           OR jsonb_typeof(v_target_request -> 'temporal_role') IS DISTINCT FROM 'string'
           OR v_target_request ->> 'source' NOT IN ('manual', 'interpolated')
           OR v_target_request ->> 'temporal_role' NOT IN ('derived', 'sample') THEN
            RAISE EXCEPTION 'Invalid Labeling cross-frame target write fields'
                USING ERRCODE = '22023';
        END IF;
        IF (v_target_request ->> 'annotation_id')::uuid = ANY(v_target_ids) THEN
            RAISE EXCEPTION 'Duplicate Labeling cross-frame target annotation'
                USING ERRCODE = '22023';
        END IF;
        v_target_ids := array_append(v_target_ids, (v_target_request ->> 'annotation_id')::uuid);
        v_target_count := v_target_count + 1;
        IF NOT ((v_target_request ->> 'source_annotation_id')::uuid = ANY(v_source_ids))
           OR NOT ((v_target_request ->> 'task_id')::uuid = ANY(v_task_ids))
           OR (v_target_request ->> 'project_id')::uuid IS DISTINCT FROM v_project_id THEN
            RAISE EXCEPTION 'Cross-frame target is outside task fences'
                USING ERRCODE = '22023';
        END IF;
    END LOOP;

    IF v_operation IN ('propagate', 'propagate_batch') THEN
        FOR v_source_request, v_ordinal IN
            SELECT value, ordinality
            FROM jsonb_array_elements(p_request -> 'source_annotations') WITH ORDINALITY AS rows(value, ordinality)
            ORDER BY ordinality
        LOOP
            IF NOT EXISTS (
                SELECT 1
                FROM jsonb_array_elements(p_request -> 'target_annotations') AS rows(value)
                WHERE value ->> 'source_annotation_id' = v_source_request ->> 'annotation_id'
            ) THEN
                RAISE EXCEPTION 'Cross-frame source requires a target Annotation'
                    USING ERRCODE = '22023';
            END IF;
        END LOOP;
    END IF;

    FOR v_track_request, v_ordinal IN
        SELECT value, ordinality
        FROM jsonb_array_elements(p_request -> 'scene_tracks') WITH ORDINALITY AS rows(value, ordinality)
        ORDER BY ordinality
    LOOP
        IF jsonb_typeof(v_track_request) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'Invalid Labeling cross-frame SceneTrack ensure'
                USING ERRCODE = '22023';
        END IF;
        SELECT array_agg(key ORDER BY key) INTO v_keys
        FROM jsonb_object_keys(v_track_request) AS request_keys(key);
        IF v_keys IS DISTINCT FROM ARRAY[
            'class_name', 'frames', 'interval_source', 'project_id', 'scene_id', 'track_id'
        ]::text[]
           OR jsonb_typeof(v_track_request -> 'project_id') IS DISTINCT FROM 'string'
           OR v_track_request ->> 'project_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(v_track_request -> 'scene_id') IS DISTINCT FROM 'string'
           OR v_track_request ->> 'scene_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(v_track_request -> 'track_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(v_track_request -> 'class_name') IS DISTINCT FROM 'string'
           OR jsonb_typeof(v_track_request -> 'frames') IS DISTINCT FROM 'array'
           OR v_track_request ->> 'interval_source' IS DISTINCT FROM 'derived'
           OR (v_track_request ->> 'project_id')::uuid IS DISTINCT FROM v_project_id
           OR jsonb_array_length(v_track_request -> 'frames') = 0 THEN
            RAISE EXCEPTION 'Invalid Labeling cross-frame SceneTrack fields'
                USING ERRCODE = '22023';
        END IF;
        v_frames := ARRAY[]::integer[];
        FOR v_frame IN SELECT value FROM jsonb_array_elements(v_track_request -> 'frames') AS rows(value)
        LOOP
            IF jsonb_typeof(v_frame) IS DISTINCT FROM 'number'
               OR v_frame #>> '{}' !~ '^(0|[1-9][0-9]*)$'
               OR (v_frame #>> '{}')::bigint > 2147483647 THEN
                RAISE EXCEPTION 'Invalid Labeling cross-frame SceneTrack frame'
                    USING ERRCODE = '22023';
            END IF;
            v_frames := array_append(v_frames, (v_frame #>> '{}')::integer);
        END LOOP;
        v_scene_track_count := v_scene_track_count + 1;
    END LOOP;

    IF v_operation IN ('propagate', 'propagate_batch') THEN
        SELECT count(*)::integer INTO v_expected_scene_count
        FROM jsonb_array_elements(p_request -> 'target_annotations') AS rows(value)
        WHERE value -> 'geometry' ->> 'type' = 'box_3d';
        IF v_scene_track_count <> v_expected_scene_count
           OR (v_operation = 'propagate' AND v_target_count <> 1) THEN
            RAISE EXCEPTION 'Invalid Labeling cross-frame SceneTrack scope'
                USING ERRCODE = '22023';
        END IF;
        v_scene_ordinal := 0;
        FOR v_target_request IN
            SELECT value
            FROM jsonb_array_elements(p_request -> 'target_annotations') AS rows(value)
            WHERE value -> 'geometry' ->> 'type' = 'box_3d'
        LOOP
            v_scene_ordinal := v_scene_ordinal + 1;
            SELECT value INTO v_track_request
            FROM jsonb_array_elements(p_request -> 'scene_tracks') WITH ORDINALITY AS rows(value, ordinality)
            WHERE ordinality = v_scene_ordinal;
            SELECT value INTO v_source_request
            FROM jsonb_array_elements(p_request -> 'source_annotations') AS rows(value)
            WHERE value ->> 'annotation_id' = v_target_request ->> 'source_annotation_id';
            IF v_track_request ->> 'track_id' IS DISTINCT FROM v_source_request ->> 'target_track_id'
               OR v_track_request ->> 'class_name' IS DISTINCT FROM v_source_request ->> 'class_name'
               OR v_track_request ->> 'project_id' IS DISTINCT FROM v_project_id::text THEN
                RAISE EXCEPTION 'SceneTrack ensure does not match propagated source'
                    USING ERRCODE = '22023';
            END IF;
        END LOOP;
    ELSIF v_scene_track_count <> 1 OR EXISTS (
        SELECT 1 FROM jsonb_array_elements(p_request -> 'source_annotations') AS rows(value)
        WHERE value ->> 'geometry_type' <> 'box_3d'
           OR value ->> 'expected_track_id' IS DISTINCT FROM value ->> 'target_track_id'
    ) THEN
        RAISE EXCEPTION 'Invalid Labeling interpolation SceneTrack scope'
            USING ERRCODE = '22023';
    ELSE
        SELECT value INTO v_track_request
        FROM jsonb_array_elements(p_request -> 'scene_tracks') WITH ORDINALITY AS rows(value, ordinality)
        WHERE ordinality = 1;
        SELECT value INTO v_source_request
        FROM jsonb_array_elements(p_request -> 'source_annotations') WITH ORDINALITY AS rows(value, ordinality)
        WHERE ordinality = 1;
        IF v_track_request ->> 'track_id' IS DISTINCT FROM v_source_request ->> 'target_track_id'
           OR v_track_request ->> 'class_name' IS DISTINCT FROM v_source_request ->> 'class_name'
           OR v_track_request ->> 'project_id' IS DISTINCT FROM v_project_id::text THEN
            RAISE EXCEPTION 'Interpolation SceneTrack ensure does not match endpoint'
                USING ERRCODE = '22023';
        END IF;
    END IF;

    -- Lock every participating Task, Annotation, then existing SceneTrack in stable order.
    FOR v_task IN
        SELECT task.id, task.project_id, task.version
        FROM public.tasks AS task
        WHERE task.id = ANY(v_task_ids)
        ORDER BY task.id
        FOR UPDATE
    LOOP
        v_locked_count := v_locked_count + 1;
        SELECT value INTO v_task_request
        FROM jsonb_array_elements(p_request -> 'task_fences') AS rows(value)
        WHERE value ->> 'task_id' = v_task.id::text;
        IF NOT FOUND OR v_task.project_id IS DISTINCT FROM (v_task_request ->> 'project_id')::uuid THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_labeling_cross_frame_write_error',
                DETAIL = jsonb_build_object('status_code', 409, 'detail',
                    jsonb_build_object('reason', 'task_target_changed'))::text;
        END IF;
        v_expected_version := (v_task_request ->> 'expected_version')::integer;
        IF v_task.version IS DISTINCT FROM v_expected_version THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_labeling_cross_frame_write_error',
                DETAIL = jsonb_build_object('status_code', 409, 'detail',
                    jsonb_build_object('reason', 'task_version_conflict', 'task_id', v_task.id))::text;
        END IF;
    END LOOP;
    IF v_locked_count <> v_task_count THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001',
            MESSAGE = 'aap_labeling_cross_frame_write_error',
            DETAIL = jsonb_build_object('status_code', 404, 'detail', 'Task not found')::text;
    END IF;

    v_locked_count := 0;
    FOR v_source IN
        SELECT annotation.*
        FROM public.annotations AS annotation
        WHERE annotation.id = ANY(v_source_ids)
        ORDER BY annotation.id
        FOR UPDATE
    LOOP
        v_locked_count := v_locked_count + 1;
        SELECT value INTO v_source_request
        FROM jsonb_array_elements(p_request -> 'source_annotations') AS rows(value)
        WHERE value ->> 'annotation_id' = v_source.id::text;
        IF NOT FOUND OR v_source.is_active IS NOT TRUE THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_labeling_cross_frame_write_error',
                DETAIL = jsonb_build_object('status_code', 404,
                    'detail', 'source annotation not found')::text;
        END IF;
        IF v_source.version IS DISTINCT FROM (v_source_request ->> 'expected_version')::integer
           OR (v_source.geometry ->> 'type') IS DISTINCT FROM v_source_request ->> 'geometry_type'
           OR v_source.class_name IS DISTINCT FROM v_source_request ->> 'class_name'
           OR v_source.track_id IS DISTINCT FROM v_source_request ->> 'expected_track_id'
           OR v_source.scene_track_id IS DISTINCT FROM NULLIF(v_source_request ->> 'expected_scene_track_id', '')::uuid
           OR v_source.temporal_role IS DISTINCT FROM v_source_request ->> 'expected_temporal_role'
           OR v_source.task_id IS DISTINCT FROM (v_source_request ->> 'task_id')::uuid
           OR v_source.project_id IS DISTINCT FROM NULLIF(v_source_request ->> 'project_id', '')::uuid THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_labeling_cross_frame_write_error',
                DETAIL = jsonb_build_object('status_code', 409, 'detail',
                    jsonb_build_object('reason', 'annotation_version_conflict',
                        'annotation_id', v_source.id))::text;
        END IF;
    END LOOP;
    IF v_locked_count <> v_source_count THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001',
            MESSAGE = 'aap_labeling_cross_frame_write_error',
            DETAIL = jsonb_build_object('status_code', 404,
                'detail', 'source annotation not found')::text;
    END IF;

    FOR v_track IN
        SELECT track.id
        FROM public.scene_tracks AS track
        WHERE track.id = ANY(v_scene_track_ids)
        ORDER BY track.id
        FOR UPDATE
    LOOP
        NULL;
    END LOOP;

    -- Validate each prepared create against its locked source and target task.
    FOR v_target_request, v_ordinal IN
        SELECT value, ordinality
        FROM jsonb_array_elements(p_request -> 'target_annotations') WITH ORDINALITY AS rows(value, ordinality)
        ORDER BY ordinality
    LOOP
        SELECT * INTO v_source
        FROM public.annotations AS annotation
        WHERE annotation.id = (v_target_request ->> 'source_annotation_id')::uuid;
        v_role := 'manual';
        IF v_operation = 'interpolate_range' THEN
            v_role := 'interpolated';
        END IF;
        IF v_target_request ->> 'source' IS DISTINCT FROM v_role
           OR v_target_request ->> 'task_id' = v_source.task_id::text
           OR v_target_request ->> 'annotation_type' IS DISTINCT FROM v_source.annotation_type
           OR v_target_request ->> 'tool_unit_id' IS DISTINCT FROM v_source.tool_unit_id
           OR v_target_request ->> 'class_name' IS DISTINCT FROM v_source.class_name
           OR v_target_request -> 'attributes' IS DISTINCT FROM COALESCE(v_source.attributes, '{}'::jsonb)
           OR v_target_request ->> 'project_id' IS DISTINCT FROM v_project_id::text THEN
            RAISE EXCEPTION 'Cross-frame target does not match its source'
                USING ERRCODE = '22023';
        END IF;
        IF v_operation IN ('propagate', 'propagate_batch') THEN
            SELECT value INTO v_source_request
            FROM jsonb_array_elements(p_request -> 'source_annotations') AS rows(value)
            WHERE value ->> 'annotation_id' = v_source.id::text;
            v_role := 'sample';
            IF v_source_request ->> 'geometry_type' = 'box_3d' THEN
                v_role := 'derived';
            END IF;
            IF v_target_request -> 'geometry' ->> 'type' IS DISTINCT FROM v_source_request ->> 'geometry_type'
               OR v_target_request ->> 'track_id' IS DISTINCT FROM v_source_request ->> 'target_track_id'
               OR v_target_request ->> 'temporal_role' IS DISTINCT FROM v_role
               OR v_target_request ->> 'video_segment_id' IS DISTINCT FROM v_source.video_segment_id::text
               OR (v_source_request ->> 'geometry_type' <> 'box_3d'
                   AND v_target_request -> 'geometry' IS DISTINCT FROM v_source.geometry)
               OR v_target_request ->> 'source_annotation_id' IS DISTINCT FROM v_source.id::text THEN
                RAISE EXCEPTION 'Invalid propagated Annotation fields'
                    USING ERRCODE = '22023';
            END IF;
        ELSE
            SELECT value INTO v_source_request
            FROM jsonb_array_elements(p_request -> 'source_annotations') AS rows(value)
            WHERE value ->> 'annotation_id' = (p_request -> 'target_annotations' -> 0 ->> 'source_annotation_id');
            IF v_target_request -> 'geometry' ->> 'type' IS DISTINCT FROM 'box_3d'
               OR v_target_request ->> 'track_id' IS DISTINCT FROM v_source_request ->> 'target_track_id'
               OR v_target_request ->> 'temporal_role' IS DISTINCT FROM 'derived'
               OR jsonb_typeof(v_target_request -> 'video_segment_id') IS DISTINCT FROM 'null' THEN
                RAISE EXCEPTION 'Invalid interpolated Annotation fields'
                    USING ERRCODE = '22023';
            END IF;
        END IF;
        IF (v_target_request ->> 'annotation_id')::uuid = ANY(v_source_ids)
           OR EXISTS (
               SELECT 1 FROM public.annotations AS annotation
               WHERE annotation.id = (v_target_request ->> 'annotation_id')::uuid
           ) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_labeling_cross_frame_write_error',
                DETAIL = jsonb_build_object('status_code', 409, 'detail',
                    jsonb_build_object('reason', 'annotation_id_conflict'))::text;
        END IF;
    END LOOP;

    IF v_operation IN ('propagate', 'propagate_batch') THEN
        v_scene_ordinal := 0;
        FOR v_target_request, v_ordinal IN
            SELECT value, ordinality
            FROM jsonb_array_elements(p_request -> 'target_annotations') WITH ORDINALITY AS rows(value, ordinality)
            ORDER BY ordinality
        LOOP
            SELECT value INTO v_source_request
            FROM jsonb_array_elements(p_request -> 'source_annotations') AS rows(value)
            WHERE value ->> 'annotation_id' = v_target_request ->> 'source_annotation_id';
            SELECT * INTO v_source
            FROM public.annotations AS annotation
            WHERE annotation.id = (v_source_request ->> 'annotation_id')::uuid;
            SELECT COALESCE(array_agg(value), ARRAY[]::text[])
            INTO v_written_fields
            FROM jsonb_array_elements_text(
                COALESCE(v_source_field_map -> v_source.id::text, '[]'::jsonb)
            ) AS fields(value);

            IF jsonb_typeof(v_source_request -> 'expected_track_id') = 'null'
               AND v_source.track_id IS NULL THEN
                UPDATE public.annotations AS annotation
                SET track_id = v_source_request ->> 'target_track_id',
                    version = COALESCE(annotation.version, 1) + 1,
                    updated_at = transaction_timestamp()
                WHERE annotation.id = v_source.id
                  AND annotation.version = (v_source_request ->> 'expected_version')::integer
                RETURNING * INTO v_source;
                IF NOT FOUND THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001',
                        MESSAGE = 'aap_labeling_cross_frame_write_error',
                        DETAIL = jsonb_build_object('status_code', 409, 'detail',
                            jsonb_build_object('reason', 'annotation_version_conflict',
                                'annotation_id', v_source.id))::text;
                END IF;
                v_written_fields := ARRAY['track_id', 'version', 'updated_at']::text[];
            END IF;

            INSERT INTO public.annotations (
                id, task_id, project_id, video_segment_id, user_id, source,
                annotation_type, tool_unit_id, class_name, geometry, track_id,
                temporal_role, attributes
            ) VALUES (
                (v_target_request ->> 'annotation_id')::uuid,
                (v_target_request ->> 'task_id')::uuid,
                (v_target_request ->> 'project_id')::uuid,
                NULLIF(v_target_request ->> 'video_segment_id', '')::uuid,
                v_actor_id,
                v_target_request ->> 'source',
                v_target_request ->> 'annotation_type',
                v_target_request ->> 'tool_unit_id',
                v_target_request ->> 'class_name',
                v_target_request -> 'geometry',
                v_target_request ->> 'track_id',
                v_target_request ->> 'temporal_role',
                v_target_request -> 'attributes'
            ) RETURNING * INTO v_inserted;

            IF v_target_request -> 'geometry' ->> 'type' = 'box_3d' THEN
                v_scene_ordinal := v_scene_ordinal + 1;
                SELECT value INTO v_track_request
                FROM jsonb_array_elements(p_request -> 'scene_tracks') WITH ORDINALITY AS rows(value, ordinality)
                WHERE ordinality = v_scene_ordinal;
                v_scene_reply := aap_labeling_v1.scene_track_domain_command(
                    v_track_request || jsonb_build_object(
                        'schema', 'aap-labeling-scene-track-domain/v1',
                        'action', 'ensure',
                        'actor_id', v_actor_id,
                        'member_change', true
                    )
                );
                IF v_scene_reply ->> 'ok' IS DISTINCT FROM 'true' THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001',
                        MESSAGE = 'aap_labeling_cross_frame_write_error',
                        DETAIL = jsonb_build_object('status_code', 409, 'detail',
                            jsonb_build_object(
                                'reason', v_scene_reply #>> '{error,code}',
                                'message', v_scene_reply #>> '{error,message}'
                            ))::text;
                END IF;
                v_track_uuid := (v_scene_reply #>> '{data,track,id}')::uuid;
                v_scene_tracks := v_scene_tracks || jsonb_build_array(jsonb_build_object(
                    'track', v_scene_reply #> '{data,track}',
                    'changed', v_scene_reply #> '{data,changed}'
                ));

                IF v_source.scene_track_id IS DISTINCT FROM v_track_uuid
                   OR v_source.temporal_role IS DISTINCT FROM 'keyframe' THEN
                    UPDATE public.annotations AS annotation
                    SET scene_track_id = v_track_uuid,
                        temporal_role = 'keyframe',
                        version = COALESCE(annotation.version, 1)
                            + CASE WHEN annotation.temporal_role IS DISTINCT FROM 'keyframe' THEN 1 ELSE 0 END,
                        updated_at = transaction_timestamp()
                    WHERE annotation.id = v_source.id
                    RETURNING * INTO v_source;
                    v_written_fields := v_written_fields || ARRAY[
                        'scene_track_id', 'temporal_role', 'version', 'updated_at'
                    ]::text[];
                END IF;
                UPDATE public.annotations AS annotation
                SET scene_track_id = v_track_uuid,
                    updated_at = transaction_timestamp()
                WHERE annotation.id = v_inserted.id
                  AND annotation.scene_track_id IS DISTINCT FROM v_track_uuid
                RETURNING * INTO v_inserted;
            END IF;
            v_source_field_map := jsonb_set(
                v_source_field_map,
                ARRAY[v_source.id::text],
                to_jsonb(v_written_fields),
                true
            );
            v_created := v_created || jsonb_build_array(to_jsonb(v_inserted));
        END LOOP;
    ELSE
        FOR v_target_request, v_ordinal IN
            SELECT value, ordinality
            FROM jsonb_array_elements(p_request -> 'target_annotations') WITH ORDINALITY AS rows(value, ordinality)
            ORDER BY ordinality
        LOOP
            INSERT INTO public.annotations (
                id, task_id, project_id, user_id, source,
                annotation_type, tool_unit_id, class_name, geometry, track_id,
                temporal_role, attributes
            ) VALUES (
                (v_target_request ->> 'annotation_id')::uuid,
                (v_target_request ->> 'task_id')::uuid,
                (v_target_request ->> 'project_id')::uuid,
                v_actor_id,
                'interpolated',
                v_target_request ->> 'annotation_type',
                v_target_request ->> 'tool_unit_id',
                v_target_request ->> 'class_name',
                v_target_request -> 'geometry',
                v_target_request ->> 'track_id',
                'derived',
                v_target_request -> 'attributes'
            ) RETURNING * INTO v_inserted;
            v_created := v_created || jsonb_build_array(to_jsonb(v_inserted));
        END LOOP;

        SELECT value INTO v_track_request
        FROM jsonb_array_elements(p_request -> 'scene_tracks') WITH ORDINALITY AS rows(value, ordinality)
        WHERE ordinality = 1;
        v_scene_reply := aap_labeling_v1.scene_track_domain_command(
            v_track_request || jsonb_build_object(
                'schema', 'aap-labeling-scene-track-domain/v1',
                'action', 'ensure',
                'actor_id', v_actor_id,
                'member_change', true
            )
        );
        IF v_scene_reply ->> 'ok' IS DISTINCT FROM 'true' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_labeling_cross_frame_write_error',
                DETAIL = jsonb_build_object('status_code', 409, 'detail',
                    jsonb_build_object(
                        'reason', v_scene_reply #>> '{error,code}',
                        'message', v_scene_reply #>> '{error,message}'
                    ))::text;
        END IF;
        v_track_uuid := (v_scene_reply #>> '{data,track,id}')::uuid;
        v_scene_tracks := v_scene_tracks || jsonb_build_array(jsonb_build_object(
            'track', v_scene_reply #> '{data,track}',
            'changed', v_scene_reply #> '{data,changed}'
        ));

        FOR v_source_request, v_ordinal IN
            SELECT value, ordinality
            FROM jsonb_array_elements(p_request -> 'source_annotations') WITH ORDINALITY AS rows(value, ordinality)
            ORDER BY ordinality
        LOOP
            SELECT * INTO v_source
            FROM public.annotations AS annotation
            WHERE annotation.id = (v_source_request ->> 'annotation_id')::uuid;
            v_written_fields := ARRAY[]::text[];
            IF v_source.scene_track_id IS DISTINCT FROM v_track_uuid
               OR v_source.temporal_role IS DISTINCT FROM 'keyframe' THEN
                UPDATE public.annotations AS annotation
                SET scene_track_id = v_track_uuid,
                    temporal_role = 'keyframe',
                    version = COALESCE(annotation.version, 1)
                        + CASE WHEN annotation.temporal_role IS DISTINCT FROM 'keyframe' THEN 1 ELSE 0 END,
                    updated_at = transaction_timestamp()
                WHERE annotation.id = v_source.id
                RETURNING * INTO v_source;
                v_written_fields := ARRAY[
                    'scene_track_id', 'temporal_role', 'version', 'updated_at'
                ]::text[];
            END IF;
            v_source_field_map := jsonb_set(
                v_source_field_map,
                ARRAY[v_source.id::text],
                to_jsonb(v_written_fields),
                true
            );
        END LOOP;
        FOR v_target_request, v_ordinal IN
            SELECT value, ordinality
            FROM jsonb_array_elements(p_request -> 'target_annotations') WITH ORDINALITY AS rows(value, ordinality)
            ORDER BY ordinality
        LOOP
            UPDATE public.annotations AS annotation
            SET scene_track_id = v_track_uuid,
                updated_at = transaction_timestamp()
            WHERE annotation.id = (v_target_request ->> 'annotation_id')::uuid
              AND annotation.scene_track_id IS DISTINCT FROM v_track_uuid
            RETURNING * INTO v_inserted;
            v_created := jsonb_set(
                v_created,
                ARRAY[(v_ordinal - 1)::text],
                to_jsonb(v_inserted),
                false
            );
        END LOOP;
    END IF;

    FOR v_source_request, v_ordinal IN
        SELECT value, ordinality
        FROM jsonb_array_elements(p_request -> 'source_annotations') WITH ORDINALITY AS rows(value, ordinality)
        ORDER BY ordinality
    LOOP
        SELECT * INTO v_source
        FROM public.annotations AS annotation
        WHERE annotation.id = (v_source_request ->> 'annotation_id')::uuid;
        v_sources := v_sources || jsonb_build_array(jsonb_build_object(
            'annotation_id', v_source.id,
            'version', v_source.version,
            'track_id', v_source.track_id,
            'scene_track_id', v_source.scene_track_id,
            'temporal_role', v_source.temporal_role,
            'updated_at', v_source.updated_at,
            'written_fields', COALESCE(v_source_field_map -> v_source.id::text, '[]'::jsonb)
        ));
    END LOOP;

    RETURN jsonb_build_object(
        'schema', 'aap-labeling-cross-frame-writes/v1',
        'ok', true,
        'request', p_request,
        'data', jsonb_build_object(
            'annotations', v_created,
            'sources', v_sources,
            'scene_tracks', v_scene_tracks
        )
    );
END;
$function$;

-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.cross_frame_write_command(jsonb) FROM PUBLIC;
