-- Cancel only accepted Jobs-owned create_tasks generations for the active association.
CREATE FUNCTION aap_jobs_v1.cancel_dataset_task_creation(
    p_dataset_id uuid,
    p_project_id uuid,
    p_association_id uuid
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_cancelled bigint;
BEGIN
    IF p_dataset_id IS NULL OR p_project_id IS NULL OR p_association_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid dataset task cancellation scope';
    END IF;

    WITH matches AS MATERIALIZED (
        SELECT job.id
        FROM public.async_jobs AS job
        WHERE job.kind = 'create_tasks'
          AND job.status IN ('pending', 'running')
          -- The FK-backed live column is authoritative; payload/retained scope is not.
          AND job.project_id = p_project_id
          AND aap_jobs_v1._task_creation_job_payload_valid(job.id, job.payload)
          AND (job.user_id IS NULL OR job.user_id::text = job.payload ->> 'actor_id')
          AND job.payload ->> 'dataset_id' = p_dataset_id::text
          AND job.payload ->> 'project_id' = p_project_id::text
          AND job.payload ->> 'association_id' = p_association_id::text
        ORDER BY job.id
        FOR UPDATE OF job
    )
    UPDATE public.async_jobs AS job
    SET status = 'cancelled',
        completed_at = pg_catalog.now(),
        updated_at = pg_catalog.now()
    FROM matches
    WHERE job.id = matches.id;

    GET DIAGNOSTICS v_cancelled = ROW_COUNT;
    RETURN v_cancelled;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.cancel_dataset_task_creation(uuid, uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_audit_v1.valid_request_context(p_context jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_ip pg_catalog.inet;
BEGIN
    IF p_context IS NULL THEN
        RETURN true;
    END IF;
    IF pg_catalog.jsonb_typeof(p_context) IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    IF (p_context - ARRAY['method', 'path', 'ip', 'request_id']) IS DISTINCT FROM '{}'::jsonb
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.jsonb_each(p_context) AS field(key, value)
           WHERE pg_catalog.jsonb_typeof(field.value) NOT IN ('string', 'null')
       ) THEN
        RETURN false;
    END IF;
    IF pg_catalog.char_length(p_context ->> 'method') > 8
       OR pg_catalog.char_length(p_context ->> 'path') > 256
       OR pg_catalog.char_length(p_context ->> 'request_id') > 36
       OR pg_catalog.char_length(p_context ->> 'ip') > 64 THEN
        RETURN false;
    END IF;

    BEGIN
        v_ip := NULLIF(p_context ->> 'ip', '')::pg_catalog.inet;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN false;
    END;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.valid_request_context(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_audit_v1.record_dataset_unlink(
    p_operation_id uuid,
    p_actor_id uuid,
    p_dataset_id uuid,
    p_project_id uuid,
    p_deleted_tasks bigint,
    p_deleted_annotations bigint,
    p_deleted_batch_ids uuid[],
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
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_dataset_id IS NULL
       OR p_project_id IS NULL OR p_deleted_tasks IS NULL OR p_deleted_tasks < 0
       OR p_deleted_annotations IS NULL OR p_deleted_annotations < 0
       OR p_deleted_batch_ids IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid dataset unlink audit metadata';
    END IF;
    IF pg_catalog.cardinality(p_deleted_batch_ids) > 0
       AND pg_catalog.array_ndims(p_deleted_batch_ids) IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid dataset unlink batch ids';
    END IF;
    IF pg_catalog.array_position(p_deleted_batch_ids, NULL) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid dataset unlink batch ids';
    END IF;
    IF NOT aap_audit_v1.valid_request_context(p_context) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid dataset unlink audit context';
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
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'dataset unlink audit actor unavailable';
    END IF;

    INSERT INTO public.audit_logs (
        actor_id, actor_email, actor_role, action, target_type, target_id,
        method, path, status_code, ip, request_id, detail_json
    ) VALUES (
        p_actor_id, v_actor #>> '{data,email}', v_actor #>> '{data,platform_role}',
        'dataset.unlink', 'dataset', p_dataset_id::text,
        p_context ->> 'method', p_context ->> 'path', 200,
        NULLIF(p_context ->> 'ip', '')::pg_catalog.inet,
        p_context ->> 'request_id',
        pg_catalog.jsonb_build_object(
            'operation_id', p_operation_id,
            'project_id', p_project_id,
            'deleted_tasks', p_deleted_tasks,
            'deleted_annotations', p_deleted_annotations,
            'deleted_batches', pg_catalog.cardinality(p_deleted_batch_ids),
            'deleted_batch_ids', pg_catalog.to_jsonb(p_deleted_batch_ids),
            'soft', false
        )
    ) RETURNING id INTO v_audit_id;
    RETURN v_audit_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_audit_v1.record_dataset_unlink(uuid, uuid, uuid, uuid, bigint, bigint, uuid[], jsonb) FROM PUBLIC;
