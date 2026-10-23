-- Fixed, caller-transaction Inference health data capability.
CREATE SCHEMA aap_inference_health_data_v1;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_inference_health_data_v1 FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_health_data_v1.health_data_policy()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-inference-health-data-native/v1',
        'data_protocol', 'aap-inference-health-data/v1',
        'transaction', 'caller_owned',
        'functions', pg_catalog.jsonb_build_array(
            'aap_inference_health_data_v1.health_data_policy()',
            'aap_inference_health_data_v1.backend_row(uuid,boolean)',
            'aap_inference_health_data_v1.membership_row(uuid,text,boolean)',
            'aap_inference_health_data_v1.observation_clock()',
            'aap_inference_health_data_v1.lock_resource(text)',
            'aap_inference_health_data_v1.try_promotion()',
            'aap_inference_health_data_v1.write_backend(uuid,text,timestamptz,text,boolean,boolean)',
            'aap_inference_health_data_v1.member_pool(uuid)',
            'aap_inference_health_data_v1.pool_row(uuid,uuid)',
            'aap_inference_health_data_v1.member_row(uuid)',
            'aap_inference_health_data_v1.apply_reconciliation(uuid,uuid,text,text,text,bigint,text,text)'
        ),
        'ready', true
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_health_data_v1.health_data_policy()
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_health_data_v1.backend_row(
    p_registry_id uuid,
    p_for_update boolean
)
RETURNS TABLE (
    id uuid,
    name text,
    url text,
    state text,
    is_interactive boolean,
    auth_method text,
    auth_token text,
    extra_params jsonb,
    gpu_resource_id text,
    vram_budget_mb integer,
    eviction_priority integer,
    health_meta jsonb,
    source text,
    error_message text,
    last_checked_at timestamptz,
    created_at timestamptz,
    updated_at timestamptz
)
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    IF p_registry_id IS NULL OR p_for_update IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Inference health data request';
    END IF;

    IF p_for_update THEN
        RETURN QUERY
        SELECT registry.id,
               registry.name::text,
               registry.url::text,
               registry.state::text,
               registry.is_interactive,
               registry.auth_method::text,
               registry.auth_token::text,
               registry.extra_params,
               registry.gpu_resource_id::text,
               registry.vram_budget_mb,
               registry.eviction_priority,
               registry.health_meta,
               registry.source::text,
               registry.error_message::text,
               registry.last_checked_at,
               registry.created_at,
               registry.updated_at
        FROM public.ml_backend_registry AS registry
        WHERE registry.id = p_registry_id
        FOR UPDATE OF registry;
    ELSE
        RETURN QUERY
        SELECT registry.id,
               registry.name::text,
               registry.url::text,
               registry.state::text,
               registry.is_interactive,
               registry.auth_method::text,
               registry.auth_token::text,
               registry.extra_params,
               registry.gpu_resource_id::text,
               registry.vram_budget_mb,
               registry.eviction_priority,
               registry.health_meta,
               registry.source::text,
               registry.error_message::text,
               registry.last_checked_at,
               registry.created_at,
               registry.updated_at
        FROM public.ml_backend_registry AS registry
        WHERE registry.id = p_registry_id;
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_health_data_v1.backend_row(uuid, boolean)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_health_data_v1.membership_row(
    p_registry_id uuid,
    p_gpu_resource_id text,
    p_for_update boolean
)
RETURNS TABLE (
    membership_epoch bigint,
    state text
)
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    IF p_registry_id IS NULL
       OR p_gpu_resource_id IS NULL
       OR p_for_update IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Inference health data request';
    END IF;

    IF p_for_update THEN
        RETURN QUERY
        SELECT membership.membership_epoch,
               membership.state::text
        FROM public.gpu_backend_memberships AS membership
        WHERE membership.backend_registry_id = p_registry_id
          AND membership.gpu_resource_id = p_gpu_resource_id
          AND membership.state IN ('pending', 'active')
        FOR UPDATE OF membership;
    ELSE
        RETURN QUERY
        SELECT membership.membership_epoch,
               membership.state::text
        FROM public.gpu_backend_memberships AS membership
        WHERE membership.backend_registry_id = p_registry_id
          AND membership.gpu_resource_id = p_gpu_resource_id
          AND membership.state IN ('pending', 'active');
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_health_data_v1.membership_row(uuid, text, boolean)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_health_data_v1.observation_clock()
RETURNS timestamptz
LANGUAGE sql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT pg_catalog.clock_timestamp()
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_health_data_v1.observation_clock()
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_health_data_v1.lock_resource(
    p_gpu_resource_id text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    IF p_gpu_resource_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Inference health data request';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'aap:gpu-resource:' || p_gpu_resource_id,
            0
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_health_data_v1.lock_resource(text)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_health_data_v1.try_promotion()
RETURNS boolean
LANGUAGE sql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended('aap:gpu-membership-promotion', 0)
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_health_data_v1.try_promotion()
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_health_data_v1.write_backend(
    p_registry_id uuid,
    p_state text,
    p_observed_at timestamptz,
    p_health_meta_json text,
    p_is_interactive boolean,
    p_has_interactive boolean
)
RETURNS integer
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
    v_health_meta jsonb;
    v_rows_written integer;
BEGIN
    IF p_registry_id IS NULL
       OR p_state IS NULL
       OR p_state NOT IN ('connected', 'error')
       OR p_observed_at IS NULL
       OR p_has_interactive IS NULL
       OR (p_has_interactive IS TRUE AND p_is_interactive IS NULL) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Inference health data request';
    END IF;

    IF p_health_meta_json IS NULL THEN
        v_health_meta := NULL;
    ELSE
        v_health_meta := p_health_meta_json::jsonb;
        IF pg_catalog.jsonb_typeof(v_health_meta) NOT IN ('object', 'null') THEN
            RAISE EXCEPTION USING
                ERRCODE = '22023',
                MESSAGE = 'invalid Inference health metadata';
        END IF;
    END IF;

    UPDATE public.ml_backend_registry AS registry
    SET state = p_state,
        last_checked_at = p_observed_at,
        health_meta = v_health_meta,
        is_interactive = CASE
            WHEN p_has_interactive THEN p_is_interactive
            ELSE registry.is_interactive
        END,
        updated_at = pg_catalog.now()
    WHERE registry.id = p_registry_id;

    GET DIAGNOSTICS v_rows_written = ROW_COUNT;
    RETURN v_rows_written;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_health_data_v1.write_backend(
        uuid, text, timestamptz, text, boolean, boolean
    )
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_health_data_v1.member_pool(
    p_registry_id uuid
)
RETURNS uuid
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_pool_id uuid;
BEGIN
    IF p_registry_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Inference health data request';
    END IF;

    SELECT member.pool_id
    INTO v_pool_id
    FROM public.ml_backend_pool_members AS member
    WHERE member.registry_id = p_registry_id;

    RETURN v_pool_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_health_data_v1.member_pool(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_health_data_v1.pool_row(
    p_pool_id uuid,
    p_registry_id uuid
)
RETURNS TABLE (
    id uuid,
    name text,
    enabled boolean,
    routing_policy text,
    legacy_instance_id uuid,
    routing_generation bigint,
    capability_fingerprint text,
    capability_snapshot jsonb,
    created_at timestamptz,
    updated_at timestamptz
)
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
    v_registry_id uuid;
BEGIN
    IF p_pool_id IS NULL OR p_registry_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Inference health data request';
    END IF;

    SELECT registry.id
    INTO v_registry_id
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
    FOR KEY SHARE OF registry;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    RETURN QUERY
    SELECT pool.id,
           pool.name::text,
           pool.enabled,
           pool.routing_policy::text,
           pool.legacy_instance_id,
           pool.routing_generation,
           pool.capability_fingerprint::text,
           pool.capability_snapshot,
           pool.created_at,
           pool.updated_at
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = p_pool_id
    FOR UPDATE OF pool;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_health_data_v1.pool_row(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_health_data_v1.member_row(
    p_registry_id uuid
)
RETURNS TABLE (
    id uuid,
    pool_id uuid,
    registry_id uuid,
    traffic_state text,
    weight integer,
    created_at timestamptz,
    updated_at timestamptz
)
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    IF p_registry_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Inference health data request';
    END IF;

    RETURN QUERY
    SELECT member.id,
           member.pool_id,
           member.registry_id,
           member.traffic_state::text,
           member.weight,
           member.created_at,
           member.updated_at
    FROM public.ml_backend_pool_members AS member
    WHERE member.registry_id = p_registry_id
    FOR UPDATE OF member;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_health_data_v1.member_row(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION
    aap_inference_health_data_v1.apply_reconciliation(
        p_registry_id uuid,
        p_pool_id uuid,
        p_operation text,
        p_snapshot_json text,
        p_candidate_fingerprint text,
        p_expected_generation bigint,
        p_expected_pool_fingerprint text,
        p_expected_member_state text
    )
RETURNS TABLE (
    pool_updated_at timestamptz,
    member_updated_at timestamptz
)
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
    v_locked_registry_id uuid;
    v_pool record;
    v_member record;
    v_snapshot jsonb;
    v_pool_updated_at timestamptz;
    v_member_updated_at timestamptz;
BEGIN
    IF p_registry_id IS NULL
       OR p_pool_id IS NULL
       OR p_operation IS NULL
       OR p_operation NOT IN ('seed', 'disable')
       OR p_snapshot_json IS NULL
       OR p_candidate_fingerprint IS NULL
       OR p_candidate_fingerprint !~ '^[0-9a-f]{64}$'
       OR p_expected_generation IS NULL
       OR (
           p_expected_pool_fingerprint IS NOT NULL
           AND p_operation = 'seed'
       )
       OR p_expected_member_state IS NULL
       OR p_expected_member_state NOT IN ('active', 'draining', 'disabled') THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid health reconciliation request';
    END IF;

    IF p_operation = 'disable'
       AND (
           p_expected_pool_fingerprint IS NULL
           OR p_expected_pool_fingerprint = p_candidate_fingerprint
           OR p_expected_member_state = 'disabled'
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid health reconciliation request';
    END IF;

    SELECT registry.id
    INTO v_locked_registry_id
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
    FOR KEY SHARE OF registry;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '40001',
            MESSAGE = 'health reconciliation snapshot changed';
    END IF;

    SELECT pool.*
    INTO v_pool
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = p_pool_id
    FOR UPDATE OF pool;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '40001',
            MESSAGE = 'health reconciliation snapshot changed';
    END IF;

    SELECT member.*
    INTO v_member
    FROM public.ml_backend_pool_members AS member
    WHERE member.registry_id = p_registry_id
    FOR UPDATE OF member;

    IF NOT FOUND
       OR v_member.pool_id IS DISTINCT FROM p_pool_id
       OR v_pool.routing_generation IS DISTINCT FROM p_expected_generation
       OR v_pool.capability_fingerprint IS DISTINCT FROM
           p_expected_pool_fingerprint
       OR v_member.traffic_state IS DISTINCT FROM p_expected_member_state THEN
        RAISE EXCEPTION USING
            ERRCODE = '40001',
            MESSAGE = 'health reconciliation snapshot changed';
    END IF;

    BEGIN
        v_snapshot := p_snapshot_json::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid health reconciliation request';
    END;

    IF pg_catalog.jsonb_typeof(v_snapshot) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid health reconciliation request';
    END IF;

    IF p_operation = 'seed' THEN
        UPDATE public.ml_backend_service_pools AS pool
        SET capability_snapshot = v_snapshot,
            capability_fingerprint = p_candidate_fingerprint,
            routing_generation = pool.routing_generation + 1,
            updated_at = pg_catalog.now()
        WHERE pool.id = p_pool_id
        RETURNING pool.updated_at INTO v_pool_updated_at;
    ELSE
        UPDATE public.ml_backend_pool_members AS member
        SET traffic_state = 'disabled',
            updated_at = pg_catalog.now()
        WHERE member.id = v_member.id
        RETURNING member.updated_at INTO v_member_updated_at;

        UPDATE public.ml_backend_service_pools AS pool
        SET enabled = CASE
                WHEN pool.legacy_instance_id = p_registry_id THEN false
                ELSE pool.enabled
            END,
            routing_generation = pool.routing_generation + 1,
            updated_at = pg_catalog.now()
        WHERE pool.id = p_pool_id
        RETURNING pool.updated_at INTO v_pool_updated_at;
    END IF;

    RETURN QUERY SELECT v_pool_updated_at, v_member_updated_at;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_health_data_v1.apply_reconciliation(
        uuid, uuid, text, text, text, bigint, text, text
    )
    FROM PUBLIC;
-- aap-statement-break
COMMENT ON FUNCTION aap_inference_health_data_v1.apply_reconciliation(
    uuid, uuid, text, text, text, bigint, text, text
)
    IS 'Applies one version-checked pool capability seed or member disable in the caller transaction using Registry, Pool, Member lock order.';
-- aap-statement-break
COMMENT ON FUNCTION aap_inference_health_data_v1.write_backend(
    uuid, text, timestamptz, text, boolean, boolean
)
    IS 'Writes only health-owned Registry status, check time, metadata and optional interactivity in the caller transaction.';
-- aap-statement-break
COMMENT ON FUNCTION aap_inference_health_data_v1.pool_row(uuid, uuid)
    IS 'Locks a registry KEY SHARE before loading the requested Pool FOR UPDATE.';
-- aap-statement-break
COMMENT ON FUNCTION aap_inference_health_data_v1.backend_row(uuid, boolean)
    IS 'Returns the full typed Registry row, optionally under FOR UPDATE.';
