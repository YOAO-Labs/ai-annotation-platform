-- aap-statement-break
-- VideoSegment lifecycle writes remain in the Labeling transaction owner.
CREATE FUNCTION aap_labeling_v1.video_segment_lifecycle_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
AS $function$
DECLARE
    v_operation text;
    v_actor_id uuid;
    v_task_id uuid;
    v_project_id uuid;
    v_dataset_item_id uuid;
    v_segment_id uuid;
    v_frame_count integer;
    v_segment_size integer;
    v_segment_count integer;
    v_rows jsonb;
    v_task public.tasks%ROWTYPE;
    v_segment public.video_segments%ROWTYPE;
    v_access jsonb;
    v_access_data jsonb;
    v_asset_access jsonb;
    v_asset_data jsonb;
    v_workspace jsonb;
    v_workspace_data jsonb;
    v_now timestamptz;
    v_assignee_id uuid;
    v_locked_by uuid;
    v_locked_at timestamptz;
    v_lock_expires_at timestamptz;
    v_status text;
    v_is_manager boolean;
    v_is_reviewer boolean;
    v_is_privileged boolean;
    v_taskless_source boolean := false;
    v_changed boolean := false;
    v_data jsonb;
BEGIN
    IF p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR pg_catalog.octet_length(p_request::text) > 135266304
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-control/v1'
       OR pg_catalog.jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid video segment lifecycle request' USING ERRCODE = '22023';
    END IF;
    v_operation := p_request ->> 'operation';
    IF v_operation NOT IN (
        'prepare_segments', 'claim_segment', 'heartbeat_segment', 'release_segment',
        'submit_segment', 'reopen_segment', 'unassign_segment'
    ) THEN
        RAISE EXCEPTION 'Unsupported video segment lifecycle operation' USING ERRCODE = '22023';
    END IF;

    IF v_operation = 'prepare_segments' THEN
        IF (p_request - ARRAY[
                'schema','operation','task_id','project_id','actor_id',
                'dataset_item_id','frame_count','segment_size_frames','now'
            ]) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY[
                'schema','operation','task_id','project_id','actor_id',
                'dataset_item_id','frame_count','segment_size_frames','now'
            ])
           OR pg_catalog.jsonb_typeof(p_request -> 'frame_count') IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(p_request -> 'segment_size_frames') IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(p_request -> 'now') IS DISTINCT FROM 'string'
           OR (p_request ->> 'frame_count') !~ '^[1-9][0-9]*$'
           OR (p_request ->> 'segment_size_frames') !~ '^[1-9][0-9]*$' THEN
            RAISE EXCEPTION 'Invalid video segment preparation request' USING ERRCODE = '22023';
        END IF;
    ELSIF v_operation IN ('claim_segment', 'heartbeat_segment') THEN
        IF (p_request - ARRAY[
                'schema','operation','task_id','project_id','actor_id',
                'expected_batch_id','expected_task_status','expected_review_round_id',
                'dataset_item_id','segment_id','now','lock_ttl_seconds','privileged'
            ]) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY[
                'schema','operation','task_id','project_id','actor_id',
                'expected_batch_id','expected_task_status','expected_review_round_id',
                'dataset_item_id','segment_id','now','lock_ttl_seconds','privileged'
            ])
           OR pg_catalog.jsonb_typeof(p_request -> 'lock_ttl_seconds') IS DISTINCT FROM 'number'
           OR (p_request ->> 'lock_ttl_seconds') !~ '^-?(0|[1-9][0-9]*)$' THEN
            RAISE EXCEPTION 'Invalid video segment lease request' USING ERRCODE = '22023';
        END IF;
    ELSIF v_operation IN ('release_segment', 'submit_segment') THEN
        IF (p_request - ARRAY[
                'schema','operation','task_id','project_id','actor_id',
                'expected_batch_id','expected_task_status','expected_review_round_id',
                'dataset_item_id','segment_id','now','privileged'
            ]) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY[
                'schema','operation','task_id','project_id','actor_id',
                'expected_batch_id','expected_task_status','expected_review_round_id',
                'dataset_item_id','segment_id','now','privileged'
            ]) THEN
            RAISE EXCEPTION 'Invalid video segment transition request' USING ERRCODE = '22023';
        END IF;
    ELSE
        IF (p_request - ARRAY[
                'schema','operation','task_id','project_id','actor_id',
                'expected_batch_id','expected_task_status','expected_review_round_id',
                'dataset_item_id','segment_id'
            ]) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY[
                'schema','operation','task_id','project_id','actor_id',
                'expected_batch_id','expected_task_status','expected_review_round_id',
                'dataset_item_id','segment_id'
            ]) THEN
            RAISE EXCEPTION 'Invalid video segment review request' USING ERRCODE = '22023';
        END IF;
    END IF;

    IF pg_catalog.jsonb_typeof(p_request -> 'actor_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_request -> 'dataset_item_id') IS DISTINCT FROM 'string'
       OR ((p_request -> 'task_id') IS DISTINCT FROM 'null'::jsonb
           AND pg_catalog.jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string')
       OR ((p_request -> 'project_id') IS DISTINCT FROM 'null'::jsonb
           AND pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string')
       OR (v_operation <> 'prepare_segments' AND (
            ((p_request -> 'expected_task_status') IS DISTINCT FROM 'null'::jsonb
                AND pg_catalog.jsonb_typeof(p_request -> 'expected_task_status') IS DISTINCT FROM 'string')
            OR (p_request -> 'expected_batch_id') IS DISTINCT FROM 'null'::jsonb
                AND pg_catalog.jsonb_typeof(p_request -> 'expected_batch_id') IS DISTINCT FROM 'string'
            OR (p_request -> 'expected_review_round_id') IS DISTINCT FROM 'null'::jsonb
                AND pg_catalog.jsonb_typeof(p_request -> 'expected_review_round_id') IS DISTINCT FROM 'string'
       ))
       OR (v_operation <> 'prepare_segments'
           AND (p_request -> 'segment_id') IS DISTINCT FROM 'null'::jsonb
           AND pg_catalog.jsonb_typeof(p_request -> 'segment_id') IS DISTINCT FROM 'string')
       OR (v_operation IN (
                'claim_segment','heartbeat_segment','release_segment','submit_segment'
            ) AND pg_catalog.jsonb_typeof(p_request -> 'now') IS DISTINCT FROM 'string')
       OR (v_operation IN (
                'claim_segment','heartbeat_segment','release_segment','submit_segment'
            ) AND pg_catalog.jsonb_typeof(p_request -> 'privileged') IS DISTINCT FROM 'boolean') THEN
        RAISE EXCEPTION 'Invalid video segment lifecycle fields' USING ERRCODE = '22023';
    END IF;

    v_actor_id := (p_request ->> 'actor_id')::uuid;
    v_dataset_item_id := (p_request ->> 'dataset_item_id')::uuid;
    IF (p_request -> 'task_id') IS NOT DISTINCT FROM 'null'::jsonb THEN
        IF (p_request -> 'project_id') IS DISTINCT FROM 'null'::jsonb
           OR (v_operation <> 'prepare_segments' AND (
                (p_request -> 'expected_batch_id') IS DISTINCT FROM 'null'::jsonb
                OR (p_request -> 'expected_task_status') IS DISTINCT FROM 'null'::jsonb
                OR (p_request -> 'expected_review_round_id') IS DISTINCT FROM 'null'::jsonb
           )) THEN
            RAISE EXCEPTION 'Invalid taskless video segment scope' USING ERRCODE = '22023';
        END IF;
        v_asset_access := aap_assets_v1.read_media_source(
            v_actor_id,v_dataset_item_id,NULL
        );
        IF v_asset_access IS NULL
           OR v_asset_access ->> 'schema' IS DISTINCT FROM 'aap-assets-media/v1'
           OR v_asset_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail',pg_catalog.jsonb_build_object('reason','permission_changed'))::text;
        END IF;
        v_asset_data := v_asset_access -> 'data';
        IF pg_catalog.jsonb_typeof(v_asset_data) IS DISTINCT FROM 'object'
           OR v_asset_data ->> 'dataset_item_id' IS DISTINCT FROM v_dataset_item_id::text
           OR v_asset_data ->> 'file_type' IS DISTINCT FROM 'video'
           OR pg_catalog.jsonb_typeof(v_asset_data -> 'video_metadata') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'Invalid taskless video source authorization' USING ERRCODE = '22023';
        END IF;
        v_taskless_source := true;
        v_task_id := NULL;
        v_project_id := NULL;
    ELSE
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_project_id := (p_request ->> 'project_id')::uuid;
    END IF;
    IF v_operation <> 'prepare_segments' THEN
        v_segment_id := (p_request ->> 'segment_id')::uuid;
    END IF;
    IF v_operation IN (
        'claim_segment','heartbeat_segment','release_segment','submit_segment'
    ) THEN
        v_now := (p_request ->> 'now')::timestamptz;
    ELSIF v_operation = 'prepare_segments' THEN
        v_now := (p_request ->> 'now')::timestamptz;
        v_frame_count := (p_request ->> 'frame_count')::integer;
        v_segment_size := (p_request ->> 'segment_size_frames')::integer;
    END IF;

    IF NOT v_taskless_source THEN
        SELECT task.* INTO v_task
        FROM public.tasks AS task
        WHERE task.id = v_task_id
          AND task.dataset_item_id = v_dataset_item_id;
        IF NOT FOUND
           OR v_task.project_id IS DISTINCT FROM v_project_id
           OR (v_operation <> 'prepare_segments'
               AND (p_request -> 'task_id') IS DISTINCT FROM 'null'::jsonb AND (
                v_task.batch_id IS DISTINCT FROM (p_request ->> 'expected_batch_id')::uuid
                OR v_task.status IS DISTINCT FROM p_request ->> 'expected_task_status'
                OR v_task.review_round_id IS DISTINCT FROM
                    (p_request ->> 'expected_review_round_id')::uuid
           ))
           OR v_task.file_type IS DISTINCT FROM 'video' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','task_target_changed'))::text;
        END IF;

        IF v_operation = 'prepare_segments' THEN
            v_workspace := aap_workspace_v1.read_media_project_access(
                v_actor_id,v_project_id
            );
        ELSE
            v_workspace := aap_workspace_v1.lock_media_project_access(
                v_actor_id,v_project_id
            );
        END IF;
        IF v_workspace IS NULL
           OR v_workspace ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
           OR v_workspace -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail',pg_catalog.jsonb_build_object('reason','permission_changed'))::text;
        END IF;
        v_workspace_data := v_workspace -> 'data';
        IF pg_catalog.jsonb_typeof(v_workspace_data) IS DISTINCT FROM 'object'
           OR v_workspace_data ->> 'actor_id' IS DISTINCT FROM v_actor_id::text
           OR v_workspace_data ->> 'project_id' IS DISTINCT FROM v_project_id::text
           OR pg_catalog.jsonb_typeof(v_workspace_data -> 'is_manager') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION 'Invalid video segment workspace authorization' USING ERRCODE = '22023';
        END IF;
        v_is_manager := (v_workspace_data ->> 'is_manager')::boolean;

        v_access := aap_labeling_v1.read_media_task_access(
            v_actor_id,v_task_id,v_dataset_item_id
        );
        IF v_access IS NULL
           OR v_access ->> 'schema' IS DISTINCT FROM 'aap-labeling-media-task-access/v1'
           OR v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail',pg_catalog.jsonb_build_object('reason','permission_changed'))::text;
        END IF;
        v_access_data := v_access -> 'data';
        IF pg_catalog.jsonb_typeof(v_access_data) IS DISTINCT FROM 'object'
           OR v_access_data ->> 'actor_id' IS DISTINCT FROM v_actor_id::text
           OR v_access_data ->> 'task_id' IS DISTINCT FROM v_task_id::text
           OR v_access_data ->> 'dataset_item_id' IS DISTINCT FROM v_dataset_item_id::text
           OR v_access_data ->> 'project_id' IS DISTINCT FROM v_project_id::text
           OR pg_catalog.jsonb_typeof(v_access_data -> 'annotation_write') IS DISTINCT FROM 'boolean'
           OR pg_catalog.jsonb_typeof(v_access_data -> 'is_manager') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION 'Invalid video segment task authorization' USING ERRCODE = '22023';
        END IF;
    ELSE
        -- Taskless internal callers historically supplied this trusted fact for
        -- these four methods; Task-bound operations derive privilege above.
        v_is_manager := CASE
            WHEN v_operation IN (
                'claim_segment','heartbeat_segment','release_segment','submit_segment'
            ) THEN (p_request ->> 'privileged')::boolean
            ELSE true
        END;
        v_is_reviewer := false;
        v_is_privileged := CASE
            WHEN v_operation IN (
                'claim_segment','heartbeat_segment','release_segment','submit_segment'
            ) THEN (p_request ->> 'privileged')::boolean
            ELSE true
        END;
    END IF;
    IF v_operation = 'prepare_segments' THEN
        SELECT pg_catalog.count(*)::integer INTO v_segment_count
        FROM public.video_segments AS segment
        WHERE segment.dataset_item_id = v_dataset_item_id;
        IF v_segment_count = 0 THEN
            v_segment_count := GREATEST(
                1,(v_frame_count + v_segment_size - 1) / v_segment_size
            );
            BEGIN
                INSERT INTO public.video_segments(
                    id,dataset_item_id,segment_index,start_frame,end_frame,status
                )
                SELECT pg_catalog.gen_random_uuid(),v_dataset_item_id,series.value,
                       series.value*v_segment_size,
                       LEAST(v_frame_count-1,
                           series.value*v_segment_size+v_segment_size-1),
                       'open'
                FROM pg_catalog.generate_series(0,v_segment_count-1) AS series(value);
            EXCEPTION WHEN unique_violation THEN
                NULL;
            END;
        END IF;
        WITH normalized AS MATERIALIZED (
            SELECT segment.id,
                   CASE WHEN segment.locked_by IS NOT NULL
                          AND segment.lock_expires_at IS NOT NULL
                          AND segment.lock_expires_at <= v_now THEN NULL
                        ELSE segment.locked_by END AS locked_by,
                   CASE WHEN segment.locked_by IS NOT NULL
                          AND segment.lock_expires_at IS NOT NULL
                          AND segment.lock_expires_at <= v_now THEN NULL
                        ELSE segment.locked_at END AS locked_at,
                   CASE WHEN segment.locked_by IS NOT NULL
                          AND segment.lock_expires_at IS NOT NULL
                          AND segment.lock_expires_at <= v_now THEN NULL
                        ELSE segment.lock_expires_at END AS lock_expires_at,
                   CASE WHEN segment.locked_by IS NOT NULL
                          AND segment.lock_expires_at IS NOT NULL
                          AND segment.lock_expires_at > v_now THEN 'locked'
                        WHEN segment.assignee_id IS NOT NULL THEN 'assigned'
                        ELSE 'open' END AS status
            FROM public.video_segments AS segment
            WHERE segment.dataset_item_id = v_dataset_item_id
              AND segment.status <> 'completed'
        )
        UPDATE public.video_segments AS segment
        SET locked_by = normalized.locked_by,
            locked_at = normalized.locked_at,
            lock_expires_at = normalized.lock_expires_at,
            status = normalized.status,
            updated_at = pg_catalog.transaction_timestamp()
        FROM normalized
        WHERE segment.id = normalized.id
          AND (segment.locked_by IS DISTINCT FROM normalized.locked_by
               OR segment.locked_at IS DISTINCT FROM normalized.locked_at
               OR segment.lock_expires_at IS DISTINCT FROM normalized.lock_expires_at
               OR segment.status IS DISTINCT FROM normalized.status);
        SELECT COALESCE(
                   pg_catalog.jsonb_agg(pg_catalog.to_jsonb(selected)),
                   '[]'::jsonb
               )
        INTO v_rows
        FROM (
            SELECT segment.*
            FROM public.video_segments AS segment
            WHERE segment.dataset_item_id = v_dataset_item_id
            ORDER BY segment.segment_index
        ) AS selected;
        SELECT pg_catalog.count(*)::integer INTO v_segment_count
        FROM public.video_segments AS segment
        WHERE segment.dataset_item_id = v_dataset_item_id;
        v_data := pg_catalog.jsonb_build_object(
            'segments',v_rows,'segment_count',v_segment_count
        );
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-labeling-video-control/v1',
            'ok',true,'request',p_request,'data',v_data
        );
    END IF;
    IF NOT v_taskless_source
       AND v_operation IN ('claim_segment','heartbeat_segment','release_segment','submit_segment')
       AND v_access_data -> 'annotation_write' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
            DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                'detail',pg_catalog.jsonb_build_object('reason','permission_changed'))::text;
    END IF;
    IF NOT v_taskless_source THEN
        v_is_reviewer := COALESCE(v_task.reviewer_id = v_actor_id,false);
        v_is_privileged := (p_request ->> 'privileged')::boolean AND CASE
            WHEN v_operation IN (
                'claim_segment','heartbeat_segment','release_segment'
            ) THEN v_is_manager
            ELSE v_is_manager OR v_is_reviewer
        END;
    END IF;
    IF v_operation IN ('reopen_segment','unassign_segment')
       AND NOT (v_is_manager OR v_is_reviewer) THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
            DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                'detail','reviewer privilege required')::text;
    END IF;

    SELECT segment.* INTO v_segment
    FROM public.video_segments AS segment
    WHERE segment.id = v_segment_id
      AND segment.dataset_item_id = v_dataset_item_id
    FOR UPDATE;
    IF NOT FOUND THEN
        v_data := pg_catalog.jsonb_build_object('segment',NULL,'changed',false);
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-labeling-video-control/v1',
            'ok',true,'request',p_request,'data',v_data
        );
    END IF;

    v_assignee_id := v_segment.assignee_id;
    v_locked_by := v_segment.locked_by;
    v_locked_at := v_segment.locked_at;
    v_lock_expires_at := v_segment.lock_expires_at;
    v_status := v_segment.status;

    IF v_operation IN ('claim_segment','heartbeat_segment','release_segment','submit_segment')
       AND v_status <> 'completed' THEN
        IF v_locked_by IS NOT NULL
           AND v_lock_expires_at IS NOT NULL
           AND v_lock_expires_at <= v_now THEN
            v_locked_by := NULL;
            v_locked_at := NULL;
            v_lock_expires_at := NULL;
        END IF;
        IF v_locked_by IS NOT NULL
           AND v_lock_expires_at IS NOT NULL
           AND v_lock_expires_at > v_now THEN
            v_status := 'locked';
        ELSIF v_assignee_id IS NOT NULL THEN
            v_status := 'assigned';
        ELSE
            v_status := 'open';
        END IF;
    END IF;

    IF v_operation = 'claim_segment' THEN
        IF v_status = 'completed' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','video_segment_completed'))::text;
        END IF;
        IF v_assignee_id IS NOT NULL AND v_assignee_id <> v_actor_id AND NOT v_is_privileged THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail','Video segment is assigned to another user')::text;
        END IF;
        IF v_locked_by IS NOT NULL AND v_locked_by <> v_actor_id AND NOT v_is_privileged THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail','Video segment is locked by another user')::text;
        END IF;
        v_assignee_id := COALESCE(v_assignee_id,v_actor_id);
        v_locked_by := v_actor_id;
        v_locked_at := v_now;
        v_lock_expires_at := v_now +
            ((p_request ->> 'lock_ttl_seconds')::integer * pg_catalog.interval '1 second');
        v_status := 'locked';
    ELSIF v_operation = 'heartbeat_segment' THEN
        IF v_locked_by IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail','Video segment is not locked')::text;
        END IF;
        IF v_locked_by <> v_actor_id AND NOT v_is_privileged THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail','Video segment lock belongs to another user')::text;
        END IF;
        v_lock_expires_at := v_now +
            ((p_request ->> 'lock_ttl_seconds')::integer * pg_catalog.interval '1 second');
        v_status := 'locked';
    ELSIF v_operation = 'release_segment' THEN
        IF v_status = 'completed' THEN
            v_data := pg_catalog.jsonb_build_object(
                'segment',pg_catalog.to_jsonb(v_segment),'changed',false
            );
            RETURN pg_catalog.jsonb_build_object(
                'schema','aap-labeling-video-control/v1',
                'ok',true,'request',p_request,'data',v_data
            );
        END IF;
        IF v_locked_by IS NOT NULL AND v_locked_by <> v_actor_id AND NOT v_is_privileged THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail','Video segment lock belongs to another user')::text;
        END IF;
        v_locked_by := NULL;
        v_locked_at := NULL;
        v_lock_expires_at := NULL;
        v_status := CASE WHEN v_assignee_id IS NOT NULL THEN 'assigned' ELSE 'open' END;
    ELSIF v_operation = 'submit_segment' THEN
        IF v_status = 'completed' THEN
            v_data := pg_catalog.jsonb_build_object(
                'segment',pg_catalog.to_jsonb(v_segment),'changed',false
            );
            RETURN pg_catalog.jsonb_build_object(
                'schema','aap-labeling-video-control/v1',
                'ok',true,'request',p_request,'data',v_data
            );
        END IF;
        IF NOT v_is_privileged THEN
            IF v_assignee_id IS DISTINCT FROM v_actor_id THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                    DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                        'detail',pg_catalog.jsonb_build_object(
                            'reason','video_segment_not_assigned_to_user'
                        ))::text;
            END IF;
            IF v_locked_by IS DISTINCT FROM v_actor_id
               OR v_lock_expires_at IS NULL
               OR v_lock_expires_at <= v_now THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                    DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                        'detail',pg_catalog.jsonb_build_object(
                            'reason','video_segment_lease_required'
                        ))::text;
            END IF;
        END IF;
        v_status := 'completed';
        v_locked_by := NULL;
        v_locked_at := NULL;
        v_lock_expires_at := NULL;
    ELSIF v_operation = 'reopen_segment' THEN
        v_status := CASE WHEN v_assignee_id IS NOT NULL THEN 'assigned' ELSE 'open' END;
        v_locked_by := NULL;
        v_locked_at := NULL;
        v_lock_expires_at := NULL;
    ELSE
        IF v_status = 'completed' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object(
                        'reason','video_segment_reopen_required'
                    ))::text;
        END IF;
        v_assignee_id := NULL;
        v_status := 'open';
        v_locked_by := NULL;
        v_locked_at := NULL;
        v_lock_expires_at := NULL;
    END IF;

    v_changed := v_segment.assignee_id IS DISTINCT FROM v_assignee_id
        OR v_segment.status IS DISTINCT FROM v_status
        OR v_segment.locked_by IS DISTINCT FROM v_locked_by
        OR v_segment.locked_at IS DISTINCT FROM v_locked_at
        OR v_segment.lock_expires_at IS DISTINCT FROM v_lock_expires_at;
    IF v_changed THEN
        UPDATE public.video_segments AS segment
        SET assignee_id = v_assignee_id,
            status = v_status,
            locked_by = v_locked_by,
            locked_at = v_locked_at,
            lock_expires_at = v_lock_expires_at,
            updated_at = pg_catalog.transaction_timestamp()
        WHERE segment.id = v_segment_id
          AND segment.dataset_item_id = v_dataset_item_id
        RETURNING segment.* INTO v_segment;
    END IF;

    v_data := pg_catalog.jsonb_build_object(
        'segment',pg_catalog.to_jsonb(v_segment),'changed',v_changed
    );
    RETURN pg_catalog.jsonb_build_object(
        'schema','aap-labeling-video-control/v1',
        'ok',true,'request',p_request,'data',v_data
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.video_segment_lifecycle_command(jsonb)
    FROM PUBLIC;
