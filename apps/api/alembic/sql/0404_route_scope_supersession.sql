-- The original boot is captured at reservation, before any later health refresh.
ALTER TABLE aap_inference_routing_v1.scopes
    ADD COLUMN reservation_boot_id text,
    ADD COLUMN reservation_eligibility_revision bigint;
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1.capture_scope_boot()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    SELECT r.health_meta #>> '{residency,boot_id}' INTO NEW.reservation_boot_id
      FROM public.ml_backend_registry AS r WHERE r.id=NEW.registry_id;
    NEW.reservation_eligibility_revision := NEW.eligibility_revision;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1.capture_scope_boot() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER aap_route_capture_boot BEFORE INSERT ON aap_inference_routing_v1.scopes
FOR EACH ROW EXECUTE FUNCTION aap_inference_routing_v1.capture_scope_boot();
-- aap-statement-break
-- Keep the original capability stable while the checked eligibility snapshot advances.
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
           s.generation, s.config_revision,
           COALESCE(s.reservation_eligibility_revision, s.eligibility_revision) AS eligibility_revision,
           s.authority_epoch, s.realm
      INTO v_scope FROM aap_inference_routing_v1.scopes AS s WHERE s.id=p_scope_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
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
-- Supersede only an unbegun reservation using a fresh, transaction-bound owner snapshot.
CREATE FUNCTION aap_inference_routing_v1.supersede_unbegun_scope(p_args text)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    args jsonb := p_args::jsonb;
    snapshot jsonb;
    selected jsonb;
    scope aap_inference_routing_v1.scopes%ROWTYPE;
    state aap_inference_routing_v1.registry_state%ROWTYPE;
    registry public.ml_backend_registry%ROWTYPE;
    member_count integer;
    v_scope_id uuid;
    v_member_id uuid;
    v_capability text;
    v_boot_id text;
