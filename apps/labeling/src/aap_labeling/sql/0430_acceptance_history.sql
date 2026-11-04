CREATE FUNCTION aap_labeling_v1._acceptance_history(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_task_id uuid := (p_request ->> 'task_id')::uuid;
    v_project_id uuid := (p_request ->> 'project_id')::uuid;
    v_actor_id uuid := (p_request ->> 'actor_id')::uuid;
    v_payload jsonb := p_request -> 'payload';
    v_action text := p_request ->> 'action';
    v_task record;
    v_row record;
    v_original record;
    v_existing record;
    v_account jsonb;
    v_before jsonb := '{}'::jsonb;
    v_after jsonb := '{}'::jsonb;
    v_versions jsonb := '{}'::jsonb;
    v_snapshot jsonb;
    v_target jsonb;
    v_current jsonb;
    v_key text;
    v_id uuid;
    v_ids uuid[];
    v_expiry timestamptz;
    v_idempotency_key text;
    v_digest text;
    v_result jsonb;
    v_replay boolean := false;
BEGIN
    IF jsonb_typeof(v_payload) IS DISTINCT FROM 'object'
       OR v_action NOT IN ('snapshot','record','restore') THEN
        RAISE EXCEPTION 'Invalid acceptance history command' USING ERRCODE = '22023';
    END IF;
    v_account := aap_identity_v1.lock_accounts_share(ARRAY[v_actor_id], true);
    IF NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(v_account -> 'accounts') AS account(value)
        WHERE account.value ->> 'id' = v_actor_id::text
          AND account.value -> 'is_active' = 'true'::jsonb
    ) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
            DETAIL = '{"status_code":403,"detail":{"reason":"actor_inactive"}}';
    END IF;
    SELECT task.id,task.project_id,task.batch_id,task.status,task.review_round_id,
           task.media_source_generation,task.dataset_item_id,task.file_path,task.file_type
    INTO v_task FROM public.tasks AS task
    WHERE task.id = v_task_id FOR UPDATE;
    IF NOT FOUND OR v_task.project_id IS DISTINCT FROM v_project_id
       OR v_task.batch_id IS DISTINCT FROM (p_request ->> 'expected_batch_id')::uuid
       OR v_task.status IS DISTINCT FROM p_request ->> 'expected_status'
       OR v_task.review_round_id IS DISTINCT FROM (p_request ->> 'expected_review_round_id')::uuid THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
            DETAIL = '{"status_code":409,"detail":{"reason":"task_target_changed"}}';
    END IF;

    IF v_action = 'snapshot' THEN
        SELECT annotation.id,annotation.task_id,annotation.project_id,annotation.user_id,
               annotation.source,annotation.annotation_type,annotation.tool_unit_id,
               annotation.class_name,annotation.geometry,annotation.confidence,
               annotation.parent_prediction_id,annotation.parent_annotation_id,annotation.track_id,
               annotation.was_cancelled,annotation.is_active,annotation.attributes,annotation.attributes_meta,
               annotation.is_locked,annotation.sensor_role,annotation.version,annotation.video_segment_id
        INTO v_row FROM public.annotations AS annotation
        WHERE annotation.id = (v_payload ->> 'annotation_id')::uuid
          AND annotation.task_id = v_task_id FOR UPDATE;
        IF NOT FOUND OR v_row.is_active IS NOT TRUE OR v_row.is_locked IS TRUE
           OR v_row.version IS DISTINCT FROM (v_payload ->> 'expected_version')::integer THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                DETAIL = '{"status_code":409,"detail":{"reason":"version_mismatch"}}';
        END IF;
        v_result := jsonb_build_object('snapshot', jsonb_build_object(
            'geometry',v_row.geometry,'class_name',v_row.class_name,
            'annotation_type',v_row.annotation_type,'tool_unit_id',v_row.tool_unit_id,
            'track_id',v_row.track_id,'confidence',v_row.confidence,
            'parent_prediction_id',v_row.parent_prediction_id,
            'user_id',v_row.user_id,'parent_annotation_id',v_row.parent_annotation_id,'source',v_row.source,
            'attributes',v_row.attributes,'attributes_meta',v_row.attributes_meta,
            'active',v_row.is_active,'version',v_row.version));
    ELSIF v_action = 'record' THEN
        v_idempotency_key := 'accept:' || encode(public.digest(convert_to(v_payload ->> 'acceptance_key','UTF8'),'sha256'),'hex');
        v_digest := v_payload ->> 'request_digest';
        IF v_digest !~ '^[0-9a-f]{64}$' OR jsonb_typeof(v_payload -> 'annotation_ids') IS DISTINCT FROM 'array'
           OR jsonb_array_length(v_payload -> 'annotation_ids') = 0
           OR jsonb_typeof(v_payload -> 'response') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'Invalid acceptance history record' USING ERRCODE = '22023';
        END IF;
        SELECT array_agg(value::uuid ORDER BY value::uuid) INTO v_ids
        FROM jsonb_array_elements_text(v_payload -> 'annotation_ids') AS ids(value);
        IF cardinality(v_ids) <> (SELECT count(DISTINCT id) FROM unnest(v_ids) AS ids(id)) THEN
            RAISE EXCEPTION 'Duplicate acceptance annotation' USING ERRCODE = '22023';
        END IF;
        v_expiry := transaction_timestamp() + interval '30 days';
        FOR v_row IN SELECT annotation.id,annotation.task_id,annotation.project_id,annotation.user_id,
            annotation.source,annotation.annotation_type,annotation.tool_unit_id,
            annotation.class_name,annotation.geometry,annotation.confidence,
            annotation.parent_prediction_id,annotation.parent_annotation_id,annotation.track_id,
            annotation.was_cancelled,annotation.is_active,annotation.attributes,annotation.attributes_meta,
            annotation.is_locked,annotation.sensor_role,annotation.version,annotation.video_segment_id
            FROM public.annotations AS annotation
            WHERE annotation.id = ANY(v_ids) ORDER BY annotation.id FOR UPDATE
        LOOP
            IF v_row.task_id IS DISTINCT FROM v_task_id
               OR (v_row.project_id IS NOT NULL AND v_row.project_id IS DISTINCT FROM v_project_id)
               OR v_row.is_active IS NOT TRUE
               OR v_row.parent_prediction_id IS DISTINCT FROM (v_payload ->> 'prediction_id')::uuid
               OR v_row.video_segment_id IS DISTINCT FROM (v_payload ->> 'video_segment_id')::uuid
               OR (v_payload -> 'before' -> v_row.id::text IS NULL
                   AND v_row.user_id IS DISTINCT FROM v_actor_id) THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                    DETAIL = '{"status_code":409,"detail":{"reason":"annotation_target_changed"}}';
            END IF;
            v_snapshot := jsonb_build_object(
                'geometry',v_row.geometry,'class_name',v_row.class_name,
                'annotation_type',v_row.annotation_type,'tool_unit_id',v_row.tool_unit_id,
                'track_id',v_row.track_id,'confidence',v_row.confidence,
                'parent_prediction_id',v_row.parent_prediction_id,
                'user_id',v_row.user_id,'parent_annotation_id',v_row.parent_annotation_id,'source',v_row.source,
                'attributes',v_row.attributes,'attributes_meta',v_row.attributes_meta,
                'active',true,'version',v_row.version);
            v_after := v_after || jsonb_build_object(v_row.id::text,v_snapshot);
            v_target := v_payload -> 'before' -> v_row.id::text;
            IF v_target IS NULL THEN
                v_target := jsonb_set(v_snapshot,'{active}','false'::jsonb);
            ELSE
                IF cardinality(v_ids) <> 1 OR (v_target ->> 'version')::integer <> v_row.version - 1
                   OR NOT EXISTS (SELECT 1 FROM public.mask_annotation_revisions AS revision
                       WHERE revision.annotation_id = v_row.id AND revision.task_id = v_task_id
                         AND revision.annotation_version = (v_target ->> 'version')::integer
                         AND revision.geometry = v_target -> 'geometry') THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                        DETAIL = '{"status_code":409,"detail":{"reason":"snapshot_unavailable"}}';
                END IF;
            END IF;
            v_before := v_before || jsonb_build_object(v_row.id::text,v_target);
            v_versions := v_versions || jsonb_build_object(v_row.id::text,v_row.version);
            IF v_row.geometry ->> 'type' IN ('raster_mask','video_mask','video_track_mask') THEN
                PERFORM public.record_mask_annotation_revision(v_project_id,v_task_id,v_row.id,v_row.version,v_row.geometry,v_row.source);
                UPDATE public.mask_annotation_revisions SET expires_at = GREATEST(expires_at,v_expiry)
                WHERE task_id = v_task_id AND annotation_id = v_row.id
                  AND annotation_version IN (v_row.version,(v_target ->> 'version')::integer);
            END IF;
        END LOOP;
        IF (SELECT count(*) FROM jsonb_object_keys(v_after)) <> cardinality(v_ids) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                DETAIL = '{"status_code":409,"detail":{"reason":"annotation_missing"}}';
        END IF;
        INSERT INTO public.annotation_operations (
            id,task_id,actor_id,kind,idempotency_key,request_digest,scope_fingerprint,
            source_versions,result_versions,report,status,response_json,created_at,completed_at
        ) VALUES (
            gen_random_uuid(),v_task_id,v_actor_id,'accept_prediction',v_idempotency_key,v_digest,v_digest,
            '{}'::jsonb,v_versions,jsonb_build_object('before',v_before,'after',v_after,
                'current_side','after','current_versions',v_versions,'video_segment_id',v_payload -> 'video_segment_id',
                'source_binding',jsonb_build_object(
                    'generation',v_task.media_source_generation,'dataset_item_id',v_task.dataset_item_id,
                    'file_path',v_task.file_path,'file_type',v_task.file_type)),
            'committed',v_payload -> 'response',transaction_timestamp(),transaction_timestamp()
        );
        v_result := jsonb_build_object('result_versions',v_versions,'restore_expires_at',v_expiry);
    ELSE
        v_idempotency_key := 'restore:' || encode(public.digest(convert_to(v_payload ->> 'idempotency_key','UTF8'),'sha256'),'hex');
        v_digest := v_payload ->> 'request_digest';
        v_key := 'accept:' || encode(public.digest(convert_to(v_payload ->> 'acceptance_key','UTF8'),'sha256'),'hex');
        SELECT operation.* INTO v_original FROM public.annotation_operations AS operation
        WHERE operation.task_id = v_task_id AND operation.actor_id = v_actor_id
          AND operation.idempotency_key = v_key AND operation.kind = 'accept_prediction' FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                DETAIL = '{"status_code":404,"detail":{"reason":"acceptance_not_found"}}';
        END IF;
        v_expiry := v_original.created_at + interval '30 days';
        IF v_expiry <= clock_timestamp() THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                DETAIL = '{"status_code":410,"detail":{"reason":"restore_expired"}}';
        END IF;
        IF v_original.report -> 'source_binding' IS DISTINCT FROM jsonb_build_object(
            'generation',v_task.media_source_generation,'dataset_item_id',v_task.dataset_item_id,
            'file_path',v_task.file_path,'file_type',v_task.file_type) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                DETAIL = '{"status_code":409,"detail":{"reason":"source_changed"}}';
        END IF;
        SELECT operation.* INTO v_existing FROM public.annotation_operations AS operation
        WHERE operation.task_id = v_task_id AND operation.actor_id = v_actor_id
          AND operation.idempotency_key = v_idempotency_key;
        IF FOUND THEN
            IF v_existing.kind <> 'restore_acceptance' OR v_existing.request_digest IS DISTINCT FROM v_digest THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                    DETAIL = '{"status_code":409,"detail":{"reason":"idempotency_conflict"}}';
            END IF;
            v_result := v_existing.response_json || jsonb_build_object('idempotent_replay',true);
        ELSE
            IF v_payload ->> 'target' NOT IN ('before','after')
               OR v_original.report -> 'current_versions' IS DISTINCT FROM v_payload -> 'expected_versions'
               OR v_original.report -> 'video_segment_id' IS DISTINCT FROM v_payload -> 'video_segment_id' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                    DETAIL = '{"status_code":409,"detail":{"reason":"version_mismatch"}}';
            END IF;
            v_target := v_original.report -> (v_payload ->> 'target');
            v_current := v_original.report -> (v_original.report ->> 'current_side');
            FOR v_key IN SELECT key FROM jsonb_object_keys(v_target) AS keys(key) ORDER BY key LOOP
                SELECT annotation.id,annotation.task_id,annotation.project_id,annotation.user_id,
                       annotation.source,annotation.annotation_type,annotation.tool_unit_id,
                       annotation.class_name,annotation.geometry,annotation.confidence,
                       annotation.parent_prediction_id,annotation.parent_annotation_id,annotation.track_id,
                       annotation.was_cancelled,annotation.is_active,annotation.attributes,annotation.attributes_meta,
                       annotation.is_locked,annotation.sensor_role,annotation.version,annotation.video_segment_id
                INTO v_row FROM public.annotations AS annotation
                WHERE annotation.id = v_key::uuid AND annotation.task_id = v_task_id FOR UPDATE;
                v_snapshot := v_current -> v_key;
                IF NOT FOUND OR (v_row.project_id IS NOT NULL AND v_row.project_id IS DISTINCT FROM v_project_id)
                   OR v_row.version IS DISTINCT FROM (v_payload -> 'expected_versions' ->> v_key)::integer
                   OR v_row.is_active IS DISTINCT FROM (v_snapshot ->> 'active')::boolean
                   OR v_row.geometry IS DISTINCT FROM v_snapshot -> 'geometry'
                   OR v_row.class_name IS DISTINCT FROM v_snapshot ->> 'class_name'
                   OR v_row.parent_prediction_id IS DISTINCT FROM (v_snapshot ->> 'parent_prediction_id')::uuid
                   OR v_row.user_id IS DISTINCT FROM (v_snapshot ->> 'user_id')::uuid
                   OR v_row.parent_annotation_id IS DISTINCT FROM (v_snapshot ->> 'parent_annotation_id')::uuid
                   OR v_row.source IS DISTINCT FROM v_snapshot ->> 'source'
                   OR v_row.annotation_type IS DISTINCT FROM v_snapshot ->> 'annotation_type'
                   OR v_row.tool_unit_id IS DISTINCT FROM v_snapshot ->> 'tool_unit_id'
                   OR v_row.track_id IS DISTINCT FROM v_snapshot ->> 'track_id'
                   OR v_row.confidence IS DISTINCT FROM (v_snapshot ->> 'confidence')::double precision
                   OR v_row.attributes IS DISTINCT FROM v_snapshot -> 'attributes'
                   OR v_row.attributes_meta IS DISTINCT FROM v_snapshot -> 'attributes_meta'
                   OR v_row.is_locked IS TRUE OR v_row.was_cancelled IS TRUE OR v_row.sensor_role IS NOT NULL
                   OR v_row.video_segment_id IS DISTINCT FROM (v_payload ->> 'video_segment_id')::uuid THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                        DETAIL = '{"status_code":409,"detail":{"reason":"version_mismatch"}}';
                END IF;
                IF (v_target -> v_key ->> 'active')::boolean IS FALSE AND EXISTS (
                    SELECT 1 FROM public.annotations AS child
                    WHERE child.parent_annotation_id = v_row.id AND child.is_active
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                        DETAIL = '{"status_code":409,"detail":{"reason":"active_children"}}';
                END IF;
                IF v_original.report ->> 'current_side' <> v_payload ->> 'target' THEN
                    v_snapshot := v_target -> v_key;
                    UPDATE public.annotations SET
                        geometry = v_snapshot -> 'geometry', class_name = v_snapshot ->> 'class_name',
                        annotation_type = v_snapshot ->> 'annotation_type', tool_unit_id = v_snapshot ->> 'tool_unit_id',
                        track_id = v_snapshot ->> 'track_id', confidence = (v_snapshot ->> 'confidence')::double precision,
                        parent_prediction_id = (v_snapshot ->> 'parent_prediction_id')::uuid,
                        attributes = v_snapshot -> 'attributes', attributes_meta = v_snapshot -> 'attributes_meta',
                        is_active = (v_snapshot ->> 'active')::boolean, version = version + 1,
                        updated_at = transaction_timestamp()
                    WHERE id = v_row.id RETURNING version INTO v_row.version;
                END IF;
                v_versions := v_versions || jsonb_build_object(v_key,v_row.version);
                UPDATE public.mask_annotation_revisions SET expires_at = GREATEST(expires_at,v_expiry)
                WHERE task_id = v_task_id AND annotation_id = v_row.id
                  AND annotation_version IN ((v_original.report -> 'before' -> v_key ->> 'version')::integer,
                      (v_original.report -> 'after' -> v_key ->> 'version')::integer);
            END LOOP;
            v_result := jsonb_build_object('acceptance_key',v_payload ->> 'acceptance_key',
                'target',v_payload ->> 'target','result_versions',v_versions,
                'active_annotation_ids',COALESCE((SELECT jsonb_agg(key ORDER BY key)
                    FROM jsonb_each(v_target) WHERE (value ->> 'active')::boolean),'[]'::jsonb),
                'restore_expires_at',v_expiry,'idempotent_replay',false);
            UPDATE public.annotation_operations SET report = report || jsonb_build_object(
                'current_side',v_payload ->> 'target','current_versions',v_versions)
            WHERE id = v_original.id;
            INSERT INTO public.annotation_operations (
                id,task_id,actor_id,kind,idempotency_key,request_digest,scope_fingerprint,
                source_versions,result_versions,report,status,response_json
            ) VALUES (
                gen_random_uuid(),v_task_id,v_actor_id,'restore_acceptance',v_idempotency_key,v_digest,v_digest,
                v_payload -> 'expected_versions',v_versions,jsonb_build_object('acceptance_operation_id',v_original.id),
                'committed',v_result
            );
        END IF;
    END IF;
    RETURN jsonb_build_object('schema','aap-labeling-mask-edits/v1','ok',true,
        'request',p_request,'data',jsonb_build_object('history',v_result));
END;
$function$;
