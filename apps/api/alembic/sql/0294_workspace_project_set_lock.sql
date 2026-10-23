CREATE FUNCTION aap_workspace_v1.lock_project_references_for_update(
    p_project_ids uuid[]
)
RETURNS uuid[]
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_project_ids uuid[];
BEGIN
    IF p_project_ids IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace Project reference set';
    END IF;

    WITH locked_projects AS MATERIALIZED (
        SELECT project.id
        FROM public.projects AS project
        WHERE project.id = ANY(p_project_ids)
        ORDER BY project.id
        FOR UPDATE OF project NOWAIT
    )
    SELECT COALESCE(
        pg_catalog.array_agg(locked.id ORDER BY locked.id),
        ARRAY[]::uuid[]
    )
    INTO v_project_ids
    FROM locked_projects AS locked;

    RETURN v_project_ids;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_v1.lock_project_references_for_update(uuid[])
    FROM PUBLIC;
