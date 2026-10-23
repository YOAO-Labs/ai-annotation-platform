-- Clear only the deleted backend pool's Project bindings in the caller transaction.
CREATE OR REPLACE FUNCTION aap_workspace_v1.clear_primary_backend_bindings(
    p_pool_id uuid
)
RETURNS bigint
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
    v_rows_cleared bigint;
    v_project_ids uuid[];
BEGIN
    IF p_pool_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace backend binding request';
    END IF;

    BEGIN
        SELECT COALESCE(pg_catalog.array_agg(locked.id ORDER BY locked.id), ARRAY[]::uuid[])
        INTO v_project_ids
        FROM (
            SELECT project.id
            FROM public.projects AS project
            WHERE project.ml_backend_pool_id = p_pool_id
            ORDER BY project.id
            FOR UPDATE NOWAIT
        ) AS locked;
    EXCEPTION WHEN lock_not_available THEN
        RAISE EXCEPTION USING ERRCODE = '55P03',
            MESSAGE = 'Workspace primary backend bindings are busy',
            HINT = 'aap.workspace.primary_binding_busy';
    END;

    UPDATE public.projects AS project
    SET ml_backend_pool_id = NULL,
        updated_at = pg_catalog.now()
    WHERE project.id = ANY(v_project_ids)
      AND project.ml_backend_pool_id = p_pool_id;

    GET DIAGNOSTICS v_rows_cleared = ROW_COUNT;
    RETURN v_rows_cleared;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.clear_primary_backend_bindings(uuid)
    FROM PUBLIC;
-- aap-statement-break
COMMENT ON FUNCTION aap_workspace_v1.clear_primary_backend_bindings(uuid)
    IS 'Clears Project primary backend-pool bindings for one exact pool in the caller transaction.';

-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_v1.enable_workspace_project_backend(
    p_project_id uuid,
    p_registry_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_pool_id uuid;
    v_pool_enabled boolean;
    v_association_id uuid;
    v_association_enabled boolean;
BEGIN
    IF p_project_id IS NULL OR p_registry_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration backend';
    END IF;

    SELECT member.pool_id
    INTO v_pool_id
    FROM public.ml_backend_pool_members AS member
    WHERE member.registry_id = p_registry_id;

    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-inference-workspace-configuration/v1',
            'project_id', p_project_id,
            'registry_id', p_registry_id,
            'pool_id', NULL
        );
    END IF;

    SELECT pool.enabled
    INTO v_pool_enabled
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = v_pool_id
    FOR UPDATE;

    IF NOT FOUND OR NOT EXISTS (
        SELECT 1
        FROM public.ml_backend_pool_members AS member
        WHERE member.registry_id = p_registry_id
          AND member.pool_id = v_pool_id
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '40001',
            MESSAGE = 'Workspace backend selection changed while waiting for its pool',
            HINT = 'aap.workspace.configuration.project_busy';
    END IF;

    SELECT association.id, association.enabled
    INTO v_association_id, v_association_enabled
    FROM public.project_ml_backend_pool AS association
    WHERE association.project_id = p_project_id
      AND association.pool_id = v_pool_id;

    IF v_pool_enabled IS DISTINCT FROM TRUE THEN
        UPDATE public.ml_backend_service_pools
        SET enabled = TRUE,
            routing_generation = routing_generation + 1,
            updated_at = pg_catalog.now()
        WHERE id = v_pool_id;
    END IF;

    IF v_association_id IS NULL THEN
        INSERT INTO public.project_ml_backend_pool (
            id, project_id, pool_id, enabled
        )
        VALUES (
            pg_catalog.gen_random_uuid(), p_project_id, v_pool_id, TRUE
        );
    ELSIF v_association_enabled IS DISTINCT FROM TRUE THEN
        UPDATE public.project_ml_backend_pool
        SET enabled = TRUE,
            updated_at = pg_catalog.now()
        WHERE id = v_association_id;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-inference-workspace-configuration/v1',
        'project_id', p_project_id,
        'registry_id', p_registry_id,
        'pool_id', v_pool_id
    );
END;
$function$;
