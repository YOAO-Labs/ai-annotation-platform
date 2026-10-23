-- Fixed Native ownership for environment and screenshot-seed Registry ingress.
CREATE SCHEMA aap_inference_registry_ingress_v1;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_inference_registry_ingress_v1 FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_inference_registry_ingress_v1.env_sync_receipts (
    request_id uuid PRIMARY KEY,
    command_sha256 text NOT NULL CHECK (
        command_sha256 COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    observed_urls integer NOT NULL CHECK (observed_urls >= 0),
    created_xid xid8 NOT NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
COMMENT ON TABLE aap_inference_registry_ingress_v1.env_sync_receipts
    IS 'Immutable environment-sync outcome bindings without request bodies, URLs, or credentials.';
-- aap-statement-break
REVOKE ALL ON TABLE aap_inference_registry_ingress_v1.env_sync_receipts
    FROM PUBLIC;
-- aap-statement-break
-- The existing invoker trigger must resolve its own relations under Native GUCs.
ALTER FUNCTION public.sync_gpu_backend_membership()
    SET search_path = pg_catalog, public, pg_temp;
-- aap-statement-break
CREATE OR REPLACE FUNCTION
    aap_inference_registry_ingress_v1.registry_ingress_policy()
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
        'schema', 'aap-inference-registry-ingress-native/v1',
        'data_protocol', 'aap-inference-registry-ingress/v1',
        'transaction', 'caller_owned',
        'functions', pg_catalog.jsonb_build_array(
            'aap_inference_registry_ingress_v1.registry_ingress_policy()',
            'aap_inference_registry_ingress_v1.registry_by_url(text)',
            'aap_inference_registry_ingress_v1.registry_candidates(text)',
            'aap_inference_registry_ingress_v1.create_registry(uuid,text,text,text,boolean,text)',
            'aap_inference_registry_ingress_v1.singleton_pool(uuid)',
            'aap_inference_registry_ingress_v1.create_singleton_pool(uuid,uuid,uuid,text,text,text)',
            'aap_inference_registry_ingress_v1.apply_observation(uuid,text,text,text,boolean,text,timestamptz,text,boolean,text)',
            'aap_inference_registry_ingress_v1.disconnect_env_registry(uuid,text)',
            'aap_inference_registry_ingress_v1.tag_seed_registry(uuid,text)',
            'aap_inference_registry_ingress_v1.sync_result(uuid,text)',
            'aap_inference_registry_ingress_v1.record_sync_result(uuid,text,integer)'
        ),
        'ready', true
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_registry_ingress_v1.registry_ingress_policy()
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION
    aap_inference_registry_ingress_v1.registry_by_url(p_url text)
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
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    IF p_url IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry ingress URL lookup';
    END IF;

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
    WHERE registry.url = p_url;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_registry_ingress_v1.registry_by_url(text)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION
    aap_inference_registry_ingress_v1.registry_candidates(p_profile text)
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
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    IF p_profile IS NULL OR p_profile NOT IN ('env', 'live') THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry ingress candidate profile';
    END IF;

    IF p_profile = 'env' THEN
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
        WHERE registry.source = 'env';
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
        WHERE registry.source <> 'seed';
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_registry_ingress_v1.registry_candidates(text)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION
    aap_inference_registry_ingress_v1.create_registry(
        p_registry_id uuid,
        p_name text,
        p_url text,
        p_source text,
        p_is_interactive boolean,
        p_extra_params_json text
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
DECLARE
    v_created record;
BEGIN
    IF p_registry_id IS NULL
       OR p_source IS NULL
       OR p_source NOT IN ('env', 'seed') THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry ingress create request';
    END IF;

    WITH extra AS MATERIALIZED (
        SELECT p_extra_params_json::jsonb AS value
    )
    INSERT INTO public.ml_backend_registry AS created (
        id,
        name,
        url,
        state,
        is_interactive,
        auth_method,
        auth_token,
        extra_params,
        gpu_resource_id,
        vram_budget_mb,
        eviction_priority,
        health_meta,
        source,
        error_message,
        last_checked_at
    )
    SELECT p_registry_id,
           p_name,
           p_url,
           'disconnected',
           p_is_interactive,
           'none',
           NULL,
           extra.value,
           NULL,
           NULL,
           0,
           NULL,
           p_source,
           NULL,
           NULL
    FROM extra
    WHERE pg_catalog.jsonb_typeof(extra.value) = 'object'
    RETURNING created.id,
              created.name::text,
              created.url::text,
              created.state::text,
              created.is_interactive,
              created.auth_method::text,
              created.auth_token::text,
              created.extra_params,
              created.gpu_resource_id::text,
              created.vram_budget_mb,
              created.eviction_priority,
              created.health_meta,
              created.source::text,
              created.error_message::text,
              created.last_checked_at,
              created.created_at,
              created.updated_at
    INTO v_created;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry ingress create request';
    END IF;

    RETURN QUERY SELECT
        v_created.id, v_created.name, v_created.url, v_created.state,
        v_created.is_interactive, v_created.auth_method, v_created.auth_token,
        v_created.extra_params, v_created.gpu_resource_id, v_created.vram_budget_mb,
        v_created.eviction_priority, v_created.health_meta, v_created.source,
        v_created.error_message, v_created.last_checked_at,
        v_created.created_at, v_created.updated_at;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_registry_ingress_v1.create_registry(
        uuid, text, text, text, boolean, text
    )
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION
    aap_inference_registry_ingress_v1.singleton_pool(p_registry_id uuid)
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
STABLE
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
            MESSAGE = 'invalid Registry ingress singleton lookup';
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
    FROM public.ml_backend_pool_members AS member
    JOIN public.ml_backend_service_pools AS pool
      ON pool.id = member.pool_id
    WHERE member.registry_id = p_registry_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_registry_ingress_v1.singleton_pool(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION
    aap_inference_registry_ingress_v1.create_singleton_pool(
        p_registry_id uuid,
        p_pool_id uuid,
        p_member_id uuid,
        p_name text,
        p_snapshot_json text,
        p_fingerprint text
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
    v_locked_registry_id uuid;
    v_snapshot jsonb;
    v_pool record;
BEGIN
    IF p_registry_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry ingress singleton create';
    END IF;

    SELECT registry.id
    INTO v_locked_registry_id
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
    FOR KEY SHARE OF registry;

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
    FROM public.ml_backend_pool_members AS member
    JOIN public.ml_backend_service_pools AS pool
      ON pool.id = member.pool_id
    WHERE member.registry_id = p_registry_id;

    IF FOUND THEN
        RETURN;
    END IF;

    IF p_pool_id IS NULL OR p_member_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry ingress singleton create';
    END IF;

    IF p_snapshot_json IS NULL THEN
        v_snapshot := NULL;
    ELSE
        v_snapshot := p_snapshot_json::jsonb;
        IF pg_catalog.jsonb_typeof(v_snapshot) NOT IN ('object', 'null') THEN
            RAISE EXCEPTION USING
                ERRCODE = '22023',
                MESSAGE = 'invalid Registry ingress singleton snapshot';
        END IF;
    END IF;

    IF v_snapshot IS NULL OR pg_catalog.jsonb_typeof(v_snapshot) = 'null' THEN
        IF p_fingerprint IS NOT NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '22023',
                MESSAGE = 'invalid Registry ingress singleton fingerprint';
        END IF;
    ELSIF p_fingerprint IS NULL
          OR p_fingerprint COLLATE "C" !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry ingress singleton fingerprint';
    END IF;

    INSERT INTO public.ml_backend_service_pools AS created (
        id,
        name,
        enabled,
        routing_policy,
        legacy_instance_id,
        routing_generation,
        capability_fingerprint,
        capability_snapshot
    )
    VALUES (
        p_pool_id,
        p_name,
        false,
        'smooth_weighted_round_robin',
        p_registry_id,
        1,
        p_fingerprint,
        v_snapshot
    )
    RETURNING created.id,
              created.name::text,
              created.enabled,
              created.routing_policy::text,
              created.legacy_instance_id,
              created.routing_generation,
              created.capability_fingerprint::text,
              created.capability_snapshot,
              created.created_at,
              created.updated_at
    INTO v_pool;

    INSERT INTO public.ml_backend_pool_members (
        id,
        pool_id,
        registry_id,
        traffic_state,
        weight
    )
    VALUES (
        p_member_id,
        p_pool_id,
        p_registry_id,
        'active',
        1
    );

    RETURN QUERY SELECT
        v_pool.id, v_pool.name, v_pool.enabled, v_pool.routing_policy,
        v_pool.legacy_instance_id, v_pool.routing_generation,
        v_pool.capability_fingerprint, v_pool.capability_snapshot,
        v_pool.created_at, v_pool.updated_at;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_registry_ingress_v1.create_singleton_pool(
        uuid, uuid, uuid, text, text, text
    )
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION
    aap_inference_registry_ingress_v1.apply_observation(
        p_registry_id uuid,
        p_profile text,
        p_state text,
        p_name text,
        p_is_interactive boolean,
        p_health_meta_json text,
        p_checked_at timestamptz,
        p_error_message text,
        p_usable_caps boolean,
        p_expected_snapshot_json text
    )
RETURNS timestamptz
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
    v_registry record;
    v_expected_snapshot jsonb;
    v_actual_snapshot jsonb;
    v_updated_at timestamptz;
BEGIN
    IF p_registry_id IS NULL
       OR p_profile IS NULL
       OR p_profile NOT IN ('env', 'seed')
       OR p_state IS NULL
       OR p_state NOT IN ('connected', 'error')
       OR p_checked_at IS NULL
       OR p_usable_caps IS NULL
       OR (
           p_usable_caps IS TRUE
           AND p_state <> 'connected'
       )
       OR (
           p_usable_caps IS FALSE
           AND p_state <> 'error'
       )
       OR p_expected_snapshot_json IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry ingress observation';
    END IF;

    SELECT registry.*
    INTO v_registry
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
    FOR UPDATE OF registry;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '40001',
            MESSAGE = 'registry ingress observation changed';
    END IF;

    BEGIN
        v_expected_snapshot := p_expected_snapshot_json::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry ingress observation snapshot';
    END;

    IF pg_catalog.jsonb_typeof(v_expected_snapshot) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry ingress observation snapshot';
    END IF;

    IF (
        SELECT pg_catalog.count(*)
        FROM pg_catalog.jsonb_object_keys(v_expected_snapshot)
    ) <> 5
       OR NOT (v_expected_snapshot ?& ARRAY[
           'url',
           'auth_method',
           'auth_token',
           'gpu_resource_id',
           'source'
       ])
       OR pg_catalog.jsonb_typeof(v_expected_snapshot -> 'url')
           IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_expected_snapshot -> 'auth_method')
           NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(v_expected_snapshot -> 'auth_token')
           NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(v_expected_snapshot -> 'gpu_resource_id')
           NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(v_expected_snapshot -> 'source')
           IS DISTINCT FROM 'string'
       OR (p_profile = 'env' AND
           v_expected_snapshot ->> 'source' IS DISTINCT FROM 'env') THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry ingress observation snapshot';
    END IF;

    v_actual_snapshot := pg_catalog.jsonb_build_object(
        'url', v_registry.url,
        'auth_method', v_registry.auth_method,
        'auth_token', v_registry.auth_token,
        'gpu_resource_id', v_registry.gpu_resource_id,
        'source', v_registry.source
    );

    IF v_actual_snapshot IS DISTINCT FROM v_expected_snapshot THEN
        RAISE EXCEPTION USING
            ERRCODE = '40001',
            MESSAGE = 'registry ingress observation changed';
    END IF;

    IF p_profile = 'env' THEN
        UPDATE public.ml_backend_registry AS registry
        SET name = p_name,
            state = p_state,
            is_interactive = p_is_interactive,
            health_meta = CASE
                WHEN p_health_meta_json IS NULL THEN NULL
                ELSE p_health_meta_json::jsonb
            END,
            last_checked_at = p_checked_at,
            updated_at = pg_catalog.now()
        WHERE registry.id = p_registry_id
        RETURNING registry.updated_at INTO v_updated_at;
    ELSIF p_usable_caps THEN
        UPDATE public.ml_backend_registry AS registry
        SET name = p_name,
            state = p_state,
            is_interactive = p_is_interactive,
            health_meta = CASE
                WHEN p_health_meta_json IS NULL THEN NULL
                ELSE p_health_meta_json::jsonb
            END,
            last_checked_at = p_checked_at,
            error_message = NULL,
            updated_at = pg_catalog.now()
        WHERE registry.id = p_registry_id
        RETURNING registry.updated_at INTO v_updated_at;
    ELSE
        UPDATE public.ml_backend_registry AS registry
        SET state = p_state,
            last_checked_at = p_checked_at,
            error_message = p_error_message,
            updated_at = pg_catalog.now()
        WHERE registry.id = p_registry_id
        RETURNING registry.updated_at INTO v_updated_at;
    END IF;

    RETURN v_updated_at;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_registry_ingress_v1.apply_observation(
        uuid, text, text, text, boolean, text, timestamptz, text, boolean, text
    )
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION
    aap_inference_registry_ingress_v1.disconnect_env_registry(
        p_registry_id uuid,
        p_expected_url text
    )
RETURNS timestamptz
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
    v_registry record;
    v_updated_at timestamptz;
BEGIN
    IF p_registry_id IS NULL OR p_expected_url IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry ingress disconnect';
    END IF;

    SELECT registry.id,
           registry.url,
           registry.source,
           registry.state,
           registry.updated_at
    INTO v_registry
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
    FOR UPDATE OF registry;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '40001',
            MESSAGE = 'registry ingress environment row changed';
    END IF;

    IF v_registry.source IS DISTINCT FROM 'env'
       OR v_registry.url IS DISTINCT FROM p_expected_url THEN
        RAISE EXCEPTION USING
            ERRCODE = '40001',
            MESSAGE = 'registry ingress environment row changed';
    END IF;

    IF v_registry.state IS DISTINCT FROM 'disconnected' THEN
        UPDATE public.ml_backend_registry AS registry
        SET state = 'disconnected',
            updated_at = pg_catalog.now()
        WHERE registry.id = p_registry_id
        RETURNING registry.updated_at INTO v_updated_at;
    ELSE
        v_updated_at := v_registry.updated_at;
    END IF;

    RETURN v_updated_at;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_registry_ingress_v1.disconnect_env_registry(uuid, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION
    aap_inference_registry_ingress_v1.tag_seed_registry(
        p_registry_id uuid,
        p_marker_json text
    )
RETURNS TABLE (
    extra_params jsonb,
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
    v_marker jsonb;
    v_current jsonb;
    v_extra jsonb;
    v_merged jsonb;
    v_current_updated_at timestamptz;
    v_updated_at timestamptz;
BEGIN
    IF p_registry_id IS NULL OR p_marker_json IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry seed marker';
    END IF;

    BEGIN
        v_marker := p_marker_json::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry seed marker';
    END;

    IF pg_catalog.jsonb_typeof(v_marker) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry seed marker';
    END IF;

    IF (
        SELECT pg_catalog.count(*)
        FROM pg_catalog.jsonb_object_keys(v_marker)
    ) <> 3
       OR NOT (v_marker ?& ARRAY['managed_by', 'profile', 'revision'])
       OR v_marker ->> 'managed_by' IS DISTINCT FROM 'screenshot-seed'
       OR v_marker ->> 'profile' IS DISTINCT FROM 'screenshots'
       OR v_marker ->> 'revision' IS DISTINCT FROM 'screenshots-2026-08-g' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry seed marker';
    END IF;

    SELECT registry.extra_params,
           registry.updated_at
    INTO v_current, v_current_updated_at
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
    FOR UPDATE OF registry;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '40001',
            MESSAGE = 'registry ingress seed row changed';
    END IF;

    IF v_current IS NULL
       OR v_current = 'null'::jsonb
       OR v_current = 'false'::jsonb
       OR v_current = '0'::jsonb
       OR v_current = '""'::jsonb
       OR v_current = '[]'::jsonb THEN
        v_extra := '{}'::jsonb;
    ELSIF pg_catalog.jsonb_typeof(v_current) = 'object' THEN
        v_extra := v_current;
    ELSE
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry seed extra parameters';
    END IF;

    v_merged := v_extra || pg_catalog.jsonb_build_object('seed', v_marker);
    IF v_merged IS DISTINCT FROM v_current THEN
        UPDATE public.ml_backend_registry AS registry
        SET extra_params = v_merged,
            updated_at = pg_catalog.now()
        WHERE registry.id = p_registry_id
        RETURNING registry.updated_at INTO v_updated_at;
        extra_params := v_merged;
        updated_at := v_updated_at;
    ELSE
        extra_params := v_current;
        updated_at := v_current_updated_at;
    END IF;

    RETURN NEXT;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_registry_ingress_v1.tag_seed_registry(uuid, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION
    aap_inference_registry_ingress_v1.sync_result(
        p_request_id uuid,
        p_command_sha256 text
    )
RETURNS jsonb
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
    v_receipt record;
BEGIN
    IF p_request_id IS NULL
       OR p_command_sha256 IS NULL
       OR p_command_sha256 COLLATE "C" !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry environment sync receipt lookup';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('aap:registry-ingress-env-sync', 0)
    );

    SELECT receipt.command_sha256,
           receipt.observed_urls,
           receipt.created_xid
    INTO v_receipt
    FROM aap_inference_registry_ingress_v1.env_sync_receipts AS receipt
    WHERE receipt.request_id = p_request_id;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    IF v_receipt.command_sha256 IS DISTINCT FROM p_command_sha256 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'Registry environment sync request identity reused';
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-inference-registry-env-sync-result/v1',
        'request_id', p_request_id,
        'command_sha256', v_receipt.command_sha256,
        'state', CASE
            WHEN v_receipt.created_xid IS NOT DISTINCT FROM
                pg_catalog.pg_current_xact_id_if_assigned()
                THEN 'caller_pending'
            ELSE 'committed'
        END,
        'observed_urls', v_receipt.observed_urls
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_registry_ingress_v1.sync_result(uuid, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION
    aap_inference_registry_ingress_v1.record_sync_result(
        p_request_id uuid,
        p_command_sha256 text,
        p_observed_urls integer
    )
RETURNS jsonb
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
    v_receipt record;
    v_created_xid xid8;
BEGIN
    IF p_request_id IS NULL
       OR p_command_sha256 IS NULL
       OR p_command_sha256 COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR p_observed_urls IS NULL
       OR p_observed_urls < 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Registry environment sync receipt';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('aap:registry-ingress-env-sync', 0)
    );

    SELECT receipt.command_sha256,
           receipt.observed_urls,
           receipt.created_xid
    INTO v_receipt
    FROM aap_inference_registry_ingress_v1.env_sync_receipts AS receipt
    WHERE receipt.request_id = p_request_id;

    IF FOUND THEN
        IF v_receipt.command_sha256 IS DISTINCT FROM p_command_sha256
           OR v_receipt.observed_urls IS DISTINCT FROM p_observed_urls THEN
            RAISE EXCEPTION USING
                ERRCODE = '22023',
                MESSAGE = 'Registry environment sync request identity reused';
        END IF;

        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-inference-registry-env-sync-result/v1',
            'request_id', p_request_id,
            'command_sha256', v_receipt.command_sha256,
            'state', CASE
                WHEN v_receipt.created_xid IS NOT DISTINCT FROM
                    pg_catalog.pg_current_xact_id_if_assigned()
                    THEN 'caller_pending'
                ELSE 'committed'
            END,
            'observed_urls', v_receipt.observed_urls
        );
    END IF;

    INSERT INTO aap_inference_registry_ingress_v1.env_sync_receipts (
        request_id,
        command_sha256,
        observed_urls,
        created_xid
    )
    VALUES (
        p_request_id,
        p_command_sha256,
        p_observed_urls,
        pg_catalog.pg_current_xact_id()
    )
    RETURNING created_xid INTO v_created_xid;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-inference-registry-env-sync-result/v1',
        'request_id', p_request_id,
        'command_sha256', p_command_sha256,
        'state', CASE
            WHEN v_created_xid IS NOT DISTINCT FROM
                pg_catalog.pg_current_xact_id_if_assigned()
                THEN 'caller_pending'
            ELSE 'committed'
        END,
        'observed_urls', p_observed_urls
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_registry_ingress_v1.record_sync_result(uuid, text, integer)
    FROM PUBLIC;
-- aap-statement-break
