-- Fixed Audit capability for the operator bootstrap command. Identity invokes
-- this on the same caller transaction after inserting the bootstrap account.
CREATE FUNCTION aap_audit_v1.record_identity_bootstrap_admin(
    p_actor_id uuid,
    p_email text,
    p_request_id text
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_audit_id bigint;
BEGIN
    IF p_actor_id IS NULL OR p_email IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid_request';
    END IF;

    INSERT INTO public.audit_logs (
        actor_id, actor_email, actor_role, action, target_type, target_id,
        method, path, status_code, ip, request_id, detail_json
    ) VALUES (
        p_actor_id, p_email, 'super_admin', 'system.bootstrap_admin',
        'user', p_actor_id::text,
        NULL, NULL, NULL, NULL, p_request_id,
        pg_catalog.jsonb_build_object('email', p_email)
    )
    RETURNING id INTO v_audit_id;

    RETURN v_audit_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.record_identity_bootstrap_admin(uuid, text, text)
    FROM PUBLIC;
