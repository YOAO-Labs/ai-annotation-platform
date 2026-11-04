CREATE OR REPLACE FUNCTION aap_audit_v1.maintenance_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_operation text;
    v_keys text[];
    v_partition jsonb;
    v_partitions jsonb := '[]'::jsonb;
    v_data jsonb := '{}'::jsonb;
    v_name text;
    v_oid oid;
    v_snapshot_lock integer;
    v_actual_oid oid;
    v_from date;
    v_to date;
    v_bound text;
    v_bounds text[];
    v_created boolean;
    v_rows jsonb;
    v_child record;
    v_namespace text;
    v_deleted bigint;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-audit-maintenance/v1'
       OR jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Audit maintenance request' USING ERRCODE = '22023';
    END IF;
    v_operation := p_request ->> 'operation';
    SELECT array_agg(key ORDER BY key) INTO v_keys
    FROM jsonb_object_keys(p_request) AS keys(key);

    IF v_operation IN ('partitions', 'refresh_daily') THEN
        IF v_keys IS DISTINCT FROM ARRAY['operation','schema']::text[] THEN
            RAISE EXCEPTION 'Invalid Audit maintenance request' USING ERRCODE = '22023';
        END IF;
    ELSIF v_operation = 'cleanup_filtering_fixture_events' THEN
        IF v_keys IS DISTINCT FROM ARRAY['namespace','operation','schema']::text[]
           OR (jsonb_typeof(p_request -> 'namespace') IS DISTINCT FROM 'null'
               AND (jsonb_typeof(p_request -> 'namespace') IS DISTINCT FROM 'string'
                    OR p_request ->> 'namespace' !~ '^[a-z0-9]{4,12}$')) THEN
            RAISE EXCEPTION 'Invalid Audit fixture cleanup request' USING ERRCODE = '22023';
        END IF;
        IF pg_catalog.lower(current_database()) !~ '(_test|_e2e)$' THEN
            RAISE EXCEPTION 'Audit fixture cleanup requires an isolated test database'
                USING ERRCODE = '42501';
        END IF;
        IF current_setting('app.allow_audit_update', true) IS DISTINCT FROM 'true' THEN
            RAISE EXCEPTION 'Audit fixture cleanup requires the caller immutability scope'
                USING ERRCODE = '42501';
        END IF;
        v_namespace := p_request ->> 'namespace';
    ELSIF v_operation = 'ensure' THEN
        IF v_keys IS DISTINCT FROM ARRAY['month','operation','schema']::text[]
           OR jsonb_typeof(p_request -> 'month') IS DISTINCT FROM 'string'
           OR p_request ->> 'month' !~ '^[0-9]{4}-(0[1-9]|1[0-2])-01$'
           OR left(p_request ->> 'month', 4) = '0000'
           OR p_request ->> 'month' >= '9999-12-01' THEN
            RAISE EXCEPTION 'Invalid Audit maintenance month' USING ERRCODE = '22023';
        END IF;
        v_from := (p_request ->> 'month')::date;
        v_to := (v_from + interval '1 month')::date;
        v_name := 'audit_logs_y' || to_char(v_from, 'YYYY"m"MM');
    ELSIF v_operation IN ('snapshot', 'drop', 'archive_event') THEN
        IF (v_operation IN ('snapshot', 'drop') AND
            v_keys IS DISTINCT FROM ARRAY['operation','partition','schema']::text[])
           OR (v_operation = 'archive_event' AND
            v_keys IS DISTINCT FROM ARRAY['object_key','operation','partition','row_count','schema']::text[])
           OR jsonb_typeof(p_request -> 'partition') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'Invalid Audit maintenance partition' USING ERRCODE = '22023';
        END IF;
        v_partition := p_request -> 'partition';
        SELECT array_agg(key ORDER BY key) INTO v_keys
        FROM jsonb_object_keys(v_partition) AS keys(key);
        IF v_keys IS DISTINCT FROM ARRAY['from','name','oid','to']::text[]
           OR jsonb_typeof(v_partition -> 'name') IS DISTINCT FROM 'string'
           OR jsonb_typeof(v_partition -> 'from') IS DISTINCT FROM 'string'
           OR jsonb_typeof(v_partition -> 'to') IS DISTINCT FROM 'string'
           OR jsonb_typeof(v_partition -> 'oid') IS DISTINCT FROM 'number'
           OR v_partition ->> 'from' !~ '^[0-9]{4}-(0[1-9]|1[0-2])-01$'
           OR left(v_partition ->> 'from', 4) = '0000'
           OR v_partition ->> 'from' >= '9999-12-01'
           OR v_partition ->> 'oid' !~ '^[1-9][0-9]{0,9}$' THEN
            RAISE EXCEPTION 'Invalid Audit maintenance partition' USING ERRCODE = '22023';
        END IF;
        IF (v_partition ->> 'oid')::bigint > 4294967295 THEN
            RAISE EXCEPTION 'Invalid Audit partition OID' USING ERRCODE = '22023';
        END IF;
        v_from := (v_partition ->> 'from')::date;
        v_to := (v_from + interval '1 month')::date;
        v_name := 'audit_logs_y' || to_char(v_from, 'YYYY"m"MM');
        v_oid := (v_partition ->> 'oid')::oid;
        v_snapshot_lock := (CASE WHEN v_oid::bigint > 2147483647
            THEN v_oid::bigint - 4294967296 ELSE v_oid::bigint END)::integer;
        IF v_partition ->> 'name' IS DISTINCT FROM v_name
           OR v_partition ->> 'to' IS DISTINCT FROM v_to::text
           OR v_to > date_trunc('month', clock_timestamp())::date THEN
            RAISE EXCEPTION 'Audit maintenance requires a completed UTC month'
                USING ERRCODE = '22023';
        END IF;
    ELSE
        RAISE EXCEPTION 'Invalid Audit maintenance operation' USING ERRCODE = '22023';
    END IF;

    IF v_operation = 'partitions' THEN
        FOR v_child IN
            SELECT child.oid, child.relname,
                   pg_get_expr(child.relpartbound, child.oid) AS bound
            FROM pg_inherits AS inheritance
            JOIN pg_class AS child ON child.oid = inheritance.inhrelid
            JOIN pg_namespace AS namespace ON namespace.oid = child.relnamespace
            WHERE inheritance.inhparent = 'public.audit_logs'::regclass
              AND namespace.nspname = 'public' AND child.relkind = 'r'
              AND child.relname ~ '^audit_logs_y[0-9]{4}m(0[1-9]|1[0-2])$'
              AND substring(child.relname FROM 13 FOR 4) <> '0000'
            ORDER BY child.relname
        LOOP
            v_from := make_date(substring(v_child.relname FROM 13 FOR 4)::integer,
                                right(v_child.relname, 2)::integer, 1);
            IF v_from >= date '9999-12-01' THEN
                CONTINUE;
            END IF;
            v_to := (v_from + interval '1 month')::date;
            v_bounds := regexp_match(v_child.bound,
                $bounds$^FOR VALUES FROM \('([^']+)'\) TO \('([^']+)'\)$$bounds$);
            IF v_bounds IS NULL
               OR v_bounds[1]::timestamptz IS DISTINCT FROM v_from::timestamptz
               OR v_bounds[2]::timestamptz IS DISTINCT FROM v_to::timestamptz THEN
                CONTINUE;
            END IF;
            v_partitions := v_partitions || jsonb_build_array(jsonb_build_object(
                'name', v_child.relname, 'from', v_from::text, 'to', v_to::text,
                'oid', v_child.oid::bigint
            ));
        END LOOP;
        v_data := jsonb_build_object('partitions', v_partitions);
    ELSIF v_operation = 'ensure' THEN
        PERFORM pg_advisory_xact_lock(hashtextextended('aap-audit-maintenance/v1:ensure', 0));
        v_created := to_regclass(format('public.%I', v_name)) IS NULL;
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS public.%I PARTITION OF public.audit_logs FOR VALUES FROM (%L) TO (%L)',
            v_name, v_from::text || ' 00:00:00+00', v_to::text || ' 00:00:00+00'
        );
        SELECT child.oid, pg_get_expr(child.relpartbound, child.oid)
        INTO v_actual_oid, v_bound
        FROM pg_inherits AS inheritance
        JOIN pg_class AS child ON child.oid = inheritance.inhrelid
        JOIN pg_namespace AS namespace ON namespace.oid = child.relnamespace
        WHERE inheritance.inhparent = 'public.audit_logs'::regclass
          AND namespace.nspname = 'public' AND child.relname = v_name
          AND child.relkind = 'r';
        v_bounds := regexp_match(v_bound,
            $bounds$^FOR VALUES FROM \('([^']+)'\) TO \('([^']+)'\)$$bounds$);
        IF v_actual_oid IS NULL OR v_bounds IS NULL
           OR v_bounds[1]::timestamptz IS DISTINCT FROM v_from::timestamptz
           OR v_bounds[2]::timestamptz IS DISTINCT FROM v_to::timestamptz THEN
            RAISE EXCEPTION 'Audit partition name or bounds conflict' USING ERRCODE = '55000';
        END IF;
        v_data := jsonb_build_object('created', v_created);
    ELSIF v_operation IN ('snapshot', 'drop') THEN
        SELECT child.oid, pg_get_expr(child.relpartbound, child.oid)
        INTO v_actual_oid, v_bound
        FROM pg_inherits AS inheritance
        JOIN pg_class AS child ON child.oid = inheritance.inhrelid
        JOIN pg_namespace AS namespace ON namespace.oid = child.relnamespace
        WHERE inheritance.inhparent = 'public.audit_logs'::regclass
          AND child.oid = v_oid AND namespace.nspname = 'public'
          AND child.relname = v_name AND child.relkind = 'r';
        v_bounds := regexp_match(v_bound,
            $bounds$^FOR VALUES FROM \('([^']+)'\) TO \('([^']+)'\)$$bounds$);
        IF v_actual_oid IS NULL OR v_bounds IS NULL
           OR v_bounds[1]::timestamptz IS DISTINCT FROM v_from::timestamptz
           OR v_bounds[2]::timestamptz IS DISTINCT FROM v_to::timestamptz THEN
            RAISE EXCEPTION 'Audit partition identity changed' USING ERRCODE = '55000';
        END IF;
        IF v_operation = 'snapshot' THEN
            -- Hold the archive snapshot stable until the caller commits its upload/drop work.
            EXECUTE format('LOCK TABLE ONLY public.%I IN ACCESS EXCLUSIVE MODE', v_name);
            IF to_regclass(format('public.%I', v_name)) IS DISTINCT FROM v_oid
               OR NOT EXISTS (
                   SELECT FROM pg_inherits AS inheritance
                   JOIN pg_class AS child ON child.oid = inheritance.inhrelid
                   WHERE inheritance.inhparent = 'public.audit_logs'::regclass
                     AND child.oid = v_oid
                     AND pg_get_expr(child.relpartbound, child.oid) = v_bound
               ) THEN
                RAISE EXCEPTION 'Audit partition identity changed' USING ERRCODE = '55000';
            END IF;
            -- CREATE also holds a table lock; mark an actual archive snapshot separately.
            PERFORM pg_advisory_xact_lock(1094796612, v_snapshot_lock);
            SELECT COALESCE(jsonb_agg(to_jsonb(event) ORDER BY event.id), '[]'::jsonb)
            INTO v_rows FROM aap_audit_v1.audit_events AS event
            WHERE event.created_at >= v_from::timestamptz
              AND event.created_at < v_to::timestamptz;
            v_data := jsonb_build_object('rows', v_rows);
        ELSE
            IF NOT EXISTS (
                SELECT FROM pg_locks WHERE pid = pg_backend_pid() AND relation = v_oid
                  AND mode = 'AccessExclusiveLock' AND granted
            ) OR NOT EXISTS (
                SELECT FROM pg_locks WHERE pid = pg_backend_pid() AND locktype = 'advisory'
                  AND classid = 1094796612::oid AND objid = v_oid AND objsubid = 2
                  AND mode = 'ExclusiveLock' AND granted
            ) THEN
                RAISE EXCEPTION 'Audit retirement requires the caller archive snapshot lock'
                    USING ERRCODE = '55000';
            END IF;
            EXECUTE format('DROP TABLE public.%I RESTRICT', v_name);
        END IF;
    ELSIF v_operation = 'archive_event' THEN
        IF jsonb_typeof(p_request -> 'row_count') IS DISTINCT FROM 'number'
           OR p_request ->> 'row_count' !~ '^(0|[1-9][0-9]*)$'
           OR length(p_request ->> 'row_count') > 19
           OR (p_request ->> 'row_count')::numeric > 9223372036854775807
           OR jsonb_typeof(p_request -> 'object_key') IS DISTINCT FROM 'string'
           OR p_request ->> 'object_key' IS DISTINCT FROM
                to_char(v_from, 'YYYY/MM') || '.jsonl.gz' THEN
            RAISE EXCEPTION 'Invalid Audit archive event' USING ERRCODE = '22023';
        END IF;
        PERFORM aap_audit_v1.append_events(jsonb_build_object(
            'schema', 'aap-audit-append/v1', 'events', jsonb_build_array(jsonb_build_object(
                'actor_id', NULL, 'actor_email', NULL, 'actor_role', NULL,
                'action', 'audit.archive', 'target_type', 'audit_partition',
                'target_id', v_name, 'method', NULL, 'path', NULL,
                'status_code', 200, 'ip', NULL, 'request_id', NULL,
                'detail_json', jsonb_build_object('partition', v_name,
                    'row_count', (p_request ->> 'row_count')::bigint,
                    'archive_key', p_request ->> 'object_key', 'from', v_from::text)
            ))
        ));
    ELSIF v_operation = 'cleanup_filtering_fixture_events' THEN
        DELETE FROM public.audit_logs AS log
        WHERE log.path = '/api/v1/__test/seed/filtering'
          AND ((v_namespace IS NULL AND log.detail_json ->> 'fixture' = 'filtering')
               OR (v_namespace IS NOT NULL
                   AND log.detail_json ->> 'namespace' = v_namespace));
        GET DIAGNOSTICS v_deleted = ROW_COUNT;
        v_data := jsonb_build_object('deleted_rows', v_deleted);
    ELSE
        PERFORM aap_audit_v1.refresh_audit_bi_mv();
    END IF;

    RETURN jsonb_build_object('schema', 'aap-audit-maintenance/v1', 'ok', true, 'data', v_data);
END
$function$;
