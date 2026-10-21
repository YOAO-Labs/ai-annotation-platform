-- Seal the health-observation input and response to one live caller transaction.
CREATE SCHEMA aap_inference_health_observation_v1;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_inference_health_observation_v1 FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_inference_health_observation_v1.observation_secret (
    singleton boolean PRIMARY KEY CHECK (singleton),
    secret bytea NOT NULL CHECK (pg_catalog.octet_length(secret) = 32)
);
-- aap-statement-break
INSERT INTO aap_inference_health_observation_v1.observation_secret (
    singleton,
    secret
)
VALUES (true, public.gen_random_bytes(32));
-- aap-statement-break
REVOKE ALL ON aap_inference_health_observation_v1.observation_secret FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION
    aap_inference_health_observation_v1.health_observation_policy()
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
        'schema', 'aap-inference-health-observation-native/v1',
        'observation_protocol', 'aap-inference-health-observation/v1',
        'subject_protocol', 'aap-inference-health-observation-subject/v1',
        'evidence_protocol', 'aap-inference-health-observation-evidence/v1',
        'origin_transaction', 'in_progress',
        'evidence_protection', 'pgcrypto-pgp-aes256-mdc',
        'functions', pg_catalog.jsonb_build_array(
            'aap_inference_health_observation_v1.health_observation_policy()',
            'aap_inference_health_observation_v1.capture_health_observation_subject(uuid,uuid,text,text,double precision)',
            'aap_inference_health_observation_v1.open_health_observation_subject(text,uuid,uuid)',
            'aap_inference_health_observation_v1.seal_health_observation(text,uuid,uuid,text)',
            'aap_inference_health_observation_v1.verify_health_observation(text,uuid,uuid,text)'
        ),
        'ready', EXISTS (
            SELECT 1
            FROM aap_inference_health_observation_v1.observation_secret AS secret
            WHERE secret.singleton IS TRUE
              AND pg_catalog.octet_length(secret.secret) = 32
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_health_observation_v1.health_observation_policy()
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION
    aap_inference_health_observation_v1.capture_health_observation_subject(
        p_registry_id uuid,
        p_request_id uuid,
        p_backend_json text,
        p_gpu_health_challenge text,
        p_timeout_seconds double precision
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
    v_backend jsonb;
    v_header jsonb;
    v_plaintext text;
    v_ciphertext bytea;
    v_vram_budget integer;
BEGIN
    IF p_registry_id IS NULL
       OR p_request_id IS NULL
       OR p_backend_json IS NULL
       OR p_gpu_health_challenge IS NOT NULL
          AND p_gpu_health_challenge !~ '^[0-9a-f]{64}$'
       OR p_timeout_seconds IS NULL
       OR p_timeout_seconds::text IN ('NaN', 'Infinity', '-Infinity')
       OR pg_catalog.octet_length(p_backend_json) > 16777216 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid health observation subject';
    END IF;

    BEGIN
        v_backend := p_backend_json::jsonb;
        IF pg_catalog.jsonb_typeof(v_backend) IS DISTINCT FROM 'object'
           OR (
               SELECT pg_catalog.count(*)
               FROM pg_catalog.jsonb_object_keys(v_backend)
           ) <> 7
           OR NOT (v_backend ?& ARRAY[
               'id',
               'url',
               'auth_method',
               'auth_token',
               'gpu_resource_id',
               'vram_budget_mb',
               'extra_params'
           ])
           OR pg_catalog.jsonb_typeof(v_backend -> 'id') IS DISTINCT FROM 'string'
           OR v_backend ->> 'id' IS DISTINCT FROM p_registry_id::text
           OR pg_catalog.jsonb_typeof(v_backend -> 'url') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_backend -> 'auth_method')
               NOT IN ('string', 'null')
           OR pg_catalog.jsonb_typeof(v_backend -> 'auth_token')
               NOT IN ('string', 'null')
           OR pg_catalog.jsonb_typeof(v_backend -> 'gpu_resource_id')
               NOT IN ('string', 'null') THEN
            RAISE EXCEPTION USING ERRCODE = '22023';
        END IF;

        IF pg_catalog.jsonb_typeof(v_backend -> 'vram_budget_mb') = 'null' THEN
            v_vram_budget := NULL;
        ELSIF pg_catalog.jsonb_typeof(v_backend -> 'vram_budget_mb') = 'number'
              AND (v_backend ->> 'vram_budget_mb') ~ '^-?(0|[1-9][0-9]*)$' THEN
            v_vram_budget := (v_backend ->> 'vram_budget_mb')::integer;
        ELSE
            RAISE EXCEPTION USING ERRCODE = '22023';
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid health observation subject';
    END;

    SELECT secret.secret
    INTO v_secret
    FROM aap_inference_health_observation_v1.observation_secret AS secret
    WHERE secret.singleton IS TRUE;

    IF NOT FOUND OR pg_catalog.octet_length(v_secret) <> 32 THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'health observation capability is unavailable';
    END IF;

    v_header := pg_catalog.jsonb_build_object(
        'schema', 'aap-inference-health-observation-subject/v1',
        'database', pg_catalog.current_database(),
        'origin_xid', pg_catalog.pg_current_xact_id()::text,
        'registry_id', p_registry_id,
        'request_id', p_request_id,
        'gpu_health_challenge', p_gpu_health_challenge,
        'timeout_seconds', p_timeout_seconds,
        'backend_chars', pg_catalog.char_length(p_backend_json)
    );
    v_plaintext := v_header::text || E'\n' || p_backend_json;
    v_ciphertext := public.pgp_sym_encrypt(
        v_plaintext,
        pg_catalog.encode(v_secret, 'hex'),
        'cipher-algo=aes256,compress-algo=0'
    );

    RETURN 'v1.' || pg_catalog.replace(
        pg_catalog.replace(pg_catalog.encode(v_ciphertext, 'base64'), E'\n', ''),
        E'\r',
        ''
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_health_observation_v1.capture_health_observation_subject(
        uuid, uuid, text, text, double precision
    )
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION
    aap_inference_health_observation_v1.open_health_observation_subject(
        p_subject text,
        p_registry_id uuid,
        p_request_id uuid
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
    v_payload text;
    v_ciphertext bytea;
    v_plaintext text;
    v_header_end integer;
    v_header jsonb;
    v_backend_json text;
    v_backend_chars integer;
    v_origin_xid xid8;
    v_gpu_health_challenge text;
    v_timeout_seconds double precision;
BEGIN
    IF p_subject IS NULL
       OR p_registry_id IS NULL
       OR p_request_id IS NULL
       OR pg_catalog.octet_length(p_subject) > 25165824
       OR p_subject !~ '^v1\.[A-Za-z0-9+/]+={0,2}$' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid health observation subject';
    END IF;

    SELECT secret.secret
    INTO v_secret
    FROM aap_inference_health_observation_v1.observation_secret AS secret
    WHERE secret.singleton IS TRUE;

    IF NOT FOUND OR pg_catalog.octet_length(v_secret) <> 32 THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'health observation capability is unavailable';
    END IF;

    BEGIN
        v_payload := pg_catalog.substr(p_subject, 4);
        v_ciphertext := pg_catalog.decode(v_payload, 'base64');
        IF pg_catalog.replace(
               pg_catalog.replace(
                   pg_catalog.encode(v_ciphertext, 'base64'),
                   E'\n',
                   ''
               ),
               E'\r',
               ''
           ) IS DISTINCT FROM v_payload THEN
            RAISE EXCEPTION USING ERRCODE = '22023';
        END IF;
        v_plaintext := public.pgp_sym_decrypt(
            v_ciphertext,
            pg_catalog.encode(v_secret, 'hex')
        );

        v_header_end := pg_catalog.strpos(v_plaintext, E'\n');
        IF v_header_end < 2 THEN
            RAISE EXCEPTION USING ERRCODE = '22023';
        END IF;
        v_header := pg_catalog.substr(v_plaintext, 1, v_header_end - 1)::jsonb;
        v_backend_json := pg_catalog.substr(v_plaintext, v_header_end + 1);

        IF pg_catalog.jsonb_typeof(v_header) IS DISTINCT FROM 'object'
           OR (
               SELECT pg_catalog.count(*)
               FROM pg_catalog.jsonb_object_keys(v_header)
           ) <> 8
           OR NOT (v_header ?& ARRAY[
               'schema',
               'database',
               'origin_xid',
               'registry_id',
               'request_id',
               'gpu_health_challenge',
               'timeout_seconds',
               'backend_chars'
           ])
           OR pg_catalog.jsonb_typeof(v_header -> 'schema')
               IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_header -> 'database')
               IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_header -> 'origin_xid')
               IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_header -> 'registry_id')
               IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_header -> 'request_id')
               IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_header -> 'timeout_seconds')
               IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(v_header -> 'backend_chars')
               IS DISTINCT FROM 'number' THEN
            RAISE EXCEPTION USING ERRCODE = '22023';
        END IF;

        IF pg_catalog.jsonb_typeof(v_header -> 'gpu_health_challenge') = 'null' THEN
            v_gpu_health_challenge := NULL;
        ELSIF pg_catalog.jsonb_typeof(v_header -> 'gpu_health_challenge') = 'string' THEN
            v_gpu_health_challenge := v_header ->> 'gpu_health_challenge';
        ELSE
            RAISE EXCEPTION USING ERRCODE = '22023';
        END IF;

        v_origin_xid := (v_header ->> 'origin_xid')::xid8;
        v_backend_chars := (v_header ->> 'backend_chars')::integer;
        v_timeout_seconds := (v_header ->> 'timeout_seconds')::double precision;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid health observation subject';
    END;

    IF v_header ->> 'schema' IS DISTINCT FROM
           'aap-inference-health-observation-subject/v1'
       OR v_header ->> 'database' IS DISTINCT FROM pg_catalog.current_database()
       OR v_header ->> 'registry_id' IS DISTINCT FROM p_registry_id::text
       OR v_header ->> 'request_id' IS DISTINCT FROM p_request_id::text
       OR v_backend_chars < 2
       OR v_backend_chars > 16777216
       OR pg_catalog.char_length(v_backend_json) <> v_backend_chars
       OR v_gpu_health_challenge IS NOT NULL
          AND v_gpu_health_challenge !~ '^[0-9a-f]{64}$'
       OR v_timeout_seconds::text IN ('NaN', 'Infinity', '-Infinity')
       OR pg_catalog.pg_xact_status(v_origin_xid) IS DISTINCT FROM 'in progress' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid health observation subject';
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'backend_json', v_backend_json,
        'gpu_health_challenge', v_gpu_health_challenge,
        'timeout_seconds', v_timeout_seconds
    )::text;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_health_observation_v1.open_health_observation_subject(
        text, uuid, uuid
    )
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION
    aap_inference_health_observation_v1.seal_health_observation(
        p_subject text,
        p_registry_id uuid,
        p_request_id uuid,
        p_observation_json text
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
    v_payload text;
    v_ciphertext bytea;
    v_subject_plaintext text;
    v_subject_header_end integer;
    v_subject_header jsonb;
    v_origin_xid xid8;
    v_header jsonb;
    v_plaintext text;
BEGIN
    IF p_subject IS NULL
       OR p_registry_id IS NULL
       OR p_request_id IS NULL
       OR p_observation_json IS NULL
       OR pg_catalog.octet_length(p_observation_json) > 16777216 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid health observation evidence';
    END IF;

    PERFORM aap_inference_health_observation_v1.open_health_observation_subject(
        p_subject,
        p_registry_id,
        p_request_id
    );

    SELECT secret.secret
    INTO v_secret
    FROM aap_inference_health_observation_v1.observation_secret AS secret
    WHERE secret.singleton IS TRUE;

    IF NOT FOUND OR pg_catalog.octet_length(v_secret) <> 32 THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'health observation capability is unavailable';
    END IF;

    BEGIN
        v_payload := pg_catalog.substr(p_subject, 4);
        v_ciphertext := pg_catalog.decode(v_payload, 'base64');
        v_subject_plaintext := public.pgp_sym_decrypt(
            v_ciphertext,
            pg_catalog.encode(v_secret, 'hex')
        );
        v_subject_header_end := pg_catalog.strpos(v_subject_plaintext, E'\n');
        IF v_subject_header_end < 2 THEN
            RAISE EXCEPTION USING ERRCODE = '22023';
        END IF;
        v_subject_header := pg_catalog.substr(
            v_subject_plaintext,
            1,
            v_subject_header_end - 1
        )::jsonb;
        v_origin_xid := (v_subject_header ->> 'origin_xid')::xid8;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid health observation subject';
    END;

    v_header := pg_catalog.jsonb_build_object(
        'schema', 'aap-inference-health-observation-evidence/v1',
        'database', pg_catalog.current_database(),
        'origin_xid', v_origin_xid::text,
        'registry_id', p_registry_id,
        'request_id', p_request_id,
        'subject_sha256',
            pg_catalog.encode(public.digest(p_subject, 'sha256'), 'hex'),
        'observation_chars', pg_catalog.char_length(p_observation_json)
    );
    v_plaintext := v_header::text || E'\n' || p_observation_json;
    v_ciphertext := public.pgp_sym_encrypt(
        v_plaintext,
        pg_catalog.encode(v_secret, 'hex'),
        'cipher-algo=aes256,compress-algo=0'
    );

    RETURN 'v1.' || pg_catalog.replace(
        pg_catalog.replace(pg_catalog.encode(v_ciphertext, 'base64'), E'\n', ''),
        E'\r',
        ''
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_health_observation_v1.seal_health_observation(
        text, uuid, uuid, text
    )
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION
    aap_inference_health_observation_v1.verify_health_observation(
        p_subject text,
        p_registry_id uuid,
        p_request_id uuid,
        p_evidence text
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
    v_subject_payload text;
    v_subject_ciphertext bytea;
    v_subject_plaintext text;
    v_subject_header_end integer;
    v_subject_header jsonb;
    v_origin_xid xid8;
    v_current_xid xid8;
    v_evidence_payload text;
    v_evidence_ciphertext bytea;
    v_evidence_plaintext text;
    v_evidence_header_end integer;
    v_evidence_header jsonb;
    v_observation_json text;
    v_observation_chars integer;
BEGIN
    IF p_subject IS NULL
       OR p_registry_id IS NULL
       OR p_request_id IS NULL
       OR p_evidence IS NULL
       OR pg_catalog.octet_length(p_subject) > 25165824
       OR pg_catalog.octet_length(p_evidence) > 25165824
       OR p_subject !~ '^v1\.[A-Za-z0-9+/]+={0,2}$'
       OR p_evidence !~ '^v1\.[A-Za-z0-9+/]+={0,2}$' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid health observation evidence';
    END IF;

    PERFORM aap_inference_health_observation_v1.open_health_observation_subject(
        p_subject,
        p_registry_id,
        p_request_id
    );

    SELECT secret.secret
    INTO v_secret
    FROM aap_inference_health_observation_v1.observation_secret AS secret
    WHERE secret.singleton IS TRUE;

    IF NOT FOUND OR pg_catalog.octet_length(v_secret) <> 32 THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'health observation capability is unavailable';
    END IF;

    BEGIN
        v_subject_payload := pg_catalog.substr(p_subject, 4);
        v_subject_ciphertext := pg_catalog.decode(v_subject_payload, 'base64');
        IF pg_catalog.replace(
               pg_catalog.replace(
                   pg_catalog.encode(v_subject_ciphertext, 'base64'),
                   E'\n',
                   ''
               ),
               E'\r',
               ''
           ) IS DISTINCT FROM v_subject_payload THEN
            RAISE EXCEPTION USING ERRCODE = '22023';
        END IF;
        v_subject_plaintext := public.pgp_sym_decrypt(
            v_subject_ciphertext,
            pg_catalog.encode(v_secret, 'hex')
        );
        v_subject_header_end := pg_catalog.strpos(v_subject_plaintext, E'\n');
        IF v_subject_header_end < 2 THEN
            RAISE EXCEPTION USING ERRCODE = '22023';
        END IF;
        v_subject_header := pg_catalog.substr(
            v_subject_plaintext,
            1,
            v_subject_header_end - 1
        )::jsonb;
        v_origin_xid := (v_subject_header ->> 'origin_xid')::xid8;

        v_evidence_payload := pg_catalog.substr(p_evidence, 4);
        v_evidence_ciphertext := pg_catalog.decode(v_evidence_payload, 'base64');
        IF pg_catalog.replace(
               pg_catalog.replace(
                   pg_catalog.encode(v_evidence_ciphertext, 'base64'),
                   E'\n',
                   ''
               ),
               E'\r',
               ''
           ) IS DISTINCT FROM v_evidence_payload THEN
            RAISE EXCEPTION USING ERRCODE = '22023';
        END IF;
        v_evidence_plaintext := public.pgp_sym_decrypt(
            v_evidence_ciphertext,
            pg_catalog.encode(v_secret, 'hex')
        );
        v_evidence_header_end := pg_catalog.strpos(v_evidence_plaintext, E'\n');
        IF v_evidence_header_end < 2 THEN
            RAISE EXCEPTION USING ERRCODE = '22023';
        END IF;
        v_evidence_header := pg_catalog.substr(
            v_evidence_plaintext,
            1,
            v_evidence_header_end - 1
        )::jsonb;
        v_observation_json := pg_catalog.substr(
            v_evidence_plaintext,
            v_evidence_header_end + 1
        );

        IF pg_catalog.jsonb_typeof(v_subject_header) IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(v_evidence_header) IS DISTINCT FROM 'object'
           OR (
               SELECT pg_catalog.count(*)
               FROM pg_catalog.jsonb_object_keys(v_evidence_header)
           ) <> 7
           OR NOT (v_evidence_header ?& ARRAY[
               'schema',
               'database',
               'origin_xid',
               'registry_id',
               'request_id',
               'subject_sha256',
               'observation_chars'
           ])
           OR pg_catalog.jsonb_typeof(v_evidence_header -> 'schema')
               IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_evidence_header -> 'database')
               IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_evidence_header -> 'origin_xid')
               IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_evidence_header -> 'registry_id')
               IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_evidence_header -> 'request_id')
               IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_evidence_header -> 'subject_sha256')
               IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_evidence_header -> 'observation_chars')
               IS DISTINCT FROM 'number' THEN
            RAISE EXCEPTION USING ERRCODE = '22023';
        END IF;

        v_observation_chars :=
            (v_evidence_header ->> 'observation_chars')::integer;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid health observation evidence';
    END;

    v_current_xid := pg_catalog.pg_current_xact_id_if_assigned();

    IF v_evidence_header ->> 'schema' IS DISTINCT FROM
           'aap-inference-health-observation-evidence/v1'
       OR v_evidence_header ->> 'database' IS DISTINCT FROM
           pg_catalog.current_database()
       OR v_evidence_header ->> 'origin_xid' IS DISTINCT FROM
           v_origin_xid::text
       OR v_evidence_header ->> 'registry_id' IS DISTINCT FROM p_registry_id::text
       OR v_evidence_header ->> 'request_id' IS DISTINCT FROM p_request_id::text
       OR v_evidence_header ->> 'subject_sha256' IS DISTINCT FROM
           pg_catalog.encode(public.digest(p_subject, 'sha256'), 'hex')
       OR v_observation_chars < 0
       OR v_observation_chars > 16777216
       OR pg_catalog.char_length(v_observation_json) <> v_observation_chars
       OR v_current_xid IS DISTINCT FROM v_origin_xid
       OR pg_catalog.pg_xact_status(v_origin_xid) IS DISTINCT FROM 'in progress' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid health observation evidence';
    END IF;

    RETURN v_observation_json;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_health_observation_v1.verify_health_observation(
        text, uuid, uuid, text
    )
    FROM PUBLIC;
