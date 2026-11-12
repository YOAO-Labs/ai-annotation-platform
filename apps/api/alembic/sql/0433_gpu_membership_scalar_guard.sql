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
    old_parameters_without_plugin jsonb;
    new_parameters_without_plugin jsonb;
    old_plugin_configuration jsonb;
    new_plugin_configuration jsonb;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        old_parameters_without_plugin := CASE
            WHEN jsonb_typeof(OLD.extra_params) = 'object' THEN OLD.extra_params - 'plugin'
            ELSE OLD.extra_params END;
        new_parameters_without_plugin := CASE
            WHEN jsonb_typeof(NEW.extra_params) = 'object' THEN NEW.extra_params - 'plugin'
            ELSE NEW.extra_params END;
        old_plugin_configuration := CASE
            WHEN jsonb_typeof(OLD.extra_params->'plugin') = 'object'
                THEN (OLD.extra_params->'plugin') - ARRAY['status', 'revision']
            ELSE OLD.extra_params->'plugin' END;
        new_plugin_configuration := CASE
            WHEN jsonb_typeof(NEW.extra_params->'plugin') = 'object'
                THEN (NEW.extra_params->'plugin') - ARRAY['status', 'revision']
            ELSE NEW.extra_params->'plugin' END;
        protected_changed := (
            OLD.gpu_resource_id IS DISTINCT FROM NEW.gpu_resource_id OR
            OLD.vram_budget_mb IS DISTINCT FROM NEW.vram_budget_mb OR
            OLD.eviction_priority IS DISTINCT FROM NEW.eviction_priority OR
            (CASE WHEN jsonb_typeof(old_parameters_without_plugin) = 'object'
                THEN old_parameters_without_plugin - 'plugin_lifecycle'
                ELSE COALESCE(old_parameters_without_plugin, '{}'::jsonb) END)
            IS DISTINCT FROM
            (CASE WHEN jsonb_typeof(new_parameters_without_plugin) = 'object'
                THEN new_parameters_without_plugin - 'plugin_lifecycle'
                ELSE COALESCE(new_parameters_without_plugin, '{}'::jsonb) END) OR
            old_plugin_configuration IS DISTINCT FROM new_plugin_configuration OR
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
            AND old_parameters_without_plugin IS NOT DISTINCT FROM new_parameters_without_plugin
            AND old_plugin_configuration IS NOT DISTINCT FROM new_plugin_configuration
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
