CREATE FUNCTION aap_labeling_v1._annotation_transfer_exact_fields(
    p_request jsonb, p_expected text[]
) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT array_agg(key ORDER BY key) = (
        SELECT array_agg(key ORDER BY key) FROM unnest(p_expected) AS fields(key)
    )
    FROM jsonb_object_keys(p_request) AS fields(key)
$function$;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._annotation_transfer_without_track_ids(p_value jsonb)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE jsonb_typeof(p_value)
        WHEN 'object' THEN COALESCE((
            SELECT jsonb_object_agg(key, aap_labeling_v1._annotation_transfer_without_track_ids(value))
            FROM jsonb_each(p_value) AS pairs(key, value)
            WHERE key <> 'track_id'
        ), '{}'::jsonb)
        WHEN 'array' THEN COALESCE((
            SELECT jsonb_agg(aap_labeling_v1._annotation_transfer_without_track_ids(value) ORDER BY ordinal)
            FROM jsonb_array_elements(p_value) WITH ORDINALITY AS items(value, ordinal)
        ), '[]'::jsonb)
        ELSE p_value
    END
$function$;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._annotation_transfer_without_suffix(p_path text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE
        WHEN p_path IS NULL OR right(leaf.name,1)='.' OR leaf.dot_reverse=0
            OR length(leaf.name)-leaf.dot_reverse+1 <= 1 THEN p_path
        ELSE left(p_path,length(p_path)-leaf.dot_reverse)
    END
    FROM (
        SELECT regexp_replace(p_path,'^.*/','') AS name,
               strpos(reverse(regexp_replace(p_path,'^.*/','')),'.') AS dot_reverse
    ) AS leaf
$function$;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._annotation_transfer_camera_bbox_valid(p_value jsonb)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN jsonb_typeof(p_value)='object'
        AND aap_labeling_v1._annotation_transfer_exact_fields(
            p_value,ARRAY['h','w','x','y']::text[]
        )
        AND jsonb_typeof(p_value -> 'x')='number'
        AND jsonb_typeof(p_value -> 'y')='number'
        AND jsonb_typeof(p_value -> 'w')='number'
        AND jsonb_typeof(p_value -> 'h')='number'
    THEN (p_value ->> 'x')::numeric >= 0
      AND (p_value ->> 'y')::numeric >= 0
      AND (p_value ->> 'w')::numeric > 0
      AND (p_value ->> 'h')::numeric > 0
      AND (p_value ->> 'w')::numeric <= 1
      AND (p_value ->> 'h')::numeric <= 1
      AND (p_value ->> 'x')::numeric + (p_value ->> 'w')::numeric <= 1.000000001
      AND (p_value ->> 'y')::numeric + (p_value ->> 'h')::numeric <= 1.000000001
    ELSE false END
$function$;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.annotation_transfer_command(p_request jsonb)
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
    v_expected text[];
    v_fields text[];
    v_data jsonb := '{}'::jsonb;
    v_row record;
    v_task_id uuid;
    v_project_id uuid;
    v_track_id text;
    v_task jsonb;
    v_tasks jsonb := '[]'::jsonb;
    v_reason text;
    v_rel text;
    v_target text;
    v_count integer;
    v_task_link record;
    v_track record;
    v_scene_reply jsonb;
    v_scene_track_id uuid;
    v_annotation_id uuid;
    v_deleted integer;
    v_annotation_ids uuid[];
    v_camera_member uuid;
    v_primary_exists boolean;
    v_member_exists boolean;
    v_interval jsonb;
    v_previous_end integer;
    v_start integer;
    v_end integer;
    v_has_member boolean;
    v_frame integer;
    v_now timestamptz;
    v_plan public.annotation_conversion_plans%ROWTYPE;
    v_mask_import public.mask_format_imports%ROWTYPE;
    v_import_status text;
    v_committed_count integer;
    v_conversion_plan jsonb;
    v_video_segments jsonb := '[]'::jsonb;
    v_existing_operation public.annotation_operations%ROWTYPE;
    v_source public.annotations%ROWTYPE;
    v_fence jsonb;
    v_action jsonb;
    v_expected_action jsonb;
    v_expected_sources jsonb;
    v_source_json jsonb;
    v_source_snapshot jsonb;
    v_source_ids uuid[];
    v_result_versions jsonb := '{}'::jsonb;
    v_updated jsonb := '[]'::jsonb;
    v_created jsonb := '[]'::jsonb;
    v_deleted_ids jsonb := '[]'::jsonb;
    v_created_id uuid;
    v_version integer;
    v_id uuid;
    v_item_index bigint;
    v_plan_index bigint;
    v_has_plan boolean;
    v_lock record;
    v_actor_id uuid;
    v_task_scene_id uuid;
    v_member_ids uuid[];
    v_track_ids uuid[];
    v_camera_members jsonb := '[]'::jsonb;
    v_camera_sources jsonb := '[]'::jsonb;
    v_camera_tracks jsonb := '[]'::jsonb;
    v_camera_links jsonb := '[]'::jsonb;
    v_member record;
    v_camera_item_id uuid;
    v_bbox jsonb;
    v_visible boolean;
    v_current_anno public.annotations%ROWTYPE;
    v_current_track public.scene_tracks%ROWTYPE;
    v_previous_exists boolean := false;
    v_track_revision integer;
    v_camera_action text;
    v_execution_id uuid;
    v_idempotency_key text;
    v_execution_digest text;
    v_source_versions jsonb := '{}'::jsonb;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-annotation-transfer/v1'
       OR jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Annotation transfer request' USING ERRCODE='22023';
    END IF;
    v_operation := p_request ->> 'operation';
    v_expected := ARRAY['schema','operation'];
    CASE v_operation
        WHEN 'resolve_task' THEN v_expected := v_expected || ARRAY['project_id','display_id','file_path'];
        WHEN 'resolve_task_by_stem' THEN v_expected := v_expected || ARRAY['project_id','normalized_path'];
        WHEN 'resolve_video_sequence' THEN v_expected := v_expected || ARRAY['project_id','target'];
        WHEN 'read_import_camera_facts' THEN v_expected := v_expected || ARRAY['task_id','track_id','sensor_role','overwrite'];
        WHEN 'purge_imported_annotations' THEN v_expected := v_expected || ARRAY['task_id'];
        WHEN 'create_imported_annotation' THEN v_expected := v_expected || ARRAY[
            'annotation_id','task_id','project_id','actor_id','source','annotation_type','tool_unit_id',
            'class_name','geometry','track_id','confidence','attributes','temporal_role','created_at',
            'scene_track_id','sensor_dataset_item_id','sensor_role','sensor_visibility',
            'calibration_revision','calibration_digest','scene_id','frame_index'];
        WHEN 'replace_imported_scene_track' THEN v_expected := v_expected || ARRAY[
            'project_id','actor_id','track_id','scene_name','class_name','intervals',
            'attributes','attributes_meta','presence_mode'];
        WHEN 'create_conversion_plan' THEN v_expected := v_expected || ARRAY[
            'plan_id','token_hash','task_id','actor_id','request_digest','snapshot_digest',
            'request_json','plan_json','expires_at'];
        WHEN 'read_conversion_plan' THEN v_expected := v_expected || ARRAY['token_hash'];
        WHEN 'lock_conversion_plan' THEN v_expected := v_expected || ARRAY['token_hash','task_id','actor_id'];
        WHEN 'read_conversion_sources' THEN v_expected := v_expected || ARRAY['task_id','annotation_ids','lock'];
        WHEN 'read_conversion_video_segments' THEN v_expected := v_expected || ARRAY['dataset_item_id'];
        WHEN 'expire_conversion_plans' THEN v_expected := v_expected || ARRAY['now'];
        WHEN 'read_conversion_replay' THEN v_expected := v_expected || ARRAY['task_id','actor_id','idempotency_key'];
        WHEN 'apply_conversion' THEN v_expected := v_expected || ARRAY[
            'plan_token_hash','plan_id','task_id','project_id','actor_id','expected_batch_id',
            'expected_status','expected_review_round_id','execution_id','idempotency_key',
            'execution_digest','scope_fingerprint','source_fences','actions','report'];
        WHEN 'finalize_conversion' THEN v_expected := v_expected || ARRAY[
            'plan_token_hash','task_id','actor_id','idempotency_key','operation_id','response_json'];
        WHEN 'read_camera_links' THEN v_expected := v_expected || ARRAY['task_id','roles'];
        WHEN 'read_camera_graph' THEN v_expected := v_expected || ARRAY[
            'task_id','project_id','scene_track_id','include_inactive'];
        WHEN 'lock_camera_member_scope' THEN v_expected := v_expected || ARRAY[
            'action','task_id','project_id','actor_id','member_id','source_annotation_id',
            'expected_version','expected_track_revision'];
        WHEN 'mutate_camera_member' THEN v_expected := v_expected || ARRAY[
            'action','task_id','project_id','actor_id','member_id','source_annotation_id','camera_role',
            'camera_item_id','bbox','visibility','expected_version','expected_track_revision',
            'expected_calibration_revision','expected_calibration_digest'];
        WHEN 'create_mask_format_import' THEN v_expected := v_expected || ARRAY[
            'id','project_id','requested_by_id','format_id','adapter_version','manifest_version',
            'staged_object_key','staged_sha256','mapping_json','options_json','mapping_digest',
            'options_digest','plan_json','plan_digest','token_hash','receipt_expires_at','result_json'];
        WHEN 'read_mask_format_import' THEN v_expected := v_expected || ARRAY['id','project_id'];
        WHEN 'lock_mask_format_import_receipt' THEN v_expected := v_expected || ARRAY[
            'token_hash','project_id','requested_by_id'];
        WHEN 'lock_mask_format_import' THEN v_expected := v_expected || ARRAY[
            'id','project_id','async_job_id'];
        WHEN 'link_mask_format_import_job' THEN v_expected := v_expected || ARRAY[
            'id','project_id','requested_by_id','async_job_id','resume'];
        WHEN 'mark_mask_format_import_running' THEN v_expected := v_expected || ARRAY['id','async_job_id'];
        WHEN 'record_mask_format_import_item' THEN v_expected := v_expected || ARRAY[
            'id','async_job_id','item_key','item_result'];
        WHEN 'finish_mask_format_import' THEN v_expected := v_expected || ARRAY[
            'action','id','async_job_id','failed','completed','skipped'];
        WHEN 'cancel_pending_mask_format_import' THEN v_expected := v_expected || ARRAY['async_job_id'];
        ELSE RAISE EXCEPTION 'Invalid Annotation transfer operation' USING ERRCODE='22023';
    END CASE;
    IF NOT aap_labeling_v1._annotation_transfer_exact_fields(p_request, v_expected) THEN
        RAISE EXCEPTION 'Invalid Annotation transfer fields' USING ERRCODE='22023';
    END IF;

    IF v_operation = 'resolve_task' THEN
        v_project_id := (p_request ->> 'project_id')::uuid;
        IF NULLIF(p_request ->> 'display_id','') IS NOT NULL THEN
            SELECT task.* INTO v_row FROM public.tasks AS task
            WHERE task.display_id = p_request ->> 'display_id';
            IF FOUND AND v_row.project_id = v_project_id THEN
                v_task := jsonb_build_object('id',v_row.id,'project_id',v_row.project_id,
                    'dataset_item_id',v_row.dataset_item_id,'display_id',v_row.display_id,
                    'file_name',v_row.file_name,'file_path',v_row.file_path,'file_type',v_row.file_type);
            END IF;
        END IF;
        IF v_task IS NULL AND NULLIF(p_request ->> 'file_path','') IS NOT NULL THEN
            v_rel := ltrim(replace(p_request ->> 'file_path', E'\\', '/'), '/');
            IF strpos(v_rel, '/') > 0 THEN
                SELECT task.* INTO v_row FROM public.tasks AS task
                WHERE task.project_id = v_project_id
                  AND (task.file_path = v_rel OR right(task.file_path, length('/' || v_rel)) = '/' || v_rel)
                ORDER BY task.file_path LIMIT 1;
                IF FOUND THEN
                    v_task := jsonb_build_object('id',v_row.id,'project_id',v_row.project_id,
                        'dataset_item_id',v_row.dataset_item_id,'display_id',v_row.display_id,
                        'file_name',v_row.file_name,'file_path',v_row.file_path,'file_type',v_row.file_type);
                END IF;
            END IF;
            IF v_task IS NULL THEN
                SELECT task.* INTO v_row FROM public.tasks AS task
                WHERE task.project_id = v_project_id AND task.file_path = p_request ->> 'file_path';
                IF FOUND THEN
                    v_task := jsonb_build_object('id',v_row.id,'project_id',v_row.project_id,
                        'dataset_item_id',v_row.dataset_item_id,'display_id',v_row.display_id,
                        'file_name',v_row.file_name,'file_path',v_row.file_path,'file_type',v_row.file_type);
                END IF;
            END IF;
        END IF;
        v_data := jsonb_build_object('task',v_task);
    ELSIF v_operation = 'resolve_task_by_stem' THEN
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_rel := p_request ->> 'normalized_path';
        IF v_rel IS NULL OR v_rel = '' THEN
            v_reason := 'empty file stem';
        ELSE
            v_target := replace(replace(replace(v_rel, E'\\', E'\\\\'), '%', E'\\%'), '_', E'\\_');
            IF strpos(v_rel, '/') > 0 THEN
                FOR v_row IN SELECT task.* FROM public.tasks AS task
                    WHERE task.project_id = v_project_id
                      AND (task.file_path LIKE v_target || '.%' ESCAPE E'\\'
                           OR task.file_path LIKE '%/' || v_target || '.%' ESCAPE E'\\')
                    ORDER BY task.file_path LIMIT 2
                LOOP
                    v_tasks := v_tasks || jsonb_build_array(jsonb_build_object(
                        'id',v_row.id,'project_id',v_row.project_id,'dataset_item_id',v_row.dataset_item_id,
                        'display_id',v_row.display_id,'file_name',v_row.file_name,
                        'file_path',v_row.file_path,'file_type',v_row.file_type));
                END LOOP;
            ELSE
                v_target := replace(replace(replace(split_part(v_rel, '/', array_length(string_to_array(v_rel, '/'),1)), E'\\', E'\\\\'), '%', E'\\%'), '_', E'\\_');
                FOR v_row IN SELECT task.* FROM public.tasks AS task
                    WHERE task.project_id = v_project_id
                      AND (task.file_name LIKE v_target || '.%' ESCAPE E'\\'
                           OR task.file_path LIKE '%/' || v_target || '.%' ESCAPE E'\\')
                    ORDER BY task.file_path LIMIT 2
                LOOP
                    v_tasks := v_tasks || jsonb_build_array(jsonb_build_object(
                        'id',v_row.id,'project_id',v_row.project_id,'dataset_item_id',v_row.dataset_item_id,
                        'display_id',v_row.display_id,'file_name',v_row.file_name,
                        'file_path',v_row.file_path,'file_type',v_row.file_type));
                END LOOP;
            END IF;
            IF jsonb_array_length(v_tasks) = 0 THEN v_reason := 'task not found in project';
            ELSIF jsonb_array_length(v_tasks) > 1 THEN v_reason := 'ambiguous task file stem: ' || v_rel;
            END IF;
        END IF;
        v_data := jsonb_build_object('tasks',v_tasks,'reason',v_reason);
    ELSIF v_operation = 'resolve_video_sequence' THEN
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_target := p_request ->> 'target';
        SELECT task.* INTO v_row FROM public.tasks AS task
        WHERE task.project_id = v_project_id
          AND v_target IN (
              aap_labeling_v1._annotation_transfer_without_suffix(ltrim(task.file_path, '/')),
              aap_labeling_v1._annotation_transfer_without_suffix(CASE WHEN strpos(ltrim(task.file_path, '/'), '/') > 0
                    THEN substring(ltrim(task.file_path, '/') FROM strpos(ltrim(task.file_path, '/'), '/') + 1)
                    ELSE ltrim(task.file_path, '/') END),
              aap_labeling_v1._annotation_transfer_without_suffix(regexp_replace(ltrim(task.file_path, '/'), '^.*/', ''))
          )
        ORDER BY task.id LIMIT 1;
        IF FOUND THEN
            v_task := jsonb_build_object('id',v_row.id,'project_id',v_row.project_id,
                'dataset_item_id',v_row.dataset_item_id,'display_id',v_row.display_id,
                'file_name',v_row.file_name,'file_path',v_row.file_path,'file_type',v_row.file_type);
        END IF;
        v_data := jsonb_build_object('task',v_task);
    ELSIF v_operation = 'read_import_camera_facts' THEN
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_track_id := p_request ->> 'track_id';
        SELECT link.dataset_item_id INTO v_camera_item_id
        FROM public.task_dataset_item_links AS link
        WHERE link.task_id = v_task_id AND link.role = p_request ->> 'sensor_role';
        SELECT annotation.scene_track_id INTO v_scene_track_id
        FROM public.annotations AS annotation
        WHERE annotation.task_id = v_task_id AND annotation.track_id = v_track_id
          AND annotation.sensor_role IS NULL AND annotation.scene_track_id IS NOT NULL
          AND annotation.is_active IS TRUE
          AND ((p_request ->> 'overwrite')::boolean IS FALSE
               OR COALESCE(annotation.attributes ->> '_imported','false') <> 'true')
        LIMIT 1;
        v_primary_exists := v_scene_track_id IS NOT NULL;
        v_member_exists := false;
        IF v_scene_track_id IS NOT NULL THEN
            SELECT EXISTS (
                SELECT FROM public.annotations AS annotation
                WHERE annotation.task_id = v_task_id
                  AND annotation.scene_track_id = v_scene_track_id
                  AND annotation.sensor_role = p_request ->> 'sensor_role'
                  AND annotation.is_active IS TRUE AND annotation.was_cancelled IS FALSE
                  AND ((p_request ->> 'overwrite')::boolean IS FALSE
                       OR COALESCE(annotation.attributes ->> '_imported','false') <> 'true')
            ) INTO v_member_exists;
        END IF;
        v_data := jsonb_build_object('camera_facts',jsonb_build_object(
            'sensor_dataset_item_id',v_camera_item_id,'scene_track_id',v_scene_track_id,
            'primary_exists',v_primary_exists,'member_exists',v_member_exists));
    ELSIF v_operation = 'purge_imported_annotations' THEN
        v_task_id := (p_request ->> 'task_id')::uuid;
        SELECT COALESCE(array_agg(DISTINCT annotation.scene_track_id)
            FILTER (WHERE annotation.scene_track_id IS NOT NULL), ARRAY[]::uuid[])
        INTO v_track_ids FROM public.annotations AS annotation
        WHERE annotation.task_id = v_task_id AND annotation.attributes ->> '_imported' = 'true';
        DELETE FROM public.annotations AS annotation
        WHERE annotation.task_id = v_task_id AND annotation.attributes ->> '_imported' = 'true';
        GET DIAGNOSTICS v_deleted = ROW_COUNT;
        IF cardinality(v_track_ids) > 0 THEN
            UPDATE public.scene_tracks AS track
            SET revision = track.revision + 1, updated_at = transaction_timestamp()
            WHERE track.id = ANY(v_track_ids);
        END IF;
        v_data := jsonb_build_object('purged_count',v_deleted);
    ELSIF v_operation = 'create_imported_annotation' THEN
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        v_annotation_id := (p_request ->> 'annotation_id')::uuid;
        PERFORM task.id FROM public.tasks AS task
        WHERE task.id = v_task_id AND task.project_id = v_project_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                DETAIL='{"status_code":409,"reason":"task_target_changed"}';
        END IF;
        IF jsonb_typeof(p_request -> 'geometry') IS DISTINCT FROM 'object'
           OR jsonb_typeof(p_request -> 'attributes') IS DISTINCT FROM 'object'
           OR p_request ->> 'source' NOT IN ('manual','prediction_based','interpolated')
           OR p_request ->> 'temporal_role' NOT IN ('keyframe','derived','sample')
           OR jsonb_typeof(p_request -> 'confidence') NOT IN ('number','null')
           OR jsonb_typeof(p_request -> 'created_at') NOT IN ('string','null') THEN
            RAISE EXCEPTION 'Invalid imported Annotation data' USING ERRCODE='22023';
        END IF;
        INSERT INTO public.annotations (
            id,task_id,project_id,user_id,source,annotation_type,tool_unit_id,class_name,geometry,
            track_id,confidence,was_cancelled,ground_truth,attributes,temporal_role,
            scene_track_id,sensor_dataset_item_id,sensor_role,sensor_visibility,
            calibration_revision,calibration_digest,created_at
        ) VALUES (
            v_annotation_id,v_task_id,v_project_id,v_actor_id,p_request ->> 'source',
            p_request ->> 'annotation_type',p_request ->> 'tool_unit_id',p_request ->> 'class_name',
            p_request -> 'geometry',p_request ->> 'track_id',(p_request ->> 'confidence')::double precision,
            false,false,p_request -> 'attributes',p_request ->> 'temporal_role',
            (p_request ->> 'scene_track_id')::uuid,(p_request ->> 'sensor_dataset_item_id')::uuid,
            p_request ->> 'sensor_role',p_request ->> 'sensor_visibility',
            (p_request ->> 'calibration_revision')::integer,p_request ->> 'calibration_digest',
            COALESCE((p_request ->> 'created_at')::timestamptz,transaction_timestamp())
        );
        v_scene_track_id := (p_request ->> 'scene_track_id')::uuid;
        IF p_request ->> 'sensor_role' IS NOT NULL THEN
            IF v_scene_track_id IS NULL OR NOT EXISTS (
                SELECT FROM public.task_dataset_item_links AS link
                WHERE link.task_id = v_task_id AND link.role = p_request ->> 'sensor_role'
                  AND link.dataset_item_id = (p_request ->> 'sensor_dataset_item_id')::uuid
            ) THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":409,"reason":"camera_role_not_linked"}';
            END IF;
            PERFORM track.id FROM public.scene_tracks AS track
            WHERE track.id = v_scene_track_id AND track.project_id = v_project_id
              AND track.retired_at IS NULL FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":409,"reason":"scene_track_missing"}';
            END IF;
            UPDATE public.scene_tracks AS track
            SET revision = track.revision + 1, updated_at = transaction_timestamp()
            WHERE track.id = v_scene_track_id;
        ELSIF p_request ->> 'scene_id' IS NOT NULL
           AND p_request ->> 'frame_index' IS NOT NULL
           AND p_request #>> '{geometry,type}' = 'box_3d'
           AND p_request ->> 'track_id' IS NOT NULL THEN
            v_scene_reply := aap_labeling_v1.scene_track_domain_command(jsonb_build_object(
                'schema','aap-labeling-scene-track-domain/v1','action','ensure',
                'project_id',v_project_id,'scene_id',(p_request ->> 'scene_id')::uuid,
                'track_id',p_request ->> 'track_id','class_name',p_request ->> 'class_name',
                'frames',jsonb_build_array((p_request ->> 'frame_index')::integer),
                'actor_id',v_actor_id,'interval_source','imported','member_change',true));
            IF v_scene_reply ->> 'ok' IS DISTINCT FROM 'true' THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                    DETAIL=jsonb_build_object('status_code',409,'reason',
                        COALESCE(v_scene_reply #>> '{error,code}','scene_track_unavailable'))::text;
            END IF;
            v_scene_track_id := (v_scene_reply #>> '{data,track,id}')::uuid;
            UPDATE public.annotations AS annotation
            SET scene_track_id = v_scene_track_id, updated_at = transaction_timestamp()
            WHERE annotation.id = v_annotation_id;
        END IF;
        v_data := jsonb_build_object('imported_annotation',jsonb_build_object(
            'annotation_id',v_annotation_id,'scene_track_id',v_scene_track_id));
    ELSIF v_operation = 'replace_imported_scene_track' THEN
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_track_id := p_request ->> 'track_id';
        IF jsonb_typeof(p_request -> 'intervals') IS DISTINCT FROM 'array'
           OR jsonb_typeof(p_request -> 'attributes') IS DISTINCT FROM 'object'
           OR jsonb_typeof(p_request -> 'attributes_meta') IS DISTINCT FROM 'object'
           OR p_request ->> 'presence_mode' NOT IN ('inferred','explicit') THEN
            RAISE EXCEPTION 'Invalid imported SceneTrack payload' USING ERRCODE='22023';
        END IF;
        FOR v_interval IN SELECT value FROM jsonb_array_elements(p_request -> 'intervals') AS rows(value)
        LOOP
            IF NOT aap_labeling_v1._annotation_transfer_exact_fields(
                    v_interval,ARRAY['end_frame','interval_id','start_frame']::text[]
               )
               OR jsonb_typeof(v_interval -> 'interval_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(v_interval -> 'start_frame') IS DISTINCT FROM 'number'
               OR (v_interval ->> 'start_frame') !~ '^[0-9]+$'
               OR jsonb_typeof(v_interval -> 'end_frame') NOT IN ('number','null')
               OR ((v_interval ->> 'end_frame') IS NOT NULL
                   AND (v_interval ->> 'end_frame') !~ '^[0-9]+$')
               OR ((v_interval ->> 'end_frame') IS NOT NULL
                   AND (v_interval ->> 'end_frame')::integer < (v_interval ->> 'start_frame')::integer) THEN
                RAISE EXCEPTION 'Invalid imported SceneTrack interval' USING ERRCODE='22023';
            END IF;
        END LOOP;
        SELECT track.* INTO v_track FROM public.scene_tracks AS track
        JOIN aap_assets_v1.spatial_scene_references AS scene ON scene.scene_id = track.scene_id
        WHERE track.project_id = v_project_id AND track.track_id = v_track_id
          AND (p_request ->> 'scene_name' IS NULL OR scene.scene_name = p_request ->> 'scene_name')
        ORDER BY track.id LIMIT 2 FOR UPDATE OF track;
        IF NOT FOUND THEN
            v_reason := 'scene track metadata could not be matched uniquely';
        ELSE
            SELECT count(*) INTO v_count FROM public.scene_tracks AS track
            JOIN aap_assets_v1.spatial_scene_references AS scene ON scene.scene_id = track.scene_id
            WHERE track.project_id = v_project_id AND track.track_id = v_track_id
              AND (p_request ->> 'scene_name' IS NULL OR scene.scene_name = p_request ->> 'scene_name');
            IF v_count <> 1 THEN
                v_reason := 'scene track metadata could not be matched uniquely';
            ELSIF v_track.class_name IS DISTINCT FROM p_request ->> 'class_name' THEN
                v_reason := 'scene track class conflicts with imported members';
            ELSE
                v_previous_end := NULL;
                v_previous_exists := false;
                FOR v_interval IN SELECT value FROM jsonb_array_elements(p_request -> 'intervals') AS rows(value)
                    ORDER BY (value ->> 'start_frame')::integer,
                             COALESCE((value ->> 'end_frame')::integer,2147483647)
                LOOP
                    v_start := (v_interval ->> 'start_frame')::integer;
                    v_end := (v_interval ->> 'end_frame')::integer;
                    IF v_previous_exists AND (v_previous_end IS NULL OR v_previous_end + 1 >= v_start) THEN
                        v_reason := 'scene track intervals overlap or are adjacent';
                        EXIT;
                    END IF;
                    v_previous_end := v_end;
                    v_previous_exists := true;
                END LOOP;
                IF v_reason IS NULL THEN
                    FOR v_frame IN
                        WITH scene_items AS (
                            SELECT item.item_id,item.frame_index
                            FROM aap_assets_v1.spatial_item_references AS item
                            WHERE item.scene_id=v_track.scene_id AND item.frame_index IS NOT NULL
                        ), item_tasks AS (
                            SELECT task.dataset_item_id AS item_id,task.id AS task_id,0 AS priority
                            FROM public.tasks AS task
                            JOIN scene_items AS item ON item.item_id=task.dataset_item_id
                            UNION ALL
                            SELECT link.dataset_item_id AS item_id,link.task_id,1 AS priority
                            FROM public.task_dataset_item_links AS link
                            JOIN scene_items AS item ON item.item_id=link.dataset_item_id
                            WHERE link.role='primary_lidar'
                        ), item_to_task AS (
                            SELECT DISTINCT ON (mapping.item_id) mapping.item_id,mapping.task_id
                            FROM item_tasks AS mapping
                            ORDER BY mapping.item_id,mapping.priority,mapping.task_id
                        ), frame_to_task AS (
                            SELECT DISTINCT ON (item.frame_index) item.frame_index,mapping.task_id
                            FROM scene_items AS item
                            JOIN item_to_task AS mapping ON mapping.item_id=item.item_id
                            ORDER BY item.frame_index,item.item_id
                        ), task_frames AS (
                            SELECT DISTINCT ON (mapping.task_id) mapping.task_id,mapping.frame_index
                            FROM frame_to_task AS mapping
                            ORDER BY mapping.task_id,mapping.frame_index DESC
                        )
                        SELECT DISTINCT mapping.frame_index
                        FROM public.annotations AS annotation
                        JOIN task_frames AS mapping ON mapping.task_id=annotation.task_id
                        WHERE annotation.scene_track_id=v_track.id
                          AND annotation.is_active IS TRUE AND annotation.was_cancelled IS FALSE
                    LOOP
                        IF NOT EXISTS (
                            SELECT FROM jsonb_array_elements(p_request -> 'intervals') AS rows(value)
                            WHERE (rows.value ->> 'start_frame')::integer <= v_frame
                              AND ((rows.value ->> 'end_frame') IS NULL
                                   OR (rows.value ->> 'end_frame')::integer >= v_frame)
                        ) THEN
                            v_reason := 'scene track intervals do not cover every imported member';
                            EXIT;
                        END IF;
                    END LOOP;
                END IF;
                IF v_reason IS NULL THEN
                    DELETE FROM public.scene_track_intervals AS interval
                    WHERE interval.scene_track_id = v_track.id;
                    INSERT INTO public.scene_track_intervals(
                        id,scene_track_id,start_frame,end_frame,source,version,created_by
                    )
                    SELECT (value ->> 'interval_id')::uuid,v_track.id,
                           (value ->> 'start_frame')::integer,(value ->> 'end_frame')::integer,
                           'imported',1,(p_request ->> 'actor_id')::uuid
                    FROM jsonb_array_elements(p_request -> 'intervals') AS rows(value);
                    UPDATE public.scene_tracks AS track
                    SET attributes=p_request -> 'attributes',
                        attributes_meta=p_request -> 'attributes_meta',
                        presence_mode=p_request ->> 'presence_mode',
                        revision=track.revision+1,updated_at=transaction_timestamp()
                    WHERE track.id=v_track.id;
                    v_scene_track_id := v_track.id;
                END IF;
            END IF;
        END IF;
        v_data := jsonb_build_object('scene_track',jsonb_build_object(
            'updated',v_reason IS NULL,'reason',v_reason,'scene_track_id',v_scene_track_id));
    ELSIF v_operation = 'create_conversion_plan' THEN
        INSERT INTO public.annotation_conversion_plans(
            id,token_hash,task_id,actor_id,request_digest,snapshot_digest,
            request_json,plan_json,expires_at
        ) VALUES (
            (p_request ->> 'plan_id')::uuid,p_request ->> 'token_hash',
            (p_request ->> 'task_id')::uuid,(p_request ->> 'actor_id')::uuid,
            p_request ->> 'request_digest',p_request ->> 'snapshot_digest',
            p_request -> 'request_json',p_request -> 'plan_json',
            (p_request ->> 'expires_at')::timestamptz
        ) RETURNING to_jsonb(annotation_conversion_plans.*) INTO v_conversion_plan;
        v_data := jsonb_build_object('plan',v_conversion_plan);
    ELSIF v_operation = 'read_conversion_plan' THEN
        SELECT to_jsonb(plan.*) INTO v_conversion_plan
        FROM public.annotation_conversion_plans AS plan
        WHERE plan.token_hash = p_request ->> 'token_hash';
        v_data := jsonb_build_object('plan',v_conversion_plan);
    ELSIF v_operation = 'lock_conversion_plan' THEN
        SELECT plan.* INTO v_plan
        FROM public.annotation_conversion_plans AS plan
        WHERE plan.token_hash = p_request ->> 'token_hash'
        FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                DETAIL='{"status_code":404,"reason":"plan_token_invalid"}';
        END IF;
        IF v_plan.task_id IS DISTINCT FROM (p_request ->> 'task_id')::uuid THEN
            RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                DETAIL='{"status_code":404,"reason":"plan_token_invalid"}';
        END IF;
        IF v_plan.actor_id IS DISTINCT FROM (p_request ->> 'actor_id')::uuid THEN
            RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                DETAIL='{"status_code":403,"reason":"plan_actor_mismatch"}';
        END IF;
        v_conversion_plan := to_jsonb(v_plan);
        v_data := jsonb_build_object('plan',v_conversion_plan);
    ELSIF v_operation = 'read_conversion_sources' THEN
        v_task_id := (p_request ->> 'task_id')::uuid;
        IF jsonb_typeof(p_request -> 'annotation_ids') IS DISTINCT FROM 'array'
           OR EXISTS (SELECT FROM jsonb_array_elements(p_request -> 'annotation_ids') AS ids(value)
                      WHERE jsonb_typeof(ids.value) IS DISTINCT FROM 'string') THEN
            RAISE EXCEPTION 'Invalid conversion source identities' USING ERRCODE='22023';
        END IF;
        SELECT COALESCE(array_agg(value::uuid ORDER BY value::uuid),ARRAY[]::uuid[])
        INTO v_annotation_ids
        FROM jsonb_array_elements_text(p_request -> 'annotation_ids') AS ids(value);
        IF cardinality(v_annotation_ids) <> cardinality(ARRAY(SELECT DISTINCT unnest(v_annotation_ids))) THEN
            RAISE EXCEPTION 'Duplicate conversion source identities' USING ERRCODE='22023';
        END IF;
        v_camera_sources := '[]'::jsonb;
        IF (p_request ->> 'lock')::boolean THEN
            FOR v_source IN SELECT annotation.* FROM public.annotations AS annotation
                WHERE annotation.task_id=v_task_id AND annotation.id=ANY(v_annotation_ids)
                  AND annotation.is_active IS TRUE AND annotation.was_cancelled IS FALSE
                ORDER BY annotation.id FOR UPDATE
            LOOP
                v_camera_sources := v_camera_sources || jsonb_build_array(to_jsonb(v_source));
            END LOOP;
        ELSE
            FOR v_source IN SELECT annotation.* FROM public.annotations AS annotation
                WHERE annotation.task_id=v_task_id AND annotation.id=ANY(v_annotation_ids)
                  AND annotation.is_active IS TRUE AND annotation.was_cancelled IS FALSE
                ORDER BY annotation.id
            LOOP
                v_camera_sources := v_camera_sources || jsonb_build_array(to_jsonb(v_source));
            END LOOP;
        END IF;
        v_data := jsonb_build_object('conversion_sources',v_camera_sources);
    ELSIF v_operation = 'read_conversion_video_segments' THEN
        FOR v_row IN SELECT segment.id,segment.dataset_item_id,segment.start_frame,
                segment.end_frame,segment.segment_index,segment.locked_by,segment.lock_expires_at
            FROM public.video_segments AS segment
            WHERE segment.dataset_item_id=(p_request ->> 'dataset_item_id')::uuid
            ORDER BY segment.segment_index
            FOR UPDATE
        LOOP
            v_video_segments := v_video_segments || jsonb_build_array(jsonb_build_object(
                'id',v_row.id,'dataset_item_id',v_row.dataset_item_id,
                'start_frame',v_row.start_frame,'end_frame',v_row.end_frame,
                'segment_index',v_row.segment_index,'locked_by',v_row.locked_by,
                'lock_expires_at',v_row.lock_expires_at));
        END LOOP;
        v_data := jsonb_build_object('video_segments',v_video_segments);
    ELSIF v_operation = 'expire_conversion_plans' THEN
        DELETE FROM public.annotation_conversion_plans AS plan
        WHERE plan.expires_at <= (p_request ->> 'now')::timestamptz;
        GET DIAGNOSTICS v_deleted = ROW_COUNT;
        v_data := jsonb_build_object('expired_count',v_deleted);
    ELSIF v_operation = 'read_conversion_replay' THEN
        SELECT operation.* INTO v_existing_operation
        FROM public.annotation_operations AS operation
        WHERE operation.task_id=(p_request ->> 'task_id')::uuid
          AND operation.actor_id=(p_request ->> 'actor_id')::uuid
          AND operation.idempotency_key=p_request ->> 'idempotency_key';
        IF FOUND THEN
            v_data := jsonb_build_object('replay',jsonb_build_object(
                'operation_id',v_existing_operation.id,
                'request_digest',v_existing_operation.request_digest,
                'response_json',v_existing_operation.response_json));
        ELSE
            v_data := jsonb_build_object('replay',NULL);
        END IF;
    ELSIF v_operation = 'apply_conversion' THEN
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        v_execution_id := (p_request ->> 'execution_id')::uuid;
        v_idempotency_key := p_request ->> 'idempotency_key';
        v_execution_digest := p_request ->> 'execution_digest';
        SELECT task.* INTO v_row FROM public.tasks AS task
        WHERE task.id=v_task_id AND task.project_id=v_project_id
          AND task.batch_id IS NOT DISTINCT FROM (p_request ->> 'expected_batch_id')::uuid
          AND task.status IS NOT DISTINCT FROM p_request ->> 'expected_status'
          AND task.review_round_id IS NOT DISTINCT FROM (p_request ->> 'expected_review_round_id')::uuid
        FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_transfer_error',
                DETAIL='{"status_code":409,"reason":"task_target_changed"}';
        END IF;
        SELECT plan.* INTO v_plan FROM public.annotation_conversion_plans AS plan
        WHERE plan.id=(p_request ->> 'plan_id')::uuid
          AND plan.token_hash=p_request ->> 'plan_token_hash'
          AND plan.task_id=v_task_id AND plan.actor_id=v_actor_id
        FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                DETAIL='{"status_code":404,"reason":"plan_token_invalid"}';
        END IF;
        IF v_plan.executed_operation_id IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                DETAIL='{"status_code":409,"reason":"plan_consumed"}';
        END IF;
        IF v_plan.expires_at <= transaction_timestamp() THEN
            RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                DETAIL='{"status_code":409,"reason":"plan_expired"}';
        END IF;
        IF EXISTS (
            SELECT FROM public.annotation_operations AS operation
            WHERE operation.task_id=v_task_id AND operation.actor_id=v_actor_id
              AND operation.idempotency_key=v_idempotency_key
        ) THEN
            RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                DETAIL='{"status_code":409,"reason":"idempotency_conflict"}';
        END IF;
        IF jsonb_typeof(v_plan.plan_json -> 'sources') IS DISTINCT FROM 'array'
           OR jsonb_typeof(p_request -> 'source_fences') IS DISTINCT FROM 'array'
           OR jsonb_array_length(v_plan.plan_json -> 'sources') <> jsonb_array_length(p_request -> 'source_fences')
           OR jsonb_typeof(v_plan.plan_json -> 'source_snapshots') IS DISTINCT FROM 'array'
           OR jsonb_array_length(v_plan.plan_json -> 'source_snapshots') <> jsonb_array_length(p_request -> 'source_fences')
           OR jsonb_typeof(v_plan.plan_json -> 'actions') IS DISTINCT FROM 'array'
           OR jsonb_typeof(p_request -> 'actions') IS DISTINCT FROM 'array'
           OR jsonb_array_length(v_plan.plan_json -> 'actions') <> jsonb_array_length(p_request -> 'actions') THEN
            RAISE EXCEPTION 'Invalid conversion plan scope' USING ERRCODE='22023';
        END IF;
        SELECT array_agg((value ->> 'annotation_id')::uuid ORDER BY (value ->> 'annotation_id')::uuid)
        INTO v_source_ids FROM jsonb_array_elements(p_request -> 'source_fences') AS entries(value);
        IF cardinality(v_source_ids) <> cardinality(ARRAY(SELECT DISTINCT unnest(v_source_ids))) THEN
            RAISE EXCEPTION 'Duplicate conversion source fences' USING ERRCODE='22023';
        END IF;
        FOR v_fence IN SELECT value FROM jsonb_array_elements(p_request -> 'source_fences') AS entries(value)
            ORDER BY (value ->> 'annotation_id')::uuid
        LOOP
            IF jsonb_typeof(v_fence) IS DISTINCT FROM 'object'
               OR NOT aap_labeling_v1._annotation_transfer_exact_fields(
                    v_fence,ARRAY['annotation_id','digest','version']::text[]
               )
               OR jsonb_typeof(v_fence -> 'annotation_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(v_fence -> 'digest') IS DISTINCT FROM 'string'
               OR jsonb_typeof(v_fence -> 'version') IS DISTINCT FROM 'number'
               OR (v_fence ->> 'version') !~ '^[1-9][0-9]*$' THEN
                RAISE EXCEPTION 'Invalid conversion source fence' USING ERRCODE='22023';
            END IF;
            SELECT annotation.* INTO v_source FROM public.annotations AS annotation
            WHERE annotation.id=(v_fence ->> 'annotation_id')::uuid
              AND annotation.task_id=v_task_id
            FOR UPDATE;
            IF NOT FOUND OR v_source.project_id IS DISTINCT FROM v_project_id
               OR v_source.is_active IS NOT TRUE OR v_source.was_cancelled IS TRUE THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":404,"reason":"annotation_not_found"}';
            END IF;
            IF v_source.is_locked IS TRUE THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":409,"reason":"annotation_locked"}';
            END IF;
            SELECT value INTO v_expected_sources
            FROM jsonb_array_elements(v_plan.plan_json -> 'sources') AS expected(value)
            WHERE value ->> 'id'=v_source.id::text;
            IF v_expected_sources IS NULL
               OR (v_expected_sources ->> 'version')::integer IS DISTINCT FROM (v_fence ->> 'version')::integer
               OR (v_expected_sources ->> 'digest') IS DISTINCT FROM v_fence ->> 'digest' THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":409,"reason":"plan_report_mismatch"}';
            END IF;
            v_source_snapshot := jsonb_build_object(
                'id',v_source.id,'version',COALESCE(v_source.version,1),
                'annotation_type',v_source.annotation_type,'tool_unit_id',v_source.tool_unit_id,
                'class_name',v_source.class_name,'geometry',v_source.geometry,'source',v_source.source,
                'confidence',v_source.confidence,'attributes',COALESCE(v_source.attributes,'{}'::jsonb),
                'attributes_meta',COALESCE(v_source.attributes_meta,'{}'::jsonb),
                'z_order',v_source.z_order,'is_locked',v_source.is_locked,'is_active',v_source.is_active);
            SELECT value -> 'snapshot' INTO v_source_json
            FROM jsonb_array_elements(v_plan.plan_json -> 'source_snapshots') AS snapshots(value)
            WHERE value ->> 'id'=v_source.id::text;
            IF COALESCE(v_source.version,1) IS DISTINCT FROM (v_fence ->> 'version')::integer
               OR v_source_snapshot IS DISTINCT FROM v_source_json THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":409,"reason":"snapshot_stale"}';
            END IF;
            v_source_versions := v_source_versions || jsonb_build_object(v_source.id::text,COALESCE(v_source.version,1));
        END LOOP;
        v_item_index := 0;
        FOR v_action IN SELECT value FROM jsonb_array_elements(p_request -> 'actions') WITH ORDINALITY AS entries(value, ordinal)
            ORDER BY ordinal
        LOOP
            SELECT value INTO v_expected_action
            FROM jsonb_array_elements(v_plan.plan_json -> 'actions') WITH ORDINALITY AS entries(value, ordinal)
            WHERE ordinal=v_item_index+1;
            IF jsonb_typeof(v_action) IS DISTINCT FROM 'object'
               OR NOT aap_labeling_v1._annotation_transfer_exact_fields(
                    v_action,ARRAY['annotation_type','frame_index','geometry','kind',
                        'result_id','source_id','tool_unit_id','track_id']::text[]
               )
               OR jsonb_typeof(v_action -> 'kind') IS DISTINCT FROM 'string'
               OR jsonb_typeof(v_action -> 'source_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(v_action -> 'frame_index') NOT IN ('number','null')
               OR jsonb_typeof(v_action -> 'result_id') NOT IN ('string','null')
               OR jsonb_typeof(v_action -> 'geometry') NOT IN ('object','null')
               OR jsonb_typeof(v_action -> 'annotation_type') NOT IN ('string','null')
               OR jsonb_typeof(v_action -> 'tool_unit_id') NOT IN ('string','null')
               OR jsonb_typeof(v_action -> 'track_id') NOT IN ('string','null')
               OR v_expected_action IS NULL
               OR NOT ((v_action ->> 'source_id')::uuid=ANY(v_source_ids))
               OR v_action ->> 'kind' IS DISTINCT FROM v_expected_action ->> 'kind'
               OR v_action ->> 'source_id' IS DISTINCT FROM v_expected_action ->> 'source_id'
               OR v_action -> 'frame_index' IS DISTINCT FROM v_expected_action -> 'frame_index' THEN
                RAISE EXCEPTION 'Conversion action does not match its plan' USING ERRCODE='22023';
            END IF;
            IF v_action ->> 'kind' IN ('create','update') THEN
                IF jsonb_typeof(v_action -> 'geometry') IS DISTINCT FROM 'object'
                   OR jsonb_typeof(v_action -> 'track_id') NOT IN ('string','null')
                   OR (v_action ->> 'kind'='create'
                       AND jsonb_typeof(v_action -> 'result_id') IS DISTINCT FROM 'string')
                   OR (v_action ->> 'kind'='update' AND v_action -> 'result_id' IS DISTINCT FROM 'null'::jsonb) THEN
                    RAISE EXCEPTION 'Invalid conversion write action' USING ERRCODE='22023';
                END IF;
                IF v_action ->> 'annotation_type' IS DISTINCT FROM v_expected_action ->> 'annotation_type'
                   OR v_action ->> 'tool_unit_id' IS DISTINCT FROM v_expected_action ->> 'tool_unit_id'
                   OR aap_labeling_v1._annotation_transfer_without_track_ids(v_action -> 'geometry')
                      IS DISTINCT FROM aap_labeling_v1._annotation_transfer_without_track_ids(v_expected_action -> 'geometry') THEN
                    RAISE EXCEPTION 'Conversion action payload does not match its plan' USING ERRCODE='22023';
                END IF;
            ELSIF v_action ->> 'result_id' IS DISTINCT FROM 'null'::jsonb
               OR v_action -> 'geometry' IS DISTINCT FROM 'null'::jsonb
               OR v_action -> 'annotation_type' IS DISTINCT FROM 'null'::jsonb
               OR v_action -> 'tool_unit_id' IS DISTINCT FROM 'null'::jsonb
               OR v_action -> 'track_id' IS DISTINCT FROM 'null'::jsonb THEN
                RAISE EXCEPTION 'Invalid conversion deactivation action' USING ERRCODE='22023';
            END IF;
            v_item_index := v_item_index+1;
        END LOOP;
        v_item_index := 0;
        FOR v_action IN SELECT value FROM jsonb_array_elements(p_request -> 'actions') WITH ORDINALITY AS entries(value, ordinal)
            ORDER BY ordinal
        LOOP
            v_id := (v_action ->> 'source_id')::uuid;
            SELECT annotation.* INTO v_source FROM public.annotations AS annotation WHERE annotation.id=v_id;
            IF v_action ->> 'kind' = 'update' THEN
                UPDATE public.annotations AS annotation
                SET geometry=v_action -> 'geometry',
                    annotation_type=v_action ->> 'annotation_type',
                    tool_unit_id=v_action ->> 'tool_unit_id',
                    track_id=v_action ->> 'track_id',user_id=v_actor_id,
                    version=annotation.version+1,updated_at=transaction_timestamp()
                WHERE annotation.id=v_id
                RETURNING * INTO v_source;
                v_updated := v_updated || jsonb_build_array(to_jsonb(v_source));
                v_version := v_source.version;
                v_result_versions := v_result_versions || jsonb_build_object(v_source.id::text,v_version);
            ELSIF v_action ->> 'kind' = 'create' THEN
                v_created_id := (v_action ->> 'result_id')::uuid;
                INSERT INTO public.annotations(
                    id,task_id,project_id,video_segment_id,user_id,source,annotation_type,tool_unit_id,
                    class_name,geometry,track_id,confidence,parent_annotation_id,attributes,attributes_meta,z_order
                ) VALUES (
                    v_created_id,v_task_id,v_project_id,v_source.video_segment_id,v_actor_id,'manual',
                    v_action ->> 'annotation_type',v_action ->> 'tool_unit_id',v_source.class_name,
                    v_action -> 'geometry',v_action ->> 'track_id',NULL,v_source.id,
                    COALESCE(v_source.attributes,'{}'::jsonb),COALESCE(v_source.attributes_meta,'{}'::jsonb),v_source.z_order
                ) RETURNING * INTO v_source;
                v_created := v_created || jsonb_build_array(to_jsonb(v_source));
                v_version := v_source.version;
                v_result_versions := v_result_versions || jsonb_build_object(v_source.id::text,v_version);
            ELSE
                UPDATE public.annotations AS annotation
                SET is_active=false,user_id=v_actor_id,
                    version=annotation.version+1,updated_at=transaction_timestamp()
                WHERE annotation.id=v_id
                RETURNING version INTO v_version;
                v_deleted_ids := v_deleted_ids || jsonb_build_array(v_id);
                v_result_versions := v_result_versions || jsonb_build_object(v_id::text,v_version);
            END IF;
        END LOOP;
        INSERT INTO public.annotation_operations(
            id,task_id,actor_id,kind,idempotency_key,request_digest,scope_fingerprint,
            source_versions,result_versions,report,status,response_json
        ) VALUES (
            v_execution_id,v_task_id,v_actor_id,'convert_annotations',v_idempotency_key,
            v_execution_digest,p_request ->> 'scope_fingerprint',v_source_versions,
            v_result_versions,p_request -> 'report','committed','{}'::jsonb
        );
        FOR v_action IN SELECT value FROM jsonb_array_elements(p_request -> 'actions') WITH ORDINALITY AS entries(value, ordinal)
            ORDER BY ordinal
        LOOP
            IF v_action ->> 'kind' IN ('create','update') THEN
                v_id := (v_action ->> 'source_id')::uuid;
                v_created_id := CASE
                    WHEN v_action ->> 'kind'='create'
                        THEN (v_action ->> 'result_id')::uuid
                    ELSE v_id
                END;
                INSERT INTO public.annotation_lineage_edges(
                    id,operation_id,source_annotation_id,result_annotation_id,relation,
                    source_version,result_version,frame_index
                ) VALUES (
                    pg_catalog.gen_random_uuid(),v_execution_id,v_id,v_created_id,'converted',
                    (v_source_versions ->> v_id::text)::integer,
                    (v_result_versions ->> v_created_id::text)::integer,
                    (v_action ->> 'frame_index')::integer
                );
            END IF;
        END LOOP;
        UPDATE public.annotation_conversion_plans AS plan
        SET executed_operation_id=v_execution_id,executed_idempotency_key=v_idempotency_key
        WHERE plan.id=v_plan.id;
        v_data := jsonb_build_object('conversion',jsonb_build_object(
            'operation_id',v_execution_id,'updated_annotations',v_updated,
            'created_annotations',v_created,'deleted_annotation_ids',v_deleted_ids,
            'result_versions',v_result_versions));
    ELSIF v_operation = 'finalize_conversion' THEN
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        v_idempotency_key := p_request ->> 'idempotency_key';
        v_execution_id := (p_request ->> 'operation_id')::uuid;
        UPDATE public.annotation_operations AS operation
        SET response_json=p_request -> 'response_json'
        WHERE operation.id=v_execution_id AND operation.task_id=v_task_id
          AND operation.actor_id=v_actor_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                DETAIL='{"status_code":404,"reason":"conversion_operation_missing"}';
        END IF;
        UPDATE public.annotation_conversion_plans AS plan
        SET executed_idempotency_key=v_idempotency_key
        WHERE plan.token_hash=p_request ->> 'plan_token_hash'
          AND plan.task_id=v_task_id AND plan.actor_id=v_actor_id
          AND plan.executed_operation_id=v_execution_id
        RETURNING to_jsonb(plan.*) INTO v_conversion_plan;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                DETAIL='{"status_code":409,"reason":"plan_target_changed"}';
        END IF;
        v_data := jsonb_build_object('plan',v_conversion_plan);
    ELSIF v_operation = 'read_camera_links' THEN
        v_task_id := (p_request ->> 'task_id')::uuid;
        IF jsonb_typeof(p_request -> 'roles') IS DISTINCT FROM 'array'
           OR EXISTS (SELECT FROM jsonb_array_elements(p_request -> 'roles') AS roles(value)
                      WHERE jsonb_typeof(roles.value) IS DISTINCT FROM 'string') THEN
            RAISE EXCEPTION 'Invalid camera role list' USING ERRCODE='22023';
        END IF;
        FOR v_row IN SELECT link.role,link.dataset_item_id
            FROM public.task_dataset_item_links AS link
            WHERE link.task_id=v_task_id
              AND link.role IN (SELECT value #>> '{}' FROM jsonb_array_elements(p_request -> 'roles') AS roles(value))
            ORDER BY link.role
        LOOP
            v_camera_links := v_camera_links || jsonb_build_array(jsonb_build_object(
                'role',v_row.role,'dataset_item_id',v_row.dataset_item_id));
        END LOOP;
        v_data := jsonb_build_object('camera_links',v_camera_links);
    ELSIF v_operation = 'read_camera_graph' THEN
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_project_id := (p_request ->> 'project_id')::uuid;
        SELECT COALESCE(direct_item.scene_id,linked_item.scene_id) INTO v_task_scene_id
        FROM public.tasks AS task
        LEFT JOIN aap_assets_v1.spatial_item_references AS direct_item
          ON direct_item.item_id=task.dataset_item_id
        LEFT JOIN LATERAL (
            SELECT item.scene_id FROM public.task_dataset_item_links AS link
            JOIN aap_assets_v1.spatial_item_references AS item ON item.item_id=link.dataset_item_id
            WHERE link.task_id=task.id AND link.role='primary_lidar' LIMIT 1
        ) AS linked_item ON true
        WHERE task.id=v_task_id AND task.project_id=v_project_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                DETAIL='{"status_code":404,"reason":"task_not_found"}';
        END IF;
        SELECT COALESCE(array_agg(DISTINCT annotation.scene_track_id)
            FILTER (WHERE annotation.scene_track_id IS NOT NULL),ARRAY[]::uuid[])
        INTO v_track_ids FROM public.annotations AS annotation
        WHERE annotation.task_id=v_task_id AND annotation.sensor_role IS NOT NULL
          AND annotation.was_cancelled IS FALSE
          AND ((p_request ->> 'include_inactive')::boolean OR annotation.is_active IS TRUE)
          AND ((p_request ->> 'scene_track_id') IS NULL
               OR annotation.scene_track_id=(p_request ->> 'scene_track_id')::uuid);
        IF p_request ->> 'scene_track_id' IS NOT NULL THEN
            v_track_ids := array_append(v_track_ids,(p_request ->> 'scene_track_id')::uuid);
        END IF;
        FOR v_source IN SELECT annotation.* FROM public.annotations AS annotation
            WHERE annotation.task_id=v_task_id AND annotation.sensor_role IS NOT NULL
              AND annotation.was_cancelled IS FALSE
              AND ((p_request ->> 'include_inactive')::boolean OR annotation.is_active IS TRUE)
              AND ((p_request ->> 'scene_track_id') IS NULL
                   OR annotation.scene_track_id=(p_request ->> 'scene_track_id')::uuid)
            ORDER BY annotation.sensor_role,annotation.created_at,annotation.id
        LOOP
            v_camera_members := v_camera_members || jsonb_build_array(to_jsonb(v_source));
        END LOOP;
        FOR v_source IN SELECT annotation.* FROM public.annotations AS annotation
            WHERE annotation.task_id=v_task_id AND annotation.scene_track_id=ANY(v_track_ids)
              AND annotation.sensor_role IS NULL AND annotation.annotation_type='box_3d'
              AND annotation.is_active IS TRUE AND annotation.was_cancelled IS FALSE
            ORDER BY annotation.created_at,annotation.id
        LOOP
            v_camera_sources := v_camera_sources || jsonb_build_array(to_jsonb(v_source));
        END LOOP;
        FOR v_track IN SELECT track.* FROM public.scene_tracks AS track
            WHERE track.id=ANY(v_track_ids)
            ORDER BY track.id
        LOOP
            v_camera_tracks := v_camera_tracks || jsonb_build_array(jsonb_build_object(
                'id',v_track.id,'project_id',v_track.project_id,'scene_id',v_track.scene_id,
                'track_id',v_track.track_id,'class_name',v_track.class_name,
                'revision',v_track.revision,'retired_at',v_track.retired_at));
        END LOOP;
        v_data := jsonb_build_object('camera_graph',jsonb_build_object(
            'task_scene_id',v_task_scene_id,'members',v_camera_members,
            'sources',v_camera_sources,'tracks',v_camera_tracks));
    ELSIF v_operation = 'lock_camera_member_scope' THEN
        v_camera_action := p_request ->> 'action';
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_project_id := (p_request ->> 'project_id')::uuid;
        IF v_camera_action NOT IN ('create','update','restore')
           OR (v_camera_action='create' AND (p_request ->> 'source_annotation_id' IS NULL
               OR p_request ->> 'member_id' IS NOT NULL OR p_request ->> 'expected_version' IS NOT NULL))
           OR (v_camera_action IN ('update','restore') AND (p_request ->> 'member_id' IS NULL
               OR p_request ->> 'source_annotation_id' IS NOT NULL
               OR (p_request ->> 'expected_version') !~ '^[1-9][0-9]*$'))
           OR (p_request ->> 'expected_track_revision') !~ '^[1-9][0-9]*$' THEN
            RAISE EXCEPTION 'Invalid camera member lock scope' USING ERRCODE='22023';
        END IF;
        PERFORM task.id FROM public.tasks AS task
        WHERE task.id=v_task_id AND task.project_id=v_project_id FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_transfer_error',
                DETAIL='{"status_code":409,"reason":"task_target_changed"}';
        END IF;
        IF v_camera_action='create' THEN
            SELECT annotation.* INTO v_current_anno FROM public.annotations AS annotation
            WHERE annotation.id=(p_request ->> 'source_annotation_id')::uuid
            FOR UPDATE;
            IF NOT FOUND OR v_current_anno.task_id IS DISTINCT FROM v_task_id
               OR v_current_anno.is_active IS NOT TRUE OR v_current_anno.was_cancelled IS TRUE
               OR v_current_anno.geometry ->> 'type' IS DISTINCT FROM 'box_3d'
               OR v_current_anno.scene_track_id IS NULL OR v_current_anno.track_id IS NULL THEN
                RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":422,"reason":"source_box_invalid"}';
            END IF;
            SELECT track.* INTO v_current_track FROM public.scene_tracks AS track
            WHERE track.id=v_current_anno.scene_track_id AND track.retired_at IS NULL
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":404,"reason":"scene_track_missing"}';
            END IF;
            IF v_current_track.revision IS DISTINCT FROM (p_request ->> 'expected_track_revision')::integer THEN
                RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":409,"reason":"track_revision_conflict"}';
            END IF;
            v_data := jsonb_build_object('camera_lock_scope',jsonb_build_object(
                'source',to_jsonb(v_current_anno),'member',NULL,
                'track',jsonb_build_object('id',v_current_track.id,'project_id',v_current_track.project_id,
                    'scene_id',v_current_track.scene_id,'track_id',v_current_track.track_id,
                    'class_name',v_current_track.class_name,'revision',v_current_track.revision,
                    'retired_at',v_current_track.retired_at)));
        ELSE
            SELECT annotation.* INTO v_current_anno FROM public.annotations AS annotation
            WHERE annotation.id=(p_request ->> 'member_id')::uuid
            FOR UPDATE;
            IF NOT FOUND OR v_current_anno.task_id IS DISTINCT FROM v_task_id
               OR v_current_anno.sensor_role IS NULL OR v_current_anno.scene_track_id IS NULL
               OR v_current_anno.was_cancelled IS TRUE
               OR (v_camera_action='update' AND v_current_anno.is_active IS NOT TRUE)
               OR (v_camera_action='restore' AND v_current_anno.is_active IS NOT FALSE) THEN
                RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":404,"reason":"camera_member_missing"}';
            END IF;
            IF v_current_anno.version IS DISTINCT FROM (p_request ->> 'expected_version')::integer THEN
                RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":409,"reason":"annotation_version_conflict"}';
            END IF;
            SELECT track.* INTO v_current_track FROM public.scene_tracks AS track
            WHERE track.id=v_current_anno.scene_track_id AND track.retired_at IS NULL
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":404,"reason":"scene_track_missing"}';
            END IF;
            IF v_current_track.revision IS DISTINCT FROM (p_request ->> 'expected_track_revision')::integer THEN
                RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":409,"reason":"track_revision_conflict"}';
            END IF;
            v_data := jsonb_build_object('camera_lock_scope',jsonb_build_object(
                'source',NULL,'member',to_jsonb(v_current_anno),
                'track',jsonb_build_object('id',v_current_track.id,'project_id',v_current_track.project_id,
                    'scene_id',v_current_track.scene_id,'track_id',v_current_track.track_id,
                    'class_name',v_current_track.class_name,'revision',v_current_track.revision,
                    'retired_at',v_current_track.retired_at)));
        END IF;
    ELSIF v_operation = 'mutate_camera_member' THEN
        v_camera_action := p_request ->> 'action';
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        v_annotation_id := (p_request ->> 'member_id')::uuid;
        IF v_camera_action NOT IN ('create','update','delete','restore')
           OR (p_request ->> 'expected_track_revision') !~ '^[1-9][0-9]*$' THEN
            RAISE EXCEPTION 'Invalid camera member mutation fence' USING ERRCODE='22023';
        END IF;
        PERFORM task.id FROM public.tasks AS task
        WHERE task.id=v_task_id AND task.project_id=v_project_id FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                DETAIL='{"status_code":409,"reason":"task_target_changed"}';
        END IF;
        IF v_camera_action = 'create' THEN
            IF p_request ->> 'source_annotation_id' IS NULL
               OR p_request ->> 'camera_role' IS NULL
               OR left(p_request ->> 'camera_role',7) IS DISTINCT FROM 'camera_'
               OR p_request ->> 'camera_item_id' IS NULL
               OR NOT aap_labeling_v1._annotation_transfer_camera_bbox_valid(p_request -> 'bbox')
               OR p_request ->> 'visibility' NOT IN ('visible','occluded','truncated','unknown')
               OR p_request ->> 'expected_calibration_revision' IS NULL
               OR (p_request ->> 'expected_calibration_revision') !~ '^[1-9][0-9]*$'
               OR p_request ->> 'expected_calibration_digest' IS NULL
               OR length(p_request ->> 'expected_calibration_digest') <> 64 THEN
                RAISE EXCEPTION 'Invalid camera member create payload' USING ERRCODE='22023';
            END IF;
            SELECT annotation.* INTO v_current_anno FROM public.annotations AS annotation
            WHERE annotation.id=(p_request ->> 'source_annotation_id')::uuid
            FOR UPDATE;
            IF NOT FOUND OR v_current_anno.task_id IS DISTINCT FROM v_task_id
               OR v_current_anno.is_active IS NOT TRUE OR v_current_anno.was_cancelled IS TRUE
               OR v_current_anno.geometry ->> 'type' IS DISTINCT FROM 'box_3d'
               OR v_current_anno.scene_track_id IS NULL OR v_current_anno.track_id IS NULL THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":422,"reason":"source_box_invalid"}';
            END IF;
            SELECT track.* INTO v_current_track FROM public.scene_tracks AS track
            WHERE track.id=v_current_anno.scene_track_id AND track.retired_at IS NULL
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":404,"reason":"scene_track_missing"}';
            END IF;
            IF v_current_track.revision IS DISTINCT FROM (p_request ->> 'expected_track_revision')::integer THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":409,"reason":"track_revision_conflict"}';
            END IF;
            IF NOT EXISTS (SELECT FROM public.task_dataset_item_links AS link
                WHERE link.task_id=v_task_id AND link.role=p_request ->> 'camera_role'
                  AND link.dataset_item_id=(p_request ->> 'camera_item_id')::uuid) THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":422,"reason":"camera_role_not_linked"}';
            END IF;
            IF EXISTS (SELECT FROM public.annotations AS member
                WHERE member.task_id=v_task_id AND member.scene_track_id=v_current_track.id
                  AND member.sensor_role=p_request ->> 'camera_role'
                  AND member.is_active IS TRUE AND member.was_cancelled IS FALSE) THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":409,"reason":"camera_member_exists"}';
            END IF;
            INSERT INTO public.annotations(
                id,task_id,project_id,user_id,source,annotation_type,tool_unit_id,class_name,
                geometry,track_id,scene_track_id,temporal_role,sensor_dataset_item_id,sensor_role,
                sensor_visibility,calibration_revision,calibration_digest
            ) VALUES (
                v_annotation_id,v_task_id,v_project_id,v_actor_id,'manual','bbox',
                v_current_anno.tool_unit_id,v_current_anno.class_name,
                jsonb_build_object('type','bbox') || (p_request -> 'bbox'),
                v_current_anno.track_id,v_current_track.id,'sample',(p_request ->> 'camera_item_id')::uuid,
                p_request ->> 'camera_role',p_request ->> 'visibility',
                (p_request ->> 'expected_calibration_revision')::integer,
                p_request ->> 'expected_calibration_digest'
            ) RETURNING * INTO v_current_anno;
            UPDATE public.scene_tracks AS track
            SET revision=track.revision+1,updated_at=transaction_timestamp()
            WHERE track.id=v_current_track.id RETURNING revision INTO v_track_revision;
        ELSE
            SELECT annotation.* INTO v_current_anno FROM public.annotations AS annotation
            WHERE annotation.id=v_annotation_id
            FOR UPDATE;
            IF NOT FOUND OR v_current_anno.task_id IS DISTINCT FROM v_task_id
               OR v_current_anno.sensor_role IS NULL OR v_current_anno.scene_track_id IS NULL
               OR (v_camera_action IN ('update','restore') AND v_current_anno.was_cancelled IS TRUE)
               OR (v_camera_action IN ('update','delete') AND v_current_anno.is_active IS NOT TRUE)
               OR (v_camera_action='restore' AND v_current_anno.is_active IS NOT FALSE) THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":404,"reason":"camera_member_missing"}';
            END IF;
            IF v_current_anno.version IS DISTINCT FROM (p_request ->> 'expected_version')::integer THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":409,"reason":"annotation_version_conflict"}';
            END IF;
            SELECT track.* INTO v_current_track FROM public.scene_tracks AS track
            WHERE track.id=v_current_anno.scene_track_id AND track.retired_at IS NULL
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":404,"reason":"scene_track_missing"}';
            END IF;
            IF v_current_track.revision IS DISTINCT FROM (p_request ->> 'expected_track_revision')::integer THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                    DETAIL='{"status_code":409,"reason":"track_revision_conflict"}';
            END IF;
            IF v_camera_action IN ('update','restore') THEN
                IF NOT EXISTS (SELECT FROM public.task_dataset_item_links AS link
                    WHERE link.task_id=v_task_id AND link.role=v_current_anno.sensor_role
                      AND link.dataset_item_id=v_current_anno.sensor_dataset_item_id)
                   OR p_request ->> 'camera_item_id' IS DISTINCT FROM v_current_anno.sensor_dataset_item_id::text
                   OR p_request ->> 'camera_role' IS DISTINCT FROM v_current_anno.sensor_role
                   OR p_request ->> 'expected_calibration_revision' IS NULL
                   OR (p_request ->> 'expected_calibration_revision') !~ '^[1-9][0-9]*$'
                   OR p_request ->> 'expected_calibration_digest' IS NULL
                   OR length(p_request ->> 'expected_calibration_digest') <> 64 THEN
                    RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                        DETAIL='{"status_code":409,"reason":"calibration_revision_conflict"}';
                END IF;
                IF v_camera_action='update'
                   AND jsonb_typeof(p_request -> 'bbox') NOT IN ('object','null') THEN
                    RAISE EXCEPTION 'Invalid camera bbox update' USING ERRCODE='22023';
                END IF;
                IF v_camera_action='update'
                   AND jsonb_typeof(p_request -> 'bbox')='object'
                   AND NOT aap_labeling_v1._annotation_transfer_camera_bbox_valid(p_request -> 'bbox') THEN
                    RAISE EXCEPTION 'Invalid camera bbox update' USING ERRCODE='22023';
                END IF;
                IF p_request ->> 'visibility' IS NOT NULL
                   AND p_request ->> 'visibility' NOT IN ('visible','occluded','truncated','unknown') THEN
                    RAISE EXCEPTION 'Invalid camera visibility update' USING ERRCODE='22023';
                END IF;
                IF v_camera_action='restore' AND EXISTS (
                    SELECT FROM public.annotations AS member
                    WHERE member.task_id=v_task_id AND member.scene_track_id=v_current_anno.scene_track_id
                      AND member.sensor_role=v_current_anno.sensor_role
                      AND member.is_active IS TRUE AND member.was_cancelled IS FALSE
                ) THEN
                    RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='aap_annotation_transfer_error',
                        DETAIL='{"status_code":409,"reason":"camera_member_exists"}';
                END IF;
            END IF;
            IF v_camera_action='update'
               AND jsonb_typeof(p_request -> 'bbox') IS NULL
               AND p_request ->> 'visibility' IS NULL THEN
                RAISE EXCEPTION 'Camera member update has no changes' USING ERRCODE='22023';
            END IF;
            UPDATE public.annotations AS annotation
            SET geometry=CASE WHEN v_camera_action='update'
                                   AND jsonb_typeof(p_request -> 'bbox')='object'
                              THEN jsonb_build_object('type','bbox') || (p_request -> 'bbox')
                              ELSE annotation.geometry END,
                sensor_visibility=CASE WHEN v_camera_action='update' AND p_request ->> 'visibility' IS NOT NULL
                                       THEN p_request ->> 'visibility'
                                       ELSE annotation.sensor_visibility END,
                calibration_revision=CASE WHEN v_camera_action IN ('update','restore')
                                          THEN (p_request ->> 'expected_calibration_revision')::integer
                                          ELSE annotation.calibration_revision END,
                calibration_digest=CASE WHEN v_camera_action IN ('update','restore')
                                        THEN p_request ->> 'expected_calibration_digest'
                                        ELSE annotation.calibration_digest END,
                is_active=CASE WHEN v_camera_action='delete' THEN false
                               WHEN v_camera_action='restore' THEN true ELSE annotation.is_active END,
                version=annotation.version+1,updated_at=transaction_timestamp()
            WHERE annotation.id=v_annotation_id
            RETURNING * INTO v_current_anno;
            UPDATE public.scene_tracks AS track
            SET revision=track.revision+1,updated_at=transaction_timestamp()
            WHERE track.id=v_current_track.id RETURNING revision INTO v_track_revision;
        END IF;
        v_data := jsonb_build_object('camera_mutation',jsonb_build_object(
            'member',to_jsonb(v_current_anno),'track_revision',v_track_revision));
    ELSIF v_operation = 'create_mask_format_import' THEN
        IF jsonb_typeof(p_request -> 'mapping_json') IS DISTINCT FROM 'object'
           OR jsonb_typeof(p_request -> 'options_json') IS DISTINCT FROM 'object'
           OR jsonb_typeof(p_request -> 'plan_json') IS DISTINCT FROM 'object'
           OR jsonb_typeof(p_request -> 'result_json') IS DISTINCT FROM 'object'
           OR jsonb_typeof(p_request -> 'format_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'adapter_version') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'manifest_version') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'staged_object_key') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid MaskFormatImport plan' USING ERRCODE='22023';
        END IF;
        INSERT INTO public.mask_format_imports(
            id,project_id,requested_by_id,format_id,adapter_version,manifest_version,
            staged_object_key,staged_sha256,mapping_json,options_json,mapping_digest,
            options_digest,plan_json,plan_digest,token_hash,receipt_expires_at,status,result_json
        ) VALUES (
            (p_request ->> 'id')::uuid,(p_request ->> 'project_id')::uuid,
            (p_request ->> 'requested_by_id')::uuid,p_request ->> 'format_id',
            p_request ->> 'adapter_version',p_request ->> 'manifest_version',
            p_request ->> 'staged_object_key',p_request ->> 'staged_sha256',
            p_request -> 'mapping_json',p_request -> 'options_json',
            p_request ->> 'mapping_digest',p_request ->> 'options_digest',
            p_request -> 'plan_json',p_request ->> 'plan_digest',p_request ->> 'token_hash',
            (p_request ->> 'receipt_expires_at')::timestamptz,'staged',p_request -> 'result_json'
        ) RETURNING * INTO v_mask_import;
        v_data := jsonb_build_object('mask_import',to_jsonb(v_mask_import));
    ELSIF v_operation = 'read_mask_format_import' THEN
        SELECT batch.* INTO v_mask_import FROM public.mask_format_imports AS batch
        WHERE batch.id=(p_request ->> 'id')::uuid
          AND ((p_request ->> 'project_id') IS NULL
               OR batch.project_id=(p_request ->> 'project_id')::uuid);
        v_data := jsonb_build_object('mask_import',CASE WHEN FOUND THEN to_jsonb(v_mask_import) ELSE NULL END);
    ELSIF v_operation = 'lock_mask_format_import_receipt' THEN
        SELECT batch.* INTO v_mask_import FROM public.mask_format_imports AS batch
        WHERE batch.token_hash=p_request ->> 'token_hash'
          AND batch.project_id=(p_request ->> 'project_id')::uuid
          AND batch.requested_by_id=(p_request ->> 'requested_by_id')::uuid
        FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_transfer_error',
                DETAIL='{"status_code":404,"reason":"format_receipt_not_found"}';
        END IF;
        v_data := jsonb_build_object('mask_import',to_jsonb(v_mask_import));
    ELSIF v_operation = 'lock_mask_format_import' THEN
        IF (p_request ->> 'id') IS NULL AND (p_request ->> 'async_job_id') IS NULL THEN
            RAISE EXCEPTION 'Mask format import lock requires an identity' USING ERRCODE='22023';
        END IF;
        SELECT batch.* INTO v_mask_import FROM public.mask_format_imports AS batch
        WHERE ((p_request ->> 'id') IS NULL OR batch.id=(p_request ->> 'id')::uuid)
          AND ((p_request ->> 'project_id') IS NULL OR batch.project_id=(p_request ->> 'project_id')::uuid)
          AND ((p_request ->> 'async_job_id') IS NULL OR batch.async_job_id=(p_request ->> 'async_job_id')::uuid)
        FOR UPDATE;
        v_data := jsonb_build_object('mask_import',CASE WHEN FOUND THEN to_jsonb(v_mask_import) ELSE NULL END);
    ELSIF v_operation = 'link_mask_format_import_job' THEN
        UPDATE public.mask_format_imports AS batch
        SET async_job_id=(p_request ->> 'async_job_id')::uuid,
            status='pending',completed_at=CASE WHEN (p_request ->> 'resume')::boolean THEN NULL ELSE batch.completed_at END,
            updated_at=transaction_timestamp()
        WHERE batch.id=(p_request ->> 'id')::uuid
          AND batch.project_id=(p_request ->> 'project_id')::uuid
          AND batch.requested_by_id=(p_request ->> 'requested_by_id')::uuid
          AND CASE WHEN (p_request ->> 'resume')::boolean
                   THEN batch.status IN ('failed','partial')
                   ELSE batch.async_job_id IS NULL AND batch.status='staged' END
        RETURNING * INTO v_mask_import;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_transfer_error',
                DETAIL='{"status_code":409,"reason":"format_import_target_changed"}';
        END IF;
        v_data := jsonb_build_object('mask_import',to_jsonb(v_mask_import));
    ELSIF v_operation = 'mark_mask_format_import_running' THEN
        UPDATE public.mask_format_imports AS batch
        SET status='running',updated_at=transaction_timestamp()
        WHERE batch.id=(p_request ->> 'id')::uuid
          AND batch.async_job_id=(p_request ->> 'async_job_id')::uuid
          AND batch.status IN ('pending','running')
        RETURNING * INTO v_mask_import;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_transfer_error',
                DETAIL='{"status_code":409,"reason":"format_import_target_changed"}';
        END IF;
        v_data := jsonb_build_object('mask_import',to_jsonb(v_mask_import));
    ELSIF v_operation = 'record_mask_format_import_item' THEN
        IF (p_request ->> 'item_key') !~ '^[0-9]+$'
           OR jsonb_typeof(p_request -> 'item_result') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'Invalid mask format import item result' USING ERRCODE='22023';
        END IF;
        IF p_request #>> '{item_result,status}'='committed' THEN
            IF NOT aap_labeling_v1._annotation_transfer_exact_fields(
                p_request -> 'item_result',ARRAY['status','task_id','imported','skipped']::text[]
            ) OR jsonb_typeof(p_request #> '{item_result,task_id}') IS DISTINCT FROM 'string'
              OR jsonb_typeof(p_request #> '{item_result,imported}') IS DISTINCT FROM 'number'
              OR jsonb_typeof(p_request #> '{item_result,skipped}') IS DISTINCT FROM 'number'
              OR (p_request #>> '{item_result,imported}') !~ '^[0-9]+$'
              OR (p_request #>> '{item_result,skipped}') !~ '^[0-9]+$' THEN
                RAISE EXCEPTION 'Invalid committed mask import result' USING ERRCODE='22023';
            END IF;
        ELSIF p_request #>> '{item_result,status}'='skipped' THEN
            IF NOT aap_labeling_v1._annotation_transfer_exact_fields(
                p_request -> 'item_result',ARRAY['skip_code','status']::text[]
            ) OR jsonb_typeof(p_request #> '{item_result,skip_code}') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION 'Invalid skipped mask import result' USING ERRCODE='22023';
            END IF;
        ELSIF p_request #>> '{item_result,status}'='failed' THEN
            IF NOT aap_labeling_v1._annotation_transfer_exact_fields(
                p_request -> 'item_result',ARRAY['reason','status']::text[]
            ) OR jsonb_typeof(p_request #> '{item_result,reason}') IS DISTINCT FROM 'string'
              OR length(p_request #>> '{item_result,reason}') > 500 THEN
                RAISE EXCEPTION 'Invalid failed mask import result' USING ERRCODE='22023';
            END IF;
        ELSE
            RAISE EXCEPTION 'Invalid mask format import item status' USING ERRCODE='22023';
        END IF;
        UPDATE public.mask_format_imports AS batch
        SET result_json=jsonb_set(
                COALESCE(batch.result_json,'{}'::jsonb),
                '{items}',
                COALESCE(batch.result_json -> 'items','{}'::jsonb)
                    || jsonb_build_object(p_request ->> 'item_key',p_request -> 'item_result'),
                true
            ),
            updated_at=transaction_timestamp()
        WHERE batch.id=(p_request ->> 'id')::uuid
          AND batch.async_job_id=(p_request ->> 'async_job_id')::uuid
        RETURNING * INTO v_mask_import;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='aap_annotation_transfer_error',
                DETAIL='{"status_code":409,"reason":"format_import_target_changed"}';
        END IF;
        v_data := jsonb_build_object('mask_import',to_jsonb(v_mask_import));
    ELSIF v_operation = 'finish_mask_format_import' THEN
        v_import_status := NULL;
        v_committed_count := 0;
        IF p_request ->> 'action' IN ('cancel','complete') THEN
            SELECT batch.* INTO v_mask_import FROM public.mask_format_imports AS batch
            WHERE batch.id=(p_request ->> 'id')::uuid
              AND batch.async_job_id=(p_request ->> 'async_job_id')::uuid
            FOR UPDATE;
        ELSIF p_request ->> 'action'='fail' AND p_request ->> 'id' IS NOT NULL THEN
            SELECT batch.* INTO v_mask_import FROM public.mask_format_imports AS batch
            WHERE batch.id=(p_request ->> 'id')::uuid
              AND ((p_request ->> 'async_job_id') IS NULL
                   OR batch.async_job_id=(p_request ->> 'async_job_id')::uuid)
            FOR UPDATE;
        ELSE
            SELECT batch.* INTO v_mask_import FROM public.mask_format_imports AS batch
            WHERE batch.async_job_id=(p_request ->> 'async_job_id')::uuid
            FOR UPDATE;
        END IF;
        IF NOT FOUND THEN
            v_data := jsonb_build_object('mask_import',NULL,'committed_count',0);
        ELSE
            SELECT count(*) INTO v_committed_count
            FROM jsonb_each(COALESCE(v_mask_import.result_json -> 'items','{}'::jsonb)) AS items(key,value)
            WHERE value ->> 'status'='committed';
            IF p_request ->> 'action'='cancel' THEN
                v_import_status := CASE WHEN v_committed_count > 0 THEN 'partial' ELSE 'cancelled' END;
            ELSIF p_request ->> 'action'='complete' THEN
                v_import_status := CASE
                    WHEN (p_request ->> 'failed')::integer > 0
                     AND ((p_request ->> 'completed')::integer > 0 OR (p_request ->> 'skipped')::integer > 0) THEN 'partial'
                    WHEN (p_request ->> 'failed')::integer > 0 THEN 'failed'
                    ELSE 'completed' END;
            ELSIF p_request ->> 'action'='fail' THEN
                v_import_status := 'failed';
            ELSIF p_request ->> 'action'='reconcile_cancel' THEN
                IF v_mask_import.status IN ('pending','running') THEN
                    v_import_status := CASE WHEN v_committed_count > 0 THEN 'partial' ELSE 'cancelled' END;
                END IF;
            ELSE
                v_import_status := 'failed';
            END IF;
            IF v_import_status IS NOT NULL THEN
                UPDATE public.mask_format_imports AS batch
                SET status=v_import_status,completed_at=clock_timestamp(),updated_at=transaction_timestamp()
                WHERE batch.id=v_mask_import.id RETURNING * INTO v_mask_import;
            END IF;
            v_data := jsonb_build_object(
                'mask_import',to_jsonb(v_mask_import),'committed_count',v_committed_count);
        END IF;
    ELSIF v_operation = 'cancel_pending_mask_format_import' THEN
        UPDATE public.mask_format_imports AS batch
        SET status='cancelled',completed_at=clock_timestamp(),updated_at=transaction_timestamp()
        WHERE batch.async_job_id=(p_request ->> 'async_job_id')::uuid
          AND batch.status='pending'
        RETURNING * INTO v_mask_import;
        v_data := jsonb_build_object('mask_import',CASE WHEN FOUND THEN to_jsonb(v_mask_import) ELSE NULL END);
    END IF;

    RETURN jsonb_build_object('schema','aap-labeling-annotation-transfer/v1',
        'ok',true,'request',p_request,
        'data',jsonb_build_object('operation',v_operation) || v_data);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.annotation_transfer_command(jsonb) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._annotation_transfer_without_suffix(text) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._annotation_transfer_camera_bbox_valid(jsonb) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._annotation_transfer_without_track_ids(jsonb) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._annotation_transfer_exact_fields(jsonb,text[]) FROM PUBLIC;
