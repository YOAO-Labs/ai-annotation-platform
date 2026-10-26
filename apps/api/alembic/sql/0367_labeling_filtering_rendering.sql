CREATE FUNCTION aap_labeling_v1.prepare_filtering_rendering(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_project_id uuid;
    v_candidate record;
    v_candidate_count integer;
    v_result jsonb;
    v_shape jsonb;
    v_confidence jsonb;
    v_rendered_result jsonb;
    v_ids jsonb := '[]'::jsonb;
    v_rendered_predictions jsonb := '[]'::jsonb;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE = '42501',
            MESSAGE = 'Filtering rendering preparation requires a test database';
    END IF;
    IF p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-e2e-fixtures/v1'
       OR p_request ->> 'operation' IS DISTINCT FROM 'prepare_filtering_rendering'
       OR pg_catalog.octet_length(p_request::text) > 16777216 THEN
        RAISE EXCEPTION 'Invalid filtering rendering request' USING ERRCODE = '22023';
    END IF;
    IF (p_request - ARRAY[
            'schema','operation','project_id','low_prediction_id',
            'at_prediction_id','above_prediction_id'
        ]) <> '{}'::jsonb
       OR NOT (p_request ?& ARRAY[
            'schema','operation','project_id','low_prediction_id',
            'at_prediction_id','above_prediction_id'
        ])
       OR pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request -> 'low_prediction_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request -> 'at_prediction_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request -> 'above_prediction_id') IS DISTINCT FROM 'string'
       OR (p_request ->> 'project_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR (p_request ->> 'low_prediction_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR (p_request ->> 'at_prediction_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR (p_request ->> 'above_prediction_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RAISE EXCEPTION 'Invalid filtering rendering request fields' USING ERRCODE = '22023';
    END IF;

    v_project_id := (p_request ->> 'project_id')::uuid;
    IF (p_request ->> 'low_prediction_id')::uuid = (p_request ->> 'at_prediction_id')::uuid
       OR (p_request ->> 'low_prediction_id')::uuid = (p_request ->> 'above_prediction_id')::uuid
       OR (p_request ->> 'at_prediction_id')::uuid = (p_request ->> 'above_prediction_id')::uuid THEN
        RAISE EXCEPTION USING ERRCODE = '42501',
            MESSAGE = 'Filtering rendering candidates must be distinct';
    END IF;

    SELECT pg_catalog.count(*)
    INTO v_candidate_count
    FROM (
        VALUES
            ((p_request ->> 'low_prediction_id')::uuid, 'filter-detector-low'::text),
            ((p_request ->> 'at_prediction_id')::uuid, 'filter-detector-at'::text),
            ((p_request ->> 'above_prediction_id')::uuid, 'filter-detector-above'::text)
    ) AS expected(prediction_id, model_version)
    JOIN public.predictions AS prediction
      ON prediction.id = expected.prediction_id
     AND prediction.model_version = expected.model_version
    JOIN public.tasks AS fixture_task
      ON fixture_task.id = prediction.task_id
     AND fixture_task.project_id = v_project_id
     AND fixture_task.file_type = 'video'
    WHERE prediction.project_id = v_project_id;
    IF v_candidate_count <> 3 THEN
        RAISE EXCEPTION USING ERRCODE = '42501',
            MESSAGE = 'Filtering rendering candidate scope is missing, duplicated, or foreign';
    END IF;

    FOR v_candidate IN
        SELECT candidate.sequence_no, candidate.prediction_id, candidate.frame_index,
               candidate.model_version
        FROM (
            VALUES
                (1, (p_request ->> 'low_prediction_id')::uuid, 0, 'filter-detector-low'::text),
                (2, (p_request ->> 'at_prediction_id')::uuid, 0, 'filter-detector-at'::text),
                (3, (p_request ->> 'above_prediction_id')::uuid, 10, 'filter-detector-above'::text)
        ) AS candidate(sequence_no, prediction_id, frame_index, model_version)
        ORDER BY candidate.sequence_no
    LOOP
        SELECT prediction.result
        INTO v_result
        FROM public.predictions AS prediction
        JOIN public.tasks AS fixture_task
          ON fixture_task.id = prediction.task_id
         AND fixture_task.project_id = v_project_id
         AND fixture_task.file_type = 'video'
        WHERE prediction.id = v_candidate.prediction_id
          AND prediction.project_id = v_project_id
          AND prediction.model_version = v_candidate.model_version
        FOR UPDATE OF prediction, fixture_task;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = '42501',
                MESSAGE = 'Filtering rendering candidate scope changed';
        END IF;
        IF pg_catalog.jsonb_typeof(v_result) IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(v_result) <> 1 THEN
            RAISE EXCEPTION 'Filtering rendering candidate result is not a single shape' USING ERRCODE = '22023';
        END IF;
        v_shape := v_result -> 0;
        IF pg_catalog.jsonb_typeof(v_shape) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'Filtering rendering candidate result shape is invalid' USING ERRCODE = '22023';
        END IF;
        IF v_shape ->> 'type' = 'rectanglelabels'
           AND v_shape ? 'score'
           AND pg_catalog.jsonb_typeof(v_shape -> 'score') IN ('number','null') THEN
            v_confidence := v_shape -> 'score';
        ELSIF v_shape ->> 'type' = 'video_bbox'
           AND v_shape ->> 'class_name' = 'car'
           AND v_shape -> 'geometry' ->> 'type' = 'video_bbox'
           AND v_shape ? 'confidence'
           AND pg_catalog.jsonb_typeof(v_shape -> 'confidence') IN ('number','null') THEN
            v_confidence := v_shape -> 'confidence';
        ELSE
            RAISE EXCEPTION 'Filtering rendering candidate has no supported confidence' USING ERRCODE = '22023';
        END IF;

        v_rendered_result := pg_catalog.jsonb_build_array(
            pg_catalog.jsonb_build_object(
                'type', 'video_bbox',
                'class_name', 'car',
                'geometry', pg_catalog.jsonb_build_object(
                    'type', 'video_bbox',
                    'frame_index', v_candidate.frame_index,
                    'x', 0.62,
                    'y', 0.62,
                    'w', 0.12,
                    'h', 0.15
                ),
                'confidence', v_confidence,
                'tool_unit_id', 'bbox',
                'attributes', '{}'::jsonb
            )
        );
        UPDATE public.predictions AS prediction
        SET result = v_rendered_result
        WHERE prediction.id = v_candidate.prediction_id
          AND prediction.project_id = v_project_id
          AND prediction.model_version = v_candidate.model_version;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = '42501',
                MESSAGE = 'Filtering rendering candidate scope changed';
        END IF;
        v_ids := v_ids || pg_catalog.jsonb_build_array(v_candidate.prediction_id);
        v_rendered_predictions := v_rendered_predictions || pg_catalog.jsonb_build_array(
            pg_catalog.jsonb_build_object(
                'id', v_candidate.prediction_id,
                'result', v_rendered_result
            )
        );
    END LOOP;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-e2e-fixtures/v1',
        'operation', 'prepare_filtering_rendering',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'ids', v_ids,
            'rendered_predictions', v_rendered_predictions
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.prepare_filtering_rendering(jsonb) FROM PUBLIC;
