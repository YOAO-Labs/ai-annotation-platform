-- A retry becomes terminal only through the Labeling-specific wrappers below.
-- Common result operations remain available for capture, but their terminal
-- member, bundle, execution and Job transitions require the immutable attempt
-- proof written in the same transaction.
CREATE FUNCTION aap_jobs_v1._prediction_retry_execution_terminal_guard()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_kind text;
    v_state text;
BEGIN
    IF NEW.state IS NOT DISTINCT FROM OLD.state
       OR NEW.state NOT IN ('completed','failed','cancelled') THEN
        RETURN NEW;
    END IF;
    SELECT job.kind INTO v_kind FROM public.async_jobs AS job WHERE job.id=NEW.job_id;
    IF v_kind IS DISTINCT FROM 'prediction_retry' THEN RETURN NEW; END IF;
    v_state := CASE NEW.state
        WHEN 'completed' THEN 'succeeded'
        WHEN 'failed' THEN CASE
            WHEN aap_labeling_v1._prediction_retry_terminal_matches(NEW.job_id,'failed',NULL) THEN 'failed'
            ELSE 'abandoned' END
        ELSE 'abandoned'
    END;
    IF NOT aap_labeling_v1._prediction_retry_terminal_matches(NEW.job_id,v_state,NULL) THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='job_not_writable';
    END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._prediction_retry_execution_terminal_guard() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_batch_result_prediction_retry_execution_guard
BEFORE UPDATE OF state ON public.batch_result_executions
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1._prediction_retry_execution_terminal_guard();
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1._prediction_retry_member_terminal_guard()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_kind text;
    v_state text;
BEGIN
    IF NEW.state IS NOT DISTINCT FROM OLD.state
       OR NEW.state NOT IN ('applied','failed','skipped') THEN
        RETURN NEW;
    END IF;
    SELECT job.kind INTO v_kind FROM public.async_jobs AS job WHERE job.id=NEW.job_id;
    IF v_kind IS DISTINCT FROM 'prediction_retry' THEN RETURN NEW; END IF;
    v_state := CASE NEW.state
        WHEN 'applied' THEN 'succeeded'
        WHEN 'failed' THEN 'failed'
        ELSE 'abandoned'
    END;
    IF NOT aap_labeling_v1._prediction_retry_terminal_matches(NEW.job_id,v_state,NULL) THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='job_not_writable';
    END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._prediction_retry_member_terminal_guard() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_batch_result_prediction_retry_member_guard
BEFORE UPDATE OF state ON public.batch_result_members
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1._prediction_retry_member_terminal_guard();
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._prediction_retry_bundle_terminal_guard()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_expected text;
BEGIN
    IF NEW.state IS NOT DISTINCT FROM OLD.state
       OR NEW.state NOT IN ('applied','failed','abandoned') THEN
        RETURN NEW;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.prediction_retry_attempts AS attempt
        WHERE attempt.job_id=NEW.job_id
    ) THEN RETURN NEW; END IF;
    v_expected := CASE NEW.state
        WHEN 'applied' THEN 'succeeded'
        WHEN 'failed' THEN 'failed'
        ELSE 'abandoned'
    END;
    IF NOT aap_labeling_v1._prediction_retry_terminal_matches(NEW.job_id,v_expected,NULL) THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='job_not_writable';
    END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._prediction_retry_bundle_terminal_guard() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_batch_result_prediction_retry_bundle_guard
BEFORE UPDATE OF state ON public.batch_result_bundles
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._prediction_retry_bundle_terminal_guard();
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1._prediction_retry_job_terminal_trigger()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_attempt_state text;
    v_proof jsonb;
    v_execution public.batch_result_executions%ROWTYPE;
    v_delivery public.prediction_retry_deliveries%ROWTYPE;
    v_skipped bigint := 0;
    v_retired jsonb;
