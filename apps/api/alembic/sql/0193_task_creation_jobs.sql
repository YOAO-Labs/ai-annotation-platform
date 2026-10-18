-- Jobs-owned durable accepted authority for initial dataset task creation.
CREATE SCHEMA aap_jobs_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_jobs_v1 FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1._task_creation_job_failure(
    p_schema text,
    p_code text
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', p_schema,
        'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    )
$function$;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1._task_creation_job_payload_valid(
    p_job_id uuid,
    p_payload jsonb
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT COALESCE(
        pg_catalog.jsonb_typeof(p_payload) = 'object'
        AND p_payload ->> 'schema' = 'aap-create-tasks/v1'
        AND p_payload ->> 'operation_id' = p_job_id::text
        AND p_payload ->> 'mode' = 'link'
        AND (p_payload ->> 'actor_id') ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
        AND (p_payload ->> 'dataset_id') ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
        AND (p_payload ->> 'project_id') ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
        AND (p_payload ->> 'association_id') ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
        AND (p_payload - ARRAY['cancel_requested', 'cancel_requested_at']) =
            pg_catalog.jsonb_build_object(
                'schema', 'aap-create-tasks/v1',
                'operation_id', p_job_id::text,
                'actor_id', p_payload ->> 'actor_id',
                'dataset_id', p_payload ->> 'dataset_id',
                'project_id', p_payload ->> 'project_id',
                'association_id', p_payload ->> 'association_id',
                'mode', 'link'
            )
        AND (
            NOT (p_payload ? 'cancel_requested')
            OR pg_catalog.jsonb_typeof(p_payload -> 'cancel_requested') = 'boolean'
        )
        AND (
            NOT (p_payload ? 'cancel_requested_at')
            OR (
                pg_catalog.jsonb_typeof(p_payload -> 'cancel_requested_at') = 'string'
                AND p_payload -> 'cancel_requested' = 'true'::jsonb
            )
        ),
        false
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._task_creation_job_failure(text, text) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._task_creation_job_payload_valid(uuid, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.create_task_creation_job(
    p_operation_id uuid,
    p_actor_id uuid,
    p_dataset_id uuid,
    p_project_id uuid,
    p_association_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_manager jsonb;
    v_association_id uuid;
    v_payload jsonb;
    v_job public.async_jobs%ROWTYPE;
    v_inserted integer;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_dataset_id IS NULL
       OR p_project_id IS NULL OR p_association_id IS NULL THEN
        RETURN aap_jobs_v1._task_creation_job_failure(
            'aap-task-creation-job-command/v1', 'invalid_request'
        );
    END IF;

    v_manager := aap_workspace_v1.lock_project_manager(p_actor_id, p_project_id);
    IF v_manager ->> 'schema' IS DISTINCT FROM 'aap-project-manager/v1' THEN
        RETURN aap_jobs_v1._task_creation_job_failure(
            'aap-task-creation-job-command/v1', 'authority_contract_mismatch'
        );
    END IF;
    IF v_manager ->> 'ok' IS DISTINCT FROM 'true' THEN
        IF pg_catalog.jsonb_typeof(v_manager -> 'error') <> 'object'
           OR v_manager #>> '{error,code}' IS NULL THEN
            RETURN aap_jobs_v1._task_creation_job_failure(
                'aap-task-creation-job-command/v1', 'authority_contract_mismatch'
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-job-command/v1',
            'ok', false,
            'error', v_manager -> 'error'
        );
    END IF;

    v_association_id := aap_assets_v1.lock_current_association(
        p_dataset_id, p_project_id
    );
    IF v_association_id IS NULL OR v_association_id <> p_association_id THEN
        RETURN aap_jobs_v1._task_creation_job_failure(
            'aap-task-creation-job-command/v1', 'association_stale'
        );
    END IF;

    v_payload := pg_catalog.jsonb_build_object(
        'schema', 'aap-create-tasks/v1',
        'operation_id', p_operation_id,
        'actor_id', p_actor_id,
        'dataset_id', p_dataset_id,
        'project_id', p_project_id,
        'association_id', p_association_id,
        'mode', 'link'
    );
    INSERT INTO public.async_jobs (
        id, kind, project_id, user_id, status, progress_pct, payload, result
    )
    VALUES (
        p_operation_id, 'create_tasks', p_project_id, p_actor_id,
        'pending', 0, v_payload, '{}'::jsonb
    )
    ON CONFLICT (id) DO NOTHING;
    GET DIAGNOSTICS v_inserted = ROW_COUNT;

    IF v_inserted = 1 THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-job-command/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object('job_id', p_operation_id)
        );
    END IF;

    SELECT job.*
    INTO v_job
    FROM public.async_jobs AS job
    WHERE job.id = p_operation_id
    FOR UPDATE;

    IF NOT FOUND
       OR v_job.kind <> 'create_tasks'
       OR NOT aap_jobs_v1._task_creation_job_payload_valid(v_job.id, v_job.payload)
       OR v_job.project_id IS DISTINCT FROM p_project_id
       OR (v_job.user_id IS NOT NULL AND v_job.user_id <> p_actor_id)
       OR (v_job.payload - ARRAY['cancel_requested', 'cancel_requested_at'])
            IS DISTINCT FROM v_payload THEN
        RETURN aap_jobs_v1._task_creation_job_failure(
            'aap-task-creation-job-command/v1', 'operation_conflict'
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-task-creation-job-command/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object('job_id', v_job.id)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.create_task_creation_job(uuid, uuid, uuid, uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.find_task_creation_job(
    p_actor_id uuid,
    p_dataset_id uuid,
    p_project_id uuid,
    p_association_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_manager jsonb;
    v_association_id uuid;
    v_job public.async_jobs%ROWTYPE;
    v_legacy_seen boolean := false;
BEGIN
    IF p_actor_id IS NULL OR p_dataset_id IS NULL OR p_project_id IS NULL
       OR p_association_id IS NULL THEN
        RETURN aap_jobs_v1._task_creation_job_failure(
            'aap-task-creation-job-discovery/v1', 'invalid_request'
        );
    END IF;

    v_manager := aap_workspace_v1.lock_project_manager(p_actor_id, p_project_id);
    IF v_manager ->> 'schema' IS DISTINCT FROM 'aap-project-manager/v1' THEN
        RETURN aap_jobs_v1._task_creation_job_failure(
            'aap-task-creation-job-discovery/v1', 'authority_contract_mismatch'
        );
    END IF;
    IF v_manager ->> 'ok' IS DISTINCT FROM 'true' THEN
        IF pg_catalog.jsonb_typeof(v_manager -> 'error') <> 'object'
           OR v_manager #>> '{error,code}' IS NULL THEN
            RETURN aap_jobs_v1._task_creation_job_failure(
                'aap-task-creation-job-discovery/v1', 'authority_contract_mismatch'
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-job-discovery/v1',
            'ok', false,
            'error', v_manager -> 'error'
        );
    END IF;

    v_association_id := aap_assets_v1.lock_current_association(
        p_dataset_id, p_project_id
    );
    IF v_association_id IS NULL OR v_association_id <> p_association_id THEN
        RETURN aap_jobs_v1._task_creation_job_failure(
            'aap-task-creation-job-discovery/v1', 'association_stale'
        );
    END IF;

    FOR v_job IN
        SELECT job.*
        FROM public.async_jobs AS job
        WHERE job.kind = 'create_tasks'
          AND job.project_id = p_project_id
          AND job.status IN ('pending', 'running')
          AND pg_catalog.jsonb_typeof(job.payload) = 'object'
          AND job.payload ->> 'dataset_id' = p_dataset_id::text
          AND job.payload ->> 'project_id' = p_project_id::text
        ORDER BY job.created_at DESC, job.id DESC
    LOOP
        IF v_job.payload ->> 'schema' = 'aap-create-tasks/v1' THEN
            IF aap_jobs_v1._task_creation_job_payload_valid(v_job.id, v_job.payload)
               AND v_job.project_id IS NOT DISTINCT FROM p_project_id
               AND (v_job.user_id IS NULL OR v_job.user_id::text = v_job.payload ->> 'actor_id') THEN
                IF v_job.payload ->> 'association_id' = p_association_id::text THEN
                    RETURN pg_catalog.jsonb_build_object(
                        'schema', 'aap-task-creation-job-discovery/v1',
                        'ok', true,
                        'data', pg_catalog.jsonb_build_object(
                            'job_id', v_job.id,
                            'operation_id', v_job.id,
                            'status', v_job.status
                        )
                    );
                END IF;
            ELSE
                v_legacy_seen := true;
            END IF;
        ELSE
            v_legacy_seen := true;
        END IF;
    END LOOP;

    IF v_legacy_seen THEN
        RETURN aap_jobs_v1._task_creation_job_failure(
            'aap-task-creation-job-discovery/v1', 'legacy_job_scope'
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-task-creation-job-discovery/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'job_id', NULL,
            'operation_id', NULL,
            'status', NULL
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.find_task_creation_job(uuid, uuid, uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.claim_task_creation_job(
    p_job_id uuid,
    p_execution_id text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_failure_schema constant text := 'aap-task-creation-job-claim/v1';
BEGIN
    IF p_job_id IS NULL OR p_execution_id IS NULL OR p_execution_id = ''
       OR pg_catalog.char_length(p_execution_id) > 64 THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'invalid_request');
    END IF;

    SELECT job.*
    INTO v_job
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'job_missing');
    END IF;
    IF v_job.kind <> 'create_tasks'
       OR v_job.payload ->> 'schema' IS DISTINCT FROM 'aap-create-tasks/v1' THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'legacy_job_scope');
    END IF;
    IF NOT aap_jobs_v1._task_creation_job_payload_valid(v_job.id, v_job.payload)
       OR v_job.project_id IS NULL
       OR v_job.project_id::text IS DISTINCT FROM v_job.payload ->> 'project_id'
       OR (v_job.user_id IS NOT NULL AND v_job.user_id::text IS DISTINCT FROM v_job.payload ->> 'actor_id') THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'job_scope_mismatch');
    END IF;

    IF v_job.status IN ('pending', 'running')
       AND COALESCE(v_job.payload -> 'cancel_requested' = 'true'::jsonb, false) THEN
        UPDATE public.async_jobs AS job
        SET status = 'cancelled',
            completed_at = pg_catalog.now(),
            updated_at = pg_catalog.now()
        WHERE job.id = p_job_id
        RETURNING job.* INTO v_job;
    END IF;

    IF v_job.status IN ('pending', 'running')
       AND (v_job.status = 'pending' OR v_job.celery_task_id IS NULL) THEN
        UPDATE public.async_jobs AS job
        SET status = CASE
                WHEN job.status = 'pending' THEN 'running'
                ELSE job.status
            END,
            started_at = CASE
                WHEN job.status = 'pending' THEN COALESCE(job.started_at, pg_catalog.now())
                ELSE job.started_at
            END,
            celery_task_id = COALESCE(job.celery_task_id, p_execution_id),
            updated_at = pg_catalog.now()
        WHERE job.id = p_job_id
        RETURNING job.* INTO v_job;
    END IF;

    IF v_job.status NOT IN ('pending', 'running', 'completed', 'failed', 'cancelled') THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'job_state_invalid');
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', v_failure_schema,
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'job_id', v_job.id,
            'operation_id', v_job.id,
            'actor_id', v_job.payload ->> 'actor_id',
            'dataset_id', v_job.payload ->> 'dataset_id',
            'project_id', v_job.payload ->> 'project_id',
            'association_id', v_job.payload ->> 'association_id',
            'status', v_job.status,
            'cancel_requested', COALESCE(
                v_job.payload -> 'cancel_requested' = 'true'::jsonb,
                false
            )
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.claim_task_creation_job(uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.lock_task_creation_job(
    p_job_id uuid,
    p_operation_id uuid,
    p_actor_id uuid,
    p_dataset_id uuid,
    p_project_id uuid,
    p_association_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_failure_schema constant text := 'aap-task-creation-job/v1';
BEGIN
    IF p_job_id IS NULL OR p_operation_id IS NULL OR p_actor_id IS NULL
       OR p_dataset_id IS NULL OR p_project_id IS NULL OR p_association_id IS NULL THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'invalid_request');
    END IF;

    SELECT job.*
    INTO v_job
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'job_missing');
    END IF;
    IF v_job.kind <> 'create_tasks'
       OR v_job.payload ->> 'schema' IS DISTINCT FROM 'aap-create-tasks/v1' THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'legacy_job_scope');
    END IF;
    IF NOT aap_jobs_v1._task_creation_job_payload_valid(v_job.id, v_job.payload)
       OR v_job.project_id IS NULL
       OR v_job.project_id::text IS DISTINCT FROM v_job.payload ->> 'project_id'
       OR (v_job.user_id IS NOT NULL AND v_job.user_id::text IS DISTINCT FROM v_job.payload ->> 'actor_id')
       OR p_job_id <> p_operation_id
       OR v_job.payload ->> 'actor_id' <> p_actor_id::text
       OR v_job.payload ->> 'dataset_id' <> p_dataset_id::text
       OR v_job.payload ->> 'project_id' <> p_project_id::text
       OR v_job.payload ->> 'association_id' <> p_association_id::text THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'job_scope_mismatch');
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', v_failure_schema,
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'job_id', v_job.id,
            'status', v_job.status,
            'cancel_requested', COALESCE(
                v_job.payload -> 'cancel_requested' = 'true'::jsonb,
                false
            )
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.lock_task_creation_job(uuid, uuid, uuid, uuid, uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.update_task_creation_progress(
    p_job_id uuid,
    p_operation_id uuid,
    p_processed bigint,
    p_total bigint,
    p_created bigint
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_progress integer;
BEGIN
    IF p_job_id IS NULL OR p_operation_id IS NULL OR p_processed IS NULL
       OR p_total IS NULL OR p_created IS NULL OR p_processed < 0 OR p_total < 0
       OR p_created < 0 OR p_processed > p_total OR p_created > p_processed THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid task creation progress';
    END IF;

    SELECT job.*
    INTO v_job
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id
    FOR UPDATE;

    IF NOT FOUND OR v_job.kind <> 'create_tasks'
       OR v_job.payload ->> 'schema' IS DISTINCT FROM 'aap-create-tasks/v1'
       OR NOT aap_jobs_v1._task_creation_job_payload_valid(v_job.id, v_job.payload)
       OR v_job.project_id IS NULL
       OR v_job.project_id::text IS DISTINCT FROM v_job.payload ->> 'project_id'
       OR (v_job.user_id IS NOT NULL AND v_job.user_id::text IS DISTINCT FROM v_job.payload ->> 'actor_id')
       OR v_job.id <> p_operation_id
       OR v_job.status NOT IN ('pending', 'running')
       OR COALESCE(v_job.payload -> 'cancel_requested' = 'true'::jsonb, false) THEN
        RETURN false;
    END IF;

    IF p_processed = p_total THEN
        v_progress := 100;
    ELSIF p_total = 0 THEN
        v_progress := 0;
    ELSE
        v_progress := pg_catalog.floor(
            p_processed::numeric * 100 / p_total
        )::integer;
    END IF;

    IF v_progress >= v_job.progress_pct + 5 OR p_processed = p_total THEN
        UPDATE public.async_jobs AS job
        SET progress_pct = GREATEST(job.progress_pct, v_progress),
            result = pg_catalog.jsonb_build_object(
                'created_tasks', p_created,
                'total', p_total
            ),
            updated_at = pg_catalog.now()
        WHERE job.id = p_job_id;
    ELSE
        UPDATE public.async_jobs AS job
        SET result = pg_catalog.jsonb_build_object(
                'created_tasks', p_created,
                'total', p_total
            ),
            updated_at = pg_catalog.now()
        WHERE job.id = p_job_id;
    END IF;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.update_task_creation_progress(uuid, uuid, bigint, bigint, bigint) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.finish_task_creation_job(
    p_job_id uuid,
    p_operation_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_operation jsonb;
    v_summary jsonb;
    v_result jsonb;
    v_status text;
    v_failure_schema constant text := 'aap-task-creation-job-finish/v1';
BEGIN
    IF p_job_id IS NULL OR p_operation_id IS NULL THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'invalid_request');
    END IF;

    -- Do not lock Jobs before Labeling reads the operation: its order is
    -- dataset -> association -> Jobs -> operation.
    SELECT job.*
    INTO v_job
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id;

    IF NOT FOUND THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'job_missing');
    END IF;
    IF v_job.kind <> 'create_tasks'
       OR v_job.payload ->> 'schema' IS DISTINCT FROM 'aap-create-tasks/v1' THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'legacy_job_scope');
    END IF;
    IF NOT aap_jobs_v1._task_creation_job_payload_valid(v_job.id, v_job.payload)
       OR v_job.project_id IS NULL
       OR v_job.project_id::text IS DISTINCT FROM v_job.payload ->> 'project_id'
       OR (v_job.user_id IS NOT NULL AND v_job.user_id::text IS DISTINCT FROM v_job.payload ->> 'actor_id')
       OR p_job_id <> p_operation_id THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'job_scope_mismatch');
    END IF;
    IF v_job.status IN ('completed', 'failed', 'cancelled') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', v_failure_schema,
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'status', v_job.status,
                'result', v_job.result
            )
        );
    END IF;
    IF v_job.status NOT IN ('pending', 'running') THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'job_state_invalid');
    END IF;

    v_operation := aap_labeling_v1.read_dataset_task_creation(p_operation_id);
    IF v_operation ->> 'schema' IS DISTINCT FROM 'aap-task-creation/v1' THEN
        RETURN aap_jobs_v1._task_creation_job_failure(
            v_failure_schema, 'operation_contract_mismatch'
        );
    END IF;
    IF v_operation ->> 'ok' IS DISTINCT FROM 'true' THEN
        IF pg_catalog.jsonb_typeof(v_operation -> 'error') <> 'object'
           OR v_operation #>> '{error,code}' IS NULL THEN
            RETURN aap_jobs_v1._task_creation_job_failure(
                v_failure_schema, 'operation_contract_mismatch'
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', v_failure_schema,
            'ok', false,
            'error', v_operation -> 'error'
        );
    END IF;

    v_summary := v_operation -> 'data';
    IF v_summary ->> 'job_id' IS DISTINCT FROM p_job_id::text
       OR v_summary ->> 'operation_id' IS DISTINCT FROM p_operation_id::text THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'job_scope_mismatch');
    END IF;
    IF v_summary ->> 'state' IS DISTINCT FROM 'completed'
       OR v_summary -> 'total' IS NULL
       OR v_summary -> 'total' = 'null'::jsonb THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'operation_incomplete');
    END IF;

    v_result := pg_catalog.jsonb_build_object(
        'created_tasks', v_summary -> 'created',
        'total', v_summary -> 'total'
    );
    UPDATE public.async_jobs AS job
    SET status = 'completed',
        progress_pct = 100,
        completed_at = pg_catalog.now(),
        result = v_result,
        updated_at = pg_catalog.now()
    WHERE job.id = p_job_id
      AND job.status IN ('pending', 'running')
    RETURNING job.status, job.result INTO v_status, v_result;

    IF NOT FOUND THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'job_not_active');
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', v_failure_schema,
        'ok', true,
        'data', pg_catalog.jsonb_build_object('status', v_status, 'result', v_result)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.finish_task_creation_job(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.fail_task_creation_job(
    p_job_id uuid,
    p_operation_id uuid,
    p_code text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_status text;
    v_result jsonb;
    v_failure_schema constant text := 'aap-task-creation-job-finish/v1';
BEGIN
    IF p_job_id IS NULL OR p_code IS NULL OR p_code !~ '^[a-z_]{1,64}$' THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'invalid_request');
    END IF;

    SELECT job.*
    INTO v_job
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'job_missing');
    END IF;
    IF v_job.kind <> 'create_tasks' THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'job_scope_mismatch');
    END IF;
    IF p_operation_id IS NULL THEN
        IF p_code <> 'legacy_job_scope'
           OR aap_jobs_v1._task_creation_job_payload_valid(v_job.id, v_job.payload) THEN
            RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'job_scope_mismatch');
        END IF;
    ELSE
        IF v_job.payload ->> 'schema' IS DISTINCT FROM 'aap-create-tasks/v1' THEN
            RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'legacy_job_scope');
        END IF;
        IF NOT aap_jobs_v1._task_creation_job_payload_valid(v_job.id, v_job.payload)
           OR v_job.project_id IS NULL
           OR v_job.project_id::text IS DISTINCT FROM v_job.payload ->> 'project_id'
           OR (v_job.user_id IS NOT NULL AND v_job.user_id::text IS DISTINCT FROM v_job.payload ->> 'actor_id')
           OR p_operation_id <> v_job.id THEN
            RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'job_scope_mismatch');
        END IF;
    END IF;

    IF v_job.status IN ('completed', 'failed', 'cancelled') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', v_failure_schema,
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'status', v_job.status,
                'result', v_job.result
            )
        );
    END IF;

    IF v_job.status IN ('pending', 'running')
       AND COALESCE(v_job.payload -> 'cancel_requested' = 'true'::jsonb, false)
       AND p_code IN ('job_not_active', 'job_cancelled', 'creation_cancelled') THEN
        UPDATE public.async_jobs AS job
        SET status = 'cancelled',
            completed_at = pg_catalog.now(),
            updated_at = pg_catalog.now()
        WHERE job.id = p_job_id
        RETURNING job.status, job.result INTO v_status, v_result;
    ELSE
        UPDATE public.async_jobs AS job
        SET status = 'failed',
            completed_at = pg_catalog.now(),
            error_message = 'task_creation:' || p_code,
            updated_at = pg_catalog.now()
        WHERE job.id = p_job_id
        RETURNING job.status, job.result INTO v_status, v_result;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', v_failure_schema,
        'ok', true,
        'data', pg_catalog.jsonb_build_object('status', v_status, 'result', v_result)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fail_task_creation_job(uuid, uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.cancel_task_creation_job(
    p_actor_id uuid,
    p_job_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_manager jsonb;
    v_failure_schema constant text := 'aap-task-creation-job-finish/v1';
BEGIN
    IF p_actor_id IS NULL OR p_job_id IS NULL THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'invalid_request');
    END IF;

    -- Resolve the frozen project without locking Jobs first. Workspace locks
    -- account -> project -> membership; only then take the Jobs row lock.
    SELECT job.*
    INTO v_job
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id;

    IF NOT FOUND THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'job_missing');
    END IF;
    IF v_job.kind <> 'create_tasks'
       OR v_job.payload ->> 'schema' IS DISTINCT FROM 'aap-create-tasks/v1' THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'legacy_job_scope');
    END IF;
    IF NOT aap_jobs_v1._task_creation_job_payload_valid(v_job.id, v_job.payload)
       OR v_job.project_id IS NULL
       OR v_job.project_id::text IS DISTINCT FROM v_job.payload ->> 'project_id'
       OR (v_job.user_id IS NOT NULL AND v_job.user_id::text IS DISTINCT FROM v_job.payload ->> 'actor_id') THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'job_scope_mismatch');
    END IF;

    v_manager := aap_workspace_v1.lock_project_manager(p_actor_id, v_job.project_id);
    IF v_manager ->> 'schema' IS DISTINCT FROM 'aap-project-manager/v1' THEN
        RETURN aap_jobs_v1._task_creation_job_failure(
            v_failure_schema, 'authority_contract_mismatch'
        );
    END IF;
    IF v_manager ->> 'ok' IS DISTINCT FROM 'true' THEN
        IF pg_catalog.jsonb_typeof(v_manager -> 'error') <> 'object'
           OR v_manager #>> '{error,code}' IS NULL THEN
            RETURN aap_jobs_v1._task_creation_job_failure(
                v_failure_schema, 'authority_contract_mismatch'
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', v_failure_schema,
            'ok', false,
            'error', v_manager -> 'error'
        );
    END IF;

    SELECT job.*
    INTO v_job
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'job_missing');
    END IF;
    IF v_job.kind <> 'create_tasks'
       OR NOT aap_jobs_v1._task_creation_job_payload_valid(v_job.id, v_job.payload)
       OR v_job.project_id IS NULL
       OR v_job.project_id::text IS DISTINCT FROM v_job.payload ->> 'project_id'
       OR (v_job.user_id IS NOT NULL AND v_job.user_id::text IS DISTINCT FROM v_job.payload ->> 'actor_id') THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'job_scope_mismatch');
    END IF;

    IF v_job.status IN ('pending', 'running') THEN
        UPDATE public.async_jobs AS job
        SET status = 'cancelled',
            completed_at = pg_catalog.now(),
            updated_at = pg_catalog.now()
        WHERE job.id = p_job_id
        RETURNING job.* INTO v_job;
    ELSIF v_job.status NOT IN ('completed', 'failed', 'cancelled') THEN
        RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'job_state_invalid');
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', v_failure_schema,
        'ok', true,
        'data', pg_catalog.jsonb_build_object('status', v_job.status, 'result', v_job.result)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.cancel_task_creation_job(uuid, uuid) FROM PUBLIC;
