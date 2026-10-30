-- The original boot is captured at reservation, before any later health refresh.
ALTER TABLE aap_inference_routing_v1.scopes
    ADD COLUMN reservation_boot_id text,
    ADD COLUMN reservation_eligibility_revision bigint;
-- aap-statement-break
-- Old attempts remain unmanaged: they cannot acquire a retroactive no-send proof.
ALTER TABLE aap_inference_routing_v1.attempts
    ADD COLUMN dispatch_state text NOT NULL DEFAULT 'unmanaged'
        CHECK (dispatch_state IN ('unmanaged','prepared','started')),
    ADD COLUMN dispatch_xid xid8,
    ADD COLUMN dispatch_binding_sha256 bytea;
-- aap-statement-break
ALTER TABLE aap_inference_routing_v1.attempts
    ALTER COLUMN dispatch_xid SET DEFAULT pg_catalog.pg_current_xact_id(),
    DROP CONSTRAINT attempts_state_check,
    ADD CONSTRAINT attempts_state_check CHECK (state IN ('active','response','uncertain','reconciled','not_sent'));
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1.guard_model_attempt()
RETURNS trigger LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD' SET extra_float_digits TO 3
AS $function$
BEGIN
    IF OLD.state = 'not_sent' AND
       (NEW.state IS DISTINCT FROM OLD.state OR NEW.reason IS DISTINCT FROM OLD.reason
        OR NEW.status_code IS DISTINCT FROM OLD.status_code) THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='not-sent attempt is terminal';
    END IF;
    IF OLD.state='active' AND OLD.dispatch_state='prepared'
       AND NEW.state IN ('uncertain','reconciled') THEN
        NEW.state := 'not_sent';
        NEW.reason := 'not_sent';
        NEW.status_code := NULL;
    ELSIF OLD.state='active' AND OLD.dispatch_state='prepared' AND NEW.state='response' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='model dispatch was not started';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1.guard_model_attempt() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER aap_route_guard_model_attempt BEFORE UPDATE ON aap_inference_routing_v1.attempts
