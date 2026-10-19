-- Retained video materialization and frame rendering are worker-owned leases.
CREATE FUNCTION aap_assets_v1.claim_accepted_video_input(
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
            IF v_source_code IN ('source_missing','source_changed','source_not_ready','not_video',
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

CREATE FUNCTION aap_assets_v1.heartbeat_accepted_video_input(
    p_input_id uuid,p_generation bigint,p_lease_token uuid,p_lease_seconds integer
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_input public.asset_accepted_video_inputs%ROWTYPE;
    v_attempt public.asset_accepted_video_input_attempts%ROWTYPE;
    v_manifest public.asset_accepted_video_manifests%ROWTYPE;
    v_reply jsonb; v_code text; v_source_code text; v_expires timestamptz;
BEGIN
    IF p_input_id IS NULL OR p_generation IS NULL OR p_generation<=0 OR p_lease_token IS NULL
       OR p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 60 AND 7200 THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'invalid_request');
    END IF;
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
    WHERE input.input_id=p_input_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._video_input_reply(NULL,'input_missing'); END IF;
    IF v_input.status<>'materializing' OR (v_input.generation,v_input.lease_token)
        IS DISTINCT FROM (p_generation,p_lease_token)
       OR v_input.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'lease_lost');
    END IF;
    SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_input_attempts AS attempt
    WHERE attempt.input_id=p_input_id AND attempt.generation=p_generation
      AND attempt.lease_token=p_lease_token AND attempt.status='leased' FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_attempt.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'lease_lost');
    END IF;
    SELECT manifest.* INTO v_manifest FROM public.asset_accepted_video_manifests AS manifest
    WHERE manifest.job_id=v_input.job_id FOR SHARE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._video_input_reply(NULL,'manifest_missing'); END IF;
    IF v_manifest.status<>'preparing' THEN RETURN aap_assets_v1._video_input_reply(NULL,'manifest_not_ready'); END IF;
    v_reply := aap_assets_v1._video_input_job(v_manifest,v_input.task_id,true);
    v_code := aap_assets_v1._video_input_code(v_reply,'aap-assets-video-input-execution/v1');
    IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_code); END IF;
    v_source_code := aap_assets_v1._video_input_current_source(v_input,v_input.request_actor_id,true,true);
    IF v_source_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_source_code); END IF;
    v_expires := pg_catalog.clock_timestamp()+pg_catalog.make_interval(secs=>p_lease_seconds);
    UPDATE public.asset_accepted_video_input_attempts AS attempt SET lease_expires_at=v_expires
    WHERE attempt.input_id=p_input_id AND attempt.generation=p_generation;
    UPDATE public.asset_accepted_video_inputs AS input SET lease_expires_at=v_expires,updated_at=pg_catalog.clock_timestamp()
    WHERE input.input_id=p_input_id;
    RETURN aap_assets_v1._video_input_reply(pg_catalog.jsonb_build_object(
        'input_id',p_input_id,'generation',p_generation,'lease_token',p_lease_token,'lease_expires_at',v_expires
    ),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._video_input_reply(NULL,'input_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.heartbeat_accepted_video_input(uuid,bigint,uuid,integer) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.stage_accepted_video_times(
    p_input_id uuid,p_generation bigint,p_lease_token uuid,p_cursor bigint,p_entries jsonb
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_input public.asset_accepted_video_inputs%ROWTYPE;
    v_attempt public.asset_accepted_video_input_attempts%ROWTYPE;
    v_manifest public.asset_accepted_video_manifests%ROWTYPE;
    v_reply jsonb; v_code text; v_source_code text; v_entry jsonb; v_existing public.asset_accepted_video_times%ROWTYPE;
    v_ordinal bigint; v_index bigint; v_pts bigint; v_keyframe boolean; v_offset integer;
    v_count bigint; v_bytes bigint; v_next bigint; v_known public.video_frame_indices%ROWTYPE;
    v_projected_count bigint; v_projected_bytes bigint;
    v_message text;
BEGIN
    IF p_input_id IS NULL OR p_generation IS NULL OR p_generation<=0 OR p_lease_token IS NULL
       OR p_cursor IS NULL OR p_cursor<0 OR p_cursor>9007199254740991
       OR pg_catalog.jsonb_typeof(p_entries) IS DISTINCT FROM 'array' THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'invalid_request');
    END IF;
    IF pg_catalog.jsonb_array_length(p_entries) NOT BETWEEN 1 AND 100
       OR pg_catalog.octet_length(pg_catalog.convert_to(p_entries::text,'UTF8'))>65536 THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'invalid_request');
    END IF;
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
    WHERE input.input_id=p_input_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._video_input_reply(NULL,'input_missing'); END IF;
    IF v_input.status<>'materializing' OR (v_input.generation,v_input.lease_token)
        IS DISTINCT FROM (p_generation,p_lease_token) OR v_input.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'lease_lost');
    END IF;
    SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_input_attempts AS attempt
    WHERE attempt.input_id=p_input_id AND attempt.generation=p_generation
      AND attempt.lease_token=p_lease_token AND attempt.status='leased' FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_attempt.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'lease_lost');
    END IF;
    SELECT manifest.* INTO v_manifest FROM public.asset_accepted_video_manifests AS manifest
    WHERE manifest.job_id=v_input.job_id FOR SHARE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._video_input_reply(NULL,'manifest_missing'); END IF;
    IF v_manifest.status<>'preparing' THEN RETURN aap_assets_v1._video_input_reply(NULL,'manifest_not_ready'); END IF;
    v_reply := aap_assets_v1._video_input_job(v_manifest,v_input.task_id,true);
    v_code := aap_assets_v1._video_input_code(v_reply,'aap-assets-video-input-execution/v1');
    IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_code); END IF;
    v_source_code := aap_assets_v1._video_input_current_source(v_input,v_input.request_actor_id,true,true);
    IF v_source_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_source_code); END IF;
    SELECT count(*),COALESCE(sum(pg_catalog.octet_length(pg_catalog.convert_to(
        aap_assets_v1._video_input_entry_text(times.ordinal,times.frame_index,times.pts_ms,times.is_keyframe),'UTF8'))),0)
    INTO v_projected_count,v_projected_bytes FROM public.asset_accepted_video_times AS times
    WHERE times.input_id=p_input_id AND times.generation=p_generation;
    IF v_projected_count<>v_input.staged_count OR v_projected_bytes<>v_input.staged_bytes THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'input_corrupt');
    END IF;
    IF v_projected_count>0 AND ((SELECT MIN(times.ordinal) FROM public.asset_accepted_video_times AS times
            WHERE times.input_id=p_input_id AND times.generation=p_generation)<>1
       OR (SELECT MAX(times.ordinal) FROM public.asset_accepted_video_times AS times
            WHERE times.input_id=p_input_id AND times.generation=p_generation)<>v_projected_count) THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'input_corrupt');
    END IF;
    v_offset := 0;
    FOR v_entry IN SELECT value FROM pg_catalog.jsonb_array_elements(p_entries) LOOP
        v_offset := v_offset+1;
        IF NOT aap_assets_v1._video_input_keys(v_entry,ARRAY['ordinal','frame_index','pts_ms','is_keyframe'])
           OR NOT aap_assets_v1._video_input_int(v_entry->'ordinal',1,2147483647)
           OR NOT aap_assets_v1._video_input_int(v_entry->'frame_index',0,2147483647)
           OR NOT aap_assets_v1._video_input_int(v_entry->'pts_ms',-9007199254740991,9007199254740991)
           OR pg_catalog.jsonb_typeof(v_entry->'is_keyframe') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._video_input_reply(NULL,'invalid_request');
        END IF;
        v_ordinal := (v_entry->>'ordinal')::bigint;
        v_index := (v_entry->>'frame_index')::bigint;
        v_pts := (v_entry->>'pts_ms')::bigint;
        v_keyframe := (v_entry->>'is_keyframe')::boolean;
        IF v_ordinal<>p_cursor+v_offset OR v_ordinal> (v_input.sampling->>'max_frames')::bigint
           OR v_index<>(v_ordinal-1)*(v_input.sampling->>'step')::bigint THEN
            RETURN aap_assets_v1._video_input_reply(NULL,'timebase_incomplete');
        END IF;
        IF (v_input.source->>'timetable_revision')::bigint>0 THEN
            SELECT frame.* INTO v_known FROM public.video_frame_indices AS frame
            WHERE frame.dataset_item_id=(v_input.source->>'dataset_item_id')::uuid
              AND frame.frame_index=v_index;
            IF NOT FOUND OR (v_known.pts_ms::bigint,v_known.is_keyframe)
                IS DISTINCT FROM (v_pts,v_keyframe) THEN
                RETURN aap_assets_v1._video_input_reply(NULL,'timebase_changed');
            END IF;
        END IF;
        SELECT times.* INTO v_existing FROM public.asset_accepted_video_times AS times
        WHERE times.input_id=p_input_id AND times.generation=p_generation AND times.ordinal=v_ordinal;
        IF FOUND THEN
            IF v_existing.lease_token IS DISTINCT FROM p_lease_token
               OR (v_existing.frame_index,v_existing.pts_ms,v_existing.is_keyframe)
                IS DISTINCT FROM (v_index::integer,v_pts,v_keyframe) THEN
                RETURN aap_assets_v1._video_input_reply(NULL,'timebase_changed');
            END IF;
        ELSE
            IF v_ordinal<>v_projected_count+1 THEN
                RETURN aap_assets_v1._video_input_reply(NULL,'manifest_cursor_invalid');
            END IF;
            v_projected_count := v_projected_count+1;
            v_projected_bytes := v_projected_bytes+pg_catalog.octet_length(pg_catalog.convert_to(
                aap_assets_v1._video_input_entry_text(v_ordinal,v_index::integer,v_pts,v_keyframe),'UTF8'));
        END IF;
    END LOOP;
    IF v_projected_bytes>67108864 OR v_projected_count>2147483647 THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'resource_budget_exceeded');
    END IF;
    FOR v_entry IN SELECT value FROM pg_catalog.jsonb_array_elements(p_entries) LOOP
        v_ordinal := (v_entry->>'ordinal')::bigint;
        v_index := (v_entry->>'frame_index')::bigint;
        v_pts := (v_entry->>'pts_ms')::bigint;
        v_keyframe := (v_entry->>'is_keyframe')::boolean;
        PERFORM 1 FROM public.asset_accepted_video_times AS times
        WHERE times.input_id=p_input_id AND times.generation=p_generation AND times.ordinal=v_ordinal;
        IF NOT FOUND THEN
            INSERT INTO public.asset_accepted_video_times(input_id,generation,lease_token,ordinal,frame_index,pts_ms,is_keyframe)
            VALUES(p_input_id,p_generation,p_lease_token,v_ordinal,v_index::integer,v_pts,v_keyframe);
        END IF;
    END LOOP;
    SELECT count(*),COALESCE(sum(pg_catalog.octet_length(pg_catalog.convert_to(
        aap_assets_v1._video_input_entry_text(times.ordinal,times.frame_index,times.pts_ms,times.is_keyframe),'UTF8'))),0)
    INTO v_count,v_bytes FROM public.asset_accepted_video_times AS times
    WHERE times.input_id=p_input_id AND times.generation=p_generation;
    IF v_count IS DISTINCT FROM v_projected_count OR v_bytes IS DISTINCT FROM v_projected_bytes THEN
        PERFORM aap_assets_v1._video_input_abort('input_corrupt');
    END IF;
    UPDATE public.asset_accepted_video_inputs AS input SET staged_count=v_count,staged_bytes=v_bytes,
        updated_at=pg_catalog.clock_timestamp() WHERE input.input_id=p_input_id;
    v_next := p_cursor+pg_catalog.jsonb_array_length(p_entries);
    RETURN aap_assets_v1._video_input_reply(pg_catalog.jsonb_build_object(
        'input_id',p_input_id,'generation',p_generation,'lease_token',p_lease_token,'next_cursor',v_next
    ),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
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
REVOKE ALL ON FUNCTION aap_assets_v1.stage_accepted_video_times(uuid,bigint,uuid,bigint,jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.finish_accepted_video_input(
    p_input_id uuid,p_generation bigint,p_lease_token uuid,p_plan jsonb
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_input public.asset_accepted_video_inputs%ROWTYPE;
    v_attempt public.asset_accepted_video_input_attempts%ROWTYPE;
    v_manifest public.asset_accepted_video_manifests%ROWTYPE;
    v_reply jsonb; v_code text; v_source_code text;
    v_plan jsonb; v_blob jsonb; v_prefix text;
    v_frame_count bigint; v_max_frames bigint; v_step bigint; v_expected bigint;
    v_rows bigint; v_bytes bigint; v_seed text; v_entries text; v_timebase text; v_digest text;
    v_entry record; v_known public.video_frame_indices%ROWTYPE;
    v_message text;
BEGIN
    IF p_plan #> '{input,blob,version_id}' = 'null'::jsonb
       OR p_plan #>> '{input,blob,version_id}' IN ('','null')
       OR pg_catalog.btrim(p_plan #>> '{input,blob,version_id}')='' THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'versioning_required');
    END IF;
    IF p_input_id IS NULL OR p_generation IS NULL OR p_generation<=0 OR p_lease_token IS NULL
       OR NOT aap_assets_v1._video_input_plan_shape_valid(p_plan)
       OR p_plan #>> '{input,input_id}' IS DISTINCT FROM p_input_id::text
       OR p_plan #>> '{input,blob,key}' IS DISTINCT FROM
            aap_assets_v1._video_input_attempt_prefix(p_input_id,p_generation,p_lease_token)||'source' THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'invalid_request');
    END IF;
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
    WHERE input.input_id=p_input_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._video_input_reply(NULL,'input_missing'); END IF;
    IF (p_plan #>> '{input,created_at}')::timestamptz IS DISTINCT FROM v_input.created_at THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'input_scope_mismatch'); END IF;
    v_plan := pg_catalog.jsonb_set(p_plan,'{input,created_at}',pg_catalog.to_jsonb(v_input.created_at),false);
    IF v_plan #>> '{input,job_id}' IS DISTINCT FROM v_input.job_id::text
       OR v_plan #>> '{input,actor_id}' IS DISTINCT FROM v_input.actor_id::text
       OR v_plan #> '{input,task}' IS DISTINCT FROM v_input.task
       OR v_plan #> '{input,source}' IS DISTINCT FROM v_input.source
       OR v_plan -> 'sampling' IS DISTINCT FROM v_input.sampling
       OR NOT aap_assets_v1._video_input_blob_valid(v_plan #> '{input,blob}',false) THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'input_scope_mismatch');
    END IF;
    v_blob := v_plan #> '{input,blob}';
    IF pg_catalog.char_length(v_blob ->> 'version_id') NOT BETWEEN 1 AND 255
       OR pg_catalog.btrim(v_blob ->> 'version_id')='' OR v_blob ->> 'version_id'='null'
       OR (v_blob ->> 'byte_size')::bigint IS DISTINCT FROM
            (v_input.source #>> '{object_identity,byte_size}')::bigint THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'input_corrupt');
    END IF;
    v_frame_count := (v_plan #>> '{input,video,frame_count}')::bigint;
    v_max_frames := (v_plan #>> '{sampling,max_frames}')::bigint;
    v_step := (v_plan #>> '{sampling,step}')::bigint;
    v_expected := LEAST(v_max_frames,(v_frame_count+v_step-1)/v_step);
    IF v_expected<1 OR v_expected>2147483647
       OR (v_expected-1)*v_step>2147483647
       OR (v_plan ->> 'selected_count')::bigint IS DISTINCT FROM v_expected THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'timebase_incomplete');
    END IF;
    SELECT count(*),COALESCE(sum(pg_catalog.octet_length(pg_catalog.convert_to(
        aap_assets_v1._video_input_entry_text(times.ordinal,times.frame_index,times.pts_ms,times.is_keyframe),'UTF8'))),0)
    INTO v_rows,v_bytes FROM public.asset_accepted_video_times AS times
    WHERE times.input_id=p_input_id AND times.generation=p_generation;
    IF v_bytes>67108864 OR v_bytes<>v_input.staged_bytes OR v_rows<>v_input.staged_count THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'resource_budget_exceeded');
    END IF;
    IF EXISTS (SELECT 1 FROM public.asset_accepted_video_times AS times
        WHERE times.input_id=p_input_id AND times.generation=p_generation AND times.lease_token<>p_lease_token) THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'timebase_changed');
    END IF;
    IF v_rows<>v_expected OR NOT EXISTS (
        SELECT 1 FROM public.asset_accepted_video_times AS times
        WHERE times.input_id=p_input_id AND times.generation=p_generation AND times.ordinal=1
    ) OR NOT EXISTS (
        SELECT 1 FROM public.asset_accepted_video_times AS times
        WHERE times.input_id=p_input_id AND times.generation=p_generation AND times.ordinal=v_expected
    ) THEN RETURN aap_assets_v1._video_input_reply(NULL,'timebase_incomplete'); END IF;
    IF EXISTS (
        SELECT 1 FROM public.asset_accepted_video_times AS times
        WHERE times.input_id=p_input_id AND times.generation=p_generation
          AND (times.frame_index::bigint<>(times.ordinal-1)*v_step OR times.ordinal>v_expected)
    ) THEN RETURN aap_assets_v1._video_input_reply(NULL,'timebase_incomplete'); END IF;
    SELECT pg_catalog.string_agg(
        aap_assets_v1._video_input_entry_text(times.ordinal,times.frame_index,times.pts_ms,times.is_keyframe),''
        ORDER BY times.ordinal
    ) INTO v_entries FROM public.asset_accepted_video_times AS times
    WHERE times.input_id=p_input_id AND times.generation=p_generation;
    v_seed := pg_catalog.array_to_string(ARRAY[
        'aap-assets-video-inputs/v1',v_input.input_id::text,v_input.job_id::text,v_input.actor_id::text,
        v_input.task->>'task_id',v_input.task->>'project_id',v_input.task->>'task_version',
        v_input.task->>'task_source_generation',v_input.task->>'task_source_snapshot',
        v_input.source->>'dataset_id',v_input.source->>'dataset_item_id',v_input.source->>'source_generation',
        v_input.source->>'source_snapshot',v_input.source->>'timetable_revision',v_blob->>'sha256',
        v_blob->>'byte_size',v_plan #>> '{input,video,frame_count}',v_plan #>> '{input,video,width}',
        v_plan #>> '{input,video,height}',v_plan #>> '{input,video,duration_ms}',
        v_plan #>> '{input,video,video_stream}',v_plan #>> '{sampling,max_frames}',
        v_plan #>> '{sampling,step}',v_plan #>> '{sampling,width}',v_plan #>> '{sampling,format}'
    ],E'\n')||E'\n';
    v_timebase := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to('timebase'||E'\n'||v_seed||COALESCE(v_entries,''),'UTF8')),'hex');
    v_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to('plan'||E'\n'||v_seed||v_timebase||E'\n','UTF8')),'hex');
    IF v_timebase IS DISTINCT FROM v_plan->>'timebase_digest'
       OR v_digest IS DISTINCT FROM v_plan->>'plan_digest' THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'input_corrupt');
    END IF;
    IF (v_input.source->>'timetable_revision')::bigint>0 THEN
        FOR v_entry IN SELECT times.frame_index,times.pts_ms,times.is_keyframe
            FROM public.asset_accepted_video_times AS times
            WHERE times.input_id=p_input_id AND times.generation=p_generation ORDER BY times.ordinal
        LOOP
            SELECT frame.* INTO v_known FROM public.video_frame_indices AS frame
            WHERE frame.dataset_item_id=(v_input.source->>'dataset_item_id')::uuid
              AND frame.frame_index=v_entry.frame_index;
            IF NOT FOUND OR (v_known.pts_ms::bigint,v_known.is_keyframe)
                IS DISTINCT FROM (v_entry.pts_ms,v_entry.is_keyframe) THEN
                RETURN aap_assets_v1._video_input_reply(NULL,'timebase_changed');
            END IF;
        END LOOP;
    END IF;
    SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_input_attempts AS attempt
    WHERE attempt.input_id=p_input_id AND attempt.generation=p_generation
      AND attempt.lease_token=p_lease_token AND attempt.status='leased' FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_attempt.lease_expires_at<=pg_catalog.clock_timestamp()
       OR v_input.status<>'materializing' OR (v_input.generation,v_input.lease_token)
            IS DISTINCT FROM (p_generation,p_lease_token)
       OR v_input.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        IF v_input.status='ready' AND v_input.generation=p_generation
           AND v_input.last_lease_token=p_lease_token AND v_input.last_outcome='ready'
           AND v_input.plan IS NOT DISTINCT FROM v_plan THEN
            SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_input_attempts AS attempt
            WHERE attempt.input_id=p_input_id AND attempt.generation=p_generation
              AND attempt.lease_token=p_lease_token AND attempt.status='ready';
            IF NOT FOUND OR v_attempt.prefix IS DISTINCT FROM
                aap_assets_v1._video_input_attempt_prefix(p_input_id,p_generation,p_lease_token) THEN
                RETURN aap_assets_v1._video_input_reply(NULL,'input_corrupt');
            END IF;
            SELECT manifest.* INTO v_manifest FROM public.asset_accepted_video_manifests AS manifest
            WHERE manifest.job_id=v_input.job_id;
            IF NOT FOUND THEN RETURN aap_assets_v1._video_input_reply(NULL,'manifest_missing'); END IF;
            v_reply := aap_assets_v1._video_input_job(v_manifest,v_input.task_id,false);
            v_code := aap_assets_v1._video_input_code(v_reply,'aap-assets-video-input-execution/v1');
            IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_code); END IF;
            v_source_code := aap_assets_v1._video_input_current_source(v_input,v_input.request_actor_id,false,false);
            IF v_source_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_source_code); END IF;
            RETURN aap_assets_v1._video_input_reply(aap_assets_v1._video_input_record(v_input),NULL);
        END IF;
        RETURN aap_assets_v1._video_input_reply(NULL,'lease_lost');
    END IF;
    SELECT manifest.* INTO v_manifest FROM public.asset_accepted_video_manifests AS manifest
    WHERE manifest.job_id=v_input.job_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_manifest.status<>'preparing' THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'manifest_not_ready');
    END IF;
    v_reply := aap_assets_v1._video_input_job(v_manifest,v_input.task_id,true);
    v_code := aap_assets_v1._video_input_code(v_reply,'aap-assets-video-input-execution/v1');
    IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_code); END IF;
    v_source_code := aap_assets_v1._video_input_current_source(v_input,v_input.request_actor_id,true,true);
    IF v_source_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_source_code); END IF;
    v_prefix := v_attempt.prefix;
    IF NOT aap_assets_v1._video_input_prefix_valid(v_prefix)
       OR v_blob->>'key' IS DISTINCT FROM v_prefix||'source' THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'input_scope_mismatch');
    END IF;
    UPDATE public.asset_accepted_video_inputs AS input SET status='ready',plan=v_plan,error_code=NULL,
        lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,last_lease_token=p_lease_token,
        last_outcome='ready',retire_after=NULL,retention_checked_at=NULL,updated_at=pg_catalog.clock_timestamp()
    WHERE input.input_id=p_input_id RETURNING * INTO v_input;
    UPDATE public.asset_accepted_video_input_attempts AS attempt SET status='ready',completed_at=pg_catalog.clock_timestamp()
    WHERE attempt.input_id=p_input_id AND attempt.generation=p_generation;
    PERFORM aap_assets_v1._video_input_complete_member(p_input_id,'ready',NULL);
    RETURN aap_assets_v1._video_input_reply(aap_assets_v1._video_input_record(v_input),NULL);
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
REVOKE ALL ON FUNCTION aap_assets_v1.finish_accepted_video_input(uuid,bigint,uuid,jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.fail_accepted_video_input(
    p_input_id uuid,p_generation bigint,p_lease_token uuid,p_error_code text
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_input public.asset_accepted_video_inputs%ROWTYPE;
    v_attempt public.asset_accepted_video_input_attempts%ROWTYPE;
    v_manifest public.asset_accepted_video_manifests%ROWTYPE;
    v_reply jsonb; v_code text; v_source_code text; v_message text;
BEGIN
    IF p_input_id IS NULL OR p_generation IS NULL OR p_generation<=0 OR p_lease_token IS NULL
       OR NOT aap_assets_v1._video_input_error_valid(p_error_code)
       OR p_error_code IN ('input_busy','source_busy','job_busy','manifest_busy','task_busy','account_busy',
           'operation_busy','lease_lost','reference_busy') THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'invalid_request');
    END IF;
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
    WHERE input.input_id=p_input_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._video_input_reply(NULL,'input_missing'); END IF;
    IF v_input.status='failed' AND v_input.generation=p_generation AND v_input.last_lease_token=p_lease_token
       AND v_input.last_outcome='failed' AND v_input.error_code=p_error_code THEN
        SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_input_attempts AS attempt
        WHERE attempt.input_id=p_input_id AND attempt.generation=p_generation AND attempt.lease_token=p_lease_token
          AND attempt.status='failed';
        IF NOT FOUND OR v_attempt.prefix IS DISTINCT FROM
            aap_assets_v1._video_input_attempt_prefix(p_input_id,p_generation,p_lease_token) THEN
            RETURN aap_assets_v1._video_input_reply(NULL,'input_corrupt');
        END IF;
        RETURN aap_assets_v1._video_input_reply(aap_assets_v1._video_input_record(v_input),NULL);
    END IF;
    IF v_input.status<>'materializing' OR (v_input.generation,v_input.lease_token)
       IS DISTINCT FROM (p_generation,p_lease_token) OR v_input.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'lease_lost');
    END IF;
    SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_input_attempts AS attempt
    WHERE attempt.input_id=p_input_id AND attempt.generation=p_generation AND attempt.lease_token=p_lease_token
      AND attempt.status='leased' FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_attempt.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'lease_lost');
    END IF;
    SELECT manifest.* INTO v_manifest FROM public.asset_accepted_video_manifests AS manifest
    WHERE manifest.job_id=v_input.job_id FOR SHARE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._video_input_reply(NULL,'manifest_missing'); END IF;
    IF v_manifest.status<>'preparing' THEN RETURN aap_assets_v1._video_input_reply(NULL,'manifest_not_ready'); END IF;
    v_reply := aap_assets_v1._video_input_job(v_manifest,v_input.task_id,true);
    v_code := aap_assets_v1._video_input_code(v_reply,'aap-assets-video-input-execution/v1');
    IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_code); END IF;
    v_source_code := aap_assets_v1._video_input_current_source(v_input,v_input.request_actor_id,true,true);
    IF v_source_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_source_code); END IF;
    PERFORM aap_assets_v1._video_input_queue_prefix(
        v_attempt.prefix,'input_failed',GREATEST(pg_catalog.clock_timestamp(),v_attempt.lease_expires_at)+INTERVAL '24 hours'
    );
    UPDATE public.asset_accepted_video_input_attempts AS attempt SET status='failed',completed_at=pg_catalog.clock_timestamp()
    WHERE attempt.input_id=p_input_id AND attempt.generation=p_generation;
    UPDATE public.asset_accepted_video_inputs AS input SET status='failed',error_code=p_error_code,
        lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,last_lease_token=p_lease_token,
        last_outcome='failed',retire_after=NULL,updated_at=pg_catalog.clock_timestamp()
    WHERE input.input_id=p_input_id RETURNING * INTO v_input;
    PERFORM aap_assets_v1._video_input_complete_member(p_input_id,'failed',p_error_code);
    RETURN aap_assets_v1._video_input_reply(aap_assets_v1._video_input_record(v_input),NULL);
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
REVOKE ALL ON FUNCTION aap_assets_v1.fail_accepted_video_input(uuid,bigint,uuid,text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.claim_accepted_video_frame(
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
            IF v_source_code IN ('task_not_visible','permission_denied','source_missing','source_changed',
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
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.heartbeat_accepted_video_frame(
    p_frame_id uuid,p_generation bigint,p_lease_token uuid,p_lease_seconds integer
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_frame public.asset_accepted_video_frames%ROWTYPE;
    v_attempt public.asset_accepted_video_frame_attempts%ROWTYPE;
    v_input public.asset_accepted_video_inputs%ROWTYPE;
    v_manifest public.asset_accepted_video_manifests%ROWTYPE;
    v_time public.asset_accepted_video_times%ROWTYPE;
    v_reply jsonb; v_code text; v_source_code text; v_expires timestamptz;
BEGIN
    IF p_frame_id IS NULL OR p_generation IS NULL OR p_generation<=0 OR p_lease_token IS NULL
       OR p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 60 AND 7200 THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'invalid_request');
    END IF;
    SELECT frame.* INTO v_frame FROM public.asset_accepted_video_frames AS frame
    WHERE frame.frame_id=p_frame_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._video_input_reply(NULL,'frame_missing'); END IF;
    IF v_frame.status<>'materializing' OR (v_frame.generation,v_frame.lease_token)
       IS DISTINCT FROM (p_generation,p_lease_token) OR v_frame.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'lease_lost');
    END IF;
    SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_frame_attempts AS attempt
    WHERE attempt.frame_id=p_frame_id AND attempt.generation=p_generation AND attempt.lease_token=p_lease_token
      AND attempt.status='leased' FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_attempt.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'lease_lost');
    END IF;
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
    WHERE input.input_id=v_frame.input_id FOR SHARE NOWAIT;
    IF NOT FOUND OR v_input.status<>'ready' OR v_input.plan->>'plan_digest' IS DISTINCT FROM v_frame.plan_digest THEN
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
    SELECT times.* INTO v_time FROM public.asset_accepted_video_times AS times
    WHERE times.input_id=v_input.input_id AND times.generation=v_input.generation
      AND times.ordinal=v_frame.ordinal AND times.lease_token=v_input.last_lease_token;
    IF NOT FOUND OR (v_time.frame_index,v_time.pts_ms,v_time.is_keyframe)
        IS DISTINCT FROM (v_frame.frame_index,v_frame.pts_ms,v_frame.is_keyframe) THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'timebase_changed');
    END IF;
    v_reply := aap_assets_v1._video_input_job(v_manifest,v_input.task_id,false);
    v_code := aap_assets_v1._video_input_code(v_reply,'aap-assets-video-input-execution/v1');
    IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_code); END IF;
    v_source_code := aap_assets_v1._video_input_current_source(v_input,v_frame.request_actor_id,true,false);
    IF v_source_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_source_code); END IF;
    v_expires := pg_catalog.clock_timestamp()+pg_catalog.make_interval(secs=>p_lease_seconds);
    UPDATE public.asset_accepted_video_frame_attempts AS attempt SET lease_expires_at=v_expires
    WHERE attempt.frame_id=p_frame_id AND attempt.generation=p_generation;
    UPDATE public.asset_accepted_video_frames AS frame SET lease_expires_at=v_expires,updated_at=pg_catalog.clock_timestamp()
    WHERE frame.frame_id=p_frame_id;
    RETURN aap_assets_v1._video_input_reply(pg_catalog.jsonb_build_object(
        'frame_id',p_frame_id,'generation',p_generation,'lease_token',p_lease_token,'lease_expires_at',v_expires
    ),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._video_input_reply(NULL,'frame_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.heartbeat_accepted_video_frame(uuid,bigint,uuid,integer) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.finish_accepted_video_frame(
    p_frame_id uuid,p_generation bigint,p_lease_token uuid,p_blob jsonb
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
    v_reply jsonb; v_code text; v_source_code text; v_suffix text; v_message text;
    v_replay boolean := false;
BEGIN
    IF pg_catalog.jsonb_typeof(p_blob)='object' AND p_blob ? 'version_id'
       AND (p_blob->'version_id'='null'::jsonb OR p_blob->>'version_id' IN ('','null')
           OR pg_catalog.btrim(p_blob->>'version_id')='') THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'versioning_required');
    END IF;
    IF p_frame_id IS NULL OR p_generation IS NULL OR p_generation<=0 OR p_lease_token IS NULL
       OR NOT aap_assets_v1._video_input_blob_valid(p_blob,true) THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'invalid_request');
    END IF;
    SELECT frame.* INTO v_frame FROM public.asset_accepted_video_frames AS frame
    WHERE frame.frame_id=p_frame_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._video_input_reply(NULL,'frame_missing'); END IF;
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
    WHERE input.input_id=v_frame.input_id FOR SHARE NOWAIT;
    IF NOT FOUND OR v_input.status<>'ready' OR v_input.plan->>'plan_digest' IS DISTINCT FROM v_frame.plan_digest THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'input_not_ready');
    END IF;
    IF v_frame.ordinal>(v_input.plan->>'selected_count')::bigint
       OR v_frame.frame_index::bigint<>(v_frame.ordinal-1)*(v_input.sampling->>'step')::bigint THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'timebase_changed');
    END IF;
    v_suffix := 'frames/'||v_frame.frame_index::text||'/w'||(v_input.sampling->>'width')||'-'||(v_input.sampling->>'format');
    IF v_frame.generation<>p_generation OR p_blob->>'key' IS DISTINCT FROM
        aap_assets_v1._video_input_attempt_prefix(v_input.input_id,p_generation,p_lease_token)||v_suffix
       OR NOT aap_assets_v1._video_input_blob_valid(p_blob,true) THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'input_scope_mismatch');
    END IF;
    IF v_frame.status='ready' AND v_frame.last_lease_token=p_lease_token AND v_frame.last_outcome='ready'
       AND v_frame.blob IS NOT DISTINCT FROM p_blob THEN
        v_replay := true;
    ELSIF v_frame.status<>'materializing' OR v_frame.lease_token IS DISTINCT FROM p_lease_token
       OR v_frame.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'lease_lost');
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
    IF v_replay THEN
        SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_frame_attempts AS attempt
        WHERE attempt.frame_id=p_frame_id AND attempt.generation=p_generation AND attempt.lease_token=p_lease_token
          AND attempt.status='ready';
        IF NOT FOUND OR v_attempt.prefix IS DISTINCT FROM
            aap_assets_v1._video_input_attempt_prefix(v_input.input_id,p_generation,p_lease_token) THEN
            RETURN aap_assets_v1._video_input_reply(NULL,'input_corrupt');
        END IF;
        v_reply := aap_assets_v1._video_input_job(v_manifest,v_input.task_id,false);
        v_code := aap_assets_v1._video_input_code(v_reply,'aap-assets-video-input-execution/v1');
        IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_code); END IF;
        v_source_code := aap_assets_v1._video_input_current_source(v_input,v_frame.request_actor_id,false,false);
        IF v_source_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_source_code); END IF;
        RETURN aap_assets_v1._video_input_reply(aap_assets_v1._video_input_frame(v_frame),NULL);
    END IF;
    SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_frame_attempts AS attempt
    WHERE attempt.frame_id=p_frame_id AND attempt.generation=p_generation AND attempt.lease_token=p_lease_token
      AND attempt.status='leased' FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_attempt.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'lease_lost');
    END IF;
    v_reply := aap_assets_v1._video_input_job(v_manifest,v_input.task_id,false);
    v_code := aap_assets_v1._video_input_code(v_reply,'aap-assets-video-input-execution/v1');
    IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_code); END IF;
    v_source_code := aap_assets_v1._video_input_current_source(v_input,v_frame.request_actor_id,true,false);
    IF v_source_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_source_code); END IF;
    UPDATE public.asset_accepted_video_frames AS frame SET status='ready',blob=p_blob,error_code=NULL,
        lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,last_lease_token=p_lease_token,
        last_outcome='ready',updated_at=pg_catalog.clock_timestamp()
    WHERE frame.frame_id=p_frame_id RETURNING * INTO v_frame;
    UPDATE public.asset_accepted_video_frame_attempts AS attempt SET status='ready',completed_at=pg_catalog.clock_timestamp()
    WHERE attempt.frame_id=p_frame_id AND attempt.generation=p_generation;
    RETURN aap_assets_v1._video_input_reply(aap_assets_v1._video_input_frame(v_frame),NULL);
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
REVOKE ALL ON FUNCTION aap_assets_v1.finish_accepted_video_frame(uuid,bigint,uuid,jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.fail_accepted_video_frame(
    p_frame_id uuid,p_generation bigint,p_lease_token uuid,p_error_code text
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_frame public.asset_accepted_video_frames%ROWTYPE;
    v_attempt public.asset_accepted_video_frame_attempts%ROWTYPE;
    v_input public.asset_accepted_video_inputs%ROWTYPE;
    v_manifest public.asset_accepted_video_manifests%ROWTYPE;
    v_time public.asset_accepted_video_times%ROWTYPE;
    v_reply jsonb; v_code text; v_source_code text; v_message text;
BEGIN
    IF p_frame_id IS NULL OR p_generation IS NULL OR p_generation<=0 OR p_lease_token IS NULL
       OR NOT aap_assets_v1._video_input_error_valid(p_error_code)
       OR p_error_code IN ('input_busy','frame_busy','source_busy','job_busy','manifest_busy','task_busy',
           'account_busy','operation_busy','lease_lost','reference_busy') THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'invalid_request');
    END IF;
    SELECT frame.* INTO v_frame FROM public.asset_accepted_video_frames AS frame
    WHERE frame.frame_id=p_frame_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._video_input_reply(NULL,'frame_missing'); END IF;
    IF v_frame.status='failed' AND v_frame.generation=p_generation AND v_frame.last_lease_token=p_lease_token
       AND v_frame.last_outcome='failed' AND v_frame.error_code=p_error_code THEN
        SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_frame_attempts AS attempt
        WHERE attempt.frame_id=p_frame_id AND attempt.generation=p_generation AND attempt.lease_token=p_lease_token
          AND attempt.status='failed';
        IF NOT FOUND OR v_attempt.prefix IS DISTINCT FROM
            aap_assets_v1._video_input_attempt_prefix(v_frame.input_id,p_generation,p_lease_token) THEN
            RETURN aap_assets_v1._video_input_reply(NULL,'input_corrupt');
        END IF;
        RETURN aap_assets_v1._video_input_reply(aap_assets_v1._video_input_frame(v_frame),NULL);
    END IF;
    IF v_frame.status<>'materializing' OR (v_frame.generation,v_frame.lease_token)
        IS DISTINCT FROM (p_generation,p_lease_token) OR v_frame.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'lease_lost');
    END IF;
    SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_frame_attempts AS attempt
    WHERE attempt.frame_id=p_frame_id AND attempt.generation=p_generation AND attempt.lease_token=p_lease_token
      AND attempt.status='leased' FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_attempt.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'lease_lost');
    END IF;
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
    WHERE input.input_id=v_frame.input_id FOR SHARE NOWAIT;
    IF NOT FOUND OR v_input.status<>'ready' OR v_input.plan->>'plan_digest' IS DISTINCT FROM v_frame.plan_digest THEN
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
    SELECT times.* INTO v_time FROM public.asset_accepted_video_times AS times
    WHERE times.input_id=v_input.input_id AND times.generation=v_input.generation
      AND times.ordinal=v_frame.ordinal AND times.lease_token=v_input.last_lease_token;
    IF NOT FOUND OR (v_time.frame_index,v_time.pts_ms,v_time.is_keyframe)
        IS DISTINCT FROM (v_frame.frame_index,v_frame.pts_ms,v_frame.is_keyframe) THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'timebase_changed');
    END IF;
    v_reply := aap_assets_v1._video_input_job(v_manifest,v_input.task_id,false);
    v_code := aap_assets_v1._video_input_code(v_reply,'aap-assets-video-input-execution/v1');
    IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_code); END IF;
    v_source_code := aap_assets_v1._video_input_current_source(v_input,v_frame.request_actor_id,true,false);
    IF v_source_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_source_code); END IF;
    PERFORM aap_assets_v1._video_input_queue_prefix(
        v_attempt.prefix,'frame_failed',GREATEST(pg_catalog.clock_timestamp(),v_attempt.lease_expires_at)+INTERVAL '24 hours'
    );
    UPDATE public.asset_accepted_video_frame_attempts AS attempt SET status='failed',completed_at=pg_catalog.clock_timestamp()
    WHERE attempt.frame_id=p_frame_id AND attempt.generation=p_generation;
    UPDATE public.asset_accepted_video_frames AS frame SET status='failed',error_code=p_error_code,
        lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,last_lease_token=p_lease_token,
        last_outcome='failed',updated_at=pg_catalog.clock_timestamp()
    WHERE frame.frame_id=p_frame_id RETURNING * INTO v_frame;
    RETURN aap_assets_v1._video_input_reply(aap_assets_v1._video_input_frame(v_frame),NULL);
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
REVOKE ALL ON FUNCTION aap_assets_v1.fail_accepted_video_frame(uuid,bigint,uuid,text) FROM PUBLIC;