-- aap-statement-break
COMMENT ON TABLE aap_inference_health_observation_v1.observation_secret
    IS 'Database-local key for authenticated health observations bound to one live caller transaction.';
-- aap-statement-break
COMMENT ON FUNCTION
    aap_inference_health_observation_v1.health_observation_policy()
    IS 'Secret-free readiness and protocol metadata for transaction-bound health observation.';
-- aap-statement-break
COMMENT ON FUNCTION
    aap_inference_health_observation_v1.capture_health_observation_subject(
        uuid, uuid, text, text, double precision
    )
    IS 'Seals the caller-loaded Backend fields without registry reads or row locks.';
-- aap-statement-break
COMMENT ON FUNCTION
    aap_inference_health_observation_v1.open_health_observation_subject(
        text, uuid, uuid
    )
    IS 'Opens a captured Backend snapshot only while its originating transaction remains in progress.';
-- aap-statement-break
COMMENT ON FUNCTION
    aap_inference_health_observation_v1.seal_health_observation(
        text, uuid, uuid, text
    )
    IS 'Seals raw provider observation text against the snapshot and original caller transaction.';
-- aap-statement-break
COMMENT ON FUNCTION
    aap_inference_health_observation_v1.verify_health_observation(
        text, uuid, uuid, text
    )
    IS 'Returns original raw observation text only in the originating live transaction.';
