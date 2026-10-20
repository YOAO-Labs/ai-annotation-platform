-- Narrow Workspace capability for project-specific brief roles.
CREATE FUNCTION aap_workspace_v1.identity_brief_project_roles(
    p_project_id uuid,
    p_user_ids uuid[]
)
RETURNS TABLE(user_id uuid, role text)
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT member.user_id, member.role
    FROM public.project_members AS member
    WHERE member.project_id = p_project_id
      AND member.user_id = ANY(p_user_ids)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.identity_brief_project_roles(
    uuid, uuid[]
) FROM PUBLIC;
