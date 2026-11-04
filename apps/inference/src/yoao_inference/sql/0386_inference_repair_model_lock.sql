-- Forward extension: hold one repair route stable for the caller transaction.
--
-- The mask-repair source owner previously read the private pool, association,
-- member and registry relations directly with FOR SHARE NOWAIT. Repair callers
-- borrow the same transaction boundary through the fixed topology capability,
-- which keeps the lock order (pool, project association, members by registry
-- UUID, registries by UUID) and reports only credential-free route status.
CREATE FUNCTION aap_inference_topology_v1.lock_repair_model_rows(
    p_project_id uuid,
    p_pool_id uuid,
    p_registry_ids uuid[]
)
RETURNS text
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
    v_pool_enabled boolean;
    v_assoc_enabled boolean;
    v_member_pool_id uuid;
    v_member_traffic_state text;
    v_member_invalid boolean := false;
    v_registry_id uuid;
    v_registry uuid;
BEGIN
    IF p_project_id IS NULL OR p_pool_id IS NULL OR p_registry_ids IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid repair model lock request';
    END IF;
    IF pg_catalog.cardinality(p_registry_ids) NOT BETWEEN 1 AND 2
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.unnest(p_registry_ids) AS requested(id)
           WHERE requested.id IS NULL
       )
       OR (
           SELECT pg_catalog.count(*)
           FROM pg_catalog.unnest(p_registry_ids)
       ) <> (
           SELECT pg_catalog.count(DISTINCT requested.id)
           FROM pg_catalog.unnest(p_registry_ids) AS requested(id)
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid repair model lock request';
    END IF;

    SELECT pool.enabled INTO v_pool_enabled
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = p_pool_id
    FOR SHARE NOWAIT;
    SELECT association.enabled INTO v_assoc_enabled
    FROM public.project_ml_backend_pool AS association
    WHERE association.project_id = p_project_id
      AND association.pool_id = p_pool_id
    FOR SHARE NOWAIT;
    IF v_pool_enabled IS NULL OR NOT v_pool_enabled
       OR v_assoc_enabled IS NULL OR NOT v_assoc_enabled THEN
        RETURN 'backend_unavailable';
    END IF;

    FOR v_registry_id IN
        SELECT requested.id
        FROM pg_catalog.unnest(p_registry_ids) AS requested(id)
        ORDER BY requested.id
    LOOP
        SELECT member.pool_id, member.traffic_state
        INTO v_member_pool_id, v_member_traffic_state
        FROM public.ml_backend_pool_members AS member
        WHERE member.registry_id = v_registry_id
        FOR SHARE NOWAIT;
        IF NOT FOUND THEN
            v_member_invalid := true;
        ELSIF v_member_pool_id <> p_pool_id
           OR v_member_traffic_state <> 'active' THEN
            v_member_invalid := true;
        END IF;
    END LOOP;
    IF v_member_invalid THEN
        RETURN 'model_changed';
    END IF;

    FOR v_registry_id IN
        SELECT requested.id
        FROM pg_catalog.unnest(p_registry_ids) AS requested(id)
        ORDER BY requested.id
    LOOP
        SELECT registry.id INTO v_registry
        FROM public.ml_backend_registry AS registry
        WHERE registry.id = v_registry_id
        FOR SHARE NOWAIT;
        IF NOT FOUND THEN
            RETURN 'model_changed';
        END IF;
    END LOOP;

    RETURN 'ready';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_topology_v1.lock_repair_model_rows(uuid, uuid, uuid[])
    FROM PUBLIC;
