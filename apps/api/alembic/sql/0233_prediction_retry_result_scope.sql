CREATE OR REPLACE FUNCTION aap_jobs_v1._lock_batch_result_scope(
    p_scope jsonb,
    p_require_lease boolean,
    p_require_current boolean DEFAULT false
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_execution public.batch_result_executions%ROWTYPE;
    v_cancel_requested boolean := false;
BEGIN
    IF NOT aap_jobs_v1._batch_result_scope_valid(p_scope, p_require_lease) THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT * INTO v_job FROM public.async_jobs
        WHERE id = (p_scope ->> 'job_id')::uuid
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'execution_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_missing');
    END IF;
    IF v_job.kind NOT IN ('batch_predict','prediction_retry')
       OR (v_job.kind='prediction_retry' AND NOT aap_labeling_v1._prediction_retry_attempt_scope_valid(p_scope,false))
       OR (v_job.user_id IS DISTINCT FROM (p_scope ->> 'actor_id')::uuid
           AND (p_require_current OR v_job.user_id IS NOT NULL))
       OR COALESCE(v_job.project_id, v_job.retained_project_id)
            IS DISTINCT FROM (p_scope ->> 'project_id')::uuid THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'scope_mismatch');
    END IF;
    IF pg_catalog.jsonb_typeof(v_job.payload) IS DISTINCT FROM 'object'
       OR (v_job.payload ? 'cancel_requested'
           AND pg_catalog.jsonb_typeof(v_job.payload -> 'cancel_requested') IS DISTINCT FROM 'boolean')
       OR (v_job.payload ? 'preannotation_request_digest'
           AND (pg_catalog.jsonb_typeof(v_job.payload -> 'preannotation_request_digest') IS DISTINCT FROM 'string'
                OR v_job.payload ->> 'preannotation_request_digest' !~ '^[0-9a-f]{64}$'))
       OR (v_job.kind='prediction_retry' AND (
           pg_catalog.jsonb_typeof(v_job.payload -> 'prediction_retry_request_digest') IS DISTINCT FROM 'string'
           OR v_job.payload ->> 'prediction_retry_request_digest' !~ '^[0-9a-f]{64}$'
           OR v_job.payload ->> 'prediction_retry_request_digest' IS DISTINCT FROM p_scope ->> 'request_digest'
       )) THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
    END IF;
    IF v_job.payload ? 'preannotation_request_digest'
       AND v_job.payload ->> 'preannotation_request_digest' IS DISTINCT FROM p_scope ->> 'request_digest' THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
    END IF;
    v_cancel_requested := COALESCE((v_job.payload ->> 'cancel_requested')::boolean, false);
    IF v_job.status NOT IN ('pending', 'running') AND p_require_current THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_not_writable');
    END IF;
    BEGIN
        SELECT * INTO v_execution FROM public.batch_result_executions
        WHERE job_id = v_job.id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'execution_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_missing');
    END IF;
    IF v_execution.actor_id IS DISTINCT FROM (p_scope ->> 'actor_id')::uuid
       OR v_execution.project_id IS DISTINCT FROM (p_scope ->> 'project_id')::uuid
       OR v_execution.request_digest IS DISTINCT FROM p_scope ->> 'request_digest'
       OR (v_job.payload ? 'preannotation_request_digest'
           AND v_job.payload ->> 'preannotation_request_digest' IS DISTINCT FROM v_execution.request_digest)
       OR (v_job.kind='prediction_retry' AND v_job.payload ->> 'prediction_retry_request_digest' IS DISTINCT FROM v_execution.request_digest) THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'scope_mismatch');
    END IF;
    IF p_require_lease THEN
        IF v_execution.generation IS DISTINCT FROM (p_scope ->> 'generation')::bigint
           OR v_execution.lease_token IS DISTINCT FROM (p_scope ->> 'lease_token')::uuid
           OR v_execution.lease_token IS NULL
           OR v_execution.lease_expires_at IS NULL
           OR v_execution.lease_expires_at <= pg_catalog.clock_timestamp() THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'lease_lost');
        END IF;
    END IF;
    RETURN aap_jobs_v1._batch_result_reply(
        pg_catalog.jsonb_build_object(
            'job_status', v_job.status,
            'cancel_requested', v_cancel_requested,
            'progress', aap_jobs_v1._batch_result_progress(v_job.id)
        ), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._lock_batch_result_scope(jsonb, boolean, boolean) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.lock_prediction_retry_finish_scope(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_locked jsonb;
    v_execution public.batch_result_executions%ROWTYPE;
    v_member public.batch_result_members%ROWTYPE;
    v_member_count bigint;
    v_member_json jsonb := NULL;
    v_job_id uuid;
BEGIN
    IF p_payload IS NULL OR pg_catalog.octet_length(p_payload::text)>2097152
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['job_id','actor_id','project_id','request_digest','generation','lease_token']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY['job_id','actor_id','project_id','request_digest','generation','lease_token'])
       OR NOT aap_jobs_v1._batch_result_scope_valid(p_payload,true) THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL,'invalid_request');
    END IF;
    v_job_id := (p_payload ->> 'job_id')::uuid;
    v_locked := aap_jobs_v1._lock_batch_result_scope(p_payload,true,true);
    IF v_locked -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_locked;
    END IF;
    SELECT * INTO v_execution FROM public.batch_result_executions
    WHERE job_id=v_job_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR NOT v_execution.selection_complete
       OR v_execution.total NOT IN (0,1) THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL,'result_conflict');
    END IF;
    SELECT count(*)::bigint INTO v_member_count
    FROM public.batch_result_members AS member
    WHERE member.job_id=v_job_id;
    IF v_member_count IS DISTINCT FROM v_execution.total OR v_member_count>1 THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL,'selection_conflict');
    END IF;
    IF v_member_count=0 THEN
        IF NOT aap_labeling_v1._prediction_retry_member_scope_matches(
            v_job_id,NULL,'null'::jsonb
        ) THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL,'scope_mismatch');
        END IF;
    ELSE
        BEGIN
            SELECT * INTO v_member FROM public.batch_result_members AS member
            WHERE member.job_id=v_job_id AND member.ordinal=1 FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL,'execution_busy');
        END;
        IF NOT FOUND THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL,'selection_conflict');
        END IF;
        IF NOT aap_labeling_v1._prediction_retry_member_scope_matches(
            v_job_id,v_member.task_id,v_member.write_scope
        ) THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL,'scope_mismatch');
        END IF;
        v_member_json := pg_catalog.jsonb_build_object(
            'ordinal',v_member.ordinal,'task_id',v_member.task_id,
            'write_scope',v_member.write_scope,'state',v_member.state,
            'result_id',v_member.result_id,'failure_id',v_member.failure_id,
            'failed_prediction_id',v_member.failed_prediction_id,
            'error_code',v_member.error_code
        );
    END IF;
    RETURN aap_jobs_v1._batch_result_reply(
        pg_catalog.jsonb_build_object(
            'progress',aap_jobs_v1._batch_result_progress(v_job_id),
            'member',v_member_json,
            'cancel_requested',v_locked #> '{data,cancel_requested}'
        ),NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL,'execution_busy');
