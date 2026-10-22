CREATE FUNCTION aap_identity_v1.read_scale_seed_super_admin()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL RESTRICTED
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-scale-seed-actors/v1',
        'kind', 'super_admin',
        'account_id', (
            SELECT account.id
            FROM public.users AS account
            WHERE account.role = 'super_admin'
            ORDER BY account.created_at
            LIMIT 1
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_identity_v1.read_scale_seed_super_admin()
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_identity_v1.read_scale_seed_annotator(
    p_candidate_ids uuid[]
)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL RESTRICTED
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-identity-scale-seed-actors/v1',
        'kind', 'annotator',
        'account_id', (
            SELECT account.id
            FROM public.users AS account
            WHERE account.id = ANY (p_candidate_ids)
              AND account.is_active IS TRUE
            ORDER BY account.created_at
            LIMIT 1
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_identity_v1.read_scale_seed_annotator(uuid[])
    FROM PUBLIC;
