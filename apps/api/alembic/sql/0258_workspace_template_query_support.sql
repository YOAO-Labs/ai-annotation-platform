-- Return only current organization memberships for Workspace template queries.
CREATE FUNCTION aap_identity_v1.workspace_template_query_org_ids(
    p_actor_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_organization_ids jsonb;
BEGIN
    IF p_actor_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace template query actor';
    END IF;

    SELECT COALESCE(
        pg_catalog.jsonb_agg(pg_catalog.to_jsonb(membership.organization_id)),
        '[]'::jsonb
    )
    INTO v_organization_ids
    FROM public.organization_members AS membership
    WHERE membership.user_id = p_actor_id
      AND membership.deleted_at IS NULL;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-workspace-template-query/v1',
        'actor_id', p_actor_id,
        'organization_ids', v_organization_ids
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_identity_v1.workspace_template_query_org_ids(uuid)
    FROM PUBLIC;
