DROP TRIGGER trg_sync_gpu_backend_membership_update ON public.ml_backend_registry;
-- aap-statement-break
CREATE TRIGGER trg_sync_gpu_backend_membership_update
AFTER UPDATE OF gpu_resource_id, vram_budget_mb, eviction_priority,
                extra_params, url ON public.ml_backend_registry
FOR EACH ROW
WHEN (OLD.gpu_resource_id IS DISTINCT FROM NEW.gpu_resource_id
   OR OLD.vram_budget_mb IS DISTINCT FROM NEW.vram_budget_mb
   OR OLD.eviction_priority IS DISTINCT FROM NEW.eviction_priority
   OR OLD.extra_params IS DISTINCT FROM NEW.extra_params
   OR OLD.url IS DISTINCT FROM NEW.url)
EXECUTE FUNCTION public.sync_gpu_backend_membership();
-- aap-statement-break
CREATE OR REPLACE FUNCTION public.sync_gpu_backend_membership()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, public, pg_temp
AS $$
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
            OLD.extra_params IS DISTINCT FROM NEW.extra_params OR
            OLD.url IS DISTINCT FROM NEW.url OR
            OLD.auth_method IS DISTINCT FROM NEW.auth_method OR
            OLD.auth_token IS DISTINCT FROM NEW.auth_token
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
        IF runtime_epoch > 0 THEN
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
$$;

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
DROP FUNCTION aap_inference_gpu_v1.gpu_plugin_terminal_proof_valid(uuid,jsonb);
