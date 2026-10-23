-- Store one database-local key for transaction-bound Backend probe subjects.
CREATE SCHEMA aap_inference_backend_probe_v1;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_inference_backend_probe_v1 FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_inference_backend_probe_v1.probe_secret (
    singleton boolean PRIMARY KEY CHECK (singleton),
    secret bytea NOT NULL CHECK (pg_catalog.octet_length(secret) = 32)
);
-- aap-statement-break
INSERT INTO aap_inference_backend_probe_v1.probe_secret(singleton, secret)
VALUES (true, public.gen_random_bytes(32));
-- aap-statement-break
REVOKE ALL ON aap_inference_backend_probe_v1.probe_secret FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_backend_probe_v1.backend_probe_policy()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-inference-backend-probe-native/v1',
        'probe_protocol', 'aap-inference-backend-probe/v1',
        'subject_protocol', 'aap-inference-backend-probe-subject/v1',
        'origin_transaction', 'in_progress',
        'functions', pg_catalog.jsonb_build_array(
            'aap_inference_backend_probe_v1.backend_probe_policy()',
            'aap_inference_backend_probe_v1.open_backend_probe_subject(text,uuid)'
        ),
        'ready', EXISTS (
            SELECT 1
            FROM aap_inference_backend_probe_v1.probe_secret AS secret
            WHERE secret.singleton IS TRUE
              AND pg_catalog.octet_length(secret.secret) = 32
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_backend_probe_v1.backend_probe_policy()
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION
    aap_inference_backend_probe_v1.open_backend_probe_subject(
        p_subject text,
        p_registry_id uuid
    )
RETURNS text
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_secret bytea;
    v_ciphertext bytea;
    v_plaintext text;
    v_header_end integer;
    v_header jsonb;
    v_backend_json text;
    v_backend_chars integer;
    v_origin_xid xid8;
BEGIN
    IF p_subject IS NULL
       OR p_registry_id IS NULL
       OR pg_catalog.octet_length(p_subject) > 16777216
       OR p_subject !~ '^v1\.[A-Za-z0-9+/]+={0,2}$' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid backend probe subject';
    END IF;

    SELECT secret.secret
    INTO v_secret
    FROM aap_inference_backend_probe_v1.probe_secret AS secret
    WHERE secret.singleton IS TRUE;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'backend probe capability is unavailable';
    END IF;

    BEGIN
        v_ciphertext := pg_catalog.decode(pg_catalog.substr(p_subject, 4), 'base64');
        v_plaintext := public.pgp_sym_decrypt(
            v_ciphertext,
            pg_catalog.encode(v_secret, 'hex')
        );
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid backend probe subject';
    END;

    BEGIN
        v_header_end := pg_catalog.strpos(v_plaintext, E'\n');
        IF v_header_end < 2 THEN
            RAISE EXCEPTION USING ERRCODE = '22023';
        END IF;

        v_header := pg_catalog.substr(v_plaintext, 1, v_header_end - 1)::jsonb;
        v_backend_json := pg_catalog.substr(v_plaintext, v_header_end + 1);

        IF pg_catalog.jsonb_typeof(v_header) IS DISTINCT FROM 'object'
           OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(v_header)) <> 5
           OR NOT (v_header ?& ARRAY[
               'schema', 'database', 'registry_id', 'origin_xid', 'backend_chars'
           ]) THEN
            RAISE EXCEPTION USING ERRCODE = '22023';
        END IF;

        v_origin_xid := (v_header ->> 'origin_xid')::xid8;
        v_backend_chars := (v_header ->> 'backend_chars')::integer;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid backend probe subject';
    END;

    IF v_header ->> 'schema' IS DISTINCT FROM
           'aap-inference-backend-probe-subject/v1'
       OR v_header ->> 'database' IS DISTINCT FROM pg_catalog.current_database()
       OR v_header ->> 'registry_id' IS DISTINCT FROM p_registry_id::text
       OR v_backend_chars < 2
       OR pg_catalog.char_length(v_backend_json) <> v_backend_chars
       OR pg_catalog.left(v_backend_json, 1) <> '{'
       OR pg_catalog.right(v_backend_json, 1) <> '}'
       OR pg_catalog.pg_xact_status(v_origin_xid) IS DISTINCT FROM 'in progress' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid backend probe subject';
    END IF;

    RETURN v_backend_json;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_backend_probe_v1.open_backend_probe_subject(text, uuid)
    FROM PUBLIC;
-- aap-statement-break
COMMENT ON TABLE aap_inference_backend_probe_v1.probe_secret
    IS 'Database-local key for authenticated, in-progress transaction Backend probe snapshots.';
-- aap-statement-break
COMMENT ON FUNCTION aap_inference_backend_probe_v1.backend_probe_policy()
    IS 'Secret-free readiness and protocol metadata for the fixed Backend probe capability.';
-- aap-statement-break
COMMENT ON FUNCTION
    aap_inference_backend_probe_v1.open_backend_probe_subject(text, uuid)
    IS 'Opens an authenticated Backend snapshot only while its originating transaction remains in progress.';
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_v1.workspace_configuration_backend(
    p_registry_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_backend record;
    v_backend_value jsonb;
    v_secret bytea;
    v_origin_xid xid8;
    v_backend_json text;
    v_plaintext text;
    v_ciphertext bytea;
    v_subject text;
BEGIN
    IF p_registry_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace configuration backend';
    END IF;

    SELECT registry.id,
           registry.url,
           registry.auth_method,
           registry.auth_token,
           registry.extra_params
    INTO v_backend
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id;

    IF FOUND THEN
        v_backend_value := pg_catalog.jsonb_build_object(
            'id', v_backend.id,
            'url', v_backend.url,
            'auth_method', v_backend.auth_method,
            'auth_token', v_backend.auth_token,
            'extra_params', v_backend.extra_params
        );

        SELECT secret.secret
        INTO v_secret
        FROM aap_inference_backend_probe_v1.probe_secret AS secret
        WHERE secret.singleton IS TRUE;

        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                ERRCODE = '55000',
                MESSAGE = 'backend probe capability is unavailable';
        END IF;

        v_origin_xid := pg_catalog.pg_current_xact_id();
        v_backend_json := v_backend_value::text;
        v_plaintext := pg_catalog.jsonb_build_object(
            'schema', 'aap-inference-backend-probe-subject/v1',
            'database', pg_catalog.current_database(),
            'registry_id', p_registry_id,
            'origin_xid', v_origin_xid::text,
            'backend_chars', pg_catalog.char_length(v_backend_json)
        )::text || E'\n' || v_backend_json;
        v_ciphertext := public.pgp_sym_encrypt(
            v_plaintext,
            pg_catalog.encode(v_secret, 'hex'),
            'cipher-algo=aes256, compress-algo=0'
        );
        v_subject := 'v1.' || pg_catalog.replace(
            pg_catalog.encode(v_ciphertext, 'base64'),
            E'\n',
            ''
        );
        v_backend_value := pg_catalog.jsonb_build_object(
            'id', v_backend.id,
            'probe_subject', v_subject
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-inference-workspace-configuration/v1',
        'registry_id', p_registry_id,
        'backend', v_backend_value
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_v1.workspace_configuration_backend(uuid)
    FROM PUBLIC;
