CREATE FUNCTION aap_labeling_v1.prediction_record_command(p_request jsonb)
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
    v_prediction_id uuid;
    v_prediction_created_at timestamptz;
    v_prediction_meta_id uuid;
    v_task_id uuid;
    v_project_id uuid;
    v_failure_id uuid;
    v_created_at timestamptz;
    v_dismissed_at timestamptz;
    v_tool_unit_id text;
    v_total_predictions integer;
    v_task_updated_at timestamptz;
    v_changed boolean := false;
    v_shapes jsonb;
    v_rejected jsonb;
    v_shape_count integer;
    v_shape_index integer;
    v_shape_numeric numeric;
    v_task_ids uuid[] := ARRAY[]::uuid[];
    v_predictions jsonb := '[]'::jsonb;
    v_failed_prediction_ids jsonb := '[]'::jsonb;
    v_prediction_count bigint := 0;
    v_failed_prediction_count bigint := 0;
    v_prediction_meta_ids jsonb := '[]'::jsonb;
    v_target_prediction_ids uuid[] := ARRAY[]::uuid[];
    v_task_scope boolean := false;
    v_source_scope text;
    v_ml_backend_count bigint := 0;
    v_external_import_count bigint := 0;
    v_unknown_count bigint := 0;
    v_total_count bigint := 0;
    v_error_code text;
    v_error_details jsonb;
    v_data jsonb;
