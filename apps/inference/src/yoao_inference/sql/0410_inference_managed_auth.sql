-- Credential-free authority for a managed-auth handshake; never returns tokens.
CREATE FUNCTION aap_inference_topology_v1.registry_auth_snapshot(p_registry_id uuid)
RETURNS jsonb LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'url', registry.url,
        'auth_method', registry.auth_method,
        'auth_revision', registry.auth_revision
    )
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.registry_auth_snapshot(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.patch_registry_auth_cas(
    p_registry_id uuid, p_patch text, p_expected_url text, p_expected_revision bigint
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_url text;
    v_revision bigint;
BEGIN
    IF p_registry_id IS NULL OR p_patch IS NULL OR p_expected_url IS NULL
       OR p_expected_revision IS NULL OR p_expected_revision < 0 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Registry auth snapshot';
    END IF;
    SELECT registry.url, registry.auth_revision INTO v_url, v_revision
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF v_url IS DISTINCT FROM p_expected_url OR v_revision IS DISTINCT FROM p_expected_revision THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001',
            MESSAGE = 'aap_inference_topology:registry_auth_changed:Registry endpoint or authentication changed during update:';
    END IF;
    RETURN aap_inference_topology_v1.patch_registry(p_registry_id, p_patch);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.patch_registry_auth_cas(uuid,text,text,bigint) FROM PUBLIC;
-- aap-statement-break
-- The old implementation remains callable only by its owner through the fenced entrypoint.
REVOKE ALL ON FUNCTION aap_inference_topology_v1.patch_registry(uuid,text) FROM PUBLIC;
-- aap-statement-break
DO $revoke_unfenced_patch$
DECLARE
    v_role record;
BEGIN
    FOR v_role IN
        SELECT role.rolname
        FROM pg_catalog.pg_proc AS proc
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            COALESCE(proc.proacl, pg_catalog.acldefault('f', proc.proowner))
        ) AS grant_row
        JOIN pg_catalog.pg_roles AS role ON role.oid = grant_row.grantee
        WHERE proc.oid = 'aap_inference_topology_v1.patch_registry(uuid,text)'::pg_catalog.regprocedure
          AND grant_row.grantee <> proc.proowner
          AND grant_row.grantee <> (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user)
          AND grant_row.privilege_type = 'EXECUTE'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE EXECUTE ON FUNCTION aap_inference_topology_v1.patch_registry(uuid,text) FROM %I',
            v_role.rolname
        );
    END LOOP;
END;
$revoke_unfenced_patch$;
-- aap-statement-break
DO $publish_fenced_topology_policy$
DECLARE
    v_definition text := pg_catalog.pg_get_functiondef(
        'aap_inference_topology_v1.topology_policy()'::pg_catalog.regprocedure
    );
    v_old text := pg_catalog.quote_literal(
        'aap_inference_topology_v1.patch_registry(uuid,text)'
    );
    v_new text := pg_catalog.quote_literal(
        'aap_inference_topology_v1.registry_auth_snapshot(uuid)'
    ) || ',' || pg_catalog.quote_literal(
        'aap_inference_topology_v1.patch_registry_auth_cas(uuid,text,text,bigint)'
    );
BEGIN
    IF pg_catalog.strpos(v_definition, v_old) = 0 THEN
        RAISE EXCEPTION 'Expected Registry write capability is absent from topology policy';
    END IF;
    EXECUTE pg_catalog.replace(v_definition, v_old, v_new);
END;
$publish_fenced_topology_policy$;
