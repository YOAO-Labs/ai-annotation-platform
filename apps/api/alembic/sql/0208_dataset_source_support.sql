-- Jobs and Audit support for atomic Dataset/DatasetItem deletion.
-- The caller owns the transaction; these functions never commit or adopt scope.
CREATE FUNCTION aap_jobs_v1.guard_dataset_source_deletion(p_dataset_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_schema constant text := 'aap-source-retention/v1';
BEGIN
    IF p_dataset_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', v_schema,
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    -- A malformed scope cannot prove that an active import is unrelated to
    -- this Dataset, so fail closed without rewriting its payload or status.
    IF EXISTS (
        SELECT 1
        FROM public.async_jobs AS job
        WHERE job.kind = 'dataset_import'
          AND job.status IN ('pending', 'running')
          AND (
              pg_catalog.jsonb_typeof(job.payload) IS DISTINCT FROM 'object'
              OR pg_catalog.jsonb_typeof(job.payload -> 'dataset_id')
                    IS DISTINCT FROM 'string'
              OR COALESCE(
                    job.payload ->> 'dataset_id'
                        ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$',
                    false
                 ) IS NOT TRUE
              OR job.payload ->> 'dataset_id' = p_dataset_id::text
          )
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', v_schema,
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'source_busy')
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', v_schema,
        'ok', true,
        'data', pg_catalog.jsonb_build_object()
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_dataset_source_deletion(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.task_creation_execution_active(
    p_operation_id uuid,
    p_job_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
BEGIN
    IF p_operation_id IS NULL THEN
        RETURN true;
    END IF;
    IF p_job_id IS NULL THEN
        -- Synchronous accepted operations remain active until Labeling marks
        -- their operation terminal in the same caller-owned transaction.
        RETURN true;
    END IF;

    SELECT job.*
    INTO v_job
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id;

    IF NOT FOUND
       OR p_job_id IS DISTINCT FROM p_operation_id
       OR v_job.kind IS DISTINCT FROM 'create_tasks'
       OR NOT aap_jobs_v1._task_creation_job_payload_valid(v_job.id, v_job.payload)
       OR v_job.project_id IS NULL
       OR v_job.project_id::text IS DISTINCT FROM v_job.payload ->> 'project_id'
       OR (
            v_job.user_id IS NOT NULL
            AND v_job.user_id::text IS DISTINCT FROM v_job.payload ->> 'actor_id'
       ) THEN
        RETURN true;
    END IF;

    IF v_job.status IN ('completed', 'failed', 'cancelled') THEN
        RETURN false;
    END IF;
    IF v_job.status IN ('pending', 'running') THEN
        RETURN COALESCE(
            v_job.payload -> 'cancel_requested' = 'true'::jsonb,
            false
        ) IS NOT TRUE;
    END IF;

    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.task_creation_execution_active(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_audit_v1.record_dataset_source_deletion(
    p_operation_id uuid,
    p_actor_id uuid,
    p_dataset_id uuid,
    p_item_id uuid,
    p_deleted_items bigint,
    p_deleted_scenes bigint,
    p_deleted_associations bigint,
    p_discarded_uploads bigint,
    p_cancelled_jobs bigint,
    p_queued_storage bigint,
    p_context jsonb
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_audit_id bigint;
    v_action text;
    v_target_type text;
    v_target_id text;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_dataset_id IS NULL
       OR p_deleted_items IS NULL OR p_deleted_items < 0
       OR p_deleted_scenes IS NULL OR p_deleted_scenes < 0
       OR p_deleted_associations IS NULL OR p_deleted_associations < 0
       OR p_discarded_uploads IS NULL OR p_discarded_uploads < 0
       OR p_cancelled_jobs IS NULL OR p_cancelled_jobs < 0
       OR p_queued_storage IS NULL OR p_queued_storage < 0
       OR NOT aap_audit_v1.valid_request_context(p_context) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid dataset source deletion audit metadata';
    END IF;

    v_actor := aap_identity_v1.lock_audit_actor(p_actor_id);
    IF v_actor IS NULL
       OR pg_catalog.jsonb_typeof(v_actor) IS DISTINCT FROM 'object'
       OR (v_actor - ARRAY['schema', 'ok', 'data']) IS DISTINCT FROM '{}'::jsonb
       OR v_actor ->> 'schema' IS DISTINCT FROM 'aap-audit-actor/v1'
       OR v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_actor -> 'data') IS DISTINCT FROM 'object'
       OR ((v_actor -> 'data') - ARRAY['actor_id', 'email', 'platform_role']) IS DISTINCT FROM '{}'::jsonb
       OR v_actor #>> '{data,actor_id}' IS DISTINCT FROM p_actor_id::text
       OR pg_catalog.jsonb_typeof(v_actor #> '{data,email}') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(v_actor #>> '{data,email}') NOT BETWEEN 1 AND 255
       OR pg_catalog.jsonb_typeof(v_actor #> '{data,platform_role}') IS DISTINCT FROM 'string'
       OR v_actor #>> '{data,platform_role}' NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'dataset source deletion audit actor unavailable';
    END IF;

    IF p_item_id IS NULL THEN
        v_action := 'dataset.delete';
        v_target_type := 'dataset';
        v_target_id := p_dataset_id::text;
    ELSE
        v_action := 'dataset.item_delete';
        v_target_type := 'dataset_item';
        v_target_id := p_item_id::text;
    END IF;

    INSERT INTO public.audit_logs (
        actor_id, actor_email, actor_role, action, target_type, target_id,
        method, path, status_code, ip, request_id, detail_json
    ) VALUES (
        p_actor_id, v_actor #>> '{data,email}', v_actor #>> '{data,platform_role}',
        v_action, v_target_type, v_target_id,
        p_context ->> 'method', p_context ->> 'path', 204,
        NULLIF(p_context ->> 'ip', '')::pg_catalog.inet,
        p_context ->> 'request_id',
        pg_catalog.jsonb_build_object(
            'operation_id', p_operation_id,
            'dataset_id', p_dataset_id,
            'item_id', p_item_id,
            'deleted_items', p_deleted_items,
            'deleted_scenes', p_deleted_scenes,
            'deleted_associations', p_deleted_associations,
            'discarded_uploads', p_discarded_uploads,
            'cancelled_jobs', p_cancelled_jobs,
            'queued_storage', p_queued_storage
        )
    ) RETURNING id INTO v_audit_id;

    RETURN v_audit_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.record_dataset_source_deletion(uuid, uuid, uuid, uuid, bigint, bigint, bigint, bigint, bigint, bigint, jsonb) FROM PUBLIC;