BEGIN
    IF NEW.kind IS DISTINCT FROM 'prediction_retry'
       OR NEW.status NOT IN ('completed','failed','cancelled')
       OR OLD.status IS NOT DISTINCT FROM NEW.status THEN
        RETURN NEW;
    END IF;
    IF NOT aap_labeling_v1._prediction_retry_attempt_exists(NEW.id) THEN
        RETURN NEW;
    END IF;
    IF NEW.status='completed' THEN
        IF NOT aap_labeling_v1._prediction_retry_terminal_matches(NEW.id,'succeeded',NULL) THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='job_not_writable';
        END IF;
    ELSE
        IF aap_labeling_v1._prediction_retry_terminal_matches(NEW.id,'failed',NULL) THEN
            v_attempt_state := 'failed';
        ELSIF aap_labeling_v1._prediction_retry_terminal_matches(NEW.id,'abandoned',NULL) THEN
            v_attempt_state := 'abandoned';
        ELSE
            v_proof := aap_labeling_v1._abandon_prediction_retry_from_job(
                NEW.id,NEW.status,COALESCE(NEW.error_message,'job_terminalized')
            );
            IF v_proof -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE=COALESCE(v_proof #>> '{error,code}','job_not_writable');
            END IF;
            v_attempt_state := 'abandoned';
        END IF;
        IF NEW.status='cancelled' AND v_attempt_state IS DISTINCT FROM 'abandoned' THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='job_not_writable';
        END IF;
    END IF;
    SELECT * INTO v_execution FROM public.batch_result_executions
    WHERE job_id=NEW.id FOR UPDATE;
    IF FOUND THEN
        v_retired := aap_labeling_v1._retire_prediction_retry_results(NEW.id);
        IF v_retired -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE=COALESCE(v_retired #>> '{error,code}','invalid_response');
        END IF;
        IF NEW.status IN ('failed','cancelled') THEN
            UPDATE public.batch_result_members AS member
            SET state='skipped',error_code='job_terminalized',updated_at=pg_catalog.clock_timestamp()
            WHERE member.job_id=NEW.id
              AND member.state IN ('planned','prepared','capturing','captured','ready');
            GET DIAGNOSTICS v_skipped = ROW_COUNT;
        END IF;
        UPDATE public.batch_result_executions
        SET state=NEW.status,
            skipped_count=skipped_count+v_skipped,
            next_ordinal=CASE WHEN v_skipped>0 THEN GREATEST(next_ordinal,2) ELSE next_ordinal END,
            worker_id=NULL,lease_token=NULL,lease_expires_at=NULL,
            error_code=COALESCE(error_code,NEW.error_message),
            updated_at=pg_catalog.clock_timestamp()
        WHERE job_id=NEW.id;
    END IF;
    SELECT * INTO v_delivery FROM public.prediction_retry_deliveries
    WHERE job_id=NEW.id FOR UPDATE;
    IF FOUND THEN
        UPDATE public.prediction_retry_deliveries
        SET state='terminal',worker_id=NULL,lease_expires_at=NULL,
            next_attempt_at=pg_catalog.clock_timestamp(),updated_at=pg_catalog.clock_timestamp()
        WHERE job_id=NEW.id;
    END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._prediction_retry_job_terminal_trigger() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_async_jobs_prediction_retry_terminal
AFTER UPDATE OF status ON public.async_jobs
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1._prediction_retry_job_terminal_trigger();
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1._prediction_retry_job_delete_guard()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
BEGIN
    IF OLD.kind='prediction_retry'
       AND aap_labeling_v1._prediction_retry_attempt_exists(OLD.id)
       AND NOT aap_labeling_v1._prediction_retry_job_delete_allowed(OLD.id) THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='job_not_writable';
    END IF;
    RETURN OLD;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._prediction_retry_job_delete_guard() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_async_jobs_prediction_retry_delete_guard
BEFORE DELETE ON public.async_jobs
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1._prediction_retry_job_delete_guard();
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1._finish_prediction_retry_job(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_execution public.batch_result_executions%ROWTYPE;
    v_member public.batch_result_members%ROWTYPE;
    v_job_id uuid;
    v_status text;
    v_result_id uuid;
    v_failed_id uuid;
    v_task_id uuid;
    v_write_scope jsonb;
    v_error text;
    v_context jsonb;
    v_outcome jsonb;
    v_stages jsonb;
    v_gpu jsonb;
    v_cost numeric := 0;
    v_required_terminal_state text;
    v_existing_members bigint;
    v_skipped_ids jsonb := '[]'::jsonb;
    v_result jsonb;
BEGIN
    IF p_payload IS NULL OR pg_catalog.octet_length(p_payload::text)>2097152
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY[
            'job_id','actor_id','project_id','request_digest','generation','lease_token',
            'status','result_id','failed_prediction_id','task_id','write_scope',
            'terminal_state','error_code','context','outcome'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY[
            'job_id','actor_id','project_id','request_digest','generation','lease_token',
            'status','result_id','failed_prediction_id','task_id','write_scope',
            'terminal_state','error_code','context','outcome'
       ])
       OR NOT aap_jobs_v1._batch_result_scope_valid(p_payload - ARRAY[
            'status','result_id','failed_prediction_id','task_id','write_scope',
            'terminal_state','error_code','context','outcome'
       ],true)
       OR pg_catalog.jsonb_typeof(p_payload -> 'status') IS DISTINCT FROM 'string'
       OR p_payload ->> 'status' NOT IN ('completed','failed','cancelled')
       OR pg_catalog.jsonb_typeof(p_payload -> 'result_id') NOT IN ('string','null')
       OR (p_payload -> 'result_id'<>'null'::jsonb AND p_payload ->> 'result_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
       OR pg_catalog.jsonb_typeof(p_payload -> 'failed_prediction_id') NOT IN ('string','null')
       OR (p_payload -> 'failed_prediction_id'<>'null'::jsonb AND p_payload ->> 'failed_prediction_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
       OR pg_catalog.jsonb_typeof(p_payload -> 'task_id') NOT IN ('string','null')
       OR (p_payload -> 'task_id'<>'null'::jsonb AND p_payload ->> 'task_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
       OR pg_catalog.jsonb_typeof(p_payload -> 'write_scope') NOT IN ('object','null')
       OR pg_catalog.jsonb_typeof(p_payload -> 'terminal_state') IS DISTINCT FROM 'string'
       OR p_payload ->> 'terminal_state' NOT IN ('succeeded','failed','abandoned')
       OR pg_catalog.jsonb_typeof(p_payload -> 'error_code') NOT IN ('string','null')
       OR pg_catalog.jsonb_typeof(p_payload -> 'context') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_payload -> 'outcome') IS DISTINCT FROM 'object'
       OR NOT aap_labeling_v1._prediction_retry_no_raw_data(p_payload -> 'context')
       OR NOT aap_labeling_v1._prediction_retry_no_raw_data(p_payload -> 'outcome') THEN
        RETURN aap_jobs_v1._prediction_retry_reply(NULL,'invalid_request');
    END IF;
    v_job_id := (p_payload ->> 'job_id')::uuid;
    v_status := p_payload ->> 'status';
    v_result_id := CASE WHEN p_payload -> 'result_id'='null'::jsonb THEN NULL ELSE (p_payload ->> 'result_id')::uuid END;
    v_failed_id := CASE WHEN p_payload -> 'failed_prediction_id'='null'::jsonb THEN NULL ELSE (p_payload ->> 'failed_prediction_id')::uuid END;
    v_task_id := CASE WHEN p_payload -> 'task_id'='null'::jsonb THEN NULL ELSE (p_payload ->> 'task_id')::uuid END;
    v_write_scope := p_payload -> 'write_scope';
    v_error := CASE WHEN p_payload -> 'error_code'='null'::jsonb THEN NULL ELSE p_payload ->> 'error_code' END;
    v_required_terminal_state := p_payload ->> 'terminal_state';
    v_context := p_payload -> 'context';
    v_outcome := p_payload -> 'outcome';
    IF (v_status='completed' AND (v_result_id IS NULL OR v_failed_id IS NULL OR v_task_id IS NULL
                                  OR v_write_scope IS NULL OR v_write_scope='null'::jsonb
                                  OR v_error IS NOT NULL OR v_required_terminal_state<>'succeeded'
                                  OR v_outcome ->> 'status' IS DISTINCT FROM 'succeeded'))
       OR (v_status='failed' AND (v_result_id IS NOT NULL OR v_failed_id IS NULL OR v_error IS NULL
                                  OR v_required_terminal_state<>'failed'
                                  OR v_outcome ->> 'status' IS DISTINCT FROM 'failed'))
       OR (v_status='cancelled' AND (v_result_id IS NOT NULL OR v_failed_id IS NULL OR v_error IS NULL
                                     OR v_required_terminal_state<>'abandoned'
                                     OR v_outcome ->> 'status' IS DISTINCT FROM 'cancelled'))
       OR (v_write_scope IS DISTINCT FROM 'null'::jsonb
           AND v_write_scope ->> 'operation' IS DISTINCT FROM 'prediction_retry') THEN
        RETURN aap_jobs_v1._prediction_retry_reply(NULL,'invalid_request');
    END IF;
    BEGIN
        SELECT * INTO v_job FROM public.async_jobs
        WHERE id=v_job_id FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_jobs_v1._prediction_retry_reply(NULL,'execution_busy');
    END;
    IF NOT FOUND THEN RETURN aap_jobs_v1._prediction_retry_reply(NULL,'job_not_writable'); END IF;
    IF v_job.kind IS DISTINCT FROM 'prediction_retry'
       OR v_job.user_id IS DISTINCT FROM (p_payload ->> 'actor_id')::uuid
       OR COALESCE(v_job.project_id,v_job.retained_project_id) IS DISTINCT FROM (p_payload ->> 'project_id')::uuid
       OR v_job.payload ->> 'prediction_retry_request_digest' IS DISTINCT FROM p_payload ->> 'request_digest' THEN
        RETURN aap_jobs_v1._prediction_retry_reply(NULL,'scope_mismatch');
    END IF;
    IF v_job.status NOT IN ('pending','running') THEN
        RETURN aap_jobs_v1._prediction_retry_reply(NULL,'job_not_writable');
    END IF;
    BEGIN
        SELECT * INTO v_execution FROM public.batch_result_executions
        WHERE job_id=v_job_id FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_jobs_v1._prediction_retry_reply(NULL,'execution_busy');
    END;
    IF NOT FOUND
       OR v_execution.actor_id IS DISTINCT FROM (p_payload ->> 'actor_id')::uuid
       OR v_execution.project_id IS DISTINCT FROM (p_payload ->> 'project_id')::uuid
       OR v_execution.request_digest IS DISTINCT FROM p_payload ->> 'request_digest'
       OR v_execution.generation IS DISTINCT FROM (p_payload ->> 'generation')::bigint
       OR v_execution.lease_token IS DISTINCT FROM (p_payload ->> 'lease_token')::uuid
       OR v_execution.lease_token IS NULL
       OR v_execution.lease_expires_at IS NULL
       OR v_execution.lease_expires_at<=pg_catalog.clock_timestamp()
       OR v_execution.state NOT IN ('preparing','running') THEN
        RETURN aap_jobs_v1._prediction_retry_reply(NULL,'lease_lost');
    END IF;
    IF NOT aap_labeling_v1._prediction_retry_terminal_matches(v_job_id,v_required_terminal_state,v_outcome) THEN
        RETURN aap_jobs_v1._prediction_retry_reply(NULL,'result_conflict');
    END IF;
    IF v_context ? 'pipeline_stages' THEN
        v_stages := v_context -> 'pipeline_stages';
    ELSE v_stages := '{}'::jsonb; END IF;
    IF v_context ? 'gpu_arbiter_failures' THEN
        v_gpu := v_context -> 'gpu_arbiter_failures';
    ELSE v_gpu := '[]'::jsonb; END IF;
    IF pg_catalog.jsonb_typeof(v_stages) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(v_gpu) IS DISTINCT FROM 'array' THEN
        RETURN aap_jobs_v1._prediction_retry_reply(NULL,'invalid_request');
    END IF;
    IF v_context ? 'total_cost' THEN
        IF pg_catalog.jsonb_typeof(v_context -> 'total_cost') IS DISTINCT FROM 'string'
           OR v_context ->> 'total_cost' !~ '^-?(0|[1-9][0-9]*)([.][0-9]+)?$' THEN
            RETURN aap_jobs_v1._prediction_retry_reply(NULL,'invalid_request');
        END IF;
        v_cost := (v_context ->> 'total_cost')::numeric;
    END IF;
    SELECT * INTO v_member FROM public.batch_result_members
    WHERE job_id=v_job_id AND ordinal=1 FOR UPDATE;
    IF FOUND THEN
        IF v_member.task_id IS DISTINCT FROM v_task_id
           OR NOT aap_labeling_v1._prediction_retry_member_scope_matches(
                v_job_id,v_member.task_id,v_member.write_scope
           ) THEN
            RETURN aap_jobs_v1._prediction_retry_reply(NULL,'scope_mismatch');
        END IF;
        IF v_status='completed' THEN
            IF v_member.state IS DISTINCT FROM 'ready'
               OR v_member.result_id IS DISTINCT FROM v_result_id THEN
                RETURN aap_jobs_v1._prediction_retry_reply(NULL,'result_not_ready');
            END IF;
        ELSIF v_member.state NOT IN ('planned','prepared','capturing','captured','ready') THEN
            RETURN aap_jobs_v1._prediction_retry_reply(NULL,'result_conflict');
        END IF;
        UPDATE public.batch_result_members
        SET state=CASE WHEN v_status='completed' THEN 'applied'
                       WHEN v_status='failed' THEN 'failed' ELSE 'skipped' END,
            failed_prediction_id=CASE WHEN v_status='failed' THEN v_failed_id ELSE NULL END,
            error_code=CASE WHEN v_status IN ('failed','cancelled') THEN v_error ELSE NULL END,
            updated_at=pg_catalog.clock_timestamp()
        WHERE job_id=v_job_id AND ordinal=1;
    ELSIF v_write_scope='null'::jsonb
       AND NOT aap_labeling_v1._prediction_retry_member_scope_matches(v_job_id,v_task_id,'null'::jsonb) THEN
        RETURN aap_jobs_v1._prediction_retry_reply(NULL,'scope_mismatch');
    ELSIF v_status='completed' OR v_write_scope IS DISTINCT FROM 'null'::jsonb THEN
        RETURN aap_jobs_v1._prediction_retry_reply(NULL,'result_not_ready');
    END IF;
    SELECT count(*)::bigint INTO v_existing_members FROM public.batch_result_members WHERE job_id=v_job_id;
    IF v_execution.total IS DISTINCT FROM v_existing_members
       OR v_execution.total NOT IN (0,1) THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='result_conflict';
    END IF;
    IF v_status='failed' THEN
        v_skipped_ids := pg_catalog.jsonb_build_array(v_failed_id);
    END IF;
    UPDATE public.batch_result_executions
    SET state=v_status,
        next_ordinal=CASE WHEN v_existing_members=1 THEN 2 ELSE next_ordinal END,
        success_count=success_count+CASE WHEN v_status='completed' THEN 1 ELSE 0 END,
        failed_count=failed_count+CASE WHEN v_status='failed' THEN 1 ELSE 0 END,
        skipped_count=skipped_count+CASE WHEN v_status='cancelled' THEN 1 ELSE 0 END,
        total_cost=(total_cost::numeric+v_cost)::text,
        pipeline_stages=aap_jobs_v1._batch_result_merge_stage_counts(pipeline_stages,v_stages),
        gpu_arbiter_failures=aap_jobs_v1._batch_result_merge_gpu_failures(gpu_arbiter_failures,v_gpu),
        worker_id=NULL,lease_token=NULL,lease_expires_at=NULL,
        error_code=CASE WHEN v_status IN ('failed','cancelled') THEN v_error ELSE NULL END,
        result_extra='{}'::jsonb,updated_at=pg_catalog.clock_timestamp()
    WHERE job_id=v_job_id RETURNING * INTO v_execution;
    v_result := COALESCE(v_job.result,'{}'::jsonb) || pg_catalog.jsonb_build_object(
        'success_count',v_execution.success_count,
        'failed_count',v_execution.failed_count,
        'skipped_count',v_execution.skipped_count,
        'failed_prediction_ids',v_skipped_ids,
        'failed_prediction_ids_truncated',false,
        'batch_result_scope',pg_catalog.jsonb_build_object(
            'job_id',v_execution.job_id,'actor_id',v_execution.actor_id,
            'project_id',v_execution.project_id,'request_digest',v_execution.request_digest
        ),
        'total_cost',COALESCE(v_context -> 'total_cost',pg_catalog.to_jsonb(v_execution.total_cost)),
        'pipeline_stages',v_execution.pipeline_stages,
        'gpu_arbiter_failures',v_execution.gpu_arbiter_failures
    ) || v_outcome;
    UPDATE public.async_jobs
    SET status=v_status,result=v_result,
        error_message=CASE WHEN v_status IN ('failed','cancelled') THEN v_outcome ->> 'reason' ELSE error_message END,
        progress_pct=100,completed_at=pg_catalog.clock_timestamp(),updated_at=pg_catalog.clock_timestamp()
    WHERE id=v_job_id;
    RETURN aap_jobs_v1._prediction_retry_reply(
        pg_catalog.jsonb_build_object(
            'progress',aap_jobs_v1._batch_result_progress(v_job_id),
            'job_id',v_job_id,'status',v_status
        ),NULL
    );
EXCEPTION WHEN SQLSTATE 'P0001' THEN
    RETURN aap_jobs_v1._prediction_retry_reply(NULL,CASE
        WHEN SQLERRM IN ('invalid_request','invalid_response','not_found','permission_denied',
            'account_not_active','already_pending','retry_limit','dismissed','scope_mismatch',
            'execution_busy','lease_lost','job_not_writable','result_conflict','result_not_ready',
            'storage_unavailable','content_deleting','content_epoch_changed') THEN SQLERRM
        ELSE 'invalid_response' END);
WHEN OTHERS THEN
    RETURN aap_jobs_v1._prediction_retry_reply(NULL,'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._finish_prediction_retry_job(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.finish_prediction_retry_write(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_attempt public.prediction_retry_attempts%ROWTYPE;
    v_failed public.failed_predictions%ROWTYPE;
    v_member jsonb;
    v_bundle public.batch_result_bundles%ROWTYPE;
    v_prepared public.batch_result_payloads%ROWTYPE;
    v_raw public.batch_result_payloads%ROWTYPE;
    v_scope jsonb;
    v_locked jsonb;
    v_read jsonb;
    v_job_id uuid;
    v_result_id uuid;
    v_digest text;
    v_receipt jsonb;
    v_outcome jsonb;
    v_terminal_digest text;
    v_jobs jsonb;
    v_prepared_count bigint;
    v_raw_count bigint;
    v_meta_count bigint;
    v_prediction_count bigint;
    v_deleted bigint;
    v_retry_job_id uuid;
    v_progress jsonb;
BEGIN
    IF p_payload IS NULL OR pg_catalog.octet_length(p_payload::text)>2097152
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['job_id','actor_id','project_id','request_digest','generation','lease_token','result_id','prepared_digest']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY['job_id','actor_id','project_id','request_digest','generation','lease_token','result_id','prepared_digest'])
       OR NOT aap_labeling_v1._batch_result_scope_valid(p_payload - ARRAY['result_id','prepared_digest'],true)
       OR pg_catalog.jsonb_typeof(p_payload -> 'result_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'result_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'prepared_digest') IS DISTINCT FROM 'string'
       OR p_payload ->> 'prepared_digest' !~ '^[0-9a-f]{64}$' THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'invalid_request');
    END IF;
    v_scope := p_payload - ARRAY['result_id','prepared_digest','generation','lease_token'];
    v_job_id := (p_payload ->> 'job_id')::uuid;
    v_result_id := (p_payload ->> 'result_id')::uuid;
    v_digest := p_payload ->> 'prepared_digest';
    v_terminal_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
        pg_catalog.jsonb_build_object('kind','succeeded','scope',v_scope,
            'result_id',v_result_id,'prepared_digest',v_digest)::text,'UTF8'
    )),'hex');
    SELECT * INTO v_attempt FROM public.prediction_retry_attempts
    WHERE job_id=v_job_id FOR UPDATE;
    IF NOT FOUND
       OR v_attempt.actor_id IS DISTINCT FROM (v_scope ->> 'actor_id')::uuid
       OR v_attempt.project_id IS DISTINCT FROM (v_scope ->> 'project_id')::uuid
       OR v_attempt.request_digest IS DISTINCT FROM v_scope ->> 'request_digest' THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'scope_mismatch');
    END IF;
    IF v_attempt.state='succeeded' THEN
        IF v_attempt.terminal_digest IS DISTINCT FROM v_terminal_digest
           OR v_attempt.outcome ->> 'result_id' IS DISTINCT FROM v_result_id::text
           OR v_attempt.outcome ->> 'prepared_digest' IS DISTINCT FROM v_digest THEN
            RETURN aap_labeling_v1._prediction_retry_reply(NULL,'result_conflict');
        END IF;
        v_read := aap_jobs_v1.read_batch_result_execution(
            v_scope || pg_catalog.jsonb_build_object('after_ordinal',0,'limit',100)
        );
        IF v_read -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN aap_labeling_v1._prediction_retry_reply(NULL,aap_labeling_v1._prediction_retry_error_code(COALESCE(v_read #>> '{error,code}','scope_mismatch')));
        END IF;
        RETURN aap_labeling_v1._prediction_retry_reply(
            pg_catalog.jsonb_build_object(
                'outcome',v_attempt.outcome,'receipt',v_attempt.outcome -> 'receipt',
                'progress',v_read #> '{data,progress}'
            ),NULL
        );
    END IF;
    IF v_attempt.state NOT IN ('accepted','applying') OR v_attempt.write_scope IS NULL
       OR NOT aap_labeling_v1._prediction_retry_attempt_scope_valid(v_scope || pg_catalog.jsonb_build_object(
            'generation',(p_payload ->> 'generation')::bigint,'lease_token',p_payload ->> 'lease_token'
       ),false) THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'lease_lost');
    END IF;
    v_locked := aap_jobs_v1.lock_prediction_retry_finish_scope(
        v_scope || pg_catalog.jsonb_build_object(
            'generation',(p_payload ->> 'generation')::bigint,'lease_token',p_payload ->> 'lease_token'
        )
    );
    IF v_locked -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,aap_labeling_v1._prediction_retry_error_code(COALESCE(v_locked #>> '{error,code}','lease_lost')));
    END IF;
    v_member := v_locked #> '{data,member}';
    IF v_member IS NULL OR v_member='null'::jsonb
       OR v_member ->> 'task_id' IS DISTINCT FROM v_attempt.task_id::text
       OR v_member -> 'write_scope' IS DISTINCT FROM v_attempt.write_scope
       OR v_member ->> 'state' IS DISTINCT FROM 'ready'
       OR v_member ->> 'result_id' IS DISTINCT FROM v_result_id::text THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'result_not_ready');
    END IF;
    SELECT * INTO v_bundle FROM public.batch_result_bundles
    WHERE result_id=v_result_id AND job_id=v_job_id AND ordinal=1 FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_bundle.task_id IS DISTINCT FROM v_attempt.task_id
       OR v_bundle.state IS DISTINCT FROM 'ready'
       OR v_bundle.result_count IS DISTINCT FROM 1
       OR NOT v_bundle.raw_complete OR NOT v_bundle.prepared_complete
       OR v_bundle.prepared_digest IS DISTINCT FROM v_digest
       OR v_bundle.receipt_count<>0 THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'result_not_ready');
    END IF;
    SELECT count(*)::bigint INTO v_prepared_count
    FROM public.batch_result_payloads AS payload
    WHERE payload.result_id=v_result_id AND payload.job_id=v_job_id
      AND payload.ordinal=1 AND payload.stage='prepared'
      AND payload.request_index=0 AND payload.complete AND payload.body IS NOT NULL;
    SELECT count(*)::bigint INTO v_raw_count
    FROM public.batch_result_payloads AS payload
    WHERE payload.result_id=v_result_id AND payload.job_id=v_job_id
      AND payload.ordinal=1 AND payload.stage='raw'
      AND payload.request_index=0 AND payload.complete AND payload.body IS NOT NULL;
    IF v_prepared_count<>1 OR v_raw_count<>1
       OR (SELECT count(*)::bigint FROM public.batch_result_payloads AS payload
           WHERE payload.result_id=v_result_id AND payload.stage='prepared')<>1
       OR (SELECT count(*)::bigint FROM public.batch_result_payloads AS payload
           WHERE payload.result_id=v_result_id AND payload.stage='raw')<>1 THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'result_conflict');
    END IF;
    SELECT * INTO v_prepared FROM public.batch_result_payloads
    WHERE result_id=v_result_id AND stage='prepared' AND request_index=0 FOR UPDATE NOWAIT;
    SELECT * INTO v_raw FROM public.batch_result_payloads
    WHERE result_id=v_result_id AND stage='raw' AND request_index=0 FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_raw.body IS NULL OR v_prepared.body IS NULL
       OR v_raw.prediction_id IS DISTINCT FROM v_prepared.prediction_id
       OR v_raw.prediction_created_at IS DISTINCT FROM v_prepared.prediction_created_at
       OR v_prepared.tool_unit_id IS NULL
       OR v_prepared.body -> 'write_scope' IS DISTINCT FROM v_attempt.write_scope
       OR v_prepared.body ->> 'task_id' IS DISTINCT FROM v_attempt.task_id::text
       OR v_prepared.body ->> 'project_id' IS DISTINCT FROM v_attempt.project_id::text
       OR NOT aap_labeling_v1._batch_result_prediction_matches(v_prepared,v_prepared.body) THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'result_conflict');
    END IF;
    SELECT count(*)::bigint INTO v_meta_count FROM public.prediction_metas AS meta
    WHERE meta.prediction_id=v_prepared.prediction_id
      AND meta.prediction_created_at=v_prepared.prediction_created_at
      AND meta.failed_prediction_id IS NULL;
    IF v_meta_count<>1 THEN RETURN aap_labeling_v1._prediction_retry_reply(NULL,'result_conflict'); END IF;
    SELECT count(*)::bigint INTO v_prediction_count FROM public.predictions AS prediction
    WHERE prediction.id=v_prepared.prediction_id
      AND prediction.created_at=v_prepared.prediction_created_at;
    IF v_prediction_count<>1 THEN RETURN aap_labeling_v1._prediction_retry_reply(NULL,'result_conflict'); END IF;
    v_receipt := pg_catalog.jsonb_build_object(
        'id',v_prepared.prediction_id,'created_at',v_prepared.prediction_created_at,
        'tool_unit_id',v_prepared.tool_unit_id
    );
    v_outcome := pg_catalog.jsonb_build_object(
        'status','succeeded','failed_id',v_attempt.failed_prediction_id,
        'prediction_id',v_prepared.prediction_id,'result_id',v_result_id,
        'prepared_digest',v_digest,'receipt',v_receipt
    );
    BEGIN
        SELECT * INTO v_failed FROM public.failed_predictions AS failed
        WHERE failed.id=v_attempt.failed_prediction_id FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'execution_busy');
    END;
    IF NOT FOUND THEN RETURN aap_labeling_v1._prediction_retry_reply(NULL,'not_found'); END IF;
    IF v_failed.project_id IS DISTINCT FROM v_attempt.project_id
       OR v_failed.task_id IS DISTINCT FROM v_attempt.task_id
       OR v_failed.ml_backend_id IS DISTINCT FROM v_attempt.ml_backend_id
       OR v_failed.ml_backend_pool_id IS DISTINCT FROM v_attempt.ml_backend_pool_id
       OR v_failed.retry_count IS DISTINCT FROM v_attempt.retry_count_at_admission
       OR v_failed.dismissed_at IS NOT NULL
       OR v_failed.extra ->> 'retry_job_id' IS DISTINCT FROM v_job_id::text
       OR v_failed.extra -> 'retry_pending' IS DISTINCT FROM 'true'::jsonb
       OR (v_failed.extra - ARRAY['retry_pending','retry_job_id'])
            IS DISTINCT FROM (v_attempt.source_extra - ARRAY['retry_pending','retry_job_id']) THEN
        RETURN aap_labeling_v1._prediction_retry_reply(
            NULL,CASE WHEN v_failed.dismissed_at IS NOT NULL THEN 'dismissed' ELSE 'result_conflict' END
        );
    END IF;
    v_attempt.terminal_digest := v_terminal_digest;
    UPDATE public.prediction_retry_attempts
    SET state='succeeded',outcome=v_outcome,terminal_digest=v_terminal_digest,
        updated_at=pg_catalog.clock_timestamp()
    WHERE job_id=v_job_id RETURNING * INTO v_attempt;
    DELETE FROM public.prediction_metas AS meta WHERE meta.failed_prediction_id=v_attempt.failed_prediction_id;
    DELETE FROM public.failed_predictions AS failed WHERE failed.id=v_attempt.failed_prediction_id;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    IF v_deleted<>1 THEN RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='result_conflict'; END IF;
    UPDATE public.batch_result_payloads
    SET receipt=v_receipt,body=NULL,roots_active=false,updated_at=pg_catalog.clock_timestamp()
    WHERE result_id=v_result_id AND stage IN ('raw','prepared');
    DELETE FROM public.batch_result_parts AS part
    WHERE part.payload_id IN (
        SELECT payload.payload_id FROM public.batch_result_payloads AS payload WHERE payload.result_id=v_result_id
    );
    UPDATE public.batch_result_bundles
    SET state='applied',receipt_count=1,applied_at=pg_catalog.clock_timestamp(),updated_at=pg_catalog.clock_timestamp()
    WHERE result_id=v_result_id;
    v_jobs := aap_jobs_v1._finish_prediction_retry_job(
        v_scope || pg_catalog.jsonb_build_object(
            'generation',(p_payload ->> 'generation')::bigint,'lease_token',p_payload ->> 'lease_token',
            'status','completed','result_id',v_result_id,'failed_prediction_id',v_attempt.failed_prediction_id,
            'task_id',v_attempt.task_id,'write_scope',v_attempt.write_scope,
            'terminal_state','succeeded','error_code',NULL,'context',v_bundle.context,'outcome',v_outcome
        )
    );
    IF v_jobs -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE=COALESCE(v_jobs #>> '{error,code}','invalid_response');
    END IF;
    RETURN aap_labeling_v1._prediction_retry_reply(
        pg_catalog.jsonb_build_object(
            'outcome',v_outcome,'receipt',v_receipt,'progress',v_jobs #> '{data,progress}'
        ),NULL
    );
EXCEPTION WHEN SQLSTATE 'P0001' THEN
    RETURN aap_labeling_v1._prediction_retry_reply(NULL,CASE
        WHEN SQLERRM IN ('invalid_request','invalid_response','not_found','permission_denied',
            'account_not_active','already_pending','retry_limit','dismissed','scope_mismatch',
            'execution_busy','lease_lost','job_not_writable','result_conflict','result_not_ready',
            'storage_unavailable','content_deleting','content_epoch_changed') THEN SQLERRM
        ELSE 'invalid_response' END);
WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_labeling_v1._prediction_retry_reply(NULL,'execution_busy');
WHEN OTHERS THEN
    RETURN aap_labeling_v1._prediction_retry_reply(NULL,'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.finish_prediction_retry_write(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.finish_prediction_retry_failure(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_attempt public.prediction_retry_attempts%ROWTYPE;
    v_failed public.failed_predictions%ROWTYPE;
    v_member jsonb;
    v_bundle public.batch_result_bundles%ROWTYPE;
    v_scope jsonb;
    v_locked jsonb;
    v_read jsonb;
    v_job_id uuid;
    v_error text;
    v_reason text;
    v_gpu jsonb;
    v_context jsonb;
    v_terminal_digest text;
    v_outcome jsonb;
    v_jobs jsonb;
    v_retry_count integer;
    v_extra jsonb;
    v_has_bundle boolean := false;
    v_abandon boolean;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload)='object' AND NOT (p_payload ? 'abandon') THEN
        p_payload := p_payload || pg_catalog.jsonb_build_object('abandon',false);
    END IF;
    IF p_payload IS NULL OR pg_catalog.octet_length(p_payload::text)>2097152
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY[
            'job_id','actor_id','project_id','request_digest','generation','lease_token',
            'error_code','reason','gpu_arbiter_error','context','abandon'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY[
            'job_id','actor_id','project_id','request_digest','generation','lease_token',
            'error_code','reason','gpu_arbiter_error','context','abandon'
       ])
       OR NOT aap_labeling_v1._batch_result_scope_valid(p_payload - ARRAY[
            'error_code','reason','gpu_arbiter_error','context','abandon'
       ],true)
       OR pg_catalog.jsonb_typeof(p_payload -> 'error_code') IS DISTINCT FROM 'string'
       OR pg_catalog.octet_length(p_payload ->> 'error_code') NOT BETWEEN 1 AND 256
       OR pg_catalog.jsonb_typeof(p_payload -> 'reason') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_payload ->> 'reason')>200
       OR pg_catalog.jsonb_typeof(p_payload -> 'gpu_arbiter_error') NOT IN ('object','null')
       OR pg_catalog.jsonb_typeof(p_payload -> 'context') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_payload -> 'abandon') IS DISTINCT FROM 'boolean'
       OR NOT aap_labeling_v1._prediction_retry_no_raw_data(p_payload -> 'gpu_arbiter_error')
       OR NOT aap_labeling_v1._prediction_retry_no_raw_data(p_payload -> 'context') THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'invalid_request');
    END IF;
    v_scope := p_payload - ARRAY['error_code','reason','gpu_arbiter_error','context','abandon','generation','lease_token'];
    v_job_id := (p_payload ->> 'job_id')::uuid;
    v_error := p_payload ->> 'error_code';
    v_reason := p_payload ->> 'reason';
    v_gpu := p_payload -> 'gpu_arbiter_error';
    v_context := p_payload -> 'context';
    v_abandon := (p_payload ->> 'abandon')::boolean;
    v_terminal_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
        pg_catalog.jsonb_build_object('kind',CASE WHEN v_abandon THEN 'abandoned' ELSE 'failed' END,
            'scope',v_scope,'error_code',v_error,'reason',v_reason,
            'gpu_arbiter_error',v_gpu,'context',v_context)::text,'UTF8'
    )),'hex');
    SELECT * INTO v_attempt FROM public.prediction_retry_attempts
    WHERE job_id=v_job_id FOR UPDATE;
    IF NOT FOUND
       OR v_attempt.actor_id IS DISTINCT FROM (v_scope ->> 'actor_id')::uuid
       OR v_attempt.project_id IS DISTINCT FROM (v_scope ->> 'project_id')::uuid
       OR v_attempt.request_digest IS DISTINCT FROM v_scope ->> 'request_digest' THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'scope_mismatch');
    END IF;
    IF v_attempt.state IN ('failed','abandoned') THEN
        IF v_attempt.terminal_digest IS DISTINCT FROM v_terminal_digest THEN
            RETURN aap_labeling_v1._prediction_retry_reply(NULL,'result_conflict');
        END IF;
        v_read := aap_jobs_v1.read_batch_result_execution(
            v_scope || pg_catalog.jsonb_build_object('after_ordinal',0,'limit',100)
        );
        IF v_read -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN aap_labeling_v1._prediction_retry_reply(NULL,aap_labeling_v1._prediction_retry_error_code(COALESCE(v_read #>> '{error,code}','scope_mismatch')));
        END IF;
        RETURN aap_labeling_v1._prediction_retry_reply(
            pg_catalog.jsonb_build_object('outcome',v_attempt.outcome,'progress',v_read #> '{data,progress}'),NULL
        );
    END IF;
    IF v_attempt.state NOT IN ('accepted','applying','failing')
       OR NOT aap_labeling_v1._prediction_retry_attempt_scope_valid(v_scope || pg_catalog.jsonb_build_object(
            'generation',(p_payload ->> 'generation')::bigint,'lease_token',p_payload ->> 'lease_token'
       ),false) THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'lease_lost');
    END IF;
    v_locked := aap_jobs_v1.lock_prediction_retry_finish_scope(
        v_scope || pg_catalog.jsonb_build_object(
            'generation',(p_payload ->> 'generation')::bigint,'lease_token',p_payload ->> 'lease_token'
        )
    );
    IF v_locked -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,aap_labeling_v1._prediction_retry_error_code(COALESCE(v_locked #>> '{error,code}','lease_lost')));
    END IF;
    v_member := v_locked #> '{data,member}';
    IF v_member IS NOT NULL AND v_member<>'null'::jsonb THEN
        IF v_member ->> 'task_id' IS DISTINCT FROM v_attempt.task_id::text
           OR v_member -> 'write_scope' IS DISTINCT FROM v_attempt.write_scope
           OR v_member ->> 'state' IN ('applied','failed','skipped') THEN
            RETURN aap_labeling_v1._prediction_retry_reply(NULL,'result_conflict');
        END IF;
        SELECT * INTO v_bundle FROM public.batch_result_bundles
        WHERE job_id=v_job_id AND ordinal=1 FOR UPDATE NOWAIT;
        IF FOUND THEN
            v_has_bundle := true;
            IF v_bundle.state IN ('applied','failed','abandoned') OR v_bundle.receipt_count>0 THEN
                RETURN aap_labeling_v1._prediction_retry_reply(NULL,'result_conflict');
            END IF;
        END IF;
    ELSIF v_attempt.write_scope IS NOT NULL THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'result_conflict');
    END IF;
    IF v_abandon THEN
        BEGIN
            SELECT * INTO v_failed FROM public.failed_predictions AS failed
            WHERE failed.id=v_attempt.failed_prediction_id FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_labeling_v1._prediction_retry_reply(NULL,'execution_busy');
        END;
        IF FOUND THEN
            UPDATE public.failed_predictions AS failed
            SET extra=failed.extra - ARRAY['retry_pending','retry_job_id']
            WHERE failed.id=v_attempt.failed_prediction_id
              AND failed.extra ->> 'retry_job_id'=v_job_id::text
              AND failed.extra -> 'retry_pending'='true'::jsonb;
        END IF;
        v_outcome := pg_catalog.jsonb_build_object(
            'status','cancelled','failed_id',v_attempt.failed_prediction_id,
            'reason',v_reason,'error_code',v_error,
            'retry_count',v_attempt.retry_count_at_admission
        );
        IF v_gpu IS NOT NULL AND v_gpu<>'null'::jsonb THEN
            v_outcome := v_outcome || pg_catalog.jsonb_build_object('gpu_arbiter_error',v_gpu);
        END IF;
        UPDATE public.prediction_retry_attempts
        SET state='abandoned',outcome=v_outcome,terminal_digest=v_terminal_digest,
            updated_at=pg_catalog.clock_timestamp()
        WHERE job_id=v_job_id RETURNING * INTO v_attempt;
        v_jobs := aap_jobs_v1._finish_prediction_retry_job(
            v_scope || pg_catalog.jsonb_build_object(
                'generation',(p_payload ->> 'generation')::bigint,'lease_token',p_payload ->> 'lease_token',
                'status','cancelled','result_id',NULL,'failed_prediction_id',v_attempt.failed_prediction_id,
                'task_id',v_attempt.task_id,'write_scope',v_attempt.write_scope,
                'terminal_state','abandoned','error_code',v_error,'context',v_context,'outcome',v_outcome
            )
        );
        IF v_jobs -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE=COALESCE(v_jobs #>> '{error,code}','invalid_response');
        END IF;
        RETURN aap_labeling_v1._prediction_retry_reply(
            pg_catalog.jsonb_build_object('outcome',v_outcome,'progress',v_jobs #> '{data,progress}'),NULL
        );
    END IF;
    BEGIN
        SELECT * INTO v_failed FROM public.failed_predictions AS failed
        WHERE failed.id=v_attempt.failed_prediction_id FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'execution_busy');
    END;
    IF NOT FOUND THEN RETURN aap_labeling_v1._prediction_retry_reply(NULL,'not_found'); END IF;
    IF v_failed.project_id IS DISTINCT FROM v_attempt.project_id
       OR v_failed.task_id IS DISTINCT FROM v_attempt.task_id
       OR v_failed.ml_backend_id IS DISTINCT FROM v_attempt.ml_backend_id
       OR v_failed.ml_backend_pool_id IS DISTINCT FROM v_attempt.ml_backend_pool_id
       OR v_failed.retry_count IS DISTINCT FROM v_attempt.retry_count_at_admission
       OR v_failed.dismissed_at IS NOT NULL
       OR v_failed.extra ->> 'retry_job_id' IS DISTINCT FROM v_job_id::text
       OR v_failed.extra -> 'retry_pending' IS DISTINCT FROM 'true'::jsonb
       OR (v_failed.extra - ARRAY['retry_pending','retry_job_id'])
            IS DISTINCT FROM (v_attempt.source_extra - ARRAY['retry_pending','retry_job_id']) THEN
        RETURN aap_labeling_v1._prediction_retry_reply(
            NULL,CASE WHEN v_failed.dismissed_at IS NOT NULL THEN 'dismissed' ELSE 'result_conflict' END
        );
    END IF;
    IF v_failed.retry_count>=3 THEN
        RETURN aap_labeling_v1._prediction_retry_reply(NULL,'retry_limit');
    END IF;
    v_retry_count := v_failed.retry_count+1;
    v_extra := (v_failed.extra - ARRAY['retry_pending','retry_job_id']);
    IF v_gpu IS NOT NULL AND v_gpu<>'null'::jsonb THEN
        v_extra := v_extra || pg_catalog.jsonb_build_object('last_gpu_arbiter_error',v_gpu);
    END IF;
    v_outcome := pg_catalog.jsonb_build_object(
        'status','failed','failed_id',v_attempt.failed_prediction_id,
        'reason',v_reason,'error_code',v_error,
        'retry_count',v_retry_count
    );
    IF v_gpu IS NOT NULL AND v_gpu<>'null'::jsonb THEN
        v_outcome := v_outcome || pg_catalog.jsonb_build_object('gpu_arbiter_error',v_gpu);
    END IF;
    UPDATE public.prediction_retry_attempts
    SET state='failed',outcome=v_outcome,terminal_digest=v_terminal_digest,
        updated_at=pg_catalog.clock_timestamp()
    WHERE job_id=v_job_id RETURNING * INTO v_attempt;
    UPDATE public.failed_predictions AS failed
    SET retry_count=v_retry_count,last_retry_at=pg_catalog.clock_timestamp(),extra=v_extra
    WHERE failed.id=v_attempt.failed_prediction_id;
    IF v_has_bundle THEN
        UPDATE public.batch_result_bundles
        SET state='failed',updated_at=pg_catalog.clock_timestamp()
        WHERE result_id=v_bundle.result_id;
        UPDATE public.batch_result_payloads
        SET body=NULL,roots_active=false,updated_at=pg_catalog.clock_timestamp()
        WHERE result_id=v_bundle.result_id;
        DELETE FROM public.batch_result_parts AS part
        WHERE part.payload_id IN (
            SELECT payload.payload_id FROM public.batch_result_payloads AS payload
            WHERE payload.result_id=v_bundle.result_id
        );
    END IF;
    v_jobs := aap_jobs_v1._finish_prediction_retry_job(
        v_scope || pg_catalog.jsonb_build_object(
            'generation',(p_payload ->> 'generation')::bigint,'lease_token',p_payload ->> 'lease_token',
            'status','failed','result_id',NULL,'failed_prediction_id',v_attempt.failed_prediction_id,
            'task_id',v_attempt.task_id,'write_scope',v_attempt.write_scope,
            'terminal_state','failed',
            'error_code',v_error,'context',v_context,'outcome',v_outcome
        )
    );
    IF v_jobs -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE=COALESCE(v_jobs #>> '{error,code}','invalid_response');
    END IF;
    RETURN aap_labeling_v1._prediction_retry_reply(
        pg_catalog.jsonb_build_object('outcome',v_outcome,'progress',v_jobs #> '{data,progress}'),NULL
    );
EXCEPTION WHEN SQLSTATE 'P0001' THEN
    RETURN aap_labeling_v1._prediction_retry_reply(NULL,CASE
        WHEN SQLERRM IN ('invalid_request','invalid_response','not_found','permission_denied',
            'account_not_active','already_pending','retry_limit','dismissed','scope_mismatch',
            'execution_busy','lease_lost','job_not_writable','result_conflict','result_not_ready',
            'storage_unavailable','content_deleting','content_epoch_changed') THEN SQLERRM
        ELSE 'invalid_response' END);
WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_labeling_v1._prediction_retry_reply(NULL,'execution_busy');
WHEN OTHERS THEN
    RETURN aap_labeling_v1._prediction_retry_reply(NULL,'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.finish_prediction_retry_failure(jsonb) FROM PUBLIC;
-- aap-statement-break
