-- Restore the caller-facing ACL to each original implementation before
-- removing its guard wrapper and returning the original name.
DO $block$
DECLARE
    v_pair record;
    v_acl record;
    v_owner name;
    v_legacy text;
    v_grantee text;
BEGIN
    FOR v_pair IN
        SELECT * FROM (VALUES
            ('aap_inference_topology_v1.create_manual_registry(uuid,text)', 'aap_inference_topology_v1.create_manual_registry_plugin_legacy(uuid,text)', 'create_manual_registry'),
            ('aap_inference_topology_v1.patch_registry(uuid,text)', 'aap_inference_topology_v1.patch_registry_plugin_legacy(uuid,text)', 'patch_registry'),
            ('aap_inference_topology_v1.delete_registry(uuid,text)', 'aap_inference_topology_v1.delete_registry_plugin_legacy(uuid,text)', 'delete_registry'),
            ('aap_inference_registry_ingress_v1.create_registry(uuid,text,text,text,boolean,text)', 'aap_inference_registry_ingress_v1.create_registry_plugin_legacy(uuid,text,text,text,boolean,text)', 'create_registry'),
            ('aap_inference_registry_ingress_v1.apply_observation(uuid,text,text,text,boolean,text,timestamptz,text,boolean,text)', 'aap_inference_registry_ingress_v1.apply_observation_plugin_legacy(uuid,text,text,text,boolean,text,timestamptz,text,boolean,text)', 'apply_observation'),
            ('aap_inference_registry_ingress_v1.disconnect_env_registry(uuid,text)', 'aap_inference_registry_ingress_v1.disconnect_env_registry_plugin_legacy(uuid,text)', 'disconnect_env_registry'),
            ('aap_inference_registry_ingress_v1.tag_seed_registry(uuid,text)', 'aap_inference_registry_ingress_v1.tag_seed_registry_plugin_legacy(uuid,text)', 'tag_seed_registry')
        ) AS pairs(wrapper, legacy, original_name)
    LOOP
        SELECT pg_catalog.pg_get_userbyid(p.proowner) INTO v_owner
        FROM pg_catalog.pg_proc AS p WHERE p.oid = v_pair.wrapper::regprocedure;
        v_legacy := v_pair.legacy::regprocedure::text;
        EXECUTE pg_catalog.format('ALTER FUNCTION %s OWNER TO %I', v_legacy, v_owner);
        FOR v_acl IN
            SELECT DISTINCT acl.grantee
            FROM pg_catalog.pg_proc AS p,
                 LATERAL pg_catalog.aclexplode(COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))) AS acl
            WHERE p.oid = v_pair.legacy::regprocedure AND acl.grantee <> p.proowner
        LOOP
            v_grantee := CASE WHEN v_acl.grantee = 0 THEN 'PUBLIC'
                ELSE pg_catalog.quote_ident(pg_catalog.pg_get_userbyid(v_acl.grantee)) END;
            EXECUTE pg_catalog.format('REVOKE ALL ON FUNCTION %s FROM %s CASCADE', v_legacy, v_grantee);
        END LOOP;
        FOR v_acl IN
            SELECT acl.grantee, acl.is_grantable
            FROM pg_catalog.pg_proc AS p,
                 LATERAL pg_catalog.aclexplode(COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))) AS acl
            WHERE p.oid = v_pair.wrapper::regprocedure AND acl.privilege_type = 'EXECUTE'
        LOOP
            v_grantee := CASE WHEN v_acl.grantee = 0 THEN 'PUBLIC'
                ELSE pg_catalog.quote_ident(pg_catalog.pg_get_userbyid(v_acl.grantee)) END;
            EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION %s TO %s%s',
                v_legacy, v_grantee, CASE WHEN v_acl.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END);
        END LOOP;
        EXECUTE pg_catalog.format('DROP FUNCTION %s', v_pair.wrapper::regprocedure);
        EXECUTE pg_catalog.format('ALTER FUNCTION %s RENAME TO %I', v_legacy, v_pair.original_name);
    END LOOP;
END;
$block$;
-- aap-statement-break
DROP FUNCTION aap_inference_topology_v1._guard_plugin_registry(uuid);
-- aap-statement-break
DROP FUNCTION aap_inference_topology_v1._guard_plugin_extra(jsonb);
