CREATE FUNCTION aap_workspace_v1.lock_project_reference_nowait(
    p_project_id uuid
)
RETURNS uuid
LANGUAGE sql
VOLATILE
STRICT
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT project.id
    FROM public.projects AS project
    WHERE project.id = p_project_id
    FOR KEY SHARE NOWAIT
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_v1.lock_project_reference_nowait(uuid)
    FROM PUBLIC;
