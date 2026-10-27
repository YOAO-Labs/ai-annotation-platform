-- Reconcile authentication rotation with routing eligibility revisions.
--
-- `0190_ml_backend_auth_rotation` installs `trg_rotate_ml_backend_authentication`
-- (BEFORE UPDATE), and `0279_inference_routing` installs
-- `aap_route_registry_before_change` (BEFORE UPDATE). PostgreSQL fires same-timing
-- triggers in name order, so the routing hook runs BEFORE the rotation function
-- rewrites NEW.state to 'disconnected' and clears the observation fields. The
-- routing eligibility revision therefore misses the disconnect and stale
-- admission snapshots can still reserve a disconnected backend.
--
-- The rotation trigger keeps its own semantics; this replacement predicts its
-- effect (auth_method/auth_token change => disconnected + cleared observation)
-- when computing the eligibility delta. Trigger names and ordering stay fixed.
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_registry_before_change()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_pool_id uuid;
    v_config_changed boolean;
    v_eligibility_changed boolean;
    v_auth_rotation boolean;
    v_effective_state text;
    v_effective_health_meta jsonb;
    v_effective_last_checked_at timestamp with time zone;
    v_effective_error_message text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        FOR v_pool_id IN
            SELECT DISTINCT member.pool_id
            FROM public.ml_backend_pool_members AS member
            WHERE member.registry_id = OLD.id
            ORDER BY member.pool_id
        LOOP
            BEGIN
                PERFORM pool.id
                FROM public.ml_backend_service_pools AS pool
                WHERE pool.id = v_pool_id
                FOR UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55P03',
                    MESSAGE = 'routing management is busy',
                    HINT = 'aap.inference.routing.management_busy';
            END;
        END LOOP;
        IF aap_inference_routing_v1._routing_has_unresolved(OLD.id)
           OR aap_inference_routing_v1._routing_has_control(OLD.id) THEN
            RAISE EXCEPTION USING
                ERRCODE = '55P03',
                MESSAGE = 'routing management is busy',
                HINT = 'aap.inference.routing.management_busy';
        END IF;
        RETURN OLD;
    END IF;

    v_config_changed := ROW(
        OLD.url, OLD.auth_method, OLD.auth_token, OLD.extra_params,
        OLD.gpu_resource_id, OLD.vram_budget_mb, OLD.eviction_priority
    ) IS DISTINCT FROM ROW(
        NEW.url, NEW.auth_method, NEW.auth_token, NEW.extra_params,
        NEW.gpu_resource_id, NEW.vram_budget_mb, NEW.eviction_priority
    );
    -- `trg_rotate_ml_backend_authentication` runs after this hook and rewrites
    -- state/observations for a credential change; use that effective row so the
    -- eligibility revision advances exactly once for the disconnect.
    v_auth_rotation := OLD.auth_method IS DISTINCT FROM NEW.auth_method
        OR OLD.auth_token IS DISTINCT FROM NEW.auth_token;
    v_effective_state := CASE
        WHEN v_auth_rotation THEN 'disconnected' ELSE NEW.state END;
    v_effective_health_meta := CASE
        WHEN v_auth_rotation THEN NULL ELSE NEW.health_meta END;
    v_effective_last_checked_at := CASE
        WHEN v_auth_rotation THEN NULL ELSE NEW.last_checked_at END;
    v_effective_error_message := CASE
        WHEN v_auth_rotation THEN NULL ELSE NEW.error_message END;
    v_eligibility_changed := ROW(
        OLD.state, OLD.is_interactive, OLD.health_meta, OLD.last_checked_at,
        OLD.error_message
    ) IS DISTINCT FROM ROW(
        v_effective_state, NEW.is_interactive, v_effective_health_meta,
        v_effective_last_checked_at, v_effective_error_message
    );

    IF v_config_changed THEN
        FOR v_pool_id IN
            SELECT DISTINCT member.pool_id
            FROM public.ml_backend_pool_members AS member
            WHERE member.registry_id = OLD.id
            ORDER BY member.pool_id
        LOOP
            BEGIN
                PERFORM pool.id
                FROM public.ml_backend_service_pools AS pool
                WHERE pool.id = v_pool_id
                FOR UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55P03',
                    MESSAGE = 'routing management is busy',
                    HINT = 'aap.inference.routing.management_busy';
            END;
        END LOOP;
        IF aap_inference_routing_v1._routing_has_unresolved(OLD.id)
           OR aap_inference_routing_v1._routing_has_control(OLD.id) THEN
            RAISE EXCEPTION USING
                ERRCODE = '55P03',
                MESSAGE = 'routing management is busy',
                HINT = 'aap.inference.routing.management_busy';
        END IF;
    END IF;

    INSERT INTO aap_inference_routing_v1.registry_state (
        registry_id, config_revision, eligibility_revision, authority_epoch,
        backend_incarnation, continuity, epoch_key, realm
    ) VALUES (
        OLD.id, 1, 1, public.gen_random_uuid(), NULL, 'unverified',
        public.gen_random_bytes(32), aap_inference_routing_v1._routing_realm()
    ) ON CONFLICT (registry_id) DO NOTHING;

    IF v_config_changed OR v_eligibility_changed THEN
        UPDATE aap_inference_routing_v1.registry_state AS state
        SET config_revision = state.config_revision + CASE WHEN v_config_changed THEN 1 ELSE 0 END,
            eligibility_revision = state.eligibility_revision + CASE WHEN v_eligibility_changed THEN 1 ELSE 0 END,
            updated_at = pg_catalog.now()
        WHERE state.registry_id = OLD.id;
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1._routing_registry_before_change()
    FROM PUBLIC;
