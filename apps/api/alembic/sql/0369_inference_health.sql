-- Forward health-observation protocol: seal and open credential-free endpoint
-- subjects inside the existing transaction-bound capability.
-- Callers capture the registered endpoint through
-- aap_inference_backend_endpoint_v1 and seal only {id, endpoint_subject}; the
-- raw endpoint record is materialized exclusively inside this credential-owning
-- capability while the origin transaction is still live. No second port or
-- credential-bearing payload ever crosses the observation boundary.
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
    v_endpoint jsonb;
    v_header jsonb;
    v_plaintext text;
    v_ciphertext bytea;
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
           ) <> 2
           OR NOT (v_backend ?& ARRAY['id', 'endpoint_subject'])
           OR pg_catalog.jsonb_typeof(v_backend -> 'id') IS DISTINCT FROM 'string'
           OR v_backend ->> 'id' IS DISTINCT FROM p_registry_id::text
           OR pg_catalog.jsonb_typeof(v_backend -> 'endpoint_subject')
               IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023';
        END IF;

        v_endpoint := v_backend -> 'endpoint_subject';
        IF (
               SELECT pg_catalog.count(*)
               FROM pg_catalog.jsonb_object_keys(v_endpoint)
           ) <> 6
           OR NOT (v_endpoint ?& ARRAY[
               'schema',
               'request_id',
               'registry_id',
               'purpose',
               'authority',
               'subject'
           ])
           OR v_endpoint ->> 'schema' IS DISTINCT FROM
               'aap-inference-backend-endpoint/v1'
           OR v_endpoint ->> 'registry_id' IS DISTINCT FROM p_registry_id::text
           OR v_endpoint ->> 'request_id' IS DISTINCT FROM p_request_id::text
           OR v_endpoint ->> 'purpose' IS DISTINCT FROM 'health_observation'
           OR pg_catalog.jsonb_typeof(v_endpoint -> 'authority')
               IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(v_endpoint -> 'subject')
               IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_endpoint ->> 'subject') < 4
           OR pg_catalog.substr(v_endpoint ->> 'subject', 1, 3)
               IS DISTINCT FROM 'v1.' THEN
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
COMMENT ON FUNCTION
    aap_inference_health_observation_v1.capture_health_observation_subject(
        uuid, uuid, text, text, double precision
    )
    IS 'Seals the caller-captured credential-free endpoint subject without registry reads or row locks.';
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
    v_backend jsonb;
    v_envelope jsonb;
    v_endpoint_subject text;
    v_opened jsonb;
    v_transport jsonb;
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

    BEGIN
        v_backend := v_backend_json::jsonb;
        IF pg_catalog.jsonb_typeof(v_backend) IS DISTINCT FROM 'object'
           OR (
               SELECT pg_catalog.count(*)
               FROM pg_catalog.jsonb_object_keys(v_backend)
           ) <> 2
           OR NOT (v_backend ?& ARRAY['id', 'endpoint_subject'])
           OR pg_catalog.jsonb_typeof(v_backend -> 'id') IS DISTINCT FROM 'string'
           OR v_backend ->> 'id' IS DISTINCT FROM p_registry_id::text
           OR pg_catalog.jsonb_typeof(v_backend -> 'endpoint_subject')
               IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023';
        END IF;

        v_envelope := v_backend -> 'endpoint_subject';
        IF (
               SELECT pg_catalog.count(*)
               FROM pg_catalog.jsonb_object_keys(v_envelope)
           ) <> 6
           OR NOT (v_envelope ?& ARRAY[
               'schema',
               'request_id',
               'registry_id',
               'purpose',
               'authority',
               'subject'
           ])
           OR v_envelope ->> 'schema' IS DISTINCT FROM
               'aap-inference-backend-endpoint/v1'
           OR v_envelope ->> 'registry_id' IS DISTINCT FROM p_registry_id::text
           OR v_envelope ->> 'request_id' IS DISTINCT FROM p_request_id::text
           OR v_envelope ->> 'purpose' IS DISTINCT FROM 'health_observation'
           OR pg_catalog.jsonb_typeof(v_envelope -> 'authority')
               IS DISTINCT FROM 'null'
           OR pg_catalog.jsonb_typeof(v_envelope -> 'subject')
               IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION USING ERRCODE = '22023';
        END IF;
        v_endpoint_subject := v_envelope ->> 'subject';
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid health observation subject';
    END;

    BEGIN
        v_opened := aap_inference_backend_endpoint_v1.open_health_observation_endpoint(
            v_endpoint_subject,
            p_registry_id,
            p_request_id,
            NULL::text
        )::jsonb;
        IF pg_catalog.jsonb_typeof(v_opened) IS DISTINCT FROM 'object'
           OR NOT (v_opened ?& ARRAY[
               'registry_id',
               'url',
               'auth_method',
               'auth_token',
               'gpu_resource_id',
               'vram_budget_mb',
               'extra_params'
           ])
           OR (v_opened ->> 'registry_id')::uuid IS DISTINCT FROM p_registry_id
           OR pg_catalog.jsonb_typeof(v_opened -> 'url')
               IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_opened -> 'auth_method')
               IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_opened -> 'auth_token')
               NOT IN ('string', 'null')
           OR pg_catalog.jsonb_typeof(v_opened -> 'gpu_resource_id')
               NOT IN ('string', 'null')
           OR pg_catalog.jsonb_typeof(v_opened -> 'vram_budget_mb')
               NOT IN ('number', 'null')
           OR pg_catalog.jsonb_typeof(v_opened -> 'extra_params')
               NOT IN ('object', 'null') THEN
            RAISE EXCEPTION USING ERRCODE = '22023';
        END IF;

        v_transport := pg_catalog.jsonb_build_object(
            'id', p_registry_id::text,
            'url', v_opened -> 'url',
            'auth_method', v_opened -> 'auth_method',
            'auth_token', v_opened -> 'auth_token',
            'gpu_resource_id', v_opened -> 'gpu_resource_id',
            'vram_budget_mb', v_opened -> 'vram_budget_mb',
            'extra_params', v_opened -> 'extra_params'
        );
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid health observation endpoint';
    END;

    RETURN pg_catalog.jsonb_build_object(
        'backend_json', v_transport::text,
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
COMMENT ON FUNCTION
    aap_inference_health_observation_v1.open_health_observation_subject(
        text, uuid, uuid
    )
    IS 'Opens a captured endpoint subject into transport fields only while its originating transaction remains in progress.';
