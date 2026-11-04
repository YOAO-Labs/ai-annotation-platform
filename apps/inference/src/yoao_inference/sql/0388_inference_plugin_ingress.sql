-- Generic Registry writers must never create or mutate plugin-owned rows.
-- The lock is held in the caller's transaction through the legacy write.
CREATE FUNCTION aap_inference_topology_v1._guard_plugin_registry(p_registry_id uuid)
RETURNS void
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_extra jsonb;
BEGIN
    IF p_registry_id IS NULL THEN
        RETURN;
    END IF;
    SELECT registry.extra_params INTO v_extra
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
    FOR UPDATE OF registry;
    IF NOT FOUND THEN
        -- Row locks cannot protect an absent id from a concurrent insert.
        LOCK TABLE public.ml_backend_registry IN SHARE ROW EXCLUSIVE MODE;
        SELECT registry.extra_params INTO v_extra
        FROM public.ml_backend_registry AS registry
        WHERE registry.id = p_registry_id
        FOR UPDATE OF registry;
    END IF;
    IF pg_catalog.jsonb_typeof(v_extra) = 'object'
       AND v_extra ?| ARRAY['plugin', 'plugin_lifecycle'] THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'plugin Registry rows are managed by plugin installation';
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1._guard_plugin_registry(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1._guard_plugin_extra(p_extra jsonb)
RETURNS void
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF pg_catalog.jsonb_typeof(p_extra) = 'object'
       AND p_extra ?| ARRAY['plugin', 'plugin_lifecycle'] THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'plugin Registry parameters are reserved for plugin installation';
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1._guard_plugin_extra(jsonb) FROM PUBLIC;
-- aap-statement-break
ALTER FUNCTION aap_inference_topology_v1.create_manual_registry(uuid, text) RENAME TO create_manual_registry_plugin_legacy;
-- aap-statement-break
ALTER FUNCTION aap_inference_topology_v1.patch_registry(uuid, text) RENAME TO patch_registry_plugin_legacy;
-- aap-statement-break
ALTER FUNCTION aap_inference_topology_v1.delete_registry(uuid, text) RENAME TO delete_registry_plugin_legacy;
-- aap-statement-break
ALTER FUNCTION aap_inference_registry_ingress_v1.create_registry(uuid, text, text, text, boolean, text) RENAME TO create_registry_plugin_legacy;
-- aap-statement-break
ALTER FUNCTION aap_inference_registry_ingress_v1.apply_observation(uuid, text, text, text, boolean, text, timestamptz, text, boolean, text) RENAME TO apply_observation_plugin_legacy;
-- aap-statement-break
ALTER FUNCTION aap_inference_registry_ingress_v1.disconnect_env_registry(uuid, text) RENAME TO disconnect_env_registry_plugin_legacy;
-- aap-statement-break
ALTER FUNCTION aap_inference_registry_ingress_v1.tag_seed_registry(uuid, text) RENAME TO tag_seed_registry_plugin_legacy;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.create_manual_registry(p_registry_id uuid, p_values text)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    IF p_registry_id IS NOT NULL AND p_values IS NOT NULL THEN
        PERFORM aap_inference_topology_v1._guard_plugin_extra(p_values::jsonb -> 'extra_params');
    END IF;
    RETURN aap_inference_topology_v1.create_manual_registry_plugin_legacy(p_registry_id, p_values);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.patch_registry(p_registry_id uuid, p_patch text)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    IF p_registry_id IS NOT NULL AND p_patch IS NOT NULL THEN
        PERFORM aap_inference_topology_v1._guard_plugin_extra(p_patch::jsonb -> 'extra_params');
    END IF;
    PERFORM aap_inference_topology_v1._guard_plugin_registry(p_registry_id);
    RETURN aap_inference_topology_v1.patch_registry_plugin_legacy(p_registry_id, p_patch);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.delete_registry(p_registry_id uuid, p_expected text)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    PERFORM aap_inference_topology_v1._guard_plugin_registry(p_registry_id);
    RETURN aap_inference_topology_v1.delete_registry_plugin_legacy(p_registry_id, p_expected);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION aap_inference_registry_ingress_v1.create_registry(
    p_registry_id uuid, p_name text, p_url text, p_source text,
    p_is_interactive boolean, p_extra_params_json text
)
RETURNS TABLE (
    id uuid, name text, url text, state text, is_interactive boolean,
    auth_method text, auth_token text, extra_params jsonb, gpu_resource_id text,
    vram_budget_mb integer, eviction_priority integer, health_meta jsonb,
    source text, error_message text, last_checked_at timestamptz,
    created_at timestamptz, updated_at timestamptz
)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    IF p_registry_id IS NOT NULL AND p_source IN ('env', 'seed') THEN
        PERFORM aap_inference_topology_v1._guard_plugin_extra(p_extra_params_json::jsonb);
    END IF;
    RETURN QUERY SELECT * FROM aap_inference_registry_ingress_v1.create_registry_plugin_legacy(
        p_registry_id, p_name, p_url, p_source, p_is_interactive, p_extra_params_json
    );
END;
$function$;
-- aap-statement-break
CREATE FUNCTION aap_inference_registry_ingress_v1.apply_observation(
    p_registry_id uuid, p_profile text, p_state text, p_name text,
    p_is_interactive boolean, p_health_meta_json text, p_checked_at timestamptz,
    p_error_message text, p_usable_caps boolean, p_expected_snapshot_json text
)
RETURNS timestamptz
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    PERFORM aap_inference_topology_v1._guard_plugin_registry(p_registry_id);
    RETURN aap_inference_registry_ingress_v1.apply_observation_plugin_legacy(
        p_registry_id, p_profile, p_state, p_name, p_is_interactive,
        p_health_meta_json, p_checked_at, p_error_message, p_usable_caps, p_expected_snapshot_json
    );
END;
$function$;
-- aap-statement-break
CREATE FUNCTION aap_inference_registry_ingress_v1.disconnect_env_registry(p_registry_id uuid, p_expected_url text)
RETURNS timestamptz
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    PERFORM aap_inference_topology_v1._guard_plugin_registry(p_registry_id);
    RETURN aap_inference_registry_ingress_v1.disconnect_env_registry_plugin_legacy(p_registry_id, p_expected_url);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION aap_inference_registry_ingress_v1.tag_seed_registry(p_registry_id uuid, p_marker_json text)
RETURNS TABLE (extra_params jsonb, updated_at timestamptz)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    PERFORM aap_inference_topology_v1._guard_plugin_registry(p_registry_id);
    RETURN QUERY SELECT * FROM aap_inference_registry_ingress_v1.tag_seed_registry_plugin_legacy(p_registry_id, p_marker_json);
END;
$function$;
-- aap-statement-break
-- Copy the existing execute ACL, including PUBLIC and grant options, onto each
-- wrapper, then remove every non-owner grant from the renamed implementation.
-- The owner remains able to execute it solely as the wrapper's definer.
DO $block$
DECLARE
    v_pair record;
    v_acl record;
    v_owner name;
    v_target text;
    v_grantee text;
BEGIN
    FOR v_pair IN
        SELECT * FROM (VALUES
            ('aap_inference_topology_v1.create_manual_registry(uuid,text)', 'aap_inference_topology_v1.create_manual_registry_plugin_legacy(uuid,text)'),
            ('aap_inference_topology_v1.patch_registry(uuid,text)', 'aap_inference_topology_v1.patch_registry_plugin_legacy(uuid,text)'),
            ('aap_inference_topology_v1.delete_registry(uuid,text)', 'aap_inference_topology_v1.delete_registry_plugin_legacy(uuid,text)'),
            ('aap_inference_registry_ingress_v1.create_registry(uuid,text,text,text,boolean,text)', 'aap_inference_registry_ingress_v1.create_registry_plugin_legacy(uuid,text,text,text,boolean,text)'),
            ('aap_inference_registry_ingress_v1.apply_observation(uuid,text,text,text,boolean,text,timestamptz,text,boolean,text)', 'aap_inference_registry_ingress_v1.apply_observation_plugin_legacy(uuid,text,text,text,boolean,text,timestamptz,text,boolean,text)'),
            ('aap_inference_registry_ingress_v1.disconnect_env_registry(uuid,text)', 'aap_inference_registry_ingress_v1.disconnect_env_registry_plugin_legacy(uuid,text)'),
            ('aap_inference_registry_ingress_v1.tag_seed_registry(uuid,text)', 'aap_inference_registry_ingress_v1.tag_seed_registry_plugin_legacy(uuid,text)')
        ) AS pairs(wrapper, legacy)
    LOOP
        SELECT pg_catalog.pg_get_userbyid(p.proowner) INTO v_owner
        FROM pg_catalog.pg_proc AS p WHERE p.oid = v_pair.legacy::regprocedure;
        v_target := v_pair.wrapper::regprocedure::text;
        EXECUTE pg_catalog.format('ALTER FUNCTION %s OWNER TO %I', v_target, v_owner);
        EXECUTE pg_catalog.format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', v_target);
        FOR v_acl IN
            SELECT DISTINCT acl.grantee
            FROM pg_catalog.pg_proc AS p,
                 LATERAL pg_catalog.aclexplode(COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))) AS acl
            WHERE p.oid = v_pair.wrapper::regprocedure AND acl.grantee <> p.proowner
        LOOP
            v_grantee := CASE WHEN v_acl.grantee = 0 THEN 'PUBLIC'
                ELSE pg_catalog.quote_ident(pg_catalog.pg_get_userbyid(v_acl.grantee)) END;
            EXECUTE pg_catalog.format('REVOKE ALL ON FUNCTION %s FROM %s CASCADE', v_target, v_grantee);
        END LOOP;
        EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION aap_inference_topology_v1._guard_plugin_registry(uuid) TO %I', v_owner);
        EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION aap_inference_topology_v1._guard_plugin_extra(jsonb) TO %I', v_owner);
        FOR v_acl IN
            SELECT acl.grantee, acl.is_grantable
            FROM pg_catalog.pg_proc AS p,
                 LATERAL pg_catalog.aclexplode(COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))) AS acl
            WHERE p.oid = v_pair.legacy::regprocedure AND acl.privilege_type = 'EXECUTE'
        LOOP
            v_grantee := CASE WHEN v_acl.grantee = 0 THEN 'PUBLIC'
                ELSE pg_catalog.quote_ident(pg_catalog.pg_get_userbyid(v_acl.grantee)) END;
            EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION %s TO %s%s',
                v_target, v_grantee, CASE WHEN v_acl.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END);
        END LOOP;
        FOR v_acl IN
            SELECT DISTINCT acl.grantee
            FROM pg_catalog.pg_proc AS p,
                 LATERAL pg_catalog.aclexplode(COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))) AS acl
            WHERE p.oid = v_pair.legacy::regprocedure AND acl.grantee <> p.proowner
        LOOP
            v_grantee := CASE WHEN v_acl.grantee = 0 THEN 'PUBLIC'
                ELSE pg_catalog.quote_ident(pg_catalog.pg_get_userbyid(v_acl.grantee)) END;
            EXECUTE pg_catalog.format('REVOKE ALL ON FUNCTION %s FROM %s CASCADE', v_pair.legacy::regprocedure, v_grantee);
        END LOOP;
    END LOOP;
END;
$block$;
