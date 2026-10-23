CREATE FUNCTION aap_workspace_v1.lock_project_member_reference(
    p_project_id uuid,
    p_user_id uuid,
    p_nowait boolean
)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
STRICT
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_user_id uuid;
BEGIN
    IF p_nowait THEN
        SELECT member.user_id
        INTO v_user_id
        FROM public.project_members AS member
        WHERE member.project_id = p_project_id
          AND member.user_id = p_user_id
        FOR SHARE OF member NOWAIT;
    ELSE
        SELECT member.user_id
        INTO v_user_id
        FROM public.project_members AS member
        WHERE member.project_id = p_project_id
          AND member.user_id = p_user_id
        FOR SHARE OF member;
    END IF;
    RETURN v_user_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_v1.lock_project_member_reference(uuid, uuid, boolean)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.project_member_rows(
    p_project_id uuid,
    p_user_ids uuid[],
    p_lock_members boolean
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
    v_rows jsonb;
BEGIN
    IF p_project_id IS NULL OR p_user_ids IS NULL OR p_lock_members IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Workspace ProjectMember rows request';
    END IF;

    IF p_lock_members THEN
        WITH selected_members AS MATERIALIZED (
            SELECT
                member.id,
                member.project_id,
                member.user_id,
                member.role,
                member.assigned_by,
                member.assigned_at,
                member.version,
                member.updated_at,
                member.weekly_target
            FROM public.project_members AS member
            WHERE member.project_id = p_project_id
              AND member.user_id = ANY(p_user_ids)
            ORDER BY member.user_id
            FOR SHARE OF member
        )
        SELECT COALESCE(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'id', member.id,
                    'project_id', member.project_id,
                    'user_id', member.user_id,
                    'role', member.role,
                    'assigned_by', member.assigned_by,
                    'assigned_at', pg_catalog.to_json(member.assigned_at),
                    'version', member.version,
                    'updated_at', pg_catalog.to_json(member.updated_at),
                    'weekly_target', member.weekly_target
                )
                ORDER BY member.user_id
            ),
            '[]'::jsonb
        )
        INTO v_rows
        FROM selected_members AS member;
    ELSE
        WITH selected_members AS MATERIALIZED (
            SELECT
                member.id,
                member.project_id,
                member.user_id,
                member.role,
                member.assigned_by,
                member.assigned_at,
                member.version,
                member.updated_at,
                member.weekly_target
            FROM public.project_members AS member
            WHERE member.project_id = p_project_id
              AND member.user_id = ANY(p_user_ids)
            ORDER BY member.user_id
        )
        SELECT COALESCE(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'id', member.id,
                    'project_id', member.project_id,
                    'user_id', member.user_id,
                    'role', member.role,
                    'assigned_by', member.assigned_by,
                    'assigned_at', pg_catalog.to_json(member.assigned_at),
                    'version', member.version,
                    'updated_at', pg_catalog.to_json(member.updated_at),
                    'weekly_target', member.weekly_target
                )
                ORDER BY member.user_id
            ),
            '[]'::jsonb
        )
        INTO v_rows
        FROM selected_members AS member;
    END IF;

    RETURN v_rows::text;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_v1.project_member_rows(uuid, uuid[], boolean)
    FROM PUBLIC;