BEGIN
    IF NOT aap_inference_routing_v1._routing_exact_keys(
        args, ARRAY['scope_id','capability','snapshot','member_id','boot_id']
    ) OR pg_catalog.jsonb_typeof(args->'capability') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid route supersession';
    END IF;
    v_scope_id := aap_inference_routing_v1._routing_uuid(args->>'scope_id');
    v_capability := args->>'capability';
    IF v_capability !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid route supersession';
    END IF;
    SELECT s.* INTO scope FROM aap_inference_routing_v1.scopes AS s WHERE s.id=v_scope_id FOR UPDATE;
    IF NOT FOUND OR aap_inference_routing_v1._routing_scope_capability(v_scope_id) IS DISTINCT FROM v_capability
       OR scope.state IS DISTINCT FROM 'reserved' OR scope.closed
       OR scope.heartbeat_deadline <= pg_catalog.clock_timestamp()
       OR scope.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm()
       OR EXISTS (SELECT 1 FROM aap_inference_routing_v1.attempts AS a WHERE a.scope_id=v_scope_id) THEN
        RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
    END IF;
    IF args->'snapshot' = 'null'::jsonb THEN
        IF args->'member_id' IS DISTINCT FROM 'null'::jsonb
           OR args->'boot_id' IS DISTINCT FROM 'null'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid route supersession';
        END IF;
        PERFORM r.id FROM public.ml_backend_registry AS r WHERE r.id=scope.registry_id FOR SHARE NOWAIT;
        IF NOT FOUND THEN
            RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
        END IF;
        SELECT s.* INTO state FROM aap_inference_routing_v1.registry_state AS s WHERE s.registry_id=scope.registry_id;
        IF state.registry_id IS NULL OR state.realm IS DISTINCT FROM scope.realm THEN
            RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
        END IF;
        RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
            'scope', aap_inference_routing_v1._routing_scope_view(v_scope_id, true),
            'stale', state.eligibility_revision IS DISTINCT FROM scope.eligibility_revision
        ));
    END IF;
    IF pg_catalog.jsonb_typeof(args->'snapshot') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(args->'boot_id') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid route supersession';
    END IF;
    v_member_id := aap_inference_routing_v1._routing_uuid(args->>'member_id');
    v_boot_id := args->>'boot_id';
    IF pg_catalog.char_length(v_boot_id) NOT BETWEEN 1 AND 128 THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid route supersession';
    END IF;
    snapshot := aap_inference_routing_v1._routing_open_snapshot(args->>'snapshot');
    IF NOT aap_inference_routing_v1._routing_exact_keys(
        snapshot, ARRAY['schema','realm','xid','mode','pool_id','physical_pool_id',
                        'physical_generation','registry_id','project_id','required_instance_id',
                        'generation','health_max_age_seconds','pool_enabled','legacy_instance_id','members']
    ) OR snapshot->>'schema' IS DISTINCT FROM 'aap-inference-routing-snapshot/v1'
      OR snapshot->>'realm' IS DISTINCT FROM aap_inference_routing_v1._routing_realm()
      OR snapshot->>'xid' IS DISTINCT FROM pg_catalog.pg_current_xact_id()::text
      OR pg_catalog.jsonb_typeof(snapshot->'members') IS DISTINCT FROM 'array' THEN
        RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
    END IF;
    IF snapshot->>'mode' IS DISTINCT FROM scope.mode
       OR snapshot->>'pool_id' IS DISTINCT FROM scope.pool_id::text
       OR snapshot->>'project_id' IS DISTINCT FROM scope.project_id::text
       OR snapshot->>'required_instance_id' IS DISTINCT FROM scope.registry_id::text
       OR snapshot->>'generation' IS DISTINCT FROM scope.generation::text
       OR snapshot->'pool_enabled' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
    END IF;
    SELECT pg_catalog.count(*), pg_catalog.min(m.value::text)::jsonb
    INTO member_count, selected
    FROM pg_catalog.jsonb_array_elements(snapshot->'members') AS m(value)
    WHERE m.value->>'registry_id'=scope.registry_id::text
      AND m.value->>'member_id'=v_member_id::text
      AND m.value->>'pool_id'=scope.pool_id::text
      AND m.value->>'traffic_state'='active';
    IF member_count <> 1 OR selected->>'config_revision' IS DISTINCT FROM scope.config_revision::text
       OR selected->>'authority_epoch' IS DISTINCT FROM scope.authority_epoch::text THEN
        RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
    END IF;
    SELECT s.* INTO state FROM aap_inference_routing_v1.registry_state AS s WHERE s.registry_id=scope.registry_id;
    SELECT r.* INTO registry FROM public.ml_backend_registry AS r WHERE r.id=scope.registry_id;
    IF state.registry_id IS NULL OR registry.id IS NULL
       OR state.realm IS DISTINCT FROM scope.realm
       OR state.config_revision IS DISTINCT FROM scope.config_revision
       OR scope.reservation_boot_id IS DISTINCT FROM v_boot_id
       OR state.authority_epoch IS DISTINCT FROM scope.authority_epoch
       OR selected->>'eligibility_revision' IS DISTINCT FROM state.eligibility_revision::text
       OR registry.state IS DISTINCT FROM 'connected'
       OR registry.last_checked_at IS NULL
       OR registry.last_checked_at < pg_catalog.clock_timestamp() - pg_catalog.make_interval(secs => (snapshot->>'health_max_age_seconds')::integer)
       OR registry.health_meta #>> '{residency,boot_id}' IS DISTINCT FROM v_boot_id THEN
        RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
    END IF;
    UPDATE aap_inference_routing_v1.scopes AS s
       SET eligibility_revision=state.eligibility_revision, updated_at=pg_catalog.now()
     WHERE s.id=v_scope_id;
    RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object(
        'scope', aap_inference_routing_v1._routing_scope_view(v_scope_id, true)
    ));
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1.supersede_unbegun_scope(text) FROM PUBLIC;
-- aap-statement-break
DO $owner$
DECLARE owner_name text;
BEGIN
    SELECT r.rolname INTO owner_name FROM pg_catalog.pg_namespace n
    JOIN pg_catalog.pg_roles r ON r.oid=n.nspowner WHERE n.nspname='aap_inference_routing_v1';
    EXECUTE pg_catalog.format('ALTER FUNCTION aap_inference_routing_v1.capture_scope_boot() OWNER TO %I', owner_name);
    EXECUTE pg_catalog.format('ALTER FUNCTION aap_inference_routing_v1.supersede_unbegun_scope(text) OWNER TO %I', owner_name);
END;
$owner$;
