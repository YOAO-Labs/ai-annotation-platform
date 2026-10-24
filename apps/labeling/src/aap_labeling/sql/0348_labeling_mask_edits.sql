CREATE FUNCTION aap_labeling_v1.mask_edit_command(p_request jsonb)
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
    v_field text;
    v_task_id uuid;
    v_project_id uuid;
    v_actor_id uuid;
    v_operation_id uuid;
    v_source_id uuid;
    v_created_id uuid;
    v_expected_version integer;
    v_task record;
    v_source record;
    v_created record;
    v_row record;
    v_original record;
    v_change jsonb;
    v_entry record;
    v_lineage jsonb;
    v_data jsonb;
    v_reply jsonb;
    v_account_reply jsonb;
    v_account jsonb;
    v_versions jsonb;
    v_report jsonb;
    v_changed jsonb := '[]'::jsonb;
    v_expected_id uuid;
    v_now timestamptz;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-mask-edits/v1'
       OR jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Mask edit request' USING ERRCODE = '22023';
    END IF;
    v_operation := p_request ->> 'operation';
    CASE v_operation
        WHEN 'find_operation' THEN
            v_expected_keys := ARRAY['schema','operation','task_id','actor_id','idempotency_key'];
        WHEN 'read_operation' THEN
            v_expected_keys := ARRAY['schema','operation','task_id','operation_id','lock'];
        WHEN 'apply_polygon_slice' THEN
            v_expected_keys := ARRAY[
                'schema','operation','task_id','project_id','actor_id','expected_batch_id',
                'expected_status','expected_review_round_id','source_annotation_id',
                'created_annotation_id','expected_version','source_geometry','created_geometry'
            ];
        WHEN 'apply_mask_mutations' THEN
            v_expected_keys := ARRAY[
                'schema','operation','task_id','project_id','actor_id','expected_batch_id',
                'expected_status','expected_review_round_id','mutation_kind',
                'expected_versions','changes'
            ];
        WHEN 'apply_slice_restore' THEN
            v_expected_keys := ARRAY[
                'schema','operation','task_id','project_id','actor_id','expected_batch_id',
                'expected_status','expected_review_round_id','original_operation_id',
                'target','no_op','annotations'
            ];
        WHEN 'write_operation' THEN
            v_expected_keys := ARRAY[
                'schema','operation','task_id','project_id','actor_id','expected_batch_id',
                'expected_status','expected_review_round_id','operation_id','kind',
                'idempotency_key','request_digest','scope_fingerprint','source_versions',
                'result_versions','report','response_json','created_at','completed_at',
                'lineage','original_operation_id','original_report'
            ];
        ELSE
            RAISE EXCEPTION 'Invalid Mask edit operation' USING ERRCODE = '22023';
    END CASE;
    SELECT array_agg(key ORDER BY key) INTO v_keys
    FROM jsonb_object_keys(p_request) AS fields(key);
    SELECT array_agg(key ORDER BY key) INTO v_expected_keys
    FROM unnest(v_expected_keys) AS fields(key);
    IF v_keys IS DISTINCT FROM v_expected_keys THEN
        RAISE EXCEPTION 'Invalid Mask edit request fields' USING ERRCODE = '22023';
    END IF;

    IF v_operation IN ('find_operation','read_operation') THEN
        IF jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid Mask operation identity' USING ERRCODE = '22023';
        END IF;
        v_task_id := (p_request ->> 'task_id')::uuid;
        IF v_operation = 'find_operation' THEN
            IF jsonb_typeof(p_request -> 'actor_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'idempotency_key') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION 'Invalid Mask operation lookup' USING ERRCODE = '22023';
            END IF;
            v_actor_id := (p_request ->> 'actor_id')::uuid;
            SELECT operation.* INTO v_original
            FROM public.annotation_operations AS operation
            WHERE operation.task_id = v_task_id
              AND operation.actor_id = v_actor_id
              AND operation.idempotency_key = p_request ->> 'idempotency_key';
        ELSE
            IF jsonb_typeof(p_request -> 'operation_id') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION 'Invalid Mask operation identifier' USING ERRCODE = '22023';
            END IF;
            v_operation_id := (p_request ->> 'operation_id')::uuid;
            IF jsonb_typeof(p_request -> 'lock') IS DISTINCT FROM 'boolean' THEN
                RAISE EXCEPTION 'Invalid Mask operation lock' USING ERRCODE = '22023';
            END IF;
            IF (p_request ->> 'lock')::boolean THEN
                SELECT operation.* INTO v_original
                FROM public.annotation_operations AS operation
                WHERE operation.task_id = v_task_id AND operation.id = v_operation_id
                FOR UPDATE;
            ELSE
                SELECT operation.* INTO v_original
                FROM public.annotation_operations AS operation
                WHERE operation.task_id = v_task_id AND operation.id = v_operation_id;
            END IF;
        END IF;
        v_data := jsonb_build_object(
            'found', FOUND,
            'operation_id', CASE WHEN FOUND THEN v_original.id END,
            'task_id', CASE WHEN FOUND THEN v_original.task_id END,
            'actor_id', CASE WHEN FOUND THEN v_original.actor_id END,
            'kind', CASE WHEN FOUND THEN v_original.kind END,
            'idempotency_key', CASE WHEN FOUND THEN v_original.idempotency_key END,
            'request_digest', CASE WHEN FOUND THEN v_original.request_digest END,
            'report', CASE WHEN FOUND THEN v_original.report END,
            'response_json', CASE WHEN FOUND THEN v_original.response_json END,
            'created_at', CASE WHEN FOUND THEN v_original.created_at END
        );
    ELSE
        FOREACH v_field IN ARRAY ARRAY['task_id','project_id','actor_id','expected_status'] LOOP
            IF jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION 'Invalid Mask task fence' USING ERRCODE = '22023';
            END IF;
        END LOOP;
        IF jsonb_typeof(p_request -> 'expected_batch_id') NOT IN ('string','null')
           OR jsonb_typeof(p_request -> 'expected_review_round_id') NOT IN ('string','null') THEN
            RAISE EXCEPTION 'Invalid Mask task fence' USING ERRCODE = '22023';
        END IF;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        v_account_reply := aap_identity_v1.lock_accounts_share(ARRAY[v_actor_id], true);
        IF jsonb_typeof(v_account_reply -> 'accounts') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid Identity account-lock reply' USING ERRCODE = '55000';
        END IF;
        SELECT account.value INTO v_account
        FROM jsonb_array_elements(v_account_reply -> 'accounts') AS account(value)
        WHERE account.value ->> 'id' = v_actor_id::text;
        IF v_account IS NULL OR v_account -> 'is_active' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                DETAIL = jsonb_build_object('status_code',403,'detail',jsonb_build_object('reason','actor_inactive'))::text;
        END IF;
        SELECT task.id, task.project_id, task.batch_id, task.status, task.review_round_id
        INTO v_task FROM public.tasks AS task WHERE task.id = v_task_id FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                DETAIL = jsonb_build_object('status_code',404,'detail',jsonb_build_object('reason','task_not_found'))::text;
        END IF;
        IF v_task.project_id IS DISTINCT FROM v_project_id
           OR v_task.batch_id IS DISTINCT FROM (p_request ->> 'expected_batch_id')::uuid
           OR v_task.status IS DISTINCT FROM p_request ->> 'expected_status'
           OR v_task.review_round_id IS DISTINCT FROM (p_request ->> 'expected_review_round_id')::uuid THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','task_target_changed'))::text;
        END IF;

        IF v_operation = 'apply_polygon_slice' THEN
            IF jsonb_typeof(p_request -> 'source_annotation_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'created_annotation_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'expected_version') IS DISTINCT FROM 'number'
               OR p_request ->> 'expected_version' !~ '^[1-9][0-9]*$'
               OR jsonb_typeof(p_request -> 'source_geometry') IS DISTINCT FROM 'object'
               OR jsonb_typeof(p_request -> 'created_geometry') IS DISTINCT FROM 'object'
               OR p_request #>> '{created_geometry,type}' IS DISTINCT FROM 'polygon' THEN
                RAISE EXCEPTION 'Invalid polygon slice payload' USING ERRCODE = '22023';
            END IF;
            v_source_id := (p_request ->> 'source_annotation_id')::uuid;
            v_created_id := (p_request ->> 'created_annotation_id')::uuid;
            v_expected_version := (p_request ->> 'expected_version')::integer;
            SELECT annotation.* INTO v_source
            FROM public.annotations AS annotation
            WHERE annotation.id = v_source_id FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                    DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','annotation_target_changed'))::text;
            END IF;
            IF v_source.task_id IS DISTINCT FROM v_task_id
               OR (v_source.project_id IS NOT NULL AND v_source.project_id IS DISTINCT FROM v_project_id)
               OR v_source.version IS DISTINCT FROM v_expected_version
               OR v_source.is_active IS NOT TRUE OR v_source.was_cancelled IS TRUE
               OR v_source.is_locked IS TRUE OR v_source.sensor_role IS NOT NULL
               OR v_source.annotation_type IS DISTINCT FROM 'polygon'
               OR v_source.geometry ->> 'type' IS DISTINCT FROM 'polygon' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                    DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','annotation_target_changed'))::text;
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.annotations AS child
                WHERE child.parent_annotation_id = v_source_id AND child.is_active IS TRUE
            ) THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                    DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','active_children'))::text;
            END IF;
            IF v_source.parent_annotation_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.annotations AS parent
                WHERE parent.id = v_source.parent_annotation_id
                  AND parent.task_id = v_task_id AND parent.is_active IS TRUE
            ) THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                    DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','parent_annotation_changed'))::text;
            END IF;
            UPDATE public.annotations AS annotation
            SET geometry = p_request -> 'source_geometry',
                version = annotation.version + 1,
                updated_at = transaction_timestamp()
            WHERE annotation.id = v_source_id
            RETURNING annotation.* INTO v_source;
            INSERT INTO public.annotations (
                id, task_id, project_id, video_segment_id, user_id, source,
                annotation_type, tool_unit_id, class_name, geometry, confidence,
                parent_annotation_id, track_id, temporal_role, was_cancelled,
                ground_truth, is_active, attributes, attributes_meta, z_order,
                is_locked, is_hidden, version
            ) VALUES (
                v_created_id, v_task_id, v_project_id, v_source.video_segment_id,
                v_actor_id, 'manual', 'polygon', v_source.tool_unit_id,
                v_source.class_name, p_request -> 'created_geometry', NULL,
                v_source.parent_annotation_id, NULL, 'sample', false, false, true,
                v_source.attributes, v_source.attributes_meta, v_source.z_order,
                false, false, 1
            ) RETURNING * INTO v_created;
            v_data := jsonb_build_object(
                'source', jsonb_build_object(
                    'annotation_id',v_source.id,'action','update','version',v_source.version,
                    'annotation_type',v_source.annotation_type,'track_id',v_source.track_id,
                    'user_id',v_source.user_id,'video_segment_id',v_source.video_segment_id,
                    'tool_unit_id',v_source.tool_unit_id,'class_name',v_source.class_name,
                    'geometry',v_source.geometry,'confidence',v_source.confidence,
                    'attributes',v_source.attributes,'attributes_meta',v_source.attributes_meta,
                    'z_order',v_source.z_order,'parent_annotation_id',v_source.parent_annotation_id,
                    'is_active',v_source.is_active,'updated_at',v_source.updated_at
                ),
                'created', jsonb_build_object(
                    'annotation_id',v_created.id,'action','create','version',v_created.version,
                    'annotation_type',v_created.annotation_type,'track_id',v_created.track_id,
                    'user_id',v_created.user_id,'video_segment_id',v_created.video_segment_id,
                    'tool_unit_id',v_created.tool_unit_id,'class_name',v_created.class_name,
                    'geometry',v_created.geometry,'confidence',v_created.confidence,
                    'attributes',v_created.attributes,'attributes_meta',v_created.attributes_meta,
                    'z_order',v_created.z_order,'parent_annotation_id',v_created.parent_annotation_id,
                    'is_active',v_created.is_active,'updated_at',v_created.updated_at
                )
            );
        ELSIF v_operation = 'apply_mask_mutations' THEN
            IF jsonb_typeof(p_request -> 'mutation_kind') IS DISTINCT FROM 'string'
               OR p_request ->> 'mutation_kind' NOT IN (
                    'slice_mask','split_components','copy_component','copy_keyframe',
                    'join_masks','overlap','delete_small_islands','fill_small_holes',
                    'resolve_same_class_overlap','mask_repair_rollback'
               )
               OR jsonb_typeof(p_request -> 'expected_versions') IS DISTINCT FROM 'object'
               OR jsonb_typeof(p_request -> 'changes') IS DISTINCT FROM 'array'
               OR jsonb_array_length(p_request -> 'changes') = 0 THEN
                RAISE EXCEPTION 'Invalid Mask mutation payload' USING ERRCODE = '22023';
            END IF;
            FOR v_entry IN
                SELECT fields.key, fields.value
                FROM jsonb_each(p_request -> 'expected_versions') AS fields(key,value)
                ORDER BY fields.key
            LOOP
                IF v_entry.key !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                   OR jsonb_typeof(v_entry.value) IS DISTINCT FROM 'number'
                   OR v_entry.value::text !~ '^[1-9][0-9]*$' THEN
                    RAISE EXCEPTION 'Invalid Mask version set' USING ERRCODE = '22023';
                END IF;
                v_expected_id := v_entry.key::uuid;
                v_expected_version := (v_entry.value #>> '{}')::integer;
                SELECT annotation.id, annotation.task_id, annotation.project_id,
                       annotation.version, annotation.is_active, annotation.was_cancelled
                INTO v_row FROM public.annotations AS annotation
                WHERE annotation.id = v_expected_id FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                        DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','scope_stale','annotation_id',v_expected_id))::text;
                END IF;
                IF v_row.task_id IS DISTINCT FROM v_task_id
                   OR (v_row.project_id IS NOT NULL AND v_row.project_id IS DISTINCT FROM v_project_id)
                   OR v_row.version IS DISTINCT FROM v_expected_version
                   OR v_row.is_active IS NOT TRUE OR v_row.was_cancelled IS TRUE THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                        DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','scope_stale','annotation_id',v_expected_id))::text;
                END IF;
            END LOOP;
            FOR v_change IN SELECT value FROM jsonb_array_elements(p_request -> 'changes') AS changes(value)
            LOOP
                IF jsonb_typeof(v_change) IS DISTINCT FROM 'object'
                   OR jsonb_typeof(v_change -> 'action') IS DISTINCT FROM 'string'
                   OR jsonb_typeof(v_change -> 'annotation_id') IS DISTINCT FROM 'string' THEN
                    RAISE EXCEPTION 'Invalid Mask annotation change' USING ERRCODE = '22023';
                END IF;
                v_created_id := (v_change ->> 'annotation_id')::uuid;
                IF v_change ->> 'action' = 'update' THEN
                    IF (SELECT value FROM jsonb_each_text(p_request -> 'expected_versions') AS e(key,value)
                        WHERE e.key = v_created_id::text) IS NULL
                       OR jsonb_typeof(v_change -> 'geometry') IS DISTINCT FROM 'object'
                       OR jsonb_typeof(v_change -> 'track_id') NOT IN ('string','null') THEN
                        RAISE EXCEPTION 'Invalid Mask update change' USING ERRCODE = '22023';
                    END IF;
                    SELECT annotation.is_locked INTO v_row
                    FROM public.annotations AS annotation WHERE annotation.id = v_created_id;
                    IF v_row.is_locked IS TRUE THEN
                        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                            DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','annotation_locked','annotation_id',v_created_id))::text;
                    END IF;
                    UPDATE public.annotations AS annotation
                    SET geometry = v_change -> 'geometry',
                        annotation_type = COALESCE(v_change #>> '{geometry,type}',''),
                        track_id = CASE WHEN p_request ->> 'mutation_kind' = 'slice_mask'
                            THEN annotation.track_id ELSE v_change ->> 'track_id' END,
                        user_id = CASE WHEN p_request ->> 'mutation_kind' = 'slice_mask'
                            THEN annotation.user_id ELSE v_actor_id END,
                        version = annotation.version + 1,
                        updated_at = transaction_timestamp()
                    WHERE annotation.id = v_created_id
                    RETURNING annotation.* INTO v_row;
                ELSIF v_change ->> 'action' = 'create' THEN
                    IF jsonb_typeof(v_change -> 'source_annotation_id') IS DISTINCT FROM 'string'
                       OR jsonb_typeof(v_change -> 'geometry') IS DISTINCT FROM 'object'
                       OR jsonb_typeof(v_change -> 'track_id') NOT IN ('string','null') THEN
                        RAISE EXCEPTION 'Invalid Mask create change' USING ERRCODE = '22023';
                    END IF;
                    v_source_id := (v_change ->> 'source_annotation_id')::uuid;
                    IF NOT (p_request -> 'expected_versions' ? v_source_id::text) THEN
                        RAISE EXCEPTION 'Mask create source is outside the locked scope' USING ERRCODE = '22023';
                    END IF;
                    SELECT annotation.* INTO v_source
                    FROM public.annotations AS annotation
                    WHERE annotation.id = v_source_id AND annotation.task_id = v_task_id
                      AND (annotation.project_id IS NULL OR annotation.project_id = v_project_id)
                      AND annotation.is_active IS TRUE
                      AND annotation.was_cancelled IS FALSE FOR UPDATE;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                            DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','annotation_locked','annotation_id',v_source_id))::text;
                    END IF;
                    IF v_source.is_locked IS TRUE THEN
                        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                            DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','annotation_locked','annotation_id',v_source_id))::text;
                    END IF;
                    INSERT INTO public.annotations (
                        id,task_id,project_id,video_segment_id,user_id,source,
                        annotation_type,tool_unit_id,class_name,geometry,track_id,
                        confidence,parent_annotation_id,temporal_role,was_cancelled,
                        ground_truth,is_active,attributes,attributes_meta,z_order,
                        is_locked,is_hidden,version
                    ) VALUES (
                        v_created_id,v_task_id,v_project_id,v_source.video_segment_id,
                        v_actor_id,'manual',COALESCE(v_change #>> '{geometry,type}',''),
                        v_source.tool_unit_id,v_source.class_name,v_change -> 'geometry',
                        v_change ->> 'track_id',NULL,
                        CASE WHEN p_request ->> 'mutation_kind' = 'slice_mask'
                            THEN v_source.parent_annotation_id ELSE NULL END,
                        'sample',false,false,true,COALESCE(v_source.attributes,'{}'::jsonb),
                        COALESCE(v_source.attributes_meta,'{}'::jsonb),v_source.z_order,
                        false,false,1
                    ) RETURNING * INTO v_row;
                ELSIF v_change ->> 'action' = 'deactivate' THEN
                    IF NOT (p_request -> 'expected_versions' ? v_created_id::text) THEN
                        RAISE EXCEPTION 'Mask delete target is outside the locked scope' USING ERRCODE = '22023';
                    END IF;
                    SELECT annotation.is_locked INTO v_row
                    FROM public.annotations AS annotation WHERE annotation.id = v_created_id;
                    IF v_row.is_locked IS TRUE THEN
                        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                            DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','annotation_locked','annotation_id',v_created_id))::text;
                    END IF;
                    UPDATE public.annotations AS annotation
                    SET is_active = false, version = annotation.version + 1,
                        updated_at = transaction_timestamp()
                    WHERE annotation.id = v_created_id
                    RETURNING annotation.* INTO v_row;
                ELSE
                    RAISE EXCEPTION 'Invalid Mask annotation change action' USING ERRCODE = '22023';
                END IF;
                v_changed := v_changed || jsonb_build_array(jsonb_build_object(
                    'annotation_id',v_row.id,'action',v_change ->> 'action',
                    'version',v_row.version,'annotation_type',v_row.annotation_type,
                    'track_id',v_row.track_id,'user_id',v_row.user_id,
                    'video_segment_id',v_row.video_segment_id,'tool_unit_id',v_row.tool_unit_id,
                    'class_name',v_row.class_name,'geometry',v_row.geometry,
                    'confidence',v_row.confidence,'attributes',v_row.attributes,
                    'attributes_meta',v_row.attributes_meta,'z_order',v_row.z_order,
                    'parent_annotation_id',v_row.parent_annotation_id,
                    'is_active',v_row.is_active,'updated_at',v_row.updated_at
                ));
            END LOOP;
            v_data := jsonb_build_object('annotations',v_changed);
        ELSIF v_operation = 'apply_slice_restore' THEN
            IF jsonb_typeof(p_request -> 'original_operation_id') IS DISTINCT FROM 'string'
               OR p_request ->> 'target' NOT IN ('before','after')
               OR jsonb_typeof(p_request -> 'no_op') IS DISTINCT FROM 'boolean'
               OR jsonb_typeof(p_request -> 'annotations') IS DISTINCT FROM 'array'
               OR jsonb_array_length(p_request -> 'annotations') = 0 THEN
                RAISE EXCEPTION 'Invalid slice restore payload' USING ERRCODE = '22023';
            END IF;
            v_operation_id := (p_request ->> 'original_operation_id')::uuid;
            SELECT operation.* INTO v_original
            FROM public.annotation_operations AS operation
            WHERE operation.id = v_operation_id AND operation.task_id = v_task_id
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                    DETAIL = jsonb_build_object('status_code',404,'detail',jsonb_build_object('reason','slice_not_found'))::text;
            END IF;
            IF v_original.kind NOT IN ('slice_polygon','slice_mask') THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                    DETAIL = jsonb_build_object('status_code',404,'detail',jsonb_build_object('reason','slice_not_found'))::text;
            END IF;
            IF (v_original.kind = 'slice_mask' AND v_original.report ->> 'slice_snapshot_schema' IS DISTINCT FROM '2')
               OR (v_original.kind = 'slice_polygon' AND v_original.report ->> 'slice_snapshot_schema' IS DISTINCT FROM '1') THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                    DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','snapshot_unavailable'))::text;
            END IF;
            IF v_original.created_at + interval '30 days' <= clock_timestamp() THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                    DETAIL = jsonb_build_object('status_code',410,'detail',jsonb_build_object('reason','restore_expired'))::text;
            END IF;
            IF v_original.report ->> 'current_side' IS DISTINCT FROM p_request ->> 'target'
               AND (p_request ->> 'no_op')::boolean THEN
                RAISE EXCEPTION 'Slice restore no-op does not match current side' USING ERRCODE = '22023';
            END IF;
            v_versions := '{}'::jsonb;
            v_expected_keys := '{}'::text[];
            v_data := '{}'::jsonb;
            FOR v_change IN SELECT value FROM jsonb_array_elements(p_request -> 'annotations') AS rows(value)
            LOOP
                IF jsonb_typeof(v_change -> 'annotation_id') IS DISTINCT FROM 'string'
                   OR jsonb_typeof(v_change -> 'expected_version') IS DISTINCT FROM 'number'
                   OR v_change ->> 'expected_version' !~ '^[1-9][0-9]*$'
                   OR jsonb_typeof(v_change -> 'expected_geometry') IS DISTINCT FROM 'object'
                   OR jsonb_typeof(v_change -> 'expected_active') IS DISTINCT FROM 'boolean'
                   OR jsonb_typeof(v_change -> 'geometry') IS DISTINCT FROM 'object'
                   OR jsonb_typeof(v_change -> 'active') IS DISTINCT FROM 'boolean' THEN
                    RAISE EXCEPTION 'Invalid slice restore annotation' USING ERRCODE = '22023';
                END IF;
                v_created_id := (v_change ->> 'annotation_id')::uuid;
                v_expected_version := (v_change ->> 'expected_version')::integer;
                SELECT annotation.* INTO v_row FROM public.annotations AS annotation
                WHERE annotation.id = v_created_id FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                        DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','version_mismatch','annotation_id',v_created_id))::text;
                END IF;
                IF v_row.task_id IS DISTINCT FROM v_task_id
                   OR (v_row.project_id IS NOT NULL AND v_row.project_id IS DISTINCT FROM v_project_id)
                   OR v_row.version IS DISTINCT FROM v_expected_version
                   OR v_row.geometry IS DISTINCT FROM v_change -> 'expected_geometry'
                   OR v_row.is_active IS DISTINCT FROM (v_change ->> 'expected_active')::boolean
                   OR v_row.is_locked IS TRUE OR v_row.was_cancelled IS TRUE
                   OR v_row.sensor_role IS NOT NULL THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                        DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','version_mismatch','annotation_id',v_created_id))::text;
                END IF;
                v_expected_keys := array_append(v_expected_keys,v_created_id::text);
                v_data := v_data || jsonb_build_object(v_created_id::text,v_row.version);
                IF (p_request ->> 'no_op')::boolean THEN
                    v_expected_version := v_row.version;
                ELSE
                    UPDATE public.annotations AS annotation
                    SET geometry = v_change -> 'geometry',
                        is_active = (v_change ->> 'active')::boolean,
                        version = annotation.version + 1,
                        updated_at = transaction_timestamp()
                    WHERE annotation.id = v_created_id
                    RETURNING annotation.version INTO v_expected_version;
                END IF;
                v_versions := v_versions || jsonb_build_object(v_created_id::text,v_expected_version);
            END LOOP;
            SELECT array_agg(key ORDER BY key) INTO v_keys
            FROM jsonb_object_keys(v_original.report -> 'current_versions') AS versions(key);
            SELECT array_agg(key ORDER BY key) INTO v_expected_keys
            FROM unnest(v_expected_keys) AS versions(key);
            IF v_data IS DISTINCT FROM v_original.report -> 'current_versions'
               OR v_keys IS DISTINCT FROM v_expected_keys THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                    DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','version_mismatch'))::text;
            END IF;
            v_data := jsonb_build_object(
                'result_versions',v_versions,'original_report',v_original.report
            );
        ELSE
            IF jsonb_typeof(p_request -> 'operation_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'kind') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'idempotency_key') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'request_digest') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'scope_fingerprint') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'source_versions') IS DISTINCT FROM 'object'
               OR jsonb_typeof(p_request -> 'result_versions') IS DISTINCT FROM 'object'
               OR jsonb_typeof(p_request -> 'report') IS DISTINCT FROM 'object'
               OR jsonb_typeof(p_request -> 'response_json') IS DISTINCT FROM 'object'
               OR jsonb_typeof(p_request -> 'lineage') IS DISTINCT FROM 'array'
               OR jsonb_typeof(p_request -> 'created_at') NOT IN ('string','null')
               OR jsonb_typeof(p_request -> 'completed_at') NOT IN ('string','null')
               OR jsonb_typeof(p_request -> 'original_operation_id') NOT IN ('string','null')
               OR jsonb_typeof(p_request -> 'original_report') NOT IN ('object','null') THEN
                RAISE EXCEPTION 'Invalid Annotation operation record' USING ERRCODE = '22023';
            END IF;
            IF p_request ->> 'kind' NOT IN (
                'slice_polygon','restore_slice','slice_mask','split_components',
                'copy_component','copy_keyframe','join_masks','overlap',
                'delete_small_islands','fill_small_holes','resolve_same_class_overlap',
                'mask_repair_rollback'
            ) THEN
                RAISE EXCEPTION 'Unsupported Mask operation kind' USING ERRCODE = '22023';
            END IF;
            IF ((p_request ->> 'original_operation_id') IS NULL)
               <> (jsonb_typeof(p_request -> 'original_report') = 'null') THEN
                RAISE EXCEPTION 'Invalid original slice update' USING ERRCODE = '22023';
            END IF;
            IF p_request ->> 'original_operation_id' IS NOT NULL THEN
                v_operation_id := (p_request ->> 'original_operation_id')::uuid;
                PERFORM 1 FROM public.annotation_operations AS original
                WHERE original.id = v_operation_id AND original.task_id = v_task_id
                  AND original.kind IN ('slice_mask','slice_polygon') FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_edit_error',
                        DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','slice_not_found'))::text;
                END IF;
                UPDATE public.annotation_operations AS original
                SET report = p_request -> 'original_report'
                WHERE original.id = v_operation_id;
            END IF;
            v_operation_id := (p_request ->> 'operation_id')::uuid;
            INSERT INTO public.annotation_operations (
                id,task_id,actor_id,kind,idempotency_key,request_digest,
                scope_fingerprint,source_versions,result_versions,report,status,
                response_json,created_at,completed_at
            ) VALUES (
                v_operation_id,v_task_id,v_actor_id,p_request ->> 'kind',
                p_request ->> 'idempotency_key',p_request ->> 'request_digest',
                p_request ->> 'scope_fingerprint',p_request -> 'source_versions',
                p_request -> 'result_versions',p_request -> 'report','committed',
                p_request -> 'response_json',
                COALESCE((p_request ->> 'created_at')::timestamptz,transaction_timestamp()),
                COALESCE((p_request ->> 'completed_at')::timestamptz,transaction_timestamp())
            );
            FOR v_lineage IN SELECT value FROM jsonb_array_elements(p_request -> 'lineage') AS edges(value)
            LOOP
                IF jsonb_typeof(v_lineage) IS DISTINCT FROM 'object'
                   OR jsonb_typeof(v_lineage -> 'relation') IS DISTINCT FROM 'string' THEN
                    RAISE EXCEPTION 'Invalid Annotation lineage edge' USING ERRCODE = '22023';
                END IF;
                INSERT INTO public.annotation_lineage_edges (
                    operation_id,source_annotation_id,result_annotation_id,
                    relation,source_version,result_version,frame_index
                ) VALUES (
                    v_operation_id,
                    (v_lineage ->> 'source_annotation_id')::uuid,
                    (v_lineage ->> 'result_annotation_id')::uuid,
                    v_lineage ->> 'relation',
                    (v_lineage ->> 'source_version')::integer,
                    (v_lineage ->> 'result_version')::integer,
                    (v_lineage ->> 'frame_index')::integer
                );
            END LOOP;
            v_data := jsonb_build_object('stored',true);
        END IF;
    END IF;
    v_reply := jsonb_build_object(
        'schema','aap-labeling-mask-edits/v1','ok',true,
        'request',p_request,'data',v_data
    );
    RETURN v_reply;
END;
$function$;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.mask_revision_command(p_request jsonb)
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
    v_task_id uuid;
    v_annotation_id uuid;
    v_version integer;
    v_reference jsonb;
    v_entry record;
    v_geometry jsonb;
    v_task_exists boolean;
    v_count integer := 0;
    v_deleted integer := 0;
    v_rows jsonb := '[]'::jsonb;
    v_reply jsonb;
    v_now timestamptz;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-mask-revisions/v1'
       OR jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Mask revision request' USING ERRCODE = '22023';
    END IF;
    v_operation := p_request ->> 'operation';
    CASE v_operation
        WHEN 'resolve' THEN
            v_expected_keys := ARRAY['schema','operation','task_id','references','lock'];
        WHEN 'protect' THEN
            v_expected_keys := ARRAY['schema','operation','task_id','references','expires_at'];
        WHEN 'expire' THEN
            v_expected_keys := ARRAY['schema','operation','now'];
        ELSE
            RAISE EXCEPTION 'Invalid Mask revision operation' USING ERRCODE = '22023';
    END CASE;
    SELECT array_agg(key ORDER BY key) INTO v_keys
    FROM jsonb_object_keys(p_request) AS fields(key);
    SELECT array_agg(key ORDER BY key) INTO v_expected_keys
    FROM unnest(v_expected_keys) AS fields(key);
    IF v_keys IS DISTINCT FROM v_expected_keys THEN
        RAISE EXCEPTION 'Invalid Mask revision request fields' USING ERRCODE = '22023';
    END IF;
    IF v_operation = 'expire' THEN
        IF jsonb_typeof(p_request -> 'now') NOT IN ('string','null') THEN
            RAISE EXCEPTION 'Invalid Mask revision expiry time' USING ERRCODE = '22023';
        END IF;
        v_now := COALESCE((p_request ->> 'now')::timestamptz,clock_timestamp());
        WITH expired AS (
            DELETE FROM public.mask_annotation_revisions AS revision
            WHERE revision.expires_at <= v_now
            RETURNING revision.id
        ) SELECT count(*)::integer INTO v_count FROM expired;
    ELSE
        IF jsonb_typeof(p_request -> 'references') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid Mask revision references' USING ERRCODE = '22023';
        END IF;
        IF v_operation = 'resolve' THEN
            IF jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'lock') IS DISTINCT FROM 'boolean' THEN
                RAISE EXCEPTION 'Invalid Mask revision resolve request' USING ERRCODE = '22023';
            END IF;
            v_task_id := (p_request ->> 'task_id')::uuid;
            SELECT EXISTS(SELECT 1 FROM public.tasks AS task WHERE task.id = v_task_id)
            INTO v_task_exists;
            IF NOT v_task_exists THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_revision_error',
                    DETAIL = jsonb_build_object('status_code',404,'detail',jsonb_build_object('reason','task_not_found'))::text;
            END IF;
        ELSE
            IF jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'expires_at') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION 'Invalid Mask revision expiry time' USING ERRCODE = '22023';
            END IF;
            v_task_id := (p_request ->> 'task_id')::uuid;
            IF NOT EXISTS (SELECT 1 FROM public.tasks AS task WHERE task.id = v_task_id) THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_revision_error',
                    DETAIL = jsonb_build_object('status_code',404,'detail',jsonb_build_object('reason','task_not_found'))::text;
            END IF;
        END IF;
        FOR v_reference IN SELECT value FROM jsonb_array_elements(p_request -> 'references') AS refs(value)
        LOOP
            IF jsonb_typeof(v_reference) IS DISTINCT FROM 'object'
               OR jsonb_object_length(v_reference) <> 3
               OR jsonb_typeof(v_reference -> 'annotation_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(v_reference -> 'annotation_version') IS DISTINCT FROM 'number'
               OR v_reference ->> 'annotation_version' !~ '^[1-9][0-9]*$'
               OR jsonb_typeof(v_reference -> 'geometry_digest') IS DISTINCT FROM 'string'
               OR v_reference ->> 'geometry_digest' !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'Invalid Mask revision reference' USING ERRCODE = '22023';
            END IF;
            v_annotation_id := (v_reference ->> 'annotation_id')::uuid;
            v_version := (v_reference ->> 'annotation_version')::integer;
            IF v_operation = 'resolve' THEN
                SELECT annotation.geometry INTO v_geometry
                FROM public.annotations AS annotation
                WHERE annotation.id = v_annotation_id AND annotation.task_id = v_task_id
                  AND annotation.version = v_version;
                IF NOT FOUND THEN
                    IF (p_request ->> 'lock')::boolean THEN
                        SELECT revision.geometry INTO v_geometry
                        FROM public.mask_annotation_revisions AS revision
                        WHERE revision.task_id = v_task_id
                          AND revision.annotation_id = v_annotation_id
                          AND revision.annotation_version = v_version
                          AND revision.geometry_digest = v_reference ->> 'geometry_digest'
                          AND revision.expires_at > clock_timestamp()
                        FOR UPDATE;
                    ELSE
                        SELECT revision.geometry INTO v_geometry
                        FROM public.mask_annotation_revisions AS revision
                        WHERE revision.task_id = v_task_id
                          AND revision.annotation_id = v_annotation_id
                          AND revision.annotation_version = v_version
                          AND revision.geometry_digest = v_reference ->> 'geometry_digest'
                          AND revision.expires_at > clock_timestamp();
                    END IF;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_revision_error',
                            DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','snapshot_unavailable'))::text;
                    END IF;
                END IF;
                IF jsonb_typeof(v_geometry) IS DISTINCT FROM 'object'
                   OR v_geometry ->> 'type' IS DISTINCT FROM 'raster_mask' THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_revision_error',
                        DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','snapshot_unavailable'))::text;
                END IF;
                v_rows := v_rows || jsonb_build_array(jsonb_build_object(
                    'annotation_id',v_annotation_id,'annotation_version',v_version,
                    'geometry',v_geometry
                ));
            ELSE
                UPDATE public.mask_annotation_revisions AS revision
                SET expires_at = GREATEST(revision.expires_at,(p_request ->> 'expires_at')::timestamptz)
                WHERE revision.task_id = v_task_id
                  AND revision.annotation_id = v_annotation_id
                  AND revision.annotation_version = v_version
                  AND revision.geometry_digest = v_reference ->> 'geometry_digest';
                GET DIAGNOSTICS v_deleted = ROW_COUNT;
                v_count := v_count + v_deleted;
            END IF;
        END LOOP;
    END IF;
    v_reply := jsonb_build_object(
        'schema','aap-labeling-mask-revisions/v1','ok',true,
        'request',p_request,
        'data',jsonb_build_object(
            'geometries',v_rows,'affected',v_count
        )
    );
    RETURN v_reply;
END;
$function$;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.ai_mask_accept_command(p_request jsonb)
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
        WHEN 'find_decision' THEN
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

    IF v_operation = 'find_decision' THEN
        IF jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'idempotency_key') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid AI Mask decision identity' USING ERRCODE = '22023';
        END IF;
        v_task_id := (p_request ->> 'task_id')::uuid;
        SELECT decision.* INTO v_decision
        FROM public.ai_mask_accept_decisions AS decision
        WHERE decision.task_id = v_task_id
          AND decision.idempotency_key = p_request ->> 'idempotency_key';
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
                task_id,idempotency_key,request_digest,candidate_id,content_digest,
                prediction_id,prediction_created_at,annotation_id,source_annotation_id,
                source_version,result_version,actor_id,response_json
            ) VALUES (
                v_task_id,p_request ->> 'idempotency_key',p_request ->> 'request_digest',
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
