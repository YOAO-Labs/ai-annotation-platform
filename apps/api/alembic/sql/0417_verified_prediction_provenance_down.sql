-- Source evidence is validated by the writing transaction, not the stored model payload.
DO $migration$
DECLARE
    v_before text := $before$       AND (v_meta.extra - ARRAY[
           'reference_input','reference_execution','reference_native',
           'original_input','original_video_input','video_prediction_proof'
       ]) IS NOT DISTINCT FROM v_expected_extra;$before$;
    v_after text := $after$       AND v_meta.extra IS NOT DISTINCT FROM v_expected_extra;$after$;
    v_definition text := pg_catalog.pg_get_functiondef(
        'aap_labeling_v1._batch_result_prediction_matches(public.batch_result_payloads,jsonb)'::pg_catalog.regprocedure
    );
BEGIN
    IF v_definition IS NULL
       OR pg_catalog.length(v_definition) - pg_catalog.length(pg_catalog.replace(v_definition, v_before, '')) <> pg_catalog.length(v_before)
       OR pg_catalog.strpos(v_definition, v_after) <> 0 THEN
        RAISE EXCEPTION 'Unexpected durable prediction metadata comparison';
    END IF;
    EXECUTE pg_catalog.replace(v_definition, v_before, v_after);
END
$migration$;
