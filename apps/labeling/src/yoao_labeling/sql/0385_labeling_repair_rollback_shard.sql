-- Forward replacement of frozen 0353 bytes for rollback shard checkpoints.
--
-- `record_rollback_shard` records one completed rollback checkpoint under
-- `result_json.rollback.shards` for a batch whose current rollback job is
-- running (`rolling_back`). It never commits: the Labeling mutation executed by
-- the caller in the same transaction and this checkpoint share the caller's
-- unit of work, so a rollback of the caller transaction removes the checkpoint.
--
-- The shard must belong to the plan; a completed checkpoint is preserved when a
-- conflicting replay arrives, while an exact replay is idempotent.
--
-- `read_batch_for_source` returns the same row shape as `read_batch_by_id`.
-- `lock=false` is an ordinary read; `lock=true` takes `FOR SHARE NOWAIT`, so a
-- Mask repair source check never queues behind a worker's batch UPDATE: it
-- either shares the row with another source read or fails immediately with
-- SQLSTATE 55P03. A failed rollback may attach a replacement job without
-- discarding completed shard checkpoints.
CREATE OR REPLACE FUNCTION aap_labeling_v1.mask_repair_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_operation text;
    v_keys text[];
    v_expected_keys text[];
    v_field text;
    v_id uuid;
    v_batch public.mask_repair_batches%ROWTYPE;
    v_scope public.mask_review_scopes%ROWTYPE;
    v_segment public.video_segments%ROWTYPE;
    v_data jsonb;
    v_rows jsonb := '[]'::jsonb;
    v_actor_id uuid;
    v_lock_reply jsonb;
    v_count integer := 0;
    v_task public.tasks%ROWTYPE;
    v_annotation public.annotations%ROWTYPE;
    v_issue public.mask_qc_issues%ROWTYPE;
    v_tracker_job public.video_tracker_jobs%ROWTYPE;
    v_existing jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-quality-data/v1'
       OR pg_catalog.jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Mask repair request' USING ERRCODE = '22023';
    END IF;
    v_operation := p_request ->> 'operation';
    CASE v_operation
        WHEN 'create_batch' THEN v_expected_keys := ARRAY['schema','operation','batch_id','project_id','requested_by_id','token_hash','plan_digest','request_json','plan_json','result_json','receipt_expires_at'];
        WHEN 'read_batch_by_id' THEN v_expected_keys := ARRAY['schema','operation','batch_id','lock'];
        WHEN 'read_batch_for_source' THEN v_expected_keys := ARRAY['schema','operation','batch_id','lock'];
        WHEN 'read_batch_by_token' THEN v_expected_keys := ARRAY['schema','operation','token_hash','lock'];
        WHEN 'read_batch_by_job' THEN v_expected_keys := ARRAY['schema','operation','job_id','terminal_kind','lock'];
        WHEN 'attach_job' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id'];
        WHEN 'attach_rollback_job' THEN v_expected_keys := ARRAY['schema','operation','batch_id','rollback_job_id'];
        WHEN 'resume_batch' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','actor_id'];
        WHEN 'begin_execution' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','at'];
        WHEN 'begin_rollback' THEN v_expected_keys := ARRAY['schema','operation','batch_id','rollback_job_id'];
        WHEN 'record_shard' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','shard_id','result_value','rollback_expires_at'];
        WHEN 'record_rollback_shard' THEN v_expected_keys := ARRAY['schema','operation','batch_id','rollback_job_id','shard_id','result_value'];
        WHEN 'record_candidate' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','item_index','result_value'];
        WHEN 'record_shard_failure' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','shard_id','task_id','error_message'];
        WHEN 'finish_execution' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','status','completed_at'];
        WHEN 'finish_rollback' THEN v_expected_keys := ARRAY['schema','operation','batch_id','rollback_job_id','rolled_back_at','result_json'];
        WHEN 'fail_rollback' THEN v_expected_keys := ARRAY['schema','operation','batch_id','rollback_job_id','error_message','result_json','failed_at'];
        WHEN 'fail_dispatch' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','error_message'];
        WHEN 'fail_rollback_dispatch' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','error_message'];
        WHEN 'cancel_pending' THEN v_expected_keys := ARRAY['schema','operation','batch_id','status','completed_at'];
        WHEN 'reconcile_terminal' THEN v_expected_keys := ARRAY['schema','operation','job_id','terminal_kind','terminal_cancelled','status','completed_at'];
        WHEN 'insert_review_scope' THEN v_expected_keys := ARRAY['schema','operation','scope_id','project_id','task_id','annotation_id','qc_issue_id','source_job_id','expected_segment_id','reviewer_id','source_annotation_version','result_annotation_version','source_job_revision','frame_start','frame_end','region_mask_ref','region_digest','candidate_digest','decision'];
        WHEN 'read_review_scopes' THEN v_expected_keys := ARRAY['schema','operation','annotation_id','result_annotation_version','frame_start','frame_end','lock'];
        WHEN 'read_repair_targets' THEN v_expected_keys := ARRAY['schema','operation','issue_id','include_related'];
        WHEN 'read_video_segment' THEN v_expected_keys := ARRAY['schema','operation','dataset_item_id','frame_index','requested_id'];
        ELSE RAISE EXCEPTION 'Invalid Mask repair operation' USING ERRCODE = '22023';
    END CASE;
    IF NOT aap_labeling_v1._quality_exact_keys(p_request, v_expected_keys) THEN
        RAISE EXCEPTION 'Invalid Mask repair request fields' USING ERRCODE = '22023';
    END IF;

    IF v_operation = 'create_batch' THEN
        FOREACH v_field IN ARRAY ARRAY['batch_id','project_id','requested_by_id'] LOOP
            IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
               OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                RAISE EXCEPTION 'Invalid Mask repair identity' USING ERRCODE = '22023';
            END IF;
        END LOOP;
        IF pg_catalog.jsonb_typeof(p_request -> 'token_hash') IS DISTINCT FROM 'string'
           OR p_request ->> 'token_hash' !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'plan_digest') IS DISTINCT FROM 'string'
           OR p_request ->> 'plan_digest' !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'request_json') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'plan_json') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'result_json') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'receipt_expires_at') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid Mask repair plan' USING ERRCODE = '22023';
        END IF;
        v_actor_id := (p_request ->> 'requested_by_id')::uuid;
        v_lock_reply := aap_identity_v1.lock_active_actor(v_actor_id);
        IF v_lock_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('schema','aap-labeling-quality-data/v1','ok',true,
                'request',p_request,'data',pg_catalog.jsonb_build_object('row',NULL,'reason','actor_inactive'));
        END IF;
        INSERT INTO public.mask_repair_batches(
            id,project_id,requested_by_id,token_hash,status,plan_digest,request_json,plan_json,result_json,receipt_expires_at
        ) VALUES (
            (p_request ->> 'batch_id')::uuid,(p_request ->> 'project_id')::uuid,v_actor_id,
            p_request ->> 'token_hash','planned',p_request ->> 'plan_digest',p_request -> 'request_json',
            p_request -> 'plan_json',p_request -> 'result_json',(p_request ->> 'receipt_expires_at')::timestamptz
        ) RETURNING public.mask_repair_batches.* INTO v_batch;
        v_data := pg_catalog.to_jsonb(v_batch);
    ELSIF v_operation IN ('read_batch_by_id','read_batch_for_source','read_batch_by_token','read_batch_by_job') THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'lock') IS DISTINCT FROM 'boolean' THEN RAISE EXCEPTION 'Invalid Mask repair lock' USING ERRCODE = '22023'; END IF;
        IF v_operation IN ('read_batch_by_id','read_batch_for_source') THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'batch_id') IS DISTINCT FROM 'string'
               OR p_request ->> 'batch_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask repair batch id' USING ERRCODE = '22023'; END IF;
            IF (p_request ->> 'lock')::boolean THEN
                IF v_operation='read_batch_for_source' THEN
                    SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.id=(p_request ->> 'batch_id')::uuid FOR SHARE NOWAIT;
                ELSE
                    SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.id=(p_request ->> 'batch_id')::uuid FOR UPDATE;
                END IF;
            ELSE
                SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.id=(p_request ->> 'batch_id')::uuid;
            END IF;
        ELSIF v_operation='read_batch_by_token' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'token_hash') IS DISTINCT FROM 'string'
               OR p_request ->> 'token_hash' !~ '^[0-9a-f]{64}$' THEN RAISE EXCEPTION 'Invalid Mask repair receipt' USING ERRCODE = '22023'; END IF;
            IF (p_request ->> 'lock')::boolean THEN
                SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.token_hash=p_request ->> 'token_hash' FOR UPDATE;
            ELSE
                SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.token_hash=p_request ->> 'token_hash';
            END IF;
        ELSE
            IF pg_catalog.jsonb_typeof(p_request -> 'job_id') IS DISTINCT FROM 'string'
               OR p_request ->> 'job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR pg_catalog.jsonb_typeof(p_request -> 'terminal_kind') IS DISTINCT FROM 'string'
               OR p_request ->> 'terminal_kind' NOT IN ('mask_repair','mask_repair_rollback') THEN RAISE EXCEPTION 'Invalid Mask repair job lookup' USING ERRCODE = '22023'; END IF;
            IF (p_request ->> 'lock')::boolean THEN
                IF p_request ->> 'terminal_kind'='mask_repair_rollback' THEN
                    SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.rollback_async_job_id=(p_request ->> 'job_id')::uuid FOR UPDATE;
                ELSE
                    SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.async_job_id=(p_request ->> 'job_id')::uuid FOR UPDATE;
                END IF;
            ELSE
                IF p_request ->> 'terminal_kind'='mask_repair_rollback' THEN
                    SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.rollback_async_job_id=(p_request ->> 'job_id')::uuid;
                ELSE
                    SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.async_job_id=(p_request ->> 'job_id')::uuid;
                END IF;
            END IF;
        END IF;
        IF FOUND THEN v_data := pg_catalog.to_jsonb(v_batch); ELSE v_data := NULL; END IF;
    ELSIF v_operation IN ('attach_job','attach_rollback_job','resume_batch','begin_execution','begin_rollback',
        'record_shard','record_rollback_shard','record_candidate','record_shard_failure','finish_execution','finish_rollback',
        'fail_rollback','fail_dispatch','fail_rollback_dispatch','cancel_pending','reconcile_terminal') THEN
        IF v_operation='reconcile_terminal' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'job_id') IS DISTINCT FROM 'string'
               OR p_request ->> 'job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR pg_catalog.jsonb_typeof(p_request -> 'terminal_kind') IS DISTINCT FROM 'string'
               OR p_request ->> 'terminal_kind' NOT IN ('mask_repair','mask_repair_rollback')
               OR pg_catalog.jsonb_typeof(p_request -> 'terminal_cancelled') IS DISTINCT FROM 'boolean'
               OR pg_catalog.jsonb_typeof(p_request -> 'status') IS DISTINCT FROM 'string'
               OR p_request ->> 'status' NOT IN ('cancelled','partial','failed','rollback_failed')
               OR pg_catalog.jsonb_typeof(p_request -> 'completed_at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair terminal state' USING ERRCODE = '22023'; END IF;
            IF p_request ->> 'terminal_kind'='mask_repair_rollback' THEN
                SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.rollback_async_job_id=(p_request ->> 'job_id')::uuid FOR UPDATE;
            ELSE
                SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.async_job_id=(p_request ->> 'job_id')::uuid FOR UPDATE;
            END IF;
            IF FOUND AND (
                (p_request ->> 'terminal_kind'='mask_repair_rollback' AND v_batch.status='rolling_back')
                OR (p_request ->> 'terminal_kind'='mask_repair' AND v_batch.status IN ('pending','running'))
            ) THEN
                UPDATE public.mask_repair_batches AS batch SET status=p_request ->> 'status',completed_at=(p_request ->> 'completed_at')::timestamptz,
                    updated_at=pg_catalog.transaction_timestamp() WHERE batch.id=v_batch.id RETURNING batch.* INTO v_batch;
                v_data := pg_catalog.to_jsonb(v_batch);
            ELSE v_data := NULL; END IF;
        ELSE
            IF pg_catalog.jsonb_typeof(p_request -> 'batch_id') IS DISTINCT FROM 'string'
               OR p_request ->> 'batch_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask repair batch id' USING ERRCODE = '22023'; END IF;
            v_id := (p_request ->> 'batch_id')::uuid;
            SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.id=v_id FOR UPDATE;
            IF NOT FOUND THEN v_data := NULL;
            ELSE
                CASE v_operation
                    WHEN 'attach_job' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask repair job id' USING ERRCODE = '22023'; END IF;
                        UPDATE public.mask_repair_batches AS batch SET async_job_id=(p_request ->> 'async_job_id')::uuid,status='pending',updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.status='planned' RETURNING batch.* INTO v_batch;
                    WHEN 'attach_rollback_job' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'rollback_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'rollback_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask repair rollback job id' USING ERRCODE = '22023'; END IF;
                        UPDATE public.mask_repair_batches AS batch SET rollback_async_job_id=(p_request ->> 'rollback_job_id')::uuid,status='rolling_back',updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.status IN ('completed','partial','rollback_failed') RETURNING batch.* INTO v_batch;
                    WHEN 'resume_batch' THEN
                        FOREACH v_field IN ARRAY ARRAY['async_job_id','actor_id'] LOOP
                            IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
                               OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask repair resume identity' USING ERRCODE = '22023'; END IF;
                        END LOOP;
                        v_actor_id := (p_request ->> 'actor_id')::uuid;
                        v_lock_reply := aap_identity_v1.lock_active_actor(v_actor_id);
                        IF v_lock_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN v_data := pg_catalog.jsonb_build_object('changed',false,'reason','actor_inactive');
                        ELSE
                            UPDATE public.mask_repair_batches AS batch SET async_job_id=(p_request ->> 'async_job_id')::uuid,requested_by_id=v_actor_id,status='pending',updated_at=pg_catalog.transaction_timestamp()
                            WHERE batch.id=v_id AND batch.status IN ('failed','partial') RETURNING batch.* INTO v_batch;
                            IF FOUND THEN v_data := pg_catalog.to_jsonb(v_batch); ELSE v_data := NULL; END IF;
                        END IF;
                    WHEN 'begin_execution' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair execution binding' USING ERRCODE = '22023'; END IF;
                        PERFORM (p_request ->> 'at')::timestamptz;
                        IF v_batch.async_job_id=(p_request ->> 'async_job_id')::uuid AND v_batch.status IN ('pending','running') THEN
                            UPDATE public.mask_repair_batches AS batch SET status='running',updated_at=pg_catalog.transaction_timestamp()
                            WHERE batch.id=v_id RETURNING batch.* INTO v_batch;
                        END IF;
                    WHEN 'begin_rollback' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'rollback_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'rollback_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask repair rollback binding' USING ERRCODE = '22023'; END IF;
                        IF v_batch.rollback_async_job_id IS DISTINCT FROM (p_request ->> 'rollback_job_id')::uuid THEN
                            v_data := pg_catalog.jsonb_build_object('changed',false);
                        END IF;
                    WHEN 'record_shard' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'shard_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'shard_id' !~ '^[0-9a-f]{24}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_value') IS DISTINCT FROM 'object'
                           OR pg_catalog.jsonb_typeof(p_request -> 'rollback_expires_at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair shard result' USING ERRCODE = '22023'; END IF;
                        IF v_batch.async_job_id=(p_request ->> 'async_job_id')::uuid THEN
                            UPDATE public.mask_repair_batches AS batch SET
                                result_json=COALESCE(batch.result_json,'{}'::jsonb) || pg_catalog.jsonb_build_object('shards',COALESCE(batch.result_json -> 'shards','{}'::jsonb) || pg_catalog.jsonb_build_object(p_request ->> 'shard_id',p_request -> 'result_value')),
                                rollback_expires_at=(p_request ->> 'rollback_expires_at')::timestamptz,updated_at=pg_catalog.transaction_timestamp()
                            WHERE batch.id=v_id RETURNING batch.* INTO v_batch;
                        ELSE v_data := pg_catalog.jsonb_build_object('changed',false); END IF;
                    WHEN 'record_rollback_shard' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'rollback_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'rollback_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'shard_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'shard_id' !~ '^[0-9a-f]{24}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_value') IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'Invalid Mask repair rollback shard' USING ERRCODE = '22023'; END IF;
                        IF NOT aap_labeling_v1._quality_exact_keys(p_request -> 'result_value', ARRAY['status','mutation_count','versions','completed_at'])
                           AND NOT aap_labeling_v1._quality_exact_keys(p_request -> 'result_value', ARRAY['status','mutation_count','versions','completed_at','annotation_facts']) THEN RAISE EXCEPTION 'Invalid Mask repair rollback checkpoint' USING ERRCODE = '22023'; END IF;
                        IF p_request -> 'result_value' ->> 'status' IS DISTINCT FROM 'completed'
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_value' -> 'mutation_count') IS DISTINCT FROM 'number'
                           OR p_request -> 'result_value' ->> 'mutation_count' !~ '^(0|[1-9][0-9]*)$'
                           OR (p_request -> 'result_value' ->> 'mutation_count')::bigint > 2147483647
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_value' -> 'versions') IS DISTINCT FROM 'object'
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_value' -> 'completed_at') IS DISTINCT FROM 'string'
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_value' -> 'annotation_facts') NOT IN ('object','null') THEN RAISE EXCEPTION 'Invalid Mask repair rollback checkpoint' USING ERRCODE = '22023'; END IF;
                        PERFORM (p_request -> 'result_value' ->> 'completed_at')::timestamptz;
                        IF v_batch.rollback_async_job_id IS DISTINCT FROM (p_request ->> 'rollback_job_id')::uuid
                           OR v_batch.status IS DISTINCT FROM 'rolling_back' THEN
                            v_data := pg_catalog.jsonb_build_object('changed',false);
                        ELSIF NOT EXISTS (
                            SELECT 1
                            FROM pg_catalog.jsonb_array_elements(COALESCE(v_batch.plan_json -> 'shards','[]'::jsonb)) AS plan_shard(value)
                            WHERE value ->> 'id' = p_request ->> 'shard_id'
                        ) THEN
                            RAISE EXCEPTION 'Invalid Mask repair rollback shard plan' USING ERRCODE = '22023';
                        ELSE
                            v_existing := v_batch.result_json -> 'rollback' -> 'shards' -> (p_request ->> 'shard_id');
                            IF (v_existing ->> 'status') = 'completed' THEN
                                IF v_existing IS NOT DISTINCT FROM (p_request -> 'result_value') THEN
                                    v_data := pg_catalog.to_jsonb(v_batch);
                                ELSE
                                    v_data := pg_catalog.jsonb_build_object('changed',false);
                                END IF;
                            ELSE
                                UPDATE public.mask_repair_batches AS batch SET
                                    result_json=COALESCE(batch.result_json,'{}'::jsonb) || pg_catalog.jsonb_build_object('rollback',
                                        COALESCE(batch.result_json -> 'rollback','{}'::jsonb) || pg_catalog.jsonb_build_object('status','running','shards',
                                            COALESCE(batch.result_json -> 'rollback' -> 'shards','{}'::jsonb) || pg_catalog.jsonb_build_object(p_request ->> 'shard_id',p_request -> 'result_value'))),
                                    updated_at=pg_catalog.transaction_timestamp()
                                WHERE batch.id=v_id RETURNING batch.* INTO v_batch;
                            END IF;
                        END IF;
                    WHEN 'record_candidate' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'item_index') IS DISTINCT FROM 'number'
                           OR p_request ->> 'item_index' !~ '^(0|[1-9][0-9]*)$'
                           OR (p_request ->> 'item_index')::bigint > 2147483647
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_value') IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'Invalid Mask repair candidate' USING ERRCODE = '22023'; END IF;
                        IF v_batch.async_job_id=(p_request ->> 'async_job_id')::uuid THEN
                            UPDATE public.mask_repair_batches AS batch SET
                                result_json=COALESCE(batch.result_json,'{}'::jsonb) || pg_catalog.jsonb_build_object('candidates',COALESCE(batch.result_json -> 'candidates','{}'::jsonb) || pg_catalog.jsonb_build_object((p_request ->> 'item_index'),p_request -> 'result_value')),
                                updated_at=pg_catalog.transaction_timestamp()
                            WHERE batch.id=v_id RETURNING batch.* INTO v_batch;
                        ELSE v_data := pg_catalog.jsonb_build_object('changed',false); END IF;
                    WHEN 'record_shard_failure' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'task_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'shard_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'shard_id' !~ '^[0-9a-f]{24}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'error_message') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair shard failure' USING ERRCODE = '22023'; END IF;
                        IF pg_catalog.length(p_request ->> 'error_message') > 500 THEN RAISE EXCEPTION 'Invalid Mask repair shard failure length' USING ERRCODE = '22023'; END IF;
                        IF v_batch.async_job_id=(p_request ->> 'async_job_id')::uuid THEN
                            UPDATE public.mask_repair_batches AS batch SET
                                result_json=COALESCE(batch.result_json,'{}'::jsonb) || pg_catalog.jsonb_build_object('shards',COALESCE(batch.result_json -> 'shards','{}'::jsonb) || pg_catalog.jsonb_build_object(p_request ->> 'shard_id',pg_catalog.jsonb_build_object('status','failed','task_id',p_request ->> 'task_id','reason',pg_catalog.left(p_request ->> 'error_message',500)))),
                                updated_at=pg_catalog.transaction_timestamp()
                            WHERE batch.id=v_id RETURNING batch.* INTO v_batch;
                        ELSE v_data := pg_catalog.jsonb_build_object('changed',false); END IF;
                    WHEN 'finish_execution' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR p_request ->> 'status' NOT IN ('completed','partial','failed')
                           OR pg_catalog.jsonb_typeof(p_request -> 'completed_at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair completion' USING ERRCODE = '22023'; END IF;
                        UPDATE public.mask_repair_batches AS batch SET status=p_request ->> 'status',completed_at=(p_request ->> 'completed_at')::timestamptz,updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.async_job_id=(p_request ->> 'async_job_id')::uuid RETURNING batch.* INTO v_batch;
                    WHEN 'finish_rollback' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'rollback_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'rollback_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'rolled_back_at') IS DISTINCT FROM 'string'
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_json') IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'Invalid Mask repair rollback completion' USING ERRCODE = '22023'; END IF;
                        UPDATE public.mask_repair_batches AS batch SET status='rolled_back',rolled_back_at=(p_request ->> 'rolled_back_at')::timestamptz,
                            result_json=p_request -> 'result_json',updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.rollback_async_job_id=(p_request ->> 'rollback_job_id')::uuid RETURNING batch.* INTO v_batch;
                    WHEN 'fail_rollback' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'rollback_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'rollback_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'error_message') IS DISTINCT FROM 'string'
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_json') IS DISTINCT FROM 'object'
                           OR pg_catalog.jsonb_typeof(p_request -> 'failed_at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair rollback failure' USING ERRCODE = '22023'; END IF;
                        PERFORM (p_request ->> 'failed_at')::timestamptz;
                        UPDATE public.mask_repair_batches AS batch SET status='rollback_failed',
                            result_json=p_request -> 'result_json',updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.rollback_async_job_id=(p_request ->> 'rollback_job_id')::uuid RETURNING batch.* INTO v_batch;
                    WHEN 'fail_dispatch' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'error_message') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair dispatch failure' USING ERRCODE = '22023'; END IF;
                        UPDATE public.mask_repair_batches AS batch SET status='failed',updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.async_job_id=(p_request ->> 'async_job_id')::uuid RETURNING batch.* INTO v_batch;
                    WHEN 'fail_rollback_dispatch' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'error_message') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair rollback dispatch failure' USING ERRCODE = '22023'; END IF;
                        UPDATE public.mask_repair_batches AS batch SET status='rollback_failed',updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.rollback_async_job_id=(p_request ->> 'async_job_id')::uuid RETURNING batch.* INTO v_batch;
                    WHEN 'cancel_pending' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'status') IS DISTINCT FROM 'string'
                           OR p_request ->> 'status' NOT IN ('cancelled','partial')
                           OR pg_catalog.jsonb_typeof(p_request -> 'completed_at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair cancellation' USING ERRCODE = '22023'; END IF;
                        UPDATE public.mask_repair_batches AS batch SET status=p_request ->> 'status',completed_at=(p_request ->> 'completed_at')::timestamptz,updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.status IN ('pending','running') RETURNING batch.* INTO v_batch;
                END CASE;
                IF v_data IS NULL OR pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object' THEN
                    IF FOUND THEN v_data := pg_catalog.to_jsonb(v_batch); END IF;
                END IF;
                IF v_data IS NULL OR pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object' THEN
                    IF FOUND THEN v_data := pg_catalog.to_jsonb(v_batch); ELSE v_data := pg_catalog.jsonb_build_object('changed',false); END IF;
                END IF;
            END IF;
        END IF;
    ELSIF v_operation = 'insert_review_scope' THEN
        FOREACH v_field IN ARRAY ARRAY['scope_id','project_id','task_id','annotation_id'] LOOP
            IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
               OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask review scope identity' USING ERRCODE = '22023'; END IF;
        END LOOP;
        IF pg_catalog.jsonb_typeof(p_request -> 'qc_issue_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'qc_issue_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'source_job_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'source_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'expected_segment_id') NOT IN ('string','null')
           OR (p_request ->> 'expected_segment_id' IS NOT NULL AND p_request ->> 'expected_segment_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR pg_catalog.jsonb_typeof(p_request -> 'reviewer_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'reviewer_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'region_mask_ref') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'source_annotation_version') IS DISTINCT FROM 'number'
           OR (p_request ->> 'source_annotation_version') !~ '^[1-9][0-9]{0,9}$'
           OR (p_request ->> 'source_annotation_version')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'result_annotation_version') IS DISTINCT FROM 'number'
           OR (p_request ->> 'result_annotation_version') !~ '^[1-9][0-9]{0,9}$'
           OR (p_request ->> 'result_annotation_version')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'source_job_revision') IS DISTINCT FROM 'number'
           OR (p_request ->> 'source_job_revision') !~ '^(0|[1-9][0-9]{0,9})$'
           OR (p_request ->> 'source_job_revision')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'frame_start') IS DISTINCT FROM 'number'
           OR (p_request ->> 'frame_start') !~ '^(0|[1-9][0-9]{0,9})$'
           OR (p_request ->> 'frame_start')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'frame_end') IS DISTINCT FROM 'number'
           OR (p_request ->> 'frame_end') !~ '^(0|[1-9][0-9]{0,9})$'
           OR (p_request ->> 'frame_end')::bigint > 2147483647
           OR (p_request ->> 'frame_end')::bigint < (p_request ->> 'frame_start')::bigint
           OR pg_catalog.jsonb_typeof(p_request -> 'region_digest') IS DISTINCT FROM 'string'
           OR p_request ->> 'region_digest' !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'candidate_digest') IS DISTINCT FROM 'string'
           OR p_request ->> 'candidate_digest' !~ '^sha256:[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'decision') IS DISTINCT FROM 'string'
           OR p_request ->> 'decision' NOT IN ('accept','reject') THEN RAISE EXCEPTION 'Invalid Mask review scope facts' USING ERRCODE = '22023'; END IF;
        v_actor_id := (p_request ->> 'reviewer_id')::uuid;
        v_lock_reply := aap_identity_v1.lock_active_actor(v_actor_id);
        IF v_lock_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_data := pg_catalog.jsonb_build_object('created',false,'reason','actor_inactive');
        ELSE
            SELECT task.id,task.project_id INTO v_task
            FROM public.tasks AS task WHERE task.id=(p_request ->> 'task_id')::uuid FOR UPDATE NOWAIT;
            SELECT annotation.id,annotation.project_id,annotation.task_id,annotation.version,annotation.is_active,annotation.was_cancelled
            INTO v_annotation FROM public.annotations AS annotation
            WHERE annotation.id=(p_request ->> 'annotation_id')::uuid FOR KEY SHARE;
            SELECT issue.id,issue.project_id,issue.task_id,issue.annotation_id,issue.region_digest,issue.region_mask_ref
            INTO v_issue FROM public.mask_qc_issues AS issue
            WHERE issue.id=(p_request ->> 'qc_issue_id')::uuid FOR KEY SHARE;
            SELECT job.id,job.task_id,job.revision,job.segment_id,job.from_frame,job.to_frame
            INTO v_tracker_job FROM public.video_tracker_jobs AS job
            WHERE job.id=(p_request ->> 'source_job_id')::uuid FOR UPDATE;
            IF v_task.id IS NULL OR v_annotation.id IS NULL OR v_issue.id IS NULL OR v_tracker_job.id IS NULL
               OR v_task.project_id IS DISTINCT FROM (p_request ->> 'project_id')::uuid
               OR v_annotation.project_id IS DISTINCT FROM (p_request ->> 'project_id')::uuid
               OR v_annotation.task_id IS DISTINCT FROM (p_request ->> 'task_id')::uuid
               OR v_annotation.version IS DISTINCT FROM (p_request ->> 'result_annotation_version')::integer
               OR v_annotation.is_active IS NOT TRUE OR v_annotation.was_cancelled IS TRUE
               OR v_issue.project_id IS DISTINCT FROM (p_request ->> 'project_id')::uuid
               OR v_issue.task_id IS DISTINCT FROM (p_request ->> 'task_id')::uuid
               OR v_issue.annotation_id IS DISTINCT FROM (p_request ->> 'annotation_id')::uuid
               OR v_issue.status IS DISTINCT FROM 'open'
               OR v_issue.annotation_version IS DISTINCT FROM (p_request ->> 'source_annotation_version')::integer
               OR v_issue.source_versions ->> v_annotation.id::text IS DISTINCT FROM p_request ->> 'source_annotation_version'
               OR v_issue.region_digest IS DISTINCT FROM p_request ->> 'region_digest'
               OR v_issue.region_mask_ref IS DISTINCT FROM p_request -> 'region_mask_ref'
               OR (p_request -> 'region_mask_ref' ->> 'sha256') IS DISTINCT FROM p_request ->> 'region_digest'
               OR v_tracker_job.task_id IS DISTINCT FROM (p_request ->> 'task_id')::uuid
               OR v_tracker_job.status NOT IN ('partially_reviewed','accepted','discarded','cancelled')
               OR v_tracker_job.revision::bigint IS DISTINCT FROM (p_request ->> 'source_job_revision')::bigint + 1
               OR v_tracker_job.segment_id IS DISTINCT FROM (p_request ->> 'expected_segment_id')::uuid
               OR (p_request ->> 'frame_start')::integer < v_tracker_job.from_frame
               OR (p_request ->> 'frame_end')::integer > v_tracker_job.to_frame THEN
                v_data := pg_catalog.jsonb_build_object('created',false,'reason','review_scope_changed');
            ELSE
        INSERT INTO public.mask_review_scopes(
            id,project_id,task_id,annotation_id,qc_issue_id,source_job_id,reviewer_id,
            source_annotation_version,result_annotation_version,source_job_revision,frame_start,frame_end,
            region_mask_ref,region_digest,candidate_digest,decision
        ) VALUES (
            (p_request ->> 'scope_id')::uuid,(p_request ->> 'project_id')::uuid,(p_request ->> 'task_id')::uuid,
            (p_request ->> 'annotation_id')::uuid,(p_request ->> 'qc_issue_id')::uuid,(p_request ->> 'source_job_id')::uuid,
            (p_request ->> 'reviewer_id')::uuid,(p_request ->> 'source_annotation_version')::integer,
            (p_request ->> 'result_annotation_version')::integer,(p_request ->> 'source_job_revision')::integer,
            (p_request ->> 'frame_start')::integer,(p_request ->> 'frame_end')::integer,p_request -> 'region_mask_ref',
            p_request ->> 'region_digest',p_request ->> 'candidate_digest',p_request ->> 'decision'
        ) RETURNING public.mask_review_scopes.* INTO v_scope;
        v_data := pg_catalog.jsonb_build_object('created',true,'row',pg_catalog.to_jsonb(v_scope));
            END IF;
        END IF;
    ELSIF v_operation = 'read_review_scopes' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'annotation_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'annotation_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'result_annotation_version') IS DISTINCT FROM 'number'
           OR (p_request ->> 'result_annotation_version') !~ '^[1-9][0-9]{0,9}$'
           OR (p_request ->> 'result_annotation_version')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'frame_start') IS DISTINCT FROM 'number'
           OR (p_request ->> 'frame_start') !~ '^(0|[1-9][0-9]{0,9})$'
           OR (p_request ->> 'frame_start')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'frame_end') IS DISTINCT FROM 'number'
           OR (p_request ->> 'frame_end') !~ '^(0|[1-9][0-9]{0,9})$'
           OR (p_request ->> 'frame_end')::bigint > 2147483647
           OR (p_request ->> 'frame_end')::bigint < (p_request ->> 'frame_start')::bigint
           OR pg_catalog.jsonb_typeof(p_request -> 'lock') IS DISTINCT FROM 'boolean' THEN RAISE EXCEPTION 'Invalid Mask review scope query' USING ERRCODE = '22023'; END IF;
        IF (p_request ->> 'lock')::boolean THEN
            FOR v_scope IN SELECT scope.* FROM public.mask_review_scopes AS scope
                WHERE scope.annotation_id=(p_request ->> 'annotation_id')::uuid
                  AND scope.result_annotation_version=(p_request ->> 'result_annotation_version')::integer
                  AND scope.frame_start <= (p_request ->> 'frame_end')::integer
                  AND scope.frame_end >= (p_request ->> 'frame_start')::integer
                ORDER BY scope.id FOR UPDATE
            LOOP v_rows := v_rows || pg_catalog.jsonb_build_array(pg_catalog.to_jsonb(v_scope)); END LOOP;
        ELSE
            SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(scope) ORDER BY scope.id),'[]'::jsonb)
            INTO v_rows FROM public.mask_review_scopes AS scope
            WHERE scope.annotation_id=(p_request ->> 'annotation_id')::uuid
              AND scope.result_annotation_version=(p_request ->> 'result_annotation_version')::integer
              AND scope.frame_start <= (p_request ->> 'frame_end')::integer
              AND scope.frame_end >= (p_request ->> 'frame_start')::integer;
        END IF;
        v_data := v_rows;
    ELSIF v_operation = 'read_repair_targets' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'issue_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'issue_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'include_related') IS DISTINCT FROM 'boolean' THEN RAISE EXCEPTION 'Invalid Mask repair target query' USING ERRCODE = '22023'; END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
            'id',annotation.id,'project_id',annotation.project_id,'task_id',annotation.task_id,
            'version',annotation.version,'geometry',annotation.geometry,'is_locked',annotation.is_locked,
            'class_name',annotation.class_name,'tool_unit_id',annotation.tool_unit_id
        ) ORDER BY annotation.id),'[]'::jsonb)
        INTO v_rows
        FROM public.mask_qc_issues AS issue
        JOIN public.annotations AS annotation ON (
            (NOT (p_request ->> 'include_related')::boolean AND annotation.id=issue.annotation_id)
            OR ((p_request ->> 'include_related')::boolean
                AND (annotation.id=issue.annotation_id OR annotation.id=ANY(issue.related_annotation_ids))
                AND annotation.is_active IS TRUE
                AND annotation.was_cancelled IS FALSE)
        )
        WHERE issue.id=(p_request ->> 'issue_id')::uuid;
        v_data := v_rows;
    ELSIF v_operation = 'read_video_segment' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'dataset_item_id') NOT IN ('string','null')
           OR (p_request ->> 'dataset_item_id' IS NOT NULL AND p_request ->> 'dataset_item_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR pg_catalog.jsonb_typeof(p_request -> 'frame_index') IS DISTINCT FROM 'number'
           OR p_request ->> 'frame_index' !~ '^(0|[1-9][0-9]{0,9})$'
           OR (p_request ->> 'frame_index')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'requested_id') NOT IN ('string','null')
           OR (p_request ->> 'requested_id' IS NOT NULL AND p_request ->> 'requested_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN RAISE EXCEPTION 'Invalid Labeling video segment lookup' USING ERRCODE = '22023'; END IF;
        SELECT segment.* INTO v_segment FROM public.video_segments AS segment
        WHERE segment.dataset_item_id=(p_request ->> 'dataset_item_id')::uuid
          AND segment.start_frame <= (p_request ->> 'frame_index')::integer
          AND segment.end_frame >= (p_request ->> 'frame_index')::integer
          AND (p_request ->> 'requested_id' IS NULL OR segment.id=(p_request ->> 'requested_id')::uuid)
        ORDER BY segment.segment_index ASC LIMIT 1;
        IF FOUND THEN
            v_data := pg_catalog.jsonb_build_object(
                'id',v_segment.id,'locked_by',v_segment.locked_by,
                'lock_expires_at',v_segment.lock_expires_at
            );
        ELSE v_data := NULL; END IF;
    END IF;
    RETURN pg_catalog.jsonb_build_object('schema','aap-labeling-quality-data/v1','ok',true,'request',p_request,'data',v_data);
EXCEPTION
    WHEN invalid_text_representation OR invalid_datetime_format OR datetime_field_overflow OR numeric_value_out_of_range THEN
        RAISE EXCEPTION 'Invalid Mask repair request value' USING ERRCODE = '22023';
END;
$function$;
