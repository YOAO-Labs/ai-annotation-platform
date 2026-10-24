CREATE FUNCTION aap_workspace_v1.pin_seed_member_assignment(
    p_member_id uuid,
    p_assigned_at timestamptz
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_assigned_at timestamptz;
    v_updated_at timestamptz;
    v_found boolean;
BEGIN
    IF p_member_id IS NULL OR p_assigned_at IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_seed_assignment';
    END IF;

    UPDATE public.project_members AS member
    SET assigned_at = p_assigned_at,
        updated_at = pg_catalog.now()
    WHERE member.id = p_member_id
    RETURNING member.assigned_at, member.updated_at
    INTO v_assigned_at, v_updated_at;
    v_found := FOUND;

    IF NOT v_found THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-seed-member-assignment/v1',
            'member_id', p_member_id,
            'found', false,
            'assigned_at', NULL,
            'updated_at', NULL
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-seed-member-assignment/v1',
        'member_id', p_member_id,
        'found', true,
        'assigned_at', v_assigned_at,
        'updated_at', v_updated_at
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_v1.pin_seed_member_assignment(uuid, timestamptz)
    FROM PUBLIC;