BEGIN
    IF p_request IS NULL OR jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-prediction-records/v1'
       OR jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Labeling Prediction record request'
            USING ERRCODE = '22023';
    END IF;
    v_operation := p_request ->> 'operation';
    SELECT array_agg(key ORDER BY key)
    INTO v_keys
    FROM jsonb_object_keys(p_request) AS request_keys(key);

    IF v_operation = 'insert_result' THEN
        v_expected_keys := ARRAY[
            'completion_cost','completion_tokens','extra','inference_time_ms',
            'ml_backend_id','ml_backend_pool_id','model_version','operation',
            'prediction_created_at','prediction_id','prediction_meta_id','project_id',
            'prompt_cost','prompt_tokens','result','schema','score','source',
            'task_id','tool_unit_id','total_cost','total_tokens'
        ]::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'prediction_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'prediction_created_at') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'prediction_meta_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'ml_backend_id') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'ml_backend_pool_id') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'model_version') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'score') NOT IN ('null','number')
           OR jsonb_typeof(p_request -> 'tool_unit_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'result') IS DISTINCT FROM 'array'
           OR jsonb_typeof(p_request -> 'source') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'inference_time_ms') NOT IN ('null','number')
           OR jsonb_typeof(p_request -> 'prompt_tokens') NOT IN ('null','number')
           OR jsonb_typeof(p_request -> 'completion_tokens') NOT IN ('null','number')
           OR jsonb_typeof(p_request -> 'total_tokens') NOT IN ('null','number')
           OR jsonb_typeof(p_request -> 'prompt_cost') NOT IN ('null','number')
           OR jsonb_typeof(p_request -> 'completion_cost') NOT IN ('null','number')
           OR jsonb_typeof(p_request -> 'total_cost') NOT IN ('null','number')
           OR jsonb_typeof(p_request -> 'extra') IS DISTINCT FROM 'object'
           OR EXISTS (
               SELECT 1 FROM jsonb_array_elements(p_request -> 'result') AS item(value)
               WHERE jsonb_typeof(item.value) IS DISTINCT FROM 'object'
           ) THEN
            RAISE EXCEPTION 'Invalid Labeling insert-result request'
                USING ERRCODE = '22023';
        END IF;

        v_prediction_id := (p_request ->> 'prediction_id')::uuid;
        v_prediction_meta_id := (p_request ->> 'prediction_meta_id')::uuid;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_project_id := (p_request ->> 'project_id')::uuid;
        PERFORM 1 FROM public.tasks AS task
        WHERE task.id = v_task_id AND task.project_id = v_project_id
        FOR UPDATE;
        IF NOT FOUND THEN
            v_error_code := 'task_not_found';
        ELSE
            INSERT INTO public.predictions (
                id, task_id, project_id, ml_backend_id, ml_backend_pool_id,
                model_version, score, tool_unit_id, result, source, created_at
            ) VALUES (
                v_prediction_id, v_task_id, v_project_id,
                NULLIF(p_request ->> 'ml_backend_id','')::uuid,
                NULLIF(p_request ->> 'ml_backend_pool_id','')::uuid,
                p_request ->> 'model_version',
                NULLIF(p_request ->> 'score','')::double precision,
                p_request ->> 'tool_unit_id', p_request -> 'result',
                p_request ->> 'source',
                COALESCE(
                    NULLIF(p_request ->> 'prediction_created_at','')::timestamptz,
                    transaction_timestamp()
                )
            ) RETURNING created_at, tool_unit_id
            INTO v_created_at, v_tool_unit_id;

            INSERT INTO public.prediction_metas (
                id, prediction_id, prediction_created_at, inference_time_ms,
                prompt_tokens, completion_tokens, total_tokens, prompt_cost,
                completion_cost, total_cost, extra
            ) VALUES (
                v_prediction_meta_id, v_prediction_id, v_created_at,
                NULLIF(p_request ->> 'inference_time_ms','')::integer,
                NULLIF(p_request ->> 'prompt_tokens','')::integer,
                NULLIF(p_request ->> 'completion_tokens','')::integer,
                NULLIF(p_request ->> 'total_tokens','')::integer,
                NULLIF(p_request ->> 'prompt_cost','')::double precision,
                NULLIF(p_request ->> 'completion_cost','')::double precision,
                NULLIF(p_request ->> 'total_cost','')::double precision,
                p_request -> 'extra'
            );

            UPDATE public.tasks AS task
            SET total_predictions = COALESCE(task.total_predictions, 0) + 1,
                updated_at = transaction_timestamp()
            WHERE task.id = v_task_id AND task.project_id = v_project_id
            RETURNING task.total_predictions, task.updated_at
            INTO v_total_predictions, v_task_updated_at;
            v_data := jsonb_build_object(
                'operation', v_operation,
                'prediction_id', v_prediction_id,
                'created_at', v_created_at,
                'tool_unit_id', v_tool_unit_id,
                'total_predictions', v_total_predictions,
                'task_updated_at', v_task_updated_at
            );
        END IF;

    ELSIF v_operation = 'insert_test_seed' THEN
        v_expected_keys := ARRAY[
            'model_version','operation','prediction_id','project_id',
            'rejected_shape_indexes','result','schema','score','task_id','tool_unit_id'
        ]::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'prediction_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'result') IS DISTINCT FROM 'array'
           OR jsonb_typeof(p_request -> 'score') NOT IN ('null','number')
           OR jsonb_typeof(p_request -> 'model_version') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'tool_unit_id') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'rejected_shape_indexes') NOT IN ('null','array')
           OR EXISTS (
               SELECT 1 FROM jsonb_array_elements(p_request -> 'result') AS item(value)
               WHERE jsonb_typeof(item.value) IS DISTINCT FROM 'object'
           )
           OR (jsonb_typeof(p_request -> 'rejected_shape_indexes') = 'array'
               AND EXISTS (
                   SELECT 1
                   FROM jsonb_array_elements(p_request -> 'rejected_shape_indexes') AS item(value)
                   WHERE jsonb_typeof(item.value) IS DISTINCT FROM 'number'
                      OR (item.value #>> '{}')::numeric <> trunc((item.value #>> '{}')::numeric)
               )) THEN
            RAISE EXCEPTION 'Invalid Labeling test-seed request'
                USING ERRCODE = '22023';
        END IF;
        v_prediction_id := (p_request ->> 'prediction_id')::uuid;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_project_id := (p_request ->> 'project_id')::uuid;
        PERFORM 1 FROM public.tasks AS task
        WHERE task.id = v_task_id AND task.project_id = v_project_id
        FOR UPDATE;
        IF NOT FOUND THEN
            v_error_code := 'task_not_found';
        ELSE
            INSERT INTO public.predictions (
                id, task_id, project_id, score, model_version, tool_unit_id,
                result, rejected_shape_indexes
            ) VALUES (
                v_prediction_id, v_task_id, v_project_id,
                NULLIF(p_request ->> 'score','')::double precision,
                p_request ->> 'model_version',
                COALESCE(p_request ->> 'tool_unit_id', 'bbox'),
                p_request -> 'result',
                CASE WHEN jsonb_typeof(p_request -> 'rejected_shape_indexes') = 'array'
                    THEN p_request -> 'rejected_shape_indexes' ELSE '[]'::jsonb END
            ) RETURNING created_at, tool_unit_id
            INTO v_created_at, v_tool_unit_id;
            v_data := jsonb_build_object(
                'operation', v_operation,
                'prediction_id', v_prediction_id,
                'created_at', v_created_at,
                'tool_unit_id', v_tool_unit_id
            );
        END IF;

    ELSIF v_operation = 'create_failed' THEN
        v_expected_keys := ARRAY[
            'error_type','extra','failure_id','message','ml_backend_id',
            'ml_backend_pool_id','model_version','operation','project_id','schema','task_id'
        ]::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'failure_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'task_id') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'ml_backend_id') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'ml_backend_pool_id') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'model_version') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'error_type') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'message') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'extra') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'Invalid Labeling create-failed request'
                USING ERRCODE = '22023';
        END IF;
        v_failure_id := (p_request ->> 'failure_id')::uuid;
        INSERT INTO public.failed_predictions (
            id, task_id, project_id, ml_backend_id, ml_backend_pool_id,
            model_version, error_type, message, extra
        ) VALUES (
            v_failure_id,
            NULLIF(p_request ->> 'task_id','')::uuid,
            (p_request ->> 'project_id')::uuid,
            NULLIF(p_request ->> 'ml_backend_id','')::uuid,
            NULLIF(p_request ->> 'ml_backend_pool_id','')::uuid,
            p_request ->> 'model_version',
            p_request ->> 'error_type', p_request ->> 'message',
            p_request -> 'extra'
        ) RETURNING created_at INTO v_created_at;
        v_data := jsonb_build_object(
            'operation', v_operation,
            'failure_id', v_failure_id,
            'created_at', v_created_at
        );

    ELSIF v_operation IN ('dismiss_failed','restore_failed') THEN
        IF v_operation = 'dismiss_failed' THEN
            v_expected_keys := ARRAY['dismissed_at','failure_id','operation','schema']::text[];
        ELSE
            v_expected_keys := ARRAY['failure_id','operation','schema']::text[];
        END IF;
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'failure_id') IS DISTINCT FROM 'string'
           OR (v_operation = 'dismiss_failed'
               AND jsonb_typeof(p_request -> 'dismissed_at') IS DISTINCT FROM 'string') THEN
            RAISE EXCEPTION 'Invalid Labeling failure-state request'
                USING ERRCODE = '22023';
        END IF;
        v_failure_id := (p_request ->> 'failure_id')::uuid;
        SELECT failure.dismissed_at INTO v_dismissed_at
        FROM public.failed_predictions AS failure
        WHERE failure.id = v_failure_id
        FOR UPDATE;
        IF FOUND THEN
            IF v_operation = 'dismiss_failed' AND v_dismissed_at IS NULL THEN
                v_dismissed_at := (p_request ->> 'dismissed_at')::timestamptz;
                UPDATE public.failed_predictions AS failure
                SET dismissed_at = v_dismissed_at
                WHERE failure.id = v_failure_id;
                v_changed := true;
            ELSIF v_operation = 'restore_failed' AND v_dismissed_at IS NOT NULL THEN
                UPDATE public.failed_predictions AS failure
                SET dismissed_at = NULL
                WHERE failure.id = v_failure_id;
                v_dismissed_at := NULL;
                v_changed := true;
            END IF;
        END IF;
        v_data := jsonb_build_object(
            'operation', v_operation,
            'failure_id', v_failure_id,
            'changed', v_changed,
            'dismissed_at', v_dismissed_at
        );

    ELSIF v_operation IN ('delete_for_tasks','delete_for_batch') THEN
        IF v_operation = 'delete_for_tasks' THEN
            v_expected_keys := ARRAY['operation','schema','task_ids']::text[];
        ELSE
            v_expected_keys := ARRAY['batch_id','operation','schema']::text[];
        END IF;
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR (v_operation = 'delete_for_tasks'
               AND (jsonb_typeof(p_request -> 'task_ids') IS DISTINCT FROM 'array'
                    OR EXISTS (
                        SELECT 1 FROM jsonb_array_elements(p_request -> 'task_ids') AS item(value)
                        WHERE jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                    )))
           OR (v_operation = 'delete_for_batch'
               AND jsonb_typeof(p_request -> 'batch_id') IS DISTINCT FROM 'string') THEN
            RAISE EXCEPTION 'Invalid Labeling prediction-delete request'
                USING ERRCODE = '22023';
        END IF;
        IF v_operation = 'delete_for_tasks' THEN
            SELECT COALESCE(array_agg(DISTINCT value::uuid ORDER BY value::uuid), ARRAY[]::uuid[])
            INTO v_task_ids
            FROM jsonb_array_elements_text(p_request -> 'task_ids') AS ids(value);
        ELSE
            SELECT COALESCE(array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[])
            INTO v_task_ids
            FROM public.tasks AS task
            WHERE task.batch_id = (p_request ->> 'batch_id')::uuid;
        END IF;

        WITH removed AS (
            DELETE FROM public.prediction_metas AS meta
            WHERE meta.prediction_id IN (
                SELECT prediction.id FROM public.predictions AS prediction
                WHERE prediction.task_id = ANY(v_task_ids)
            )
               OR meta.failed_prediction_id IN (
                SELECT failure.id FROM public.failed_predictions AS failure
                WHERE failure.task_id = ANY(v_task_ids)
            )
            RETURNING meta.id
        )
        SELECT COALESCE(jsonb_agg(removed.id ORDER BY removed.id), '[]'::jsonb)
        INTO v_prediction_meta_ids
        FROM removed;

        WITH removed AS (
            DELETE FROM public.predictions AS prediction
            WHERE prediction.task_id = ANY(v_task_ids)
            RETURNING prediction.id, prediction.created_at
        )
        SELECT count(*),
               COALESCE(jsonb_agg(jsonb_build_object(
                   'id', removed.id, 'created_at', removed.created_at
               ) ORDER BY removed.id, removed.created_at), '[]'::jsonb)
        INTO v_prediction_count, v_predictions
        FROM removed;

        WITH removed AS (
            DELETE FROM public.failed_predictions AS failure
            WHERE failure.task_id = ANY(v_task_ids)
            RETURNING failure.id
        )
        SELECT count(*),
               COALESCE(jsonb_agg(removed.id ORDER BY removed.id), '[]'::jsonb)
        INTO v_failed_prediction_count, v_failed_prediction_ids
        FROM removed;
        v_data := jsonb_build_object(
            'operation', v_operation,
            'predictions', v_prediction_count,
            'failed_predictions', v_failed_prediction_count,
            'deleted_predictions', v_predictions,
            'deleted_failed_prediction_ids', v_failed_prediction_ids,
            'deleted_prediction_meta_ids', v_prediction_meta_ids
        );

    ELSIF v_operation = 'purge_import_predictions' THEN
        v_expected_keys := ARRAY[
            'operation','project_id','schema','source_scope','task_ids'
        ]::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'source_scope') IS DISTINCT FROM 'string'
           OR p_request ->> 'source_scope' NOT IN ('ml_backend','external_import','all')
           OR jsonb_typeof(p_request -> 'task_ids') NOT IN ('null','array')
           OR (jsonb_typeof(p_request -> 'task_ids') = 'array'
               AND EXISTS (
                   SELECT 1 FROM jsonb_array_elements(p_request -> 'task_ids') AS item(value)
                   WHERE jsonb_typeof(item.value) IS DISTINCT FROM 'string'
               )) THEN
            RAISE EXCEPTION 'Invalid Labeling imported-prediction purge request'
                USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_source_scope := p_request ->> 'source_scope';
        IF jsonb_typeof(p_request -> 'task_ids') = 'array' THEN
            v_task_scope := true;
            SELECT COALESCE(
                array_agg(DISTINCT value::uuid ORDER BY value::uuid),
                ARRAY[]::uuid[]
            ) INTO v_task_ids
            FROM jsonb_array_elements_text(p_request -> 'task_ids') AS ids(value);
        END IF;

        SELECT
            count(*) FILTER (WHERE prediction.source = 'ml_backend'),
            count(*) FILTER (WHERE prediction.source = 'external_import'),
            count(*) FILTER (
                WHERE prediction.source IS NULL
                   OR prediction.source NOT IN ('ml_backend','external_import')
            ),
            count(*),
            COALESCE(array_agg(prediction.id ORDER BY prediction.id), ARRAY[]::uuid[])
        INTO v_ml_backend_count, v_external_import_count, v_unknown_count,
             v_total_count, v_target_prediction_ids
        FROM public.predictions AS prediction
        WHERE prediction.project_id = v_project_id
          AND (NOT v_task_scope OR prediction.task_id = ANY(v_task_ids))
          AND (v_source_scope = 'all' OR prediction.source = v_source_scope);

        WITH removed AS (
            DELETE FROM public.prediction_metas AS meta
            WHERE EXISTS (
                SELECT 1 FROM public.predictions AS prediction
                WHERE prediction.id = meta.prediction_id
                  AND prediction.created_at = meta.prediction_created_at
                  AND prediction.id = ANY(v_target_prediction_ids)
                  AND prediction.project_id = v_project_id
                  AND (NOT v_task_scope OR prediction.task_id = ANY(v_task_ids))
                  AND (v_source_scope = 'all' OR prediction.source = v_source_scope)
            )
            RETURNING meta.id
        )
        SELECT COALESCE(jsonb_agg(removed.id ORDER BY removed.id), '[]'::jsonb)
        INTO v_prediction_meta_ids
        FROM removed;

        WITH removed AS (
            DELETE FROM public.predictions AS prediction
            WHERE prediction.id = ANY(v_target_prediction_ids)
              AND prediction.project_id = v_project_id
              AND (NOT v_task_scope OR prediction.task_id = ANY(v_task_ids))
              AND (v_source_scope = 'all' OR prediction.source = v_source_scope)
            RETURNING prediction.id, prediction.created_at
        )
        SELECT COALESCE(jsonb_agg(jsonb_build_object(
                   'id', removed.id, 'created_at', removed.created_at
               ) ORDER BY removed.id, removed.created_at), '[]'::jsonb)
        INTO v_predictions
        FROM removed;
        v_data := jsonb_build_object(
            'operation', v_operation,
            'ml_backend', v_ml_backend_count,
            'external_import', v_external_import_count,
            'unknown', v_unknown_count,
            'total', v_total_count,
            'deleted_predictions', v_predictions,
            'deleted_prediction_meta_ids', v_prediction_meta_ids
        );

    ELSIF v_operation = 'reject_candidate' THEN
        v_expected_keys := ARRAY[
            'operation','prediction_created_at','prediction_id','project_id',
            'schema','shape_index','task_id'
        ]::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'prediction_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'prediction_created_at') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'shape_index') NOT IN ('null','number')
           OR (jsonb_typeof(p_request -> 'shape_index') = 'number'
               AND (p_request ->> 'shape_index')::numeric <> trunc((p_request ->> 'shape_index')::numeric)) THEN
            RAISE EXCEPTION 'Invalid Labeling candidate-rejection request'
                USING ERRCODE = '22023';
        END IF;
        v_prediction_id := (p_request ->> 'prediction_id')::uuid;
        v_prediction_created_at := (p_request ->> 'prediction_created_at')::timestamptz;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_project_id := (p_request ->> 'project_id')::uuid;
        SELECT prediction.result, prediction.rejected_shape_indexes
        INTO v_shapes, v_rejected
        FROM public.predictions AS prediction
        WHERE prediction.id = v_prediction_id
          AND prediction.created_at = v_prediction_created_at
          AND prediction.task_id = v_task_id
          AND prediction.project_id = v_project_id
        FOR UPDATE;
        IF NOT FOUND THEN
            v_error_code := 'prediction_not_found';
        ELSIF jsonb_typeof(v_shapes) IS DISTINCT FROM 'array' THEN
            v_error_code := 'prediction_result_invalid';
        ELSE
            v_shape_count := jsonb_array_length(v_shapes);
            IF jsonb_typeof(p_request -> 'shape_index') = 'number' THEN
                v_shape_numeric := (p_request ->> 'shape_index')::numeric;
                IF v_shape_numeric < 0 OR v_shape_numeric >= v_shape_count THEN
                    v_error_code := 'shape_index_out_of_range';
                    v_error_details := jsonb_build_object(
                        'shape_index', p_request -> 'shape_index',
                        'shape_count', v_shape_count
                    );
                ELSE
                    v_shape_index := v_shape_numeric::integer;
                    IF jsonb_typeof(v_rejected) IS DISTINCT FROM 'array' THEN
                        v_rejected := '[]'::jsonb;
                    END IF;
                    IF NOT (v_rejected @> jsonb_build_array(v_shape_index)) THEN
                        v_rejected := v_rejected || jsonb_build_array(v_shape_index);
                    END IF;
                END IF;
            ELSE
                IF jsonb_typeof(v_rejected) IS DISTINCT FROM 'array' THEN
                    v_rejected := '[]'::jsonb;
                END IF;
                FOR v_shape_index IN SELECT generate_series(0, v_shape_count - 1) LOOP
                    IF NOT (v_rejected @> jsonb_build_array(v_shape_index)) THEN
                        v_rejected := v_rejected || jsonb_build_array(v_shape_index);
                    END IF;
                END LOOP;
            END IF;
            IF v_error_code IS NULL THEN
                UPDATE public.predictions AS prediction
                SET rejected_shape_indexes = v_rejected
                WHERE prediction.id = v_prediction_id
                  AND prediction.created_at = v_prediction_created_at;
                v_data := jsonb_build_object(
                    'operation', v_operation,
                    'prediction_id', v_prediction_id,
                    'created_at', v_prediction_created_at,
                    'task_id', v_task_id,
                    'project_id', v_project_id,
                    'rejected_shape_indexes', v_rejected
                );
            END IF;
        END IF;

    ELSE
        RAISE EXCEPTION 'Unsupported Labeling Prediction record operation'
            USING ERRCODE = '22023';
    END IF;

    IF v_error_code IS NOT NULL THEN
        RETURN jsonb_build_object(
            'schema', 'aap-labeling-prediction-records/v1',
            'ok', false,
            'request', p_request,
            'data', NULL,
            'error', jsonb_build_object(
                'code', v_error_code,
                'details', COALESCE(v_error_details, '{}'::jsonb)
            )
        );
    END IF;
    RETURN jsonb_build_object(
        'schema', 'aap-labeling-prediction-records/v1',
        'ok', true,
        'request', p_request,
        'data', v_data,
        'error', NULL
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.prediction_record_command(jsonb) FROM PUBLIC;
