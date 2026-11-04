LOCK TABLE aap_inference_routing_v1.control_attempts IN SHARE ROW EXCLUSIVE MODE;
-- aap-statement-break
DO $migration$
DECLARE
    v_before text := $before$v_path NOT IN ('/unload', '/lifecycle/unload', '/lifecycle/terminal-drain')$before$;
    v_after text := $after$v_path NOT IN ('/unload', '/lifecycle/unload')$after$;
    v_definition text := pg_catalog.pg_get_functiondef(
        'aap_inference_routing_v1._routing_step_0279(text,text)'::pg_catalog.regprocedure
    );
BEGIN
    IF EXISTS (
        SELECT 1 FROM aap_inference_routing_v1.control_attempts
        WHERE path = '/lifecycle/terminal-drain' AND state IN ('active', 'uncertain')
    ) THEN
        RAISE EXCEPTION 'Cannot downgrade with unresolved terminal drain attempts';
    END IF;
    IF v_definition IS NULL
       OR pg_catalog.length(v_definition) - pg_catalog.length(pg_catalog.replace(v_definition, v_before, '')) <> pg_catalog.length(v_before)
       OR pg_catalog.strpos(v_definition, v_after) <> 0 THEN
        RAISE EXCEPTION 'Unexpected native routing control path guard';
    END IF;
    EXECUTE pg_catalog.replace(v_definition, v_before, v_after);
END
$migration$;
