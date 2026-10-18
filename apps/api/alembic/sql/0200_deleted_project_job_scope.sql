ALTER TABLE public.async_jobs
    ADD COLUMN retained_project_id uuid;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.retain_async_job_project_scope()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.retained_project_id IS NOT NULL THEN
            RAISE EXCEPTION 'retained async-job project scope is database-managed'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.retained_project_id IS NOT NULL
       AND NEW.retained_project_id IS DISTINCT FROM OLD.retained_project_id THEN
        RAISE EXCEPTION 'retained async-job project scope is immutable'
            USING ERRCODE = '23514';
    END IF;

    IF OLD.project_id IS NOT NULL AND NEW.project_id IS NULL THEN
        NEW.retained_project_id := OLD.project_id;
    ELSIF OLD.retained_project_id IS NULL
          AND NEW.retained_project_id IS NOT NULL THEN
        RAISE EXCEPTION 'retained async-job project scope is database-managed'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.project_id IS NOT NULL
       AND NEW.project_id IS DISTINCT FROM OLD.project_id THEN
        RAISE EXCEPTION 'async-job project scope cannot be rebound'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.retain_async_job_project_scope() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER async_jobs_retain_project_scope
BEFORE INSERT OR UPDATE OF project_id, retained_project_id ON public.async_jobs
FOR EACH ROW
EXECUTE FUNCTION aap_jobs_v1.retain_async_job_project_scope();

-- aap-statement-break
-- Detached terminal jobs keep the scope captured by the Jobs owner.
CREATE OR REPLACE FUNCTION aap_jobs_v1.claim_task_creation_job(
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
       OR (v_job.project_id IS NULL AND v_job.status NOT IN ('completed', 'failed', 'cancelled'))
       OR COALESCE(v_job.project_id, v_job.retained_project_id)::text IS DISTINCT FROM v_job.payload ->> 'project_id'
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
-- Detached terminal jobs keep the scope captured by the Jobs owner.
CREATE OR REPLACE FUNCTION aap_jobs_v1.finish_task_creation_job(
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
       OR (v_job.project_id IS NULL AND v_job.status NOT IN ('completed', 'failed', 'cancelled'))
       OR COALESCE(v_job.project_id, v_job.retained_project_id)::text IS DISTINCT FROM v_job.payload ->> 'project_id'
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
-- Detached terminal jobs keep the scope captured by the Jobs owner.
CREATE OR REPLACE FUNCTION aap_jobs_v1.fail_task_creation_job(
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
        -- Old SET NULL rows have no trustworthy project scope to adopt.
        IF NOT (
            (p_code = 'legacy_job_scope'
             AND NOT aap_jobs_v1._task_creation_job_payload_valid(v_job.id, v_job.payload))
            OR (p_code = 'job_scope_mismatch'
                AND v_job.project_id IS NULL
                AND v_job.retained_project_id IS NULL
                AND aap_jobs_v1._task_creation_job_payload_valid(v_job.id, v_job.payload))
        ) THEN
            RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'job_scope_mismatch');
        END IF;
    ELSE
        IF v_job.payload ->> 'schema' IS DISTINCT FROM 'aap-create-tasks/v1' THEN
            RETURN aap_jobs_v1._task_creation_job_failure(v_failure_schema, 'legacy_job_scope');
        END IF;
        IF NOT aap_jobs_v1._task_creation_job_payload_valid(v_job.id, v_job.payload)
           OR (v_job.project_id IS NULL AND v_job.status NOT IN ('completed', 'failed', 'cancelled'))
           OR COALESCE(v_job.project_id, v_job.retained_project_id)::text IS DISTINCT FROM v_job.payload ->> 'project_id'
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
