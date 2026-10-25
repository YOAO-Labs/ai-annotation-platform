-- Human video controls use the same Labeling owner as tracker publication.
CREATE FUNCTION aap_labeling_v1.video_control_command(p_request jsonb)
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
    v_access jsonb;
    v_access_data jsonb;
    v_workspace_access jsonb;
    v_task_lock jsonb;
    v_task public.tasks%ROWTYPE;
    v_job public.video_tracker_jobs%ROWTYPE;
    v_annotation public.annotations%ROWTYPE;
    v_segment public.video_segments%ROWTYPE;
    v_now timestamptz;
    v_data jsonb;
    v_rows jsonb;
    v_ids uuid[];
    v_id uuid;
    v_expected_version integer;
    v_updated integer;
    v_cutoff timestamptz;
    v_counts jsonb;
    v_has_more boolean;
    v_batch public.task_batches%ROWTYPE;
    v_manager_reply jsonb;
    v_manager_ids uuid[];
    v_frame_count integer;
    v_segment_size integer;
    v_segment_count integer;
    v_new_status text;
    v_new_cancel_at timestamptz;
    v_new_completed_at timestamptz;
    v_new_staged jsonb;
    v_source jsonb;
    v_new_track jsonb;
    v_review_scope jsonb;
    v_scope_source_version integer;
    v_source_id uuid;
    v_expected_revision integer;
    v_actor jsonb;
