CREATE OR REPLACE FUNCTION yoao_inference_topology_v1.prepare_plugin_installation(p_intent text)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v jsonb := p_intent::jsonb;
    r public.ml_backend_registry%ROWTYPE;
    i jsonb;
    k uuid;
    exec_mode text;
    tier text;
    vram_budget integer;
BEGIN
    IF jsonb_typeof(v) IS DISTINCT FROM 'object'
       OR v - ARRAY['installation_id','name','url','auth_token','manifest','resource','execution_mode','source_tier'] <> '{}'::jsonb
       OR jsonb_typeof(v->'manifest') IS DISTINCT FROM 'object'
       OR jsonb_typeof(v->'resource') IS DISTINCT FROM 'object'
       OR coalesce(length(v->>'auth_token'),0) < 32
       OR coalesce(v->>'name','') = '' OR coalesce(v->>'url','') = ''
       OR v->>'url' IS DISTINCT FROM rtrim(v->>'url','/') THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid plugin installation intent';
    END IF;
    exec_mode := coalesce(v->>'execution_mode', v->'manifest'->>'execution_mode', 'container');
    IF exec_mode NOT IN ('container','direct')
       OR coalesce(v->'manifest'->>'execution_mode','container') IS DISTINCT FROM exec_mode THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid plugin execution_mode';
    END IF;
    tier := coalesce(v->>'source_tier','third_party');
    IF tier NOT IN ('official','third_party') THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid plugin source_tier';
    END IF;
    IF exec_mode = 'direct' AND (
        v->'resource'->>'gpu_resource_id' IS NOT NULL
        OR v->'resource'->>'physical_gpu_uuid' IS NOT NULL
        OR v->'manifest'->>'artifact' IS NOT NULL
    ) THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='direct plugin cannot bind local GPU or OCI artifact';
    END IF;
    k := (v->>'installation_id')::uuid;
    IF k IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid plugin installation identity';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(k::text, 388));
    SELECT * INTO r FROM public.ml_backend_registry WHERE id=k FOR UPDATE;
    IF FOUND THEN
        i := r.extra_params->'plugin';
        IF i IS NULL OR i->>'installation_id' IS DISTINCT FROM k::text
           OR i->'manifest' IS DISTINCT FROM v->'manifest'
           OR i->'resource' IS DISTINCT FROM v->'resource'
           OR coalesce(i->>'execution_mode','container') IS DISTINCT FROM exec_mode
           OR coalesce(i->>'source_tier','third_party') IS DISTINCT FROM tier
           OR r.name IS DISTINCT FROM v->>'name'
           OR r.url IS DISTINCT FROM v->>'url'
           OR r.auth_method IS DISTINCT FROM 'token'
           OR r.auth_token IS DISTINCT FROM (CASE WHEN i->>'status'='retired'
                THEN 'retired-sha256:' || encode(sha256(convert_to(v->>'auth_token','UTF8')),'hex')
                ELSE v->>'auth_token' END) THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='plugin installation intent conflicts with existing identity';
        END IF;
        RETURN jsonb_build_object('registry_id',k,'installation',i);
    END IF;
    IF exec_mode = 'container' AND NOT (v ? 'execution_mode') AND NOT (v ? 'source_tier') THEN
        i := jsonb_build_object('installation_id',k,'manifest',v->'manifest',
            'resource',v->'resource','status','prepared','revision',1);
    ELSE
        i := jsonb_build_object('installation_id',k,'manifest',v->'manifest',
            'resource',v->'resource','status','prepared','revision',1,
            'execution_mode',exec_mode,'source_tier',tier);
    END IF;
    vram_budget := CASE
        WHEN v->'resource'->>'gpu_resource_id' IS NULL THEN NULL
        ELSE NULLIF(v->'manifest'->'resource'->>'gpu_memory_mb','0')::integer
    END;
    BEGIN
        INSERT INTO public.ml_backend_registry
            (id,name,url,state,is_interactive,auth_method,auth_token,extra_params,
             gpu_resource_id,vram_budget_mb,eviction_priority,source)
        VALUES (k,v->>'name',v->>'url','disconnected',false,'token',v->>'auth_token',
            jsonb_build_object('plugin',i,'plugin_lifecycle',jsonb_build_object('stopped', '[]'::jsonb)),
            v->'resource'->>'gpu_resource_id',
            vram_budget,0,'manual');
    EXCEPTION WHEN unique_violation THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='plugin installation identity or endpoint already exists';
    END;
    RETURN jsonb_build_object('registry_id',k,'installation',i);
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_inference_topology_v1.command_plugin_installation(
    p_id uuid, p_revision integer, p_action text, p_evidence text
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    r public.ml_backend_registry%ROWTYPE;
    i jsonb; lifecycle jsonb; evidence jsonb; active jsonb; stopped jsonb;
    status text;
    is_direct boolean;
    is_local_gpu boolean;
    direct_activation jsonb;
    terminal_drain_supported boolean;
    stopped_entries jsonb;
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
    is_direct := coalesce(i->>'execution_mode','container') = 'direct';
    is_local_gpu := r.gpu_resource_id IS NOT NULL
        OR (NOT is_direct AND i #>> '{manifest,resource,device}' = 'cuda');
    IF p_action IN ('check_tracker_retirement','disable','quiesce','retire','cancel_prepared') THEN
        IF NOT yoao_inference_routing_v1._tracker_scopes_physically_retired(p_id) THEN
            RAISE EXCEPTION USING ERRCODE='22023',
                MESSAGE='tracker scope physical retirement required';
        END IF;
        IF p_action='check_tracker_retirement' THEN
            IF status NOT IN ('prepared','disabled') OR p_evidence IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid plugin stop check';
            END IF;
            RETURN jsonb_build_object('registry_id',p_id,'installation',i);
        END IF;
    END IF;
    IF p_action='record_launch' THEN
        IF is_direct THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='direct plugin does not accept container launch receipts';
        END IF;
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
        IF is_direct AND NOT is_local_gpu THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='unarbitrated direct plugin does not accept GPU qualification';
        END IF;
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
        IF is_direct THEN
            IF is_local_gpu
               OR status NOT IN ('prepared','disabled')
               OR jsonb_typeof(evidence) IS DISTINCT FROM 'object'
               OR evidence->>'endpoint' IS DISTINCT FROM r.url
               OR coalesce(evidence->>'boot_id','') = ''
               OR pg_catalog.char_length(evidence->>'boot_id') > 128
               OR evidence->>'boot_id' IS DISTINCT FROM pg_catalog.btrim(evidence->>'boot_id')
               OR evidence->'control_epoch' IS DISTINCT FROM 'null'::jsonb
               OR jsonb_typeof(evidence->'terminal_drain_supported') IS DISTINCT FROM 'boolean' THEN
                RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid direct plugin activation evidence or state';
            END IF;
            IF evidence ? 'manifest' OR evidence ? 'resource' THEN
                IF status <> 'prepared'
                   OR lifecycle ? 'activation'
                   OR evidence - ARRAY['endpoint','boot_id','control_epoch','terminal_drain_supported','manifest','resource'] <> '{}'::jsonb
                   OR jsonb_typeof(evidence->'manifest') IS DISTINCT FROM 'object'
                   OR jsonb_typeof(evidence->'resource') IS DISTINCT FROM 'object'
                   OR coalesce(evidence->'manifest'->>'execution_mode','') IS DISTINCT FROM 'direct'
                   OR evidence->'manifest'->>'artifact' IS NOT NULL
                   OR evidence->'resource'->>'gpu_resource_id' IS NOT NULL
                   OR evidence->'resource'->>'physical_gpu_uuid' IS NOT NULL THEN
                    RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid direct plugin manifest finalization';
                END IF;
                i := i || jsonb_build_object('manifest',evidence->'manifest','resource',evidence->'resource');
            ELSIF evidence - ARRAY['endpoint','boot_id','control_epoch','terminal_drain_supported'] <> '{}'::jsonb THEN
                RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid direct plugin activation evidence or state';
            END IF;
            FOR stopped IN SELECT value FROM jsonb_array_elements(coalesce(lifecycle->'stopped','[]'::jsonb)) LOOP
                IF stopped->>'boot_id'=evidence->>'boot_id' THEN
                    RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='stopped launch cannot be reactivated';
                END IF;
            END LOOP;
            direct_activation := jsonb_build_object(
                'endpoint',evidence->'endpoint',
                'boot_id',evidence->'boot_id',
                'control_epoch',evidence->'control_epoch',
                'terminal_drain_supported',evidence->'terminal_drain_supported'
            );
            lifecycle := (lifecycle - 'proof') || jsonb_build_object('activation',direct_activation);
            status := 'active';
        ELSE
            IF status NOT IN ('prepared','disabled') OR jsonb_typeof(evidence) IS DISTINCT FROM 'object'
               OR (is_local_gpu
                   AND jsonb_typeof(lifecycle->'qualification') IS DISTINCT FROM 'object')
               OR evidence - ARRAY['endpoint','boot_id','control_epoch'] IS DISTINCT FROM lifecycle->'launch'
               OR evidence->>'endpoint' IS DISTINCT FROM r.url
               OR coalesce(evidence->>'boot_id','') = ''
               OR (NOT is_local_gpu
                   AND evidence->'control_epoch' IS DISTINCT FROM 'null'::jsonb)
               OR (is_local_gpu
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
        END IF;
    ELSIF p_action='disable' THEN
        IF status <> 'active' THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='plugin must be active to disable';
        END IF;
        lifecycle := lifecycle - 'proof';
        status := 'disabled';
    ELSIF p_action='quiesce' THEN
        IF is_direct THEN
            IF is_local_gpu
               OR status NOT IN ('prepared','disabled')
               OR jsonb_typeof(evidence) IS DISTINCT FROM 'object'
               OR evidence - ARRAY['endpoint','boot_id','control_epoch','generation','pool_ids','operation_id','terminal_drain_supported'] <> '{}'::jsonb
               OR evidence->>'endpoint' IS DISTINCT FROM r.url
               OR coalesce(evidence->>'boot_id','') = ''
               OR pg_catalog.char_length(evidence->>'boot_id') > 128
               OR evidence->>'boot_id' IS DISTINCT FROM pg_catalog.btrim(evidence->>'boot_id')
               OR evidence->'control_epoch' IS DISTINCT FROM 'null'::jsonb
               OR evidence->'generation' IS DISTINCT FROM 'null'::jsonb
               OR coalesce(evidence->>'operation_id','') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR jsonb_typeof(evidence->'terminal_drain_supported') IS DISTINCT FROM 'boolean'
               OR jsonb_typeof(evidence->'pool_ids') IS DISTINCT FROM 'array'
               OR jsonb_array_length(evidence->'pool_ids')=0 THEN
                RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid or stale plugin quiescence proof';
            END IF;
            terminal_drain_supported := (evidence->>'terminal_drain_supported')::boolean;
            IF active IS NOT NULL AND active <> 'null'::jsonb THEN
                IF coalesce((active->>'terminal_drain_supported')::boolean, false)
                   AND NOT terminal_drain_supported THEN
                    RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='direct plugin requires terminal drain proof';
                END IF;
                IF NOT terminal_drain_supported
                   AND evidence->>'boot_id' IS DISTINCT FROM active->>'boot_id' THEN
                    RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid or stale plugin quiescence proof';
                END IF;
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.ml_backend_pool_members
                WHERE registry_id=p_id AND traffic_state='active'
            ) THEN
                RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='pool member must be drained before direct plugin quiescence';
            END IF;
            IF EXISTS (SELECT 1 FROM yoao_inference_routing_v1.controls WHERE registry_id=p_id AND phase IN ('prepared','draining','uncertain'))
               OR EXISTS (SELECT 1 FROM yoao_inference_routing_v1.scopes WHERE registry_id=p_id AND state IN ('reserved','active','uncertain'))
               OR EXISTS (SELECT 1 FROM yoao_inference_routing_v1.attempts a JOIN yoao_inference_routing_v1.scopes q ON q.id=a.scope_id WHERE q.registry_id=p_id AND a.state IN ('active','uncertain'))
               OR EXISTS (SELECT 1 FROM yoao_inference_routing_v1.control_attempts a JOIN yoao_inference_routing_v1.controls q ON q.id=a.control_id WHERE q.registry_id=p_id AND a.state IN ('active','uncertain')) THEN
                RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='routing activity must be quiescent before direct plugin quiescence';
            END IF;
            stopped_entries := CASE
                WHEN terminal_drain_supported THEN
                    CASE
                        WHEN active IS NOT NULL AND active <> 'null'::jsonb
                             AND coalesce(active->>'boot_id','') <> ''
                             AND active->>'boot_id' IS DISTINCT FROM evidence->>'boot_id'
                        THEN jsonb_build_array(
                            jsonb_build_object('container_id',NULL,'boot_id',active->'boot_id'),
                            jsonb_build_object('container_id',NULL,'boot_id',evidence->'boot_id')
                        )
                        ELSE jsonb_build_array(
                            jsonb_build_object('container_id',NULL,'boot_id',evidence->'boot_id')
                        )
                    END
                ELSE '[]'::jsonb
            END;
            lifecycle := lifecycle || jsonb_build_object(
                'proof',evidence || jsonb_build_object('revision',p_revision+1),
                'stopped',coalesce(lifecycle->'stopped','[]'::jsonb) || stopped_entries
            );
            status := 'disabled';
        ELSE
            IF status NOT IN ('prepared','disabled')
               OR jsonb_typeof(evidence) IS DISTINCT FROM 'object'
               OR evidence - ARRAY['endpoint','boot_id','control_epoch','generation','pool_ids','operation_id']
                   IS DISTINCT FROM lifecycle->'launch'
               OR evidence->>'endpoint' IS DISTINCT FROM r.url
               OR coalesce(evidence->>'boot_id','') = ''
               OR (NOT is_local_gpu AND (
                   evidence->'control_epoch' IS DISTINCT FROM 'null'::jsonb
                   OR evidence->'generation' IS DISTINCT FROM 'null'::jsonb
                   OR coalesce(evidence->>'operation_id','') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'))
               OR (is_local_gpu AND (
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
            IF is_local_gpu
               AND NOT yoao_inference_gpu_v1.gpu_plugin_terminal_proof_valid(p_id, r.extra_params ||
                   jsonb_build_object('plugin',i || jsonb_build_object('revision',p_revision+1),
                                      'plugin_lifecycle',lifecycle)) THEN
                RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='settled GPU terminal proof required for quiescence';
            END IF;
            status := 'disabled';
        END IF;
    ELSIF p_action='cancel_prepared' THEN
        IF status <> 'prepared' OR lifecycle ?| ARRAY['launch','activation','proof','qualification']
           OR lifecycle->'stopped' IS DISTINCT FROM '[]'::jsonb
           OR (is_local_gpu
               AND NOT yoao_inference_gpu_v1.gpu_plugin_prepared_cancellation_valid(p_id, r.extra_params)) THEN
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
        IF is_local_gpu
           AND NOT yoao_inference_gpu_v1.gpu_plugin_terminal_proof_valid(p_id, r.extra_params) THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='settled GPU terminal proof required for retirement';
        END IF;
        status := 'retired';
    ELSE
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='unknown plugin installation command';
    END IF;
    i := i || jsonb_build_object('status',status,'revision',p_revision+1);
    IF p_action IN ('disable','quiesce','retire') AND is_local_gpu THEN
        INSERT INTO yoao_inference_routing_v1.plugin_terminal_update_guard
          (pid,xid,registry_id,old_extra,new_extra,old_auth,new_auth,old_gpu,new_gpu,old_budget,new_budget)
        VALUES (pg_catalog.pg_backend_pid(),pg_catalog.pg_current_xact_id(),p_id,r.extra_params,
          r.extra_params || jsonb_build_object('plugin',i,'plugin_lifecycle',lifecycle),r.auth_token,
          CASE WHEN status='retired' AND r.auth_token IS NOT NULL THEN 'retired-sha256:' || encode(sha256(convert_to(r.auth_token,'UTF8')),'hex') ELSE r.auth_token END,
          r.gpu_resource_id,CASE WHEN status='retired' THEN NULL ELSE r.gpu_resource_id END,
          r.vram_budget_mb,CASE WHEN status='retired' THEN NULL ELSE r.vram_budget_mb END);
    END IF;
    UPDATE public.ml_backend_registry SET extra_params=extra_params ||
        jsonb_build_object('plugin',i,'plugin_lifecycle',lifecycle),
        url=CASE WHEN p_action='cancel_prepared' AND is_direct
            THEN 'retired-cancelled://' || p_id::text
            ELSE url END,
        auth_token=CASE WHEN status='retired' AND auth_token IS NOT NULL
            THEN 'retired-sha256:' || encode(sha256(convert_to(auth_token,'UTF8')),'hex')
            ELSE auth_token END,
        gpu_resource_id=CASE WHEN status='retired' THEN NULL ELSE gpu_resource_id END,
        vram_budget_mb=CASE WHEN status='retired' THEN NULL ELSE vram_budget_mb END,
        updated_at=clock_timestamp()
    WHERE id=p_id;
    DELETE FROM yoao_inference_routing_v1.plugin_terminal_update_guard
      WHERE pid=pg_catalog.pg_backend_pid() AND xid=pg_catalog.pg_current_xact_id() AND registry_id=p_id;
    RETURN jsonb_build_object('registry_id',p_id,'installation',i);
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_inference_topology_v1.authorize_plugin_routing_replacement(
    p_id uuid, p_revision integer, p_control_id uuid, p_endpoint text,
    p_container_id text, p_boot_id text
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    r public.ml_backend_registry%ROWTYPE;
    t yoao_inference_gpu_v1.installation_terminal_intents%ROWTYPE;
    m public.gpu_backend_memberships%ROWTYPE;
    f public.gpu_backend_fences%ROWTYPE;
    s yoao_inference_routing_v1.registry_state%ROWTYPE;
    c yoao_inference_routing_v1.controls%ROWTYPE;
    b yoao_inference_routing_v1.plugin_replacement_bindings%ROWTYPE;
    digest bytea;
    expected_id uuid;
    result jsonb;
    is_direct boolean;
    boot_stopped boolean;
BEGIN
    IF p_id IS NULL OR p_revision IS NULL OR p_revision < 1 OR p_control_id IS NULL
       OR p_endpoint IS NULL OR pg_catalog.char_length(p_endpoint) NOT BETWEEN 1 AND 2048
       OR p_container_id IS NULL
       OR (p_container_id <> 'direct' AND p_container_id !~ '^[0-9a-f]{64}$')
       OR p_boot_id IS NULL OR pg_catalog.char_length(p_boot_id) NOT BETWEEN 1 AND 128
       OR p_boot_id <> pg_catalog.btrim(p_boot_id) THEN
        RETURN pg_catalog.jsonb_build_object('status','rejected','reason','invalid_identity');
    END IF;
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
    is_direct := coalesce(r.extra_params #>> '{plugin,execution_mode}','container') = 'direct';
    IF is_direct THEN
        IF p_container_id IS DISTINCT FROM 'direct' OR r.gpu_resource_id IS NOT NULL THEN
            RETURN pg_catalog.jsonb_build_object('status','rejected','reason','invalid_identity');
        END IF;
        SELECT * INTO s FROM yoao_inference_routing_v1.registry_state WHERE registry_id=p_id FOR UPDATE;
        IF s.registry_id IS NULL OR s.backend_incarnation IS NULL THEN
            RETURN pg_catalog.jsonb_build_object('status','not_replacement');
        END IF;
        boot_stopped := EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(
                CASE WHEN pg_catalog.jsonb_typeof(r.extra_params #> '{plugin_lifecycle,stopped}')='array'
                     THEN r.extra_params #> '{plugin_lifecycle,stopped}'
                     ELSE '[]'::jsonb END
            ) x
            WHERE x->>'boot_id'=p_boot_id
        );
        IF r.url IS DISTINCT FROM p_endpoint
           OR r.extra_params #>> '{plugin,installation_id}' IS DISTINCT FROM p_id::text
           OR r.extra_params #> '{plugin,revision}' IS DISTINCT FROM pg_catalog.to_jsonb(p_revision)
           OR r.extra_params #>> '{plugin,status}' IS DISTINCT FROM 'disabled'
           OR pg_catalog.jsonb_typeof(r.extra_params #> '{plugin_lifecycle,stopped}') IS DISTINCT FROM 'array'
           OR boot_stopped THEN
            RETURN pg_catalog.jsonb_build_object('status','rejected','reason','replacement_unproven');
        END IF;
        IF r.extra_params #>> '{plugin_lifecycle,activation,boot_id}' IS NOT DISTINCT FROM p_boot_id THEN
            RETURN pg_catalog.jsonb_build_object('status','not_replacement');
        END IF;
    ELSE
        IF p_container_id = 'direct' THEN
            RETURN pg_catalog.jsonb_build_object('status','rejected','reason','invalid_identity');
        END IF;
        SELECT * INTO t FROM yoao_inference_gpu_v1.installation_terminal_intents
          WHERE backend_registry_id=p_id FOR UPDATE;
        IF NOT FOUND THEN
            RETURN pg_catalog.jsonb_build_object('status','not_replacement');
        END IF;
        SELECT * INTO m FROM public.gpu_backend_memberships
          WHERE backend_registry_id=p_id AND gpu_resource_id=r.gpu_resource_id FOR UPDATE;
        SELECT * INTO f FROM public.gpu_backend_fences WHERE backend_registry_id=p_id FOR UPDATE;
        SELECT * INTO s FROM yoao_inference_routing_v1.registry_state WHERE registry_id=p_id FOR UPDATE;
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
    END IF;

    SELECT * INTO c FROM yoao_inference_routing_v1.controls WHERE id=p_control_id FOR UPDATE;
    SELECT * INTO b FROM yoao_inference_routing_v1.plugin_replacement_bindings WHERE control_id=p_control_id FOR UPDATE;
    IF b.control_id IS NOT NULL AND (b.registry_id IS DISTINCT FROM p_id OR b.revision IS DISTINCT FROM p_revision
       OR b.endpoint IS DISTINCT FROM p_endpoint OR b.container_id IS DISTINCT FROM p_container_id
       OR b.boot_id IS DISTINCT FROM p_boot_id) THEN
        RETURN pg_catalog.jsonb_build_object('status','rejected','reason','binding_changed');
    END IF;
    IF c.id IS NOT NULL AND c.phase = 'completed' THEN
        IF b.control_id IS NULL OR c.registry_id IS DISTINCT FROM p_id
           OR c.kind IS DISTINCT FROM 'epoch' OR c.recovered_kind IS NOT NULL
           OR c.config_revision IS DISTINCT FROM s.config_revision
           OR c.realm IS DISTINCT FROM s.realm
           OR s.realm IS DISTINCT FROM yoao_inference_routing_v1._routing_realm()
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
    IF EXISTS (SELECT 1 FROM yoao_inference_routing_v1.controls WHERE registry_id=p_id AND id<>p_control_id AND phase IN ('prepared','draining','uncertain'))
       OR EXISTS (SELECT 1 FROM yoao_inference_routing_v1.scopes WHERE registry_id=p_id AND state IN ('reserved','active','uncertain'))
       OR EXISTS (SELECT 1 FROM yoao_inference_routing_v1.attempts a JOIN yoao_inference_routing_v1.scopes q ON q.id=a.scope_id WHERE q.registry_id=p_id AND a.state IN ('active','uncertain'))
       OR EXISTS (SELECT 1 FROM yoao_inference_routing_v1.control_attempts a JOIN yoao_inference_routing_v1.controls q ON q.id=a.control_id WHERE q.registry_id=p_id AND a.state IN ('active','uncertain'))
       OR (b.control_id IS NULL AND EXISTS (SELECT 1 FROM yoao_inference_routing_v1.control_attempts WHERE control_id=p_control_id)) THEN
        RETURN pg_catalog.jsonb_build_object('status','rejected','reason','routing_not_quiescent');
    END IF;
    IF c.id IS NULL THEN
        result := yoao_inference_routing_v1.routing_step('prepare_epoch',
            pg_catalog.jsonb_build_object('control_id',p_control_id,'registry_id',p_id)::text);
        IF result->>'status' IS DISTINCT FROM 'ok' THEN
            RETURN pg_catalog.jsonb_build_object('status','rejected','reason',COALESCE(result->>'reason','prepare_failed'));
        END IF;
        SELECT * INTO c FROM yoao_inference_routing_v1.controls WHERE id=p_control_id FOR UPDATE;
    END IF;
    IF b.control_id IS NULL THEN
        INSERT INTO yoao_inference_routing_v1.plugin_replacement_bindings
          (control_id,registry_id,revision,endpoint,container_id,boot_id)
        VALUES (p_control_id,p_id,p_revision,p_endpoint,p_container_id,p_boot_id);
        UPDATE yoao_inference_routing_v1.controls SET backend_incarnation=NULL WHERE id=p_control_id;
    END IF;
    RETURN pg_catalog.jsonb_build_object('status','authorized');
END
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_inference_topology_v1.set_plugin_asset_approval(p_id uuid, p_expected integer, p_decision text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    r public.ml_backend_registry%ROWTYPE;
    d jsonb;
    old_revision integer;
    manifest jsonb;
    launch jsonb;
    activation jsonb;
    is_direct boolean;
BEGIN
    IF p_id IS NULL OR p_expected IS NULL OR p_expected < 0 OR p_decision IS NULL OR octet_length(p_decision) > 1048576 THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid asset approval';
    END IF;
    d := p_decision::jsonb;
    SELECT * INTO r FROM public.ml_backend_registry WHERE id=p_id FOR UPDATE;
    IF NOT FOUND OR NOT (r.extra_params ? 'plugin') OR r.extra_params #>> '{plugin,status}' IS DISTINCT FROM 'active' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='plugin installation unavailable';
    END IF;
    manifest := r.extra_params #> '{plugin,manifest}';
    launch := r.extra_params #> '{plugin_lifecycle,launch}';
    activation := r.extra_params #> '{plugin_lifecycle,activation}';
    is_direct := coalesce(r.extra_params #>> '{plugin,execution_mode}','container') = 'direct';
    IF jsonb_typeof(d) IS DISTINCT FROM 'object'
       OR d->>'expected_revision' IS DISTINCT FROM p_expected::text
       OR d->>'expected_installation_revision' IS DISTINCT FROM r.extra_params #>> '{plugin,revision}'
       OR d #>> '{descriptor,plugin_id}' IS DISTINCT FROM manifest->>'plugin_id'
       OR d #>> '{descriptor,plugin_version}' IS DISTINCT FROM manifest->>'version'
       OR manifest->>'asset_manifest_sha256' IS NULL
       OR d #>> '{descriptor,asset_manifest_sha256}' IS DISTINCT FROM manifest->>'asset_manifest_sha256'
       OR jsonb_typeof(d->'approved') IS DISTINCT FROM 'array'
       OR jsonb_typeof(d->'inventory') IS DISTINCT FROM 'array'
       OR COALESCE(d->>'descriptor_sha256','') !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='asset approval domain mismatch';
    END IF;
    IF is_direct THEN
        IF d #>> '{descriptor,artifact}' IS NOT NULL
           OR manifest->>'artifact' IS NOT NULL
           OR d->>'container_id' IS NOT NULL
           OR d->>'image_id' IS NOT NULL
           OR coalesce(activation->>'boot_id','') = ''
           OR d->>'runtime_instance_id' IS DISTINCT FROM activation->>'boot_id' THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='asset approval domain mismatch';
        END IF;
    ELSE
        IF d #>> '{descriptor,artifact}' IS DISTINCT FROM manifest->>'artifact'
           OR d->>'container_id' IS DISTINCT FROM launch->>'container_id'
           OR d->>'image_id' IS DISTINCT FROM launch->>'image_id'
           OR d->>'runtime_instance_id' IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='asset approval domain mismatch';
        END IF;
    END IF;
    SELECT revision INTO old_revision FROM yoao_inference_topology_v1.plugin_asset_approvals WHERE registry_id=p_id FOR UPDATE;
    IF COALESCE(old_revision,0) <> p_expected THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='stale asset approval revision';
    END IF;
    INSERT INTO yoao_inference_topology_v1.plugin_asset_approvals(registry_id,revision,decision)
    VALUES (p_id,p_expected+1,d)
    ON CONFLICT (registry_id) DO UPDATE SET revision=EXCLUDED.revision,decision=EXCLUDED.decision;
    RETURN jsonb_build_object('revision',p_expected+1,'decision',d);
END
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_inference_routing_v1.prediction_scope_reserve(p_arguments text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_args jsonb;
    v_proof jsonb;
    v_reservation jsonb;
    v_scope_id uuid;
    v_job_id uuid;
    v_lock jsonb;
    v_result jsonb;
    v_operation text;
    v_owner text;
BEGIN
    IF p_arguments IS NULL OR pg_catalog.octet_length(p_arguments) > 2097152 THEN
        RETURN yoao_inference_routing_v1._routing_rejected('invalid_command');
    END IF;
    v_args := p_arguments::jsonb;
    IF NOT yoao_inference_routing_v1._routing_exact_keys(v_args, ARRAY['proof','reservation']) THEN
        RETURN yoao_inference_routing_v1._routing_rejected('invalid_command');
    END IF;
    v_proof := v_args -> 'proof';
    v_reservation := v_args -> 'reservation';
    IF NOT yoao_jobs_v1._batch_result_scope_valid(v_proof, true)
       OR NOT yoao_inference_routing_v1._routing_exact_keys(v_reservation,
            ARRAY['snapshot','candidate_ids','scope_id','command_sha256','owner','operation','ttl_ms']) THEN
        RETURN yoao_inference_routing_v1._routing_rejected('invalid_command');
    END IF;
    v_job_id := (v_proof ->> 'job_id')::uuid;
    v_scope_id := yoao_inference_routing_v1._routing_uuid(v_reservation ->> 'scope_id');
    v_operation := v_reservation ->> 'operation';
    v_owner := v_reservation ->> 'owner';
    IF NOT ((v_operation = 'image_batch_predict'
             AND v_owner ~ ('^batch:' || v_job_id::text || ':stage:[0-9]+$'))
         OR (v_operation = 'prediction_retry'
             AND v_owner ~ ('^retry:' || v_job_id::text || ':stage:[0-9]+$'))) THEN
        RETURN yoao_inference_routing_v1._routing_rejected('invalid_command');
    END IF;
    v_lock := yoao_jobs_v1._lock_batch_result_scope(v_proof, true, true);
    IF v_lock -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR v_lock #> '{data,cancel_requested}' IS DISTINCT FROM 'false'::jsonb
       OR yoao_jobs_v1.prediction_fixture_open(v_job_id) IS DISTINCT FROM true THEN
        RETURN yoao_inference_routing_v1._routing_rejected('scope_unavailable');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.async_jobs AS job
        WHERE job.id = v_job_id
          AND ((job.kind = 'batch_predict' AND v_operation = 'image_batch_predict')
            OR (job.kind = 'prediction_retry' AND v_operation = 'prediction_retry'))
    ) THEN
        RETURN yoao_inference_routing_v1._routing_rejected('scope_unavailable');
    END IF;
    INSERT INTO yoao_inference_routing_v1.prediction_execution_scopes (
        scope_id, job_id, actor_id, project_id, request_digest, generation,
        lease_token_digest, creating_xid
    ) VALUES (
        v_scope_id, v_job_id, (v_proof ->> 'actor_id')::uuid,
        (v_proof ->> 'project_id')::uuid, v_proof ->> 'request_digest',
        (v_proof ->> 'generation')::bigint,
        public.digest(pg_catalog.convert_to(v_proof ->> 'lease_token', 'UTF8'), 'sha256'),
        pg_catalog.pg_current_xact_id()
    );
    v_result := yoao_inference_routing_v1.routing_step('reserve_scope', v_reservation::text);
    IF v_result ->> 'status' IS DISTINCT FROM 'ok' THEN
        DELETE FROM yoao_inference_routing_v1.prediction_execution_scopes WHERE scope_id = v_scope_id;
        RETURN v_result;
    END IF;
    IF v_result #>> '{scope,project_id}' IS DISTINCT FROM v_proof ->> 'project_id' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'prediction scope project mismatch';
    END IF;
    UPDATE yoao_inference_routing_v1.prediction_execution_scopes AS binding
    SET uses_gpu = registry.gpu_resource_id IS NOT NULL
        OR registry.vram_budget_mb IS NOT NULL
        OR (
            COALESCE(registry.extra_params #>> '{plugin,execution_mode}', 'container') <> 'direct'
            AND COALESCE(registry.extra_params #>> '{plugin,manifest,resource,device}', 'cpu') <> 'cpu'
        )
    FROM yoao_inference_routing_v1.scopes AS scope
    JOIN public.ml_backend_registry AS registry ON registry.id=scope.registry_id
    WHERE binding.scope_id=v_scope_id AND scope.id=binding.scope_id;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='prediction backend identity missing'; END IF;
    RETURN v_result;
END
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_inference_routing_v1.prediction_test_scope_reserve(p_arguments text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_args jsonb;
    v_proof jsonb;
    v_reservation jsonb;
    v_run_id uuid;
    v_root_id uuid;
    v_run_state text;
    v_root_state text;
    v_project_id uuid;
    v_task_id uuid;
    v_scope_id uuid;
    v_result jsonb;
BEGIN
    IF p_arguments IS NULL OR pg_catalog.octet_length(p_arguments) > 2097152 THEN
        RETURN yoao_inference_routing_v1._routing_rejected('invalid_command');
    END IF;
    v_args := p_arguments::jsonb;
    IF NOT yoao_inference_routing_v1._routing_exact_keys(v_args, ARRAY['proof','reservation']) THEN
        RETURN yoao_inference_routing_v1._routing_rejected('invalid_command');
    END IF;
    v_proof := v_args -> 'proof';
    v_reservation := v_args -> 'reservation';
    IF NOT yoao_inference_routing_v1._routing_exact_keys(v_proof, ARRAY['project_id','task_id'])
       OR NOT yoao_inference_routing_v1._routing_exact_keys(v_reservation,
            ARRAY['snapshot','candidate_ids','scope_id','command_sha256','owner','operation','ttl_ms']) THEN
        RETURN yoao_inference_routing_v1._routing_rejected('invalid_command');
    END IF;
    v_project_id := yoao_inference_routing_v1._routing_uuid(v_proof ->> 'project_id');
    v_task_id := yoao_inference_routing_v1._routing_uuid(v_proof ->> 'task_id');
    v_scope_id := yoao_inference_routing_v1._routing_uuid(v_reservation ->> 'scope_id');
    IF v_reservation ->> 'operation' IS DISTINCT FROM 'predict_test'
       OR v_reservation ->> 'owner' IS DISTINCT FROM 'api:predict-test:' || v_task_id::text
       OR NOT EXISTS (SELECT 1 FROM yoao_labeling_v1.task_rows AS task
                      WHERE task.id = v_task_id AND task.project_id = v_project_id) THEN
        RETURN yoao_inference_routing_v1._routing_rejected('scope_unavailable');
    END IF;
    SELECT resource.run_id INTO v_run_id
    FROM yoao_fixture_v1.fixture_run_resources AS resource
    WHERE resource.kind = 'project' AND resource.resource_id = v_project_id
      AND resource.state = 'published';
    IF v_run_id IS NOT NULL THEN
        SELECT COALESCE(run.parent_run_id, run.id) INTO v_root_id
        FROM yoao_fixture_v1.fixture_runs AS run WHERE run.id = v_run_id;
        SELECT run.state INTO v_root_state FROM yoao_fixture_v1.fixture_runs AS run
        WHERE run.id = v_root_id FOR SHARE NOWAIT;
        SELECT run.state INTO v_run_state FROM yoao_fixture_v1.fixture_runs AS run
        WHERE run.id = v_run_id FOR SHARE NOWAIT;
        IF v_root_state IS DISTINCT FROM 'open' OR v_run_state IS DISTINCT FROM 'open' THEN
            RETURN yoao_inference_routing_v1._routing_rejected('scope_unavailable');
        END IF;
        INSERT INTO yoao_inference_routing_v1.fixture_prediction_test_scopes(
            scope_id, run_id, project_id, task_id, creating_xid
        ) VALUES (v_scope_id, v_run_id, v_project_id, v_task_id, pg_catalog.pg_current_xact_id());
    END IF;
    v_result := yoao_inference_routing_v1.routing_step('reserve_scope', v_reservation::text);
    IF v_result ->> 'status' IS DISTINCT FROM 'ok' THEN
        DELETE FROM yoao_inference_routing_v1.fixture_prediction_test_scopes WHERE scope_id = v_scope_id;
        RETURN v_result;
    END IF;
    IF v_result #>> '{scope,project_id}' IS DISTINCT FROM v_project_id::text THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'prediction test project mismatch';
    END IF;
    IF v_run_id IS NOT NULL THEN
        UPDATE yoao_inference_routing_v1.fixture_prediction_test_scopes AS binding
        SET uses_gpu = registry.gpu_resource_id IS NOT NULL
            OR registry.vram_budget_mb IS NOT NULL
            OR (
                COALESCE(registry.extra_params #>> '{plugin,execution_mode}', 'container') <> 'direct'
                AND COALESCE(registry.extra_params #>> '{plugin,manifest,resource,device}', 'cpu') <> 'cpu'
            )
        FROM yoao_inference_routing_v1.scopes AS scope
        JOIN public.ml_backend_registry AS registry ON registry.id=scope.registry_id
        WHERE binding.scope_id=v_scope_id AND scope.id=binding.scope_id;
        IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='prediction backend identity missing'; END IF;
    END IF;
    RETURN v_result;
END
$function$;
