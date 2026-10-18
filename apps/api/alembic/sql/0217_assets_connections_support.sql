-- Settings owns the effective connector host policy and fences absent-setting
-- inserts during a Source mutation's short final application transaction.
CREATE FUNCTION aap_settings_v1.lock_connector_host_allowlist(p_fallback jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    LOCK TABLE public.system_settings IN SHARE MODE NOWAIT;
    RETURN aap_settings_v1.resolve_connector_host_allowlist(p_fallback);
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-settings-connector-allowlist/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'policy_busy')
        );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_settings_v1.lock_connector_host_allowlist(jsonb) FROM PUBLIC;
-- aap-statement-break

-- Audit is written by its owner in the same transaction as the Source receipt.
CREATE FUNCTION aap_audit_v1.record_storage_connection_change(
    p_operation_id uuid,
    p_actor_id uuid,
    p_action text,
    p_connection_id uuid,
    p_context jsonb,
    p_detail jsonb
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_audit_id bigint;
    v_status integer;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_action IS NULL
       OR p_action NOT IN ('create', 'update', 'delete', 'test')
       OR p_connection_id IS NULL
       OR pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object'
       OR NOT aap_audit_v1.valid_request_context(p_context) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid storage connection audit metadata';
    END IF;

    v_actor := aap_identity_v1.lock_audit_actor(p_actor_id);
    IF v_actor IS NULL
       OR pg_catalog.jsonb_typeof(v_actor) IS DISTINCT FROM 'object'
       OR (v_actor - ARRAY['schema', 'ok', 'data']) IS DISTINCT FROM '{}'::jsonb
       OR v_actor ->> 'schema' IS DISTINCT FROM 'aap-audit-actor/v1'
       OR v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_actor -> 'data') IS DISTINCT FROM 'object'
       OR ((v_actor -> 'data') - ARRAY['actor_id', 'email', 'platform_role']) IS DISTINCT FROM '{}'::jsonb
       OR v_actor #>> '{data,actor_id}' IS DISTINCT FROM p_actor_id::text
       OR pg_catalog.jsonb_typeof(v_actor #> '{data,email}') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(v_actor #>> '{data,email}') NOT BETWEEN 1 AND 255
       OR pg_catalog.jsonb_typeof(v_actor #> '{data,platform_role}') IS DISTINCT FROM 'string'
       OR v_actor #>> '{data,platform_role}' NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'storage connection audit actor unavailable';
    END IF;

    v_status := CASE p_action
        WHEN 'create' THEN 201
        WHEN 'delete' THEN 204
        ELSE 200
    END;
    INSERT INTO public.audit_logs (
        actor_id, actor_email, actor_role, action, target_type, target_id,
        method, path, status_code, ip, request_id, detail_json
    ) VALUES (
        p_actor_id, v_actor #>> '{data,email}', v_actor #>> '{data,platform_role}',
        'storage_connection.' || p_action, 'storage_connection', p_connection_id::text,
        p_context ->> 'method', p_context ->> 'path', v_status,
        NULLIF(p_context ->> 'ip', '')::pg_catalog.inet,
        p_context ->> 'request_id',
        pg_catalog.jsonb_build_object('operation_id', p_operation_id) || p_detail
    ) RETURNING id INTO v_audit_id;
    RETURN v_audit_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.record_storage_connection_change(uuid, uuid, text, uuid, jsonb, jsonb) FROM PUBLIC;
