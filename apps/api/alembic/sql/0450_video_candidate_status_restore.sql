-- Match the admitted, digest-bound candidate contract without changing frozen SQL.
DO $migration$
DECLARE
    v_signature text;
    v_definition text;
    v_body_before text := $before$v_command -> 'body',ARRAY[$before$;
    v_body_after text := $after$(v_command -> 'body') - 'approved_registry_ids',ARRAY[$after$;
    v_guard text := $guard$OR v_command ->> 'operation' IS DISTINCT FROM 'video_acquire'$guard$;
    v_candidates text := $candidates$
           OR (CASE WHEN (v_command -> 'body') ? 'approved_registry_ids' THEN
                CASE pg_catalog.jsonb_typeof(v_command #> '{body,approved_registry_ids}')
                    WHEN 'null' THEN false
                    WHEN 'array' THEN
                        pg_catalog.jsonb_array_length(v_command #> '{body,approved_registry_ids}') > 1024
                        OR EXISTS (
                            SELECT 1 FROM pg_catalog.jsonb_array_elements(
                                v_command #> '{body,approved_registry_ids}'
                            ) AS candidate(value)
                            WHERE pg_catalog.jsonb_typeof(candidate.value) IS DISTINCT FROM 'string'
                               OR candidate.value #>> '{}' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                        )
                        OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_array_elements(
                                v_command #> '{body,approved_registry_ids}'
                            )) <> (SELECT pg_catalog.count(DISTINCT candidate.value)
                                   FROM pg_catalog.jsonb_array_elements(
                                       v_command #> '{body,approved_registry_ids}'
                                   ) AS candidate(value))
                    ELSE true
                END
              ELSE false END)$candidates$;
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'aap_inference_routing_v1.video_execution_scope_step(text,text)',
        'aap_jobs_v1._video_restore_route_snapshot(bytea,text)'
    ] LOOP
        v_definition := pg_catalog.pg_get_functiondef(v_signature::pg_catalog.regprocedure);
        IF v_definition IS NULL
           OR pg_catalog.length(v_definition) - pg_catalog.length(
                pg_catalog.replace(v_definition, v_body_before, '')
              ) <> pg_catalog.length(v_body_before)
           OR pg_catalog.length(v_definition) - pg_catalog.length(
                pg_catalog.replace(v_definition, v_guard, '')
              ) <> pg_catalog.length(v_guard)
           OR pg_catalog.strpos(v_definition, v_body_after) <> 0 THEN
            RAISE EXCEPTION 'Unexpected video candidate verifier: %', v_signature;
        END IF;
        v_definition := pg_catalog.replace(v_definition, v_body_before, v_body_after);
        EXECUTE pg_catalog.replace(v_definition, v_guard, v_guard || v_candidates);
    END LOOP;
END
$migration$;