FOR EACH ROW EXECUTE FUNCTION aap_inference_routing_v1.guard_model_attempt();
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
-- The start transition is the dispatch authorization linearization point.
CREATE FUNCTION aap_inference_routing_v1.model_dispatch_step(p_args text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD' SET extra_float_digits TO 3
AS $function$
DECLARE
    args jsonb := p_args::jsonb;
    binding jsonb;
    current_binding jsonb;
    v_scope aap_inference_routing_v1.scopes%ROWTYPE;
    v_attempt aap_inference_routing_v1.attempts%ROWTYPE;
    v_state aap_inference_routing_v1.registry_state%ROWTYPE;
    v_registry public.ml_backend_registry%ROWTYPE;
    v_scope_id uuid;
    v_attempt_id uuid;
    v_member_id uuid;
    v_action text;
    v_transport jsonb;
    v_age bigint;
BEGIN
    IF NOT aap_inference_routing_v1._routing_exact_keys(
        args, ARRAY['scope_id','capability','attempt_id','action','binding']
    ) OR pg_catalog.jsonb_typeof(args->'capability') IS DISTINCT FROM 'string'
      OR pg_catalog.jsonb_typeof(args->'action') IS DISTINCT FROM 'string'
      OR args->>'capability' !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid model dispatch';
    END IF;
    v_scope_id := aap_inference_routing_v1._routing_uuid(args->>'scope_id');
    v_attempt_id := aap_inference_routing_v1._routing_uuid(args->>'attempt_id');
    v_action := args->>'action';
    IF v_action NOT IN ('prepare','start','not_sent') THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid model dispatch';
    END IF;
    SELECT s.* INTO v_scope FROM aap_inference_routing_v1.scopes AS s
     WHERE s.id=v_scope_id FOR UPDATE;
    IF NOT FOUND OR aap_inference_routing_v1._routing_scope_capability(v_scope_id) IS DISTINCT FROM args->>'capability' THEN
        RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
    END IF;
    SELECT a.* INTO v_attempt FROM aap_inference_routing_v1.attempts AS a
     WHERE a.id=v_attempt_id AND a.scope_id=v_scope_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
    END IF;
    IF v_action='not_sent' THEN
        IF args->'binding' IS DISTINCT FROM 'null'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid model dispatch';
        END IF;
        IF v_attempt.state='not_sent' THEN
            RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object('state','not_sent'));
        END IF;
        IF v_attempt.state IS DISTINCT FROM 'active' OR v_attempt.dispatch_state IS DISTINCT FROM 'prepared' THEN
            RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
        END IF;
        UPDATE aap_inference_routing_v1.attempts SET state='not_sent',reason='not_sent',status_code=NULL,
            updated_at=pg_catalog.now() WHERE id=v_attempt_id;
        RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object('state','not_sent'));
    END IF;
    binding := args->'binding';
    IF NOT aap_inference_routing_v1._routing_exact_keys(
        binding, ARRAY['member_id','boot_id','revalidate','health_max_age_seconds',
                       'model_id','model_variants','check_approval','approval']
    ) OR pg_catalog.jsonb_typeof(binding->'revalidate') IS DISTINCT FROM 'boolean'
      OR pg_catalog.jsonb_typeof(binding->'check_approval') IS DISTINCT FROM 'boolean'
      OR (binding->'boot_id' <> 'null'::jsonb AND pg_catalog.jsonb_typeof(binding->'boot_id') IS DISTINCT FROM 'string')
      OR (binding->'model_id' <> 'null'::jsonb AND pg_catalog.jsonb_typeof(binding->'model_id') IS DISTINCT FROM 'string')
      OR (binding->'model_variants' <> 'null'::jsonb AND pg_catalog.jsonb_typeof(binding->'model_variants') IS DISTINCT FROM 'object')
      OR (binding->'approval' <> 'null'::jsonb AND pg_catalog.jsonb_typeof(binding->'approval') IS DISTINCT FROM 'object') THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid model dispatch binding';
    END IF;
    IF binding->'member_id' <> 'null'::jsonb THEN
        v_member_id := aap_inference_routing_v1._routing_uuid(binding->>'member_id');
    END IF;
    v_age := aap_inference_routing_v1._routing_integer(binding->'health_max_age_seconds',1,3600);
    IF v_scope.closed OR v_scope.state IS DISTINCT FROM 'active' OR v_attempt.state IS DISTINCT FROM 'active' THEN
        RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
    END IF;
    IF v_action='prepare' THEN
        IF v_attempt.dispatch_state IS DISTINCT FROM 'unmanaged'
           OR v_attempt.dispatch_xid IS DISTINCT FROM pg_catalog.pg_current_xact_id() THEN
            RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
        END IF;
        UPDATE aap_inference_routing_v1.attempts
           SET dispatch_state='prepared',
               dispatch_binding_sha256=public.digest(pg_catalog.convert_to(binding::text,'UTF8'),'sha256'),
               updated_at=pg_catalog.now() WHERE id=v_attempt_id;
        RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object('state','prepared'));
    END IF;
    IF v_attempt.dispatch_state IS DISTINCT FROM 'prepared' THEN
        RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
    END IF;
    SELECT r.* INTO v_registry FROM public.ml_backend_registry AS r WHERE r.id=v_scope.registry_id FOR SHARE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
    END IF;
    IF (binding->>'revalidate')::boolean IS DISTINCT FROM
       (v_scope.pool_id IS NOT NULL AND v_registry.gpu_resource_id IS NOT NULL)
       OR ((binding->>'revalidate')::boolean AND v_member_id IS NULL) THEN
        RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
    END IF;
    SELECT s.* INTO v_state FROM aap_inference_routing_v1.registry_state AS s WHERE s.registry_id=v_scope.registry_id;
    v_transport := aap_inference_routing_v1._routing_transport_identity(v_scope.registry_id);
    IF v_transport IS NULL OR pg_catalog.encode(public.digest(pg_catalog.convert_to(v_transport::text,'UTF8'),'sha256'),'hex')
          IS DISTINCT FROM v_attempt.transport_identity_sha256
       OR v_state.registry_id IS NULL OR v_state.realm IS DISTINCT FROM v_scope.realm
       OR v_state.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm()
       OR v_state.config_revision IS DISTINCT FROM v_scope.config_revision
       OR v_state.authority_epoch IS DISTINCT FROM v_scope.authority_epoch THEN
        RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
    END IF;
    IF v_scope.pool_id IS NOT NULL THEN
        PERFORM aap_inference_routing_v1._routing_lock_admission_pool(v_scope.pool_id);
        PERFORM p.id FROM public.ml_backend_service_pools AS p
         WHERE p.id=v_scope.pool_id AND p.enabled AND p.routing_generation=v_scope.generation;
        IF NOT FOUND THEN RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch'); END IF;
        PERFORM m.id FROM public.ml_backend_pool_members AS m
         WHERE m.pool_id=v_scope.pool_id AND m.registry_id=v_scope.registry_id
           AND (v_member_id IS NULL OR m.id=v_member_id) AND m.traffic_state='active'
         FOR SHARE NOWAIT;
        IF NOT FOUND THEN RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch'); END IF;
        IF v_scope.project_id IS NOT NULL THEN
            PERFORM a.id FROM public.project_ml_backend_pool AS a
             WHERE a.project_id=v_scope.project_id AND a.pool_id=v_scope.pool_id AND a.enabled;
            IF NOT FOUND THEN RETURN aap_inference_routing_v1._routing_rejected('pool_not_enabled'); END IF;
        END IF;
    ELSIF v_member_id IS NOT NULL THEN
        RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
    END IF;
    IF aap_inference_routing_v1._routing_has_control(v_scope.registry_id) THEN
        RETURN aap_inference_routing_v1._routing_rejected('management_busy');
    END IF;
    IF v_scope.operation='tracker' AND NOT aap_jobs_v1._video_execution_scope_is_current(v_scope_id,false) THEN
        RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
    END IF;
    IF (binding->>'revalidate')::boolean THEN
        IF (binding->>'boot_id' IS NULL AND v_scope.reservation_eligibility_revision IS DISTINCT FROM v_scope.eligibility_revision)
           OR (binding->>'boot_id' IS NOT NULL AND (
               v_scope.reservation_boot_id IS DISTINCT FROM binding->>'boot_id'
               OR v_registry.health_meta #>> '{residency,boot_id}' IS DISTINCT FROM binding->>'boot_id')) THEN
            RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
        END IF;
        IF NOT EXISTS (SELECT 1 FROM aap_inference_routing_v1.attempts AS a
                       WHERE a.scope_id=v_scope_id AND a.id<>v_attempt_id)
           AND (v_state.eligibility_revision IS DISTINCT FROM v_scope.eligibility_revision
             OR (v_scope.reservation_eligibility_revision IS DISTINCT FROM v_scope.eligibility_revision
                 AND (v_registry.state IS DISTINCT FROM 'connected' OR v_registry.last_checked_at IS NULL
                      OR v_registry.last_checked_at < pg_catalog.clock_timestamp()-pg_catalog.make_interval(secs=>v_age::integer)))) THEN
            RETURN aap_inference_routing_v1._routing_rejected('generation_mismatch');
        END IF;
    END IF;
    current_binding := binding;
    IF (binding->>'check_approval')::boolean THEN
        current_binding := pg_catalog.jsonb_set(binding, '{approval}',
            COALESCE(aap_inference_topology_v1.plugin_asset_approval(v_scope.registry_id), 'null'::jsonb));
    END IF;
    IF public.digest(pg_catalog.convert_to(current_binding::text,'UTF8'),'sha256')
       IS DISTINCT FROM v_attempt.dispatch_binding_sha256 THEN
        RETURN aap_inference_routing_v1._routing_rejected('transport_changed');
    END IF;
    UPDATE aap_inference_routing_v1.attempts SET dispatch_state='started',updated_at=pg_catalog.now()
     WHERE id=v_attempt_id;
    RETURN aap_inference_routing_v1._routing_reply('ok', pg_catalog.jsonb_build_object('state','started'));
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1.model_dispatch_step(text) FROM PUBLIC;
-- aap-statement-break
DO $owner$
DECLARE owner_name text;
BEGIN
    SELECT r.rolname INTO owner_name FROM pg_catalog.pg_namespace n
    JOIN pg_catalog.pg_roles r ON r.oid=n.nspowner WHERE n.nspname='aap_inference_routing_v1';
    EXECUTE pg_catalog.format('ALTER FUNCTION aap_inference_routing_v1.capture_scope_boot() OWNER TO %I', owner_name);
    EXECUTE pg_catalog.format('ALTER FUNCTION aap_inference_routing_v1.supersede_unbegun_scope(text) OWNER TO %I', owner_name);
    EXECUTE pg_catalog.format('ALTER FUNCTION aap_inference_routing_v1.guard_model_attempt() OWNER TO %I', owner_name);
    EXECUTE pg_catalog.format('ALTER FUNCTION aap_inference_routing_v1.model_dispatch_step(text) OWNER TO %I', owner_name);
    EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA aap_inference_topology_v1 TO %I', owner_name);
    EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION aap_inference_topology_v1.plugin_asset_approval(uuid) TO %I', owner_name);
END;
$owner$;
