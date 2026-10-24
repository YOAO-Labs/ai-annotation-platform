CREATE VIEW aap_jobs_v1.async_job_creation AS
SELECT
    job.id,
    job.kind,
    job.project_id,
    job.retained_project_id,
    job.user_id,
    job.status,
    job.progress_pct,
    job.payload,
    job.result,
    job.error_message,
    job.celery_task_id,
    job.started_at,
    job.completed_at,
    job.created_at,
    job.updated_at
FROM public.async_jobs AS job
WHERE pg_catalog.has_table_privilege(
          current_user,
          'public.async_jobs',
          'INSERT'
      )
   OR (
       job.status = 'pending'
       AND job.progress_pct = 0
       AND job.result = '{}'::pg_catalog.jsonb
       AND job.retained_project_id IS NULL
       AND job.started_at IS NULL
       AND job.completed_at IS NULL
       AND job.error_message IS NULL
   )
WITH LOCAL CHECK OPTION;
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.async_job_creation FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.read_job(p_job_id uuid)
RETURNS TABLE(
    id uuid,
    kind character varying,
    project_id uuid,
    retained_project_id uuid,
    user_id uuid,
    status character varying,
    progress_pct integer,
    payload jsonb,
    result jsonb,
    error_message text,
    celery_task_id character varying,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone,
    updated_at timestamp with time zone
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT
        job.id,
        job.kind,
        job.project_id,
        job.retained_project_id,
        job.user_id,
        job.status,
        job.progress_pct,
        job.payload,
        job.result,
        job.error_message,
        job.celery_task_id,
        job.started_at,
        job.completed_at,
        job.created_at,
        job.updated_at
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.read_job(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.jobs_by_execution_id(p_execution_id text)
RETURNS TABLE(
    id uuid,
    kind character varying,
    project_id uuid,
    retained_project_id uuid,
    user_id uuid,
    status character varying,
    progress_pct integer,
    payload jsonb,
    result jsonb,
    error_message text,
    celery_task_id character varying,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone,
    updated_at timestamp with time zone
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT
        job.id,
        job.kind,
        job.project_id,
        job.retained_project_id,
        job.user_id,
        job.status,
        job.progress_pct,
        job.payload,
        job.result,
        job.error_message,
        job.celery_task_id,
        job.started_at,
        job.completed_at,
        job.created_at,
        job.updated_at
    FROM public.async_jobs AS job
    WHERE job.celery_task_id = p_execution_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.jobs_by_execution_id(text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.insert_video_summary(
    p_tracker_job_id uuid,
    p_kind text,
    p_user_id uuid,
    p_project_id uuid,
    p_payload jsonb,
    p_celery_task_id text
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_inserted_id uuid;
BEGIN
    IF p_tracker_job_id IS NULL
       OR p_kind IS NULL
       OR p_kind NOT IN ('video_tracker', 'video_correction')
       OR p_payload IS NULL
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR p_payload ->> 'video_tracker_job_id'
            IS DISTINCT FROM p_tracker_job_id::text THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid_video_tracker_summary';
    END IF;

    INSERT INTO public.async_jobs (
        id,
        kind,
        user_id,
        project_id,
        payload,
        celery_task_id,
        status
    )
    VALUES (
        p_tracker_job_id,
        p_kind,
        p_user_id,
        p_project_id,
        p_payload || pg_catalog.jsonb_build_object(
            'video_execution_protocol', 'aap-video-execution-data/v1'
        ),
        p_celery_task_id,
        'pending'
    )
    ON CONFLICT (id) DO NOTHING
    RETURNING id INTO v_inserted_id;

    RETURN v_inserted_id IS NOT NULL;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.insert_video_summary(
    uuid, text, uuid, uuid, jsonb, text
) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.mark_dispatch_failed(
    p_job_id uuid,
    p_completed_at timestamp with time zone,
    p_error text
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_changed_id uuid;
BEGIN
    UPDATE public.async_jobs AS job
    SET status = 'failed',
        completed_at = p_completed_at,
        updated_at = pg_catalog.now(),
        error_message = pg_catalog.left(COALESCE(p_error, ''), 4000)
    WHERE job.id = p_job_id
      AND job.status = 'pending'
    RETURNING job.id INTO v_changed_id;

    RETURN v_changed_id IS NOT NULL;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.mark_dispatch_failed(
    uuid, timestamp with time zone, text
) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.cancel_batch_predict_jobs(
    p_batch_id uuid,
    p_completed_at timestamp with time zone
)
RETURNS uuid[]
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_changed_ids uuid[];
BEGIN
    WITH changed AS (
        UPDATE public.async_jobs AS job
        SET status = 'cancelled',
            completed_at = p_completed_at,
            updated_at = pg_catalog.now()
        WHERE job.kind = 'batch_predict'
          AND job.payload ->> 'batch_id' = p_batch_id::text
          AND job.status IN ('pending', 'running')
        RETURNING job.id
    )
    SELECT COALESCE(
        pg_catalog.array_agg(changed.id), ARRAY[]::uuid[]
    )
    INTO v_changed_ids
    FROM changed;

    RETURN v_changed_ids;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.cancel_batch_predict_jobs(
    uuid, timestamp with time zone
) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.delete_batch_predict_jobs(p_batch_id uuid)
RETURNS uuid[]
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_deleted_ids uuid[];
BEGIN
    WITH deleted AS (
        DELETE FROM public.async_jobs AS job
        WHERE job.kind = 'batch_predict'
          AND job.payload ->> 'batch_id' = p_batch_id::text
        RETURNING job.id
    )
    SELECT COALESCE(
        pg_catalog.array_agg(deleted.id), ARRAY[]::uuid[]
    )
    INTO v_deleted_ids
    FROM deleted;

    RETURN v_deleted_ids;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.delete_batch_predict_jobs(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.delete_terminal_jobs_before(
    p_cutoff timestamp with time zone,
    p_statuses text[]
)
RETURNS uuid[]
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_deleted_ids uuid[];
BEGIN
    WITH deleted AS (
        DELETE FROM public.async_jobs AS job
        WHERE job.status::text = ANY(p_statuses)
          AND job.created_at < p_cutoff
          AND (
              job.kind <> 'dataset_import'
              OR job.payload ->> 'import_protocol'
                    IS DISTINCT FROM 'aap-dataset-import/v1'
          )
        RETURNING job.id
    )
    SELECT COALESCE(
        pg_catalog.array_agg(deleted.id), ARRAY[]::uuid[]
    )
    INTO v_deleted_ids
    FROM deleted;

    RETURN v_deleted_ids;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.delete_terminal_jobs_before(
    timestamp with time zone, text[]
) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1._jobs_lifecycle_has_keys(
    p_value jsonb,
    p_keys text[]
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE
        WHEN pg_catalog.jsonb_typeof(p_value) IS DISTINCT FROM 'object' THEN false
        ELSE (
            SELECT pg_catalog.count(*) = pg_catalog.cardinality(p_keys)
            FROM pg_catalog.jsonb_object_keys(p_value) AS fields(field_name)
        )
        AND NOT EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_object_keys(p_value) AS fields(field_name)
            WHERE field_name <> ALL(p_keys)
        )
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._jobs_lifecycle_has_keys(jsonb, text[])
FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1._jobs_lifecycle_is_truthy(p_value jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_type text := pg_catalog.jsonb_typeof(p_value);
BEGIN
    IF p_value IS NULL OR v_type IS NULL OR v_type = 'null' THEN
        RETURN false;
    ELSIF v_type = 'boolean' THEN
        RETURN p_value = 'true'::pg_catalog.jsonb;
    ELSIF v_type = 'number' THEN
        RETURN (p_value #>> '{}')::numeric <> 0;
    ELSIF v_type = 'string' THEN
        RETURN pg_catalog.char_length(p_value #>> '{}') > 0;
    ELSIF v_type = 'array' THEN
        RETURN pg_catalog.jsonb_array_length(p_value) > 0;
    ELSIF v_type = 'object' THEN
        RETURN EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_object_keys(p_value)
        );
    END IF;
    RETURN false;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._jobs_lifecycle_is_truthy(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1._jobs_lifecycle_integer(p_value jsonb)
RETURNS TABLE(is_valid boolean, integer_value numeric)
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_type text := pg_catalog.jsonb_typeof(p_value);
    v_text text;
BEGIN
    is_valid := false;
    integer_value := NULL;

    IF v_type = 'boolean' THEN
        integer_value := CASE WHEN p_value = 'true'::pg_catalog.jsonb THEN 1 ELSE 0 END;
        is_valid := true;
    ELSIF v_type = 'number' THEN
        integer_value := pg_catalog.trunc((p_value #>> '{}')::numeric);
        is_valid := true;
    ELSIF v_type = 'string' THEN
        v_text := p_value #>> '{}';
        IF v_text ~ '^[[:space:]]*[+-]?[0-9]+(_[0-9]+)*[[:space:]]*$' THEN
            integer_value := pg_catalog.regexp_replace(
                pg_catalog.btrim(v_text), '_', '', 'g'
            )::numeric;
            is_valid := true;
        END IF;
    END IF;

    RETURN NEXT;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._jobs_lifecycle_integer(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.apply_lifecycle(p_job_id uuid, p_change jsonb)
RETURNS TABLE(applied boolean, updated_at timestamp with time zone)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_changed_fields text[] := ARRAY[]::text[];
    v_written_fields text[];
    v_expected_fields text[];
    v_field text;
    v_operation text;
    v_reads jsonb;
    v_values jsonb;
    v_arguments jsonb;
    v_command jsonb;
    v_lifecycle_values jsonb := '{}'::jsonb;
    v_patch jsonb;
    v_set_clause text := '';
    v_expression text;
    v_status text;
    v_error text;
    v_invoker text;
    v_updated_at_override_present boolean;
    v_updated_at_override timestamp with time zone;
    v_row_updated_at timestamp with time zone;
    v_row_count bigint;
    v_pct_valid boolean;
    v_pct_numeric numeric;
    v_pct_integer integer;
    v_extra_truthy boolean;
    v_allowed_statuses text[];
    v_keys_count bigint;
BEGIN
    IF p_job_id IS NULL
       OR NOT aap_jobs_v1._jobs_lifecycle_has_keys(
           p_change,
           ARRAY[
               'schema',
               'commands',
               'projection_changes',
               'changed_fields',
               'updated_at_override_present',
               'updated_at_override',
               'legacy_changes'
           ]
       )
       OR p_change ->> 'schema' <> 'aap-jobs-lifecycle/v1'
       OR pg_catalog.jsonb_typeof(p_change -> 'commands') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(p_change -> 'projection_changes')
            IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_change -> 'changed_fields')
            IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(p_change -> 'updated_at_override_present')
            IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(p_change -> 'legacy_changes')
            IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid_jobs_lifecycle_request';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_object_keys(p_change -> 'projection_changes') AS keys(key)
        WHERE keys.key NOT IN ('payload', 'result')
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_object_keys(p_change -> 'legacy_changes') AS keys(key)
        WHERE keys.key NOT IN (
            'id', 'kind', 'project_id', 'retained_project_id', 'user_id',
            'status', 'progress_pct', 'payload', 'result', 'error_message',
            'celery_task_id', 'started_at', 'completed_at', 'created_at', 'updated_at'
        )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid_jobs_lifecycle_request';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(p_change -> 'changed_fields') AS fields(value)
        WHERE pg_catalog.jsonb_typeof(fields.value) IS DISTINCT FROM 'string'
           OR fields.value #>> '{}' NOT IN (
               'id', 'kind', 'project_id', 'retained_project_id', 'user_id',
               'status', 'progress_pct', 'payload', 'result', 'error_message',
               'celery_task_id', 'started_at', 'completed_at', 'created_at', 'updated_at'
           )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid_jobs_lifecycle_request';
    END IF;

    SELECT COALESCE(
        pg_catalog.array_agg(fields.value #>> '{}' ORDER BY fields.ordinality),
        ARRAY[]::text[]
    )
    INTO v_changed_fields
    FROM pg_catalog.jsonb_array_elements(p_change -> 'changed_fields')
        WITH ORDINALITY AS fields(value, ordinality);

    SELECT pg_catalog.count(*) INTO v_keys_count
    FROM pg_catalog.unnest(v_changed_fields) AS fields(value);
    IF v_keys_count <> (
        SELECT pg_catalog.count(DISTINCT fields.value)
        FROM pg_catalog.unnest(v_changed_fields) AS fields(value)
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'duplicate_jobs_lifecycle_field';
    END IF;

    v_updated_at_override_present :=
        p_change -> 'updated_at_override_present' = 'true'::pg_catalog.jsonb;
    IF v_updated_at_override_present THEN
        IF NOT ('updated_at' = ANY(v_changed_fields))
           OR pg_catalog.jsonb_typeof(p_change -> 'updated_at_override')
                NOT IN ('string', 'null') THEN
            RAISE EXCEPTION USING
                ERRCODE = '22023',
                MESSAGE = 'invalid_jobs_lifecycle_request';
        END IF;
        v_updated_at_override := (
            pg_catalog.jsonb_populate_record(
                NULL::public.async_jobs,
                pg_catalog.jsonb_build_object(
                    'updated_at', p_change -> 'updated_at_override'
                )
            )
        ).updated_at;
    ELSIF 'updated_at' = ANY(v_changed_fields)
       OR p_change -> 'updated_at_override' IS DISTINCT FROM 'null'::pg_catalog.jsonb THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid_jobs_lifecycle_request';
    END IF;

    IF (p_change -> 'legacy_changes') ? 'updated_at'
       AND (
           NOT v_updated_at_override_present
           OR (p_change -> 'legacy_changes' -> 'updated_at')
                IS DISTINCT FROM p_change -> 'updated_at_override'
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid_jobs_lifecycle_request';
    END IF;

    FOR v_command IN
        SELECT command.value
        FROM pg_catalog.jsonb_array_elements(p_change -> 'commands') AS command(value)
    LOOP
        IF NOT aap_jobs_v1._jobs_lifecycle_has_keys(
               v_command,
               ARRAY['operation', 'reads', 'values', 'written_fields', 'arguments']
           )
           OR pg_catalog.jsonb_typeof(v_command -> 'operation')
                IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_command -> 'reads') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(v_command -> 'values') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(v_command -> 'written_fields')
                IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(v_command -> 'arguments')
                IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING
                ERRCODE = '22023',
                MESSAGE = 'invalid_jobs_lifecycle_command';
        END IF;

        v_operation := v_command ->> 'operation';
        v_reads := v_command -> 'reads';
        v_values := v_command -> 'values';
        v_arguments := v_command -> 'arguments';

        IF EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_object_keys(v_reads) AS keys(key)
            WHERE keys.key NOT IN (
                'status', 'payload', 'celery_task_id', 'result', 'progress_pct',
                'error_message', 'started_at', 'completed_at', 'created_at',
                'updated_at', 'kind', 'id', 'project_id', 'retained_project_id',
                'user_id'
            )
        ) OR EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_object_keys(v_values) AS keys(key)
            WHERE keys.key NOT IN (
                'status', 'payload', 'celery_task_id', 'result', 'progress_pct',
                'error_message', 'started_at', 'completed_at', 'created_at',
                'updated_at', 'kind', 'id', 'project_id', 'retained_project_id',
                'user_id'
            )
        ) OR EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(v_command -> 'written_fields') AS fields(value)
            WHERE pg_catalog.jsonb_typeof(fields.value) IS DISTINCT FROM 'string'
               OR fields.value #>> '{}' NOT IN (
                   'id', 'kind', 'project_id', 'retained_project_id', 'user_id',
                   'status', 'progress_pct', 'payload', 'result', 'error_message',
                   'celery_task_id', 'started_at', 'completed_at', 'created_at', 'updated_at'
               )
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '22023',
                MESSAGE = 'invalid_jobs_lifecycle_command';
        END IF;

        SELECT COALESCE(
            pg_catalog.array_agg(fields.value #>> '{}' ORDER BY fields.ordinality),
            ARRAY[]::text[]
        )
        INTO v_written_fields
        FROM pg_catalog.jsonb_array_elements(v_command -> 'written_fields')
            WITH ORDINALITY AS fields(value, ordinality);

        IF pg_catalog.cardinality(v_written_fields) <> (
            SELECT pg_catalog.count(DISTINCT fields.value)
            FROM pg_catalog.unnest(v_written_fields) AS fields(value)
        ) OR (
            SELECT pg_catalog.count(*)
            FROM pg_catalog.jsonb_object_keys(v_values)
        ) <> pg_catalog.cardinality(v_written_fields)
           OR EXISTS (
               SELECT 1
               FROM pg_catalog.jsonb_object_keys(v_values) AS keys(key)
               WHERE NOT (keys.key = ANY(v_written_fields))
           )
           OR EXISTS (
               SELECT 1
               FROM pg_catalog.unnest(v_written_fields) AS fields(value)
               WHERE NOT (v_values ? fields.value)
           ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '22023',
                MESSAGE = 'invalid_jobs_lifecycle_command';
        END IF;

        v_expected_fields := ARRAY[]::text[];
        CASE v_operation
            WHEN 'mark_running' THEN
                IF NOT aap_jobs_v1._jobs_lifecycle_has_keys(
                       v_arguments, ARRAY['celery_task_id']
                   )
                   OR pg_catalog.jsonb_typeof(v_arguments -> 'celery_task_id')
                        NOT IN ('string', 'null')
                   OR NOT (v_reads ? 'status')
                   OR pg_catalog.jsonb_typeof(v_reads -> 'status')
                        IS DISTINCT FROM 'string' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                v_status := v_reads ->> 'status';
                IF v_status = 'pending' THEN
                    v_expected_fields := ARRAY['status', 'started_at'];
                END IF;
                IF aap_jobs_v1._jobs_lifecycle_is_truthy(
                       v_arguments -> 'celery_task_id'
                   ) THEN
                    IF NOT aap_jobs_v1._jobs_lifecycle_has_keys(
                           v_reads, ARRAY['status', 'celery_task_id']
                       )
                       OR pg_catalog.jsonb_typeof(v_reads -> 'celery_task_id')
                            NOT IN ('string', 'null') THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '22023',
                            MESSAGE = 'invalid_jobs_lifecycle_command';
                    END IF;
                    IF NOT aap_jobs_v1._jobs_lifecycle_is_truthy(
                           v_reads -> 'celery_task_id'
                       ) THEN
                        v_expected_fields := pg_catalog.array_append(
                            v_expected_fields, 'celery_task_id'
                        );
                    END IF;
                ELSIF NOT aap_jobs_v1._jobs_lifecycle_has_keys(
                    v_reads, ARRAY['status']
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                IF 'status' = ANY(v_written_fields)
                   AND v_values -> 'status' IS DISTINCT FROM '"running"'::pg_catalog.jsonb THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                IF 'started_at' = ANY(v_written_fields)
                   AND pg_catalog.jsonb_typeof(v_values -> 'started_at')
                        IS DISTINCT FROM 'string' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                IF 'celery_task_id' = ANY(v_written_fields)
                   AND v_values -> 'celery_task_id'
                        IS DISTINCT FROM v_arguments -> 'celery_task_id' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;

            WHEN 'bind_execution_id' THEN
                IF NOT aap_jobs_v1._jobs_lifecycle_has_keys(
                       v_arguments, ARRAY['execution_id']
                   )
                   OR pg_catalog.jsonb_typeof(v_arguments -> 'execution_id')
                        NOT IN ('string', 'null')
                   OR pg_catalog.jsonb_typeof(v_reads) IS DISTINCT FROM 'object' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                IF EXISTS (SELECT 1 FROM pg_catalog.jsonb_object_keys(v_reads)) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                v_expected_fields := ARRAY['celery_task_id'];
                IF 'celery_task_id' = ANY(v_written_fields)
                   AND v_values -> 'celery_task_id'
                        IS DISTINCT FROM v_arguments -> 'execution_id' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;

            WHEN 'update_payload' THEN
                IF NOT aap_jobs_v1._jobs_lifecycle_has_keys(
                       v_arguments, ARRAY['updates']
                   )
                   OR NOT aap_jobs_v1._jobs_lifecycle_has_keys(
                       v_reads, ARRAY['payload']
                   ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                v_expected_fields := ARRAY['payload'];
                IF 'payload' = ANY(v_written_fields)
                   AND pg_catalog.jsonb_typeof(v_values -> 'payload')
                        IS DISTINCT FROM 'object' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;

            WHEN 'update_progress' THEN
                IF NOT aap_jobs_v1._jobs_lifecycle_has_keys(
                       v_arguments, ARRAY['pct', 'extra_payload', 'result']
                   )
                   OR pg_catalog.jsonb_typeof(v_arguments -> 'extra_payload')
                        NOT IN ('object', 'array', 'string', 'number', 'boolean', 'null')
                   OR pg_catalog.jsonb_typeof(v_arguments -> 'result') IS NULL THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                IF v_arguments -> 'result' <> 'null'::pg_catalog.jsonb THEN
                    v_expected_fields := pg_catalog.array_append(
                        v_expected_fields, 'result'
                    );
                END IF;
                SELECT parsed.is_valid, parsed.integer_value
                INTO v_pct_valid, v_pct_numeric
                FROM aap_jobs_v1._jobs_lifecycle_integer(v_arguments -> 'pct') AS parsed;
                IF v_pct_valid THEN
                    v_expected_fields := pg_catalog.array_append(
                        v_expected_fields, 'progress_pct'
                    );
                    v_pct_integer := CASE
                        WHEN v_pct_numeric <= 0 THEN 0
                        WHEN v_pct_numeric >= 100 THEN 100
                        ELSE v_pct_numeric::integer
                    END;
                    IF 'progress_pct' = ANY(v_written_fields)
                       AND v_values -> 'progress_pct'
                            IS DISTINCT FROM pg_catalog.to_jsonb(v_pct_integer) THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '22023',
                            MESSAGE = 'invalid_jobs_lifecycle_command';
                    END IF;
                    v_extra_truthy := aap_jobs_v1._jobs_lifecycle_is_truthy(
                        v_arguments -> 'extra_payload'
                    );
                    IF v_extra_truthy THEN
                        IF NOT aap_jobs_v1._jobs_lifecycle_has_keys(
                               v_reads, ARRAY['payload']
                           ) THEN
                            RAISE EXCEPTION USING
                                ERRCODE = '22023',
                                MESSAGE = 'invalid_jobs_lifecycle_command';
                        END IF;
                        v_expected_fields := pg_catalog.array_append(
                            v_expected_fields, 'payload'
                        );
                    ELSIF EXISTS (
                        SELECT 1 FROM pg_catalog.jsonb_object_keys(v_reads)
                    ) THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '22023',
                            MESSAGE = 'invalid_jobs_lifecycle_command';
                    END IF;
                ELSIF EXISTS (
                    SELECT 1 FROM pg_catalog.jsonb_object_keys(v_reads)
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                IF 'result' = ANY(v_written_fields)
                   AND v_values -> 'result'
                        IS DISTINCT FROM v_arguments -> 'result' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                IF 'payload' = ANY(v_written_fields)
                   AND pg_catalog.jsonb_typeof(v_values -> 'payload')
                        IS DISTINCT FROM 'object' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;

            WHEN 'replace_result' THEN
                IF NOT aap_jobs_v1._jobs_lifecycle_has_keys(
                       v_arguments, ARRAY['result']
                   )
                   OR EXISTS (SELECT 1 FROM pg_catalog.jsonb_object_keys(v_reads)) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                v_expected_fields := ARRAY['result'];
                IF 'result' = ANY(v_written_fields)
                   AND v_values -> 'result'
                        IS DISTINCT FROM v_arguments -> 'result' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;

            WHEN 'mark_complete' THEN
                IF NOT aap_jobs_v1._jobs_lifecycle_has_keys(
                       v_arguments, ARRAY['result']
                   )
                   OR NOT aap_jobs_v1._jobs_lifecycle_has_keys(
                       v_reads, ARRAY['status']
                   )
                   OR pg_catalog.jsonb_typeof(v_reads -> 'status')
                        IS DISTINCT FROM 'string' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                v_status := v_reads ->> 'status';
                IF v_status NOT IN ('cancelled', 'failed') THEN
                    v_expected_fields := ARRAY['status', 'progress_pct', 'completed_at'];
                    IF v_arguments -> 'result' <> 'null'::pg_catalog.jsonb THEN
                        v_expected_fields := pg_catalog.array_append(
                            v_expected_fields, 'result'
                        );
                    END IF;
                END IF;
                IF 'status' = ANY(v_written_fields)
                   AND v_values -> 'status'
                        IS DISTINCT FROM '"completed"'::pg_catalog.jsonb THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                IF 'progress_pct' = ANY(v_written_fields)
                   AND v_values -> 'progress_pct'
                        IS DISTINCT FROM '100'::pg_catalog.jsonb THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                IF 'completed_at' = ANY(v_written_fields)
                   AND pg_catalog.jsonb_typeof(v_values -> 'completed_at')
                        IS DISTINCT FROM 'string' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                IF 'result' = ANY(v_written_fields)
                   AND v_values -> 'result'
                        IS DISTINCT FROM v_arguments -> 'result' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;

            WHEN 'mark_failed' THEN
                IF NOT aap_jobs_v1._jobs_lifecycle_has_keys(
                       v_arguments, ARRAY['error', 'result']
                   )
                   OR pg_catalog.jsonb_typeof(v_arguments -> 'error')
                        NOT IN ('string', 'null')
                   OR NOT aap_jobs_v1._jobs_lifecycle_has_keys(
                       v_reads, ARRAY['status']
                   )
                   OR pg_catalog.jsonb_typeof(v_reads -> 'status')
                        IS DISTINCT FROM 'string' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                v_status := v_reads ->> 'status';
                IF v_status NOT IN ('completed', 'cancelled') THEN
                    v_expected_fields := ARRAY['status', 'completed_at', 'error_message'];
                    IF v_arguments -> 'result' <> 'null'::pg_catalog.jsonb THEN
                        v_expected_fields := pg_catalog.array_append(
                            v_expected_fields, 'result'
                        );
                    END IF;
                END IF;
                IF 'status' = ANY(v_written_fields)
                   AND v_values -> 'status'
                        IS DISTINCT FROM '"failed"'::pg_catalog.jsonb THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                IF 'completed_at' = ANY(v_written_fields)
                   AND pg_catalog.jsonb_typeof(v_values -> 'completed_at')
                        IS DISTINCT FROM 'string' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                v_error := pg_catalog.left(COALESCE(v_arguments ->> 'error', ''), 4000);
                IF 'error_message' = ANY(v_written_fields)
                   AND v_values -> 'error_message'
                        IS DISTINCT FROM pg_catalog.to_jsonb(v_error) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                IF 'result' = ANY(v_written_fields)
                   AND v_values -> 'result'
                        IS DISTINCT FROM v_arguments -> 'result' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;

            WHEN 'mark_cancelled' THEN
                IF NOT aap_jobs_v1._jobs_lifecycle_has_keys(
                       v_arguments, ARRAY['result', 'allowed_statuses', 'progress_pct']
                   )
                   OR pg_catalog.jsonb_typeof(v_arguments -> 'allowed_statuses')
                        NOT IN ('array', 'null') THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                IF v_arguments -> 'allowed_statuses' = 'null'::pg_catalog.jsonb THEN
                    IF EXISTS (SELECT 1 FROM pg_catalog.jsonb_object_keys(v_reads)) THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '22023',
                            MESSAGE = 'invalid_jobs_lifecycle_command';
                    END IF;
                    v_status := NULL;
                ELSE
                    IF NOT aap_jobs_v1._jobs_lifecycle_has_keys(
                           v_reads, ARRAY['status']
                       )
                       OR pg_catalog.jsonb_typeof(v_reads -> 'status')
                            IS DISTINCT FROM 'string'
                       OR EXISTS (
                           SELECT 1
                           FROM pg_catalog.jsonb_array_elements(
                               v_arguments -> 'allowed_statuses'
                           ) AS allowed(value)
                           WHERE pg_catalog.jsonb_typeof(allowed.value)
                                IS DISTINCT FROM 'string'
                       ) THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '22023',
                            MESSAGE = 'invalid_jobs_lifecycle_command';
                    END IF;
                    v_status := v_reads ->> 'status';
                    SELECT COALESCE(
                        pg_catalog.array_agg(value #>> '{}'), ARRAY[]::text[]
                    )
                    INTO v_allowed_statuses
                    FROM pg_catalog.jsonb_array_elements(
                        v_arguments -> 'allowed_statuses'
                    ) AS allowed(value);
                END IF;
                IF v_arguments -> 'allowed_statuses' = 'null'::pg_catalog.jsonb
                   OR v_status = ANY(v_allowed_statuses) THEN
                    v_expected_fields := ARRAY['status', 'completed_at'];
                    IF v_arguments -> 'progress_pct' <> 'null'::pg_catalog.jsonb THEN
                        SELECT parsed.is_valid, parsed.integer_value
                        INTO v_pct_valid, v_pct_numeric
                        FROM aap_jobs_v1._jobs_lifecycle_integer(
                            v_arguments -> 'progress_pct'
                        ) AS parsed;
                        IF v_pct_valid THEN
                            v_expected_fields := pg_catalog.array_append(
                                v_expected_fields, 'progress_pct'
                            );
                            v_pct_integer := CASE
                                WHEN v_pct_numeric <= 0 THEN 0
                                WHEN v_pct_numeric >= 100 THEN 100
                                ELSE v_pct_numeric::integer
                            END;
                            IF 'progress_pct' = ANY(v_written_fields)
                               AND v_values -> 'progress_pct'
                                    IS DISTINCT FROM pg_catalog.to_jsonb(v_pct_integer) THEN
                                RAISE EXCEPTION USING
                                    ERRCODE = '22023',
                                    MESSAGE = 'invalid_jobs_lifecycle_command';
                            END IF;
                        END IF;
                    END IF;
                    IF v_arguments -> 'result' <> 'null'::pg_catalog.jsonb
                       AND (v_arguments -> 'progress_pct' = 'null'::pg_catalog.jsonb
                            OR v_pct_valid) THEN
                        v_expected_fields := pg_catalog.array_append(
                            v_expected_fields, 'result'
                        );
                    END IF;
                ELSIF v_arguments -> 'allowed_statuses' <> 'null'::pg_catalog.jsonb
                   AND v_status <> ALL(v_allowed_statuses) THEN
                    IF pg_catalog.cardinality(v_written_fields) <> 0 THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '22023',
                            MESSAGE = 'invalid_jobs_lifecycle_command';
                    END IF;
                    v_expected_fields := ARRAY[]::text[];
                END IF;
                IF 'status' = ANY(v_written_fields)
                   AND v_values -> 'status'
                        IS DISTINCT FROM '"cancelled"'::pg_catalog.jsonb THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                IF 'completed_at' = ANY(v_written_fields)
                   AND pg_catalog.jsonb_typeof(v_values -> 'completed_at')
                        IS DISTINCT FROM 'string' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                IF 'result' = ANY(v_written_fields)
                   AND v_values -> 'result'
                        IS DISTINCT FROM v_arguments -> 'result' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;

            WHEN 'request_cancel' THEN
                IF NOT aap_jobs_v1._jobs_lifecycle_has_keys(v_arguments, ARRAY[]::text[])
                   OR NOT (v_reads ? 'status')
                   OR pg_catalog.jsonb_typeof(v_reads -> 'status')
                        IS DISTINCT FROM 'string' THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                v_status := v_reads ->> 'status';
                IF v_status IN ('pending', 'running') THEN
                    IF NOT aap_jobs_v1._jobs_lifecycle_has_keys(
                           v_reads, ARRAY['status', 'payload']
                       ) THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '22023',
                            MESSAGE = 'invalid_jobs_lifecycle_command';
                    END IF;
                    v_expected_fields := ARRAY['payload'];
                ELSIF NOT aap_jobs_v1._jobs_lifecycle_has_keys(
                    v_reads, ARRAY['status']
                ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;
                IF 'payload' = ANY(v_written_fields)
                   AND (
                       pg_catalog.jsonb_typeof(v_values -> 'payload')
                            IS DISTINCT FROM 'object'
                       OR v_values -> 'payload' -> 'cancel_requested'
                            IS DISTINCT FROM 'true'::pg_catalog.jsonb
                       OR pg_catalog.jsonb_typeof(
                           v_values -> 'payload' -> 'cancel_requested_at'
                       ) IS DISTINCT FROM 'string'
                   ) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'invalid_jobs_lifecycle_command';
                END IF;

            ELSE
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'invalid_jobs_lifecycle_command';
        END CASE;

        IF pg_catalog.cardinality(v_written_fields) > 0
           AND v_written_fields IS DISTINCT FROM
               v_expected_fields[1:pg_catalog.cardinality(v_written_fields)] THEN
            RAISE EXCEPTION USING
                ERRCODE = '22023',
                MESSAGE = 'invalid_jobs_lifecycle_command';
        END IF;

        FOREACH v_field IN ARRAY v_written_fields LOOP
            IF v_field NOT IN ('payload', 'result') THEN
                v_lifecycle_values := v_lifecycle_values ||
                    pg_catalog.jsonb_build_object(v_field, v_values -> v_field);
            END IF;
        END LOOP;
    END LOOP;

    v_invoker := pg_catalog.current_setting('role', true);
    IF v_invoker IS NULL OR v_invoker = 'none' THEN
        v_invoker := SESSION_USER;
    END IF;
    IF v_updated_at_override_present
       AND (NOT pg_catalog.has_column_privilege(
                v_invoker, 'public.async_jobs', 'updated_at', 'UPDATE'
            ) OR NOT pg_catalog.has_column_privilege(
                v_invoker, 'public.async_jobs', 'id', 'SELECT'
            )) THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'jobs_lifecycle_legacy_write_not_allowed';
    END IF;
    IF p_change -> 'legacy_changes' <> '{}'::jsonb
       AND NOT pg_catalog.has_column_privilege(
           v_invoker, 'public.async_jobs', 'id', 'SELECT'
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'jobs_lifecycle_legacy_write_not_allowed';
    END IF;
    FOR v_field IN
        SELECT key FROM pg_catalog.jsonb_object_keys(p_change -> 'legacy_changes') AS keys(key)
    LOOP
        IF NOT (v_field = ANY(v_changed_fields))
           OR NOT pg_catalog.has_column_privilege(
               v_invoker,
               'public.async_jobs',
               v_field,
               'UPDATE'
           ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '42501',
                MESSAGE = 'jobs_lifecycle_legacy_write_not_allowed';
        END IF;
    END LOOP;

    v_patch :=
        v_lifecycle_values
        || (p_change -> 'legacy_changes')
        || (p_change -> 'projection_changes');

    FOREACH v_field IN ARRAY v_changed_fields LOOP
        IF v_field <> 'updated_at'
           AND NOT (v_patch ? v_field) THEN
            RAISE EXCEPTION USING
                ERRCODE = '22023',
                MESSAGE = 'missing_jobs_lifecycle_field_value';
        END IF;
    END LOOP;

    IF pg_catalog.cardinality(v_changed_fields) = 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'empty_jobs_lifecycle_change';
    END IF;

    FOREACH v_field IN ARRAY v_changed_fields LOOP
        IF v_field = 'updated_at' THEN
            v_expression := '$3';
        ELSIF v_field IN ('payload', 'result') THEN
            v_expression := '$2 -> ' || pg_catalog.quote_literal(v_field);
        ELSE
            v_expression := pg_catalog.format(
                '(pg_catalog.jsonb_populate_record(NULL::public.async_jobs, $2)).%I',
                v_field
            );
        END IF;
        IF v_set_clause <> '' THEN
            v_set_clause := v_set_clause || ', ';
        END IF;
        v_set_clause := v_set_clause || pg_catalog.format('%I = %s', v_field, v_expression);
    END LOOP;
    IF NOT v_updated_at_override_present THEN
        v_set_clause := v_set_clause || ', updated_at = pg_catalog.now()';
    END IF;

    EXECUTE pg_catalog.format(
        'UPDATE public.async_jobs AS job SET %s WHERE job.id = $1 RETURNING job.updated_at',
        v_set_clause
    )
    INTO v_row_updated_at
    USING p_job_id, v_patch, v_updated_at_override;
    GET DIAGNOSTICS v_row_count = ROW_COUNT;

    IF v_row_count = 0 THEN
        RETURN QUERY SELECT false, NULL::timestamp with time zone;
        RETURN;
    END IF;
    RETURN QUERY SELECT true, v_row_updated_at;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.apply_lifecycle(uuid, jsonb) FROM PUBLIC;
-- aap-statement-break
