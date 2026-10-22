CREATE FUNCTION aap_workspace_v1.read_project_row(p_project_id uuid)
RETURNS text
LANGUAGE sql
VOLATILE
STRICT
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT aap_workspace_configuration_v1._class_rename_project_row(
        p_project_id, 'read'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.read_project_row(uuid)
    FROM PUBLIC;
