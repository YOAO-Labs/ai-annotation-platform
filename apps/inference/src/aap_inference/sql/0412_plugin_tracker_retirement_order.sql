-- Refuse a plugin stop until every tracker scope has the existing exact
-- routing-owner retirement proof.  Scope rows and proof stay Inference-owned.
CREATE FUNCTION aap_inference_routing_v1._tracker_scopes_physically_retired(p_registry_id uuid)
RETURNS boolean
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT p_registry_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM aap_inference_routing_v1.scopes AS scope
        WHERE scope.registry_id = p_registry_id AND scope.operation = 'tracker'
          AND aap_inference_routing_v1.video_execution_scope_retired(scope.id)
              IS DISTINCT FROM true
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._tracker_scopes_physically_retired(uuid)
    FROM PUBLIC;
-- aap-statement-break
-- The Registry row is locked by routing reservation and plugin lifecycle
-- commands.  Do not create a tracker scope after a plugin has been disabled.
CREATE FUNCTION aap_inference_routing_v1._guard_disabled_plugin_tracker_scope()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_extra jsonb;
BEGIN
    IF NEW.operation <> 'tracker' THEN
        RETURN NEW;
    END IF;
    SELECT registry.extra_params INTO v_extra
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = NEW.registry_id FOR SHARE;
    IF FOUND AND v_extra ? 'plugin'
       AND v_extra #>> '{plugin,status}' IS DISTINCT FROM 'active' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'disabled plugin cannot admit tracker scopes';
    END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._guard_disabled_plugin_tracker_scope()
    FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER aap_guard_disabled_plugin_tracker_scope
BEFORE INSERT ON aap_inference_routing_v1.scopes
FOR EACH ROW EXECUTE FUNCTION aap_inference_routing_v1._guard_disabled_plugin_tracker_scope();
-- aap-statement-break
DO $grant$
DECLARE
    routing_owner name;
    topology_owner name;
    jobs_owner name;
    routing_needs_create boolean;
    jobs_need_create boolean;
BEGIN
    SELECT pg_catalog.pg_get_userbyid(proowner) INTO routing_owner
    FROM pg_catalog.pg_proc
    WHERE oid = 'aap_inference_routing_v1.video_execution_scope_retired(uuid)'::regprocedure;
    SELECT pg_catalog.pg_get_userbyid(proowner) INTO topology_owner
    FROM pg_catalog.pg_proc
    WHERE oid = 'aap_inference_topology_v1.command_plugin_installation(uuid,integer,text,text)'::regprocedure;
    SELECT pg_catalog.pg_get_userbyid(proowner) INTO jobs_owner
    FROM pg_catalog.pg_proc
    WHERE oid = 'aap_jobs_v1.video_execution_step(text,text)'::regprocedure;
    routing_needs_create := NOT pg_catalog.has_schema_privilege(
        routing_owner, 'aap_inference_routing_v1', 'CREATE'
    );
    jobs_need_create := NOT pg_catalog.has_schema_privilege(
        jobs_owner, 'aap_jobs_v1', 'CREATE'
    );
    IF routing_needs_create THEN
        EXECUTE pg_catalog.format('GRANT CREATE ON SCHEMA aap_inference_routing_v1 TO %I', routing_owner);
    END IF;
    IF jobs_need_create THEN
        EXECUTE pg_catalog.format('GRANT CREATE ON SCHEMA aap_jobs_v1 TO %I', jobs_owner);
    END IF;
    EXECUTE pg_catalog.format('ALTER FUNCTION aap_inference_routing_v1._tracker_scopes_physically_retired(uuid) OWNER TO %I', routing_owner);
    EXECUTE pg_catalog.format('ALTER FUNCTION aap_inference_routing_v1._guard_disabled_plugin_tracker_scope() OWNER TO %I', routing_owner);
    EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA aap_inference_routing_v1 TO %I', topology_owner);
    EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION aap_inference_routing_v1._tracker_scopes_physically_retired(uuid) TO %I', topology_owner);
    EXECUTE pg_catalog.format(
        'ALTER FUNCTION aap_jobs_v1._video_execution_settled(uuid,uuid,text,text,uuid) OWNER TO %I',
        jobs_owner
    );
    IF routing_needs_create THEN
        EXECUTE pg_catalog.format('REVOKE CREATE ON SCHEMA aap_inference_routing_v1 FROM %I', routing_owner);
    END IF;
    IF jobs_need_create THEN
        EXECUTE pg_catalog.format('REVOKE CREATE ON SCHEMA aap_jobs_v1 FROM %I', jobs_owner);
    END IF;
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
    IF p_action IN ('check_tracker_retirement','disable','quiesce','retire','cancel_prepared') THEN
        IF NOT aap_inference_routing_v1._tracker_scopes_physically_retired(p_id) THEN
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
