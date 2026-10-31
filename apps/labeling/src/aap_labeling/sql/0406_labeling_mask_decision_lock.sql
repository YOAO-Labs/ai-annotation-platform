CREATE OR REPLACE FUNCTION aap_labeling_v1.ai_mask_accept_command(p_request jsonb)
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
    v_key text;
    v_task_id uuid;
    v_project_id uuid;
    v_actor_id uuid;
    v_task record;
    v_annotation record;
    v_decision record;
    v_account_reply jsonb;
    v_account jsonb;
    v_data jsonb;
    v_reply jsonb;
    v_now timestamptz;
    v_deleted integer := 0;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-ai-mask-accept/v1'
       OR jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid AI Mask accept request' USING ERRCODE = '22023';
    END IF;
    v_operation := p_request ->> 'operation';
    CASE v_operation
        WHEN 'find_decision', 'lock_decision' THEN
            v_expected_keys := ARRAY['schema','operation','task_id','idempotency_key'];
        WHEN 'record_decision' THEN
            v_expected_keys := ARRAY[
                'schema','operation','task_id','project_id','actor_id','expected_batch_id',
                'expected_status','expected_review_round_id','idempotency_key','request_digest',
                'candidate_id','content_digest','prediction_id','prediction_created_at',
                'annotation_id','source_annotation_id','source_version','result_version','response_json'
            ];
        WHEN 'refine_annotation' THEN
            v_expected_keys := ARRAY[
                'schema','operation','task_id','project_id','actor_id','expected_batch_id',
                'expected_status','expected_review_round_id','annotation_id','expected_version',
                'video_segment_id','annotation_type','prediction_id','class_name','geometry',
                'track_id','confidence'
            ];
        WHEN 'expire_decisions' THEN
            v_expected_keys := ARRAY['schema','operation','now'];
        ELSE
            RAISE EXCEPTION 'Invalid AI Mask accept operation' USING ERRCODE = '22023';
    END CASE;
    SELECT array_agg(key ORDER BY key) INTO v_keys
    FROM jsonb_object_keys(p_request) AS fields(key);
    SELECT array_agg(key ORDER BY key) INTO v_expected_keys
    FROM unnest(v_expected_keys) AS fields(key);
    IF v_keys IS DISTINCT FROM v_expected_keys THEN
        RAISE EXCEPTION 'Invalid AI Mask accept request fields' USING ERRCODE = '22023';
    END IF;

    IF v_operation IN ('find_decision', 'lock_decision') THEN
        IF jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'idempotency_key') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid AI Mask decision identity' USING ERRCODE = '22023';
        END IF;
        v_task_id := (p_request ->> 'task_id')::uuid;
        IF v_operation = 'lock_decision' THEN
            SELECT decision.* INTO v_decision
            FROM public.ai_mask_accept_decisions AS decision
            WHERE decision.task_id = v_task_id
              AND decision.idempotency_key = p_request ->> 'idempotency_key'
            FOR SHARE NOWAIT;
        ELSE
            SELECT decision.* INTO v_decision
            FROM public.ai_mask_accept_decisions AS decision
            WHERE decision.task_id = v_task_id
              AND decision.idempotency_key = p_request ->> 'idempotency_key';
        END IF;
        v_data := jsonb_build_object(
            'decision',jsonb_build_object(
                'found',FOUND,
                'request_digest',CASE WHEN FOUND THEN v_decision.request_digest END,
                'candidate_id',CASE WHEN FOUND THEN v_decision.candidate_id END,
                'content_digest',CASE WHEN FOUND THEN v_decision.content_digest END,
                'prediction_id',CASE WHEN FOUND THEN v_decision.prediction_id END,
                'prediction_created_at',CASE WHEN FOUND THEN v_decision.prediction_created_at END,
                'annotation_id',CASE WHEN FOUND THEN v_decision.annotation_id END,
                'source_annotation_id',CASE WHEN FOUND THEN v_decision.source_annotation_id END,
                'source_version',CASE WHEN FOUND THEN v_decision.source_version END,
                'result_version',CASE WHEN FOUND THEN v_decision.result_version END,
                'actor_id',CASE WHEN FOUND THEN v_decision.actor_id END,
                'response_json',CASE WHEN FOUND THEN v_decision.response_json END,
                'expires_at',CASE WHEN FOUND THEN v_decision.expires_at END
            ),
            'annotation_id',NULL,'version',NULL,'updated_at',NULL,'affected',0
        );
    ELSIF v_operation = 'expire_decisions' THEN
        IF jsonb_typeof(p_request -> 'now') NOT IN ('string','null') THEN
            RAISE EXCEPTION 'Invalid AI Mask expiry time' USING ERRCODE = '22023';
        END IF;
        v_now := COALESCE((p_request ->> 'now')::timestamptz,clock_timestamp());
        WITH expired AS (
            DELETE FROM public.ai_mask_accept_decisions AS decision
            WHERE decision.expires_at <= v_now RETURNING decision.id
        ) SELECT count(*)::integer INTO v_deleted FROM expired;
        v_data := jsonb_build_object(
            'decision',NULL,'annotation_id',NULL,'version',NULL,'updated_at',NULL,
            'affected',v_deleted
        );
    ELSE
        FOREACH v_key IN ARRAY ARRAY['task_id','project_id','actor_id','expected_status'] LOOP
            IF jsonb_typeof(p_request -> v_key) IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION 'Invalid AI Mask task fence' USING ERRCODE = '22023';
            END IF;
        END LOOP;
        IF jsonb_typeof(p_request -> 'expected_batch_id') NOT IN ('string','null')
           OR jsonb_typeof(p_request -> 'expected_review_round_id') NOT IN ('string','null') THEN
            RAISE EXCEPTION 'Invalid AI Mask task fence' USING ERRCODE = '22023';
        END IF;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        v_account_reply := aap_identity_v1.lock_accounts_share(ARRAY[v_actor_id],true);
        IF jsonb_typeof(v_account_reply -> 'accounts') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid Identity account-lock reply' USING ERRCODE = '55000';
        END IF;
        SELECT account.value INTO v_account
        FROM jsonb_array_elements(v_account_reply -> 'accounts') AS account(value)
        WHERE account.value ->> 'id' = v_actor_id::text;
        IF v_account IS NULL OR v_account -> 'is_active' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_ai_mask_accept_error',
                DETAIL = jsonb_build_object('status_code',403,'detail',jsonb_build_object('reason','actor_inactive'))::text;
        END IF;
        SELECT task.id,task.project_id,task.batch_id,task.status,task.review_round_id
        INTO v_task FROM public.tasks AS task WHERE task.id = v_task_id FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_ai_mask_accept_error',
                DETAIL = jsonb_build_object('status_code',404,'detail',jsonb_build_object('reason','task_not_found'))::text;
        END IF;
        IF v_task.project_id IS DISTINCT FROM v_project_id
           OR v_task.batch_id IS DISTINCT FROM (p_request ->> 'expected_batch_id')::uuid
           OR v_task.status IS DISTINCT FROM p_request ->> 'expected_status'
           OR v_task.review_round_id IS DISTINCT FROM (p_request ->> 'expected_review_round_id')::uuid THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_ai_mask_accept_error',
                DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','task_target_changed'))::text;
        END IF;
        IF v_operation = 'refine_annotation' THEN
            IF jsonb_typeof(p_request -> 'annotation_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'expected_version') IS DISTINCT FROM 'number'
               OR p_request ->> 'expected_version' !~ '^[1-9][0-9]*$'
               OR jsonb_typeof(p_request -> 'video_segment_id') NOT IN ('string','null')
               OR jsonb_typeof(p_request -> 'annotation_type') IS DISTINCT FROM 'string'
               OR p_request ->> 'annotation_type' NOT IN ('raster_mask','video_track_mask')
               OR jsonb_typeof(p_request -> 'prediction_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'class_name') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'geometry') IS DISTINCT FROM 'object'
               OR jsonb_typeof(p_request -> 'track_id') NOT IN ('string','null')
               OR jsonb_typeof(p_request -> 'confidence') NOT IN ('number','null') THEN
                RAISE EXCEPTION 'Invalid AI Mask refinement payload' USING ERRCODE = '22023';
            END IF;
            SELECT annotation.* INTO v_annotation
            FROM public.annotations AS annotation
            WHERE annotation.id = (p_request ->> 'annotation_id')::uuid FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_ai_mask_accept_error',
                    DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','annotation_target_changed'))::text;
            END IF;
            IF v_annotation.task_id IS DISTINCT FROM v_task_id
               OR (v_annotation.project_id IS NOT NULL AND v_annotation.project_id IS DISTINCT FROM v_project_id)
               OR v_annotation.version IS DISTINCT FROM (p_request ->> 'expected_version')::integer
               OR v_annotation.is_active IS NOT TRUE OR v_annotation.is_locked IS TRUE
               OR v_annotation.video_segment_id IS DISTINCT FROM (p_request ->> 'video_segment_id')::uuid
               OR v_annotation.annotation_type IS DISTINCT FROM p_request ->> 'annotation_type'
               OR v_annotation.geometry ->> 'type' IS DISTINCT FROM p_request ->> 'annotation_type' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_ai_mask_accept_error',
                    DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','annotation_target_changed'))::text;
            END IF;
            UPDATE public.annotations AS annotation
            SET annotation_type = p_request ->> 'annotation_type',
                tool_unit_id = 'region',
                class_name = p_request ->> 'class_name',
                geometry = p_request -> 'geometry',
                track_id = p_request ->> 'track_id',
                confidence = (p_request ->> 'confidence')::double precision,
                parent_prediction_id = (p_request ->> 'prediction_id')::uuid,
                version = annotation.version + 1,
                updated_at = transaction_timestamp()
            WHERE annotation.id = v_annotation.id
            RETURNING annotation.id,annotation.version,annotation.updated_at
            INTO v_annotation;
            v_data := jsonb_build_object(
                'decision',NULL,'annotation_id',v_annotation.id,'version',v_annotation.version,
                'updated_at',v_annotation.updated_at,'affected',1
            );
        ELSE
            IF jsonb_typeof(p_request -> 'idempotency_key') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'request_digest') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'candidate_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'content_digest') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'prediction_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'prediction_created_at') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'annotation_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'source_annotation_id') NOT IN ('string','null')
               OR jsonb_typeof(p_request -> 'source_version') NOT IN ('number','null')
               OR jsonb_typeof(p_request -> 'result_version') IS DISTINCT FROM 'number'
               OR jsonb_typeof(p_request -> 'response_json') IS DISTINCT FROM 'object' THEN
                RAISE EXCEPTION 'Invalid AI Mask decision record' USING ERRCODE = '22023';
            END IF;
            IF p_request ->> 'request_digest' !~ '^[0-9a-f]{64}$'
               OR p_request ->> 'content_digest' !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'Invalid AI Mask decision digest' USING ERRCODE = '22023';
            END IF;
            PERFORM 1 FROM public.predictions AS prediction
            WHERE prediction.id = (p_request ->> 'prediction_id')::uuid
              AND prediction.created_at = (p_request ->> 'prediction_created_at')::timestamptz
              AND prediction.task_id = v_task_id
              AND prediction.project_id = v_project_id
              AND prediction.source = 'interactive_accept'
            FOR KEY SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_ai_mask_accept_error',
                    DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','prediction_source_changed'))::text;
            END IF;
            INSERT INTO public.ai_mask_accept_decisions (
                id,task_id,idempotency_key,request_digest,candidate_id,content_digest,
                prediction_id,prediction_created_at,annotation_id,source_annotation_id,
                source_version,result_version,actor_id,response_json
            ) VALUES (
                pg_catalog.gen_random_uuid(),v_task_id,
                p_request ->> 'idempotency_key',p_request ->> 'request_digest',
                p_request ->> 'candidate_id',p_request ->> 'content_digest',
                (p_request ->> 'prediction_id')::uuid,
                (p_request ->> 'prediction_created_at')::timestamptz,
                (p_request ->> 'annotation_id')::uuid,
                (p_request ->> 'source_annotation_id')::uuid,
                (p_request ->> 'source_version')::integer,
                (p_request ->> 'result_version')::integer,v_actor_id,
                p_request -> 'response_json'
            );
            v_data := jsonb_build_object(
                'decision',NULL,'annotation_id',NULL,'version',NULL,'updated_at',NULL,
                'affected',1
            );
        END IF;
    END IF;
    v_reply := jsonb_build_object(
        'schema','aap-labeling-ai-mask-accept/v1','ok',true,
        'request',p_request,'data',v_data
    );
    RETURN v_reply;
END;
$function$;
