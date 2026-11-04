-- Private immutable binding prevents a control from being reused for different replacement facts.
CREATE TABLE aap_inference_routing_v1.plugin_replacement_bindings (
    control_id uuid PRIMARY KEY REFERENCES aap_inference_routing_v1.controls(id) ON DELETE RESTRICT,
    registry_id uuid NOT NULL,
    revision integer NOT NULL,
    endpoint text NOT NULL,
    container_id text NOT NULL,
    boot_id text NOT NULL
);
-- aap-statement-break
REVOKE ALL ON TABLE aap_inference_routing_v1.plugin_replacement_bindings FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.authorize_plugin_routing_replacement(
    p_id uuid, p_revision integer, p_control_id uuid, p_endpoint text,
    p_container_id text, p_boot_id text
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    r public.ml_backend_registry%ROWTYPE;
    t aap_inference_gpu_v1.installation_terminal_intents%ROWTYPE;
    m public.gpu_backend_memberships%ROWTYPE;
    f public.gpu_backend_fences%ROWTYPE;
    s aap_inference_routing_v1.registry_state%ROWTYPE;
    c aap_inference_routing_v1.controls%ROWTYPE;
    b aap_inference_routing_v1.plugin_replacement_bindings%ROWTYPE;
    digest bytea;
    expected_id uuid;
    result jsonb;
BEGIN
    IF p_id IS NULL OR p_revision IS NULL OR p_revision < 1 OR p_control_id IS NULL
       OR p_endpoint IS NULL OR pg_catalog.char_length(p_endpoint) NOT BETWEEN 1 AND 2048
       OR p_container_id IS NULL OR p_container_id !~ '^[0-9a-f]{64}$'
       OR p_boot_id IS NULL OR pg_catalog.char_length(p_boot_id) NOT BETWEEN 1 AND 128
       OR p_boot_id <> pg_catalog.btrim(p_boot_id) THEN
        RETURN pg_catalog.jsonb_build_object('status','rejected','reason','invalid_identity');
    END IF;
    -- UUIDv5, namespace=installation UUID, name exactly the host operation ID.
    digest := public.digest(pg_catalog.uuid_send(p_id) || pg_catalog.convert_to(
        'plugin:bind-routing:' || p_container_id || ':' || p_boot_id, 'UTF8'), 'sha1');
    digest := pg_catalog.set_byte(digest, 6, (pg_catalog.get_byte(digest, 6) & 15) | 80);
    digest := pg_catalog.set_byte(digest, 8, (pg_catalog.get_byte(digest, 8) & 63) | 128);
    expected_id := pg_catalog.encode(pg_catalog.substring(digest, 1, 16), 'hex')::uuid;
    IF p_control_id IS DISTINCT FROM expected_id THEN
        RETURN pg_catalog.jsonb_build_object('status','rejected','reason','control_identity');
    END IF;

    SELECT * INTO r FROM public.ml_backend_registry WHERE id=p_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object('status','rejected','reason','installation_changed');
    END IF;
    SELECT * INTO t FROM aap_inference_gpu_v1.installation_terminal_intents
      WHERE backend_registry_id=p_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object('status','not_replacement');
    END IF;
    SELECT * INTO m FROM public.gpu_backend_memberships
      WHERE backend_registry_id=p_id AND gpu_resource_id=r.gpu_resource_id FOR UPDATE;
    SELECT * INTO f FROM public.gpu_backend_fences WHERE backend_registry_id=p_id FOR UPDATE;
    SELECT * INTO s FROM aap_inference_routing_v1.registry_state WHERE registry_id=p_id FOR UPDATE;
    IF r.url IS DISTINCT FROM p_endpoint
       OR r.extra_params #>> '{plugin,installation_id}' IS DISTINCT FROM p_id::text
       OR r.extra_params #> '{plugin,revision}' IS DISTINCT FROM pg_catalog.to_jsonb(p_revision)
       OR r.extra_params #>> '{plugin,status}' NOT IN ('prepared','disabled')
       OR r.extra_params #>> '{plugin,status}' IS NULL
       OR r.extra_params #>> '{plugin,manifest,resource,device}' IS DISTINCT FROM 'cuda'
       OR r.gpu_resource_id IS NULL
       OR r.extra_params #>> '{plugin,resource,gpu_resource_id}' IS DISTINCT FROM r.gpu_resource_id
       OR r.extra_params #>> '{plugin_lifecycle,launch,installation_id}' IS DISTINCT FROM p_id::text
       OR r.extra_params #>> '{plugin_lifecycle,launch,registry_id}' IS DISTINCT FROM p_id::text
       OR r.extra_params #>> '{plugin_lifecycle,launch,artifact}' IS DISTINCT FROM r.extra_params #>> '{plugin,manifest,artifact}'
       OR r.extra_params #> '{plugin_lifecycle,launch,resource}' IS DISTINCT FROM r.extra_params #> '{plugin,resource}'
       OR r.extra_params #>> '{plugin_lifecycle,launch,container_id}' IS DISTINCT FROM p_container_id
       OR COALESCE(r.extra_params #>> '{plugin_lifecycle,launch,image_id}','') !~ '^sha256:[0-9a-f]{64}$'
       OR (r.extra_params #> '{plugin_lifecycle,launch}') - ARRAY['installation_id','registry_id','artifact','image_id','container_id','resource'] IS DISTINCT FROM '{}'::jsonb
       OR pg_catalog.jsonb_typeof(r.extra_params #> '{plugin_lifecycle,qualification}') IS DISTINCT FROM 'object'
       OR (r.extra_params #> '{plugin_lifecycle,qualification}') - ARRAY['report','sha256'] IS DISTINCT FROM '{}'::jsonb
       OR pg_catalog.jsonb_typeof(r.extra_params #> '{plugin_lifecycle,qualification,report}') IS DISTINCT FROM 'object'
       OR COALESCE(r.extra_params #>> '{plugin_lifecycle,qualification,sha256}','') !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(r.extra_params #> '{plugin_lifecycle,stopped}') IS DISTINCT FROM 'array'
       OR EXISTS (SELECT 1 FROM pg_catalog.jsonb_array_elements(CASE WHEN pg_catalog.jsonb_typeof(r.extra_params #> '{plugin_lifecycle,stopped}')='array' THEN r.extra_params #> '{plugin_lifecycle,stopped}' ELSE '[]'::jsonb END) x
                  WHERE x->>'container_id'=p_container_id OR x->>'boot_id'=p_boot_id)
       OR m.backend_registry_id IS NULL OR m.state IS DISTINCT FROM 'active'
       OR f.backend_registry_id IS NULL OR s.registry_id IS NULL
       OR NOT t.settled OR t.membership_epoch IS DISTINCT FROM m.membership_epoch
       OR t.generation > f.generation_high_water OR t.control_epoch > f.control_epoch_high_water
       OR t.runtime_epoch > f.runtime_epoch_high_water
       OR t.revision IS NULL OR pg_catalog.jsonb_typeof(t.revision) IS DISTINCT FROM 'number'
       OR (t.revision #>> '{}')::numeric >= p_revision
       OR t.request->>'registry_id' IS DISTINCT FROM p_id::text
       OR t.request->>'resource_id' IS DISTINCT FROM r.gpu_resource_id
       OR t.request->>'endpoint' IS DISTINCT FROM p_endpoint
       OR COALESCE(t.request->>'container_id','') !~ '^[0-9a-f]{64}$'
       OR COALESCE(t.request->>'boot_id','') = ''
       OR t.request->>'container_id'=p_container_id OR t.request->>'boot_id'=p_boot_id
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.jsonb_array_elements(CASE WHEN pg_catalog.jsonb_typeof(r.extra_params #> '{plugin_lifecycle,stopped}')='array' THEN r.extra_params #> '{plugin_lifecycle,stopped}' ELSE '[]'::jsonb END) x
                      WHERE x->>'container_id'=t.request->>'container_id' AND x->>'boot_id'=t.request->>'boot_id') THEN
        RETURN pg_catalog.jsonb_build_object('status','rejected','reason','replacement_unproven');
    END IF;

    SELECT * INTO c FROM aap_inference_routing_v1.controls WHERE id=p_control_id FOR UPDATE;
    SELECT * INTO b FROM aap_inference_routing_v1.plugin_replacement_bindings WHERE control_id=p_control_id FOR UPDATE;
    IF b.control_id IS NOT NULL AND (b.registry_id IS DISTINCT FROM p_id OR b.revision IS DISTINCT FROM p_revision
       OR b.endpoint IS DISTINCT FROM p_endpoint OR b.container_id IS DISTINCT FROM p_container_id
       OR b.boot_id IS DISTINCT FROM p_boot_id) THEN
        RETURN pg_catalog.jsonb_build_object('status','rejected','reason','binding_changed');
    END IF;
    -- A completed bind can be replayed after GPU activation fails without
    -- changing the epoch or admitting a different replacement.
    IF c.id IS NOT NULL AND c.phase = 'completed' THEN
        IF b.control_id IS NULL OR c.registry_id IS DISTINCT FROM p_id
           OR c.kind IS DISTINCT FROM 'epoch' OR c.recovered_kind IS NOT NULL
           OR c.config_revision IS DISTINCT FROM s.config_revision
           OR c.realm IS DISTINCT FROM s.realm
           OR s.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm()
           OR s.continuity IS DISTINCT FROM 'ready'
           OR c.new_epoch IS DISTINCT FROM s.authority_epoch
           OR c.backend_incarnation IS NULL
           OR c.backend_incarnation IS DISTINCT FROM s.backend_incarnation THEN
            RETURN pg_catalog.jsonb_build_object('status','rejected','reason','control_changed');
        END IF;
        RETURN pg_catalog.jsonb_build_object('status','authorized');
    END IF;
    IF c.id IS NOT NULL AND (c.registry_id IS DISTINCT FROM p_id OR c.kind IS DISTINCT FROM 'epoch'
       OR c.phase IS DISTINCT FROM 'prepared' OR c.recovered_kind IS NOT NULL
       OR c.config_revision IS DISTINCT FROM s.config_revision OR c.realm IS DISTINCT FROM s.realm
       OR c.new_epoch IS NULL
       OR c.retired_epoch IS DISTINCT FROM CASE WHEN s.continuity='ready' THEN s.authority_epoch ELSE NULL END
       OR (b.control_id IS NULL AND c.backend_incarnation IS DISTINCT FROM s.backend_incarnation)
       OR (b.control_id IS NOT NULL AND c.backend_incarnation IS NOT NULL)) THEN
        RETURN pg_catalog.jsonb_build_object('status','rejected','reason','control_changed');
    END IF;
    IF EXISTS (SELECT 1 FROM aap_inference_routing_v1.controls WHERE registry_id=p_id AND id<>p_control_id AND phase IN ('prepared','draining','uncertain'))
       OR EXISTS (SELECT 1 FROM aap_inference_routing_v1.scopes WHERE registry_id=p_id AND state IN ('reserved','active','uncertain'))
       OR EXISTS (SELECT 1 FROM aap_inference_routing_v1.attempts a JOIN aap_inference_routing_v1.scopes q ON q.id=a.scope_id WHERE q.registry_id=p_id AND a.state IN ('active','uncertain'))
       OR EXISTS (SELECT 1 FROM aap_inference_routing_v1.control_attempts a JOIN aap_inference_routing_v1.controls q ON q.id=a.control_id WHERE q.registry_id=p_id AND a.state IN ('active','uncertain'))
       OR (b.control_id IS NULL AND EXISTS (SELECT 1 FROM aap_inference_routing_v1.control_attempts WHERE control_id=p_control_id)) THEN
        RETURN pg_catalog.jsonb_build_object('status','rejected','reason','routing_not_quiescent');
    END IF;
    IF c.id IS NULL THEN
        result := aap_inference_routing_v1.routing_step('prepare_epoch',
            pg_catalog.jsonb_build_object('control_id',p_control_id,'registry_id',p_id)::text);
        IF result->>'status' IS DISTINCT FROM 'ok' THEN
            RETURN pg_catalog.jsonb_build_object('status','rejected','reason',COALESCE(result->>'reason','prepare_failed'));
        END IF;
        SELECT * INTO c FROM aap_inference_routing_v1.controls WHERE id=p_control_id FOR UPDATE;
    END IF;
    IF b.control_id IS NULL THEN
        INSERT INTO aap_inference_routing_v1.plugin_replacement_bindings
          (control_id,registry_id,revision,endpoint,container_id,boot_id)
        VALUES (p_control_id,p_id,p_revision,p_endpoint,p_container_id,p_boot_id);
        UPDATE aap_inference_routing_v1.controls SET backend_incarnation=NULL WHERE id=p_control_id;
    END IF;
    RETURN pg_catalog.jsonb_build_object('status','authorized');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.authorize_plugin_routing_replacement(uuid,integer,uuid,text,text,text) FROM PUBLIC;
-- aap-statement-break
DO $grant$
DECLARE owner_name name;
BEGIN
    SELECT pg_catalog.pg_get_userbyid(proowner) INTO owner_name FROM pg_catalog.pg_proc
    WHERE oid='aap_inference_topology_v1.command_plugin_installation(uuid,integer,text,text)'::regprocedure;
    EXECUTE pg_catalog.format('ALTER TABLE aap_inference_routing_v1.plugin_replacement_bindings OWNER TO %I',owner_name);
    EXECUTE pg_catalog.format('ALTER FUNCTION aap_inference_topology_v1.authorize_plugin_routing_replacement(uuid,integer,uuid,text,text,text) OWNER TO %I',owner_name);
    EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA aap_inference_routing_v1, aap_inference_gpu_v1 TO %I',owner_name);
    EXECUTE pg_catalog.format('GRANT SELECT ON aap_inference_routing_v1.registry_state, aap_inference_routing_v1.controls, aap_inference_routing_v1.scopes, aap_inference_routing_v1.attempts, aap_inference_routing_v1.control_attempts, aap_inference_gpu_v1.installation_terminal_intents, public.gpu_backend_memberships, public.gpu_backend_fences TO %I',owner_name);
    EXECUTE pg_catalog.format('GRANT UPDATE (backend_incarnation) ON aap_inference_routing_v1.controls TO %I',owner_name);
    EXECUTE pg_catalog.format('GRANT UPDATE (updated_at) ON aap_inference_routing_v1.registry_state, public.gpu_backend_memberships, public.gpu_backend_fences TO %I',owner_name);
    EXECUTE pg_catalog.format('GRANT UPDATE (settled) ON aap_inference_gpu_v1.installation_terminal_intents TO %I',owner_name);
    EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION aap_inference_routing_v1.routing_step(text,text) TO %I',owner_name);
END
$grant$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_topology_v1.command_plugin_installation(
    p_id uuid, p_revision integer, p_action text, p_evidence text
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    r public.ml_backend_registry%ROWTYPE;
    i jsonb; lifecycle jsonb; evidence jsonb; active jsonb; stopped jsonb;
    status text;
BEGIN
    SELECT * INTO r FROM public.ml_backend_registry WHERE id=p_id FOR UPDATE;
    IF NOT FOUND OR NOT (r.extra_params ? 'plugin') THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='plugin installation not found';
    END IF;
    i := r.extra_params->'plugin';
    lifecycle := r.extra_params->'plugin_lifecycle';
    IF jsonb_typeof(lifecycle) IS DISTINCT FROM 'object'
       OR (i->>'revision')::integer IS DISTINCT FROM p_revision THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='stale plugin installation revision';
    END IF;
    status := i->>'status';
    evidence := CASE WHEN p_evidence IS NULL THEN NULL ELSE p_evidence::jsonb END;
    active := lifecycle->'activation';
    IF p_action='record_launch' THEN
        IF status NOT IN ('prepared','disabled')
           OR jsonb_typeof(evidence) IS DISTINCT FROM 'object'
           OR evidence - ARRAY['installation_id','registry_id','artifact','image_id','container_id','resource'] <> '{}'::jsonb
           OR evidence->>'installation_id' IS DISTINCT FROM p_id::text
           OR evidence->>'registry_id' IS DISTINCT FROM p_id::text
           OR evidence->>'artifact' IS DISTINCT FROM i->'manifest'->>'artifact'
           OR evidence->'resource' IS DISTINCT FROM i->'resource'
           OR coalesce(evidence->>'container_id','') !~ '^[0-9a-f]{64}$'
           OR coalesce(evidence->>'image_id','') !~ '^sha256:[0-9a-f]{64}$' THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid plugin launch receipt or state';
        END IF;
        FOR stopped IN SELECT value FROM jsonb_array_elements(coalesce(lifecycle->'stopped','[]'::jsonb)) LOOP
            IF stopped->>'container_id'=evidence->>'container_id' THEN
                RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='stopped launch cannot be reactivated';
            END IF;
        END LOOP;
        IF lifecycle ? 'launch' AND lifecycle->'launch' IS DISTINCT FROM evidence
           AND NOT (lifecycle ? 'proof') THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='previous launch requires quiescence proof';
        END IF;
        IF lifecycle->'launch' = evidence THEN
            RETURN jsonb_build_object('registry_id',p_id,'installation',i);
        END IF;
        IF lifecycle->'launch' IS DISTINCT FROM evidence THEN
            lifecycle := (lifecycle - 'proof' - 'activation' - 'qualification') || jsonb_build_object('launch',evidence);
        END IF;
    ELSIF p_action='approve_qualification' THEN
        IF status NOT IN ('prepared','disabled')
           OR jsonb_typeof(lifecycle->'launch') IS DISTINCT FROM 'object'
           OR jsonb_typeof(evidence) IS DISTINCT FROM 'object'
           OR evidence - ARRAY['report','sha256'] <> '{}'::jsonb
           OR jsonb_typeof(evidence->'report') IS DISTINCT FROM 'object'
           OR coalesce(evidence->>'sha256','') !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid plugin qualification evidence or state';
        END IF;
        IF lifecycle->'qualification' = evidence THEN
            RETURN jsonb_build_object('registry_id',p_id,'installation',i);
        END IF;
        lifecycle := lifecycle || jsonb_build_object('qualification',evidence);
    ELSIF p_action='activate' THEN
        IF status NOT IN ('prepared','disabled') OR jsonb_typeof(evidence) IS DISTINCT FROM 'object'
           OR (i->'manifest'->'resource'->>'device'='cuda'
               AND jsonb_typeof(lifecycle->'qualification') IS DISTINCT FROM 'object')
           OR evidence - ARRAY['endpoint','boot_id','control_epoch'] IS DISTINCT FROM lifecycle->'launch'
           OR evidence->>'endpoint' IS DISTINCT FROM r.url
           OR coalesce(evidence->>'boot_id','') = ''
           OR (i->'manifest'->'resource'->>'device'='cpu'
               AND evidence->'control_epoch' IS DISTINCT FROM 'null'::jsonb)
           OR (i->'manifest'->'resource'->>'device'='cuda'
               AND evidence->'control_epoch' IS DISTINCT FROM 'null'::jsonb
               AND coalesce(evidence->>'control_epoch','') !~ '^[1-9][0-9]*$') THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid plugin activation evidence or state';
        END IF;
        FOR stopped IN SELECT value FROM jsonb_array_elements(coalesce(lifecycle->'stopped','[]'::jsonb)) LOOP
            IF stopped->>'container_id'=evidence->>'container_id'
               OR stopped->>'boot_id'=evidence->>'boot_id' THEN
                RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='stopped launch cannot be reactivated';
            END IF;
        END LOOP;
        lifecycle := (lifecycle - 'proof') || jsonb_build_object('activation',evidence);
        status := 'active';
    ELSIF p_action='disable' THEN
        IF status <> 'active' THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='plugin must be active to disable';
        END IF;
        lifecycle := lifecycle - 'proof';
        status := 'disabled';
    ELSIF p_action='quiesce' THEN
        IF status NOT IN ('prepared','disabled')
           OR jsonb_typeof(evidence) IS DISTINCT FROM 'object'
           OR evidence - ARRAY['endpoint','boot_id','control_epoch','generation','pool_ids','operation_id']
               IS DISTINCT FROM lifecycle->'launch'
           OR evidence->>'endpoint' IS DISTINCT FROM r.url
           OR coalesce(evidence->>'boot_id','') = ''
           OR (i->'manifest'->'resource'->>'device'='cpu' AND (
               evidence->'control_epoch' IS DISTINCT FROM 'null'::jsonb
               OR evidence->'generation' IS DISTINCT FROM 'null'::jsonb
               OR coalesce(evidence->>'operation_id','') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'))
           OR (i->'manifest'->'resource'->>'device'='cuda' AND (
               coalesce(evidence->>'control_epoch','') !~ '^[1-9][0-9]*$'
               OR coalesce(evidence->>'generation','') !~ '^[1-9][0-9]*$'))
           OR (active IS NOT NULL AND active <> 'null'::jsonb AND (
               evidence->>'boot_id' IS DISTINCT FROM active->>'boot_id'
               OR (active->>'control_epoch' IS NOT NULL AND
                   (evidence->>'control_epoch')::numeric < (active->>'control_epoch')::numeric)))
           OR jsonb_typeof(evidence->'pool_ids') IS DISTINCT FROM 'array'
           OR jsonb_array_length(evidence->'pool_ids')=0 THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid or stale plugin quiescence proof';
        END IF;
        lifecycle := lifecycle || jsonb_build_object('proof',evidence || jsonb_build_object('revision',p_revision+1),
            'stopped',coalesce(lifecycle->'stopped','[]'::jsonb) || jsonb_build_array(
                jsonb_build_object('container_id',evidence->'container_id','boot_id',evidence->'boot_id')));
        IF i->'manifest'->'resource'->>'device'='cuda'
           AND NOT aap_inference_gpu_v1.gpu_plugin_terminal_proof_valid(p_id, r.extra_params ||
               jsonb_build_object('plugin',i || jsonb_build_object('revision',p_revision+1),
                                  'plugin_lifecycle',lifecycle)) THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='settled GPU terminal proof required for quiescence';
        END IF;
        status := 'disabled';
    ELSIF p_action='cancel_prepared' THEN
        IF status <> 'prepared' OR lifecycle ?| ARRAY['launch','activation','proof','qualification']
           OR lifecycle->'stopped' IS DISTINCT FROM '[]'::jsonb
           OR (i->'manifest'->'resource'->>'device'='cuda'
               AND NOT aap_inference_gpu_v1.gpu_plugin_prepared_cancellation_valid(p_id, r.extra_params)) THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='never-launched preparation required for cancellation';
        END IF;
        lifecycle := lifecycle || jsonb_build_object('cancelled_prepared',true);
        status := 'retired';
    ELSIF p_action='retire' THEN
        IF status = 'retired' THEN
            RETURN jsonb_build_object('registry_id',p_id,'installation',i);
        END IF;
        IF status <> 'disabled' OR jsonb_typeof(lifecycle->'proof') IS DISTINCT FROM 'object'
           OR (lifecycle->'proof'->>'revision')::integer IS DISTINCT FROM p_revision THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='valid quiescence proof required for retirement';
        END IF;
        IF i->'manifest'->'resource'->>'device'='cuda'
           AND NOT aap_inference_gpu_v1.gpu_plugin_terminal_proof_valid(p_id, r.extra_params) THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='settled GPU terminal proof required for retirement';
        END IF;
        status := 'retired';
    ELSE
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='unknown plugin installation command';
    END IF;
    i := i || jsonb_build_object('status',status,'revision',p_revision+1);
    IF p_action IN ('disable','quiesce','retire') AND i #>> '{manifest,resource,device}'='cuda' THEN
        INSERT INTO aap_inference_routing_v1.plugin_terminal_update_guard
          (pid,xid,registry_id,old_extra,new_extra,old_auth,new_auth,old_gpu,new_gpu,old_budget,new_budget)
        VALUES (pg_catalog.pg_backend_pid(),pg_catalog.pg_current_xact_id(),p_id,r.extra_params,
          r.extra_params || jsonb_build_object('plugin',i,'plugin_lifecycle',lifecycle),r.auth_token,
          CASE WHEN status='retired' AND r.auth_token IS NOT NULL THEN 'retired-sha256:' || encode(sha256(convert_to(r.auth_token,'UTF8')),'hex') ELSE r.auth_token END,
          r.gpu_resource_id,CASE WHEN status='retired' THEN NULL ELSE r.gpu_resource_id END,
          r.vram_budget_mb,CASE WHEN status='retired' THEN NULL ELSE r.vram_budget_mb END);
    END IF;
    UPDATE public.ml_backend_registry SET extra_params=extra_params ||
        jsonb_build_object('plugin',i,'plugin_lifecycle',lifecycle),
        auth_token=CASE WHEN status='retired' AND auth_token IS NOT NULL
            THEN 'retired-sha256:' || encode(sha256(convert_to(auth_token,'UTF8')),'hex')
            ELSE auth_token END,
        gpu_resource_id=CASE WHEN status='retired' THEN NULL ELSE gpu_resource_id END,
        vram_budget_mb=CASE WHEN status='retired' THEN NULL ELSE vram_budget_mb END,
        updated_at=clock_timestamp()
    WHERE id=p_id;
    DELETE FROM aap_inference_routing_v1.plugin_terminal_update_guard
      WHERE pid=pg_catalog.pg_backend_pid() AND xid=pg_catalog.pg_current_xact_id() AND registry_id=p_id;
    RETURN jsonb_build_object('registry_id',p_id,'installation',i);
END;
$function$;
