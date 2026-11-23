CREATE FUNCTION yoao_labeling_v1.finish_prediction_retry_pipeline_failure(
    p_payload jsonb, p_pipeline_errors jsonb
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
AS $function$
DECLARE
    v_job_id uuid;
    v_previous_state text;
    v_reply jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_pipeline_errors) IS DISTINCT FROM 'array'
       OR pg_catalog.octet_length(p_pipeline_errors::text) > 2000000
       OR NOT yoao_labeling_v1._prediction_retry_no_raw_data(p_pipeline_errors) THEN
        RETURN yoao_labeling_v1._prediction_retry_reply(NULL, 'invalid_request');
    END IF;
    v_job_id := (p_payload->>'job_id')::uuid;
    SELECT attempt.state INTO v_previous_state
    FROM public.prediction_retry_attempts AS attempt
    WHERE attempt.job_id = v_job_id
    FOR UPDATE;

    v_reply := yoao_labeling_v1.finish_prediction_retry_failure(p_payload);
    IF v_reply->>'ok' = 'true' AND v_previous_state IN ('accepted', 'applying', 'failing') THEN
        UPDATE public.failed_predictions AS failure
        SET extra = COALESCE(failure.extra, '{}'::jsonb)
            || pg_catalog.jsonb_build_object('last_pipeline_errors', p_pipeline_errors)
        FROM public.prediction_retry_attempts AS attempt
        WHERE attempt.job_id = v_job_id AND attempt.state IN ('failed', 'abandoned')
          AND failure.id = attempt.failed_prediction_id
          AND failure.project_id = attempt.project_id
          AND failure.task_id IS NOT DISTINCT FROM attempt.task_id
          AND failure.retry_count = (attempt.outcome->>'retry_count')::integer
          AND failure.extra->>'retry_job_id' IS NULL;
    END IF;
    RETURN v_reply;
EXCEPTION WHEN invalid_text_representation THEN
    RETURN yoao_labeling_v1._prediction_retry_reply(NULL, 'invalid_request');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1.finish_prediction_retry_pipeline_failure(jsonb,jsonb) FROM PUBLIC;
