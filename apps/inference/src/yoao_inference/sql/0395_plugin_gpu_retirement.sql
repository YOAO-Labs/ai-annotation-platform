-- Release a retired installation's physical claim only after its owner proof.
CREATE FUNCTION aap_inference_gpu_v1.gpu_plugin_retirement_claim_valid(p_id uuid, p_extra jsonb)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF p_extra #>> '{plugin,status}' = 'disabled' THEN
        RETURN aap_inference_gpu_v1.gpu_plugin_terminal_proof_valid(p_id, p_extra);
    END IF;
    IF p_extra #>> '{plugin,status}' = 'retired'
       AND (p_extra #>> '{plugin_lifecycle,proof,revision}')::integer =
           (p_extra #>> '{plugin,revision}')::integer - 1 THEN
        RETURN aap_inference_gpu_v1.gpu_plugin_terminal_proof_valid(p_id,
            jsonb_set(p_extra, '{plugin,revision}', p_extra #> '{plugin_lifecycle,proof,revision}'));
    END IF;
    RETURN false;
END
$function$;
-- aap-statement-break
-- A settled, revoked installation is authoritative closure, not a new live health probe.
CREATE FUNCTION aap_inference_gpu_v1.gpu_plugin_retirement_closure(
    p_id uuid, p_resource text, p_epoch bigint
) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    r public.ml_backend_registry%ROWTYPE;
    m public.gpu_backend_memberships%ROWTYPE;
    f public.gpu_backend_fences%ROWTYPE;
    lifecycle jsonb;
    kind text;
BEGIN
    SELECT * INTO r FROM public.ml_backend_registry WHERE id=p_id;
    SELECT * INTO m FROM public.gpu_backend_memberships
      WHERE backend_registry_id=p_id AND gpu_resource_id=p_resource;
    SELECT * INTO f FROM public.gpu_backend_fences WHERE backend_registry_id=p_id;
    IF r.id IS NULL OR m.backend_registry_id IS NULL OR f.backend_registry_id IS NULL
       OR r.gpu_resource_id IS NOT NULL
       OR r.extra_params #>> '{plugin,status}' IS DISTINCT FROM 'retired'
       OR r.extra_params #>> '{plugin,resource,gpu_resource_id}' IS DISTINCT FROM p_resource
       OR r.extra_params #>> '{plugin,manifest,resource,device}' IS DISTINCT FROM 'cuda'
       OR r.auth_token IS NULL OR r.auth_token !~ '^retired-sha256:[0-9a-f]{64}$'
       OR m.state <> 'retiring' OR m.membership_epoch <> p_epoch OR m.retirement_id IS NULL
       OR m.retired_generation_high_water IS DISTINCT FROM f.generation_high_water
       OR m.retired_control_epoch_high_water IS DISTINCT FROM f.control_epoch_high_water
       OR m.retired_runtime_epoch_high_water IS DISTINCT FROM f.runtime_epoch_high_water
       OR m.retired_token_expiry_high_water IS DISTINCT FROM f.token_expiry_high_water THEN
        RETURN NULL;
    END IF;
    lifecycle := r.extra_params->'plugin_lifecycle';
    IF lifecycle->'cancelled_prepared' = 'true'::jsonb
       AND NOT (lifecycle ?| ARRAY['launch','activation','proof','qualification'])
       AND lifecycle->'stopped' = '[]'::jsonb
       AND f.generation_high_water=0 AND f.control_epoch_high_water=0
       AND f.runtime_epoch_high_water=0 AND f.token_expiry_high_water IS NULL
       AND NOT EXISTS (SELECT 1 FROM aap_inference_gpu_v1.installation_control_intents WHERE backend_registry_id=p_id)
       AND NOT EXISTS (SELECT 1 FROM aap_inference_gpu_v1.installation_terminal_intents WHERE backend_registry_id=p_id) THEN
        kind := 'never_launched';
    ELSIF EXISTS (
        SELECT 1 FROM aap_inference_gpu_v1.installation_terminal_intents t
        WHERE t.backend_registry_id=p_id AND t.settled
          AND t.membership_epoch=m.membership_epoch-1
          AND t.revision=to_jsonb((r.extra_params #>> '{plugin,revision}')::integer-2)
          AND (lifecycle #>> '{proof,revision}')::integer=(r.extra_params #>> '{plugin,revision}')::integer-1
          AND t.generation=f.generation_high_water AND t.control_epoch=f.control_epoch_high_water
          AND t.runtime_epoch=f.runtime_epoch_high_water
          AND t.request->>'registry_id'=p_id::text
          AND t.request->>'resource_id'=p_resource
          AND t.request->>'endpoint'=r.url
          AND t.request->>'endpoint'=lifecycle #>> '{proof,endpoint}'
          AND t.request->>'boot_id'=lifecycle #>> '{proof,boot_id}'
          AND t.request->>'operation_id'=lifecycle #>> '{proof,operation_id}'
          AND t.request->>'container_id'=lifecycle #>> '{proof,container_id}'
          AND t.request->'pool_ids'=lifecycle #> '{proof,pool_ids}'
          AND t.generation::text=lifecycle #>> '{proof,generation}'
          AND t.control_epoch::text=lifecycle #>> '{proof,control_epoch}'
          AND lifecycle->'stopped' @> jsonb_build_array(jsonb_build_object(
              'container_id',t.request->>'container_id','boot_id',t.request->>'boot_id'))
    ) THEN
        kind := 'terminal_unloaded';
    ELSE
        RETURN NULL;
    END IF;
    RETURN jsonb_build_object('kind',kind,'registry_id',p_id,'resource_id',p_resource,
        'membership_epoch',p_epoch,'retirement_id',m.retirement_id,
        'installation_revision',r.extra_params #> '{plugin,revision}',
        'generation',f.generation_high_water,'control_epoch',f.control_epoch_high_water,
        'runtime_epoch',f.runtime_epoch_high_water,'operation_id',lifecycle #> '{proof,operation_id}');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_gpu_v1.gpu_plugin_retirement_claim_valid(uuid,jsonb) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_gpu_v1.gpu_plugin_retirement_closure(uuid,text,bigint) FROM PUBLIC;
-- aap-statement-break
DO $block$
DECLARE gpu_owner name; topology_owner name; collector_owner name;
BEGIN
    SELECT pg_get_userbyid(proowner) INTO gpu_owner FROM pg_proc
      WHERE oid='aap_inference_gpu_v1.gpu_authority_step(text,text)'::regprocedure;
    SELECT pg_get_userbyid(proowner) INTO topology_owner FROM pg_proc
      WHERE oid='aap_inference_topology_v1.command_plugin_installation(uuid,integer,text,text)'::regprocedure;
    SELECT pg_get_userbyid(proowner) INTO collector_owner FROM pg_proc
      WHERE oid='aap_inference_gpu_collector_v1.gpu_collector_step(text,text)'::regprocedure;
    EXECUTE format('ALTER FUNCTION aap_inference_gpu_v1.gpu_plugin_retirement_claim_valid(uuid,jsonb) OWNER TO %I',gpu_owner);
    EXECUTE format('ALTER FUNCTION aap_inference_gpu_v1.gpu_plugin_retirement_closure(uuid,text,bigint) OWNER TO %I',gpu_owner);
    EXECUTE format('GRANT EXECUTE ON FUNCTION aap_inference_gpu_v1.gpu_plugin_retirement_claim_valid(uuid,jsonb) TO %I',topology_owner);
    EXECUTE format('GRANT EXECUTE ON FUNCTION aap_inference_gpu_v1.gpu_plugin_retirement_closure(uuid,text,bigint) TO %I',collector_owner);
END
$block$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION public.sync_gpu_backend_membership()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, public, pg_temp
AS $function$
DECLARE
    new_max_concurrency integer := 4;
    runtime_epoch bigint := 0;
    fence_generation bigint := 0;
    fence_control_epoch bigint := 0;
    fence_token_expiry timestamptz := NULL;
    retirement_reason text := NULL;
    protected_changed boolean := true;
    affected_rows integer := 0;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        protected_changed := (
            OLD.gpu_resource_id IS DISTINCT FROM NEW.gpu_resource_id OR
            OLD.vram_budget_mb IS DISTINCT FROM NEW.vram_budget_mb OR
            OLD.eviction_priority IS DISTINCT FROM NEW.eviction_priority OR
            (COALESCE(OLD.extra_params, '{}'::jsonb) - 'plugin' - 'plugin_lifecycle') IS DISTINCT FROM
            (COALESCE(NEW.extra_params, '{}'::jsonb) - 'plugin' - 'plugin_lifecycle') OR
            ((OLD.extra_params->'plugin') - 'status' - 'revision') IS DISTINCT FROM
            ((NEW.extra_params->'plugin') - 'status' - 'revision') OR
            OLD.url IS DISTINCT FROM NEW.url OR
            OLD.auth_method IS DISTINCT FROM NEW.auth_method OR
            (OLD.auth_token IS DISTINCT FROM NEW.auth_token AND NOT (
                OLD.auth_token IS NOT NULL AND NEW.auth_token =
                    'retired-sha256:' || pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(OLD.auth_token, 'UTF8')), 'hex')
                AND OLD.extra_params #>> '{plugin,status}' = 'disabled'
                AND NEW.extra_params #>> '{plugin,status}' = 'retired'
                AND aap_inference_gpu_v1.gpu_plugin_terminal_proof_valid(OLD.id, OLD.extra_params)
            ))
        );
        IF NOT protected_changed THEN
            RETURN NEW;
        END IF;
    END IF;

    IF TG_OP <> 'INSERT' THEN
        IF OLD.gpu_resource_id IS NOT NULL THEN
            PERFORM 1
            FROM gpu_backend_memberships
            WHERE backend_registry_id = OLD.id
              AND gpu_resource_id = OLD.gpu_resource_id
              AND state IN ('pending', 'active')
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'current GPU membership is missing'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_gpu_backend_membership_current_missing';
            END IF;
        END IF;

        SELECT
            runtime_epoch_high_water,
            generation_high_water,
            control_epoch_high_water,
            token_expiry_high_water
        INTO
            runtime_epoch,
            fence_generation,
            fence_control_epoch,
            fence_token_expiry
        FROM gpu_backend_fences
        WHERE backend_registry_id = OLD.id
        FOR UPDATE;
        IF NOT FOUND THEN
            IF OLD.gpu_resource_id IS NOT NULL THEN
                RAISE EXCEPTION 'durable GPU fence is missing'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_gpu_backend_membership_fence_missing';
            END IF;
            runtime_epoch := 0;
            fence_generation := 0;
            fence_control_epoch := 0;
            fence_token_expiry := NULL;
        END IF;
    END IF;

    IF TG_OP <> 'DELETE' AND NEW.gpu_resource_id IS NOT NULL THEN
        IF NOT (COALESCE(NEW.extra_params, '{}'::jsonb) ? 'max_concurrency') THEN
            new_max_concurrency := 4;
        ELSIF COALESCE(NEW.extra_params ->> 'max_concurrency', '')
                  ~ '^[1-9][0-9]{0,4}$' THEN
            new_max_concurrency := (NEW.extra_params ->> 'max_concurrency')::integer;
            IF new_max_concurrency > 10000 THEN
                RAISE EXCEPTION 'max_concurrency must be an integer from 1 to 10000'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_gpu_backend_membership_max_concurrency';
            END IF;
        ELSE
            RAISE EXCEPTION 'max_concurrency must be an integer from 1 to 10000'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'ck_gpu_backend_membership_max_concurrency';
        END IF;
    END IF;

    IF TG_OP = 'DELETE' THEN
        IF runtime_epoch > 0 THEN
            RAISE EXCEPTION 'managed GPU backend requires retirement before delete'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'ck_gpu_backend_membership_managed_mutation';
        END IF;
        retirement_reason := 'registry_deleted';
    ELSIF TG_OP = 'UPDATE' THEN
        IF runtime_epoch > 0 AND NOT COALESCE((
            NEW.gpu_resource_id IS NULL AND NEW.vram_budget_mb IS NULL
            AND NEW.extra_params #>> '{plugin,status}' = 'retired'
            AND OLD.extra_params #>> '{plugin,status}' IN ('disabled','retired')
            AND OLD.url IS NOT DISTINCT FROM NEW.url
            AND OLD.auth_method IS NOT DISTINCT FROM NEW.auth_method
            AND OLD.eviction_priority IS NOT DISTINCT FROM NEW.eviction_priority
            AND (OLD.extra_params - 'plugin') IS NOT DISTINCT FROM (NEW.extra_params - 'plugin')
            AND ((OLD.extra_params->'plugin') - 'status' - 'revision') IS NOT DISTINCT FROM
                ((NEW.extra_params->'plugin') - 'status' - 'revision')
            AND ((OLD.extra_params #>> '{plugin,status}' = 'retired'
                  AND OLD.auth_token ~ '^retired-sha256:[0-9a-f]{64}$'
                  AND OLD.auth_token IS NOT DISTINCT FROM NEW.auth_token)
                 OR (OLD.extra_params #>> '{plugin,status}' = 'disabled'
                     AND NEW.auth_token = 'retired-sha256:' ||
                         pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(OLD.auth_token,'UTF8')),'hex')))
            AND aap_inference_gpu_v1.gpu_plugin_retirement_claim_valid(OLD.id, OLD.extra_params)
        ), false) THEN
            RAISE EXCEPTION 'managed GPU backend requires retirement before mutation'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'ck_gpu_backend_membership_managed_mutation';
        END IF;
        IF OLD.gpu_resource_id IS DISTINCT FROM NEW.gpu_resource_id THEN
            retirement_reason := CASE
                WHEN NEW.gpu_resource_id IS NULL THEN 'claim_removed'
                ELSE 'resource_moved'
            END;
        END IF;
    END IF;

    IF TG_OP <> 'INSERT'
       AND OLD.gpu_resource_id IS NOT NULL
       AND (TG_OP = 'DELETE' OR OLD.gpu_resource_id IS DISTINCT FROM NEW.gpu_resource_id) THEN
        UPDATE gpu_backend_memberships
        SET state = 'retiring',
            membership_epoch = membership_epoch + 1,
            retired_at = clock_timestamp(),
            retire_reason = retirement_reason,
            retired_health_state = OLD.state,
            retired_health_meta = OLD.health_meta,
            retired_health_checked_at = OLD.last_checked_at,
            retired_generation_high_water = fence_generation,
            retired_control_epoch_high_water = fence_control_epoch,
            retired_runtime_epoch_high_water = runtime_epoch,
            retired_token_expiry_high_water = fence_token_expiry,
            updated_at = clock_timestamp()
        WHERE backend_registry_id = OLD.id
          AND gpu_resource_id = OLD.gpu_resource_id
          AND state IN ('pending', 'active');
        GET DIAGNOSTICS affected_rows = ROW_COUNT;
        IF affected_rows <> 1 THEN
            RAISE EXCEPTION 'current GPU membership changed during retirement'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'ck_gpu_backend_membership_retirement_race';
        END IF;
    END IF;

    IF TG_OP <> 'DELETE' AND NEW.gpu_resource_id IS NOT NULL THEN
        IF TG_OP = 'INSERT' OR OLD.gpu_resource_id IS NULL THEN
            INSERT INTO gpu_backend_fences (
                backend_registry_id,
                generation_high_water,
                control_epoch_high_water,
                runtime_epoch_high_water
            ) VALUES (NEW.id, 0, 0, 0)
            ON CONFLICT (backend_registry_id) DO NOTHING;
            IF TG_OP = 'INSERT' THEN
                SELECT
                    runtime_epoch_high_water,
                    generation_high_water,
                    control_epoch_high_water,
                    token_expiry_high_water
                INTO
                    runtime_epoch,
                    fence_generation,
                    fence_control_epoch,
                    fence_token_expiry
                FROM gpu_backend_fences
                WHERE backend_registry_id = NEW.id
                FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'durable GPU fence is missing'
                        USING ERRCODE = '23514',
                              CONSTRAINT = 'ck_gpu_backend_membership_fence_missing';
                END IF;
            END IF;
        END IF;

        IF TG_OP = 'INSERT'
           OR OLD.gpu_resource_id IS NULL
           OR OLD.gpu_resource_id IS DISTINCT FROM NEW.gpu_resource_id THEN
            INSERT INTO gpu_backend_memberships (
                backend_registry_id,
                gpu_resource_id,
                membership_epoch,
                runtime_epoch_baseline,
                state,
                vram_budget_mb,
                eviction_priority,
                max_concurrency
            ) VALUES (
                NEW.id,
                NEW.gpu_resource_id,
                1,
                runtime_epoch,
                'pending',
                NEW.vram_budget_mb,
                NEW.eviction_priority,
                new_max_concurrency
            )
            ON CONFLICT (backend_registry_id, gpu_resource_id) DO NOTHING;
            GET DIAGNOSTICS affected_rows = ROW_COUNT;
            IF affected_rows <> 1 THEN
                RAISE EXCEPTION 'retired GPU membership must be collected before resource re-entry'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_gpu_backend_membership_resource_reentry';
            END IF;
        ELSE
            UPDATE gpu_backend_memberships
            SET membership_epoch = membership_epoch + 1,
                runtime_epoch_baseline = runtime_epoch,
                state = 'pending',
                vram_budget_mb = NEW.vram_budget_mb,
                eviction_priority = NEW.eviction_priority,
                max_concurrency = new_max_concurrency,
                updated_at = clock_timestamp()
            WHERE backend_registry_id = NEW.id
              AND gpu_resource_id = NEW.gpu_resource_id
              AND state IN ('pending', 'active');
            GET DIAGNOSTICS affected_rows = ROW_COUNT;
            IF affected_rows <> 1 THEN
                RAISE EXCEPTION 'current GPU membership changed during configuration update'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_gpu_backend_membership_update_race';
            END IF;
        END IF;
    ELSIF TG_OP = 'DELETE' AND OLD.gpu_resource_id IS NULL THEN
        IF EXISTS (
            SELECT 1
            FROM gpu_backend_fences AS fence
            WHERE fence.backend_registry_id = OLD.id
        ) THEN
            DELETE FROM gpu_backend_fences AS fence
            WHERE fence.backend_registry_id = OLD.id
              AND NOT EXISTS (
                  SELECT 1
                  FROM gpu_backend_memberships AS membership
                  WHERE membership.backend_registry_id = OLD.id
              );
        END IF;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        UPDATE ml_backend_registry
        SET state = 'disconnected',
            health_meta = NULL,
            last_checked_at = NULL,
            error_message = NULL
        WHERE id = NEW.id;
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$function$;
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
    UPDATE public.ml_backend_registry SET extra_params=extra_params ||
        jsonb_build_object('plugin',i,'plugin_lifecycle',lifecycle),
        auth_token=CASE WHEN status='retired' AND auth_token IS NOT NULL
            THEN 'retired-sha256:' || encode(sha256(convert_to(auth_token,'UTF8')),'hex')
            ELSE auth_token END,
        gpu_resource_id=CASE WHEN status='retired' THEN NULL ELSE gpu_resource_id END,
        vram_budget_mb=CASE WHEN status='retired' THEN NULL ELSE vram_budget_mb END,
        updated_at=clock_timestamp()
    WHERE id=p_id;
    RETURN jsonb_build_object('registry_id',p_id,'installation',i);
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_gpu_collector_v1.gpu_collector_step(p_action text, p_arguments text)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
SET DateStyle = 'ISO, YMD'
SET extra_float_digits = 3
AS $function$
DECLARE
    v_args jsonb;
    v_proof jsonb;
    v_residency jsonb;
    v_result jsonb;
    v_id uuid;
    v_resource text;
    v_epoch bigint;
    v_retirement uuid;
    v_membership public.gpu_backend_memberships%ROWTYPE;
    v_fence public.gpu_backend_fences%ROWTYPE;
    v_registry_exists boolean;
    v_domain jsonb;
    v_probe_started timestamp with time zone;
    v_observed timestamp with time zone;
    v_now timestamp with time zone;
    v_deadline bigint;
    v_expected_deadline bigint;
    v_fingerprint text;
    v_collection_id text;
    v_receipt jsonb;
    v_receipt_fingerprint text;
    v_closure jsonb;
BEGIN
    IF p_action IS DISTINCT FROM 'collect_tombstone'
       OR p_arguments IS NULL OR pg_catalog.octet_length(p_arguments)>1048576 THEN
        RAISE EXCEPTION 'invalid bounded GPU collector request' USING ERRCODE='22023';
    END IF;
    BEGIN v_args:=p_arguments::jsonb;
    EXCEPTION WHEN invalid_text_representation THEN RAISE EXCEPTION 'invalid bounded GPU collector request' USING ERRCODE='22023'; END;
    IF pg_catalog.jsonb_typeof(v_args)<>'object' THEN RAISE EXCEPTION 'invalid bounded GPU collector request' USING ERRCODE='22023'; END IF;
    v_now:=pg_catalog.transaction_timestamp();

    IF v_args->>'phase'='prepare' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['phase','backend_id','resource_id','membership_epoch','proof'])
           OR (v_args->'proof'<>'null'::jsonb AND NOT aap_inference_gpu_v1._exact_keys(v_args->'proof',ARRAY['challenge','probe_started_at','observed_at','evidence_deadline_ms','evidence_fingerprint','endpoint_sha256','residency'])) THEN
            RAISE EXCEPTION 'invalid bounded GPU collector request' USING ERRCODE='22023';
        END IF;
        v_id:=aap_inference_gpu_v1._uuid(v_args->'backend_id');
        v_resource:=aap_inference_gpu_v1._resource(v_args->'resource_id');
        v_epoch:=aap_inference_gpu_v1._int(v_args->'membership_epoch',1,9223372036854775807);
        IF v_args->'proof'<>'null'::jsonb THEN
            v_proof:=v_args->'proof';
            IF pg_catalog.jsonb_typeof(v_proof->'residency')<>'object' THEN RAISE EXCEPTION 'invalid bounded GPU collector request' USING ERRCODE='22023'; END IF;
            IF aap_inference_gpu_v1._string(v_proof->'challenge',64) !~ '^[0-9a-f]{64}$'
               OR aap_inference_gpu_v1._string(v_proof->'evidence_fingerprint',64) !~ '^[0-9a-f]{64}$'
               OR aap_inference_gpu_v1._string(v_proof->'endpoint_sha256',64) !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'invalid bounded GPU collector request' USING ERRCODE='22023';
            END IF;
            PERFORM aap_inference_gpu_v1._int(v_proof->'evidence_deadline_ms',1,9223372036854775807);
            v_probe_started:=aap_inference_gpu_v1._timestamp(v_proof->'probe_started_at');
            v_observed:=aap_inference_gpu_v1._timestamp(v_proof->'observed_at');
            v_residency:=v_proof->'residency';
            IF NOT aap_inference_gpu_v1._residency_shape(v_residency) THEN
                RAISE EXCEPTION 'invalid bounded GPU collector request' USING ERRCODE='22023';
            END IF;

        END IF;

        PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('aap:gpu-resource:'||v_resource,0));
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m
         WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        IF NOT FOUND THEN v_result:=pg_catalog.jsonb_build_object('status','already_absent');
        ELSIF v_membership.state<>'retiring' OR v_membership.membership_epoch<>v_epoch OR v_membership.retirement_id IS NULL THEN
            v_result:=pg_catalog.jsonb_build_object('status','stale','reason','membership_changed');
        ELSE
            SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
            IF NOT FOUND THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','fence_missing');
            ELSIF v_membership.retired_token_expiry_high_water IS NOT NULL AND v_now<=v_membership.retired_token_expiry_high_water THEN
                v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','waiting_token_horizon');
            ELSE
                SELECT EXISTS(SELECT 1 FROM public.ml_backend_registry r WHERE r.id=v_id) INTO v_registry_exists;
                SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                    'backend_id',m.backend_registry_id,'membership_epoch',m.membership_epoch,'state',m.state
                ) ORDER BY m.backend_registry_id),'[]'::jsonb)
                INTO v_domain FROM public.gpu_backend_memberships m WHERE m.gpu_resource_id=v_resource;
                IF v_proof IS NULL THEN
                    v_closure := aap_inference_gpu_v1.gpu_plugin_retirement_closure(v_id,v_resource,v_epoch);
                    IF v_closure IS NULL THEN
                        v_result:=pg_catalog.jsonb_build_object('status','receipt_required','backend_memberships',v_domain,'retirement_id',v_membership.retirement_id,'registry_exists',v_registry_exists);
                    ELSE
                        v_deadline := floor(extract(epoch FROM pg_catalog.clock_timestamp())*1000)::bigint + 180000;
                        v_fingerprint := pg_catalog.encode(public.digest(v_closure::text,'sha256'),'hex');
                        v_collection_id := pg_catalog.encode(public.digest(pg_catalog.convert_to(
                            v_fingerprint||':'||v_id::text||':'||v_epoch::text||':'||v_membership.retirement_id::text,'UTF8'
                        ),'sha256'),'hex');
                        v_result := pg_catalog.jsonb_build_object(
                            'status','closure_valid','backend_memberships',v_domain,
                            'retirement_id',v_membership.retirement_id,'registry_exists',v_registry_exists,
                            'vram_budget_mb',v_membership.vram_budget_mb,'collection_id',v_collection_id,
                            'evidence_deadline_ms',v_deadline,'evidence_fingerprint',v_fingerprint);
                    END IF;
                ELSE
                    v_now:=pg_catalog.clock_timestamp();
                    v_deadline:=floor(extract(epoch FROM v_observed)*1000)::bigint+180000;
                    v_expected_deadline:=aap_inference_gpu_v1._int(v_proof->'evidence_deadline_ms',1,9223372036854775807);
                    IF v_observed<=v_probe_started OR v_observed>v_now OR v_now-v_observed>interval '3 minutes'
                       OR v_expected_deadline<>v_deadline
                       OR floor(extract(epoch FROM v_now)*1000)::bigint>=v_expected_deadline THEN
                        v_result:=pg_catalog.jsonb_build_object('status','stale','reason','live_proof_expired');
                    ELSIF v_residency->>'state' NOT IN ('unloaded','resident')
                       OR v_residency->'gpu_loaded' IS DISTINCT FROM 'false'::jsonb
                       OR v_residency->'draining' IS DISTINCT FROM 'false'::jsonb
                       OR v_residency->'evictable' IS DISTINCT FROM 'false'::jsonb
                       OR (v_residency->>'active_requests')::bigint<>0
                       OR (v_residency->>'builders')::bigint<>0
                       OR (v_residency->>'borrowers')::bigint<>0
                       OR v_residency->'identity' IS DISTINCT FROM pg_catalog.jsonb_build_object(
                           'audience','aap-gpu-lifecycle',
                           'backend_registry_id',v_id::text,
                           'gpu_resource_id',v_resource
                       )
                       OR (v_residency->>'generation' IS NOT NULL AND (v_residency->>'generation')::bigint>v_fence.generation_high_water)
                       OR (v_residency->>'control_epoch' IS NOT NULL AND (v_residency->>'control_epoch')::bigint>v_fence.control_epoch_high_water)
                       OR EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(pool_id,pool_value) WHERE p.pool_value->'resident' IS DISTINCT FROM 'false'::jsonb) THEN
                        v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','live_proof_not_unloaded');
                    ELSE
                        v_fingerprint:=pg_catalog.encode(public.digest(pg_catalog.jsonb_build_object(
                            'backend_id',v_id,'resource_id',v_resource,'membership_epoch',v_epoch,
                            'retirement_id',v_membership.retirement_id,'challenge',v_proof->>'challenge',
                            'probe_started_at',aap_inference_gpu_v1._iso(v_probe_started),
                            'observed_at',aap_inference_gpu_v1._iso(v_observed),
                            'evidence_deadline_ms',v_expected_deadline,
                            'endpoint_sha256',v_proof->>'endpoint_sha256','residency',v_residency
                        )::text,'sha256'),'hex');
                        IF v_fingerprint IS DISTINCT FROM v_proof->>'evidence_fingerprint' THEN
                            v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','live_proof_fingerprint_mismatch');
                        ELSE
                            v_collection_id:=pg_catalog.encode(public.digest(pg_catalog.convert_to(
                                v_fingerprint||':'||v_id::text||':'||v_epoch::text||':'||v_membership.retirement_id::text,'UTF8'
                            ),'sha256'),'hex');
                            v_result:=pg_catalog.jsonb_build_object(
                                'status','proof_valid','backend_memberships',v_domain,
                                'retirement_id',v_membership.retirement_id,'registry_exists',v_registry_exists,
                                'vram_budget_mb',v_membership.vram_budget_mb,'collection_id',v_collection_id
                            );
                        END IF;
                    END IF;
                END IF;
            END IF;
        END IF;


    ELSIF v_args->>'phase'='commit' THEN
        IF NOT aap_inference_gpu_v1._exact_keys(v_args,ARRAY['phase','backend_id','resource_id','membership_epoch','retirement_id','receipt_fingerprint','registry_exists'])
           OR pg_catalog.jsonb_typeof(v_args->'registry_exists')<>'boolean' THEN
            RAISE EXCEPTION 'invalid bounded GPU collector request' USING ERRCODE='22023';
        END IF;
        v_id:=aap_inference_gpu_v1._uuid(v_args->'backend_id');
        v_resource:=aap_inference_gpu_v1._resource(v_args->'resource_id');
        v_epoch:=aap_inference_gpu_v1._int(v_args->'membership_epoch',1,9223372036854775807);
        v_retirement:=aap_inference_gpu_v1._uuid(v_args->'retirement_id');
        v_receipt_fingerprint:=aap_inference_gpu_v1._string(v_args->'receipt_fingerprint',64);
        IF v_receipt_fingerprint !~ '^[0-9a-f]{64}$' THEN RAISE EXCEPTION 'invalid bounded GPU collector request' USING ERRCODE='22023'; END IF;
        PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('aap:gpu-resource:'||v_resource,0));
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m
         WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        IF NOT FOUND OR v_membership.state<>'retiring' OR v_membership.membership_epoch<>v_epoch OR v_membership.retirement_id<>v_retirement THEN
            v_result:=pg_catalog.jsonb_build_object('status','stale','reason','membership_changed');
        ELSE
            SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
            SELECT EXISTS(SELECT 1 FROM public.ml_backend_registry r WHERE r.id=v_id) INTO v_registry_exists;
            v_now:=pg_catalog.clock_timestamp();
            IF v_fence.backend_registry_id IS NULL OR (v_membership.retired_token_expiry_high_water IS NOT NULL AND v_now<=v_membership.retired_token_expiry_high_water) THEN
                v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','token_horizon_not_elapsed');
            ELSIF v_registry_exists IS DISTINCT FROM (v_args->>'registry_exists')::boolean THEN
                v_result:=pg_catalog.jsonb_build_object('status','stale','reason','registry_presence_changed');
            ELSE
                v_receipt:=pg_catalog.jsonb_build_object(
                    'backend_id',v_id,'resource_id',v_resource,'membership_epoch',v_epoch,
                    'retirement_id',v_retirement,'fingerprint',v_receipt_fingerprint
                );
                PERFORM pg_catalog.set_config('app.gpu_tombstone_gc_receipt',v_receipt::text,true);
                DELETE FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource;
                IF NOT v_registry_exists THEN
                    DELETE FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id;
                END IF;
                v_result:=pg_catalog.jsonb_build_object('status','collected');
            END IF;
        END IF;
    ELSE
        RAISE EXCEPTION 'invalid bounded GPU collector request' USING ERRCODE='22023';
    END IF;

    v_result:=pg_catalog.jsonb_build_object('protocol','aap-inference-gpu-collector-native/v1','action',p_action,'result',v_result);
    IF pg_catalog.octet_length(v_result::text)>4194304 THEN RAISE EXCEPTION 'GPU collector response exceeds bounded size' USING ERRCODE='22023'; END IF;
    RETURN v_result;
END
$function$;
-- aap-statement-break
-- Earlier cancelled/retired installations retain their audit record, not a live GPU claim.
UPDATE public.ml_backend_registry r SET gpu_resource_id=NULL, vram_budget_mb=NULL
WHERE r.extra_params #>> '{plugin,status}'='retired' AND r.gpu_resource_id IS NOT NULL
  AND (aap_inference_gpu_v1.gpu_plugin_retirement_claim_valid(r.id,r.extra_params)
       OR (r.extra_params #> '{plugin_lifecycle,cancelled_prepared}'='true'::jsonb
           AND aap_inference_gpu_v1.gpu_plugin_prepared_cancellation_valid(r.id,
               jsonb_set(r.extra_params,'{plugin,status}','"prepared"'::jsonb))));
