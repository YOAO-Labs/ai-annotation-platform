CREATE OR REPLACE FUNCTION aap_labeling_v1._batch_result_prediction_matches(
    p_payload public.batch_result_payloads,
    p_body jsonb
)
RETURNS boolean
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_prediction public.predictions%ROWTYPE;
    v_meta public.prediction_metas%ROWTYPE;
    v_token jsonb;
    v_expected_extra jsonb;
BEGIN
    SELECT * INTO v_prediction
    FROM public.predictions AS prediction
    WHERE prediction.id = p_payload.prediction_id
      AND prediction.created_at = p_payload.prediction_created_at;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT * INTO v_meta
    FROM public.prediction_metas AS meta
    WHERE meta.prediction_id = p_payload.prediction_id
      AND meta.prediction_created_at = p_payload.prediction_created_at;
    IF NOT FOUND THEN RETURN false; END IF;
    v_token := CASE WHEN p_body -> 'token_meta' IS NULL OR p_body -> 'token_meta' = 'null'::jsonb
        THEN '{}'::jsonb ELSE p_body -> 'token_meta' END;
    IF pg_catalog.jsonb_typeof(v_token) IS DISTINCT FROM 'object' THEN RETURN false; END IF;
    v_expected_extra := CASE
        WHEN p_body -> 'pipeline_extra' IS NULL OR p_body -> 'pipeline_extra' = 'null'::jsonb
            THEN '{}'::jsonb ELSE p_body -> 'pipeline_extra' END;
    v_expected_extra := v_expected_extra - ARRAY[
        'reference_input','reference_execution','reference_native',
        'original_input','original_video_input','video_prediction_proof'
    ];
    IF p_body -> 'model_provenance' IS NOT NULL
       AND p_body -> 'model_provenance' <> 'null'::jsonb
       AND p_body -> 'model_provenance' <> '{}'::jsonb THEN
        v_expected_extra := v_expected_extra || pg_catalog.jsonb_build_object(
            'trained_model', p_body -> 'model_provenance'
        );
    END IF;
    IF p_body -> 'model_provenance' IS NOT NULL
       AND p_body -> 'model_provenance' <> 'null'::jsonb
       AND p_body -> 'model_provenance' <> '{}'::jsonb
       AND v_token ? 'inference_runtime' THEN
        v_expected_extra := v_expected_extra || pg_catalog.jsonb_build_object(
            'inference_runtime', v_token -> 'inference_runtime'
        );
    END IF;
    IF v_token ? 'segmentation' THEN
        v_expected_extra := v_expected_extra || pg_catalog.jsonb_build_object(
            'segmentation', v_token -> 'segmentation'
        );
    END IF;
    IF p_body -> 'accepted_video_input' IS NOT NULL AND p_body -> 'accepted_video_input' <> 'null'::jsonb THEN
        v_expected_extra := v_expected_extra || pg_catalog.jsonb_build_object(
            'accepted_video_input',p_body -> 'accepted_video_input',
            'accepted_video_execution_job_id',p_body #> '{write_scope,job_id}'
        );
    END IF;
    RETURN v_prediction.task_id IS NOT DISTINCT FROM (p_body ->> 'task_id')::uuid
       AND v_prediction.project_id IS NOT DISTINCT FROM (p_body ->> 'project_id')::uuid
       AND v_prediction.ml_backend_id IS NOT DISTINCT FROM NULLIF(p_body ->> 'ml_backend_id','')::uuid
       AND v_prediction.ml_backend_pool_id IS NOT DISTINCT FROM NULLIF(p_body ->> 'ml_backend_pool_id','')::uuid
       AND v_prediction.model_version IS NOT DISTINCT FROM p_body ->> 'model_version'
       AND v_prediction.score IS NOT DISTINCT FROM NULLIF(p_body ->> 'score','')::double precision
       AND v_prediction.tool_unit_id IS NOT DISTINCT FROM p_payload.tool_unit_id
       AND v_prediction.result IS NOT DISTINCT FROM p_body -> 'result'
       AND v_prediction.source IS NOT DISTINCT FROM COALESCE(p_body ->> 'source','ml_backend')
       AND v_prediction.cluster IS NULL
       AND v_prediction.mislabeling IS NULL
       AND v_prediction.rejected_shape_indexes IS NOT DISTINCT FROM '[]'::jsonb
       AND v_meta.failed_prediction_id IS NULL
       AND v_meta.inference_time_ms IS NOT DISTINCT FROM NULLIF(p_body ->> 'inference_time_ms','')::integer
       AND v_meta.prompt_tokens IS NOT DISTINCT FROM NULLIF(v_token ->> 'prompt_tokens','')::integer
       AND v_meta.completion_tokens IS NOT DISTINCT FROM NULLIF(v_token ->> 'completion_tokens','')::integer
       AND v_meta.total_tokens IS NOT DISTINCT FROM NULLIF(v_token ->> 'total_tokens','')::integer
       AND v_meta.prompt_cost IS NOT DISTINCT FROM NULLIF(v_token ->> 'prompt_cost','')::double precision
       AND v_meta.completion_cost IS NOT DISTINCT FROM NULLIF(v_token ->> 'completion_cost','')::double precision
       AND v_meta.total_cost IS NOT DISTINCT FROM NULLIF(v_token ->> 'total_cost','')::double precision
       AND v_meta.extra IS NOT DISTINCT FROM v_expected_extra;
END
$function$;