BEGIN
    IF p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR pg_catalog.octet_length(p_request::text) > 135266304
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-control/v1'
       OR pg_catalog.jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid video control request' USING ERRCODE = '22023';
    END IF;
    v_operation := p_request ->> 'operation';

    IF v_operation = 'read_job' OR v_operation = 'lock_review_job' THEN
        IF (p_request - ARRAY['schema','operation','job_id','actor_id']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','job_id','actor_id'])
           OR pg_catalog.jsonb_typeof(p_request -> 'job_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'actor_id') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid video tracker read request' USING ERRCODE = '22023';
        END IF;
        v_id := (p_request ->> 'job_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        SELECT job.* INTO v_job
        FROM public.video_tracker_jobs AS job
        WHERE job.id = v_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',404,
                    'detail','Video tracker job not found')::text;
        END IF;
        v_task_id := v_job.task_id;
        v_dataset_item_id := v_job.dataset_item_id;
        IF v_operation = 'lock_review_job' THEN
            v_workspace_access := aap_workspace_v1.lock_media_project_access(
                v_actor_id, (SELECT task.project_id FROM public.tasks AS task WHERE task.id=v_task_id)
            );
            IF v_workspace_access IS NULL
               OR v_workspace_access ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
               OR v_workspace_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                    DETAIL = pg_catalog.jsonb_build_object('status_code',404,
                        'detail',pg_catalog.jsonb_build_object('reason','task_not_visible'))::text;
            END IF;
        ELSE
            v_access := aap_labeling_v1.read_media_task_access(
                v_actor_id, v_task_id, v_dataset_item_id
            );
        END IF;
        IF v_operation='lock_review_job' THEN
            v_access := aap_labeling_v1.read_media_task_access(
                v_actor_id,v_task_id,v_dataset_item_id
            );
        END IF;
        IF v_access IS NULL
           OR v_access ->> 'schema' IS DISTINCT FROM 'aap-labeling-media-task-access/v1'
           OR v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',404,
                    'detail',pg_catalog.jsonb_build_object('reason','task_not_visible'))::text;
        END IF;
        IF v_operation = 'lock_review_job' THEN
            BEGIN
                SELECT job.* INTO v_job
                FROM public.video_tracker_jobs AS job
                WHERE job.id = v_id
                FOR UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                    DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                        'detail',pg_catalog.jsonb_build_object('reason','tracker_job_busy'))::text;
            END;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                    DETAIL = pg_catalog.jsonb_build_object('status_code',404,
                        'detail','Video tracker job not found')::text;
            END IF;
        END IF;
        v_data := pg_catalog.to_jsonb(v_job);

    ELSIF v_operation = 'read_task_jobs' THEN
        IF (p_request - ARRAY['schema','operation','task_id','actor_id','statuses']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','task_id','actor_id','statuses'])
           OR pg_catalog.jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'actor_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'statuses') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid task tracker-list request' USING ERRCODE = '22023';
        END IF;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        SELECT task.* INTO v_task FROM public.tasks AS task WHERE task.id=v_task_id;
        IF NOT FOUND OR v_task.dataset_item_id IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',404,
                    'detail',pg_catalog.jsonb_build_object('reason','task_not_visible'))::text;
        END IF;
        v_access := aap_labeling_v1.read_media_task_access(
            v_actor_id, v_task_id, v_task.dataset_item_id
        );
        IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',404,
                    'detail',pg_catalog.jsonb_build_object('reason','task_not_visible'))::text;
        END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(job)
                    ORDER BY job.created_at DESC, job.id DESC),'[]'::jsonb)
        INTO v_data
        FROM public.video_tracker_jobs AS job
        WHERE job.task_id=v_task_id
          AND job.status IN (
              SELECT entry.value #>> '{}' FROM pg_catalog.jsonb_array_elements(p_request -> 'statuses') AS entry(value)
          )
          AND (
              (v_access #>> '{data,is_manager}')='true'
              OR (
                  (p_request -> 'statuses') = '["queued","running"]'::jsonb
                  AND job.created_by=v_actor_id
              )
              OR (
                  (p_request -> 'statuses') <> '["queued","running"]'::jsonb
                  AND v_task.status='review'
                  AND v_task.reviewer_id=v_actor_id
                  AND v_task.reviewer_claimed_at IS NOT NULL
              )
              OR job.created_by=v_actor_id
          );

    ELSIF v_operation = 'read_annotations' THEN
        IF (p_request - ARRAY['schema','operation','task_id','actor_id','annotation_ids','lock_for_update']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','task_id','actor_id','annotation_ids','lock_for_update'])
           OR pg_catalog.jsonb_typeof(p_request -> 'annotation_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid video annotation-read request' USING ERRCODE = '22023';
        END IF;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        SELECT task.* INTO v_task FROM public.tasks AS task WHERE task.id=v_task_id;
        IF NOT FOUND OR v_task.dataset_item_id IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',404,
                    'detail',pg_catalog.jsonb_build_object('reason','task_not_visible'))::text;
        END IF;
        v_access := aap_labeling_v1.read_media_task_access(
            v_actor_id, v_task_id, v_task.dataset_item_id
        );
        IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',404,
                    'detail',pg_catalog.jsonb_build_object('reason','task_not_visible'))::text;
        END IF;
        IF (p_request ->> 'lock_for_update')::boolean THEN
            WITH locked AS MATERIALIZED (
                SELECT annotation.* FROM public.annotations AS annotation
                WHERE annotation.task_id=v_task_id
                  AND annotation.id IN (
                      SELECT entry.value::uuid
                      FROM pg_catalog.jsonb_array_elements_text(p_request -> 'annotation_ids') AS entry(value)
                  )
                ORDER BY annotation.id FOR UPDATE OF annotation
            )
            SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(locked)
                        ORDER BY locked.id),'[]'::jsonb)
            INTO v_data FROM locked;
        ELSE
            SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(annotation)
                        ORDER BY annotation.id),'[]'::jsonb)
            INTO v_data
            FROM public.annotations AS annotation
            WHERE annotation.task_id=v_task_id
              AND annotation.id IN (
                  SELECT entry.value::uuid
                  FROM pg_catalog.jsonb_array_elements_text(p_request -> 'annotation_ids') AS entry(value)
              );
        END IF;

    ELSIF v_operation = 'read_active_correction' THEN
        IF (p_request - ARRAY['schema','operation','task_id','actor_id','track_id']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','task_id','actor_id','track_id']) THEN
            RAISE EXCEPTION 'Invalid active correction lookup' USING ERRCODE = '22023';
        END IF;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        SELECT task.* INTO v_task FROM public.tasks AS task WHERE task.id=v_task_id;
        IF NOT FOUND OR v_task.dataset_item_id IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',404,
                    'detail',pg_catalog.jsonb_build_object('reason','task_not_visible'))::text;
        END IF;
        v_access := aap_labeling_v1.read_media_task_access(v_actor_id,v_task_id,v_task.dataset_item_id);
        IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR v_access #>> '{data,annotation_write}' IS DISTINCT FROM 'true' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail',pg_catalog.jsonb_build_object('reason','permission_changed'))::text;
        END IF;
        SELECT job.id INTO v_id
        FROM public.video_tracker_jobs AS job
        WHERE job.task_id=v_task_id
          AND job.job_kind='correction'
          AND job.track_id_snapshot=p_request ->> 'track_id'
          AND job.status IN ('queued','running','pending_review','partially_reviewed')
        ORDER BY job.created_at,job.id
        LIMIT 1;
        v_data := pg_catalog.to_jsonb(v_id);

    ELSIF v_operation = 'lock_review_annotations' THEN
        IF (p_request - ARRAY['schema','operation','job_id','task_id','actor_id','annotations']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','job_id','task_id','actor_id','annotations'])
           OR pg_catalog.jsonb_typeof(p_request -> 'annotations') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid video review-source lock request' USING ERRCODE = '22023';
        END IF;
        v_id := (p_request ->> 'job_id')::uuid;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        SELECT job.* INTO v_job FROM public.video_tracker_jobs AS job WHERE job.id=v_id;
        IF NOT FOUND OR v_job.task_id IS DISTINCT FROM v_task_id THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','task_changed'))::text;
        END IF;
        SELECT task.* INTO v_task FROM public.tasks AS task WHERE task.id=v_task_id;
        IF NOT FOUND OR v_task.dataset_item_id IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','task_changed'))::text;
        END IF;
        v_access := aap_labeling_v1.read_media_task_access(
            v_actor_id, v_task_id, v_task.dataset_item_id
        );
        IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR v_access #>> '{data,annotation_write}' IS DISTINCT FROM 'true' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail',pg_catalog.jsonb_build_object('reason','permission_changed'))::text;
        END IF;
        WITH requested AS MATERIALIZED (
            SELECT (entry.value ->> 'annotation_id')::uuid AS id,
                   (entry.value ->> 'expected_version')::integer AS expected_version
            FROM pg_catalog.jsonb_array_elements(p_request -> 'annotations') AS entry(value)
        ), locked AS MATERIALIZED (
            SELECT annotation.*
            FROM public.annotations AS annotation
            JOIN requested ON requested.id=annotation.id
            WHERE annotation.task_id=v_task_id
            ORDER BY annotation.id
            FOR UPDATE OF annotation
        )
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(locked)
                    ORDER BY locked.id),'[]'::jsonb)
        INTO v_data FROM locked;
        IF pg_catalog.jsonb_array_length(v_data) < pg_catalog.jsonb_array_length(p_request -> 'annotations') THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','source_version_conflict'))::text;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(v_data) AS row(value)
            JOIN pg_catalog.jsonb_array_elements(p_request -> 'annotations') AS requested(value)
              ON requested.value ->> 'annotation_id'=row.value ->> 'id'
            WHERE (row.value ->> 'version')::integer
                    IS DISTINCT FROM (requested.value ->> 'expected_version')::integer
               OR row.value ->> 'is_active' IS DISTINCT FROM 'true'
               OR row.value ->> 'was_cancelled' IS DISTINCT FROM 'false'
               OR row.value ->> 'is_locked' IS DISTINCT FROM 'false'
        ) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','source_version_conflict'))::text;
        END IF;

    ELSIF v_operation = 'ensure_segments' THEN
        IF (p_request - ARRAY['schema','operation','task_id','project_id','actor_id','expected_batch_id','expected_task_status','expected_review_round_id','dataset_item_id','segment_id','from_frame','to_frame','frame_count','segment_size_frames','now','count_segments']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','task_id','project_id','actor_id','expected_batch_id','expected_task_status','expected_review_round_id','dataset_item_id','segment_id','from_frame','to_frame','frame_count','segment_size_frames','now','count_segments'])
           OR pg_catalog.jsonb_typeof(p_request -> 'frame_count') IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(p_request -> 'segment_size_frames') IS DISTINCT FROM 'number'
           OR (p_request ->> 'frame_count') !~ '^[1-9][0-9]*$'
           OR (p_request ->> 'segment_size_frames') !~ '^[1-9][0-9]*$' THEN
            RAISE EXCEPTION 'Invalid video segment setup request' USING ERRCODE = '22023';
        END IF;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        v_dataset_item_id := (p_request ->> 'dataset_item_id')::uuid;
        v_frame_count := (p_request ->> 'frame_count')::integer;
        v_segment_size := (p_request ->> 'segment_size_frames')::integer;
        v_now := (p_request ->> 'now')::timestamptz;
        SELECT task.* INTO v_task FROM public.tasks AS task WHERE task.id=v_task_id;
        IF NOT FOUND
           OR v_task.project_id IS DISTINCT FROM v_project_id
           OR v_task.dataset_item_id IS DISTINCT FROM v_dataset_item_id
           OR v_task.batch_id IS DISTINCT FROM (p_request ->> 'expected_batch_id')::uuid
           OR v_task.status IS DISTINCT FROM p_request ->> 'expected_task_status'
           OR v_task.review_round_id IS DISTINCT FROM (p_request ->> 'expected_review_round_id')::uuid THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','task_target_changed'))::text;
        END IF;
        v_access := aap_workspace_v1.lock_media_project_access(v_actor_id,v_project_id);
        IF v_access IS NULL OR v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail',pg_catalog.jsonb_build_object('reason','permission_changed'))::text;
        END IF;
        v_access := aap_labeling_v1.read_media_task_access(
            v_actor_id,v_task_id,v_dataset_item_id
        );
        IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR v_access #>> '{data,annotation_write}' IS DISTINCT FROM 'true' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail',pg_catalog.jsonb_build_object('reason','permission_changed'))::text;
        END IF;
        SELECT pg_catalog.count(*)::integer INTO v_segment_count
        FROM public.video_segments AS segment
        WHERE segment.dataset_item_id=v_dataset_item_id;
        IF v_segment_count=0 THEN
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
            WHERE segment.dataset_item_id=v_dataset_item_id
              AND segment.status <> 'completed'
        )
        UPDATE public.video_segments AS segment
        SET locked_by=normalized.locked_by,
            locked_at=normalized.locked_at,
            lock_expires_at=normalized.lock_expires_at,
            status=normalized.status,
            updated_at=transaction_timestamp()
        FROM normalized
        WHERE segment.id=normalized.id
          AND (segment.locked_by IS DISTINCT FROM normalized.locked_by
               OR segment.locked_at IS DISTINCT FROM normalized.locked_at
               OR segment.lock_expires_at IS DISTINCT FROM normalized.lock_expires_at
               OR segment.status IS DISTINCT FROM normalized.status);
        IF (p_request -> 'segment_id') IS DISTINCT FROM 'null'::jsonb THEN
            SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(selected)), '[]'::jsonb)
            INTO v_rows FROM (
                SELECT segment.* FROM public.video_segments AS segment
                WHERE segment.id=(p_request ->> 'segment_id')::uuid
                  AND segment.dataset_item_id=v_dataset_item_id
                ORDER BY segment.segment_index FOR UPDATE
            ) AS selected;
        ELSE
            SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(selected)), '[]'::jsonb)
            INTO v_rows FROM (
                SELECT segment.* FROM public.video_segments AS segment
                WHERE segment.dataset_item_id=v_dataset_item_id
                  AND segment.start_frame <= (p_request ->> 'to_frame')::integer
                  AND segment.end_frame >= (p_request ->> 'from_frame')::integer
                ORDER BY segment.segment_index FOR UPDATE
            ) AS selected;
        END IF;
        IF (p_request ->> 'count_segments')::boolean THEN
            SELECT pg_catalog.count(*)::integer INTO v_segment_count
            FROM public.video_segments AS segment
            WHERE segment.dataset_item_id=v_dataset_item_id;
        ELSE
            v_segment_count := 0;
        END IF;
        v_data := pg_catalog.jsonb_build_object('segments',v_rows,'segment_count',v_segment_count);

    ELSIF v_operation = 'read_segments' THEN
        IF (p_request - ARRAY['schema','operation','task_id','project_id','actor_id','dataset_item_id','segment_id','from_frame','to_frame','nowait','count_segments']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','task_id','project_id','actor_id','dataset_item_id','segment_id','from_frame','to_frame','nowait','count_segments']) THEN
            RAISE EXCEPTION 'Invalid video segment-read request' USING ERRCODE = '22023';
        END IF;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        v_dataset_item_id := (p_request ->> 'dataset_item_id')::uuid;
        SELECT task.* INTO v_task FROM public.tasks AS task
        WHERE task.id=v_task_id AND task.project_id=v_project_id
          AND task.dataset_item_id=v_dataset_item_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',404,
                    'detail','Video segment not found')::text;
        END IF;
        v_access := aap_labeling_v1.read_media_task_access(v_actor_id,v_task_id,v_dataset_item_id);
        IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',404,
                    'detail',pg_catalog.jsonb_build_object('reason','task_not_visible'))::text;
        END IF;
        IF (p_request -> 'segment_id') IS DISTINCT FROM 'null'::jsonb THEN
            IF (p_request ->> 'nowait')::boolean THEN
                SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(selected)), '[]'::jsonb)
                INTO v_rows FROM (
                    SELECT segment.* FROM public.video_segments AS segment
                    WHERE segment.id=(p_request ->> 'segment_id')::uuid
                      AND segment.dataset_item_id=v_dataset_item_id
                    ORDER BY segment.segment_index FOR UPDATE NOWAIT
                ) AS selected;
            ELSE
                SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(selected)), '[]'::jsonb)
                INTO v_rows FROM (
                    SELECT segment.* FROM public.video_segments AS segment
                    WHERE segment.id=(p_request ->> 'segment_id')::uuid
                      AND segment.dataset_item_id=v_dataset_item_id
                    ORDER BY segment.segment_index FOR UPDATE
                ) AS selected;
            END IF;
        ELSE
            IF (p_request ->> 'nowait')::boolean THEN
                SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(selected)), '[]'::jsonb)
                INTO v_rows FROM (
                    SELECT segment.* FROM public.video_segments AS segment
                    WHERE segment.dataset_item_id=v_dataset_item_id
                      AND segment.start_frame <= (p_request ->> 'to_frame')::integer
                      AND segment.end_frame >= (p_request ->> 'from_frame')::integer
                    ORDER BY segment.segment_index FOR UPDATE NOWAIT
                ) AS selected;
            ELSE
                SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(selected)), '[]'::jsonb)
                INTO v_rows FROM (
                    SELECT segment.* FROM public.video_segments AS segment
                    WHERE segment.dataset_item_id=v_dataset_item_id
                      AND segment.start_frame <= (p_request ->> 'to_frame')::integer
                      AND segment.end_frame >= (p_request ->> 'from_frame')::integer
                    ORDER BY segment.segment_index FOR UPDATE
                ) AS selected;
            END IF;
        END IF;
        IF (p_request ->> 'count_segments')::boolean THEN
            SELECT pg_catalog.count(*) INTO v_updated
            FROM public.video_segments AS segment
            WHERE segment.dataset_item_id=v_dataset_item_id;
        ELSE
            v_updated := 0;
        END IF;
        v_data := pg_catalog.jsonb_build_object('segments',v_rows,'segment_count',v_updated);

    ELSIF v_operation = 'check_review_scopes' THEN
        IF (p_request - ARRAY['schema','operation','task_id','actor_id','annotation_id','result_annotation_version','from_frame','to_frame','frame_indices']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','task_id','actor_id','annotation_id','result_annotation_version','from_frame','to_frame','frame_indices'])
           OR pg_catalog.jsonb_typeof(p_request -> 'frame_indices') NOT IN ('array','null')
           OR (p_request ->> 'from_frame')::integer > (p_request ->> 'to_frame')::integer THEN
            RAISE EXCEPTION 'Invalid Mask review-scope query' USING ERRCODE = '22023';
        END IF;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        v_id := (p_request ->> 'annotation_id')::uuid;
        SELECT task.* INTO v_task FROM public.tasks AS task WHERE task.id=v_task_id;
        IF NOT FOUND OR v_task.dataset_item_id IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',404,
                    'detail',pg_catalog.jsonb_build_object('reason','task_not_visible'))::text;
        END IF;
        v_access := aap_labeling_v1.read_media_task_access(
            v_actor_id,v_task_id,v_task.dataset_item_id
        );
        IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR v_access #>> '{data,annotation_write}' IS DISTINCT FROM 'true' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail',pg_catalog.jsonb_build_object('reason','permission_changed'))::text;
        END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_object(
                        'scope_id',selected.id,
                        'frame_start',selected.frame_start,
                        'frame_end',selected.frame_end,
                        'region_mask_ref',selected.region_mask_ref,
                        'region_digest',selected.region_digest,
                        'decision',selected.decision
                    ) ORDER BY selected.id
                ),'[]'::jsonb)
        INTO v_rows
        FROM (
            SELECT scope.id,scope.frame_start,scope.frame_end,
                   scope.region_mask_ref,scope.region_digest,scope.decision
            FROM public.mask_review_scopes AS scope
            WHERE scope.task_id=v_task_id
              AND scope.annotation_id=v_id
              AND scope.result_annotation_version=(p_request ->> 'result_annotation_version')::integer
              AND scope.frame_start <= (p_request ->> 'to_frame')::integer
              AND scope.frame_end >= (p_request ->> 'from_frame')::integer
              AND (p_request -> 'frame_indices'='null'::jsonb OR EXISTS (
                  SELECT 1
                  FROM pg_catalog.jsonb_array_elements_text(p_request -> 'frame_indices') AS frame(value)
                  WHERE (frame.value)::integer BETWEEN scope.frame_start AND scope.frame_end
              ))
            ORDER BY scope.id
            FOR UPDATE
        ) AS selected;
        v_data := pg_catalog.jsonb_build_object('scopes',v_rows);

    ELSIF v_operation = 'expire_candidates' THEN
        IF (p_request - ARRAY['schema','operation','cutoff']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','cutoff'])
           OR pg_catalog.jsonb_typeof(p_request -> 'cutoff') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid candidate-expiry request' USING ERRCODE = '22023';
        END IF;
        v_cutoff := (p_request ->> 'cutoff')::timestamptz;
        WITH expired AS (
            UPDATE public.video_tracker_jobs AS job
            SET staged_result=NULL,
                status=CASE WHEN job.status IN ('pending_review','partially_reviewed')
                            THEN 'discarded' ELSE job.status END,
                revision=job.revision+1,
                updated_at=transaction_timestamp()
            WHERE job.staged_result IS NOT NULL
              AND job.status IN ('pending_review','partially_reviewed','cancelled')
              AND job.completed_at IS NOT NULL
              AND job.completed_at <= v_cutoff
            RETURNING job.id
        )
        SELECT pg_catalog.count(*)::integer INTO v_updated FROM expired;
        v_data := pg_catalog.jsonb_build_object('expired',v_updated);

    ELSIF v_operation = 'create_job' THEN
        IF (p_request - ARRAY['schema','operation','task_id','project_id','dataset_item_id','actor_id','expected_batch_id','expected_task_status','expected_review_round_id','annotation_id','segment_id','target_class_name','target_tool_unit_id','job_kind','track_id_snapshot','correction_frame','model_key','direction','from_frame','to_frame','prompt']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','task_id','project_id','dataset_item_id','actor_id','expected_batch_id','expected_task_status','expected_review_round_id','annotation_id','segment_id','target_class_name','target_tool_unit_id','job_kind','track_id_snapshot','correction_frame','model_key','direction','from_frame','to_frame','prompt'])
           OR pg_catalog.jsonb_typeof(p_request -> 'prompt') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'model_key') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'direction') IS DISTINCT FROM 'string'
           OR (p_request ->> 'from_frame')::integer > (p_request ->> 'to_frame')::integer THEN
            RAISE EXCEPTION 'Invalid video tracker create request' USING ERRCODE = '22023';
        END IF;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_dataset_item_id := (p_request ->> 'dataset_item_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        v_access := aap_workspace_v1.lock_media_project_access(v_actor_id,v_project_id);
        IF v_access IS NULL OR v_access ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
           OR v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail',pg_catalog.jsonb_build_object('reason','permission_changed'))::text;
        END IF;
        v_access_data := v_access -> 'data';
        BEGIN
            SELECT task.* INTO v_task FROM public.tasks AS task
            WHERE task.id=v_task_id FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','task_busy'))::text;
        END;
        IF NOT FOUND
           OR v_task.project_id IS DISTINCT FROM v_project_id
           OR v_task.dataset_item_id IS DISTINCT FROM v_dataset_item_id
           OR v_task.batch_id IS DISTINCT FROM (p_request ->> 'expected_batch_id')::uuid
           OR v_task.status IS DISTINCT FROM p_request ->> 'expected_task_status'
           OR v_task.review_round_id IS DISTINCT FROM (p_request ->> 'expected_review_round_id')::uuid THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','task_target_changed'))::text;
        END IF;
        v_access := aap_labeling_v1.read_media_task_access(v_actor_id,v_task_id,v_dataset_item_id);
        IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR v_access #>> '{data,annotation_write}' IS DISTINCT FROM 'true' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail',pg_catalog.jsonb_build_object('reason','permission_changed'))::text;
        END IF;
        SELECT segment.* INTO v_segment FROM public.video_segments AS segment
        WHERE segment.id=(p_request ->> 'segment_id')::uuid
          AND segment.dataset_item_id=v_dataset_item_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND OR (p_request ->> 'from_frame')::integer < v_segment.start_frame
           OR (p_request ->> 'to_frame')::integer > v_segment.end_frame THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','segment_lease_changed'))::text;
        END IF;
        IF ((v_access_data #>> '{video_collaboration_enabled}')::boolean
                OR (v_access_data #>> '{is_manager}') IS DISTINCT FROM 'true')
           AND (v_segment.locked_by IS DISTINCT FROM v_actor_id
                OR v_segment.lock_expires_at IS NULL
                OR v_segment.lock_expires_at <= pg_catalog.clock_timestamp()) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','segment_lease_changed'))::text;
        END IF;
        IF (v_access_data #>> '{video_collaboration_enabled}')::boolean
           AND v_segment.assignee_id IS DISTINCT FROM v_actor_id THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','segment_lease_changed'))::text;
        END IF;
        v_now := transaction_timestamp();
        BEGIN
            INSERT INTO public.video_tracker_jobs(
                id,task_id,dataset_item_id,annotation_id,segment_id,target_class_name,
                target_tool_unit_id,created_by,status,job_kind,track_id_snapshot,
                correction_frame,model_key,direction,from_frame,to_frame,prompt,
                revision,event_channel
            ) VALUES (
                pg_catalog.gen_random_uuid(),v_task_id,v_dataset_item_id,
                (p_request ->> 'annotation_id')::uuid,
                v_segment.id,(p_request ->> 'target_class_name'),
                (p_request ->> 'target_tool_unit_id'),v_actor_id,'queued',
                p_request ->> 'job_kind',(p_request ->> 'track_id_snapshot'),
                (p_request ->> 'correction_frame')::integer,p_request ->> 'model_key',
                p_request ->> 'direction',(p_request ->> 'from_frame')::integer,
                (p_request ->> 'to_frame')::integer,p_request -> 'prompt',1,'pending'
            ) RETURNING * INTO v_job;
        EXCEPTION WHEN unique_violation THEN
            IF p_request ->> 'job_kind'='correction' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                    DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                        'detail',pg_catalog.jsonb_build_object('reason','correction_job_active'))::text;
            END IF;
            RAISE;
        END;
        UPDATE public.video_tracker_jobs AS job
        SET event_channel='video-tracker-job:' || v_job.id::text,
            updated_at=transaction_timestamp()
        WHERE job.id=v_job.id RETURNING job.* INTO v_job;
        v_data := pg_catalog.to_jsonb(v_job);

    ELSIF v_operation = 'cancel_job' THEN
        IF (p_request - ARRAY['schema','operation','job_id','actor_id','now']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','job_id','actor_id','now']) THEN
            RAISE EXCEPTION 'Invalid video tracker cancellation request' USING ERRCODE = '22023';
        END IF;
        v_id := (p_request ->> 'job_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        v_now := (p_request ->> 'now')::timestamptz;
        SELECT job.* INTO v_job FROM public.video_tracker_jobs AS job WHERE job.id=v_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',404,
                    'detail','Video tracker job not found')::text;
        END IF;
        SELECT task.* INTO v_task FROM public.tasks AS task WHERE task.id=v_job.task_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',404,
                    'detail','Video tracker job not found')::text;
        END IF;
        v_access := aap_workspace_v1.lock_media_project_access(v_actor_id,v_task.project_id);
        IF v_access IS NULL OR v_access ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
           OR v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',404,
                    'detail',pg_catalog.jsonb_build_object('reason','task_not_visible'))::text;
        END IF;
        v_access_data := v_access -> 'data';
        v_access := aap_labeling_v1.read_media_task_access(
            v_actor_id,v_task.id,v_job.dataset_item_id
        );
        IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',404,
                    'detail',pg_catalog.jsonb_build_object('reason','task_not_visible'))::text;
        END IF;
        IF v_job.created_by IS DISTINCT FROM v_actor_id
           AND v_access_data -> 'is_manager' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail','Video tracker job belongs to another user')::text;
        END IF;
        BEGIN
            SELECT job.* INTO v_job FROM public.video_tracker_jobs AS job
            WHERE job.id=v_id FOR UPDATE;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','tracker_job_busy'))::text;
        END;
        IF v_job.job_kind='correction'
           AND v_job.status IN ('accepted','discarded','failed') THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object(
                        'reason','correction_job_terminal','status',v_job.status
                    ))::text;
        END IF;
        IF v_job.job_kind='tracking'
           AND v_job.status IN ('pending_review','partially_reviewed') THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail','Candidate awaiting review cannot be cancelled; discard it instead')::text;
        END IF;
        v_new_status := v_job.status;
        v_new_cancel_at := v_job.cancel_requested_at;
        v_new_completed_at := v_job.completed_at;
        v_new_staged := v_job.staged_result;
        IF v_job.job_kind='correction'
           AND v_job.status IN ('pending_review','partially_reviewed') THEN
            v_new_status := 'cancelled';
            v_new_staged := NULL;
            v_new_cancel_at := COALESCE(v_new_cancel_at,v_now);
            v_new_completed_at := COALESCE(v_new_completed_at,v_now);
        ELSIF v_job.status NOT IN (
            'completed','failed','cancelled','pending_review','partially_reviewed','accepted','discarded'
        ) THEN
            v_new_status := 'cancelled';
            IF v_job.job_kind='correction' THEN v_new_staged := NULL; END IF;
            v_new_cancel_at := COALESCE(v_new_cancel_at,v_now);
            v_new_completed_at := COALESCE(v_new_completed_at,v_now);
        END IF;
        IF v_new_status IS DISTINCT FROM v_job.status
           OR v_new_staged IS DISTINCT FROM v_job.staged_result
           OR v_new_cancel_at IS DISTINCT FROM v_job.cancel_requested_at
           OR v_new_completed_at IS DISTINCT FROM v_job.completed_at THEN
            UPDATE public.video_tracker_jobs AS job
            SET status=v_new_status, staged_result=v_new_staged,
                cancel_requested_at=v_new_cancel_at, completed_at=v_new_completed_at,
                updated_at=transaction_timestamp()
            WHERE job.id=v_id RETURNING job.* INTO v_job;
        END IF;
        v_data := pg_catalog.to_jsonb(v_job);

    ELSIF v_operation IN ('save_keyframe','operate_keyframe') THEN
        IF (p_request - ARRAY['schema','operation','task_id','project_id','actor_id','expected_batch_id','expected_task_status','expected_review_round_id','annotation_id','segment_id','frame_index','expected_version','geometry']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','task_id','project_id','actor_id','expected_batch_id','expected_task_status','expected_review_round_id','annotation_id','segment_id','frame_index','expected_version','geometry'])
           OR pg_catalog.jsonb_typeof(p_request -> 'geometry') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'Invalid video keyframe write request' USING ERRCODE = '22023';
        END IF;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        v_id := (p_request ->> 'annotation_id')::uuid;
        SELECT task.* INTO v_task FROM public.tasks AS task
        WHERE task.id=v_task_id FOR UPDATE NOWAIT;
        IF NOT FOUND
           OR v_task.project_id IS DISTINCT FROM v_project_id
           OR v_task.batch_id IS DISTINCT FROM (p_request ->> 'expected_batch_id')::uuid
           OR v_task.status IS DISTINCT FROM p_request ->> 'expected_task_status'
           OR v_task.review_round_id IS DISTINCT FROM (p_request ->> 'expected_review_round_id')::uuid THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','task_target_changed'))::text;
        END IF;
        v_access := aap_workspace_v1.lock_media_project_access(v_actor_id,v_project_id);
        IF v_access IS NULL OR v_access ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
           OR v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail',pg_catalog.jsonb_build_object('reason','permission_changed'))::text;
        END IF;
        v_access_data := v_access -> 'data';
        v_access := aap_labeling_v1.read_media_task_access(v_actor_id,v_task_id,v_task.dataset_item_id);
        IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR v_access #>> '{data,annotation_write}' IS DISTINCT FROM 'true' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail',pg_catalog.jsonb_build_object('reason','permission_changed'))::text;
        END IF;
        v_task_lock := aap_labeling_v1.task_edit_lock_command(
            pg_catalog.jsonb_build_object(
                'schema','aap-labeling-task-locks/v1',
                'action','assert_write_allowed',
                'task_id',v_task_id,
                'user_id',v_actor_id
            )
        );
        IF v_task_lock ->> 'schema' IS DISTINCT FROM 'aap-labeling-task-locks-data/v1'
           OR v_task_lock -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR v_task_lock #> '{data,owner_id}' IS DISTINCT FROM 'null'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','task_lock_conflict'))::text;
        END IF;
        SELECT segment.* INTO v_segment FROM public.video_segments AS segment
        WHERE segment.id=(p_request ->> 'segment_id')::uuid
          AND segment.dataset_item_id=v_task.dataset_item_id
        FOR UPDATE;
        IF NOT FOUND
           OR (p_request ->> 'frame_index')::integer < v_segment.start_frame
           OR (p_request ->> 'frame_index')::integer > v_segment.end_frame
           OR ((v_access_data ->> 'video_collaboration_enabled')::boolean
               AND v_segment.assignee_id IS DISTINCT FROM v_actor_id)
           OR (((v_access_data ->> 'video_collaboration_enabled')::boolean
                OR v_access_data -> 'is_manager' IS DISTINCT FROM 'true'::jsonb)
               AND (v_segment.locked_by IS DISTINCT FROM v_actor_id
                    OR v_segment.lock_expires_at IS NULL
                    OR v_segment.lock_expires_at <= pg_catalog.clock_timestamp())) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','segment_lease_changed'))::text;
        END IF;
        SELECT annotation.* INTO v_annotation FROM public.annotations AS annotation
        WHERE annotation.id=v_id FOR UPDATE;
        IF NOT FOUND OR v_annotation.task_id IS DISTINCT FROM v_task_id
           OR v_annotation.project_id IS DISTINCT FROM v_project_id
           OR v_annotation.is_active IS NOT TRUE
           OR v_annotation.was_cancelled IS TRUE
           OR v_annotation.is_locked IS TRUE
           OR v_annotation.version IS DISTINCT FROM (p_request ->> 'expected_version')::integer
           OR v_annotation.geometry ->> 'type' IS DISTINCT FROM 'video_track_mask'
           OR (v_annotation.video_segment_id IS NOT NULL
               AND v_annotation.video_segment_id IS DISTINCT FROM v_segment.id)
           OR p_request -> 'geometry' ->> 'type' IS DISTINCT FROM 'video_track_mask' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','source_version_conflict'))::text;
        END IF;
        UPDATE public.annotations AS annotation
        SET geometry=p_request -> 'geometry',
            version=annotation.version+1,
            updated_at=transaction_timestamp()
        WHERE annotation.id=v_id
        RETURNING annotation.version,annotation.updated_at
        INTO v_expected_version,v_now;
        v_data := pg_catalog.jsonb_build_object(
            'annotation_id',v_id,'version',v_expected_version,'updated_at',v_now
        );

    ELSIF v_operation = 'apply_review' THEN
        IF (p_request - ARRAY['schema','operation','job_id','task_id','actor_id','kind','expected_revision','expected_status','prompt','staged_result','status','source_annotations','new_tracks','mask_review_scope']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','job_id','task_id','actor_id','kind','expected_revision','expected_status','prompt','staged_result','status','source_annotations','new_tracks','mask_review_scope'])
           OR pg_catalog.jsonb_typeof(p_request -> 'prompt') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'source_annotations') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(p_request -> 'new_tracks') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(p_request -> 'kind') IS DISTINCT FROM 'string'
           OR (p_request ->> 'kind'='discard' AND p_request ->> 'status'<>'discarded')
           OR (p_request ->> 'kind'='accept' AND p_request ->> 'status' NOT IN ('accepted','partially_reviewed'))
           OR (p_request -> 'mask_review_scope' IS DISTINCT FROM 'null'::jsonb
               AND (p_request ->> 'kind'<>'decide'
                    OR pg_catalog.jsonb_typeof(p_request -> 'mask_review_scope') IS DISTINCT FROM 'object'))
           OR (p_request -> 'staged_result' IS DISTINCT FROM 'null'::jsonb
               AND pg_catalog.jsonb_typeof(p_request -> 'staged_result') IS DISTINCT FROM 'object') THEN
            RAISE EXCEPTION 'Invalid video candidate review request' USING ERRCODE = '22023';
        END IF;
        v_id := (p_request ->> 'job_id')::uuid;
        v_task_id := (p_request ->> 'task_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        SELECT job.* INTO v_job FROM public.video_tracker_jobs AS job WHERE job.id=v_id;
        IF NOT FOUND OR v_job.task_id IS DISTINCT FROM v_task_id THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','task_changed'))::text;
        END IF;
        SELECT task.* INTO v_task FROM public.tasks AS task WHERE task.id=v_task_id;
        IF NOT FOUND OR v_task.dataset_item_id IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','task_changed'))::text;
        END IF;
        v_access := aap_workspace_v1.lock_media_project_access(v_actor_id,v_task.project_id);
        IF v_access IS NULL OR v_access ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
           OR v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail',pg_catalog.jsonb_build_object('reason','permission_changed'))::text;
        END IF;
        v_workspace_access := v_access -> 'data';
        v_access := aap_labeling_v1.read_media_task_access(
            v_actor_id,v_task_id,v_task.dataset_item_id
        );
        IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail',pg_catalog.jsonb_build_object('reason','permission_changed'))::text;
        END IF;
        IF p_request ->> 'kind'='discard'
           AND v_job.created_by IS DISTINCT FROM v_actor_id
           AND v_workspace_access -> 'is_manager' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','permission_changed'))::text;
        END IF;
        IF p_request ->> 'kind' IN ('accept','decide') THEN
            BEGIN
                SELECT task.* INTO v_task FROM public.tasks AS task
                WHERE task.id=v_task_id FOR UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                    DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                        'detail',pg_catalog.jsonb_build_object('reason','task_locked'))::text;
            END;
            v_access := aap_labeling_v1.read_media_task_access(
                v_actor_id,v_task_id,v_task.dataset_item_id
            );
            IF v_access #>> '{data,annotation_write}' IS DISTINCT FROM 'true'
               OR v_task.status='completed' OR
               (v_task.status='review' AND v_workspace_access -> 'is_manager' IS DISTINCT FROM 'true'::jsonb
                AND (v_workspace_access ->> 'project_role' IS DISTINCT FROM 'reviewer'
                     OR v_task.reviewer_id IS DISTINCT FROM v_actor_id
                     OR v_task.reviewer_claimed_at IS NULL)) OR
               (v_task.status NOT IN ('review','pending','in_progress')
                AND v_workspace_access -> 'is_manager' IS DISTINCT FROM 'true'::jsonb) THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                    DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                        'detail',pg_catalog.jsonb_build_object('reason','task_locked'))::text;
            END IF;
        END IF;
        IF p_request ->> 'kind' IN ('accept','decide') AND v_job.segment_id IS NOT NULL THEN
            SELECT segment.* INTO v_segment FROM public.video_segments AS segment
            WHERE segment.id=v_job.segment_id AND segment.dataset_item_id=v_job.dataset_item_id
            FOR UPDATE NOWAIT;
            IF NOT FOUND
               OR v_job.from_frame < v_segment.start_frame
               OR v_job.to_frame > v_segment.end_frame
               OR (v_segment.locked_by IS NOT NULL
                   AND v_segment.locked_by IS DISTINCT FROM v_actor_id
                   AND v_segment.lock_expires_at > pg_catalog.clock_timestamp())
               OR (v_task.status<>'review'
                   AND v_workspace_access -> 'is_manager' IS DISTINCT FROM 'true'::jsonb
                   AND (v_segment.assignee_id IS DISTINCT FROM v_actor_id
                        OR v_segment.locked_by IS DISTINCT FROM v_actor_id
                        OR v_segment.lock_expires_at IS NULL
                        OR v_segment.lock_expires_at <= pg_catalog.clock_timestamp())) THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                    DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                        'detail',pg_catalog.jsonb_build_object('reason','segment_lease_changed'))::text;
            END IF;
        END IF;
        BEGIN
            SELECT job.* INTO v_job FROM public.video_tracker_jobs AS job
            WHERE job.id=v_id FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','tracker_job_busy'))::text;
        END;
        IF NOT FOUND OR v_job.task_id IS DISTINCT FROM v_task_id
           OR v_job.revision IS DISTINCT FROM (p_request ->> 'expected_revision')::integer
           OR v_job.status IS DISTINCT FROM p_request ->> 'expected_status'
           OR v_job.status NOT IN ('pending_review','partially_reviewed','cancelled') THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                    'detail',pg_catalog.jsonb_build_object('reason','job_revision_conflict'))::text;
        END IF;
        FOR v_source IN
            SELECT entry.value FROM pg_catalog.jsonb_array_elements(p_request -> 'source_annotations') AS entry(value)
            ORDER BY (entry.value ->> 'annotation_id')::uuid
        LOOP
            IF pg_catalog.jsonb_typeof(v_source) IS DISTINCT FROM 'object'
               OR (v_source - ARRAY['annotation_id','expected_version','geometry','track_id','annotation_type','tool_unit_id']) IS DISTINCT FROM '{}'::jsonb
               OR NOT (v_source ?& ARRAY['annotation_id','expected_version','geometry','track_id','annotation_type','tool_unit_id']) THEN
                RAISE EXCEPTION 'Invalid video source Annotation update' USING ERRCODE = '22023';
            END IF;
            SELECT annotation.* INTO v_annotation FROM public.annotations AS annotation
            WHERE annotation.id=(v_source ->> 'annotation_id')::uuid
            FOR UPDATE;
            IF NOT FOUND OR v_annotation.task_id IS DISTINCT FROM v_task_id
               OR v_annotation.project_id IS DISTINCT FROM v_task.project_id
               OR v_annotation.is_active IS NOT TRUE OR v_annotation.was_cancelled IS TRUE
               OR v_annotation.is_locked IS TRUE
               OR v_annotation.version IS DISTINCT FROM (v_source ->> 'expected_version')::integer
               OR pg_catalog.jsonb_typeof(v_source -> 'geometry') IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_source -> 'track_id') NOT IN ('string','null')
               OR pg_catalog.jsonb_typeof(v_source -> 'annotation_type') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_source -> 'tool_unit_id') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                    DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                        'detail',pg_catalog.jsonb_build_object('reason','source_version_conflict'))::text;
            END IF;
            UPDATE public.annotations AS annotation
            SET geometry=v_source -> 'geometry',
                track_id=(v_source ->> 'track_id'),
                annotation_type=v_source ->> 'annotation_type',
                tool_unit_id=v_source ->> 'tool_unit_id',
                version=annotation.version+1,
                updated_at=transaction_timestamp()
            WHERE annotation.id=v_annotation.id;
        END LOOP;
        FOR v_new_track IN
            SELECT entry.value FROM pg_catalog.jsonb_array_elements(p_request -> 'new_tracks') AS entry(value)
            ORDER BY (entry.value ->> 'annotation_id')::uuid
        LOOP
            IF pg_catalog.jsonb_typeof(v_new_track) IS DISTINCT FROM 'object'
               OR (v_new_track - ARRAY['annotation_id','video_segment_id','user_id','class_name','tool_unit_id','geometry','track_id','annotation_type']) IS DISTINCT FROM '{}'::jsonb
               OR NOT (v_new_track ?& ARRAY['annotation_id','video_segment_id','user_id','class_name','tool_unit_id','geometry','track_id','annotation_type'])
               OR pg_catalog.jsonb_typeof(v_new_track -> 'geometry') IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_new_track -> 'track_id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_new_track -> 'annotation_type') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_new_track -> 'tool_unit_id') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_new_track -> 'class_name') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION 'Invalid discovered video track' USING ERRCODE = '22023';
            END IF;
            INSERT INTO public.annotations(
                id,task_id,project_id,video_segment_id,user_id,source,annotation_type,
                tool_unit_id,class_name,geometry,confidence,parent_prediction_id,
                parent_annotation_id,track_id,scene_track_id,sensor_dataset_item_id,
                sensor_role,sensor_visibility,calibration_revision,calibration_digest,
                temporal_role,lead_time,was_cancelled,ground_truth,is_active,attributes,
                attributes_meta,z_order,is_locked,is_hidden,version
            ) VALUES (
                (v_new_track ->> 'annotation_id')::uuid,v_task_id,v_task.project_id,
                (v_new_track ->> 'video_segment_id')::uuid,
                (v_new_track ->> 'user_id')::uuid,'ai_tracker',
                v_new_track ->> 'annotation_type',v_new_track ->> 'tool_unit_id',
                v_new_track ->> 'class_name',v_new_track -> 'geometry',NULL,NULL,NULL,
                v_new_track ->> 'track_id',NULL,NULL,NULL,NULL,NULL,NULL,
                'sample',NULL,false,false,true,'{}'::jsonb,'{}'::jsonb,0,false,false,1
            );
        END LOOP;
        v_review_scope := p_request -> 'mask_review_scope';
        IF v_review_scope IS DISTINCT FROM 'null'::jsonb THEN
            IF (v_review_scope - ARRAY[
                    'id','project_id','task_id','annotation_id','qc_issue_id','source_job_id',
                    'reviewer_id','source_annotation_version','result_annotation_version',
                    'source_job_revision','frame_start','frame_end','region_mask_ref',
                    'region_digest','candidate_digest','decision'
                ]) IS DISTINCT FROM '{}'::jsonb
               OR NOT (v_review_scope ?& ARRAY[
                    'id','project_id','task_id','annotation_id','qc_issue_id','source_job_id',
                    'reviewer_id','source_annotation_version','result_annotation_version',
                    'source_job_revision','frame_start','frame_end','region_mask_ref',
                    'region_digest','candidate_digest','decision'
                ])
               OR pg_catalog.jsonb_typeof(v_review_scope -> 'region_mask_ref') IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_review_scope -> 'decision') IS DISTINCT FROM 'string'
               OR v_review_scope ->> 'decision' NOT IN ('accept','reject')
               OR (v_review_scope ->> 'region_digest') !~ '^[0-9a-f]{64}$'
               OR (v_review_scope ->> 'candidate_digest') !~ '^sha256:[0-9a-f]{64}$'
               OR (v_review_scope ->> 'frame_start')::integer < 0
               OR (v_review_scope ->> 'frame_end')::integer IS DISTINCT FROM
                    (v_review_scope ->> 'frame_start')::integer
               OR (v_review_scope ->> 'project_id')::uuid IS DISTINCT FROM v_task.project_id
               OR (v_review_scope ->> 'task_id')::uuid IS DISTINCT FROM v_task_id
               OR (v_review_scope ->> 'source_job_id')::uuid IS DISTINCT FROM v_id
               OR (v_review_scope ->> 'reviewer_id')::uuid IS DISTINCT FROM v_actor_id
               OR (v_review_scope ->> 'source_job_revision')::integer IS DISTINCT FROM
                    (p_request ->> 'expected_revision')::integer
               OR (p_request #>> '{prompt,review_state,last_decision,decision}')
                    IS DISTINCT FROM v_review_scope ->> 'decision'
               OR (p_request #>> '{prompt,review_state,last_decision,region_digest}')
                    IS DISTINCT FROM v_review_scope ->> 'region_digest'
               OR (p_request #>> '{prompt,review_state,last_decision,candidate_digest}')
                    IS DISTINCT FROM v_review_scope ->> 'candidate_digest' THEN
                RAISE EXCEPTION 'Invalid Mask review scope evidence' USING ERRCODE = '22023';
            END IF;
            SELECT annotation.* INTO v_annotation
            FROM public.annotations AS annotation
            WHERE annotation.id=(v_review_scope ->> 'annotation_id')::uuid
              AND annotation.task_id=v_task_id
              AND annotation.project_id=v_task.project_id;
            IF NOT FOUND OR v_annotation.is_active IS NOT TRUE
               OR v_annotation.was_cancelled IS TRUE
               OR v_annotation.version IS DISTINCT FROM
                    (v_review_scope ->> 'result_annotation_version')::integer THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                    DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                        'detail',pg_catalog.jsonb_build_object('reason','source_version_conflict'))::text;
            END IF;
            v_scope_source_version := NULLIF(
                v_job.prompt #>> ARRAY[
                    'expected_source_versions',(v_review_scope ->> 'annotation_id')
                ],''
            )::integer;
            IF v_scope_source_version IS NULL
               OR v_scope_source_version IS DISTINCT FROM
                    (v_review_scope ->> 'source_annotation_version')::integer
               OR NOT (v_job.prompt -> 'expected_source_versions'
                    ? (v_review_scope ->> 'annotation_id')) THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                    DETAIL = pg_catalog.jsonb_build_object('status_code',409,
                        'detail',pg_catalog.jsonb_build_object('reason','source_version_conflict'))::text;
            END IF;
            INSERT INTO public.mask_review_scopes(
                id,project_id,task_id,annotation_id,qc_issue_id,source_job_id,
                reviewer_id,source_annotation_version,result_annotation_version,
                source_job_revision,frame_start,frame_end,region_mask_ref,
                region_digest,candidate_digest,decision
            ) VALUES (
                (v_review_scope ->> 'id')::uuid,
                (v_review_scope ->> 'project_id')::uuid,
                (v_review_scope ->> 'task_id')::uuid,
                (v_review_scope ->> 'annotation_id')::uuid,
                (v_review_scope ->> 'qc_issue_id')::uuid,
                (v_review_scope ->> 'source_job_id')::uuid,
                (v_review_scope ->> 'reviewer_id')::uuid,
                (v_review_scope ->> 'source_annotation_version')::integer,
                (v_review_scope ->> 'result_annotation_version')::integer,
                (v_review_scope ->> 'source_job_revision')::integer,
                (v_review_scope ->> 'frame_start')::integer,
                (v_review_scope ->> 'frame_end')::integer,
                v_review_scope -> 'region_mask_ref',
                v_review_scope ->> 'region_digest',
                v_review_scope ->> 'candidate_digest',
                v_review_scope ->> 'decision'
            );
        END IF;
        UPDATE public.video_tracker_jobs AS job
        SET prompt=p_request -> 'prompt',
            staged_result=CASE WHEN p_request -> 'staged_result'='null'::jsonb
                               THEN NULL ELSE p_request -> 'staged_result' END,
            status=p_request ->> 'status',
            revision=CASE WHEN p_request ->> 'kind'='decide'
                          THEN job.revision+1 ELSE job.revision END,
            updated_at=transaction_timestamp()
        WHERE job.id=v_id
        RETURNING job.* INTO v_job;
        v_data := pg_catalog.to_jsonb(v_job);

    ELSIF v_operation = 'mark_enqueued' THEN
        IF (p_request - ARRAY['schema','operation','job_id','celery_task_id']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','job_id','celery_task_id']) THEN
            RAISE EXCEPTION 'Invalid tracker enqueue receipt' USING ERRCODE = '22023';
        END IF;
        UPDATE public.video_tracker_jobs AS job
        SET celery_task_id=p_request ->> 'celery_task_id',
            updated_at=transaction_timestamp()
        WHERE job.id=(p_request ->> 'job_id')::uuid
        RETURNING job.* INTO v_job;
        v_data := CASE WHEN FOUND THEN pg_catalog.to_jsonb(v_job) ELSE NULL END;

    ELSIF v_operation = 'mark_enqueue_failed' THEN
        IF (p_request - ARRAY['schema','operation','job_id','completed_at']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','job_id','completed_at']) THEN
            RAISE EXCEPTION 'Invalid tracker enqueue failure receipt' USING ERRCODE = '22023';
        END IF;
        UPDATE public.video_tracker_jobs AS job
        SET status='failed',error_message='tracker_enqueue_failed',
            completed_at=(p_request ->> 'completed_at')::timestamptz,
            staged_result=NULL,updated_at=transaction_timestamp()
        WHERE job.id=(p_request ->> 'job_id')::uuid AND job.status='queued'
        RETURNING job.* INTO v_job;
        IF NOT FOUND THEN
            SELECT job.* INTO v_job FROM public.video_tracker_jobs AS job
            WHERE job.id=(p_request ->> 'job_id')::uuid;
        END IF;
        v_data := CASE WHEN FOUND THEN pg_catalog.to_jsonb(v_job) ELSE NULL END;

    ELSIF v_operation = 'list_timeline' THEN
        IF (p_request - ARRAY['schema','operation','actor_id','model_key','project_id','allowed_project_ids','status','cursor_created_at','cursor_id','limit']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_request ?& ARRAY['schema','operation','actor_id','model_key','project_id','allowed_project_ids','status','cursor_created_at','cursor_id','limit'])
           OR pg_catalog.jsonb_typeof(p_request -> 'actor_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'limit') IS DISTINCT FROM 'number'
           OR (p_request -> 'cursor_id' IS DISTINCT FROM 'null'::jsonb)
              IS DISTINCT FROM (p_request -> 'cursor_created_at' IS DISTINCT FROM 'null'::jsonb) THEN
            RAISE EXCEPTION 'Invalid video tracker timeline request' USING ERRCODE = '22023';
        END IF;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        SELECT pg_catalog.jsonb_build_object(
            'actor_id',facts.actor_id,'platform_role',facts.platform_role,
            'is_active',facts.is_active
        ) INTO v_actor
        FROM aap_identity_v1.read_actor_facts(v_actor_id) AS facts;
        IF v_actor IS NULL OR v_actor -> 'is_active' IS DISTINCT FROM 'true'::jsonb
           OR v_actor ->> 'platform_role' NOT IN ('super_admin','project_admin') THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail',pg_catalog.jsonb_build_object('reason','permission_denied'))::text;
        END IF;
        IF p_request -> 'project_id' IS DISTINCT FROM 'null'::jsonb THEN
            v_project_id := (p_request ->> 'project_id')::uuid;
            v_access := aap_workspace_v1.read_media_project_access(v_actor_id,v_project_id);
            IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb
               OR v_access #>> '{data,is_manager}' IS DISTINCT FROM 'true' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                    DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                        'detail',pg_catalog.jsonb_build_object('reason','permission_denied'))::text;
            END IF;
        END IF;
        v_manager_ids := NULL;
        IF p_request -> 'allowed_project_ids' IS DISTINCT FROM 'null'::jsonb THEN
            SELECT COALESCE(pg_catalog.array_agg(entry.value::uuid ORDER BY entry.value::uuid),ARRAY[]::uuid[])
            INTO v_ids FROM pg_catalog.jsonb_array_elements_text(
                p_request -> 'allowed_project_ids'
            ) AS entry(value);
            v_manager_reply := aap_workspace_v1.read_media_managed_projects(v_actor_id,v_ids);
            IF v_manager_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                    DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                        'detail',pg_catalog.jsonb_build_object('reason','permission_denied'))::text;
            END IF;
            SELECT COALESCE(pg_catalog.array_agg((entry.value ->> 'project_id')::uuid),ARRAY[]::uuid[])
            INTO v_manager_ids
            FROM pg_catalog.jsonb_array_elements(v_manager_reply #> '{data,projects}') AS entry(value);
        ELSIF v_actor ->> 'platform_role'='project_admin'
              AND p_request -> 'project_id'='null'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_video_control_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail',pg_catalog.jsonb_build_object('reason','permission_denied'))::text;
        END IF;
        SELECT COALESCE(
            pg_catalog.jsonb_object_agg(counts.status,counts.count), '{}'::jsonb
        ) INTO v_counts
        FROM (
            SELECT job.status,pg_catalog.count(*)::integer AS count
            FROM public.video_tracker_jobs AS job
            JOIN public.tasks AS task ON task.id=job.task_id
            WHERE (p_request -> 'model_key'='null'::jsonb OR job.model_key=p_request ->> 'model_key')
              AND (p_request -> 'project_id'='null'::jsonb OR task.project_id=v_project_id)
              AND (v_manager_ids IS NULL OR task.project_id=ANY(v_manager_ids))
            GROUP BY job.status
        ) AS counts;
        SELECT COALESCE(pg_catalog.jsonb_agg(
                   pg_catalog.jsonb_build_object(
                       'job',pg_catalog.to_jsonb(selected) - 'project_id',
                       'project_id',selected.project_id
                   ) ORDER BY selected.created_at DESC,selected.id DESC
               ),'[]'::jsonb)
        INTO v_rows
        FROM (
            SELECT job.*,task.project_id
            FROM public.video_tracker_jobs AS job
            JOIN public.tasks AS task ON task.id=job.task_id
            WHERE (p_request -> 'model_key'='null'::jsonb OR job.model_key=p_request ->> 'model_key')
              AND (p_request -> 'project_id'='null'::jsonb OR task.project_id=v_project_id)
              AND (v_manager_ids IS NULL OR task.project_id=ANY(v_manager_ids))
              AND (p_request -> 'status'='null'::jsonb OR job.status=p_request ->> 'status')
              AND (p_request -> 'cursor_created_at'='null'::jsonb
                   OR job.created_at < (p_request ->> 'cursor_created_at')::timestamptz
                   OR (job.created_at=(p_request ->> 'cursor_created_at')::timestamptz
                       AND job.id < (p_request ->> 'cursor_id')::uuid))
            ORDER BY job.created_at DESC,job.id DESC
            LIMIT (p_request ->> 'limit')::integer + 1
        ) AS selected;
        v_has_more := pg_catalog.jsonb_array_length(v_rows) > (p_request ->> 'limit')::integer;
        v_data := pg_catalog.jsonb_build_object(
            'counts',v_counts,'rows',v_rows,'has_more',v_has_more
        );

    ELSE
        RAISE EXCEPTION 'Unsupported video control operation' USING ERRCODE = '22023';
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema','aap-labeling-video-control/v1',
        'ok',true,
        'request',p_request,
        'data',v_data
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.video_control_command(jsonb) FROM PUBLIC;
