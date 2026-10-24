CREATE FUNCTION aap_labeling_v1.annotation_write_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_operation text;
    v_keys text[];
    v_expected_keys text[];
    v_key text;
    v_task_id uuid;
    v_project_id uuid;
    v_actor_id uuid;
    v_annotation_id uuid;
    v_parent_annotation_id uuid;
    v_parent_prediction_id uuid;
    v_video_segment_id uuid;
    v_scene_id uuid;
    v_scene_track_id uuid;
    v_task_project_id uuid;
    v_expected_version integer;
    v_annotation record;
    v_parent record;
    v_operation_row record;
    v_operation_id uuid;
    v_geometry jsonb;
    v_attributes jsonb;
    v_attributes_meta jsonb;
    v_class_name text;
    v_track_id text;
    v_temporal_role text;
    v_confidence double precision;
    v_lead_time double precision;
    v_frame_index integer;
    v_scene_reply jsonb;
    v_reply_data jsonb;
    v_request_digest text;
    v_idempotency_key text;
    v_track_reclassified boolean := false;
    v_deleted_children integer := 0;
    v_deleted_camera_members integer := 0;
    v_child_id uuid;
    v_camera_id uuid;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-annotation-writes/v1'
       OR jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Labeling Annotation write request'
            USING ERRCODE = '22023';
    END IF;

    v_operation := p_request ->> 'operation';
    CASE v_operation
        WHEN 'create' THEN
            v_expected_keys := ARRAY[
                'actor_id', 'annotation_id', 'annotation_type', 'attributes',
                'class_name', 'confidence', 'geometry',
                'lead_time', 'operation', 'parent_annotation_id',
                'parent_prediction_id', 'project_id', 'schema',
                'task_id', 'tool_unit_id', 'track_id', 'video_segment_id'
            ]::text[];
        WHEN 'update' THEN
            v_expected_keys := ARRAY[
                'actor_id', 'annotation_id', 'expected_version', 'operation',
                'project_id', 'schema', 'task_id', 'track_id'
            ]::text[];
            FOREACH v_key IN ARRAY ARRAY[
                'attributes', 'attributes_meta', 'class_name', 'confidence',
                'geometry', 'is_hidden', 'is_locked', 'z_order'
            ]::text[] LOOP
                IF p_request ? v_key THEN
                    v_expected_keys := array_append(v_expected_keys, v_key);
                END IF;
            END LOOP;
        WHEN 'bind_scene_track_member' THEN
            v_expected_keys := ARRAY[
                'actor_id', 'annotation_id', 'expected_version', 'frame_index',
                'operation', 'project_id', 'scene_id', 'schema', 'task_id',
                'temporal_role'
            ]::text[];
        WHEN 'delete' THEN
            v_expected_keys := ARRAY[
                'actor_id', 'annotation_id', 'expected_version', 'operation',
                'project_id', 'schema', 'task_id'
            ]::text[];
        WHEN 'read_create_receipt' THEN
            v_expected_keys := ARRAY[
                'actor_id', 'idempotency_key', 'operation', 'project_id',
                'request_digest', 'schema', 'task_id'
            ]::text[];
        WHEN 'write_create_receipt' THEN
            v_expected_keys := ARRAY[
                'actor_id', 'idempotency_key', 'operation', 'operation_id', 'project_id',
                'request_digest', 'response_json', 'schema', 'task_id'
            ]::text[];
        ELSE
            RAISE EXCEPTION 'Invalid Labeling Annotation write operation'
                USING ERRCODE = '22023';
    END CASE;

    SELECT array_agg(key ORDER BY key) INTO v_keys
    FROM jsonb_object_keys(p_request) AS keys(key);
    SELECT array_agg(key ORDER BY key) INTO v_expected_keys
    FROM unnest(v_expected_keys) AS expected(key);
    IF v_keys IS DISTINCT FROM v_expected_keys THEN
        RAISE EXCEPTION 'Invalid Labeling Annotation write fields'
            USING ERRCODE = '22023';
    END IF;

    FOREACH v_key IN ARRAY ARRAY['task_id', 'project_id']::text[] LOOP
        IF jsonb_typeof(p_request -> v_key) IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid Labeling Annotation write identity'
                USING ERRCODE = '22023';
        END IF;
    END LOOP;
    v_task_id := (p_request ->> 'task_id')::uuid;
    v_project_id := (p_request ->> 'project_id')::uuid;
    IF jsonb_typeof(p_request -> 'actor_id') NOT IN ('string', 'null')
       OR (v_operation IN ('create', 'read_create_receipt', 'write_create_receipt')
           AND jsonb_typeof(p_request -> 'actor_id') IS DISTINCT FROM 'string') THEN
        RAISE EXCEPTION 'Invalid Annotation actor identity' USING ERRCODE = '22023';
    END IF;
    v_actor_id := (p_request ->> 'actor_id')::uuid;

    IF v_operation IN ('create', 'update', 'bind_scene_track_member', 'delete') THEN
        IF jsonb_typeof(p_request -> 'annotation_id') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid Annotation identifier' USING ERRCODE = '22023';
        END IF;
        v_annotation_id := (p_request ->> 'annotation_id')::uuid;
        SELECT task.project_id INTO v_task_project_id
        FROM public.tasks AS task
        WHERE task.id = v_task_id
        FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_annotation_write_error',
                DETAIL = jsonb_build_object(
                    'status_code', 404, 'detail', 'Task not found'
                )::text;
        END IF;
        IF v_task_project_id IS DISTINCT FROM v_project_id THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_annotation_write_error',
                DETAIL = jsonb_build_object(
                    'status_code', 409, 'detail', jsonb_build_object(
                        'reason', 'task_target_changed'
                    )
                )::text;
        END IF;
    ELSE
        SELECT task.project_id INTO v_task_project_id
        FROM public.tasks AS task
        WHERE task.id = v_task_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_annotation_write_error',
                DETAIL = jsonb_build_object(
                    'status_code', 404, 'detail', 'Task not found'
                )::text;
        END IF;
        IF v_task_project_id IS DISTINCT FROM v_project_id THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_annotation_write_error',
                DETAIL = jsonb_build_object(
                    'status_code', 409, 'detail', jsonb_build_object(
                        'reason', 'task_target_changed'
                    )
                )::text;
        END IF;
    END IF;

    IF v_operation = 'create' THEN
        IF jsonb_typeof(p_request -> 'annotation_type') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'tool_unit_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'class_name') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'geometry') IS DISTINCT FROM 'object'
           OR jsonb_typeof(p_request -> 'attributes') IS DISTINCT FROM 'object'
           OR jsonb_typeof(p_request -> 'confidence') NOT IN ('number', 'null')
           OR jsonb_typeof(p_request -> 'lead_time') NOT IN ('number', 'null')
           OR jsonb_typeof(p_request -> 'parent_prediction_id') NOT IN ('string', 'null')
           OR jsonb_typeof(p_request -> 'parent_annotation_id') NOT IN ('string', 'null')
           OR jsonb_typeof(p_request -> 'video_segment_id') NOT IN ('string', 'null')
           OR jsonb_typeof(p_request -> 'track_id') NOT IN ('string', 'null') THEN
            RAISE EXCEPTION 'Invalid Annotation create fields' USING ERRCODE = '22023';
        END IF;
        v_geometry := p_request -> 'geometry';
        v_attributes := p_request -> 'attributes';
        v_class_name := p_request ->> 'class_name';
        v_track_id := p_request ->> 'track_id';
        v_parent_prediction_id := (p_request ->> 'parent_prediction_id')::uuid;
        v_parent_annotation_id := (p_request ->> 'parent_annotation_id')::uuid;
        v_video_segment_id := (p_request ->> 'video_segment_id')::uuid;
        v_confidence := (p_request ->> 'confidence')::double precision;
        v_lead_time := (p_request ->> 'lead_time')::double precision;
        IF v_geometry ->> 'type' = 'box_3d'
           AND (v_track_id IS NULL OR length(v_track_id) = 0 OR length(v_track_id) > 64) THEN
            RAISE EXCEPTION 'Invalid Annotation Scene Track identity' USING ERRCODE = '22023';
        END IF;
        IF v_parent_annotation_id IS NOT NULL THEN
            SELECT annotation.task_id, annotation.video_segment_id,
                   annotation.parent_annotation_id, annotation.is_active
            INTO v_parent
            FROM public.annotations AS annotation
            WHERE annotation.id = v_parent_annotation_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001',
                    MESSAGE = 'aap_annotation_write_error',
                    DETAIL = jsonb_build_object(
                        'status_code', 400,
                        'detail', format('parent annotation %s not found or inactive', v_parent_annotation_id)
                    )::text;
            END IF;
            IF v_parent.is_active IS NOT TRUE THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001',
                    MESSAGE = 'aap_annotation_write_error',
                    DETAIL = jsonb_build_object(
                        'status_code', 400,
                        'detail', format('parent annotation %s not found or inactive', v_parent_annotation_id)
                    )::text;
            END IF;
            IF v_parent.task_id IS DISTINCT FROM v_task_id THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001',
                    MESSAGE = 'aap_annotation_write_error',
                    DETAIL = jsonb_build_object(
                        'status_code', 400,
                        'detail', 'parent annotation must belong to the same task (parent-child is frame-internal)'
                    )::text;
            END IF;
            IF v_parent.video_segment_id IS DISTINCT FROM v_video_segment_id THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001',
                    MESSAGE = 'aap_annotation_write_error',
                    DETAIL = jsonb_build_object(
                        'status_code', 400,
                        'detail', 'parent annotation must belong to the same video segment'
                    )::text;
            END IF;
            IF v_parent.parent_annotation_id IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001',
                    MESSAGE = 'aap_annotation_write_error',
                    DETAIL = jsonb_build_object(
                        'status_code', 400,
                        'detail', 'parent-child nesting is limited to one level'
                    )::text;
            END IF;
        END IF;

        INSERT INTO public.annotations (
            id, task_id, project_id, video_segment_id, user_id, source,
            annotation_type, tool_unit_id, class_name, geometry, confidence,
            parent_prediction_id, parent_annotation_id, track_id, scene_track_id,
            temporal_role, lead_time, was_cancelled, ground_truth, is_active,
            attributes, attributes_meta, z_order, is_locked, is_hidden, version
        ) VALUES (
            v_annotation_id, v_task_id, v_project_id, v_video_segment_id, v_actor_id,
            CASE WHEN v_parent_prediction_id IS NOT NULL THEN 'prediction_based' ELSE 'manual' END,
            COALESCE(NULLIF(v_geometry ->> 'type', ''), p_request ->> 'annotation_type'),
            p_request ->> 'tool_unit_id', v_class_name, v_geometry, v_confidence,
            v_parent_prediction_id, v_parent_annotation_id, v_track_id, NULL,
            CASE WHEN v_geometry ->> 'type' = 'box_3d' THEN 'keyframe' ELSE 'sample' END,
            v_lead_time, false, false, true, v_attributes, '{}'::jsonb,
            0, false, false, 1
        );

        v_reply_data := jsonb_build_object(
            'mutation', jsonb_build_object(
                'annotation_id', v_annotation_id, 'version', 1, 'changed', true,
                'deleted_children', 0, 'deleted_camera_members', 0
            ),
            'receipt', NULL
        );
    ELSIF v_operation = 'update' THEN
        IF jsonb_typeof(p_request -> 'expected_version') IS DISTINCT FROM 'number'
           OR p_request ->> 'expected_version' !~ '^[1-9][0-9]*$'
           OR jsonb_typeof(p_request -> 'track_id') NOT IN ('string', 'null') THEN
            RAISE EXCEPTION 'Invalid Annotation update fence' USING ERRCODE = '22023';
        END IF;
        v_expected_version := (p_request ->> 'expected_version')::integer;
        SELECT annotation.class_name, annotation.geometry, annotation.id, annotation.is_active, annotation.is_locked, annotation.project_id, annotation.scene_track_id, annotation.sensor_role, annotation.task_id, annotation.temporal_role, annotation.track_id, annotation.version INTO v_annotation
        FROM public.annotations AS annotation
        WHERE annotation.id = v_annotation_id
        FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_annotation_write_error',
                DETAIL = jsonb_build_object('status_code', 404, 'detail', 'Annotation not found')::text;
        END IF;
        IF v_annotation.task_id IS DISTINCT FROM v_task_id
           OR (v_annotation.project_id IS NOT NULL
               AND v_annotation.project_id IS DISTINCT FROM v_project_id)
           OR v_annotation.is_active IS NOT TRUE THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_annotation_write_error',
                DETAIL = jsonb_build_object('status_code', 404, 'detail', 'Annotation not found')::text;
        END IF;
        IF v_annotation.sensor_role IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_annotation_write_error',
                DETAIL = jsonb_build_object(
                    'status_code', 422,
                    'detail', 'camera members must be edited through the point-cloud camera member API'
                )::text;
        END IF;
        IF v_annotation.version IS DISTINCT FROM v_expected_version THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_annotation_write_error',
                DETAIL = jsonb_build_object(
                    'status_code', 409, 'detail', jsonb_build_object(
                        'reason', 'version_mismatch', 'current_version', v_annotation.version
                    )
                )::text;
        END IF;
        IF p_request ? 'geometry'
           AND jsonb_typeof(p_request -> 'geometry') NOT IN ('object', 'null') THEN
            RAISE EXCEPTION 'Invalid Annotation geometry patch' USING ERRCODE = '22023';
        END IF;
        IF p_request ? 'geometry'
           AND jsonb_typeof(p_request -> 'geometry') = 'object'
           AND v_annotation.is_locked THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_annotation_write_error',
                DETAIL = jsonb_build_object(
                    'status_code', 409,
                    'detail', jsonb_build_object('reason', 'annotation_locked')
                )::text;
        END IF;
        IF p_request ? 'class_name'
           AND jsonb_typeof(p_request -> 'class_name') NOT IN ('string', 'null')
           OR p_request ? 'confidence'
           AND jsonb_typeof(p_request -> 'confidence') NOT IN ('number', 'null')
           OR p_request ? 'attributes'
           AND jsonb_typeof(p_request -> 'attributes') NOT IN ('object', 'null')
           OR p_request ? 'attributes_meta'
           AND jsonb_typeof(p_request -> 'attributes_meta') NOT IN ('object', 'null')
           OR p_request ? 'z_order'
           AND (jsonb_typeof(p_request -> 'z_order') NOT IN ('number', 'null')
                OR (p_request ->> 'z_order') IS NOT NULL AND p_request ->> 'z_order' !~ '^-?[0-9]+$')
           OR p_request ? 'is_locked'
           AND jsonb_typeof(p_request -> 'is_locked') NOT IN ('boolean', 'null')
           OR p_request ? 'is_hidden'
           AND jsonb_typeof(p_request -> 'is_hidden') NOT IN ('boolean', 'null') THEN
            RAISE EXCEPTION 'Invalid Annotation update fields' USING ERRCODE = '22023';
        END IF;

        v_geometry := CASE WHEN jsonb_typeof(p_request -> 'geometry') = 'object'
            THEN p_request -> 'geometry' ELSE NULL END;
        v_class_name := CASE WHEN jsonb_typeof(p_request -> 'class_name') = 'string'
            THEN p_request ->> 'class_name' ELSE NULL END;
        v_track_id := p_request ->> 'track_id';
        IF v_annotation.scene_track_id IS NOT NULL
           AND v_class_name IS NOT NULL
           AND v_class_name IS DISTINCT FROM v_annotation.class_name THEN
            v_scene_reply := aap_labeling_v1.scene_track_domain_command(
                jsonb_build_object(
                    'schema', 'aap-labeling-scene-track-domain/v1',
                    'action', 'reclassify',
                    'annotation_id', v_annotation.id,
                    'scene_track_id', v_annotation.scene_track_id,
                    'track_id', v_annotation.track_id,
                    'expected_class_name', v_annotation.class_name,
                    'class_name', v_class_name
                )
            );
            IF v_scene_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-scene-track-domain/v1'
               OR v_scene_reply ->> 'action' IS DISTINCT FROM 'reclassify'
               OR v_scene_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001',
                    MESSAGE = 'aap_annotation_write_error',
                    DETAIL = jsonb_build_object(
                        'status_code', 409,
                        'detail', jsonb_build_object(
                            'reason', COALESCE(v_scene_reply #>> '{error,code}', 'scene_track_unavailable'),
                            'message', COALESCE(v_scene_reply #>> '{error,message}', 'Scene Track operation failed')
                        )
                    )::text;
            END IF;
            v_track_reclassified := true;
        END IF;

        v_temporal_role := v_annotation.temporal_role;
        IF v_annotation.scene_track_id IS NOT NULL
           AND v_geometry IS NOT NULL THEN
            v_temporal_role := 'keyframe';
        END IF;

        UPDATE public.annotations AS annotation
        SET geometry = COALESCE(v_geometry, annotation.geometry),
            annotation_type = CASE WHEN v_geometry IS NOT NULL
                THEN COALESCE(NULLIF(v_geometry ->> 'type', ''), '')
                ELSE annotation.annotation_type END,
            class_name = COALESCE(v_class_name, annotation.class_name),
            confidence = CASE WHEN jsonb_typeof(p_request -> 'confidence') = 'number'
                THEN (p_request ->> 'confidence')::double precision
                ELSE annotation.confidence END,
            attributes = CASE WHEN jsonb_typeof(p_request -> 'attributes') = 'object'
                THEN p_request -> 'attributes' ELSE annotation.attributes END,
            attributes_meta = CASE WHEN jsonb_typeof(p_request -> 'attributes_meta') = 'object'
                THEN p_request -> 'attributes_meta' ELSE annotation.attributes_meta END,
            z_order = CASE WHEN jsonb_typeof(p_request -> 'z_order') = 'number'
                THEN (p_request ->> 'z_order')::integer ELSE annotation.z_order END,
            is_locked = CASE WHEN jsonb_typeof(p_request -> 'is_locked') = 'boolean'
                THEN (p_request ->> 'is_locked')::boolean ELSE annotation.is_locked END,
            is_hidden = CASE WHEN jsonb_typeof(p_request -> 'is_hidden') = 'boolean'
                THEN (p_request ->> 'is_hidden')::boolean ELSE annotation.is_hidden END,
            track_id = CASE WHEN v_geometry IS NOT NULL
                    OR (v_annotation.geometry ->> 'type' = 'box_3d' AND annotation.track_id IS NULL)
                THEN v_track_id ELSE annotation.track_id END,
            temporal_role = v_temporal_role,
            version = annotation.version + 1,
            updated_at = transaction_timestamp()
        WHERE annotation.id = v_annotation.id
        RETURNING annotation.class_name, annotation.geometry, annotation.id, annotation.is_active, annotation.is_locked, annotation.project_id, annotation.scene_track_id, annotation.sensor_role, annotation.task_id, annotation.temporal_role, annotation.track_id, annotation.version INTO v_annotation;

        IF v_annotation.scene_track_id IS NOT NULL
           AND NOT v_track_reclassified THEN
            v_scene_reply := aap_labeling_v1.scene_track_domain_command(
                jsonb_build_object(
                    'schema', 'aap-labeling-scene-track-domain/v1',
                    'action', 'touch',
                    'annotation_id', v_annotation.id,
                    'scene_track_id', v_annotation.scene_track_id,
                    'track_id', v_annotation.track_id,
                    'class_name', v_annotation.class_name
                )
            );
            IF v_scene_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-scene-track-domain/v1'
               OR v_scene_reply ->> 'action' IS DISTINCT FROM 'touch'
               OR v_scene_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001',
                    MESSAGE = 'aap_annotation_write_error',
                    DETAIL = jsonb_build_object(
                        'status_code', 409,
                        'detail', jsonb_build_object(
                            'reason', COALESCE(v_scene_reply #>> '{error,code}', 'scene_track_unavailable'),
                            'message', COALESCE(v_scene_reply #>> '{error,message}', 'Scene Track operation failed')
                        )
                    )::text;
            END IF;
        END IF;

        v_reply_data := jsonb_build_object(
            'mutation', jsonb_build_object(
                'annotation_id', v_annotation.id,
                'version', v_annotation.version,
                'changed', true,
                'deleted_children', 0,
                'deleted_camera_members', 0
            ),
            'receipt', NULL
        );
    ELSIF v_operation = 'bind_scene_track_member' THEN
        IF jsonb_typeof(p_request -> 'expected_version') IS DISTINCT FROM 'number'
           OR p_request ->> 'expected_version' !~ '^[1-9][0-9]*$'
           OR jsonb_typeof(p_request -> 'scene_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'frame_index') IS DISTINCT FROM 'number'
           OR p_request ->> 'frame_index' !~ '^[0-9]+$'
           OR jsonb_typeof(p_request -> 'temporal_role') IS DISTINCT FROM 'string'
           OR p_request ->> 'temporal_role' NOT IN ('keyframe', 'derived', 'sample') THEN
            RAISE EXCEPTION 'Invalid Annotation Scene Track binding' USING ERRCODE = '22023';
        END IF;
        v_expected_version := (p_request ->> 'expected_version')::integer;
        v_scene_id := (p_request ->> 'scene_id')::uuid;
        v_frame_index := (p_request ->> 'frame_index')::integer;
        SELECT annotation.class_name, annotation.geometry, annotation.id, annotation.is_active, annotation.is_locked, annotation.project_id, annotation.scene_track_id, annotation.sensor_role, annotation.task_id, annotation.temporal_role, annotation.track_id, annotation.version INTO v_annotation
        FROM public.annotations AS annotation
        WHERE annotation.id = v_annotation_id
        FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_annotation_write_error',
                DETAIL = jsonb_build_object('status_code', 404, 'detail', 'Annotation not found')::text;
        END IF;
        IF v_annotation.task_id IS DISTINCT FROM v_task_id
           OR (v_annotation.project_id IS NOT NULL
               AND v_annotation.project_id IS DISTINCT FROM v_project_id)
           OR v_annotation.is_active IS NOT TRUE THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_annotation_write_error',
                DETAIL = jsonb_build_object('status_code', 409, 'detail', jsonb_build_object(
                    'reason', 'annotation_target_changed'
                ))::text;
        END IF;
        IF v_annotation.version IS DISTINCT FROM v_expected_version THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_annotation_write_error',
                DETAIL = jsonb_build_object('status_code', 409, 'detail', jsonb_build_object(
                    'reason', 'version_mismatch', 'current_version', v_annotation.version
                ))::text;
        END IF;
        IF v_annotation.sensor_role IS NOT NULL
           OR v_annotation.scene_track_id IS NOT NULL
           OR v_annotation.geometry ->> 'type' IS DISTINCT FROM 'box_3d'
           OR v_annotation.track_id IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_annotation_write_error',
                DETAIL = jsonb_build_object('status_code', 409, 'detail', jsonb_build_object(
                    'reason', 'annotation_target_changed'
                ))::text;
        END IF;
        v_scene_reply := aap_labeling_v1.scene_track_domain_command(
            jsonb_build_object(
                'schema', 'aap-labeling-scene-track-domain/v1',
                'action', 'ensure',
                'project_id', v_project_id,
                'scene_id', v_scene_id,
                'track_id', v_annotation.track_id,
                'class_name', v_annotation.class_name,
                'frames', jsonb_build_array(v_frame_index),
                'actor_id', v_actor_id,
                'interval_source', 'manual',
                'member_change', true
            )
        );
        IF v_scene_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-scene-track-domain/v1'
           OR v_scene_reply ->> 'action' IS DISTINCT FROM 'ensure'
           OR v_scene_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR jsonb_typeof(v_scene_reply #> '{data,track,id}') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_annotation_write_error',
                DETAIL = jsonb_build_object(
                    'status_code', 409,
                    'detail', jsonb_build_object(
                        'reason', COALESCE(v_scene_reply #>> '{error,code}', 'scene_track_unavailable'),
                        'message', COALESCE(v_scene_reply #>> '{error,message}', 'Scene Track operation failed')
                    )
                )::text;
        END IF;
        v_scene_track_id := (v_scene_reply #>> '{data,track,id}')::uuid;
        UPDATE public.annotations AS annotation
        SET scene_track_id = v_scene_track_id,
            temporal_role = p_request ->> 'temporal_role',
            updated_at = transaction_timestamp()
        WHERE annotation.id = v_annotation.id
        RETURNING annotation.class_name, annotation.geometry, annotation.id, annotation.is_active, annotation.is_locked, annotation.project_id, annotation.scene_track_id, annotation.sensor_role, annotation.task_id, annotation.temporal_role, annotation.track_id, annotation.version INTO v_annotation;
        v_reply_data := jsonb_build_object(
            'mutation', jsonb_build_object(
                'annotation_id', v_annotation.id,
                'version', v_annotation.version,
                'changed', true,
                'deleted_children', 0,
                'deleted_camera_members', 0
            ),
            'receipt', NULL
        );
    ELSIF v_operation = 'delete' THEN
        IF jsonb_typeof(p_request -> 'expected_version') IS DISTINCT FROM 'number'
           OR p_request ->> 'expected_version' !~ '^[1-9][0-9]*$' THEN
            RAISE EXCEPTION 'Invalid Annotation delete fence' USING ERRCODE = '22023';
        END IF;
        v_expected_version := (p_request ->> 'expected_version')::integer;
        SELECT annotation.class_name, annotation.geometry, annotation.id, annotation.is_active, annotation.is_locked, annotation.project_id, annotation.scene_track_id, annotation.sensor_role, annotation.task_id, annotation.temporal_role, annotation.track_id, annotation.version INTO v_annotation
        FROM public.annotations AS annotation
        WHERE annotation.id = v_annotation_id
        FOR UPDATE;
        IF NOT FOUND THEN
            v_reply_data := jsonb_build_object(
                'mutation', jsonb_build_object(
                    'annotation_id', v_annotation_id, 'version', 0, 'changed', false,
                    'deleted_children', 0, 'deleted_camera_members', 0
                ),
                'receipt', NULL
            );
        ELSIF v_annotation.task_id IS DISTINCT FROM v_task_id
           OR (v_annotation.project_id IS NOT NULL
               AND v_annotation.project_id IS DISTINCT FROM v_project_id) THEN
            v_reply_data := jsonb_build_object(
                'mutation', jsonb_build_object(
                    'annotation_id', v_annotation_id, 'version', 0, 'changed', false,
                    'deleted_children', 0, 'deleted_camera_members', 0
                ),
                'receipt', NULL
            );
        ELSE
            IF v_annotation.version IS DISTINCT FROM v_expected_version THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001',
                    MESSAGE = 'aap_annotation_write_error',
                    DETAIL = jsonb_build_object(
                        'status_code', 409, 'detail', jsonb_build_object(
                            'reason', 'version_mismatch', 'current_version', v_annotation.version
                        )
                    )::text;
            END IF;
            IF v_annotation.sensor_role IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001',
                    MESSAGE = 'aap_annotation_write_error',
                    DETAIL = jsonb_build_object(
                        'status_code', 422,
                        'detail', 'camera members must be deleted through the point-cloud camera member API'
                    )::text;
            END IF;
            IF v_annotation.is_locked THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001',
                    MESSAGE = 'aap_annotation_write_error',
                    DETAIL = jsonb_build_object(
                        'status_code', 409,
                        'detail', jsonb_build_object('reason', 'annotation_locked')
                    )::text;
            END IF;
            UPDATE public.annotations AS annotation
            SET is_active = false,
                version = annotation.version + 1,
                updated_at = transaction_timestamp()
            WHERE annotation.id = v_annotation.id
            RETURNING annotation.class_name, annotation.geometry, annotation.id, annotation.is_active, annotation.is_locked, annotation.project_id, annotation.scene_track_id, annotation.sensor_role, annotation.task_id, annotation.temporal_role, annotation.track_id, annotation.version INTO v_annotation;

            IF v_annotation.scene_track_id IS NOT NULL THEN
                v_scene_reply := aap_labeling_v1.scene_track_domain_command(
                    jsonb_build_object(
                        'schema', 'aap-labeling-scene-track-domain/v1',
                        'action', 'touch',
                        'annotation_id', v_annotation.id,
                        'scene_track_id', v_annotation.scene_track_id,
                        'track_id', v_annotation.track_id,
                        'class_name', v_annotation.class_name
                    )
                );
                IF v_scene_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-scene-track-domain/v1'
                   OR v_scene_reply ->> 'action' IS DISTINCT FROM 'touch'
                   OR v_scene_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001',
                        MESSAGE = 'aap_annotation_write_error',
                        DETAIL = jsonb_build_object(
                            'status_code', 409,
                            'detail', jsonb_build_object(
                                'reason', COALESCE(v_scene_reply #>> '{error,code}', 'scene_track_unavailable'),
                                'message', COALESCE(v_scene_reply #>> '{error,message}', 'Scene Track operation failed')
                            )
                        )::text;
                END IF;
                FOR v_camera_id IN
                    SELECT camera.id
                    FROM public.annotations AS camera
                    WHERE camera.task_id = v_task_id
                      AND camera.scene_track_id = v_annotation.scene_track_id
                      AND camera.sensor_role IS NOT NULL
                      AND camera.is_active
                      AND NOT camera.was_cancelled
                    ORDER BY camera.id
                    FOR UPDATE
                LOOP
                    UPDATE public.annotations AS camera
                    SET is_active = false,
                        version = camera.version + 1,
                        updated_at = transaction_timestamp()
                    WHERE camera.id = v_camera_id;
                    v_deleted_camera_members := v_deleted_camera_members + 1;
                END LOOP;
            END IF;

            FOR v_child_id IN
                SELECT child.id
                FROM public.annotations AS child
                WHERE child.parent_annotation_id = v_annotation_id
                  AND child.is_active
                ORDER BY child.id
                FOR UPDATE
            LOOP
                UPDATE public.annotations AS child
                SET is_active = false,
                    version = child.version + 1,
                    updated_at = transaction_timestamp()
                WHERE child.id = v_child_id;
                v_deleted_children := v_deleted_children + 1;
            END LOOP;
            v_reply_data := jsonb_build_object(
                'mutation', jsonb_build_object(
                    'annotation_id', v_annotation.id,
                    'version', v_annotation.version,
                    'changed', true,
                    'deleted_children', v_deleted_children,
                    'deleted_camera_members', v_deleted_camera_members
                ),
                'receipt', NULL
            );
        END IF;
    ELSE
        IF jsonb_typeof(p_request -> 'idempotency_key') IS DISTINCT FROM 'string'
           OR length(p_request ->> 'idempotency_key') NOT BETWEEN 1 AND 128
           OR jsonb_typeof(p_request -> 'request_digest') IS DISTINCT FROM 'string'
           OR p_request ->> 'request_digest' !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION 'Invalid Annotation create receipt fields' USING ERRCODE = '22023';
        END IF;
        v_idempotency_key := p_request ->> 'idempotency_key';
        v_request_digest := p_request ->> 'request_digest';
        IF v_operation = 'write_create_receipt'
           AND (jsonb_typeof(p_request -> 'response_json') IS DISTINCT FROM 'object'
                OR jsonb_typeof(p_request -> 'operation_id') IS DISTINCT FROM 'string') THEN
            RAISE EXCEPTION 'Invalid Annotation create receipt response' USING ERRCODE = '22023';
        END IF;
        IF v_operation = 'write_create_receipt' THEN
            v_operation_id := (p_request ->> 'operation_id')::uuid;
        END IF;
        SELECT operation.id, operation.kind, operation.request_digest, operation.response_json
        INTO v_operation_row
        FROM public.annotation_operations AS operation
        WHERE operation.task_id = v_task_id
          AND operation.actor_id = v_actor_id
          AND operation.idempotency_key = v_idempotency_key;
        IF v_operation = 'read_create_receipt' THEN
            IF NOT FOUND THEN
                v_reply_data := jsonb_build_object(
                    'mutation', NULL,
                    'receipt', jsonb_build_object('found', false, 'response_json', NULL)
                );
            ELSIF v_operation_row.kind IS DISTINCT FROM 'create_annotation'
               OR v_operation_row.request_digest IS DISTINCT FROM v_request_digest THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001',
                    MESSAGE = 'aap_annotation_write_error',
                    DETAIL = jsonb_build_object(
                        'status_code', 409,
                        'detail', jsonb_build_object(
                            'reason', 'idempotency_conflict',
                            'message', 'idempotency key was already used with another request',
                            'operation_id', v_operation_row.id
                        )
                    )::text;
            ELSE
                v_reply_data := jsonb_build_object(
                    'mutation', NULL,
                    'receipt', jsonb_build_object(
                        'found', true, 'response_json', v_operation_row.response_json
                    )
                );
            END IF;
        ELSE
            IF FOUND THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001',
                    MESSAGE = 'aap_annotation_write_error',
                    DETAIL = jsonb_build_object(
                        'status_code', 409,
                        'detail', jsonb_build_object(
                            'reason', 'idempotency_conflict',
                            'message', 'idempotency key was already used with another request',
                            'operation_id', v_operation_row.id
                        )
                    )::text;
            END IF;
            INSERT INTO public.annotation_operations (
                id, task_id, actor_id, kind, idempotency_key, request_digest,
                scope_fingerprint, source_versions, result_versions, report,
                status, response_json
            ) VALUES (
                v_operation_id, v_task_id, v_actor_id, 'create_annotation',
                v_idempotency_key, v_request_digest, v_request_digest,
                '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, 'committed',
                p_request -> 'response_json'
            );
            v_reply_data := jsonb_build_object(
                'mutation', NULL,
                'receipt', jsonb_build_object('found', true, 'response_json', NULL)
            );
        END IF;
    END IF;

    RETURN jsonb_build_object(
        'schema', 'aap-labeling-annotation-writes/v1',
        'ok', true,
        'request', p_request,
        'data', v_reply_data
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.annotation_write_command(jsonb) FROM PUBLIC;
