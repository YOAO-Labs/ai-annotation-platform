CREATE FUNCTION aap_audit_v1.query_events(p_request pg_catalog.jsonb)
RETURNS pg_catalog.jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_keys text[];
    v_filter_keys text[];
    v_operation text;
    v_page bigint;
    v_page_size bigint;
    v_after_created_at timestamptz;
    v_after_id bigint;
    v_action text;
    v_target_type text;
    v_target_id text;
    v_actor_id uuid;
    v_from timestamptz;
    v_to timestamptz;
    v_business_only boolean;
    v_detail_key text;
    v_detail_value text;
    v_total bigint;
    v_events pg_catalog.jsonb := '[]'::pg_catalog.jsonb;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'invalid audit query request' USING ERRCODE = '22023';
    END IF;

    SELECT array_agg(key ORDER BY key) INTO v_keys
    FROM jsonb_object_keys(p_request) AS object_key(key);
    IF v_keys IS DISTINCT FROM ARRAY[
        'after', 'filters', 'operation', 'page', 'page_size', 'schema'
    ]::text[]
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-audit-query/v1'
       OR jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_request -> 'page') IS DISTINCT FROM 'number'
       OR p_request ->> 'page' !~ '^[1-9][0-9]*$'
       OR jsonb_typeof(p_request -> 'page_size') IS DISTINCT FROM 'number'
       OR p_request ->> 'page_size' !~ '^[1-9][0-9]*$'
       OR jsonb_typeof(p_request -> 'filters') IS DISTINCT FROM 'object'
       OR jsonb_typeof(p_request -> 'after') NOT IN ('object', 'null') THEN
        RAISE EXCEPTION 'invalid audit query request' USING ERRCODE = '22023';
    END IF;

    v_operation := p_request ->> 'operation';
    v_page := (p_request ->> 'page')::bigint;
    v_page_size := (p_request ->> 'page_size')::bigint;
    IF v_operation NOT IN ('list', 'export') OR v_page < 1 OR v_page_size < 1
       OR (v_operation = 'list' AND v_page_size > 200)
       OR (v_operation = 'export' AND (
           v_page <> 1 OR v_page_size <> 50000
           OR jsonb_typeof(p_request -> 'after') IS DISTINCT FROM 'null'
       )) THEN
        RAISE EXCEPTION 'invalid audit query request' USING ERRCODE = '22023';
    END IF;

    SELECT array_agg(key ORDER BY key) INTO v_filter_keys
    FROM jsonb_object_keys(p_request -> 'filters') AS object_key(key);
    IF v_filter_keys IS DISTINCT FROM ARRAY[
        'action', 'actor_id', 'business_only', 'detail_key', 'detail_value',
        'from', 'target_id', 'target_type', 'to'
    ]::text[] THEN
        RAISE EXCEPTION 'invalid audit query filters' USING ERRCODE = '22023';
    END IF;
    IF jsonb_typeof(p_request #> '{filters,action}') NOT IN ('string', 'null')
       OR jsonb_typeof(p_request #> '{filters,target_type}') NOT IN ('string', 'null')
       OR jsonb_typeof(p_request #> '{filters,target_id}') NOT IN ('string', 'null')
       OR jsonb_typeof(p_request #> '{filters,actor_id}') NOT IN ('string', 'null')
       OR jsonb_typeof(p_request #> '{filters,from}') NOT IN ('string', 'null')
       OR jsonb_typeof(p_request #> '{filters,to}') NOT IN ('string', 'null')
       OR jsonb_typeof(p_request #> '{filters,business_only}') IS DISTINCT FROM 'boolean'
       OR jsonb_typeof(p_request #> '{filters,detail_key}') NOT IN ('string', 'null')
       OR jsonb_typeof(p_request #> '{filters,detail_value}') NOT IN ('string', 'null') THEN
        RAISE EXCEPTION 'invalid audit query filters' USING ERRCODE = '22023';
    END IF;
    IF (p_request #>> '{filters,from}' IS NOT NULL
        AND p_request #>> '{filters,from}' !~ '(Z|[+-][0-9]{2}:[0-9]{2})$')
       OR (p_request #>> '{filters,to}' IS NOT NULL
        AND p_request #>> '{filters,to}' !~ '(Z|[+-][0-9]{2}:[0-9]{2})$') THEN
        RAISE EXCEPTION 'audit query time filters require an explicit timezone'
            USING ERRCODE = '22023';
    END IF;
    v_action := NULLIF(p_request #>> '{filters,action}', '');
    v_target_type := NULLIF(p_request #>> '{filters,target_type}', '');
    v_target_id := NULLIF(p_request #>> '{filters,target_id}', '');
    v_actor_id := NULL;
    IF p_request #>> '{filters,actor_id}' IS NOT NULL
       AND p_request #>> '{filters,actor_id}' <> '' THEN
        IF p_request #>> '{filters,actor_id}' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND p_request #>> '{filters,actor_id}' !~* '^[0-9a-f]{32}$' THEN
            RAISE EXCEPTION 'invalid audit query actor id' USING ERRCODE = '22023';
        END IF;
        v_actor_id := (p_request #>> '{filters,actor_id}')::uuid;
    END IF;
    v_from := NULL;
    IF p_request #>> '{filters,from}' IS NOT NULL THEN
        v_from := (p_request #>> '{filters,from}')::timestamptz;
    END IF;
    v_to := NULL;
    IF p_request #>> '{filters,to}' IS NOT NULL THEN
        v_to := (p_request #>> '{filters,to}')::timestamptz;
    END IF;
    v_business_only := (p_request #>> '{filters,business_only}')::boolean;
    v_detail_key := p_request #>> '{filters,detail_key}';
    v_detail_value := p_request #>> '{filters,detail_value}';

    IF v_operation = 'list' AND jsonb_typeof(p_request -> 'after') = 'object' THEN
        SELECT array_agg(key ORDER BY key) INTO v_keys
        FROM jsonb_object_keys(p_request -> 'after') AS object_key(key);
        IF v_keys IS DISTINCT FROM ARRAY['created_at', 'id']::text[]
           OR jsonb_typeof(p_request #> '{after,created_at}') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request #> '{after,id}') IS DISTINCT FROM 'string'
           OR p_request #>> '{after,created_at}' !~ '(Z|[+-][0-9]{2}:[0-9]{2})$'
           OR p_request #>> '{after,id}' !~ '^-?[0-9]{1,19}$' THEN
            RAISE EXCEPTION 'invalid audit query cursor' USING ERRCODE = '22023';
        END IF;
        v_after_created_at := (p_request #>> '{after,created_at}')::timestamptz;
        v_after_id := (p_request #>> '{after,id}')::bigint;
    ELSIF v_operation = 'list'
          AND jsonb_typeof(p_request -> 'after') = 'null' THEN
        v_after_created_at := NULL;
        v_after_id := NULL;
    ELSIF v_operation = 'export'
          AND jsonb_typeof(p_request -> 'after') = 'null' THEN
        v_after_created_at := NULL;
        v_after_id := NULL;
    ELSE
        RAISE EXCEPTION 'invalid audit query cursor' USING ERRCODE = '22023';
    END IF;

    SELECT count(*) INTO v_total
    FROM public.audit_logs AS log
    WHERE (v_action IS NULL OR log.action = v_action)
      AND (v_target_type IS NULL OR log.target_type = v_target_type)
      AND (v_target_id IS NULL OR log.target_id = v_target_id)
      AND (v_actor_id IS NULL OR log.actor_id = v_actor_id)
      AND (v_from IS NULL OR log.created_at >= v_from)
      AND (v_to IS NULL OR log.created_at <= v_to)
      AND (NOT v_business_only OR log.action NOT LIKE 'http.%')
      AND (
          v_detail_key IS NULL OR v_detail_key = '' OR v_detail_value IS NULL
          OR log.detail_json @> jsonb_build_object(v_detail_key, v_detail_value)
      );

    IF v_operation = 'list' OR v_total <= 50000 THEN
        SELECT COALESCE(
            jsonb_agg(
                jsonb_build_object(
                    'id', page.id::text,
                    'actor_id', page.actor_id,
                    'actor_email', COALESCE(page.actor_email, profile.email),
                    'actor_role', page.actor_role,
                    'action', page.action,
                    'target_type', page.target_type,
                    'target_id', page.target_id,
                    'method', page.method,
                    'path', page.path,
                    'status_code', page.status_code,
                    'ip', CASE
                        WHEN page.ip IS NULL THEN NULL
                        WHEN pg_catalog.masklen(page.ip) = CASE pg_catalog.family(page.ip)
                            WHEN 4 THEN 32 ELSE 128
                        END THEN pg_catalog.host(page.ip)
                        ELSE page.ip::text
                    END,
                    'detail_json', page.detail_json,
                    'request_id', page.request_id,
                    'created_at', pg_catalog.to_char(
                        page.created_at AT TIME ZONE 'UTC',
                        'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                    )
                ) ORDER BY page.created_at DESC, page.id DESC
            ),
            '[]'::pg_catalog.jsonb
        ) INTO v_events
        FROM (
            SELECT log.id, log.actor_id, log.actor_email, log.actor_role,
                   log.action, log.target_type, log.target_id, log.method,
                   log.path, log.status_code, log.ip, log.detail_json,
                   log.request_id, log.created_at
            FROM public.audit_logs AS log
            WHERE (v_action IS NULL OR log.action = v_action)
              AND (v_target_type IS NULL OR log.target_type = v_target_type)
              AND (v_target_id IS NULL OR log.target_id = v_target_id)
              AND (v_actor_id IS NULL OR log.actor_id = v_actor_id)
              AND (v_from IS NULL OR log.created_at >= v_from)
              AND (v_to IS NULL OR log.created_at <= v_to)
              AND (NOT v_business_only OR log.action NOT LIKE 'http.%')
              AND (
                  v_detail_key IS NULL OR v_detail_key = '' OR v_detail_value IS NULL
                  OR log.detail_json @> jsonb_build_object(v_detail_key, v_detail_value)
              )
              AND (
                  v_operation = 'export' OR v_after_created_at IS NULL
                  OR log.created_at < v_after_created_at
                  OR (log.created_at = v_after_created_at AND log.id < v_after_id)
              )
            ORDER BY log.created_at DESC, log.id DESC
            LIMIT CASE WHEN v_operation = 'export' THEN 50000 ELSE v_page_size END
            OFFSET CASE
                WHEN v_operation = 'list' AND v_after_created_at IS NULL
                    THEN (v_page - 1) * v_page_size
                ELSE 0
            END
        ) AS page
        LEFT JOIN aap_identity_v1.account_profiles AS profile
            ON profile.id = page.actor_id;
    END IF;

    RETURN jsonb_build_object(
        'schema', 'aap-audit-query/v1',
        'ok', true,
        'data', jsonb_build_object(
            'operation', v_operation,
            'total', v_total::text,
            'events', v_events
        )
    );
END
$function$;

-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.query_events(pg_catalog.jsonb) FROM PUBLIC;
