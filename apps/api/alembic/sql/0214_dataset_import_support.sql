-- Narrow Jobs, Settings, Notifications, and Audit owner contracts for Assets import.
CREATE SCHEMA aap_settings_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
CREATE SCHEMA aap_notifications_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_settings_v1 FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_notifications_v1 FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_settings_v1.resolve_connector_host_allowlist(p_fallback jsonb)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_entries jsonb;
    v_source text := 'environment';
BEGIN
    IF p_fallback IS NULL
       OR pg_catalog.jsonb_typeof(p_fallback) IS DISTINCT FROM 'array' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-settings-connector-allowlist/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    IF pg_catalog.jsonb_array_length(p_fallback) > 256
       OR EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(p_fallback) AS entry(value)
            WHERE pg_catalog.jsonb_typeof(entry.value) IS DISTINCT FROM 'string'
               OR pg_catalog.char_length(entry.value #>> '{}') NOT BETWEEN 1 AND 253
               OR entry.value #>> '{}' ~ '[[:cntrl:]]'
       ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-settings-connector-allowlist/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    SELECT setting.value_json INTO v_entries
    FROM public.system_settings AS setting
    WHERE setting.key = 'connector_host_allowlist';
    IF FOUND AND v_entries IS NOT NULL AND v_entries IS DISTINCT FROM 'null'::jsonb THEN
        v_source := 'database';
    ELSE
        v_entries := p_fallback;
    END IF;

    IF pg_catalog.jsonb_typeof(v_entries) IS DISTINCT FROM 'array' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-settings-connector-allowlist/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_response')
        );
    END IF;
    IF pg_catalog.jsonb_array_length(v_entries) > 256 OR EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(v_entries) AS entry(value)
            WHERE pg_catalog.jsonb_typeof(entry.value) IS DISTINCT FROM 'string'
               OR pg_catalog.char_length(entry.value #>> '{}') NOT BETWEEN 1 AND 253
               OR entry.value #>> '{}' ~ '[[:cntrl:]]'
       ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-settings-connector-allowlist/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_response')
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-settings-connector-allowlist/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'entries', v_entries,
            'source', v_source
        )
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_settings_v1.resolve_connector_host_allowlist(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_notifications_v1.create_job_terminal_notification(
    p_actor_id uuid,
    p_job_id uuid,
    p_status text,
    p_payload jsonb
)
RETURNS uuid
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_type text;
    v_channels jsonb;
    v_id uuid;
    v_actor record;
BEGIN
    IF p_actor_id IS NULL OR p_job_id IS NULL OR p_status IS NULL OR p_status NOT IN (
        'completed', 'failed', 'cancelled'
    ) OR p_payload IS NULL OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR pg_catalog.pg_column_size(p_payload) > 16384 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid job notification';
    END IF;
    v_type := 'job.' || p_status;

    SELECT actor.actor_id, actor.platform_role, actor.is_active
    INTO v_actor
    FROM aap_identity_v1.read_actor_facts(p_actor_id) AS actor;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF v_actor.actor_id IS DISTINCT FROM p_actor_id
       OR v_actor.platform_role IS NULL OR v_actor.is_active IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'notification Identity facts mismatch';
    END IF;

    SELECT preference.channels INTO v_channels
    FROM public.notification_preferences AS preference
    WHERE preference.user_id = p_actor_id AND preference.type = v_type;
    IF FOUND AND pg_catalog.jsonb_typeof(v_channels -> 'in_app') = 'boolean'
       AND v_channels -> 'in_app' = 'false'::jsonb THEN
        RETURN NULL;
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'aap-dataset-import-notification:' || p_actor_id::text || ':'
                || v_type || ':' || p_job_id::text,
            0
        )
    );
    SELECT notification.id INTO v_id
    FROM public.notifications AS notification
    WHERE notification.user_id = p_actor_id
      AND notification.type = v_type
      AND notification.target_type = 'async_job'
      AND notification.target_id = p_job_id
    LIMIT 1;
    IF FOUND THEN
        RETURN v_id;
    END IF;

    INSERT INTO public.notifications (
        id, user_id, type, target_type, target_id, payload
    ) VALUES (
        pg_catalog.gen_random_uuid(), p_actor_id, v_type, 'async_job', p_job_id, p_payload
    ) RETURNING id INTO v_id;
    RETURN v_id;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_notifications_v1.create_job_terminal_notification(uuid, uuid, text, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1._dataset_import_job_reply(p_data jsonb, p_code text)
RETURNS jsonb
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_code IS NOT NULL THEN
        pg_catalog.jsonb_build_object(
            'schema', 'aap-dataset-import-job/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', p_code)
        )
    ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-dataset-import-job/v1',
        'ok', true,
        'data', p_data
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._dataset_import_job_reply(jsonb, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.create_dataset_import_job(
    p_job_id uuid, p_actor_id uuid, p_payload jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_id uuid;
BEGIN
    IF p_job_id IS NULL OR p_actor_id IS NULL
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY[
            'import_protocol', 'actor_id', 'dataset_id', 'dataset_display_id', 'dataset_name',
            'connection_id', 'connection_name', 'connection_kind', 'source_path',
            'recursive', 'include_globs', 'settings_snapshot'
       ]) <> '{}'::jsonb
       OR NOT (p_payload ?& ARRAY[
            'import_protocol', 'actor_id', 'dataset_id', 'dataset_display_id', 'dataset_name',
            'connection_id', 'connection_name', 'connection_kind', 'source_path',
            'recursive', 'include_globs', 'settings_snapshot'
       ])
       OR p_payload ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR p_payload ->> 'import_protocol' IS DISTINCT FROM 'aap-dataset-import/v1' THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'invalid_request');
    END IF;
    INSERT INTO public.async_jobs (
        id, kind, user_id, status, progress_pct, payload, result
    ) VALUES (
        p_job_id, 'dataset_import', p_actor_id, 'pending', 0, p_payload, '{}'::jsonb
    ) ON CONFLICT (id) DO NOTHING RETURNING id INTO v_id;
    IF v_id IS NULL THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'operation_conflict');
    END IF;
    RETURN aap_jobs_v1._dataset_import_job_reply(
        pg_catalog.jsonb_build_object('job_id', v_id), NULL
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.create_dataset_import_job(uuid, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.lock_dataset_import_job(p_job_id uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_job public.async_jobs%ROWTYPE;
BEGIN
    IF p_job_id IS NULL THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'invalid_request');
    END IF;
    SELECT job.* INTO v_job
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id AND job.kind = 'dataset_import'
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'job_missing');
    END IF;
    IF pg_catalog.jsonb_typeof(v_job.payload) IS DISTINCT FROM 'object'
       OR v_job.payload ->> 'import_protocol' IS DISTINCT FROM 'aap-dataset-import/v1'
       OR pg_catalog.jsonb_typeof(v_job.payload -> 'dataset_id') IS DISTINCT FROM 'string'
       OR COALESCE(
            v_job.payload ->> 'dataset_id'
                ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$',
            false
       ) IS NOT TRUE
       OR pg_catalog.jsonb_typeof(v_job.payload -> 'actor_id') IS DISTINCT FROM 'string'
       OR COALESCE(
            v_job.payload ->> 'actor_id'
                ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$',
            false
       ) IS NOT TRUE
       OR v_job.user_id IS NULL
       OR v_job.user_id::text IS DISTINCT FROM v_job.payload ->> 'actor_id' THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'operation_conflict');
    END IF;
    RETURN aap_jobs_v1._dataset_import_job_reply(
        pg_catalog.jsonb_build_object(
            'job_id', v_job.id,
            'actor_id', v_job.payload ->> 'actor_id',
            'dataset_id', v_job.payload ->> 'dataset_id',
            'user_id', v_job.user_id,
            'status', v_job.status,
            'cancel_requested', COALESCE(v_job.payload -> 'cancel_requested' = 'true'::jsonb, false)
        ),
        NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'dataset_busy');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.lock_dataset_import_job(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.claim_dataset_import_job(p_job_id uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_job public.async_jobs%ROWTYPE;
BEGIN
    IF p_job_id IS NULL THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'invalid_request');
    END IF;
    SELECT job.* INTO v_job
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id AND job.kind = 'dataset_import'
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'job_missing');
    END IF;
    IF pg_catalog.jsonb_typeof(v_job.payload) IS DISTINCT FROM 'object'
       OR v_job.payload ->> 'import_protocol' IS DISTINCT FROM 'aap-dataset-import/v1'
       OR v_job.user_id IS NULL
       OR v_job.user_id::text IS DISTINCT FROM v_job.payload ->> 'actor_id' THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'operation_conflict');
    END IF;
    IF v_job.status = 'cancelled'
       OR COALESCE(v_job.payload -> 'cancel_requested' = 'true'::jsonb, false) THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'import_cancelled');
    END IF;
    IF v_job.status NOT IN ('pending', 'running') THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'job_terminal');
    END IF;
    UPDATE public.async_jobs AS job
    SET status = 'running',
        started_at = COALESCE(job.started_at, pg_catalog.now()),
        updated_at = pg_catalog.now()
    WHERE job.id = p_job_id;
    RETURN aap_jobs_v1._dataset_import_job_reply(
        pg_catalog.jsonb_build_object('job_id', p_job_id, 'status', 'running'), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'dataset_busy');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.claim_dataset_import_job(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.update_dataset_import_job_progress(
    p_job_id uuid, p_stage text, p_progress integer,
    p_total_files integer DEFAULT NULL, p_total_bytes bigint DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_job public.async_jobs%ROWTYPE;
BEGIN
    IF p_job_id IS NULL OR p_stage IS NULL OR p_stage NOT IN ('enumerating', 'importing')
       OR p_progress IS NULL
       OR p_progress NOT BETWEEN 0 AND 100
       OR (p_total_files IS NOT NULL AND p_total_files < 0)
       OR (p_total_bytes IS NOT NULL AND p_total_bytes < 0)
       OR (p_total_files IS NULL) <> (p_total_bytes IS NULL) THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'invalid_request');
    END IF;
    SELECT job.* INTO v_job
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id AND job.kind = 'dataset_import'
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'job_missing');
    END IF;
    IF pg_catalog.jsonb_typeof(v_job.payload) IS DISTINCT FROM 'object'
       OR v_job.payload ->> 'import_protocol' IS DISTINCT FROM 'aap-dataset-import/v1'
       OR v_job.user_id IS NULL
       OR v_job.user_id::text IS DISTINCT FROM v_job.payload ->> 'actor_id' THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'operation_conflict');
    END IF;
    IF v_job.status NOT IN ('pending', 'running') THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'job_terminal');
    END IF;
    UPDATE public.async_jobs AS job
    SET progress_pct = p_progress,
        payload = job.payload || pg_catalog.jsonb_strip_nulls(
            pg_catalog.jsonb_build_object(
                'stage', p_stage,
                'total_files', p_total_files,
                'total_bytes', p_total_bytes
            )
        ),
        updated_at = pg_catalog.now()
    WHERE job.id = p_job_id;
    RETURN aap_jobs_v1._dataset_import_job_reply(
        pg_catalog.jsonb_build_object('job_id', p_job_id, 'progress_pct', p_progress), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'dataset_busy');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.update_dataset_import_job_progress(uuid, text, integer, integer, bigint) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1._dataset_import_notification_payload(
    p_job public.async_jobs, p_status text, p_result jsonb
)
RETURNS jsonb
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_strip_nulls(pg_catalog.jsonb_build_object(
        'kind', p_job.kind,
        'status', p_status,
        'dataset_id', p_job.payload ->> 'dataset_id',
        'dataset_display_id', p_job.payload ->> 'dataset_display_id',
        'dataset_name', p_job.payload ->> 'dataset_name',
        'connection_name', p_job.payload ->> 'connection_name',
        'connection_kind', p_job.payload ->> 'connection_kind',
        'error_message', p_job.error_message,
        'total_files', p_result -> 'total',
        'total', p_result -> 'total',
        'added', p_result -> 'added',
        'imported', p_result -> 'imported',
        'skipped', p_result -> 'skipped',
        'error_count', p_result -> 'error_count'
    ))
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._dataset_import_notification_payload(public.async_jobs, text, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1._dataset_import_result_valid(p_result jsonb)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_key text;
    v_error jsonb;
BEGIN
    IF p_result IS NULL OR pg_catalog.jsonb_typeof(p_result) IS DISTINCT FROM 'object'
       OR (p_result - ARRAY[
            'total', 'added', 'imported', 'skipped', 'error_count', 'errors', 'linked_tasks'
       ]) <> '{}'::jsonb
       OR NOT (p_result ?& ARRAY[
            'total', 'added', 'imported', 'skipped', 'error_count', 'errors', 'linked_tasks'
       ]) THEN
        RETURN false;
    END IF;
    FOREACH v_key IN ARRAY ARRAY['total', 'added', 'imported', 'skipped', 'error_count', 'linked_tasks'] LOOP
        IF pg_catalog.jsonb_typeof(p_result -> v_key) IS DISTINCT FROM 'number'
           OR pg_catalog.char_length(p_result ->> v_key) > 10
           OR (p_result ->> v_key) !~ '^(0|[1-9][0-9]*)$'
           OR (p_result ->> v_key)::numeric > 2147483647 THEN
            RETURN false;
        END IF;
    END LOOP;
    IF p_result ->> 'imported' IS DISTINCT FROM p_result ->> 'added'
       OR pg_catalog.jsonb_typeof(p_result -> 'errors') IS DISTINCT FROM 'array' THEN
        RETURN false;
    END IF;
    IF pg_catalog.jsonb_array_length(p_result -> 'errors') > 100 THEN
        RETURN false;
    END IF;
    FOR v_error IN SELECT entry.value FROM pg_catalog.jsonb_array_elements(p_result -> 'errors') AS entry(value) LOOP
        IF pg_catalog.jsonb_typeof(v_error) IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_error #>> '{}') > 512
           OR v_error #>> '{}' ~ '[[:cntrl:]]' THEN
            RETURN false;
        END IF;
    END LOOP;
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._dataset_import_result_valid(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.finish_dataset_import_job(
    p_job_id uuid, p_status text, p_result jsonb, p_error_code text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_notification_id uuid;
BEGIN
    IF p_job_id IS NULL OR p_status IS NULL OR p_status NOT IN ('completed', 'failed')
       OR NOT aap_jobs_v1._dataset_import_result_valid(p_result)
       OR (p_error_code IS NOT NULL AND p_error_code !~ '^[a-z][a-z0-9_]{0,63}$') THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'invalid_request');
    END IF;
    SELECT job.* INTO v_job
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id AND job.kind = 'dataset_import'
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'job_missing');
    END IF;
    IF pg_catalog.jsonb_typeof(v_job.payload) IS DISTINCT FROM 'object'
       OR v_job.payload ->> 'import_protocol' IS DISTINCT FROM 'aap-dataset-import/v1'
       OR v_job.user_id IS NULL
       OR v_job.user_id::text IS DISTINCT FROM v_job.payload ->> 'actor_id' THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'operation_conflict');
    END IF;
    IF v_job.status = 'cancelled' THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'import_cancelled');
    END IF;
    IF v_job.status NOT IN ('pending', 'running') THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'job_terminal');
    END IF;
    UPDATE public.async_jobs AS job
    SET status = p_status,
        progress_pct = CASE WHEN p_status = 'completed' THEN 100 ELSE job.progress_pct END,
        result = p_result,
        error_message = p_error_code,
        completed_at = pg_catalog.now(),
        updated_at = pg_catalog.now()
    WHERE job.id = p_job_id
    RETURNING job.* INTO v_job;
    IF v_job.user_id IS NOT NULL THEN
        v_notification_id := aap_notifications_v1.create_job_terminal_notification(
            v_job.user_id,
            v_job.id,
            p_status,
            aap_jobs_v1._dataset_import_notification_payload(v_job, p_status, p_result)
        );
    END IF;
    RETURN aap_jobs_v1._dataset_import_job_reply(
        pg_catalog.jsonb_build_object(
            'job_id', p_job_id,
            'status', p_status,
            'notification_id', v_notification_id
        ),
        NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'dataset_busy');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.finish_dataset_import_job(uuid, text, jsonb, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.cancel_dataset_import_job(
    p_actor_id uuid, p_job_id uuid, p_result jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_actor jsonb;
    v_notification_id uuid;
BEGIN
    IF p_actor_id IS NULL OR p_job_id IS NULL
       OR NOT aap_jobs_v1._dataset_import_result_valid(p_result) THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'invalid_request');
    END IF;
    v_actor := aap_identity_v1.lock_active_actor(p_actor_id);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR v_actor #>> '{data,actor_id}' IS DISTINCT FROM p_actor_id::text THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, COALESCE(v_actor #>> '{error,code}', 'account_not_active'));
    END IF;
    SELECT job.* INTO v_job
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id AND job.kind = 'dataset_import'
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'job_missing');
    END IF;
    IF pg_catalog.jsonb_typeof(v_job.payload) IS DISTINCT FROM 'object'
       OR v_job.payload ->> 'import_protocol' IS DISTINCT FROM 'aap-dataset-import/v1'
       OR v_job.user_id IS NULL
       OR v_job.user_id::text IS DISTINCT FROM v_job.payload ->> 'actor_id' THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'operation_conflict');
    END IF;
    IF v_job.user_id IS DISTINCT FROM p_actor_id
       AND v_actor #>> '{data,platform_role}' IS DISTINCT FROM 'super_admin' THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'manager_required');
    END IF;
    IF v_job.status NOT IN ('pending', 'running') THEN
        RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'job_terminal');
    END IF;
    UPDATE public.async_jobs AS job
    SET status = 'cancelled',
        result = p_result,
        completed_at = pg_catalog.now(),
        updated_at = pg_catalog.now()
    WHERE job.id = p_job_id
    RETURNING job.* INTO v_job;
    IF v_job.user_id IS NOT NULL THEN
        v_notification_id := aap_notifications_v1.create_job_terminal_notification(
            v_job.user_id,
            v_job.id,
            'cancelled',
            aap_jobs_v1._dataset_import_notification_payload(v_job, 'cancelled', p_result)
        );
    END IF;
    RETURN aap_jobs_v1._dataset_import_job_reply(
        pg_catalog.jsonb_build_object(
            'job_id', p_job_id,
            'status', 'cancelled',
            'notification_id', v_notification_id
        ),
        NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_jobs_v1._dataset_import_job_reply(NULL, 'dataset_busy');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.cancel_dataset_import_job(uuid, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_audit_v1.record_dataset_import(
    p_operation_id uuid,
    p_actor_id uuid,
    p_dataset_id uuid,
    p_detail jsonb,
    p_context jsonb
)
RETURNS bigint
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_audit_id bigint;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_dataset_id IS NULL
       OR pg_catalog.jsonb_typeof(p_detail) IS DISTINCT FROM 'object'
       OR (p_detail - ARRAY[
            'connection_id', 'connection_kind', 'source_path', 'recursive',
            'include_globs', 'settings_version'
       ]) <> '{}'::jsonb
       OR NOT (p_detail ?& ARRAY[
            'connection_id', 'connection_kind', 'source_path', 'recursive',
            'include_globs', 'settings_version'
       ])
       OR NOT aap_audit_v1.valid_request_context(p_context) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid dataset import audit metadata';
    END IF;
    v_actor := aap_identity_v1.lock_audit_actor(p_actor_id);
    IF v_actor IS NULL
       OR v_actor ->> 'schema' IS DISTINCT FROM 'aap-audit-actor/v1'
       OR v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR v_actor #>> '{data,actor_id}' IS DISTINCT FROM p_actor_id::text THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'dataset import audit actor unavailable';
    END IF;
    INSERT INTO public.audit_logs (
        actor_id, actor_email, actor_role, action, target_type, target_id,
        method, path, status_code, ip, request_id, detail_json
    ) VALUES (
        p_actor_id,
        v_actor #>> '{data,email}',
        v_actor #>> '{data,platform_role}',
        'dataset.import',
        'dataset',
        p_dataset_id::text,
        p_context ->> 'method',
        p_context ->> 'path',
        202,
        NULLIF(p_context ->> 'ip', '')::pg_catalog.inet,
        p_context ->> 'request_id',
        p_detail || pg_catalog.jsonb_build_object(
            'operation_id', p_operation_id,
            'dataset_id', p_dataset_id
        )
    ) RETURNING id INTO v_audit_id;
    RETURN v_audit_id;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.record_dataset_import(uuid, uuid, uuid, jsonb, jsonb) FROM PUBLIC;
