-- Permit the routing owner's optional, digest-bound approved candidate list.
DO $migration$
DECLARE
    v_before text := $before$OR NOT aap_jobs_v1._video_execution_exact_keys(
                v_command -> 'body', ARRAY[
                    'pool_id','registry_id','project_id','mode','required_instance_id',
                    'owner','operation','execution'
                ]
           )$before$;
    v_after text := $after$OR NOT aap_jobs_v1._video_execution_exact_keys(
                (v_command -> 'body') - 'approved_registry_ids', ARRAY[
                    'pool_id','registry_id','project_id','mode','required_instance_id',
                    'owner','operation','execution'
                ]
           )
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
              ELSE false END)$after$;
    v_definition text := pg_catalog.pg_get_functiondef(
        'aap_jobs_v1._video_execution_route_step(text,text)'::pg_catalog.regprocedure
    );
BEGIN
    IF v_definition IS NULL
       OR pg_catalog.length(v_definition) - pg_catalog.length(pg_catalog.replace(v_definition, v_before, '')) <> pg_catalog.length(v_before)
       OR pg_catalog.strpos(v_definition, v_after) <> 0 THEN
        RAISE EXCEPTION 'Unexpected video route command guard';
    END IF;
    EXECUTE pg_catalog.replace(v_definition, v_before, v_after);
END
$migration$;
