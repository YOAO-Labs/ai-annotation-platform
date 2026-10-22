CREATE FUNCTION aap_workspace_v1.actor_project_memberships(p_user_id uuid)
RETURNS TABLE(project_id uuid, member_role text)
LANGUAGE sql
STABLE
STRICT
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT member.project_id, member.role::text
    FROM public.project_members AS member
    WHERE member.user_id = p_user_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.actor_project_memberships(uuid)
    FROM PUBLIC;
