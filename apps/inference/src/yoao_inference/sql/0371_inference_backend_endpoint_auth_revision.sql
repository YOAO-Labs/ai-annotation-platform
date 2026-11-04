-- Forward: consume the published Registry auth_revision in backend endpoint
-- snapshots (additive companion to the frozen 0369 owner source).
--
-- 0190_ml_backend_auth_rotation makes auth_revision database managed and rotates it
-- on credential changes. The endpoint transport identity previously covered only
-- url/auth_method/auth_token, so a revision bump with an identical token left a
-- captured subject valid. These replacements add auth_revision to the endpoint
-- record, the transport identity digest, the capture shape check, the subject
-- shape check and the returned snapshot. Apply after the owner installer (bo0369).
CREATE OR REPLACE FUNCTION aap_inference_backend_endpoint_v1._endpoint_record(p_registry_id uuid)
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
        'registry_id', registry.id,
        'url', registry.url,
        'auth_method', registry.auth_method,
        'auth_token', registry.auth_token,
        'auth_revision', registry.auth_revision,
        'gpu_resource_id', registry.gpu_resource_id,
        'vram_budget_mb', registry.vram_budget_mb,
        'eviction_priority', registry.eviction_priority,
        'extra_params', registry.extra_params
    )
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_backend_endpoint_v1._endpoint_record(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_backend_endpoint_v1._endpoint_transport_identity(p_endpoint jsonb)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'url', p_endpoint -> 'url',
        'auth_method', p_endpoint -> 'auth_method',
        'auth_token', p_endpoint -> 'auth_token',
        'auth_revision', p_endpoint -> 'auth_revision',
        'gpu_resource_id', p_endpoint -> 'gpu_resource_id',
        'extra_params', p_endpoint -> 'extra_params'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_backend_endpoint_v1._endpoint_transport_identity(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_backend_endpoint_v1.capture_backend_endpoint_subject(
    p_registry_id uuid,
    p_request_id uuid,
    p_purpose text,
    p_binding_json text
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
    v_secret bytea;
    v_endpoint jsonb;
    v_transport_identity jsonb;
    v_authority_input jsonb;
    v_authority jsonb;
    v_route_result jsonb;
    v_endpoint_sha256 text;
    v_endpoint_json text;
    v_plaintext text;
    v_ciphertext bytea;
    v_header jsonb;
    v_origin_xid xid8;
BEGIN
    IF p_registry_id IS NULL OR p_request_id IS NULL
       OR p_purpose NOT IN ('health_observation', 'setup', 'catalog_fallback', 'stats', 'backend_read', 'lifecycle', 'execution', 'gpu_control')
       OR (p_binding_json IS NOT NULL AND pg_catalog.octet_length(p_binding_json) > 4096) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid backend endpoint capture';
    END IF;
    IF p_purpose IN ('health_observation', 'setup', 'catalog_fallback', 'stats', 'backend_read') THEN
        IF p_binding_json IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'read endpoint capture cannot carry route authority';
        END IF;
    ELSE
        IF p_binding_json IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'stateful endpoint capture requires route authority';
        END IF;
        BEGIN
            v_authority_input := p_binding_json::jsonb;
            v_authority := aap_inference_backend_endpoint_v1._endpoint_authority(v_authority_input);
        EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid backend endpoint capture';
        END;
        IF (p_purpose = 'execution' AND v_authority ->> 'kind' <> 'scope')
           OR (p_purpose = 'lifecycle' AND v_authority ->> 'kind' <> 'control')
           OR p_request_id IS DISTINCT FROM (v_authority ->> 'attempt_id')::uuid THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid backend endpoint purpose binding';
        END IF;
    END IF;
    v_endpoint := aap_inference_backend_endpoint_v1._endpoint_record(p_registry_id);
    IF v_endpoint IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'backend endpoint unavailable';
    END IF;
    IF pg_catalog.jsonb_typeof(v_endpoint -> 'url') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_endpoint -> 'auth_method') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_endpoint -> 'auth_token') NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(v_endpoint -> 'auth_revision') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_endpoint -> 'gpu_resource_id') NOT IN ('string', 'null') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid registered backend endpoint';
    END IF;
    IF v_authority IS NOT NULL THEN
        v_transport_identity := aap_inference_backend_endpoint_v1._endpoint_transport_identity(v_endpoint);
        v_route_result := aap_inference_routing_v1.routing_step(
            'capture_transport_subject',
            pg_catalog.jsonb_build_object(
                'parent_kind', v_authority ->> 'kind',
                'parent_id', COALESCE(v_authority ->> 'scope_id', v_authority ->> 'control_id'),
                'capability', v_authority_input ->> 'capability',
                'attempt_id', v_authority ->> 'attempt_id',
                'transport_identity', v_transport_identity
            )::text
        );
        IF v_route_result ->> 'status' IS DISTINCT FROM 'ok'
           OR pg_catalog.jsonb_typeof(v_route_result -> 'subject') IS DISTINCT FROM 'string' THEN
            RETURN pg_catalog.jsonb_build_object(
                'status', 'rejected',
                'reason', COALESCE(v_route_result ->> 'reason', 'backend_not_ready')
            );
        END IF;
    END IF;
    SELECT secret.secret
    INTO v_secret
    FROM aap_inference_backend_endpoint_v1.endpoint_secret AS secret
    WHERE secret.singleton IS TRUE;
    IF NOT FOUND OR pg_catalog.octet_length(v_secret) <> 32 THEN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'backend endpoint capability unavailable';
    END IF;
    v_endpoint_sha256 := pg_catalog.encode(
        public.digest(pg_catalog.convert_to(v_endpoint::text, 'UTF8'), 'sha256'), 'hex'
    );
    v_endpoint_json := v_endpoint::text;
    v_origin_xid := pg_catalog.pg_current_xact_id();
    v_header := pg_catalog.jsonb_build_object(
        'schema', 'aap-inference-backend-endpoint-subject/v1',
        'database', pg_catalog.current_database(),
        'origin_xid', v_origin_xid::text,
        'registry_id', p_registry_id,
        'request_id', p_request_id,
        'purpose', p_purpose,
        'authority', v_authority,
        'endpoint_sha256', v_endpoint_sha256,
        'endpoint_chars', pg_catalog.char_length(v_endpoint_json)
    );
    v_plaintext := v_header::text || E'\n' || v_endpoint_json;
    v_ciphertext := public.pgp_sym_encrypt(
        v_plaintext, pg_catalog.encode(v_secret, 'hex'), 'cipher-algo=aes256,compress-algo=0'
    );
    RETURN pg_catalog.jsonb_build_object(
        'status', 'ok',
        'subject', 'v1.' || pg_catalog.replace(pg_catalog.replace(pg_catalog.encode(v_ciphertext, 'base64'), E'\n', ''), E'\r', '')
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_backend_endpoint_v1.capture_backend_endpoint_subject(uuid, uuid, text, text) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_backend_endpoint_v1._open_backend_endpoint_subject(
    p_subject text,
    p_registry_id uuid,
    p_request_id uuid,
    p_purpose text,
    p_authority_json text
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
    v_header jsonb;
    v_endpoint_json text;
    v_endpoint jsonb;
    v_current_endpoint jsonb;
    v_authority_input jsonb;
    v_authority jsonb;
    v_endpoint_sha256 text;
    v_endpoint_chars integer;
    v_header_end integer;
    v_origin_xid xid8;
    v_database text;
    v_origin_status text;
    v_realm text;
    v_registry_id uuid;
    v_request_id uuid;
    v_purpose text;
    v_scope_registry_id uuid;
    v_scope_realm text;
    v_scope_state text;
    v_scope_closed boolean;
    v_scope_revision bigint;
    v_registry_revision bigint;
    v_control_registry_id uuid;
    v_control_realm text;
    v_control_kind text;
    v_control_phase text;
    v_control_revision bigint;
BEGIN
    IF p_subject IS NULL OR p_registry_id IS NULL OR p_request_id IS NULL
       OR p_purpose NOT IN ('health_observation', 'setup', 'catalog_fallback', 'stats', 'backend_read', 'lifecycle', 'execution', 'gpu_control')
       OR pg_catalog.octet_length(p_subject) > 25165824
       OR p_subject !~ '^v1\.[A-Za-z0-9+/]+={0,2}$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid backend endpoint subject';
    END IF;
    IF p_purpose IN ('health_observation', 'setup', 'catalog_fallback', 'stats', 'backend_read') THEN
        IF p_authority_json IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'read endpoint open cannot carry route authority';
        END IF;
        v_authority := NULL;
    ELSE
        IF p_authority_json IS NULL OR pg_catalog.octet_length(p_authority_json) > 4096 THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'stateful endpoint open requires route authority';
        END IF;
        BEGIN
            v_authority_input := p_authority_json::jsonb;
            IF pg_catalog.jsonb_typeof(v_authority_input) IS DISTINCT FROM 'object'
               OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(v_authority_input)) <> 4
               OR NOT (v_authority_input ?& ARRAY['kind', 'scope_id', 'control_id', 'attempt_id']) THEN
                RAISE EXCEPTION USING ERRCODE = '22023';
            END IF;
            v_authority := aap_inference_backend_endpoint_v1._endpoint_authority(
                pg_catalog.jsonb_build_object(
                    'kind', v_authority_input -> 'kind',
                    'scope_id', v_authority_input -> 'scope_id',
                    'control_id', v_authority_input -> 'control_id',
                    'attempt_id', v_authority_input -> 'attempt_id',
                    'capability', '0000000000000000000000000000000000000000000000000000000000000000'
                )
            );
        EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid backend endpoint authority';
        END;
        IF (p_purpose = 'execution' AND v_authority ->> 'kind' <> 'scope')
           OR (p_purpose = 'lifecycle' AND v_authority ->> 'kind' <> 'control')
           OR p_request_id IS DISTINCT FROM (v_authority ->> 'attempt_id')::uuid THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid backend endpoint purpose binding';
        END IF;
    END IF;
    SELECT secret.secret INTO v_secret
    FROM aap_inference_backend_endpoint_v1.endpoint_secret AS secret
    WHERE secret.singleton IS TRUE;
    IF NOT FOUND OR pg_catalog.octet_length(v_secret) <> 32 THEN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'backend endpoint capability unavailable';
    END IF;
    BEGIN
        v_ciphertext := pg_catalog.decode(pg_catalog.substr(p_subject, 4), 'base64');
        IF pg_catalog.replace(pg_catalog.replace(pg_catalog.encode(v_ciphertext, 'base64'), E'\n', ''), E'\r', '')
           IS DISTINCT FROM pg_catalog.substr(p_subject, 4) THEN
            RAISE EXCEPTION USING ERRCODE = '22023';
        END IF;
        v_plaintext := public.pgp_sym_decrypt(v_ciphertext, pg_catalog.encode(v_secret, 'hex'));
        v_header_end := pg_catalog.strpos(v_plaintext, E'\n');
        IF v_header_end < 2 THEN
            RAISE EXCEPTION USING ERRCODE = '22023';
        END IF;
        v_header := pg_catalog.substr(v_plaintext, 1, v_header_end - 1)::jsonb;
        v_endpoint_json := pg_catalog.substr(v_plaintext, v_header_end + 1);
        v_endpoint := v_endpoint_json::jsonb;
        v_origin_xid := (v_header ->> 'origin_xid')::xid8;
        v_endpoint_chars := (v_header ->> 'endpoint_chars')::integer;
        v_endpoint_sha256 := v_header ->> 'endpoint_sha256';
        v_database := v_header ->> 'database';
        v_registry_id := (v_header ->> 'registry_id')::uuid;
        v_request_id := (v_header ->> 'request_id')::uuid;
        v_purpose := v_header ->> 'purpose';
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid backend endpoint subject';
    END;
    IF pg_catalog.jsonb_typeof(v_header) IS DISTINCT FROM 'object'
       OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(v_header)) <> 9
       OR NOT (v_header ?& ARRAY['schema', 'database', 'origin_xid', 'registry_id', 'request_id', 'purpose', 'authority', 'endpoint_sha256', 'endpoint_chars']) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid backend endpoint subject';
    END IF;
    v_origin_status := pg_catalog.pg_xact_status(v_origin_xid);
    IF v_header ->> 'schema' IS DISTINCT FROM 'aap-inference-backend-endpoint-subject/v1'
       OR v_database IS DISTINCT FROM pg_catalog.current_database()
       OR v_registry_id IS DISTINCT FROM p_registry_id
       OR v_request_id IS DISTINCT FROM p_request_id
       OR v_purpose IS DISTINCT FROM p_purpose
       OR (v_authority IS NULL AND v_header -> 'authority' IS DISTINCT FROM 'null'::jsonb)
       OR (v_authority IS NOT NULL AND v_header -> 'authority' IS DISTINCT FROM v_authority)
       OR v_endpoint_chars IS NULL
       OR v_endpoint_chars < 2
       OR v_endpoint_chars > 16777216
       OR pg_catalog.char_length(v_endpoint_json) <> v_endpoint_chars
       OR v_endpoint_sha256 IS NULL
       OR v_endpoint_sha256 IS DISTINCT FROM pg_catalog.encode(
           public.digest(pg_catalog.convert_to(v_endpoint_json, 'UTF8'), 'sha256'), 'hex'
       )
       OR v_origin_status IS NULL
       OR (p_purpose IN ('health_observation', 'setup', 'catalog_fallback', 'stats', 'backend_read')
           AND v_origin_status IS DISTINCT FROM 'in progress')
       OR (p_purpose NOT IN ('health_observation', 'setup', 'catalog_fallback', 'stats', 'backend_read')
           AND v_origin_status NOT IN ('in progress', 'committed'))
       OR pg_catalog.jsonb_typeof(v_endpoint) IS DISTINCT FROM 'object'
       OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(v_endpoint)) <> 9
       OR NOT (v_endpoint ?& ARRAY['registry_id', 'url', 'auth_method', 'auth_token', 'auth_revision', 'gpu_resource_id', 'vram_budget_mb', 'eviction_priority', 'extra_params'])
       OR (v_endpoint ->> 'registry_id')::uuid IS DISTINCT FROM p_registry_id
       OR pg_catalog.jsonb_typeof(v_endpoint -> 'url') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_endpoint -> 'auth_method') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_endpoint -> 'auth_token') NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(v_endpoint -> 'auth_revision') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_endpoint -> 'gpu_resource_id') NOT IN ('string', 'null') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid backend endpoint subject';
    END IF;
    IF v_authority IS NOT NULL THEN
        v_current_endpoint := aap_inference_backend_endpoint_v1._endpoint_record(p_registry_id);
        IF v_current_endpoint IS NULL
           OR pg_catalog.encode(public.digest(pg_catalog.convert_to(v_current_endpoint::text, 'UTF8'), 'sha256'), 'hex')
              IS DISTINCT FROM v_endpoint_sha256
           OR v_endpoint IS DISTINCT FROM v_current_endpoint THEN
            RAISE EXCEPTION USING ERRCODE = '40001', MESSAGE = 'backend endpoint snapshot changed';
        END IF;
    END IF;
    IF v_authority IS NOT NULL THEN
        SELECT database.oid::text || ':' || database.datname
        INTO v_realm
        FROM pg_catalog.pg_database AS database
        WHERE database.datname = pg_catalog.current_database();
        IF v_authority ->> 'kind' = 'scope' THEN
            SELECT scope.registry_id, scope.realm, scope.state, scope.closed,
                   scope.config_revision, registry.config_revision
            INTO v_scope_registry_id, v_scope_realm, v_scope_state, v_scope_closed,
                 v_scope_revision, v_registry_revision
            FROM aap_inference_routing_v1.scopes AS scope
            JOIN aap_inference_routing_v1.registry_state AS registry
              ON registry.registry_id = scope.registry_id
            WHERE scope.id = (v_authority ->> 'scope_id')::uuid;
            IF NOT FOUND OR v_scope_registry_id IS DISTINCT FROM p_registry_id
               OR v_scope_realm IS DISTINCT FROM v_realm
               OR v_scope_state NOT IN ('reserved', 'active')
               OR v_scope_closed IS DISTINCT FROM false
               OR v_scope_revision IS DISTINCT FROM v_registry_revision
               OR EXISTS (
                   SELECT 1 FROM aap_inference_routing_v1.attempts AS attempt
                   WHERE attempt.id = (v_authority ->> 'attempt_id')::uuid
               ) THEN
                RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'backend endpoint scope authority unavailable';
            END IF;
        ELSE
            SELECT control.registry_id, control.realm, control.kind, control.phase,
                   control.config_revision, registry.config_revision
            INTO v_control_registry_id, v_control_realm, v_control_kind, v_control_phase,
                 v_control_revision, v_registry_revision
            FROM aap_inference_routing_v1.controls AS control
            JOIN aap_inference_routing_v1.registry_state AS registry
              ON registry.registry_id = control.registry_id
            WHERE control.id = (v_authority ->> 'control_id')::uuid;
            IF NOT FOUND OR v_control_registry_id IS DISTINCT FROM p_registry_id
               OR v_control_realm IS DISTINCT FROM v_realm
               OR v_control_kind NOT IN ('unload', 'reload')
               OR v_control_phase <> 'prepared'
               OR v_control_revision IS DISTINCT FROM v_registry_revision
               OR EXISTS (
                   SELECT 1 FROM aap_inference_routing_v1.control_attempts AS attempt
                   WHERE attempt.id = (v_authority ->> 'attempt_id')::uuid
               ) THEN
                RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'backend endpoint control authority unavailable';
            END IF;
        END IF;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-inference-backend-endpoint-snapshot/v1',
        'registry_id', p_registry_id,
        'request_id', p_request_id,
        'purpose', p_purpose,
        'authority', v_authority,
        'endpoint_sha256', v_endpoint_sha256,
        'url', v_endpoint -> 'url',
        'auth_method', v_endpoint -> 'auth_method',
        'auth_token', v_endpoint -> 'auth_token',
        'auth_revision', v_endpoint -> 'auth_revision',
        'gpu_resource_id', v_endpoint -> 'gpu_resource_id',
        'vram_budget_mb', v_endpoint -> 'vram_budget_mb',
        'eviction_priority', v_endpoint -> 'eviction_priority',
        'extra_params', v_endpoint -> 'extra_params'
    )::text;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_backend_endpoint_v1._open_backend_endpoint_subject(text, uuid, uuid, text, text) FROM PUBLIC;
-- aap-statement-break
