-- Decisions are distinct from immutable Registry installation and lifecycle facts.
CREATE TABLE aap_inference_topology_v1.plugin_asset_approvals (
    registry_id uuid PRIMARY KEY,
    revision integer NOT NULL CHECK (revision > 0),
    decision jsonb NOT NULL
);
-- aap-statement-break
REVOKE ALL ON aap_inference_topology_v1.plugin_asset_approvals FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.plugin_asset_approval(p_id uuid)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT jsonb_build_object('revision', COALESCE(a.revision, 0), 'decision', a.decision)
    FROM public.ml_backend_registry r
    LEFT JOIN aap_inference_topology_v1.plugin_asset_approvals a ON a.registry_id = r.id
    WHERE r.id = p_id AND r.extra_params ? 'plugin'
$function$;
-- aap-statement-break
CREATE FUNCTION aap_inference_topology_v1.set_plugin_asset_approval(p_id uuid, p_expected integer, p_decision text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE r public.ml_backend_registry%ROWTYPE; d jsonb; old_revision integer; manifest jsonb; launch jsonb;
BEGIN
    IF p_id IS NULL OR p_expected IS NULL OR p_expected < 0 OR p_decision IS NULL OR octet_length(p_decision) > 1048576 THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid asset approval';
    END IF;
    d := p_decision::jsonb;
    SELECT * INTO r FROM public.ml_backend_registry WHERE id=p_id FOR UPDATE;
    IF NOT FOUND OR NOT (r.extra_params ? 'plugin') OR r.extra_params #>> '{plugin,status}' IS DISTINCT FROM 'active' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='plugin installation unavailable';
    END IF;
    manifest := r.extra_params #> '{plugin,manifest}';
    launch := r.extra_params #> '{plugin_lifecycle,launch}';
    IF jsonb_typeof(d) IS DISTINCT FROM 'object'
       OR d->>'expected_revision' IS DISTINCT FROM p_expected::text
       OR d->>'expected_installation_revision' IS DISTINCT FROM r.extra_params #>> '{plugin,revision}'
       OR d #>> '{descriptor,plugin_id}' IS DISTINCT FROM manifest->>'plugin_id'
       OR d #>> '{descriptor,plugin_version}' IS DISTINCT FROM manifest->>'version'
       OR d #>> '{descriptor,artifact}' IS DISTINCT FROM manifest->>'artifact'
       OR d #>> '{descriptor,asset_manifest_sha256}' IS DISTINCT FROM manifest->>'asset_manifest_sha256'
       OR d->>'container_id' IS DISTINCT FROM launch->>'container_id'
       OR d->>'image_id' IS DISTINCT FROM launch->>'image_id'
       OR jsonb_typeof(d->'approved') IS DISTINCT FROM 'array'
       OR jsonb_typeof(d->'inventory') IS DISTINCT FROM 'array'
       OR COALESCE(d->>'descriptor_sha256','') !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='asset approval domain mismatch';
    END IF;
    SELECT revision INTO old_revision FROM aap_inference_topology_v1.plugin_asset_approvals WHERE registry_id=p_id FOR UPDATE;
    IF COALESCE(old_revision,0) <> p_expected THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='stale asset approval revision';
    END IF;
    INSERT INTO aap_inference_topology_v1.plugin_asset_approvals(registry_id,revision,decision)
    VALUES (p_id,p_expected+1,d)
    ON CONFLICT (registry_id) DO UPDATE SET revision=EXCLUDED.revision,decision=EXCLUDED.decision;
    RETURN jsonb_build_object('revision',p_expected+1,'decision',d);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.plugin_asset_approval(uuid) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_topology_v1.set_plugin_asset_approval(uuid,integer,text) FROM PUBLIC;
-- aap-statement-break
DO $grant$
DECLARE topology_owner name; endpoint_owner name; grantee name;
BEGIN
    SELECT pg_get_userbyid(proowner) INTO topology_owner FROM pg_proc
    WHERE oid='aap_inference_topology_v1.command_plugin_installation(uuid,integer,text,text)'::regprocedure;
    SELECT pg_get_userbyid(proowner) INTO endpoint_owner FROM pg_proc
    WHERE oid='aap_inference_backend_endpoint_v1.open_execution_endpoint(text,uuid,uuid,text)'::regprocedure;
    EXECUTE format('ALTER TABLE aap_inference_topology_v1.plugin_asset_approvals OWNER TO %I', topology_owner);
    EXECUTE format('ALTER FUNCTION aap_inference_topology_v1.plugin_asset_approval(uuid) OWNER TO %I', topology_owner);
    EXECUTE format('ALTER FUNCTION aap_inference_topology_v1.set_plugin_asset_approval(uuid,integer,text) OWNER TO %I', topology_owner);
    EXECUTE format('GRANT USAGE ON SCHEMA aap_inference_topology_v1 TO %I', endpoint_owner);
    EXECUTE format('GRANT EXECUTE ON FUNCTION aap_inference_topology_v1.plugin_asset_approval(uuid) TO %I', endpoint_owner);
    FOR grantee IN SELECT DISTINCT pg_get_userbyid(a.grantee) FROM pg_proc p,
        LATERAL aclexplode(coalesce(p.proacl, acldefault('f',p.proowner))) a
        WHERE p.oid='aap_inference_topology_v1.command_plugin_installation(uuid,integer,text,text)'::regprocedure
          AND a.privilege_type='EXECUTE' AND a.grantee<>0 LOOP
        EXECUTE format('GRANT EXECUTE ON FUNCTION aap_inference_topology_v1.plugin_asset_approval(uuid) TO %I', grantee);
        EXECUTE format('GRANT EXECUTE ON FUNCTION aap_inference_topology_v1.set_plugin_asset_approval(uuid,integer,text) TO %I', grantee);
    END LOOP;
END
$grant$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_backend_endpoint_v1.open_execution_endpoint(
    p_subject text, p_registry_id uuid, p_request_id uuid, p_authority_json text
) RETURNS text LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp SET TimeZone TO 'UTC' SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3 AS $function$
    SELECT (aap_inference_backend_endpoint_v1._open_endpoint_for_purpose(
        p_subject,p_registry_id,p_request_id,'execution',p_authority_json)::jsonb ||
        jsonb_build_object('plugin_asset_approval',
            aap_inference_topology_v1.plugin_asset_approval(p_registry_id)))::text
$function$;
