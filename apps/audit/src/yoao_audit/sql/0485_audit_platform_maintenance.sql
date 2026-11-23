CREATE FUNCTION yoao_audit_v1._assert_platform_partition(
    p_month date, p_parent bigint, p_source bigint
) RETURNS void
LANGUAGE plpgsql STABLE
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT FROM pg_class AS parent
        JOIN pg_partitioned_table AS key ON key.partrelid = parent.oid
        JOIN pg_attribute AS attribute ON attribute.attrelid = parent.oid
            AND attribute.attname = 'created_at' AND NOT attribute.attisdropped
        JOIN pg_inherits AS inheritance ON inheritance.inhparent = parent.oid
        JOIN pg_class AS leaf ON leaf.oid = inheritance.inhrelid
        WHERE parent.oid = 'public.audit_logs'::regclass
          AND parent.oid::bigint = p_parent
          AND parent.relkind = 'p' AND parent.relpersistence = 'p'
          AND key.partstrat = 'r' AND key.partnatts = 1
          AND key.partattrs::text = attribute.attnum::text
          AND attribute.atttypid = 'timestamptz'::regtype
          AND leaf.oid::bigint = p_source
          AND leaf.relnamespace = 'public'::regnamespace
          AND leaf.relname = 'audit_logs_y' || to_char(p_month, 'YYYY"m"MM')
          AND leaf.relkind = 'r' AND leaf.relpersistence = 'p'
          AND leaf.relispartition AND NOT inheritance.inhdetachpending
          AND NOT EXISTS (SELECT FROM pg_inherits WHERE inhparent = leaf.oid)
          AND (SELECT count(*) FROM pg_inherits WHERE inhrelid = leaf.oid) = 1
          AND pg_get_expr(leaf.relpartbound, leaf.oid) = format(
              'FOR VALUES FROM (%L) TO (%L)', p_month::timestamptz::text,
              (p_month + interval '1 month')::timestamptz::text
          )
    ) THEN
        RAISE EXCEPTION 'Audit platform partition identity changed' USING ERRCODE = '55000';
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_audit_v1._assert_platform_partition(date,bigint,bigint) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_audit_v1.platform_maintenance(
    p_operation text, p_month date, p_parent bigint, p_source bigint,
    p_cursor text, p_output uuid, p_published_at timestamptz
) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_name text;
    v_end date;
    v_cursor refcursor;
