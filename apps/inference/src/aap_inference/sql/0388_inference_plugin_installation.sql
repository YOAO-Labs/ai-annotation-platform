-- A caller-owned transaction retains the Registry row lock until commit.  The
-- returned facts deliberately exclude the endpoint credential.
CREATE FUNCTION aap_inference_topology_v1.lock_plugin_registry(p_registry_id uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_registry_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid registry id';
    END IF;
    PERFORM 1 FROM public.ml_backend_registry WHERE id = p_registry_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    RETURN aap_inference_topology_v1._registry_json(p_registry_id);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.lock_plugin_registry(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.prepare_plugin_installation(p_intent text)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v jsonb := p_intent::jsonb;
    r public.ml_backend_registry%ROWTYPE;
    i jsonb;
    k uuid;
BEGIN
    IF jsonb_typeof(v) IS DISTINCT FROM 'object'
       OR v - ARRAY['installation_id','name','url','auth_token','manifest','resource'] <> '{}'::jsonb
       OR jsonb_typeof(v->'manifest') IS DISTINCT FROM 'object'
       OR jsonb_typeof(v->'resource') IS DISTINCT FROM 'object'
       OR coalesce(length(v->>'auth_token'),0) < 32
       OR coalesce(v->>'name','') = '' OR coalesce(v->>'url','') = ''
       OR v->>'url' IS DISTINCT FROM rtrim(v->>'url','/') THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid plugin installation intent';
    END IF;
    k := (v->>'installation_id')::uuid;
    IF k IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid plugin installation identity';
    END IF;
    -- Serialize even an absent UUID, so concurrent idempotent creates compare
    -- intent after the first insert rather than racing a unique violation.
    PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(k::text, 388));
    SELECT * INTO r FROM public.ml_backend_registry WHERE id=k FOR UPDATE;
    IF FOUND THEN
        i := r.extra_params->'plugin';
        IF i IS NULL OR i->>'installation_id' IS DISTINCT FROM k::text
           OR i->'manifest' IS DISTINCT FROM v->'manifest'
           OR i->'resource' IS DISTINCT FROM v->'resource'
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
    i := jsonb_build_object('installation_id',k,'manifest',v->'manifest',
        'resource',v->'resource','status','prepared','revision',1);
    BEGIN
        INSERT INTO public.ml_backend_registry
            (id,name,url,state,is_interactive,auth_method,auth_token,extra_params,
             gpu_resource_id,vram_budget_mb,eviction_priority,source)
        VALUES (k,v->>'name',v->>'url','disconnected',false,'token',v->>'auth_token',
            jsonb_build_object('plugin',i,'plugin_lifecycle',jsonb_build_object('stopped', '[]'::jsonb)),
            v->'resource'->>'gpu_resource_id',
            NULLIF(v->'manifest'->'resource'->>'gpu_memory_mb','0')::integer,0,'manual');
    EXCEPTION WHEN unique_violation THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='plugin installation identity or endpoint already exists';
    END;
    RETURN jsonb_build_object('registry_id',k,'installation',i);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.plugin_installation(p_id uuid)
RETURNS jsonb LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT jsonb_build_object('registry_id',r.id,'installation',r.extra_params->'plugin')
    FROM public.ml_backend_registry r
    WHERE r.id=p_id AND r.extra_params ? 'plugin'
$function$;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.plugin_installations()
RETURNS jsonb LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT coalesce(jsonb_agg(jsonb_build_object('registry_id',r.id,
        'installation',r.extra_params->'plugin') ORDER BY r.id),'[]'::jsonb)
    FROM public.ml_backend_registry r WHERE r.extra_params ? 'plugin'
$function$;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.plugin_installation_evidence(p_id uuid)
RETURNS jsonb LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT r.extra_params->'plugin_lifecycle'
    FROM public.ml_backend_registry r WHERE r.id=p_id AND r.extra_params ? 'plugin'
$function$;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.command_plugin_installation(
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
        status := 'disabled';
    ELSIF p_action='retire' THEN
        IF status = 'retired' THEN
            RETURN jsonb_build_object('registry_id',p_id,'installation',i);
        END IF;
        IF status <> 'disabled' OR jsonb_typeof(lifecycle->'proof') IS DISTINCT FROM 'object'
           OR (lifecycle->'proof'->>'revision')::integer IS DISTINCT FROM p_revision THEN
            RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='valid quiescence proof required for retirement';
        END IF;
        status := 'retired';
    ELSE
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='unknown plugin installation command';
    END IF;
    i := i || jsonb_build_object('status',status,'revision',p_revision+1);
    UPDATE public.ml_backend_registry SET extra_params=extra_params ||
        jsonb_build_object('plugin',i,'plugin_lifecycle',lifecycle),
        auth_token=CASE WHEN status='retired' AND auth_token IS NOT NULL
            THEN 'retired-sha256:' || encode(sha256(convert_to(auth_token,'UTF8')),'hex')
            ELSE auth_token END,
        updated_at=clock_timestamp()
    WHERE id=p_id;
    RETURN jsonb_build_object('registry_id',p_id,'installation',i);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.prepare_plugin_installation(text) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.plugin_installation(uuid) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.plugin_installations() FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.plugin_installation_evidence(uuid) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.command_plugin_installation(uuid,integer,text,text) FROM PUBLIC;
