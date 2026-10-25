CREATE FUNCTION aap_labeling_v1.annotation_prediction_command(p_request jsonb)
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
    v_expected_keys text[] := ARRAY[
        'schema', 'operation', 'annotation_id', 'task_id', 'project_id', 'actor_id',
        'expected_batch_id', 'expected_status', 'expected_review_round_id'
    ];
    v_field text;
    v_annotation_id uuid;
    v_task_id uuid;
    v_project_id uuid;
    v_actor_id uuid;
    v_expected_version integer;
    v_task record;
    v_annotation record;
    v_account_reply jsonb;
    v_account jsonb;
    v_source record;
    v_data jsonb;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-annotation-prediction/v1'
       OR jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Annotation prediction request' USING ERRCODE = '22023';
    END IF;
    v_operation := p_request ->> 'operation';
    CASE v_operation
        WHEN 'create_accepted' THEN
            v_expected_keys := v_expected_keys || ARRAY[
                'prediction_id', 'prediction_created_at', 'annotation_type',
                'tool_unit_id', 'class_name', 'geometry', 'confidence',
                'video_segment_id', 'attributes', 'attributes_meta', 'track_id',
                'temporal_role'
            ];
        WHEN 'apply_secondary_attributes' THEN
            v_expected_keys := v_expected_keys || ARRAY[
                'expected_version', 'attributes', 'attributes_meta'
            ];
        WHEN 'mark_secondary_child' THEN
            v_expected_keys := v_expected_keys || ARRAY[
                'expected_version', 'parent_annotation_id'
            ];
        ELSE
            RAISE EXCEPTION 'Invalid Annotation prediction operation' USING ERRCODE = '22023';
    END CASE;
    SELECT array_agg(key ORDER BY key) INTO v_keys
    FROM jsonb_object_keys(p_request) AS fields(key);
    SELECT array_agg(key ORDER BY key) INTO v_expected_keys
    FROM unnest(v_expected_keys) AS fields(key);
    IF v_keys IS DISTINCT FROM v_expected_keys THEN
        RAISE EXCEPTION 'Invalid Annotation prediction request fields' USING ERRCODE = '22023';
    END IF;
    FOREACH v_field IN ARRAY ARRAY['annotation_id', 'task_id', 'project_id', 'actor_id'] LOOP
        IF jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
           OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RAISE EXCEPTION 'Invalid Annotation prediction identity' USING ERRCODE = '22023';
        END IF;
    END LOOP;
    IF jsonb_typeof(p_request -> 'expected_batch_id') NOT IN ('string', 'null')
       OR jsonb_typeof(p_request -> 'expected_review_round_id') NOT IN ('string', 'null')
       OR jsonb_typeof(p_request -> 'expected_status') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Annotation prediction task fence' USING ERRCODE = '22023';
    END IF;
    v_annotation_id := (p_request ->> 'annotation_id')::uuid;
    v_task_id := (p_request ->> 'task_id')::uuid;
    v_project_id := (p_request ->> 'project_id')::uuid;
    v_actor_id := (p_request ->> 'actor_id')::uuid;

    v_account_reply := aap_identity_v1.lock_accounts_share(ARRAY[v_actor_id], true);
    IF jsonb_typeof(v_account_reply -> 'accounts') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'Invalid Identity account-lock reply' USING ERRCODE = '55000';
    END IF;
    SELECT account.value INTO v_account
    FROM jsonb_array_elements(v_account_reply -> 'accounts') AS account(value)
    WHERE account.value ->> 'id' = v_actor_id::text;
    IF v_account IS NULL OR v_account -> 'is_active' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_annotation_prediction_error',
            DETAIL = jsonb_build_object('status_code', 403,
                'detail', jsonb_build_object('reason', 'actor_inactive'))::text;
    END IF;
    SELECT task.id, task.project_id, task.batch_id, task.status, task.review_round_id
    INTO v_task FROM public.tasks AS task
    WHERE task.id = v_task_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_annotation_prediction_error',
            DETAIL = jsonb_build_object('status_code', 404, 'detail', 'Task not found')::text;
    END IF;
    IF v_task.project_id IS DISTINCT FROM v_project_id
       OR v_task.batch_id IS DISTINCT FROM (p_request ->> 'expected_batch_id')::uuid
       OR v_task.status IS DISTINCT FROM p_request ->> 'expected_status'
       OR v_task.review_round_id IS DISTINCT FROM (p_request ->> 'expected_review_round_id')::uuid THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_annotation_prediction_error',
            DETAIL = jsonb_build_object('status_code', 409,
                'detail', jsonb_build_object('reason', 'task_target_changed'))::text;
    END IF;

    IF v_operation = 'create_accepted' THEN
        FOREACH v_field IN ARRAY ARRAY[
            'prediction_id', 'prediction_created_at', 'annotation_type',
            'tool_unit_id', 'class_name', 'temporal_role'
        ] LOOP
            IF jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION 'Invalid accepted Annotation fields' USING ERRCODE = '22023';
            END IF;
        END LOOP;
        IF jsonb_typeof(p_request -> 'geometry') IS DISTINCT FROM 'object'
           OR jsonb_typeof(p_request -> 'attributes') IS DISTINCT FROM 'object'
           OR jsonb_typeof(p_request -> 'attributes_meta') IS DISTINCT FROM 'object'
           OR jsonb_typeof(p_request -> 'confidence') NOT IN ('number', 'null')
           OR jsonb_typeof(p_request -> 'video_segment_id') NOT IN ('string', 'null')
           OR jsonb_typeof(p_request -> 'track_id') NOT IN ('string', 'null')
           OR p_request ->> 'temporal_role' IS DISTINCT FROM (CASE
               WHEN p_request #>> '{geometry,type}' = 'box_3d' THEN 'keyframe' ELSE 'sample' END)
           OR (p_request #>> '{geometry,type}' = 'box_3d' AND (
               p_request ->> 'track_id' IS NULL OR p_request ->> 'track_id' = ''
           )) THEN
            RAISE EXCEPTION 'Invalid accepted Annotation payload' USING ERRCODE = '22023';
        END IF;
        SELECT prediction.id, prediction.task_id, prediction.project_id, prediction.source,
               prediction.tool_unit_id
        INTO v_source FROM public.predictions AS prediction
        WHERE prediction.id = (p_request ->> 'prediction_id')::uuid
          AND prediction.created_at = (p_request ->> 'prediction_created_at')::timestamptz
        FOR KEY SHARE;
        IF NOT FOUND OR v_source.task_id IS DISTINCT FROM v_task_id
           OR v_source.project_id IS DISTINCT FROM v_project_id
           OR v_source.source = 'interactive_accept'
           OR COALESCE(NULLIF(v_source.tool_unit_id, ''), 'bbox')
               IS DISTINCT FROM p_request ->> 'tool_unit_id' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_annotation_prediction_error',
                DETAIL = jsonb_build_object('status_code', 409,
                    'detail', jsonb_build_object('reason', 'prediction_source_changed'))::text;
        END IF;
        INSERT INTO public.annotations (
            id, task_id, project_id, video_segment_id, user_id, source,
            annotation_type, tool_unit_id, class_name, geometry, confidence,
            parent_prediction_id, parent_annotation_id, track_id, scene_track_id,
            temporal_role, was_cancelled, ground_truth, is_active,
            attributes, attributes_meta, z_order, is_locked, is_hidden, version
        ) VALUES (
            v_annotation_id, v_task_id, v_project_id,
            (p_request ->> 'video_segment_id')::uuid, v_actor_id, 'prediction_based',
            p_request ->> 'annotation_type', p_request ->> 'tool_unit_id',
            p_request ->> 'class_name', p_request -> 'geometry',
            (p_request ->> 'confidence')::double precision,
            v_source.id, NULL, p_request ->> 'track_id', NULL,
            p_request ->> 'temporal_role', false, false, true,
            p_request -> 'attributes', p_request -> 'attributes_meta',
            0, false, false, 1
        ) RETURNING jsonb_build_object(
            'annotation_id', annotations.id, 'version', annotations.version,
            'source', annotations.source, 'updated_at', annotations.updated_at
        ) INTO v_data;
    ELSE
        IF jsonb_typeof(p_request -> 'expected_version') IS DISTINCT FROM 'number'
           OR p_request ->> 'expected_version' !~ '^[1-9][0-9]*$' THEN
            RAISE EXCEPTION 'Invalid Annotation prediction version' USING ERRCODE = '22023';
        END IF;
        v_expected_version := (p_request ->> 'expected_version')::integer;
        SELECT annotation.id, annotation.task_id, annotation.project_id,
               annotation.version, annotation.is_active, annotation.was_cancelled, annotation.sensor_role,
               annotation.parent_annotation_id, annotation.source, annotation.updated_at
        INTO v_annotation FROM public.annotations AS annotation
        WHERE annotation.id = v_annotation_id FOR UPDATE NOWAIT;
        IF NOT FOUND OR v_annotation.task_id IS DISTINCT FROM v_task_id
           OR v_annotation.project_id IS DISTINCT FROM v_project_id
           OR v_annotation.version IS DISTINCT FROM v_expected_version
           OR v_annotation.is_active IS NOT TRUE
           OR v_annotation.was_cancelled IS TRUE
           OR (v_operation = 'mark_secondary_child' AND v_annotation.sensor_role IS NOT NULL) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_annotation_prediction_error',
                DETAIL = jsonb_build_object('status_code', 409,
                    'detail', jsonb_build_object('reason', 'annotation_target_changed'))::text;
        END IF;
        IF v_operation = 'apply_secondary_attributes' THEN
            IF jsonb_typeof(p_request -> 'attributes') IS DISTINCT FROM 'object'
               OR jsonb_typeof(p_request -> 'attributes_meta') IS DISTINCT FROM 'object' THEN
                RAISE EXCEPTION 'Invalid secondary attribute payload' USING ERRCODE = '22023';
            END IF;
            UPDATE public.annotations AS annotation
            SET attributes = p_request -> 'attributes',
                attributes_meta = p_request -> 'attributes_meta',
                version = annotation.version + 1, updated_at = transaction_timestamp()
            WHERE annotation.id = v_annotation_id
            RETURNING jsonb_build_object(
                'annotation_id', annotation.id, 'version', annotation.version,
                'source', annotation.source, 'updated_at', annotation.updated_at
            ) INTO v_data;
        ELSE
            IF jsonb_typeof(p_request -> 'parent_annotation_id') IS DISTINCT FROM 'string'
               OR v_annotation.parent_annotation_id IS DISTINCT FROM
                   (p_request ->> 'parent_annotation_id')::uuid THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_annotation_prediction_error',
                    DETAIL = jsonb_build_object('status_code', 409,
                        'detail', jsonb_build_object('reason', 'annotation_parent_changed'))::text;
            END IF;
            IF v_annotation.source = 'prediction_based' THEN
                v_data := jsonb_build_object(
                    'annotation_id', v_annotation.id, 'version', v_annotation.version,
                    'source', v_annotation.source, 'updated_at', v_annotation.updated_at
                );
            ELSE
                UPDATE public.annotations AS annotation
                SET source = 'prediction_based', updated_at = transaction_timestamp()
                WHERE annotation.id = v_annotation_id
                RETURNING jsonb_build_object(
                    'annotation_id', annotation.id, 'version', annotation.version,
                    'source', annotation.source, 'updated_at', annotation.updated_at
                ) INTO v_data;
            END IF;
        END IF;
    END IF;
    RETURN jsonb_build_object(
        'schema', 'aap-labeling-annotation-prediction/v1', 'ok', true,
        'request', p_request, 'data', v_data
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.annotation_prediction_command(jsonb) FROM PUBLIC;
