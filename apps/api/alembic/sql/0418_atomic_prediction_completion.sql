-- Keep replacement, qualified candidates and partial-failure receipts in one transaction.
DO $migration$
DECLARE
    v_definition text := pg_catalog.pg_get_functiondef('aap_jobs_v1.mark_batch_result_overwrite(jsonb)'::pg_catalog.regprocedure);
    v_before_0 text := $before0$    IF v_member.state NOT IN ('planned', 'prepared') OR v_member.result_id IS NOT NULL
$before0$;
    v_after_0 text := $after0$    IF v_member.state <> 'ready' OR v_member.result_id IS NULL
$after0$;
BEGIN
    IF v_definition IS NULL
       OR pg_catalog.length(v_definition) - pg_catalog.length(pg_catalog.replace(v_definition, v_before_0, '')) <> pg_catalog.length(v_before_0) THEN
        RAISE EXCEPTION 'Unexpected definition of aap_jobs_v1.mark_batch_result_overwrite(jsonb)';
    END IF;
    v_definition := pg_catalog.replace(v_definition, v_before_0, v_after_0);
    EXECUTE v_definition;
END
$migration$;
-- aap-statement-break

DO $migration$
DECLARE
    v_definition text := pg_catalog.pg_get_functiondef('aap_labeling_v1.finish_batch_result_write(jsonb)'::pg_catalog.regprocedure);
    v_before_0 text := $before0$    v_count bigint;
BEGIN$before0$;
    v_after_0 text := $after0$    v_count bigint;
    v_failed public.failed_predictions%ROWTYPE;
    v_partial boolean;
BEGIN$after0$;
    v_before_1 text := $before1$    v_advance := aap_jobs_v1.advance_batch_result_member(
        v_scope || pg_catalog.jsonb_build_object(
            'generation',(p_payload ->> 'generation')::bigint,
            'lease_token',p_payload ->> 'lease_token',
            'ordinal',v_ordinal,
            'task_id',v_bundle.task_id,
            'result_id',v_result,
            'failed_prediction_id',NULL,
            'error_code',NULL,
            'outcome','applied',
            'context',v_bundle.context
        )
    );$before1$;
    v_after_1 text := $after1$    v_partial := COALESCE(v_bundle.context -> 'pipeline_partial' = 'true'::jsonb, false);
    IF v_partial THEN
        SELECT * INTO v_failed FROM public.failed_predictions
        WHERE id = (v_member ->> 'failure_id')::uuid FOR KEY SHARE;
        IF NOT FOUND
           OR v_failed.project_id IS DISTINCT FROM (v_scope ->> 'project_id')::uuid
           OR v_failed.task_id IS DISTINCT FROM v_bundle.task_id
           OR v_failed.error_type IS DISTINCT FROM 'pipeline_partial'
           OR v_failed.execution_snapshot_id IS DISTINCT FROM
               aap_jobs_v1.read_job_execution_snapshot((v_scope ->> 'job_id')::uuid)
           OR v_failed.extra -> 'partial_prediction_ids' IS DISTINCT FROM (
               SELECT COALESCE(pg_catalog.jsonb_agg(payload.prediction_id::text ORDER BY payload.request_index), '[]'::jsonb)
               FROM public.batch_result_payloads AS payload
               WHERE payload.result_id = v_result AND payload.stage = 'prepared'
           ) THEN
            RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='result_conflict';
        END IF;
    END IF;
    v_advance := aap_jobs_v1.advance_batch_result_member(
        v_scope || pg_catalog.jsonb_build_object(
            'generation',(p_payload ->> 'generation')::bigint,
            'lease_token',p_payload ->> 'lease_token',
            'ordinal',v_ordinal,
            'task_id',v_bundle.task_id,
            'result_id',v_result,
            'failed_prediction_id',CASE WHEN v_partial THEN v_failed.id ELSE NULL END,
            'error_code',CASE WHEN v_partial THEN 'pipeline_partial' ELSE NULL END,
            'outcome',CASE WHEN v_partial THEN 'failed' ELSE 'applied' END,
            'context',v_bundle.context
        )
    );$after1$;
BEGIN
    IF v_definition IS NULL
       OR pg_catalog.length(v_definition) - pg_catalog.length(pg_catalog.replace(v_definition, v_before_0, '')) <> pg_catalog.length(v_before_0) THEN
        RAISE EXCEPTION 'Unexpected definition of aap_labeling_v1.finish_batch_result_write(jsonb)';
    END IF;
    v_definition := pg_catalog.replace(v_definition, v_before_0, v_after_0);
    IF v_definition IS NULL
       OR pg_catalog.length(v_definition) - pg_catalog.length(pg_catalog.replace(v_definition, v_before_1, '')) <> pg_catalog.length(v_before_1) THEN
        RAISE EXCEPTION 'Unexpected definition of aap_labeling_v1.finish_batch_result_write(jsonb)';
    END IF;
    v_definition := pg_catalog.replace(v_definition, v_before_1, v_after_1);
    EXECUTE v_definition;
END
$migration$;
