-- Fixed, caller-transaction topology reads and writes. No function returns auth_token.
CREATE SCHEMA aap_inference_topology_v1;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_inference_topology_v1 FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1._registry_json(p_registry_id uuid)
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
        'id', registry.id,
        'name', registry.name,
        'url', registry.url,
        'state', registry.state,
        'is_interactive', registry.is_interactive,
        'auth_method', registry.auth_method,
        'credential_configured', registry.auth_token IS NOT NULL AND registry.auth_token <> '',
        'extra_params', registry.extra_params,
        'gpu_resource_id', registry.gpu_resource_id,
        'vram_budget_mb', registry.vram_budget_mb,
        'eviction_priority', registry.eviction_priority,
        'health_meta', registry.health_meta,
        'source', registry.source,
        'error_message', registry.error_message,
        'last_checked_at', registry.last_checked_at,
        'created_at', registry.created_at,
        'updated_at', registry.updated_at
    )
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1._registry_json(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1._pool_json(p_pool_id uuid)
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
        'id', pool.id,
        'name', pool.name,
        'enabled', pool.enabled,
        'routing_policy', pool.routing_policy,
        'legacy_instance_id', pool.legacy_instance_id,
        'routing_generation', pool.routing_generation,
        'capability_fingerprint', pool.capability_fingerprint,
        'capability_snapshot', pool.capability_snapshot,
        'created_at', pool.created_at,
        'updated_at', pool.updated_at
    )
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = p_pool_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1._pool_json(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1._association_json(p_assoc_id uuid)
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
        'id', association.id,
        'project_id', association.project_id,
        'pool_id', association.pool_id,
        'enabled', association.enabled,
        'default_variants', association.default_variants,
        'created_at', association.created_at,
        'updated_at', association.updated_at
    )
    FROM public.project_ml_backend_pool AS association
    WHERE association.id = p_assoc_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1._association_json(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1._member_json(p_member_id uuid)
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
        'id', member.id,
        'pool_id', member.pool_id,
        'registry_id', member.registry_id,
        'traffic_state', member.traffic_state,
        'weight', member.weight,
        'created_at', member.created_at,
        'updated_at', member.updated_at,
        'registry', aap_inference_topology_v1._registry_json(member.registry_id)
    )
    FROM public.ml_backend_pool_members AS member
    WHERE member.id = p_member_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1._member_json(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1._project_backend_json(
    p_project_id uuid,
    p_registry_id uuid
)
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
        'project_id', p_project_id,
        'registry', aap_inference_topology_v1._registry_json(registry.id),
        'pool_id', member.pool_id,
        'pool_legacy_instance_id', pool.legacy_instance_id,
        'member_id', member.id,
        'traffic_state', member.traffic_state,
        'weight', member.weight,
        'association', aap_inference_topology_v1._association_json(association.id)
    )
    FROM public.ml_backend_registry AS registry
    LEFT JOIN public.ml_backend_pool_members AS member
        ON member.registry_id = registry.id
    LEFT JOIN public.ml_backend_service_pools AS pool
        ON pool.id = member.pool_id
    LEFT JOIN public.project_ml_backend_pool AS association
        ON association.project_id = p_project_id
       AND association.pool_id = member.pool_id
    WHERE registry.id = p_registry_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_topology_v1._project_backend_json(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1._project_pool_json(
    p_project_id uuid,
    p_pool_id uuid
)
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
        'project_id', p_project_id,
        'pool', aap_inference_topology_v1._pool_json(pool.id),
        'member_count', (
            SELECT pg_catalog.count(*)::integer
            FROM public.ml_backend_pool_members AS member
            WHERE member.pool_id = pool.id
        ),
        'association', aap_inference_topology_v1._association_json(association.id)
    )
    FROM public.ml_backend_service_pools AS pool
    LEFT JOIN public.project_ml_backend_pool AS association
        ON association.project_id = p_project_id
       AND association.pool_id = pool.id
    WHERE pool.id = p_pool_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_topology_v1._project_pool_json(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.topology_policy()
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
        'schema', 'aap-inference-topology-native/v1',
        'data_protocol', 'aap-inference-topology/v1',
        'transaction', 'caller_owned',
        'functions', pg_catalog.jsonb_build_array(
            'aap_inference_topology_v1.topology_policy()',
            'aap_inference_topology_v1.registry_facts(uuid)',
            'aap_inference_topology_v1.registry_url_owner(text)',
            'aap_inference_topology_v1.registry_catalog()',
            'aap_inference_topology_v1.registry_names(uuid[])',
            'aap_inference_topology_v1.registry_reference(uuid)',
            'aap_inference_topology_v1.pool_reference(uuid)',
            'aap_inference_topology_v1.pool_for_registry(uuid)',
            'aap_inference_topology_v1.pool_catalog()',
            'aap_inference_topology_v1.pool_members(uuid)',
            'aap_inference_topology_v1.project_backend_catalog(uuid)',
            'aap_inference_topology_v1.enabled_project_ids(uuid[])',
            'aap_inference_topology_v1.project_backend_facts(uuid[])',
            'aap_inference_topology_v1.project_pool_catalog(uuid)',
            'aap_inference_topology_v1.project_assoc_for_registry(uuid,uuid)',
            'aap_inference_topology_v1.project_interactive_backend(uuid)',
            'aap_inference_topology_v1.create_manual_registry(uuid,text)',
            'aap_inference_topology_v1.singleton_pool(uuid)',
            'aap_inference_topology_v1.create_singleton_pool(uuid,uuid,text,text,text)',
            'aap_inference_topology_v1.create_singleton_member(uuid,uuid,uuid)',
            'aap_inference_topology_v1.patch_registry(uuid,text)',
            'aap_inference_topology_v1.validate_registry_delete(uuid)',
            'aap_inference_topology_v1.delete_registry(uuid,text)',
            'aap_inference_topology_v1.create_pool(uuid,text)',
            'aap_inference_topology_v1.patch_pool(uuid,text)',
            'aap_inference_topology_v1.lock_empty_pool(uuid)',
            'aap_inference_topology_v1.delete_empty_pool(uuid)',
            'aap_inference_topology_v1.lock_member_upsert_rows(uuid,uuid)',
            'aap_inference_topology_v1.upsert_member(uuid,uuid,integer,text)',
            'aap_inference_topology_v1.remove_member(uuid,uuid,text)',
            'aap_inference_topology_v1.drain_member(uuid,uuid)',
            'aap_inference_topology_v1.resume_member(uuid,uuid)',
            'aap_inference_topology_v1.drift_review_rows(uuid,uuid)',
            'aap_inference_topology_v1.lock_drift_acceptance_rows(uuid,uuid)',
            'aap_inference_topology_v1.accept_drift(uuid,uuid,text,boolean,text)',
            'aap_inference_topology_v1.set_project_registry_enablement(uuid,uuid,boolean,text)',
            'aap_inference_topology_v1.set_project_pool_enablement(uuid,uuid,boolean,text)',
            'aap_inference_topology_v1.delete_project_associations(uuid)'
        ),
        'ready', true
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.topology_policy() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.registry_facts(p_registry_id uuid)
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
    SELECT aap_inference_topology_v1._registry_json(p_registry_id)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.registry_facts(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.registry_url_owner(p_url text)
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
    SELECT pg_catalog.jsonb_build_object('id', registry.id, 'name', registry.name)
    FROM public.ml_backend_registry AS registry
    WHERE registry.url = p_url
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.registry_url_owner(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.registry_catalog()
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
    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            aap_inference_topology_v1._registry_json(registry.id)
            ORDER BY registry.created_at DESC
        ),
        '[]'::jsonb
    )
    FROM public.ml_backend_registry AS registry
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.registry_catalog() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.registry_names(p_registry_ids uuid[])
RETURNS jsonb
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
    IF p_registry_ids IS NULL OR pg_catalog.cardinality(p_registry_ids) > 500 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid topology name batch';
    END IF;
    RETURN COALESCE(
        (
            SELECT pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object('id', registry.id, 'name', registry.name)
            )
            FROM public.ml_backend_registry AS registry
            WHERE registry.id = ANY(p_registry_ids)
        ),
        '[]'::jsonb
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.registry_names(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.registry_reference(p_registry_id uuid)
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
    SELECT pg_catalog.jsonb_build_object('id', registry.id)
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.registry_reference(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.pool_reference(p_pool_id uuid)
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
    SELECT aap_inference_topology_v1._pool_json(p_pool_id)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.pool_reference(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.pool_for_registry(p_registry_id uuid)
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
    SELECT aap_inference_topology_v1._pool_json(member.pool_id)
    FROM public.ml_backend_pool_members AS member
    WHERE member.registry_id = p_registry_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.pool_for_registry(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.pool_catalog()
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
    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            aap_inference_topology_v1._pool_json(pool.id)
            ORDER BY pool.created_at DESC
        ),
        '[]'::jsonb
    )
    FROM public.ml_backend_service_pools AS pool
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.pool_catalog() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.pool_members(p_pool_id uuid)
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
    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            aap_inference_topology_v1._member_json(member.id)
            ORDER BY member.id
        ),
        '[]'::jsonb
    )
    FROM public.ml_backend_pool_members AS member
    WHERE member.pool_id = p_pool_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.pool_members(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.project_backend_catalog(p_project_id uuid)
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
    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            aap_inference_topology_v1._project_backend_json(p_project_id, registry.id)
            ORDER BY registry.created_at DESC
        ),
        '[]'::jsonb
    )
    FROM public.ml_backend_registry AS registry
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_topology_v1.project_backend_catalog(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.project_backend_facts(p_project_ids uuid[])
RETURNS jsonb
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
    IF p_project_ids IS NULL OR pg_catalog.cardinality(p_project_ids) > 500 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid topology project batch';
    END IF;
    RETURN COALESCE(
        (
            SELECT pg_catalog.jsonb_agg(
                aap_inference_topology_v1._project_backend_json(
                    association.project_id,
                    member.registry_id
                )
            )
            FROM public.project_ml_backend_pool AS association
            JOIN public.ml_backend_pool_members AS member
              ON member.pool_id = association.pool_id
            WHERE association.project_id = ANY(p_project_ids)
              AND association.enabled
        ),
        '[]'::jsonb
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.project_backend_facts(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.enabled_project_ids(p_project_ids uuid[])
RETURNS jsonb
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
    IF p_project_ids IS NULL OR pg_catalog.cardinality(p_project_ids) > 500 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid topology project batch';
    END IF;
    RETURN COALESCE(
        (
            SELECT pg_catalog.jsonb_agg(enabled.project_id ORDER BY enabled.project_id)
            FROM (
                SELECT DISTINCT association.project_id
                FROM public.project_ml_backend_pool AS association
                WHERE association.project_id = ANY(p_project_ids)
                  AND association.enabled
            ) AS enabled
        ),
        '[]'::jsonb
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.enabled_project_ids(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.project_pool_catalog(p_project_id uuid)
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
    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            aap_inference_topology_v1._project_pool_json(p_project_id, pool.id)
            ORDER BY pool.created_at DESC
        ),
        '[]'::jsonb
    )
    FROM public.ml_backend_service_pools AS pool
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_topology_v1.project_pool_catalog(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.project_assoc_for_registry(
    p_project_id uuid,
    p_registry_id uuid
)
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
    SELECT aap_inference_topology_v1._association_json(association.id)
    FROM public.ml_backend_pool_members AS member
    JOIN public.project_ml_backend_pool AS association
      ON association.pool_id = member.pool_id
     AND association.project_id = p_project_id
    WHERE member.registry_id = p_registry_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_topology_v1.project_assoc_for_registry(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.project_interactive_backend(p_project_id uuid)
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
    SELECT aap_inference_topology_v1._registry_json(registry.id)
    FROM public.ml_backend_registry AS registry
    JOIN public.ml_backend_pool_members AS member
      ON member.registry_id = registry.id
    JOIN public.project_ml_backend_pool AS association
      ON association.pool_id = member.pool_id
    WHERE association.project_id = p_project_id
      AND association.enabled
      AND registry.is_interactive
      AND registry.state = 'connected'
    LIMIT 1
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_topology_v1.project_interactive_backend(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.create_manual_registry(
    p_registry_id uuid,
    p_values text
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
    v_values jsonb;
    v_owner text;
BEGIN
    IF p_registry_id IS NULL OR p_values IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid manual Registry create';
    END IF;
    v_values := p_values::jsonb;
    IF pg_catalog.jsonb_typeof(v_values) <> 'object'
       OR v_values - ARRAY[
            'name', 'url', 'source', 'is_interactive', 'auth_method', 'auth_token',
            'extra_params', 'gpu_resource_id', 'vram_budget_mb', 'eviction_priority'
       ] <> '{}'::jsonb
       OR v_values->>'source' IS DISTINCT FROM 'manual'
       OR v_values->>'name' IS NULL
       OR v_values->>'url' IS NULL
       OR v_values->>'url' = '' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid manual Registry fields';
    END IF;
    SELECT registry.name INTO v_owner
    FROM public.ml_backend_registry AS registry
    WHERE registry.url = v_values->>'url';
    IF FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0001',
            MESSAGE = 'aap_inference_topology:registry_url_conflict:' || v_owner || ':';
    END IF;
    BEGIN
        INSERT INTO public.ml_backend_registry (
            id, name, url, state, is_interactive, auth_method, auth_token,
            extra_params, gpu_resource_id, vram_budget_mb, eviction_priority,
            health_meta, source, error_message, last_checked_at
        ) VALUES (
            p_registry_id,
            v_values->>'name',
            v_values->>'url',
            'disconnected',
            CASE WHEN v_values ? 'is_interactive'
                 THEN (v_values->>'is_interactive')::boolean ELSE false END,
            CASE WHEN v_values ? 'auth_method'
                 THEN v_values->>'auth_method' ELSE 'none' END,
            CASE WHEN v_values ? 'auth_token'
                 THEN v_values->>'auth_token' ELSE NULL END,
            CASE WHEN v_values ? 'extra_params'
                 THEN v_values->'extra_params' ELSE '{}'::jsonb END,
            CASE WHEN v_values ? 'gpu_resource_id'
                 THEN v_values->>'gpu_resource_id' ELSE NULL END,
            CASE WHEN v_values ? 'vram_budget_mb'
                 THEN (v_values->>'vram_budget_mb')::integer ELSE NULL END,
            CASE WHEN v_values ? 'eviction_priority'
                 THEN (v_values->>'eviction_priority')::integer ELSE 0 END,
            NULL,
            'manual',
            NULL,
            NULL
        );
    EXCEPTION WHEN unique_violation THEN
        SELECT registry.name INTO v_owner
        FROM public.ml_backend_registry AS registry
        WHERE registry.url = v_values->>'url';
        IF FOUND THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P0001',
                MESSAGE = 'aap_inference_topology:registry_url_conflict:' || v_owner || ':';
        END IF;
        RAISE;
    END;
    RETURN aap_inference_topology_v1._registry_json(p_registry_id);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_topology_v1.create_manual_registry(uuid, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.singleton_pool(p_registry_id uuid)
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
    SELECT aap_inference_topology_v1._pool_json(member.pool_id)
    FROM public.ml_backend_pool_members AS member
    WHERE member.registry_id = p_registry_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.singleton_pool(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.create_singleton_pool(
    p_registry_id uuid,
    p_pool_id uuid,
    p_name text,
    p_snapshot text,
    p_fingerprint text
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
    v_existing uuid;
BEGIN
    SELECT member.pool_id INTO v_existing
    FROM public.ml_backend_pool_members AS member
    WHERE member.registry_id = p_registry_id;
    IF FOUND THEN
        RETURN aap_inference_topology_v1._pool_json(v_existing);
    END IF;
    IF p_registry_id IS NULL OR p_pool_id IS NULL OR p_name IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid singleton Pool create';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.ml_backend_registry AS registry
        WHERE registry.id = p_registry_id
    ) THEN
        RETURN NULL;
    END IF;
    INSERT INTO public.ml_backend_service_pools (
        id, name, enabled, routing_policy, legacy_instance_id,
        routing_generation, capability_snapshot, capability_fingerprint
    ) VALUES (
        p_pool_id, p_name, false, 'smooth_weighted_round_robin', p_registry_id,
        1, CASE WHEN p_snapshot IS NULL THEN NULL ELSE p_snapshot::jsonb END,
        p_fingerprint
    );
    RETURN aap_inference_topology_v1._pool_json(p_pool_id);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_topology_v1.create_singleton_pool(uuid, uuid, text, text, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.create_singleton_member(
    p_registry_id uuid,
    p_pool_id uuid,
    p_member_id uuid
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
BEGIN
    INSERT INTO public.ml_backend_pool_members (
        id, pool_id, registry_id, traffic_state, weight
    ) VALUES (p_member_id, p_pool_id, p_registry_id, 'active', 1);
    RETURN aap_inference_topology_v1._member_json(p_member_id);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_topology_v1.create_singleton_member(uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.patch_registry(
    p_registry_id uuid,
    p_patch text
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
    v_patch jsonb;
    v_registry public.ml_backend_registry%ROWTYPE;
    v_protected_change boolean;
    v_next_url text;
    v_owner text;
    v_runtime_epoch bigint;
BEGIN
    IF p_registry_id IS NULL OR p_patch IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Registry patch';
    END IF;
    v_patch := p_patch::jsonb;
    IF pg_catalog.jsonb_typeof(v_patch) <> 'object'
       OR v_patch - ARRAY[
            'name', 'url', 'is_interactive', 'auth_method', 'auth_token',
            'extra_params', 'gpu_resource_id', 'vram_budget_mb', 'eviction_priority'
       ] <> '{}'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unsupported Registry patch field';
    END IF;
    SELECT registry.* INTO v_registry
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF v_patch ? 'url' THEN
        IF jsonb_typeof(v_patch->'url') <> 'string' OR v_patch->>'url' = '' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'url must not be null or empty';
        END IF;
        v_next_url := v_patch->>'url';
        SELECT registry.name INTO v_owner
        FROM public.ml_backend_registry AS registry
        WHERE registry.url = v_next_url AND registry.id <> p_registry_id;
        IF FOUND THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P0001',
                MESSAGE = 'aap_inference_topology:registry_url_conflict:' || v_owner || ':';
        END IF;
    ELSE
        v_next_url := v_registry.url;
    END IF;
    v_protected_change :=
        (v_patch ? 'url' AND v_next_url IS DISTINCT FROM v_registry.url)
        OR (v_patch ? 'auth_method' AND v_patch->>'auth_method' IS DISTINCT FROM v_registry.auth_method)
        OR (v_patch ? 'auth_token' AND v_patch->>'auth_token' IS DISTINCT FROM v_registry.auth_token)
        OR (v_patch ? 'extra_params' AND v_patch->'extra_params' IS DISTINCT FROM v_registry.extra_params)
        OR (v_patch ? 'gpu_resource_id' AND v_patch->>'gpu_resource_id' IS DISTINCT FROM v_registry.gpu_resource_id)
        OR (v_patch ? 'vram_budget_mb' AND (v_patch->>'vram_budget_mb')::integer IS DISTINCT FROM v_registry.vram_budget_mb)
        OR (v_patch ? 'eviction_priority' AND (v_patch->>'eviction_priority')::integer IS DISTINCT FROM v_registry.eviction_priority);
    IF v_protected_change THEN
        SELECT fence.runtime_epoch_high_water INTO v_runtime_epoch
        FROM public.gpu_backend_fences AS fence
        WHERE fence.backend_registry_id = p_registry_id;
        IF COALESCE(v_runtime_epoch, 0) > 0 THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P0001',
                MESSAGE = 'aap_inference_topology:managed_gpu_mutation:managed GPU backend requires retirement before mutation:';
        END IF;
    END IF;
    BEGIN
        UPDATE public.ml_backend_registry AS registry
        SET name = CASE WHEN v_patch ? 'name' THEN v_patch->>'name' ELSE registry.name END,
            url = CASE WHEN v_patch ? 'url' THEN v_next_url ELSE registry.url END,
            is_interactive = CASE WHEN v_patch ? 'is_interactive'
                THEN (v_patch->>'is_interactive')::boolean ELSE registry.is_interactive END,
            auth_method = CASE WHEN v_patch ? 'auth_method'
                THEN v_patch->>'auth_method' ELSE registry.auth_method END,
            auth_token = CASE WHEN v_patch ? 'auth_token'
                THEN v_patch->>'auth_token' ELSE registry.auth_token END,
            extra_params = CASE WHEN v_patch ? 'extra_params'
                THEN v_patch->'extra_params' ELSE registry.extra_params END,
            gpu_resource_id = CASE WHEN v_patch ? 'gpu_resource_id'
                THEN v_patch->>'gpu_resource_id' ELSE registry.gpu_resource_id END,
            vram_budget_mb = CASE WHEN v_patch ? 'vram_budget_mb'
                THEN (v_patch->>'vram_budget_mb')::integer ELSE registry.vram_budget_mb END,
            eviction_priority = CASE WHEN v_patch ? 'eviction_priority'
                THEN (v_patch->>'eviction_priority')::integer ELSE registry.eviction_priority END,
            state = CASE WHEN v_protected_change THEN 'disconnected' ELSE registry.state END,
            health_meta = CASE WHEN v_protected_change THEN NULL ELSE registry.health_meta END,
            last_checked_at = CASE WHEN v_protected_change THEN NULL ELSE registry.last_checked_at END,
            error_message = CASE WHEN v_protected_change THEN NULL ELSE registry.error_message END,
            updated_at = pg_catalog.now()
        WHERE registry.id = p_registry_id;
    EXCEPTION WHEN unique_violation THEN
        SELECT registry.name INTO v_owner
        FROM public.ml_backend_registry AS registry
        WHERE registry.url = v_next_url AND registry.id <> p_registry_id;
        IF FOUND THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P0001',
                MESSAGE = 'aap_inference_topology:registry_url_conflict:' || v_owner || ':';
        END IF;
        RAISE;
    END;
    RETURN aap_inference_topology_v1._registry_json(p_registry_id);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.patch_registry(uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.validate_registry_delete(p_registry_id uuid)
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
    v_running integer;
    v_runtime_epoch bigint;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.ml_backend_registry AS registry
        WHERE registry.id = p_registry_id
    ) THEN
        RETURN pg_catalog.jsonb_build_object('exists', false, 'running_jobs', 0);
    END IF;
    IF aap_learning_v1.registry_evaluation_reference_exists(p_registry_id) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0001',
            MESSAGE = 'aap_inference_topology:registry_evaluation_reference:inference Registry is referenced by frozen Learning samples:';
    END IF;
    v_running := aap_jobs_v1.count_running_registry_predictions(p_registry_id);
    IF v_running > 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0001',
            MESSAGE = 'aap_inference_topology:registry_delete_blocked:running prediction count:' || v_running::text;
    END IF;
    SELECT fence.runtime_epoch_high_water INTO v_runtime_epoch
    FROM public.gpu_backend_fences AS fence
    WHERE fence.backend_registry_id = p_registry_id;
    IF COALESCE(v_runtime_epoch, 0) > 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0001',
            MESSAGE = 'aap_inference_topology:managed_gpu_mutation:managed GPU backend requires retirement before delete:';
    END IF;
    RETURN pg_catalog.jsonb_build_object('exists', true, 'running_jobs', v_running);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_topology_v1.validate_registry_delete(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.delete_registry(
    p_registry_id uuid,
    p_expected text
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
    v_expected jsonb;
    v_registry public.ml_backend_registry%ROWTYPE;
    v_member public.ml_backend_pool_members%ROWTYPE;
    v_pool_id uuid;
    v_pool public.ml_backend_service_pools%ROWTYPE;
    v_actual jsonb;
BEGIN
    IF p_registry_id IS NULL OR p_expected IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Registry delete';
    END IF;
    v_expected := p_expected::jsonb;
    SELECT registry.* INTO v_registry
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
    FOR UPDATE;
    IF NOT FOUND THEN
        v_actual := pg_catalog.jsonb_build_object(
            'registry_id', p_registry_id,
            'member_id', NULL,
            'pool_id', NULL,
            'routing_generation', NULL,
            'traffic_state', NULL
        );
        IF v_expected <> 'null'::jsonb AND v_expected IS DISTINCT FROM v_actual THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P0001',
                MESSAGE = 'aap_inference_topology:topology_changed:service-pool topology changed after quiescence check; drain again and retry:';
        END IF;
        RETURN pg_catalog.jsonb_build_object('deleted', false, 'pool_id', NULL);
    END IF;
    PERFORM aap_inference_topology_v1.validate_registry_delete(p_registry_id);
    SELECT member.pool_id INTO v_pool_id
    FROM public.ml_backend_pool_members AS member
    WHERE member.registry_id = p_registry_id;
    FOR v_pool IN
        SELECT pool.*
        FROM public.ml_backend_service_pools AS pool
        WHERE pool.id = v_pool_id OR pool.legacy_instance_id = p_registry_id
        ORDER BY pool.id
        FOR UPDATE
    LOOP
        NULL;
    END LOOP;
    SELECT member.* INTO v_member
    FROM public.ml_backend_pool_members AS member
    WHERE member.registry_id = p_registry_id
    FOR UPDATE;
    IF NOT FOUND THEN
        v_actual := pg_catalog.jsonb_build_object(
            'registry_id', p_registry_id,
            'member_id', NULL,
            'pool_id', NULL,
            'routing_generation', NULL,
            'traffic_state', NULL
        );
        IF v_expected <> 'null'::jsonb AND v_expected IS DISTINCT FROM v_actual THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P0001',
                MESSAGE = 'aap_inference_topology:topology_changed:service-pool topology changed after quiescence check; drain again and retry:';
        END IF;
    ELSE
        SELECT pool.* INTO v_pool
        FROM public.ml_backend_service_pools AS pool
        WHERE pool.id = v_member.pool_id;
        v_actual := pg_catalog.jsonb_build_object(
            'registry_id', p_registry_id,
            'member_id', v_member.id,
            'pool_id', v_member.pool_id,
            'routing_generation', v_pool.routing_generation,
            'traffic_state', v_member.traffic_state
        );
        IF v_expected <> 'null'::jsonb AND v_expected IS DISTINCT FROM v_actual THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P0001',
                MESSAGE = 'aap_inference_topology:topology_changed:service-pool topology changed after quiescence check; drain again and retry:';
        END IF;
        IF v_member.pool_id IS DISTINCT FROM v_pool_id THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P0001',
                MESSAGE = 'aap_inference_topology:topology_changed:service-pool membership changed during deletion:';
        END IF;
        DELETE FROM public.ml_backend_pool_members AS member WHERE member.id = v_member.id;
    END IF;
    UPDATE public.ml_backend_service_pools AS pool
    SET enabled = false,
        legacy_instance_id = NULL,
        routing_generation = pool.routing_generation + 1,
        updated_at = pg_catalog.now()
    WHERE pool.legacy_instance_id = p_registry_id;
    IF v_member.id IS NOT NULL THEN
        UPDATE public.ml_backend_service_pools AS pool
        SET routing_generation = pool.routing_generation + 1,
            updated_at = pg_catalog.now()
        WHERE pool.id = v_pool_id
          AND pool.legacy_instance_id IS DISTINCT FROM p_registry_id;
    END IF;
    DELETE FROM public.ml_backend_registry AS registry WHERE registry.id = p_registry_id;
    RETURN pg_catalog.jsonb_build_object('deleted', true, 'pool_id', v_pool_id);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.delete_registry(uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.create_pool(p_pool_id uuid, p_name text)
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
BEGIN
    IF p_pool_id IS NULL OR p_name IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Pool create';
    END IF;
    INSERT INTO public.ml_backend_service_pools (
        id, name, enabled, routing_policy, legacy_instance_id,
        routing_generation, capability_fingerprint, capability_snapshot
    ) VALUES (
        p_pool_id, p_name, false, 'smooth_weighted_round_robin', NULL, 1, NULL, NULL
    );
    RETURN aap_inference_topology_v1._pool_json(p_pool_id);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.create_pool(uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.patch_pool(p_pool_id uuid, p_patch text)
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
    v_patch jsonb;
    v_pool public.ml_backend_service_pools%ROWTYPE;
    v_changed boolean := false;
BEGIN
    IF p_pool_id IS NULL OR p_patch IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Pool patch';
    END IF;
    v_patch := p_patch::jsonb;
    IF pg_catalog.jsonb_typeof(v_patch) <> 'object'
       OR v_patch - ARRAY['name', 'enabled'] <> '{}'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unsupported Pool patch field';
    END IF;
    SELECT pool.* INTO v_pool
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = p_pool_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF v_patch ? 'enabled' AND (v_patch->>'enabled')::boolean THEN
        IF v_pool.legacy_instance_id IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P0001',
                MESSAGE = 'aap_inference_topology:pool_has_members:cannot enable a pool with no legacy instance; add a member first (D15):';
        END IF;
        IF v_pool.capability_fingerprint IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P0001',
                MESSAGE = 'aap_inference_topology:pool_has_members:cannot enable a pool before a member capability snapshot is available:';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.ml_backend_pool_members AS member
            WHERE member.pool_id = p_pool_id
              AND member.registry_id = v_pool.legacy_instance_id
              AND member.traffic_state = 'active'
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P0001',
                MESSAGE = 'aap_inference_topology:pool_has_members:legacy instance must be an active member of the pool:';
        END IF;
    END IF;
    IF v_patch ? 'name' AND v_patch->>'name' IS DISTINCT FROM v_pool.name THEN
        v_changed := true;
    END IF;
    IF v_patch ? 'enabled' AND (v_patch->>'enabled')::boolean IS DISTINCT FROM v_pool.enabled THEN
        v_changed := true;
    END IF;
    UPDATE public.ml_backend_service_pools AS pool
    SET name = CASE WHEN v_patch ? 'name' AND v_patch->>'name' IS NOT NULL
                    THEN v_patch->>'name' ELSE pool.name END,
        enabled = CASE WHEN v_patch ? 'enabled'
                       THEN (v_patch->>'enabled')::boolean ELSE pool.enabled END,
        routing_generation = pool.routing_generation + CASE WHEN v_changed THEN 1 ELSE 0 END,
        updated_at = pg_catalog.now()
    WHERE pool.id = p_pool_id;
    RETURN aap_inference_topology_v1._pool_json(p_pool_id);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.patch_pool(uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.lock_empty_pool(p_pool_id uuid)
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
    v_pool public.ml_backend_service_pools%ROWTYPE;
BEGIN
    SELECT pool.* INTO v_pool
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = p_pool_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.ml_backend_pool_members AS member
        WHERE member.pool_id = p_pool_id
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0001',
            MESSAGE = 'aap_inference_topology:pool_has_members:remove every service-pool member before deleting the pool:';
    END IF;
    RETURN aap_inference_topology_v1._pool_json(p_pool_id);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.lock_empty_pool(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.delete_empty_pool(p_pool_id uuid)
RETURNS boolean
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
    v_locked uuid;
BEGIN
    SELECT pool.id INTO v_locked
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = p_pool_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.ml_backend_pool_members AS member
        WHERE member.pool_id = p_pool_id
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0001',
            MESSAGE = 'aap_inference_topology:pool_has_members:remove every service-pool member before deleting the pool:';
    END IF;
    DELETE FROM public.ml_backend_service_pools AS pool WHERE pool.id = p_pool_id;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.delete_empty_pool(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.lock_member_upsert_rows(
    p_pool_id uuid,
    p_registry_id uuid
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
    v_registry uuid;
    v_pool uuid;
    v_member uuid;
BEGIN
    SELECT registry.id INTO v_registry
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
    FOR SHARE;
    SELECT pool.id INTO v_pool
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = p_pool_id
    FOR UPDATE;
    SELECT member.id INTO v_member
    FROM public.ml_backend_pool_members AS member
    WHERE member.registry_id = p_registry_id
    FOR UPDATE;
    RETURN pg_catalog.jsonb_build_object(
        'pool', aap_inference_topology_v1._pool_json(v_pool),
        'registry', aap_inference_topology_v1._registry_json(v_registry),
        'member', aap_inference_topology_v1._member_json(v_member)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_topology_v1.lock_member_upsert_rows(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.upsert_member(
    p_pool_id uuid,
    p_registry_id uuid,
    p_weight integer,
    p_expected text
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
    v_expected jsonb;
    v_registry public.ml_backend_registry%ROWTYPE;
    v_pool public.ml_backend_service_pools%ROWTYPE;
    v_member public.ml_backend_pool_members%ROWTYPE;
    v_member_exists boolean := false;
    v_other_member boolean;
    v_candidate jsonb;
    v_candidate_fp text;
BEGIN
    IF p_pool_id IS NULL OR p_registry_id IS NULL OR p_expected IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid member upsert';
    END IF;
    v_expected := p_expected::jsonb;
    SELECT registry.* INTO v_registry
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
    FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'registry not found';
    END IF;
    SELECT pool.* INTO v_pool
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = p_pool_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'pool not found';
    END IF;
    SELECT member.* INTO v_member
    FROM public.ml_backend_pool_members AS member
    WHERE member.registry_id = p_registry_id
    FOR UPDATE;
    v_member_exists := FOUND;
    v_candidate := v_registry.health_meta->'capabilities';
    IF v_expected->>'pool_generation' IS DISTINCT FROM v_pool.routing_generation::text
       OR v_expected->>'pool_fingerprint' IS DISTINCT FROM v_pool.capability_fingerprint
       OR v_expected->'pool_snapshot' IS DISTINCT FROM COALESCE(v_pool.capability_snapshot, 'null'::jsonb)
       OR v_expected->'registry_capabilities' IS DISTINCT FROM COALESCE(v_candidate, 'null'::jsonb)
       OR v_expected->>'member_id' IS DISTINCT FROM (CASE WHEN v_member_exists THEN v_member.id::text ELSE NULL END)
       OR v_expected->>'member_state' IS DISTINCT FROM (CASE WHEN v_member_exists THEN v_member.traffic_state ELSE NULL END)
       OR v_expected->>'member_weight' IS DISTINCT FROM (CASE WHEN v_member_exists THEN v_member.weight::text ELSE NULL END) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'topology member upsert review became stale';
    END IF;
    v_candidate_fp := v_expected->>'candidate_fingerprint';
    IF v_member_exists AND v_member.pool_id <> p_pool_id THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0001',
            MESSAGE = 'aap_inference_topology:pool_member_conflict:registry already belongs to another service pool:' || v_member.pool_id::text;
    END IF;
    IF v_pool.capability_fingerprint IS NULL AND NOT v_member_exists THEN
        SELECT EXISTS (
            SELECT 1 FROM public.ml_backend_pool_members AS member
            WHERE member.pool_id = p_pool_id
        ) INTO v_other_member;
        IF v_other_member THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P0001',
                MESSAGE = 'aap_inference_topology:pool_capability_unavailable:service pool capability baseline is unavailable; refresh the existing member before adding replicas:';
        END IF;
    END IF;
    IF v_member_exists THEN
        IF v_pool.capability_fingerprint IS NULL AND v_candidate_fp IS NOT NULL THEN
            UPDATE public.ml_backend_service_pools AS pool
            SET capability_fingerprint = v_candidate_fp,
                capability_snapshot = v_expected->'candidate_snapshot',
                routing_generation = pool.routing_generation + 1,
                updated_at = pg_catalog.now()
            WHERE pool.id = p_pool_id;
        END IF;
        IF v_member.weight IS DISTINCT FROM p_weight THEN
            UPDATE public.ml_backend_pool_members AS member
            SET weight = p_weight, updated_at = pg_catalog.now()
            WHERE member.id = v_member.id;
            UPDATE public.ml_backend_service_pools AS pool
            SET routing_generation = pool.routing_generation + 1,
                updated_at = pg_catalog.now()
            WHERE pool.id = p_pool_id;
        END IF;
    ELSE
        INSERT INTO public.ml_backend_pool_members (
            id, pool_id, registry_id, traffic_state, weight
        ) VALUES (
            pg_catalog.gen_random_uuid(), p_pool_id, p_registry_id, 'active', p_weight
        ) RETURNING * INTO v_member;
        IF v_pool.capability_fingerprint IS NULL AND v_candidate_fp IS NOT NULL THEN
            UPDATE public.ml_backend_service_pools AS pool
            SET capability_fingerprint = v_candidate_fp,
                capability_snapshot = v_expected->'candidate_snapshot',
                legacy_instance_id = COALESCE(pool.legacy_instance_id, p_registry_id),
                routing_generation = pool.routing_generation + 1,
                updated_at = pg_catalog.now()
            WHERE pool.id = p_pool_id;
        ELSE
            UPDATE public.ml_backend_service_pools AS pool
            SET legacy_instance_id = COALESCE(pool.legacy_instance_id, p_registry_id),
                routing_generation = pool.routing_generation + 1,
                updated_at = pg_catalog.now()
            WHERE pool.id = p_pool_id;
        END IF;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'member', aap_inference_topology_v1._member_json(v_member.id),
        'pool', aap_inference_topology_v1._pool_json(p_pool_id)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_topology_v1.upsert_member(uuid, uuid, integer, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.remove_member(
    p_pool_id uuid,
    p_registry_id uuid,
    p_expected text
)
RETURNS boolean
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
    v_expected jsonb;
    v_registry uuid;
    v_pool public.ml_backend_service_pools%ROWTYPE;
    v_member public.ml_backend_pool_members%ROWTYPE;
    v_actual jsonb;
BEGIN
    IF p_expected IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'missing topology comparison';
    END IF;
    v_expected := p_expected::jsonb;
    SELECT registry.id INTO v_registry
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
    FOR KEY SHARE;
    IF NOT FOUND THEN
        v_actual := pg_catalog.jsonb_build_object(
            'registry_id', p_registry_id, 'member_id', NULL, 'pool_id', NULL,
            'routing_generation', NULL, 'traffic_state', NULL
        );
        IF v_expected <> 'null'::jsonb AND v_expected IS DISTINCT FROM v_actual THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_inference_topology:topology_changed:service-pool topology changed after quiescence check; drain again and retry:';
        END IF;
        RETURN false;
    END IF;
    SELECT pool.* INTO v_pool
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = p_pool_id
    FOR UPDATE;
    IF NOT FOUND THEN
        v_actual := pg_catalog.jsonb_build_object(
            'registry_id', p_registry_id, 'member_id', NULL, 'pool_id', NULL,
            'routing_generation', NULL, 'traffic_state', NULL
        );
        IF v_expected <> 'null'::jsonb AND v_expected IS DISTINCT FROM v_actual THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_inference_topology:topology_changed:service-pool topology changed after quiescence check; drain again and retry:';
        END IF;
        RETURN false;
    END IF;
    SELECT member.* INTO v_member
    FROM public.ml_backend_pool_members AS member
    WHERE member.pool_id = p_pool_id AND member.registry_id = p_registry_id
    FOR UPDATE;
    IF NOT FOUND THEN
        v_actual := pg_catalog.jsonb_build_object(
            'registry_id', p_registry_id, 'member_id', NULL, 'pool_id', NULL,
            'routing_generation', NULL, 'traffic_state', NULL
        );
        IF v_expected <> 'null'::jsonb AND v_expected IS DISTINCT FROM v_actual THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_inference_topology:topology_changed:service-pool topology changed after quiescence check; drain again and retry:';
        END IF;
        RETURN false;
    END IF;
    v_actual := pg_catalog.jsonb_build_object(
        'registry_id', p_registry_id, 'member_id', v_member.id, 'pool_id', v_member.pool_id,
        'routing_generation', v_pool.routing_generation, 'traffic_state', v_member.traffic_state
    );
    IF v_expected <> 'null'::jsonb AND v_expected IS DISTINCT FROM v_actual THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001',
            MESSAGE = 'aap_inference_topology:topology_changed:service-pool topology changed after quiescence check; drain again and retry:';
    END IF;
    DELETE FROM public.ml_backend_pool_members AS member WHERE member.id = v_member.id;
    IF v_pool.legacy_instance_id = p_registry_id THEN
        UPDATE public.ml_backend_service_pools AS pool
        SET legacy_instance_id = NULL, enabled = false,
            routing_generation = pool.routing_generation + 1,
            updated_at = pg_catalog.now()
        WHERE pool.id = p_pool_id;
    ELSE
        UPDATE public.ml_backend_service_pools AS pool
        SET routing_generation = pool.routing_generation + 1,
            updated_at = pg_catalog.now()
        WHERE pool.id = p_pool_id;
    END IF;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_topology_v1.remove_member(uuid, uuid, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.drain_member(
    p_pool_id uuid,
    p_registry_id uuid
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
    v_pool public.ml_backend_service_pools%ROWTYPE;
    v_member public.ml_backend_pool_members%ROWTYPE;
BEGIN
    SELECT pool.* INTO v_pool
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = p_pool_id
    FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT member.* INTO v_member
    FROM public.ml_backend_pool_members AS member
    WHERE member.pool_id = p_pool_id AND member.registry_id = p_registry_id
    FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    IF v_member.traffic_state <> 'draining' THEN
        UPDATE public.ml_backend_pool_members AS member
        SET traffic_state = 'draining', updated_at = pg_catalog.now()
        WHERE member.id = v_member.id
        RETURNING * INTO v_member;
        UPDATE public.ml_backend_service_pools AS pool
        SET routing_generation = pool.routing_generation + 1,
            updated_at = pg_catalog.now()
        WHERE pool.id = p_pool_id;
    END IF;
    RETURN aap_inference_topology_v1._member_json(v_member.id);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.drain_member(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.resume_member(
    p_pool_id uuid,
    p_registry_id uuid
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
    v_pool public.ml_backend_service_pools%ROWTYPE;
    v_member public.ml_backend_pool_members%ROWTYPE;
BEGIN
    SELECT pool.* INTO v_pool
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = p_pool_id
    FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT member.* INTO v_member
    FROM public.ml_backend_pool_members AS member
    WHERE member.pool_id = p_pool_id AND member.registry_id = p_registry_id
    FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    IF v_member.traffic_state = 'disabled' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'disabled member needs capability re-validation before resume';
    END IF;
    IF v_member.traffic_state <> 'active' THEN
        UPDATE public.ml_backend_pool_members AS member
        SET traffic_state = 'active', updated_at = pg_catalog.now()
        WHERE member.id = v_member.id
        RETURNING * INTO v_member;
        UPDATE public.ml_backend_service_pools AS pool
        SET routing_generation = pool.routing_generation + 1,
            updated_at = pg_catalog.now()
        WHERE pool.id = p_pool_id;
    END IF;
    RETURN aap_inference_topology_v1._member_json(v_member.id);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.resume_member(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.drift_review_rows(
    p_pool_id uuid,
    p_registry_id uuid
)
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
        'pool', aap_inference_topology_v1._pool_json(pool.id),
        'members', COALESCE(
            (
                SELECT pg_catalog.jsonb_agg(
                    aap_inference_topology_v1._member_json(member.id)
                    ORDER BY member.id
                )
                FROM public.ml_backend_pool_members AS member
                WHERE member.pool_id = pool.id
            ),
            '[]'::jsonb
        )
    )
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = p_pool_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_topology_v1.drift_review_rows(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.lock_drift_acceptance_rows(
    p_pool_id uuid,
    p_registry_id uuid
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
    v_registry uuid;
    v_pool uuid;
BEGIN
    SELECT registry.id INTO v_registry
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
    FOR SHARE;
    SELECT pool.id INTO v_pool
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = p_pool_id
    FOR UPDATE;
    IF v_pool IS NOT NULL THEN
        PERFORM member.id
        FROM public.ml_backend_pool_members AS member
        WHERE member.pool_id = p_pool_id
        ORDER BY member.id
        FOR UPDATE OF member;
    END IF;
    RETURN aap_inference_topology_v1.drift_review_rows(p_pool_id, p_registry_id);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_topology_v1.lock_drift_acceptance_rows(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.accept_drift(
    p_pool_id uuid,
    p_registry_id uuid,
    p_candidate_fingerprint text,
    p_enable_pool boolean,
    p_expected text
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
    v_expected jsonb;
    v_registry public.ml_backend_registry%ROWTYPE;
    v_pool public.ml_backend_service_pools%ROWTYPE;
    v_member public.ml_backend_pool_members%ROWTYPE;
    v_member_facts jsonb;
BEGIN
    IF p_expected IS NULL OR p_candidate_fingerprint IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid capability acceptance';
    END IF;
    v_expected := p_expected::jsonb;
    SELECT registry.* INTO v_registry
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
    FOR SHARE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT pool.* INTO v_pool
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = p_pool_id
    FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    PERFORM member.id
    FROM public.ml_backend_pool_members AS member
    WHERE member.pool_id = p_pool_id
    ORDER BY member.id
    FOR UPDATE OF member;
    SELECT member.* INTO v_member
    FROM public.ml_backend_pool_members AS member
    WHERE member.pool_id = p_pool_id AND member.registry_id = p_registry_id;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'id', member.id::text,
                'registry_id', member.registry_id::text,
                'traffic_state', member.traffic_state,
                'capabilities', registry.health_meta->'capabilities'
            ) ORDER BY member.id
        ),
        '[]'::jsonb
    ) INTO v_member_facts
    FROM public.ml_backend_pool_members AS member
    JOIN public.ml_backend_registry AS registry ON registry.id = member.registry_id
    WHERE member.pool_id = p_pool_id;
    IF v_expected->>'pool_generation' IS DISTINCT FROM v_pool.routing_generation::text
       OR v_expected->>'pool_fingerprint' IS DISTINCT FROM v_pool.capability_fingerprint
       OR v_expected->'pool_snapshot' IS DISTINCT FROM COALESCE(v_pool.capability_snapshot, 'null'::jsonb)
       OR v_expected->'candidate_capabilities' IS DISTINCT FROM COALESCE(v_registry.health_meta->'capabilities', 'null'::jsonb)
       OR v_expected->>'candidate_state' IS DISTINCT FROM v_registry.state
       OR v_expected->>'candidate_member_id' IS DISTINCT FROM v_member.id::text
       OR v_expected->>'candidate_member_state' IS DISTINCT FROM v_member.traffic_state
       OR v_expected->'member_facts' IS DISTINCT FROM v_member_facts THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001',
            MESSAGE = 'aap_inference_topology:capability_changed_during_review:backend capability changed after review; refresh the preview:';
    END IF;
    IF v_registry.state <> 'connected' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001',
            MESSAGE = 'aap_inference_topology:capability_revalidation_failed:backend must be connected before accepting capability drift:';
    END IF;
    IF v_member.traffic_state <> 'disabled' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001',
            MESSAGE = 'aap_inference_topology:capability_revalidation_not_required:only a disabled member can accept capability drift:';
    END IF;
    IF v_pool.capability_fingerprint IS NULL OR v_pool.capability_snapshot IS NULL
       OR v_registry.health_meta->'capabilities' IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001',
            MESSAGE = 'aap_inference_topology:capability_snapshot_unavailable:service pool capability baseline is unavailable:';
    END IF;
    IF p_enable_pool THEN
        IF v_pool.legacy_instance_id IS NULL OR NOT EXISTS (
            SELECT 1 FROM public.ml_backend_pool_members AS legacy
            WHERE legacy.pool_id = p_pool_id
              AND legacy.registry_id = v_pool.legacy_instance_id
              AND (legacy.registry_id = p_registry_id OR legacy.traffic_state = 'active')
        ) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001',
                MESSAGE = 'aap_inference_topology:capability_revalidation_failed:legacy instance must be an active member before enabling the pool:';
        END IF;
    END IF;
    UPDATE public.ml_backend_pool_members AS member
    SET traffic_state = 'active', updated_at = pg_catalog.now()
    WHERE member.id = v_member.id;
    UPDATE public.ml_backend_service_pools AS pool
    SET capability_snapshot = CASE
            WHEN COALESCE((v_expected->>'replace_baseline')::boolean, false)
                THEN v_expected->'replacement_snapshot'
            ELSE pool.capability_snapshot
        END,
        capability_fingerprint = CASE
            WHEN COALESCE((v_expected->>'replace_baseline')::boolean, false)
                THEN p_candidate_fingerprint
            ELSE pool.capability_fingerprint
        END,
        enabled = CASE WHEN p_enable_pool THEN true ELSE pool.enabled END,
        routing_generation = pool.routing_generation + 1,
        updated_at = pg_catalog.now()
    WHERE pool.id = p_pool_id;
    RETURN aap_inference_topology_v1._pool_json(p_pool_id);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_topology_v1.accept_drift(uuid, uuid, text, boolean, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.set_project_registry_enablement(
    p_project_id uuid,
    p_registry_id uuid,
    p_enabled boolean,
    p_overrides text
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
    v_overrides jsonb;
    v_registry uuid;
    v_initial_pool uuid;
    v_current_pool uuid;
    v_pool public.ml_backend_service_pools%ROWTYPE;
    v_assoc public.project_ml_backend_pool%ROWTYPE;
    v_has_variants boolean;
BEGIN
    IF p_project_id IS NULL OR p_registry_id IS NULL OR p_enabled IS NULL OR p_overrides IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid project Registry enablement';
    END IF;
    v_overrides := p_overrides::jsonb;
    IF pg_catalog.jsonb_typeof(v_overrides) <> 'object'
       OR v_overrides - ARRAY['default_variants'] <> '{}'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid project Registry overrides';
    END IF;
    v_has_variants := v_overrides ? 'default_variants';
    SELECT registry.id INTO v_registry
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
    FOR SHARE;
    SELECT member.pool_id INTO v_initial_pool
    FROM public.ml_backend_pool_members AS member
    WHERE member.registry_id = p_registry_id;
    IF v_registry IS NULL OR v_initial_pool IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001',
            MESSAGE = 'aap_inference_topology:project_topology_changed:backend or service-pool membership is no longer available:';
    END IF;
    SELECT pool.* INTO v_pool
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = v_initial_pool
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001',
            MESSAGE = 'aap_inference_topology:project_topology_changed:backend or service-pool membership is no longer available:';
    END IF;
    SELECT member.pool_id INTO v_current_pool
    FROM public.ml_backend_pool_members AS member
    WHERE member.registry_id = p_registry_id;
    IF v_current_pool IS DISTINCT FROM v_initial_pool THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001',
            MESSAGE = 'aap_inference_topology:project_topology_changed:service-pool membership changed while enabling the backend:';
    END IF;
    IF p_enabled AND NOT v_pool.enabled THEN
        UPDATE public.ml_backend_service_pools AS pool
        SET enabled = true,
            routing_generation = pool.routing_generation + 1,
            updated_at = pg_catalog.now()
        WHERE pool.id = v_initial_pool;
    END IF;
    INSERT INTO public.project_ml_backend_pool AS association (
        id, project_id, pool_id, enabled, default_variants
    ) VALUES (
        pg_catalog.gen_random_uuid(), p_project_id, v_initial_pool, p_enabled,
        CASE WHEN v_has_variants THEN v_overrides->'default_variants' ELSE NULL END
    )
    ON CONFLICT (project_id, pool_id) DO UPDATE
    SET enabled = EXCLUDED.enabled,
        default_variants = CASE WHEN v_has_variants
            THEN EXCLUDED.default_variants ELSE association.default_variants END,
        updated_at = pg_catalog.now()
    RETURNING * INTO v_assoc;
    RETURN aap_inference_topology_v1._association_json(v_assoc.id);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_topology_v1.set_project_registry_enablement(uuid, uuid, boolean, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.set_project_pool_enablement(
    p_project_id uuid,
    p_pool_id uuid,
    p_enabled boolean,
    p_overrides text
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
    v_overrides jsonb;
    v_pool public.ml_backend_service_pools%ROWTYPE;
    v_assoc public.project_ml_backend_pool%ROWTYPE;
    v_has_variants boolean;
BEGIN
    IF p_project_id IS NULL OR p_pool_id IS NULL OR p_enabled IS NULL OR p_overrides IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid project Pool enablement';
    END IF;
    v_overrides := p_overrides::jsonb;
    IF pg_catalog.jsonb_typeof(v_overrides) <> 'object'
       OR v_overrides - ARRAY['default_variants'] <> '{}'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid project Pool overrides';
    END IF;
    v_has_variants := v_overrides ? 'default_variants';
    SELECT pool.* INTO v_pool
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = p_pool_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'service Pool not found';
    END IF;
    IF p_enabled AND (NOT v_pool.enabled OR v_pool.legacy_instance_id IS NULL) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001',
            MESSAGE = 'aap_inference_topology:project_pool_not_enabled:pool is disabled or has no legacy instance; cannot enable for project:';
    END IF;
    INSERT INTO public.project_ml_backend_pool AS association (
        id, project_id, pool_id, enabled, default_variants
    ) VALUES (
        pg_catalog.gen_random_uuid(), p_project_id, p_pool_id, p_enabled,
        CASE WHEN v_has_variants THEN v_overrides->'default_variants' ELSE NULL END
    )
    ON CONFLICT (project_id, pool_id) DO UPDATE
    SET enabled = EXCLUDED.enabled,
        default_variants = CASE WHEN v_has_variants
            THEN EXCLUDED.default_variants ELSE association.default_variants END,
        updated_at = pg_catalog.now()
    RETURNING * INTO v_assoc;
    RETURN aap_inference_topology_v1._association_json(v_assoc.id);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_topology_v1.set_project_pool_enablement(uuid, uuid, boolean, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.delete_project_associations(p_project_id uuid)
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
    v_deleted integer;
BEGIN
    IF p_project_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid project association deletion';
    END IF;
    DELETE FROM public.project_ml_backend_pool AS association
    WHERE association.project_id = p_project_id;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN v_deleted;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.delete_project_associations(uuid) FROM PUBLIC;