BEGIN
    IF p_operation = 'refresh_daily' AND p_month IS NULL AND p_parent IS NULL
       AND p_source IS NULL AND p_cursor IS NULL AND p_output IS NULL
       AND p_published_at IS NULL THEN
        PERFORM yoao_audit_v1.refresh_audit_bi_mv();
        RETURN;
    END IF;
    IF p_month IS NULL OR p_month < date '2000-01-01' OR p_month >= date '9999-12-01'
       OR extract(day FROM p_month) <> 1 THEN
        RAISE EXCEPTION 'Invalid Audit platform partition month' USING ERRCODE = '22023';
    END IF;
    v_name := 'audit_logs_y' || to_char(p_month, 'YYYY"m"MM');
    v_end := (p_month + interval '1 month')::date;

    IF p_operation = 'ensure' AND p_parent IS NULL AND p_source IS NULL
       AND p_cursor IS NULL AND p_output IS NULL AND p_published_at IS NULL THEN
        LOCK TABLE ONLY public.audit_logs IN ACCESS EXCLUSIVE MODE NOWAIT;
        IF to_regclass(format('public.%I', v_name)) IS NOT NULL THEN
            RAISE EXCEPTION 'Audit platform partition name conflict' USING ERRCODE = '55000';
        END IF;
        EXECUTE format(
            'CREATE TABLE public.%I PARTITION OF public.audit_logs FOR VALUES FROM (%L) TO (%L)',
            v_name, p_month::timestamptz, v_end::timestamptz
        );
        PERFORM yoao_audit_v1._assert_platform_partition(p_month,
            'public.audit_logs'::regclass::oid::bigint,
            to_regclass(format('public.%I', v_name))::oid::bigint);
        RETURN;
    END IF;

    IF p_operation IS NULL OR p_operation NOT IN ('lock_source','open_source','lock_retirement','retire')
       OR p_parent IS NULL OR p_source IS NULL
       OR (p_operation = 'open_source') IS DISTINCT FROM (p_cursor IS NOT NULL)
       OR (p_operation = 'retire') IS DISTINCT FROM (p_output IS NOT NULL)
       OR (p_operation = 'retire') IS DISTINCT FROM (p_published_at IS NOT NULL)
       OR v_end > date_trunc('month', clock_timestamp())::date THEN
        RAISE EXCEPTION 'Invalid Audit platform archive operation' USING ERRCODE = '22023';
    END IF;
    PERFORM yoao_audit_v1._assert_platform_partition(p_month,p_parent,p_source);
    IF p_operation = 'lock_source' THEN
        EXECUTE format('LOCK TABLE ONLY public.%I IN SHARE MODE NOWAIT', v_name);
        PERFORM yoao_audit_v1._assert_platform_partition(p_month,p_parent,p_source);
        RETURN;
    END IF;

    IF NOT EXISTS (
        SELECT FROM pg_locks
        WHERE pid = pg_backend_pid() AND locktype = 'relation' AND relation::bigint = p_source
          AND mode = 'ShareLock' AND granted
    ) THEN
        RAISE EXCEPTION 'Audit platform source lock is required' USING ERRCODE = '55000';
    END IF;
    IF p_operation = 'open_source' THEN
        IF p_cursor !~ '^audit_archive_[0-9a-f]{32}$' THEN
            RAISE EXCEPTION 'Invalid Audit platform archive cursor' USING ERRCODE = '22023';
        END IF;
        v_cursor := p_cursor;
        OPEN v_cursor NO SCROLL FOR EXECUTE format(
            'SELECT id, actor_id, actor_email, actor_role, action, target_type, target_id,
                    method, path, status_code, ip, detail_json, request_id, created_at
             FROM ONLY public.%I ORDER BY id, created_at', v_name
        );
    ELSIF p_operation = 'lock_retirement' THEN
        LOCK TABLE ONLY public.audit_logs IN ACCESS EXCLUSIVE MODE NOWAIT;
        EXECUTE format('LOCK TABLE ONLY public.%I IN ACCESS EXCLUSIVE MODE NOWAIT', v_name);
        PERFORM yoao_audit_v1._assert_platform_partition(p_month,p_parent,p_source);
    ELSE
        IF 2 <> (
            SELECT count(DISTINCT relation) FROM pg_locks
            WHERE pid = pg_backend_pid() AND locktype = 'relation'
              AND relation::bigint IN (p_parent,p_source) AND mode = 'AccessExclusiveLock' AND granted
        ) THEN
            RAISE EXCEPTION 'Audit platform retirement requires exclusive locks' USING ERRCODE = '55000';
        END IF;
        PERFORM FROM public.audit_archive_outputs AS output
        WHERE output.id = p_output AND output.state = 'pending'
          AND output.source_schema = 'public' AND output.source_table = v_name
          AND output.parent_oid = p_parent AND output.source_oid = p_source
          AND output.month_start = p_month AND output.month_end = v_end
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Audit platform retirement requires its pending candidate' USING ERRCODE = '55000';
        END IF;
        EXECUTE format('DROP TABLE public.%I RESTRICT', v_name);
        UPDATE public.audit_archive_outputs
        SET state = 'ready', published_at = p_published_at
        WHERE id = p_output;
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_audit_v1.platform_maintenance(text,date,bigint,bigint,text,uuid,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_audit_v1.archive_output_has_ready(p_year integer, p_month integer)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT FROM public.audit_archive_outputs
        WHERE year = p_year AND month = p_month AND state = 'ready'
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_audit_v1.archive_output_has_ready(integer,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_audit_v1.archive_output_ready_month(p_year integer, p_month integer)
RETURNS SETOF public.audit_archive_outputs
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT output.* FROM public.audit_archive_outputs AS output
    WHERE output.year = p_year AND output.month = p_month AND output.state = 'ready'
    FOR SHARE;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_audit_v1.archive_output_ready_month(integer,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_audit_v1.archive_output_pending(p_output uuid)
RETURNS SETOF public.audit_archive_outputs
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT output.* FROM public.audit_archive_outputs AS output
    WHERE output.id = p_output AND output.state = 'pending'
    FOR UPDATE NOWAIT;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_audit_v1.archive_output_pending(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_audit_v1.archive_output_ready_list()
RETURNS TABLE(year integer, month integer, id uuid, size_bytes bigint,
              published_at timestamptz, object_key text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT output.year, output.month, output.id, output.size_bytes,
           output.published_at, output.storage_ref->>'object_key'
    FROM public.audit_archive_outputs AS output
    WHERE output.state = 'ready';
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_audit_v1.archive_output_ready_list() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_audit_v1.archive_output_history()
RETURNS TABLE(pending bigint, ready bigint)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT count(*) FILTER (WHERE state = 'pending'), count(*) FILTER (WHERE state = 'ready')
    FROM public.audit_archive_outputs;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_audit_v1.archive_output_history() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_audit_v1.archive_output_save_candidate(
    p_id uuid, p_job_id uuid, p_month date, p_parent bigint, p_source bigint,
    p_rows bigint, p_source_size bigint, p_source_sha text, p_size bigint,
    p_sha text, p_ref jsonb, p_created_at timestamptz
) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
BEGIN
    PERFORM yoao_audit_v1._assert_platform_partition(p_month,p_parent,p_source);
    IF (p_month + interval '1 month')::date > date_trunc('month', clock_timestamp())::date
       OR NOT EXISTS (
            SELECT FROM pg_locks WHERE pid = pg_backend_pid() AND locktype = 'relation'
              AND relation::bigint = p_source AND mode = 'ShareLock' AND granted
       ) THEN
        RAISE EXCEPTION 'Audit archive candidate requires its locked completed source' USING ERRCODE = '55000';
    END IF;
    INSERT INTO public.audit_archive_outputs (
        id, job_id, year, month, source_schema, source_table, parent_oid, source_oid,
        month_start, month_end, format_revision, row_count, source_size_bytes,
        source_sha256, size_bytes, sha256, storage_ref, state, created_at
    ) VALUES (
        p_id, p_job_id, extract(year FROM p_month), extract(month FROM p_month), 'public',
        'audit_logs_y' || to_char(p_month, 'YYYY"m"MM'), p_parent, p_source,
        p_month, (p_month + interval '1 month')::date, 1, p_rows, p_source_size,
        p_source_sha, p_size, p_sha, p_ref, 'pending', p_created_at
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_audit_v1.archive_output_save_candidate(
    uuid,uuid,date,bigint,bigint,bigint,bigint,text,bigint,text,jsonb,timestamptz
) FROM PUBLIC;
