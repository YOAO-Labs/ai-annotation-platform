CREATE FUNCTION aap_audit_v1.append_events(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_event jsonb;
    v_keys text[];
    v_actor_id uuid;
    v_actor_email text;
    v_actor_role text;
    v_action text;
    v_target_type text;
    v_target_id text;
    v_method text;
    v_path text;
    v_status_code smallint;
    v_ip inet;
    v_detail_json jsonb;
    v_request_id text;
    v_id bigint;
    v_created_at timestamptz;
    v_receipts jsonb := '[]'::jsonb;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'invalid audit append request' USING ERRCODE = '22023';
    END IF;

    SELECT array_agg(key ORDER BY key) INTO v_keys
    FROM jsonb_object_keys(p_request) AS object_key(key);
    IF v_keys IS DISTINCT FROM ARRAY['events', 'schema']::text[]
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-audit-append/v1' THEN
        RAISE EXCEPTION 'invalid audit append request' USING ERRCODE = '22023';
    END IF;
    IF jsonb_typeof(p_request -> 'events') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'invalid audit append request' USING ERRCODE = '22023';
    END IF;
    IF jsonb_array_length(p_request -> 'events') = 0 THEN
        RAISE EXCEPTION 'invalid audit append request' USING ERRCODE = '22023';
    END IF;

    FOR v_event IN
        SELECT event_value
        FROM jsonb_array_elements(p_request -> 'events') WITH ORDINALITY AS items(event_value, ordinal)
        ORDER BY ordinal
    LOOP
        IF jsonb_typeof(v_event) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'invalid audit append event' USING ERRCODE = '22023';
        END IF;

        SELECT array_agg(key ORDER BY key) INTO v_keys
        FROM jsonb_object_keys(v_event) AS object_key(key);
        IF v_keys IS DISTINCT FROM ARRAY[
            'action', 'actor_email', 'actor_id', 'actor_role', 'detail_json', 'ip',
            'method', 'path', 'request_id', 'status_code', 'target_id', 'target_type'
        ]::text[] THEN
            RAISE EXCEPTION 'invalid audit append event' USING ERRCODE = '22023';
        END IF;

        IF jsonb_typeof(v_event -> 'action') IS DISTINCT FROM 'string'
           OR length(v_event ->> 'action') > 64
           OR jsonb_typeof(v_event -> 'actor_id') NOT IN ('string', 'null')
           OR jsonb_typeof(v_event -> 'actor_email') NOT IN ('string', 'null')
           OR jsonb_typeof(v_event -> 'actor_role') NOT IN ('string', 'null')
           OR jsonb_typeof(v_event -> 'target_type') NOT IN ('string', 'null')
           OR jsonb_typeof(v_event -> 'target_id') NOT IN ('string', 'null')
           OR jsonb_typeof(v_event -> 'method') NOT IN ('string', 'null')
           OR jsonb_typeof(v_event -> 'path') NOT IN ('string', 'null')
           OR jsonb_typeof(v_event -> 'status_code') NOT IN ('number', 'null')
           OR jsonb_typeof(v_event -> 'ip') NOT IN ('string', 'null')
           OR jsonb_typeof(v_event -> 'request_id') NOT IN ('string', 'null') THEN
            RAISE EXCEPTION 'invalid audit append event' USING ERRCODE = '22023';
        END IF;

        v_actor_id := NULL;
        IF v_event ->> 'actor_id' IS NOT NULL THEN
            IF v_event ->> 'actor_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                RAISE EXCEPTION 'invalid audit actor id' USING ERRCODE = '22023';
            END IF;
            v_actor_id := (v_event ->> 'actor_id')::uuid;
        END IF;

        v_actor_email := v_event ->> 'actor_email';
        v_actor_role := v_event ->> 'actor_role';
        v_action := v_event ->> 'action';
        v_target_type := v_event ->> 'target_type';
        v_target_id := v_event ->> 'target_id';
        v_method := v_event ->> 'method';
        v_path := v_event ->> 'path';
        v_status_code := NULL;
        IF v_event ->> 'status_code' IS NOT NULL THEN
            IF v_event ->> 'status_code' !~ '^-?[0-9]+$' THEN
                RAISE EXCEPTION 'invalid audit status code' USING ERRCODE = '22023';
            END IF;
            v_status_code := (v_event ->> 'status_code')::smallint;
        END IF;
        v_ip := NULL;
        IF v_event ->> 'ip' IS NOT NULL THEN
            v_ip := (v_event ->> 'ip')::inet;
        END IF;
        -- Keeping any JSONB value (including JSON null) preserves the existing payload.
        v_detail_json := v_event -> 'detail_json';
        v_request_id := v_event ->> 'request_id';

        IF length(v_actor_email) > 255 OR length(v_actor_role) > 32
           OR length(v_target_type) > 32 OR length(v_target_id) > 64
           OR length(v_method) > 8 OR length(v_path) > 256
           OR length(v_request_id) > 36 THEN
            RAISE EXCEPTION 'audit append field exceeds storage limit' USING ERRCODE = '22023';
        END IF;

        INSERT INTO public.audit_logs (
            actor_id, actor_email, actor_role, action, target_type, target_id,
            method, path, status_code, ip, detail_json, request_id
        ) VALUES (
            v_actor_id, v_actor_email, v_actor_role, v_action, v_target_type, v_target_id,
            v_method, v_path, v_status_code, v_ip, v_detail_json, v_request_id
        )
        RETURNING id, created_at INTO v_id, v_created_at;

        v_receipts := v_receipts || jsonb_build_array(
            jsonb_build_object('id', v_id::text, 'created_at', v_created_at)
        );
    END LOOP;

    RETURN jsonb_build_object(
        'schema', 'aap-audit-append/v1',
        'ok', true,
        'data', jsonb_build_object('events', v_receipts)
    );
END
$function$;

-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.append_events(jsonb) FROM PUBLIC;
