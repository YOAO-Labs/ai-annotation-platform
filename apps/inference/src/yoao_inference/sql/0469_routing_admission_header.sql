-- Old senders, providers and GPU workers must be physically drained offline.
-- The database checks its own receipts, not external processes or bearer expiry.
LOCK TABLE aap_inference_routing_v1.scopes, aap_inference_routing_v1.attempts,
    aap_inference_routing_v1.controls, aap_inference_routing_v1.control_attempts,
    aap_inference_routing_v1.scope_retirements IN EXCLUSIVE MODE NOWAIT;
-- aap-statement-break
DO $cutover$
DECLARE
    capability record;
    definition text;
BEGIN
    IF EXISTS (
        SELECT FROM pg_catalog.pg_stat_activity AS c
        WHERE c.datname = current_database() AND c.pid <> pg_backend_pid()
          AND c.backend_type = 'client backend' AND c.usesysid IS NOT NULL
          AND pg_catalog.has_schema_privilege(c.usesysid, 'aap_inference_routing_v1', 'USAGE')
          AND pg_catalog.has_function_privilege(c.usesysid,
              'aap_inference_routing_v1.routing_step(text,text)', 'EXECUTE')
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '55006', MESSAGE = 'Drain routing clients before control wire cutover';
    END IF;
    IF EXISTS (SELECT FROM aap_inference_routing_v1.scopes WHERE NOT closed OR state IN ('reserved','active','uncertain'))
       OR EXISTS (SELECT FROM aap_inference_routing_v1.attempts WHERE state IN ('active','uncertain'))
       OR EXISTS (SELECT FROM aap_inference_routing_v1.controls WHERE phase <> 'completed')
       OR EXISTS (SELECT FROM aap_inference_routing_v1.control_attempts WHERE state IN ('active','uncertain'))
       OR EXISTS (SELECT FROM aap_inference_routing_v1.scope_retirements WHERE phase <> 'completed') THEN
        RAISE EXCEPTION USING ERRCODE = '55006', MESSAGE = 'Settle routing scopes, attempts and controls before cutover';
    END IF;
    -- Preserve owners, ACLs and all existing token/receipt data. Only the two
    -- reviewed bodies installed through the terminal-drain revision may move.
    FOR capability IN SELECT * FROM (VALUES
        ('aap_inference_routing_v1._routing_step_0279(text,text)'::regprocedure,
         'ea8889f55fe63767b5e446f015104d70caaa94b1a390cc269f2189c4b5319805'),
        ('aap_inference_routing_v1._routing_receipt_safe(jsonb)'::regprocedure,
         '29515cafd76a731f1855e9bc5197bf3d9acd1c0b50ec06e6d7e991add1e460bc')
    ) AS expected(proc, body_sha) LOOP
        SELECT pg_catalog.pg_get_functiondef(p.oid) INTO STRICT definition
        FROM pg_catalog.pg_proc AS p WHERE p.oid = capability.proc
          AND p.prosecdef AND p.proconfig @> ARRAY['search_path=pg_catalog, pg_temp']
          AND pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p.prosrc, 'UTF8')), 'hex') = capability.body_sha;
        EXECUTE pg_catalog.replace(definition, 'X-AAP-Route-Admission', 'X-YOAO-Route-Admission');
    END LOOP;
END;
$cutover$;
