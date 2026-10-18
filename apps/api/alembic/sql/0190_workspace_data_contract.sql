-- Install with psql --single-transaction and ON_ERROR_STOP enabled.
CREATE SCHEMA aap_workspace_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_workspace_v1 FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.lock_project_reference(p_project_id uuid)
RETURNS uuid
LANGUAGE sql
VOLATILE
STRICT
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT p.id
    FROM public.projects AS p
    WHERE p.id = p_project_id
    FOR KEY SHARE
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.lock_project_reference(uuid) FROM PUBLIC;