WHEN OTHERS THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL,'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.lock_prediction_retry_finish_scope(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._batch_result_scope_matches_member(
    p_scope jsonb,
    p_job_id uuid,
    p_actor_id uuid,
    p_project_id uuid,
    p_task_id uuid
)
RETURNS boolean
LANGUAGE plpgsql STABLE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_allowed text[] := ARRAY[
        'task_id', 'project_id', 'actor_id', 'actor_known', 'operation',
        'execution_scope', 'execution_scope_known', 'task_version', 'task_status',
        'assignee_id', 'review_round_id', 'source_annotation_id',
        'source_annotation_version', 'expected_batch_id', 'expected_batch_id_known',
        'job_id', 'job_id_known', 'source_binding_known', 'task_source_generation',
        'source_dataset_item_id', 'source_file_path', 'source_file_type',
        'accepted_input_id'
    ];
    v_key text;
    v_value jsonb;
    v_job_kind text;
BEGIN
    SELECT job.kind INTO v_job_kind FROM public.async_jobs AS job WHERE job.id=p_job_id;
    IF NOT FOUND THEN RETURN false; END IF;
    IF p_scope IS NULL OR p_scope = 'null'::jsonb THEN RETURN v_job_kind='batch_predict'; END IF;
    IF pg_catalog.jsonb_typeof(p_scope) IS DISTINCT FROM 'object'
       OR (p_scope - v_allowed) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_scope ?& ARRAY[
            'task_id', 'project_id', 'actor_id', 'actor_known', 'operation',
            'execution_scope', 'execution_scope_known', 'task_version', 'task_status',
            'assignee_id', 'review_round_id', 'source_annotation_id',
            'source_annotation_version', 'expected_batch_id', 'expected_batch_id_known',
            'job_id', 'job_id_known', 'source_binding_known', 'task_source_generation',
            'source_dataset_item_id', 'source_file_path', 'source_file_type',
            'accepted_input_id'
       ]) THEN
        RETURN false;
    END IF;
    IF p_scope ->> 'task_id' IS DISTINCT FROM p_task_id::text
       OR p_scope ->> 'project_id' IS DISTINCT FROM p_project_id::text
       OR p_scope ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR p_scope ->> 'job_id' IS DISTINCT FROM p_job_id::text
       OR p_scope ->> 'operation' IS DISTINCT FROM CASE WHEN v_job_kind='prediction_retry' THEN 'prediction_retry' ELSE 'preannotation' END
       OR p_scope -> 'actor_known' IS DISTINCT FROM 'true'::jsonb
       OR p_scope -> 'execution_scope_known' IS DISTINCT FROM 'true'::jsonb
       OR p_scope -> 'job_id_known' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN false;
    END IF;
    FOREACH v_key IN ARRAY ARRAY['task_id', 'project_id', 'actor_id', 'job_id'] LOOP
        IF pg_catalog.jsonb_typeof(p_scope -> v_key) IS DISTINCT FROM 'string'
           OR p_scope ->> v_key !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN false;
        END IF;
    END LOOP;
    FOREACH v_key IN ARRAY ARRAY['actor_known', 'execution_scope_known', 'expected_batch_id_known', 'job_id_known', 'source_binding_known'] LOOP
        IF pg_catalog.jsonb_typeof(p_scope -> v_key) IS DISTINCT FROM 'boolean' THEN
            RETURN false;
        END IF;
    END LOOP;
    IF pg_catalog.jsonb_typeof(p_scope -> 'operation') IS DISTINCT FROM 'string'
       OR (p_scope -> 'execution_scope' <> 'null'::jsonb
           AND pg_catalog.jsonb_typeof(p_scope -> 'execution_scope') IS DISTINCT FROM 'string')
       OR (p_scope -> 'task_status' <> 'null'::jsonb
           AND pg_catalog.jsonb_typeof(p_scope -> 'task_status') IS DISTINCT FROM 'string')
       OR (p_scope -> 'source_file_path' <> 'null'::jsonb
           AND pg_catalog.jsonb_typeof(p_scope -> 'source_file_path') IS DISTINCT FROM 'string')
       OR (p_scope -> 'source_file_type' <> 'null'::jsonb
           AND pg_catalog.jsonb_typeof(p_scope -> 'source_file_type') IS DISTINCT FROM 'string') THEN
        RETURN false;
    END IF;
    FOREACH v_key IN ARRAY ARRAY[
        'assignee_id', 'review_round_id', 'source_annotation_id',
        'expected_batch_id', 'source_dataset_item_id', 'accepted_input_id'
    ] LOOP
        v_value := p_scope -> v_key;
        IF v_value <> 'null'::jsonb AND (
            pg_catalog.jsonb_typeof(v_value) IS DISTINCT FROM 'string'
            OR v_value #>> '{}' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        ) THEN
            RETURN false;
        END IF;
    END LOOP;
    FOREACH v_key IN ARRAY ARRAY['task_version', 'source_annotation_version', 'task_source_generation'] LOOP
        v_value := p_scope -> v_key;
        IF v_value <> 'null'::jsonb AND (
            pg_catalog.jsonb_typeof(v_value) IS DISTINCT FROM 'number'
            OR v_value #>> '{}' !~ '^(0|[1-9][0-9]*)$'
        ) THEN
            RETURN false;
        END IF;
    END LOOP;
    IF v_job_kind='prediction_retry' THEN
        RETURN aap_labeling_v1._prediction_retry_member_scope_matches(p_job_id,p_task_id,p_scope);
    END IF;
    RETURN v_job_kind='batch_predict';
EXCEPTION WHEN OTHERS THEN
    RETURN false;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._batch_result_scope_matches_member(jsonb, uuid, uuid, uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1.claim_batch_result_execution(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_execution public.batch_result_executions%ROWTYPE;
    v_worker uuid;
    v_execution_id text;
    v_seconds integer;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_lease jsonb;
BEGIN
    IF p_payload IS NULL OR pg_catalog.octet_length(p_payload::text) > 2097152
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['job_id', 'actor_id', 'project_id', 'request_digest', 'worker_id', 'execution_id', 'lease_seconds']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY['job_id', 'actor_id', 'project_id', 'request_digest', 'worker_id', 'execution_id', 'lease_seconds'])
       OR NOT aap_jobs_v1._batch_result_scope_valid(
            p_payload - ARRAY['worker_id', 'execution_id', 'lease_seconds'], false
       )
       OR pg_catalog.jsonb_typeof(p_payload -> 'worker_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'worker_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR (p_payload -> 'execution_id' <> 'null'::jsonb
           AND pg_catalog.jsonb_typeof(p_payload -> 'execution_id') IS DISTINCT FROM 'string')
       OR pg_catalog.jsonb_typeof(p_payload -> 'lease_seconds') IS DISTINCT FROM 'number'
       OR p_payload ->> 'lease_seconds' !~ '^[0-9]+$' THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_worker := (p_payload ->> 'worker_id')::uuid;
    v_execution_id := CASE WHEN p_payload -> 'execution_id' = 'null'::jsonb
        THEN NULL ELSE p_payload ->> 'execution_id' END;
    v_seconds := (p_payload ->> 'lease_seconds')::integer;
    IF v_seconds < 5 OR v_seconds > 300 OR v_execution_id = '' THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT * INTO v_job FROM public.async_jobs
        WHERE id = (p_payload ->> 'job_id')::uuid
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'execution_busy');
    END;
    IF NOT FOUND THEN RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_missing'); END IF;
    IF v_job.kind NOT IN ('batch_predict','prediction_retry')
       OR (v_job.kind='prediction_retry' AND NOT aap_labeling_v1._prediction_retry_attempt_scope_valid(
            p_payload - ARRAY['worker_id','execution_id','lease_seconds'],false
       ))
       OR v_job.user_id IS DISTINCT FROM (p_payload ->> 'actor_id')::uuid
       OR v_job.project_id IS DISTINCT FROM (p_payload ->> 'project_id')::uuid
       OR (v_job.retained_project_id IS NOT NULL AND v_job.retained_project_id IS DISTINCT FROM v_job.project_id) THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'scope_mismatch');
    END IF;
    IF pg_catalog.jsonb_typeof(v_job.payload) IS DISTINCT FROM 'object'
       OR (v_job.payload ? 'cancel_requested'
           AND pg_catalog.jsonb_typeof(v_job.payload -> 'cancel_requested') IS DISTINCT FROM 'boolean')
       OR (v_job.payload ? 'preannotation_request_digest'
           AND (pg_catalog.jsonb_typeof(v_job.payload -> 'preannotation_request_digest') IS DISTINCT FROM 'string'
                OR v_job.payload ->> 'preannotation_request_digest' !~ '^[0-9a-f]{64}$'))
       OR (v_job.kind='prediction_retry' AND (
           pg_catalog.jsonb_typeof(v_job.payload -> 'prediction_retry_request_digest') IS DISTINCT FROM 'string'
           OR v_job.payload ->> 'prediction_retry_request_digest' !~ '^[0-9a-f]{64}$'
           OR v_job.payload ->> 'prediction_retry_request_digest' IS DISTINCT FROM p_payload ->> 'request_digest'
       )) THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
    END IF;
    IF (v_job.kind='batch_predict' AND v_job.payload ? 'preannotation_request_digest'
       AND v_job.payload ->> 'preannotation_request_digest' IS DISTINCT FROM p_payload ->> 'request_digest')
       OR (v_job.kind='prediction_retry' AND v_job.payload ->> 'prediction_retry_request_digest' IS DISTINCT FROM p_payload ->> 'request_digest') THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
    END IF;
    IF v_job.status NOT IN ('pending', 'running') THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_not_writable');
    END IF;
    BEGIN
        SELECT * INTO v_execution FROM public.batch_result_executions
        WHERE job_id = v_job.id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'execution_busy');
    END;
    IF NOT FOUND THEN
        IF v_job.kind='prediction_retry' THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL,'scope_mismatch');
        END IF;
        IF v_job.status <> 'pending' THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_not_writable');
        END IF;
        INSERT INTO public.batch_result_executions(
            job_id, actor_id, project_id, request_digest, execution_id,
            state, generation, worker_id, lease_token, lease_expires_at
        ) VALUES (
            v_job.id, v_job.user_id, v_job.project_id,
            p_payload ->> 'request_digest', v_execution_id,
            'preparing', 1, v_worker, pg_catalog.gen_random_uuid(),
            v_now + pg_catalog.make_interval(secs => v_seconds)
        ) RETURNING * INTO v_execution;
    ELSE
        IF v_execution.actor_id IS DISTINCT FROM v_job.user_id
           OR v_execution.project_id IS DISTINCT FROM v_job.project_id
           OR v_execution.request_digest IS DISTINCT FROM p_payload ->> 'request_digest' THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
        END IF;
        IF v_execution.state IN ('completed', 'failed', 'cancelled') THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_not_writable');
        END IF;
        IF v_execution.lease_token IS NOT NULL
           AND v_execution.lease_expires_at > v_now THEN
            IF v_execution.worker_id = v_worker
               AND v_execution.execution_id IS NOT DISTINCT FROM v_execution_id THEN
                v_lease := aap_jobs_v1._batch_result_lease(v_execution);
                RETURN aap_jobs_v1._batch_result_reply(
                    pg_catalog.jsonb_build_object('lease', v_lease, 'progress', aap_jobs_v1._batch_result_progress(v_job.id)), NULL
                );
            END IF;
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'execution_busy');
        END IF;
        IF v_execution.generation >= 9223372036854775807 THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
        END IF;
        UPDATE public.batch_result_executions
        SET generation = generation + 1,
            worker_id = v_worker,
            lease_token = pg_catalog.gen_random_uuid(),
            execution_id = v_execution_id,
            lease_expires_at = v_now + pg_catalog.make_interval(secs => v_seconds),
            updated_at = v_now
        WHERE job_id = v_job.id
        RETURNING * INTO v_execution;
    END IF;
    IF v_job.kind='batch_predict' AND NOT (v_job.payload ? 'preannotation_request_digest') THEN
        UPDATE public.async_jobs
        SET payload = payload || pg_catalog.jsonb_build_object(
                'preannotation_request_digest', v_execution.request_digest
            ),
            updated_at = pg_catalog.clock_timestamp()
        WHERE id = v_job.id;
    END IF;
    v_lease := aap_jobs_v1._batch_result_lease(v_execution);
    RETURN aap_jobs_v1._batch_result_reply(
        pg_catalog.jsonb_build_object(
            'lease', v_lease, 'progress', aap_jobs_v1._batch_result_progress(v_job.id)
        ), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.claim_batch_result_execution(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1.read_batch_result_execution(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_scope jsonb;
    v_job public.async_jobs%ROWTYPE;
    v_execution public.batch_result_executions%ROWTYPE;
    v_after bigint;
    v_limit integer;
    v_members jsonb;
    v_next bigint;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['job_id','actor_id','project_id','request_digest','after_ordinal','limit']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY['job_id','actor_id','project_id','request_digest','after_ordinal','limit'])
       OR NOT aap_jobs_v1._batch_result_scope_valid(p_payload - ARRAY['after_ordinal','limit'], false)
       OR pg_catalog.jsonb_typeof(p_payload -> 'after_ordinal') IS DISTINCT FROM 'number'
       OR p_payload ->> 'after_ordinal' !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.char_length(p_payload ->> 'after_ordinal') > 19
       OR (p_payload ->> 'after_ordinal')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(p_payload -> 'limit') IS DISTINCT FROM 'number'
       OR p_payload ->> 'limit' !~ '^[0-9]+$'
       OR pg_catalog.char_length(p_payload ->> 'limit') > 3
       OR (p_payload ->> 'limit')::numeric > 100 THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_after := (p_payload ->> 'after_ordinal')::bigint;
    v_limit := (p_payload ->> 'limit')::integer;
    IF v_after < 0 OR v_limit < 1 OR v_limit > 100 THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_scope := p_payload - ARRAY['after_ordinal','limit'];
    SELECT * INTO v_execution FROM public.batch_result_executions
    WHERE job_id = (v_scope ->> 'job_id')::uuid;
    IF NOT FOUND THEN RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_missing'); END IF;
    IF v_execution.actor_id IS DISTINCT FROM (v_scope ->> 'actor_id')::uuid
       OR v_execution.project_id IS DISTINCT FROM (v_scope ->> 'project_id')::uuid
       OR v_execution.request_digest IS DISTINCT FROM v_scope ->> 'request_digest' THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'scope_mismatch');
    END IF;
    SELECT * INTO v_job FROM public.async_jobs WHERE id = v_execution.job_id;
    IF FOUND THEN
        IF v_job.kind NOT IN ('batch_predict','prediction_retry')
           OR (v_job.kind='prediction_retry' AND NOT aap_labeling_v1._prediction_retry_attempt_scope_valid(v_scope,true))
           OR (v_job.kind='prediction_retry' AND v_job.payload ->> 'prediction_retry_request_digest' IS DISTINCT FROM v_scope ->> 'request_digest')
           OR (v_job.user_id IS DISTINCT FROM (v_scope ->> 'actor_id')::uuid AND v_job.user_id IS NOT NULL)
           OR COALESCE(v_job.project_id, v_job.retained_project_id) IS DISTINCT FROM (v_scope ->> 'project_id')::uuid THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'scope_mismatch');
        END IF;
    ELSIF aap_labeling_v1._prediction_retry_attempt_exists(v_execution.job_id)
       AND NOT aap_labeling_v1._prediction_retry_attempt_scope_valid(v_scope,true) THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL,'scope_mismatch');
    ELSIF v_execution.state NOT IN ('completed','failed','cancelled') THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_missing');
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(aap_jobs_v1._batch_result_member_json(member) ORDER BY member.ordinal), '[]'::jsonb)
    INTO v_members
    FROM (
        SELECT * FROM public.batch_result_members
        WHERE job_id = v_execution.job_id AND ordinal > v_after
        ORDER BY ordinal LIMIT v_limit
    ) AS member;
    SELECT max(member.ordinal) INTO v_next
    FROM (
        SELECT ordinal FROM public.batch_result_members
        WHERE job_id = v_execution.job_id AND ordinal > v_after
        ORDER BY ordinal LIMIT v_limit
    ) AS member;
    IF v_next IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.batch_result_members
        WHERE job_id = v_execution.job_id AND ordinal > v_next
    ) THEN
        v_next := v_next;
    ELSE
        v_next := NULL;
    END IF;
    RETURN aap_jobs_v1._batch_result_reply(
        pg_catalog.jsonb_build_object(
            'progress', aap_jobs_v1._batch_result_progress(v_execution.job_id),
            'members', v_members, 'next_ordinal', v_next
        ), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.read_batch_result_execution(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1.batch_result_execution_referenced(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_execution public.batch_result_executions%ROWTYPE;
    v_scope jsonb;
    v_active boolean := false;
BEGIN
    IF NOT aap_jobs_v1._batch_result_scope_valid(p_payload, false) THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    SELECT * INTO v_job FROM public.async_jobs WHERE id = (p_payload ->> 'job_id')::uuid;
    IF NOT FOUND THEN
        SELECT * INTO v_execution FROM public.batch_result_executions
        WHERE job_id = (p_payload ->> 'job_id')::uuid;
        IF NOT FOUND THEN
            RETURN aap_jobs_v1._batch_result_reply(
                pg_catalog.jsonb_build_object(
                    'job_id', p_payload ->> 'job_id', 'referenced', false,
                    'terminal', false, 'job_status', NULL
                ), NULL
            );
        END IF;
        IF v_execution.actor_id IS DISTINCT FROM (p_payload ->> 'actor_id')::uuid
           OR v_execution.project_id IS DISTINCT FROM (p_payload ->> 'project_id')::uuid
           OR v_execution.request_digest IS DISTINCT FROM p_payload ->> 'request_digest' THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'scope_mismatch');
        END IF;
        IF aap_labeling_v1._prediction_retry_attempt_exists(v_execution.job_id)
           AND NOT aap_labeling_v1._prediction_retry_attempt_scope_valid(p_payload,true) THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL,'scope_mismatch');
        END IF;
        IF v_execution.state NOT IN ('completed','failed','cancelled') THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_missing');
        END IF;
        RETURN aap_jobs_v1._batch_result_reply(
            pg_catalog.jsonb_build_object(
                'job_id', v_execution.job_id, 'referenced', false,
                'terminal', true, 'job_status', v_execution.state
            ), NULL
        );
    END IF;
    SELECT * INTO v_execution FROM public.batch_result_executions WHERE job_id = v_job.id;
    IF NOT FOUND THEN
        RETURN aap_jobs_v1._batch_result_reply(
            pg_catalog.jsonb_build_object(
                'job_id', v_job.id, 'referenced', false,
                'terminal', v_job.status IN ('completed','failed','cancelled'),
                'job_status', v_job.status
            ), NULL
        );
    END IF;
    IF v_job.kind NOT IN ('batch_predict','prediction_retry')
       OR (v_job.kind='prediction_retry' AND NOT aap_labeling_v1._prediction_retry_attempt_scope_valid(p_payload,true))
       OR v_execution.actor_id IS DISTINCT FROM (p_payload ->> 'actor_id')::uuid
       OR v_execution.project_id IS DISTINCT FROM (p_payload ->> 'project_id')::uuid
       OR v_execution.request_digest IS DISTINCT FROM p_payload ->> 'request_digest' THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'scope_mismatch');
    END IF;
    v_active := v_job.status IN ('pending','running')
        AND v_execution.state IN ('preparing','running');
    RETURN aap_jobs_v1._batch_result_reply(
        pg_catalog.jsonb_build_object(
            'job_id', v_job.id, 'referenced', v_active,
            'terminal', v_job.status IN ('completed','failed','cancelled'),
            'job_status', v_job.status
        ), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.batch_result_execution_referenced(jsonb) FROM PUBLIC;
