-- Revoked request authority terminalizes only its own queued unit.
-- Retained video materialization and frame rendering are worker-owned leases.
CREATE OR REPLACE FUNCTION aap_assets_v1.claim_accepted_video_input(
    p_worker_id uuid, p_input_id uuid, p_lease_seconds integer
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_input public.asset_accepted_video_inputs%ROWTYPE;
    v_attempt public.asset_accepted_video_input_attempts%ROWTYPE;
    v_manifest public.asset_accepted_video_manifests%ROWTYPE;
    v_candidate record;
    v_reply jsonb;
    v_source_code text;
    v_code text;
    v_now timestamptz;
    v_expires timestamptz;
    v_token uuid;
    v_generation bigint;
    v_prefix text;
    v_claim jsonb;
    v_expired boolean := false;
    v_message text;
BEGIN
    IF p_worker_id IS NULL OR p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 60 AND 7200 THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'invalid_request');
    END IF;
    IF p_input_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.asset_accepted_video_inputs AS input WHERE input.input_id=p_input_id
    ) THEN RETURN aap_assets_v1._video_input_reply(NULL,'input_missing'); END IF;
    FOR v_candidate IN
        SELECT input.input_id FROM public.asset_accepted_video_inputs AS input
        WHERE (p_input_id IS NULL OR input.input_id=p_input_id)
          AND (input.status='pending' OR (input.status='materializing'
              AND input.lease_expires_at<=pg_catalog.clock_timestamp()))
        ORDER BY input.created_at,input.input_id
        LIMIT CASE WHEN p_input_id IS NULL THEN 100 ELSE 1 END
        FOR UPDATE OF input SKIP LOCKED
    LOOP
        v_expired := false;
        v_attempt := NULL;
        SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
        WHERE input.input_id=v_candidate.input_id;
        v_now := pg_catalog.clock_timestamp();
        IF v_input.status='materializing' THEN
            SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_input_attempts AS attempt
            WHERE attempt.input_id=v_input.input_id AND attempt.generation=v_input.generation
              AND attempt.lease_token=v_input.lease_token AND attempt.status='leased'
            FOR UPDATE NOWAIT;
            IF NOT FOUND OR v_attempt.lease_expires_at>v_now THEN
                RETURN aap_assets_v1._video_input_reply(NULL,'input_busy');
            END IF;
            v_expired := true;
            v_input.last_lease_token := v_attempt.lease_token;
            v_input.last_outcome := 'lease_expired';
        END IF;
        SELECT manifest.* INTO v_manifest FROM public.asset_accepted_video_manifests AS manifest
        WHERE manifest.job_id=v_input.job_id;
        IF NOT FOUND THEN RETURN aap_assets_v1._video_input_reply(NULL,'manifest_missing'); END IF;
        IF v_manifest.status<>'preparing' THEN
            RETURN aap_assets_v1._video_input_reply(NULL,'manifest_not_ready');
        END IF;
        v_reply := aap_assets_v1._video_input_job(v_manifest,v_input.task_id,true);
        v_code := aap_assets_v1._video_input_code(v_reply,'aap-assets-video-input-execution/v1');
        IF v_code='job_not_active' THEN
            IF v_expired THEN
                UPDATE public.asset_accepted_video_input_attempts AS attempt
                SET status='abandoned',completed_at=v_now
                WHERE attempt.input_id=v_attempt.input_id AND attempt.generation=v_attempt.generation;
                PERFORM aap_assets_v1._video_input_queue_prefix(
                    v_attempt.prefix,'lease_expired',GREATEST(v_now,v_attempt.lease_expires_at)+INTERVAL '24 hours'
                );
            END IF;
            UPDATE public.asset_accepted_video_inputs AS input SET status='failed',error_code='job_not_active',
                lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                last_lease_token=COALESCE(v_input.last_lease_token,input.last_lease_token),
                last_outcome=CASE WHEN COALESCE(v_input.last_lease_token,input.last_lease_token) IS NULL
                    THEN NULL ELSE COALESCE(v_input.last_outcome,input.last_outcome) END,
                updated_at=v_now WHERE input.input_id=v_input.input_id RETURNING * INTO v_input;
            PERFORM aap_assets_v1._video_input_complete_member(v_input.input_id,'failed','job_not_active');
            CONTINUE;
        END IF;
        IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_code); END IF;
        v_source_code := aap_assets_v1._video_input_current_source(
            v_input,v_input.request_actor_id,true,true
        );
        IF v_source_code IS NOT NULL THEN
            IF v_source_code IN ('account_not_active','invalid_account_role','source_missing','source_changed','source_not_ready','not_video',
                'task_scope_changed','timebase_changed','input_expired','input_corrupt','permission_denied',
                'task_not_visible') THEN
                IF v_expired THEN
                    UPDATE public.asset_accepted_video_input_attempts AS attempt
                    SET status='abandoned',completed_at=v_now
                    WHERE attempt.input_id=v_attempt.input_id AND attempt.generation=v_attempt.generation;
                    PERFORM aap_assets_v1._video_input_queue_prefix(
                        v_attempt.prefix,'lease_expired',GREATEST(v_now,v_attempt.lease_expires_at)+INTERVAL '24 hours'
                    );
                END IF;
                UPDATE public.asset_accepted_video_inputs AS input
                SET status='failed',error_code=v_source_code,lease_owner=NULL,lease_token=NULL,
                    lease_expires_at=NULL,
                    last_lease_token=CASE WHEN v_expired THEN v_attempt.lease_token ELSE input.last_lease_token END,
                    last_outcome=CASE WHEN v_expired THEN 'lease_expired' ELSE input.last_outcome END,updated_at=v_now
                WHERE input.input_id=v_input.input_id RETURNING * INTO v_input;
                PERFORM aap_assets_v1._video_input_complete_member(v_input.input_id,'failed',v_source_code);
                CONTINUE;
            END IF;
            RETURN aap_assets_v1._video_input_reply(NULL,v_source_code);
        END IF;
        IF v_expired THEN
            UPDATE public.asset_accepted_video_input_attempts AS attempt
            SET status='abandoned',completed_at=v_now
            WHERE attempt.input_id=v_attempt.input_id AND attempt.generation=v_attempt.generation;
            PERFORM aap_assets_v1._video_input_queue_prefix(
                v_attempt.prefix,'lease_expired',GREATEST(v_now,v_attempt.lease_expires_at)+INTERVAL '24 hours'
            );
        END IF;
        IF v_input.attempt_count>=5 OR v_input.generation>=9007199254740991 THEN
            UPDATE public.asset_accepted_video_inputs AS input
            SET status='failed',error_code='resource_budget_exceeded',lease_owner=NULL,
                lease_token=NULL,lease_expires_at=NULL,
                last_lease_token=COALESCE(v_input.last_lease_token,input.last_lease_token),
                last_outcome=CASE WHEN COALESCE(v_input.last_lease_token,input.last_lease_token) IS NULL
                    THEN NULL ELSE COALESCE(v_input.last_outcome,input.last_outcome) END,updated_at=v_now
            WHERE input.input_id=v_input.input_id RETURNING * INTO v_input;
            PERFORM aap_assets_v1._video_input_complete_member(
                v_input.input_id,'failed','resource_budget_exceeded'
            );
            CONTINUE;
        END IF;
        v_generation := v_input.generation+1;
        v_token := pg_catalog.gen_random_uuid();
        v_prefix := aap_assets_v1._video_input_attempt_prefix(v_input.input_id,v_generation,v_token);
        v_expires := v_now+pg_catalog.make_interval(secs=>p_lease_seconds);
        INSERT INTO public.asset_accepted_video_input_attempts(
            input_id,generation,lease_token,prefix,status,lease_expires_at
        ) VALUES(v_input.input_id,v_generation,v_token,v_prefix,'leased',v_expires);
        UPDATE public.asset_accepted_video_inputs AS input
        SET status='materializing',generation=v_generation,attempt_count=input.attempt_count+1,
            lease_owner=p_worker_id,lease_token=v_token,lease_expires_at=v_expires,error_code=NULL,
            staged_count=0,staged_bytes=0,
            last_lease_token=COALESCE(v_input.last_lease_token,input.last_lease_token),
            last_outcome=COALESCE(v_input.last_outcome,input.last_outcome),updated_at=v_now
        WHERE input.input_id=v_input.input_id RETURNING * INTO v_input;
        v_claim := pg_catalog.jsonb_build_object(
            'input',aap_assets_v1._video_input_record(v_input),'source',v_input.source_facts,
            'generation',v_generation,'lease_token',v_token,'lease_expires_at',v_expires,
            'blob_key',v_prefix||'source'
        );
        RETURN aap_assets_v1._video_input_reply(pg_catalog.jsonb_build_object('claim',v_claim),NULL);
    END LOOP;
    RETURN aap_assets_v1._video_input_reply(pg_catalog.jsonb_build_object('claim',NULL),NULL);
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'input_busy');
    WHEN OTHERS THEN
        GET STACKED DIAGNOSTICS v_message=MESSAGE_TEXT;
        IF v_message LIKE 'video_input_abort:%' THEN
            RETURN aap_assets_v1._video_input_reply(NULL,pg_catalog.substr(v_message,19));
        END IF;
        RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.claim_accepted_video_input(uuid,uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.claim_accepted_video_frame(
    p_worker_id uuid,p_frame_id uuid,p_lease_seconds integer
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_frame public.asset_accepted_video_frames%ROWTYPE;
    v_attempt public.asset_accepted_video_frame_attempts%ROWTYPE;
    v_input public.asset_accepted_video_inputs%ROWTYPE;
    v_manifest public.asset_accepted_video_manifests%ROWTYPE;
    v_member public.asset_accepted_video_members%ROWTYPE;
    v_time public.asset_accepted_video_times%ROWTYPE;
    v_candidate record; v_reply jsonb; v_code text; v_source_code text;
    v_now timestamptz; v_expires timestamptz; v_token uuid; v_generation bigint; v_prefix text;
    v_key text; v_claim jsonb; v_expired boolean;
    v_message text;
BEGIN
    IF p_worker_id IS NULL OR p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 60 AND 7200 THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'invalid_request');
    END IF;
    IF p_frame_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.asset_accepted_video_frames AS frame WHERE frame.frame_id=p_frame_id
    ) THEN RETURN aap_assets_v1._video_input_reply(NULL,'frame_missing'); END IF;
    FOR v_candidate IN
        SELECT frame.frame_id FROM public.asset_accepted_video_frames AS frame
        WHERE (p_frame_id IS NULL OR frame.frame_id=p_frame_id)
          AND (frame.status='pending' OR (frame.status='materializing'
              AND frame.lease_expires_at<=pg_catalog.clock_timestamp()))
        ORDER BY frame.created_at,frame.frame_id
        LIMIT CASE WHEN p_frame_id IS NULL THEN 100 ELSE 1 END
        FOR UPDATE OF frame SKIP LOCKED
    LOOP
        v_expired := false;
        v_attempt := NULL;
        SELECT frame.* INTO v_frame FROM public.asset_accepted_video_frames AS frame
        WHERE frame.frame_id=v_candidate.frame_id;
        v_now := pg_catalog.clock_timestamp();
        IF v_frame.status='materializing' THEN
            SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_frame_attempts AS attempt
            WHERE attempt.frame_id=v_frame.frame_id AND attempt.generation=v_frame.generation
              AND attempt.lease_token=v_frame.lease_token AND attempt.status='leased' FOR UPDATE NOWAIT;
            IF NOT FOUND OR v_attempt.lease_expires_at>v_now THEN
                RETURN aap_assets_v1._video_input_reply(NULL,'frame_busy');
            END IF;
            v_expired := true;
        END IF;
        SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
        WHERE input.input_id=v_frame.input_id FOR SHARE NOWAIT;
        IF NOT FOUND OR v_input.status<>'ready' OR v_input.plan IS NULL
           OR v_input.plan->>'plan_digest' IS DISTINCT FROM v_frame.plan_digest THEN
            RETURN aap_assets_v1._video_input_reply(NULL,'input_not_ready');
        END IF;
        IF v_frame.ordinal>(v_input.plan->>'selected_count')::bigint
           OR v_frame.frame_index::bigint<>(v_frame.ordinal-1)*(v_input.sampling->>'step')::bigint THEN
            RETURN aap_assets_v1._video_input_reply(NULL,'timebase_changed');
        END IF;
        SELECT manifest.* INTO v_manifest FROM public.asset_accepted_video_manifests AS manifest
        WHERE manifest.job_id=v_input.job_id FOR SHARE NOWAIT;
        IF NOT FOUND THEN RETURN aap_assets_v1._video_input_reply(NULL,'manifest_missing'); END IF;
        IF v_manifest.status<>'sealed' THEN RETURN aap_assets_v1._video_input_reply(NULL,'manifest_not_ready'); END IF;
        SELECT member.* INTO v_member FROM public.asset_accepted_video_members AS member
        WHERE member.job_id=v_input.job_id AND member.task_id=v_input.task_id;
        IF NOT FOUND OR v_member.state<>'ready' OR v_member.input_id IS DISTINCT FROM v_input.input_id THEN
            RETURN aap_assets_v1._video_input_reply(NULL,'input_not_ready');
        END IF;
        SELECT times.* INTO v_time FROM public.asset_accepted_video_times AS times
        WHERE times.input_id=v_input.input_id AND times.generation=v_input.generation
          AND times.ordinal=v_frame.ordinal AND times.lease_token=v_input.last_lease_token;
        IF NOT FOUND OR (v_time.frame_index,v_time.pts_ms,v_time.is_keyframe)
            IS DISTINCT FROM (v_frame.frame_index,v_frame.pts_ms,v_frame.is_keyframe) THEN
            RETURN aap_assets_v1._video_input_reply(NULL,'timebase_changed');
        END IF;
        v_reply := aap_assets_v1._video_input_job(v_manifest,v_input.task_id,false);
        v_code := aap_assets_v1._video_input_code(v_reply,'aap-assets-video-input-execution/v1');
        IF v_code IS NOT NULL THEN
            IF v_code IN ('job_not_active','task_not_visible','permission_denied','source_missing','source_changed',
                'source_not_ready','not_video','task_scope_changed','timebase_changed','input_expired','input_corrupt') THEN
                IF v_expired THEN
                    UPDATE public.asset_accepted_video_frame_attempts AS attempt SET status='abandoned',completed_at=v_now
                    WHERE attempt.frame_id=v_attempt.frame_id AND attempt.generation=v_attempt.generation;
                    PERFORM aap_assets_v1._video_input_queue_prefix(v_attempt.prefix,'lease_expired',
                        GREATEST(v_now,v_attempt.lease_expires_at)+INTERVAL '24 hours');
                END IF;
                UPDATE public.asset_accepted_video_frames AS frame SET status='failed',error_code=v_code,
                    lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                    last_lease_token=CASE WHEN v_expired THEN v_attempt.lease_token ELSE frame.last_lease_token END,
                    last_outcome=CASE WHEN v_expired THEN 'lease_expired' ELSE frame.last_outcome END,updated_at=v_now
                WHERE frame.frame_id=v_frame.frame_id;
                CONTINUE;
            END IF;
            RETURN aap_assets_v1._video_input_reply(NULL,v_code);
        END IF;
        v_source_code := aap_assets_v1._video_input_current_source(v_input,v_frame.request_actor_id,true,false);
        IF v_source_code IS NOT NULL THEN
            IF v_source_code IN ('account_not_active','invalid_account_role','task_not_visible','permission_denied','source_missing','source_changed',
                'source_not_ready','not_video','task_scope_changed','timebase_changed','input_expired','input_corrupt') THEN
                IF v_expired THEN
                    UPDATE public.asset_accepted_video_frame_attempts AS attempt SET status='abandoned',completed_at=v_now
                    WHERE attempt.frame_id=v_attempt.frame_id AND attempt.generation=v_attempt.generation;
                    PERFORM aap_assets_v1._video_input_queue_prefix(v_attempt.prefix,'lease_expired',
                        GREATEST(v_now,v_attempt.lease_expires_at)+INTERVAL '24 hours');
                END IF;
                UPDATE public.asset_accepted_video_frames AS frame SET status='failed',error_code=v_source_code,
                    lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                    last_lease_token=CASE WHEN v_expired THEN v_attempt.lease_token ELSE frame.last_lease_token END,
                    last_outcome=CASE WHEN v_expired THEN 'lease_expired' ELSE frame.last_outcome END,updated_at=v_now
                WHERE frame.frame_id=v_frame.frame_id;
                CONTINUE;
            END IF;
            RETURN aap_assets_v1._video_input_reply(NULL,v_source_code);
        END IF;
        IF v_expired THEN
            UPDATE public.asset_accepted_video_frame_attempts AS attempt SET status='abandoned',completed_at=v_now
            WHERE attempt.frame_id=v_attempt.frame_id AND attempt.generation=v_attempt.generation;
            PERFORM aap_assets_v1._video_input_queue_prefix(
                v_attempt.prefix,'lease_expired',GREATEST(v_now,v_attempt.lease_expires_at)+INTERVAL '24 hours'
            );
        END IF;
        IF v_frame.attempt_count>=5 OR v_frame.generation>=9007199254740991 THEN
            UPDATE public.asset_accepted_video_frames AS frame SET status='failed',error_code='resource_budget_exceeded',
                lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                last_lease_token=CASE WHEN v_expired THEN v_attempt.lease_token ELSE frame.last_lease_token END,
                last_outcome=CASE WHEN v_expired THEN 'lease_expired' ELSE frame.last_outcome END,updated_at=v_now
            WHERE frame.frame_id=v_frame.frame_id;
            CONTINUE;
        END IF;
        v_generation := v_frame.generation+1;
        v_token := pg_catalog.gen_random_uuid();
        v_prefix := aap_assets_v1._video_input_attempt_prefix(v_input.input_id,v_generation,v_token);
        v_key := v_prefix||'frames/'||v_frame.frame_index::text||'/w'||
            (v_input.sampling->>'width')||'-'||(v_input.sampling->>'format');
        v_expires := v_now+pg_catalog.make_interval(secs=>p_lease_seconds);
        INSERT INTO public.asset_accepted_video_frame_attempts(
            frame_id,input_id,generation,lease_token,prefix,status,lease_expires_at
        ) VALUES(v_frame.frame_id,v_input.input_id,v_generation,v_token,v_prefix,'leased',v_expires);
        UPDATE public.asset_accepted_video_frames AS frame SET status='materializing',generation=v_generation,
            attempt_count=frame.attempt_count+1,lease_owner=p_worker_id,lease_token=v_token,
            lease_expires_at=v_expires,error_code=NULL,
            last_lease_token=CASE WHEN v_expired THEN v_attempt.lease_token ELSE frame.last_lease_token END,
            last_outcome=CASE WHEN v_expired THEN 'lease_expired' ELSE frame.last_outcome END,updated_at=v_now
        WHERE frame.frame_id=v_frame.frame_id RETURNING * INTO v_frame;
        v_claim := pg_catalog.jsonb_build_object(
            'frame',aap_assets_v1._video_input_frame(v_frame),'generation',v_generation,
            'lease_token',v_token,'lease_expires_at',v_expires,'blob_key',v_key
        );
        RETURN aap_assets_v1._video_input_reply(pg_catalog.jsonb_build_object('claim',v_claim),NULL);
    END LOOP;
    RETURN aap_assets_v1._video_input_reply(pg_catalog.jsonb_build_object('claim',NULL),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._video_input_reply(NULL,'frame_busy');
WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS v_message=MESSAGE_TEXT;
    IF v_message LIKE 'video_input_abort:%' THEN
        RETURN aap_assets_v1._video_input_reply(NULL,pg_catalog.substr(v_message,19));
    END IF;
    RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.claim_accepted_video_frame(uuid,uuid,integer) FROM PUBLIC;
