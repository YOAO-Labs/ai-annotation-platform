ALTER FUNCTION aap_workspace_project_query_v1.project_query_step(
    jsonb, text, jsonb, text, text, text[], text[], uuid, text,
    timestamptz, timestamptz, bigint, integer
) RENAME TO _project_query_step_impl;
-- aap-statement-break
CREATE FUNCTION aap_workspace_project_query_v1.project_query_step(
    p_request jsonb,
    p_action text,
    p_parameters jsonb,
    p_status text,
    p_search text,
    p_type_keys text[],
    p_data_types text[],
    p_member_id uuid,
    p_project_role text,
    p_created_from timestamptz,
    p_created_to timestamptz,
    p_offset bigint,
    p_limit integer
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_reply jsonb;
    v_member jsonb;
    v_assigned_at timestamptz;
    v_updated_at timestamptz;
BEGIN
    v_reply := aap_workspace_project_query_v1._project_query_step_impl(
        p_request,
        p_action,
        p_parameters,
        p_status,
        p_search,
        p_type_keys,
        p_data_types,
        p_member_id,
        p_project_role,
        p_created_from,
        p_created_to,
        p_offset,
        p_limit
    );

    v_member := v_reply #> '{facts,member}';
    IF p_action = 'member_access'
       AND pg_catalog.jsonb_typeof(v_member) = 'object' THEN
        -- A STABLE wrapper shares the original membership-read statement snapshot.
        SELECT membership.assigned_at, membership.updated_at
        INTO v_assigned_at, v_updated_at
        FROM public.project_members AS membership
        WHERE membership.id = (v_member->>'id')::uuid
          AND membership.project_id = (v_member->>'project_id')::uuid
          AND membership.user_id = (v_member->>'user_id')::uuid;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'XX000',
                MESSAGE = 'Project member authority row changed within a stable snapshot';
        END IF;

        IF (
            v_assigned_at IS NOT NULL
            AND pg_catalog.isfinite(v_assigned_at)
            AND (
                v_assigned_at < TIMESTAMPTZ '0001-01-01 00:00:00+00'
                OR v_assigned_at > TIMESTAMPTZ '9999-12-31 23:59:59.999999+00'
            )
        ) OR (
            v_updated_at IS NOT NULL
            AND pg_catalog.isfinite(v_updated_at)
            AND (
                v_updated_at < TIMESTAMPTZ '0001-01-01 00:00:00+00'
                OR v_updated_at > TIMESTAMPTZ '9999-12-31 23:59:59.999999+00'
            )
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22008',
                MESSAGE = 'Project member timestamp is outside the Python datetime range';
        END IF;
    END IF;

    RETURN v_reply;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_project_query_v1.project_query_step(
        jsonb, text, jsonb, text, text, text[], text[], uuid, text,
        timestamptz, timestamptz, bigint, integer
    )
    FROM PUBLIC;
