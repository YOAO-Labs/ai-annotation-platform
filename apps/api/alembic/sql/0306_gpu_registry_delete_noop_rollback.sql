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
        DELETE FROM gpu_backend_fences AS fence
        WHERE fence.backend_registry_id = OLD.id
          AND NOT EXISTS (
              SELECT 1
              FROM gpu_backend_memberships AS membership
              WHERE membership.backend_registry_id = OLD.id
          );
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
