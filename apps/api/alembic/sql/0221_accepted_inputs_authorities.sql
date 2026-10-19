-- Jobs grants an exact, durable single-task Workbench preannotation scope.
CREATE FUNCTION aap_jobs_v1._accepted_image_input_job_reply(p_data jsonb, p_code text)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_code IS NULL THEN pg_catalog.jsonb_build_object(
        'schema', 'aap-jobs-accepted-image-input/v1', 'ok', true, 'data', p_data
    ) ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-jobs-accepted-image-input/v1', 'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._accepted_image_input_job_reply(jsonb, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.project_accepted_image_input_job(
    p_job_id uuid, p_task_id uuid, p_actor_id uuid, p_require_active boolean
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_tasks jsonb;
    v_scope jsonb;
    v_scope_tasks jsonb;
    v_cancel jsonb;
    v_cancel_requested boolean;
    v_active boolean;
BEGIN
    IF p_job_id IS NULL OR p_task_id IS NULL OR p_actor_id IS NULL
       OR p_require_active IS NULL THEN
        RETURN aap_jobs_v1._accepted_image_input_job_reply(NULL, 'invalid_request');
    END IF;
    SELECT job.* INTO v_job
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id;
    IF NOT FOUND THEN
        RETURN aap_jobs_v1._accepted_image_input_job_reply(NULL, 'job_not_active');
    END IF;
    IF v_job.kind IS DISTINCT FROM 'batch_predict'
       OR v_job.user_id IS DISTINCT FROM p_actor_id
       OR v_job.project_id IS NULL
       OR v_job.celery_task_id IS DISTINCT FROM v_job.id::text
       OR pg_catalog.jsonb_typeof(v_job.payload) IS DISTINCT FROM 'object'
       OR v_job.payload ->> 'execution_scope' IS DISTINCT FROM 'workbench' THEN
        RETURN aap_jobs_v1._accepted_image_input_job_reply(NULL, 'input_scope_mismatch');
    END IF;
    v_tasks := v_job.payload -> 'task_ids';
    v_scope := v_job.payload -> 'scope';
    IF pg_catalog.jsonb_typeof(v_tasks) IS DISTINCT FROM 'array' THEN
        RETURN aap_jobs_v1._accepted_image_input_job_reply(NULL, 'input_scope_mismatch');
    END IF;
    IF pg_catalog.jsonb_array_length(v_tasks) <> 1
       OR pg_catalog.jsonb_typeof(v_tasks -> 0) IS DISTINCT FROM 'string'
       OR v_tasks ->> 0 IS DISTINCT FROM p_task_id::text THEN
        RETURN aap_jobs_v1._accepted_image_input_job_reply(NULL, 'input_scope_mismatch');
    END IF;
    IF pg_catalog.jsonb_typeof(v_scope) IS DISTINCT FROM 'object'
       OR (v_scope - 'task_ids') IS DISTINCT FROM '{}'::jsonb THEN
        RETURN aap_jobs_v1._accepted_image_input_job_reply(NULL, 'input_scope_mismatch');
    END IF;
    v_scope_tasks := v_scope -> 'task_ids';
    IF pg_catalog.jsonb_typeof(v_scope_tasks) IS DISTINCT FROM 'array'
       OR v_scope_tasks IS DISTINCT FROM v_tasks THEN
        RETURN aap_jobs_v1._accepted_image_input_job_reply(NULL, 'input_scope_mismatch');
    END IF;
    IF pg_catalog.jsonb_array_length(v_scope_tasks) <> 1
       OR pg_catalog.jsonb_typeof(v_scope_tasks -> 0) IS DISTINCT FROM 'string'
       OR v_scope_tasks ->> 0 IS DISTINCT FROM p_task_id::text THEN
        RETURN aap_jobs_v1._accepted_image_input_job_reply(NULL, 'input_scope_mismatch');
    END IF;
    v_cancel := v_job.payload -> 'cancel_requested';
    IF v_cancel IS NULL THEN
        v_cancel_requested := false;
    ELSIF pg_catalog.jsonb_typeof(v_cancel) = 'boolean' THEN
        v_cancel_requested := (v_cancel = 'true'::jsonb);
    ELSE
        RETURN aap_jobs_v1._accepted_image_input_job_reply(NULL, 'invalid_response');
    END IF;
    v_active := v_job.status IN ('pending', 'running') AND NOT v_cancel_requested;
    IF p_require_active AND NOT v_active THEN
        RETURN aap_jobs_v1._accepted_image_input_job_reply(NULL, 'job_not_active');
    END IF;
    IF v_job.status NOT IN ('pending', 'running', 'completed', 'failed', 'cancelled') THEN
        RETURN aap_jobs_v1._accepted_image_input_job_reply(NULL, 'invalid_response');
    END IF;
    RETURN aap_jobs_v1._accepted_image_input_job_reply(
        pg_catalog.jsonb_build_object(
            'job_id', v_job.id,
            'task_id', p_task_id,
            'actor_id', v_job.user_id,
            'project_id', v_job.project_id,
            'status', v_job.status,
            'active', v_active,
            'cancel_requested', v_cancel_requested
        ), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.project_accepted_image_input_job(uuid, uuid, uuid, boolean)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.accepted_image_input_job_consistency(
    p_job_id uuid, p_task_id uuid, p_actor_id uuid, p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_scope jsonb;
    v_scope_tasks jsonb;
    v_tasks jsonb;
    v_cancel jsonb;
    v_cancel_requested boolean := false;
    v_exists boolean := false;
    v_consistent boolean := true;
    v_active boolean := false;
    v_full_scope boolean := false;
BEGIN
    IF p_job_id IS NULL OR p_task_id IS NULL
       OR p_actor_id IS NULL OR p_project_id IS NULL THEN
        RETURN aap_jobs_v1._accepted_image_input_job_reply(NULL, 'invalid_request');
    END IF;
    SELECT job.* INTO v_job
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id;
    v_exists := FOUND;
    IF v_exists THEN
        IF v_job.kind IS DISTINCT FROM 'batch_predict'
           OR (v_job.user_id IS NOT NULL AND v_job.user_id IS DISTINCT FROM p_actor_id)
           OR (v_job.project_id IS NOT NULL AND v_job.project_id IS DISTINCT FROM p_project_id)
           OR (v_job.retained_project_id IS NOT NULL
               AND v_job.retained_project_id IS DISTINCT FROM p_project_id)
           OR pg_catalog.jsonb_typeof(v_job.payload) IS DISTINCT FROM 'object' THEN
            v_consistent := false;
        ELSE
            IF v_job.payload ? 'execution_scope'
               AND v_job.payload ->> 'execution_scope' IS DISTINCT FROM 'workbench' THEN
                v_consistent := false;
            END IF;
            v_tasks := v_job.payload -> 'task_ids';
            IF v_job.payload ? 'task_ids' THEN
                IF pg_catalog.jsonb_typeof(v_tasks) IS DISTINCT FROM 'array' THEN
                    v_consistent := false;
                ELSIF pg_catalog.jsonb_array_length(v_tasks) <> 1
                   OR pg_catalog.jsonb_typeof(v_tasks -> 0) IS DISTINCT FROM 'string'
                   OR v_tasks ->> 0 IS DISTINCT FROM p_task_id::text THEN
                    v_consistent := false;
                END IF;
            END IF;
            v_scope := v_job.payload -> 'scope';
            IF v_job.payload ? 'scope' THEN
                IF pg_catalog.jsonb_typeof(v_scope) IS DISTINCT FROM 'object'
                   OR (v_scope - 'task_ids') IS DISTINCT FROM '{}'::jsonb THEN
                    v_consistent := false;
                ELSE
                    v_scope_tasks := v_scope -> 'task_ids';
                    IF pg_catalog.jsonb_typeof(v_scope_tasks) IS DISTINCT FROM 'array' THEN
                        v_consistent := false;
                    ELSIF pg_catalog.jsonb_array_length(v_scope_tasks) <> 1
                       OR pg_catalog.jsonb_typeof(v_scope_tasks -> 0) IS DISTINCT FROM 'string'
                       OR v_scope_tasks ->> 0 IS DISTINCT FROM p_task_id::text
                       OR (v_tasks IS NOT NULL AND v_scope_tasks IS DISTINCT FROM v_tasks) THEN
                        v_consistent := false;
                    END IF;
                END IF;
            END IF;
            v_cancel := v_job.payload -> 'cancel_requested';
            IF v_cancel IS NULL THEN
                v_cancel_requested := false;
            ELSIF pg_catalog.jsonb_typeof(v_cancel) = 'boolean' THEN
                v_cancel_requested := (v_cancel = 'true'::jsonb);
            ELSE
                v_consistent := false;
            END IF;
            IF v_job.user_id = p_actor_id
               AND v_job.project_id = p_project_id
               AND v_job.payload ->> 'execution_scope' = 'workbench'
               AND pg_catalog.jsonb_typeof(v_tasks) = 'array'
               AND pg_catalog.jsonb_typeof(v_scope_tasks) = 'array' THEN
                IF pg_catalog.jsonb_array_length(v_tasks) = 1
                   AND v_tasks ->> 0 = p_task_id::text
                   AND pg_catalog.jsonb_array_length(v_scope_tasks) = 1
                   AND v_scope_tasks ->> 0 = p_task_id::text
                   AND v_job.celery_task_id = v_job.id::text THEN
                    v_full_scope := true;
                END IF;
            END IF;
            IF v_job.status NOT IN ('pending', 'running', 'completed', 'failed', 'cancelled') THEN
                v_consistent := false;
            END IF;
            v_active := v_consistent AND v_full_scope AND NOT v_cancel_requested
                AND v_job.status IN ('pending', 'running');
        END IF;
    END IF;
    RETURN aap_jobs_v1._accepted_image_input_job_reply(
        pg_catalog.jsonb_build_object(
            'job_id', p_job_id,
            'task_id', p_task_id,
            'actor_id', p_actor_id,
            'project_id', p_project_id,
            'exists', v_exists,
            'consistent', v_consistent,
            'active', v_active
        ), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.accepted_image_input_job_consistency(uuid, uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

-- Labeling contributes the Task binding/version facts without granting Assets
-- any direct Task table access.
CREATE FUNCTION aap_labeling_v1._accepted_image_input_task_reply(p_data jsonb, p_code text)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_code IS NULL THEN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-accepted-image-input/v1', 'ok', true, 'data', p_data
    ) ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-accepted-image-input/v1', 'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._accepted_image_input_task_reply(jsonb, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.project_accepted_image_input_task(
    p_task_id uuid, p_actor_id uuid, p_lock_source boolean
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_source jsonb;
    v_source_data jsonb;
    v_task_id uuid;
    v_project_id uuid;
    v_item_id uuid;
    v_task_version integer;
    v_task_generation bigint;
    v_snapshot text;
    v_file_type text;
    v_upload_ready boolean;
BEGIN
    IF p_task_id IS NULL OR p_lock_source IS NULL
       OR (p_actor_id IS NULL AND NOT p_lock_source) THEN
        RETURN aap_labeling_v1._accepted_image_input_task_reply(NULL, 'invalid_request');
    END IF;
    v_source := aap_labeling_v1.read_image_task_source(
        p_task_id, p_actor_id, p_lock_source
    );
    IF v_source IS NULL
       OR v_source ->> 'schema' IS DISTINCT FROM 'aap-labeling-image-source/v1'
       OR pg_catalog.jsonb_typeof(v_source -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._accepted_image_input_task_reply(NULL, 'invalid_response');
    END IF;
    IF v_source -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._accepted_image_input_task_reply(
            NULL, v_source #>> '{error,code}'
        );
    END IF;
    v_source_data := v_source -> 'data';
    IF pg_catalog.jsonb_typeof(v_source_data) IS DISTINCT FROM 'object'
       OR (v_source_data - ARRAY[
            'task_id', 'project_id', 'dataset_item_id', 'file_type', 'file_path',
            'file_name', 'created_at', 'source_generation', 'source_snapshot',
            'object_identity', 'source_drifted', 'upload_ready', 'thumbnail_path',
            'blurhash', 'thumbnail_source_generation'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (v_source_data ?& ARRAY[
            'task_id', 'project_id', 'dataset_item_id', 'file_type', 'file_path',
            'file_name', 'created_at', 'source_generation', 'source_snapshot',
            'object_identity', 'source_drifted', 'upload_ready', 'thumbnail_path',
            'blurhash', 'thumbnail_source_generation'
       ])
       OR pg_catalog.jsonb_typeof(v_source_data -> 'task_id') IS DISTINCT FROM 'string'
       OR v_source_data ->> 'task_id' IS DISTINCT FROM p_task_id::text
       OR pg_catalog.jsonb_typeof(v_source_data -> 'project_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_source_data -> 'source_generation') IS DISTINCT FROM 'number'
       OR v_source_data ->> 'source_generation' !~ '^[1-9][0-9]*$'
       OR pg_catalog.jsonb_typeof(v_source_data -> 'source_snapshot') IS DISTINCT FROM 'string'
       OR v_source_data ->> 'source_snapshot' !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(v_source_data -> 'file_type') IS DISTINCT FROM 'string'
       OR v_source_data ->> 'file_type' IS DISTINCT FROM 'image'
       OR pg_catalog.jsonb_typeof(v_source_data -> 'upload_ready') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._accepted_image_input_task_reply(NULL, 'invalid_response');
    END IF;
    BEGIN
        v_task_id := (v_source_data ->> 'task_id')::uuid;
        v_project_id := (v_source_data ->> 'project_id')::uuid;
        v_item_id := CASE
            WHEN v_source_data -> 'dataset_item_id' = 'null'::jsonb THEN NULL
            ELSE (v_source_data ->> 'dataset_item_id')::uuid
        END;
        v_task_generation := (v_source_data ->> 'source_generation')::bigint;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_labeling_v1._accepted_image_input_task_reply(NULL, 'invalid_response');
    END;
    v_snapshot := v_source_data ->> 'source_snapshot';
    v_file_type := v_source_data ->> 'file_type';
    v_upload_ready := (v_source_data ->> 'upload_ready')::boolean;
    SELECT task.version INTO v_task_version
    FROM public.tasks AS task
    WHERE task.id = p_task_id;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._accepted_image_input_task_reply(NULL, 'source_missing');
    END IF;
    IF v_task_version IS NULL OR v_task_version <= 0
       OR v_task_id IS DISTINCT FROM p_task_id
       OR v_file_type IS DISTINCT FROM 'image' THEN
        RETURN aap_labeling_v1._accepted_image_input_task_reply(NULL, 'invalid_response');
    END IF;
    IF NOT v_upload_ready THEN
        RETURN aap_labeling_v1._accepted_image_input_task_reply(NULL, 'source_not_ready');
    END IF;
    RETURN aap_labeling_v1._accepted_image_input_task_reply(
        pg_catalog.jsonb_build_object(
            'task_id', v_task_id,
            'project_id', v_project_id,
            'dataset_item_id', v_item_id,
            'task_version', v_task_version,
            'task_source_generation', v_task_generation,
            'task_source_snapshot', v_snapshot
        ), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_labeling_v1._accepted_image_input_task_reply(NULL, 'source_busy');
WHEN OTHERS THEN
    RETURN aap_labeling_v1._accepted_image_input_task_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.project_accepted_image_input_task(uuid, uuid, boolean)
    FROM PUBLIC;
