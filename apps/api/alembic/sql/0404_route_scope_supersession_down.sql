DO $guard$
BEGIN
    IF EXISTS (SELECT 1 FROM aap_inference_routing_v1.attempts
               WHERE state='not_sent' OR dispatch_state='prepared') THEN
        RAISE EXCEPTION 'Cannot downgrade while no-send history or prepared attempts exist';
    END IF;
    IF EXISTS (
        SELECT 1 FROM aap_inference_routing_v1.scopes
         WHERE NOT closed AND reservation_eligibility_revision IS NOT NULL
           AND reservation_eligibility_revision IS DISTINCT FROM eligibility_revision
    ) THEN
        RAISE EXCEPTION 'Cannot downgrade while superseded route scopes remain open';
    END IF;
END;
$guard$;
-- aap-statement-break
DROP FUNCTION aap_inference_routing_v1.model_dispatch_step(text);
-- aap-statement-break
DROP TRIGGER aap_route_guard_model_attempt ON aap_inference_routing_v1.attempts;
-- aap-statement-break
DROP FUNCTION aap_inference_routing_v1.guard_model_attempt();
-- aap-statement-break
ALTER TABLE aap_inference_routing_v1.attempts
    DROP CONSTRAINT attempts_state_check,
    ADD CONSTRAINT attempts_state_check CHECK (state IN ('active','response','uncertain','reconciled')),
    DROP COLUMN dispatch_binding_sha256,
    DROP COLUMN dispatch_xid,
    DROP COLUMN dispatch_state;
-- aap-statement-break
DROP FUNCTION aap_inference_routing_v1.supersede_unbegun_scope(text);
-- aap-statement-break
DROP TRIGGER aap_route_capture_boot ON aap_inference_routing_v1.scopes;
-- aap-statement-break
DROP FUNCTION aap_inference_routing_v1.capture_scope_boot();
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_scope_capability(p_scope_id uuid)
RETURNS text
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_scope record;
    v_secret bytea;
    v_payload text;
BEGIN
    SELECT s.id, s.registry_id, s.pool_id, s.project_id, s.mode, s.owner, s.operation,
           s.generation, s.config_revision, s.eligibility_revision, s.authority_epoch, s.realm
      INTO v_scope FROM aap_inference_routing_v1.scopes AS s WHERE s.id=p_scope_id;
    IF NOT FOUND THEN RETURN NULL; END IF;
    v_secret := aap_inference_routing_v1._routing_secret();
    IF v_secret IS NULL OR v_scope.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm() THEN
        RETURN NULL;
    END IF;
    v_payload := pg_catalog.jsonb_build_object(
        'schema', 'aap-inference-routing-capability/v1', 'database', v_scope.realm,
        'id', v_scope.id, 'registry_id', v_scope.registry_id, 'pool_id', v_scope.pool_id,
        'project_id', v_scope.project_id, 'mode', v_scope.mode, 'owner', v_scope.owner,
        'operation', v_scope.operation, 'generation', v_scope.generation,
        'config_revision', v_scope.config_revision, 'eligibility_revision', v_scope.eligibility_revision,
        'authority_epoch', v_scope.authority_epoch
    )::text;
    RETURN pg_catalog.encode(public.hmac(pg_catalog.convert_to(v_payload, 'UTF8'), v_secret, 'sha256'), 'hex');
END;
$function$;
-- aap-statement-break
ALTER TABLE aap_inference_routing_v1.scopes
    DROP COLUMN reservation_boot_id,
    DROP COLUMN reservation_eligibility_revision;
