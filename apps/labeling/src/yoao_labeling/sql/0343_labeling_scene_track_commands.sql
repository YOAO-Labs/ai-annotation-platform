CREATE FUNCTION aap_labeling_v1.scene_track_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_action text;
    v_keys text[];
    v_expected_keys text[];
    v_error_status integer;
    v_error_reason text;
    v_error_message text;
    v_data jsonb;
    v_operation public.scene_track_operations%ROWTYPE;
    v_primary public.scene_tracks%ROWTYPE;
    v_secondary public.scene_tracks%ROWTYPE;
    v_row record;
    v_entry record;
    v_lock_reply jsonb;
    v_scene_id uuid;
    v_project_id uuid;
    v_actor_id uuid;
    v_operation_id uuid;
    v_primary_id uuid;
    v_secondary_id uuid;
    v_interval_track_id uuid;
    v_created_track_id text;
    v_created_track_uuid uuid;
    v_target_task_id uuid;
    v_track_ids text[] := ARRAY[]::text[];
    v_before_track_ids text[] := ARRAY[]::text[];
    v_after_track_ids text[] := ARRAY[]::text[];
    v_replaced_track_ids uuid[] := ARRAY[]::uuid[];
    v_kind text;
    v_track_id text;
    v_secondary_track_id text;
    v_frame_index integer;
    v_resume_frame integer;
    v_request_digest text;
    v_idempotency_key text;
    v_expected_snapshot_token text;
    v_snapshot_token text;
    v_current_snapshot jsonb;
    v_before_state jsonb;
    v_after_state jsonb;
    v_source_revisions jsonb;
    v_result_revisions jsonb;
    v_response jsonb;
    v_task_ids jsonb;
    v_operations jsonb;
    v_interval_set jsonb;
    v_target_valid boolean;
    v_expected_revision integer;
    v_actual_revision integer;
BEGIN
    IF p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_request -> 'schema') IS DISTINCT FROM 'string'
       OR p_request ->> 'schema' <> 'aap-labeling-scene-track-commands/v1'
       OR pg_catalog.jsonb_typeof(p_request -> 'action') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Labeling SceneTrack command request'
            USING ERRCODE = '22023';
    END IF;

    v_action := p_request ->> 'action';
    SELECT pg_catalog.array_agg(key ORDER BY key)
    INTO v_keys
    FROM pg_catalog.jsonb_object_keys(p_request) AS request_keys(key);

    IF v_action = 'lock' THEN
        v_expected_keys := ARRAY[
            'action','project_id','resume_frame','scene_id','schema','target_task_id','track_ids'
        ]::text[];
    ELSIF v_action = 'replay' THEN
        v_expected_keys := ARRAY[
            'action','actor_id','idempotency_key','request_digest','scene_id','schema'
        ]::text[];
    ELSIF v_action = 'apply' THEN
        v_expected_keys := ARRAY[
            'action','actor_id','after_intervals','command','created_track_attributes',
            'created_track_attributes_meta','created_track_id','expected_snapshot_token',
            'idempotency_key','preview','project_id','request_digest','scene_id','schema',
            'snapshot','target_task_id'
        ]::text[];
    ELSIF v_action = 'inspect' THEN
        v_expected_keys := ARRAY['action','operation_id','scene_id','schema']::text[];
    ELSIF v_action = 'list' THEN
        v_expected_keys := ARRAY[
            'action','committed_only','exclude_reverts','limit','scene_id','schema','track_id'
        ]::text[];
    ELSIF v_action = 'revert' THEN
        v_expected_keys := ARRAY[
            'action','actor_id','idempotency_key','operation_id','scene_id','schema'
        ]::text[];
    ELSE
        RAISE EXCEPTION 'Invalid Labeling SceneTrack command action'
            USING ERRCODE = '22023';
    END IF;

    IF v_keys IS DISTINCT FROM v_expected_keys THEN
        RAISE EXCEPTION 'Invalid Labeling SceneTrack command fields'
            USING ERRCODE = '22023';
    END IF;

    IF v_action = 'lock' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'scene_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'track_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(p_request -> 'resume_frame') NOT IN ('null','number')
           OR pg_catalog.jsonb_typeof(p_request -> 'target_task_id') NOT IN ('null','string')
           OR EXISTS (
               SELECT 1
               FROM pg_catalog.jsonb_array_elements(p_request -> 'track_ids') AS item(value)
               WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                  OR item.value #>> '{}' = ''
                  OR pg_catalog.length(item.value #>> '{}') > 64
           ) THEN
            RAISE EXCEPTION 'Invalid Labeling SceneTrack lock request'
                USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_scene_id := (p_request ->> 'scene_id')::uuid;
        v_resume_frame := NULLIF(p_request ->> 'resume_frame', '')::integer;
        v_target_task_id := NULLIF(p_request ->> 'target_task_id', '')::uuid;
        SELECT pg_catalog.array_agg(value ORDER BY value)
        INTO v_track_ids
        FROM (
            SELECT DISTINCT item.value #>> '{}' AS value
            FROM pg_catalog.jsonb_array_elements(p_request -> 'track_ids') AS item(value)
        ) AS requested_tracks;
        IF v_track_ids IS NULL OR pg_catalog.cardinality(v_track_ids) NOT BETWEEN 1 AND 2 THEN
            RAISE EXCEPTION 'Invalid Labeling SceneTrack lock track count'
                USING ERRCODE = '22023';
        END IF;

        BEGIN
            FOR v_row IN
                SELECT task.id
                FROM public.tasks AS task
                WHERE task.id IN (
                    SELECT annotation.task_id
                    FROM public.annotations AS annotation
                    JOIN public.scene_tracks AS track
                      ON track.id = annotation.scene_track_id
                    WHERE track.project_id = v_project_id
                      AND track.scene_id = v_scene_id
                      AND track.track_id = ANY(v_track_ids)
                    UNION
                    SELECT v_target_task_id
                    WHERE v_target_task_id IS NOT NULL
                )
                ORDER BY task.id
                FOR UPDATE OF task NOWAIT
            LOOP
                NULL;
            END LOOP;
        EXCEPTION
            WHEN lock_not_available OR deadlock_detected THEN
                v_error_status := 409;
                v_error_reason := 'task_evidence_busy';
                v_error_message := 'task is being updated; refresh and retry';
        END;

        IF v_error_reason IS NULL AND v_resume_frame IS NOT NULL THEN
            SELECT EXISTS (
                SELECT 1
                FROM public.tasks AS task
                LEFT JOIN aap_assets_v1.spatial_item_references AS direct_item
                  ON direct_item.item_id = task.dataset_item_id
                WHERE task.id = v_target_task_id
                  AND direct_item.scene_id = v_scene_id
                  AND direct_item.frame_index = v_resume_frame
                UNION ALL
                SELECT 1
                FROM public.task_dataset_item_links AS link
                JOIN public.tasks AS task ON task.id = link.task_id
                JOIN aap_assets_v1.spatial_item_references AS linked_item
                  ON linked_item.item_id = link.dataset_item_id
                WHERE link.task_id = v_target_task_id
                  AND link.role = 'primary_lidar'
                  AND task.dataset_item_id IS NULL
                  AND linked_item.scene_id = v_scene_id
                  AND linked_item.frame_index = v_resume_frame
            ) INTO v_target_valid;
            IF v_target_task_id IS NULL OR NOT COALESCE(v_target_valid, false) THEN
                v_error_status := 422;
                v_error_reason := 'resume_frame_unavailable';
                v_error_message := 'resume frame does not have a task in this Scene';
        END IF;

        END IF;

        IF v_error_reason IS NULL AND EXISTS (
            SELECT 1
            FROM public.annotations AS annotation
            JOIN public.scene_tracks AS track
              ON track.id = annotation.scene_track_id
            JOIN public.tasks AS task ON task.id = annotation.task_id
            LEFT JOIN aap_assets_v1.spatial_item_references AS direct_item
              ON direct_item.item_id = task.dataset_item_id
            LEFT JOIN LATERAL (
                SELECT item.scene_id, item.frame_index
                FROM public.task_dataset_item_links AS link
                JOIN public.tasks AS linked_task ON linked_task.id = link.task_id
                JOIN aap_assets_v1.spatial_item_references AS item
                  ON item.item_id = link.dataset_item_id
                WHERE link.task_id = task.id
                  AND link.role = 'primary_lidar'
                  AND linked_task.dataset_item_id IS NULL
                LIMIT 1
            ) AS linked_item ON true
            WHERE track.project_id = v_project_id
              AND track.scene_id = v_scene_id
              AND track.track_id = ANY(v_track_ids)
              AND annotation.was_cancelled IS FALSE
              AND (
                  CASE WHEN task.dataset_item_id IS NOT NULL THEN direct_item.scene_id
                       ELSE linked_item.scene_id END IS DISTINCT FROM v_scene_id
                  OR CASE WHEN task.dataset_item_id IS NOT NULL THEN direct_item.frame_index
                          ELSE linked_item.frame_index END IS NULL
              )
        ) THEN
            v_error_status := 409;
            v_error_reason := 'track_crosses_scene';
            v_error_message := 'Scene Track contains a member outside the current Scene';
        END IF;

        IF v_error_reason IS NULL THEN
            FOR v_row IN
                SELECT track.id
                FROM public.scene_tracks AS track
                WHERE track.project_id = v_project_id
                  AND track.scene_id = v_scene_id
                  AND track.track_id = ANY(v_track_ids)
                ORDER BY track.track_id
                FOR UPDATE OF track
            LOOP
                NULL;
            END LOOP;

            FOR v_row IN
                SELECT interval.id
                FROM public.scene_track_intervals AS interval
                JOIN public.scene_tracks AS track
                  ON track.id = interval.scene_track_id
                WHERE track.project_id = v_project_id
                  AND track.scene_id = v_scene_id
                  AND track.track_id = ANY(v_track_ids)
                ORDER BY track.track_id, interval.start_frame, interval.id
                FOR UPDATE OF interval
            LOOP
                NULL;
            END LOOP;

            FOR v_row IN
                SELECT annotation.id
                FROM public.annotations AS annotation
                JOIN public.scene_tracks AS track
                  ON track.id = annotation.scene_track_id
                WHERE track.project_id = v_project_id
                  AND track.scene_id = v_scene_id
                  AND track.track_id = ANY(v_track_ids)
                  AND annotation.was_cancelled IS FALSE
                ORDER BY annotation.id
                FOR UPDATE OF annotation
            LOOP
                NULL;
            END LOOP;
            v_data := pg_catalog.jsonb_build_object('locked', true);
        END IF;

    ELSIF v_action = 'replay' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'scene_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'actor_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'idempotency_key') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'request_digest') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid Labeling SceneTrack replay request'
                USING ERRCODE = '22023';
        END IF;
        v_scene_id := (p_request ->> 'scene_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        v_idempotency_key := p_request ->> 'idempotency_key';
        v_request_digest := p_request ->> 'request_digest';
        IF pg_catalog.length(v_idempotency_key) NOT BETWEEN 8 AND 128
           OR v_request_digest !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION 'Invalid Labeling SceneTrack replay key or digest'
                USING ERRCODE = '22023';
        END IF;
        PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
            'aap:scene-track-command:' || v_scene_id::text || ':' ||
            v_actor_id::text || ':' || v_idempotency_key, 0
        ));
        SELECT * INTO v_operation
        FROM public.scene_track_operations AS operation
        WHERE operation.scene_id = v_scene_id
          AND operation.actor_id = v_actor_id
          AND operation.idempotency_key = v_idempotency_key;
        IF FOUND AND v_operation.request_digest IS DISTINCT FROM v_request_digest THEN
            v_error_status := 409;
            v_error_reason := 'idempotency_key_reused';
            v_error_message := 'idempotency key was already used for a different command';
        ELSE
            v_data := pg_catalog.jsonb_build_object(
                'found', FOUND,
                'request_digest', CASE WHEN FOUND THEN v_operation.request_digest ELSE NULL END,
                'response_json', CASE WHEN FOUND THEN v_operation.response_json ELSE NULL END
            );
        END IF;

    ELSIF v_action = 'inspect' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'scene_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'operation_id') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid Labeling SceneTrack inspect request'
                USING ERRCODE = '22023';
        END IF;
        v_scene_id := (p_request ->> 'scene_id')::uuid;
        v_operation_id := (p_request ->> 'operation_id')::uuid;
        SELECT * INTO v_operation
        FROM public.scene_track_operations AS operation
        WHERE operation.id = v_operation_id
          AND operation.scene_id = v_scene_id;
        IF NOT FOUND THEN
            v_error_status := 404;
            v_error_reason := 'operation_not_found';
            v_error_message := 'Scene Track operation was not found';
        ELSE
            SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(task_id) ORDER BY task_id), '[]'::jsonb)
            INTO v_task_ids
            FROM (
                SELECT DISTINCT (member.value ->> 'task_id')::uuid AS task_id
                FROM (
                    SELECT v_operation.before_state AS state
                    UNION ALL
                    SELECT v_operation.after_state AS state
                ) AS snapshots
                CROSS JOIN LATERAL pg_catalog.jsonb_each(snapshots.state -> 'tracks') AS track(key, value)
                CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(track.value -> 'members') AS member(value)
                WHERE member.value ? 'task_id'
            ) AS operation_tasks;
            v_data := pg_catalog.jsonb_build_object(
                'operation', pg_catalog.jsonb_build_object(
                    'id', v_operation.id,
                    'kind', v_operation.kind,
                    'status', v_operation.status,
                    'created_at', v_operation.created_at,
                    'completed_at', v_operation.completed_at,
                    'response_json', v_operation.response_json,
                    'task_ids', COALESCE(v_task_ids, '[]'::jsonb)
                ),
                'before_state', v_operation.before_state,
                'after_state', v_operation.after_state,
                'task_ids', COALESCE(v_task_ids, '[]'::jsonb)
            );
        END IF;

    ELSIF v_action = 'list' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'scene_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'track_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'limit') IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(p_request -> 'committed_only') IS DISTINCT FROM 'boolean'
           OR pg_catalog.jsonb_typeof(p_request -> 'exclude_reverts') IS DISTINCT FROM 'boolean'
           OR p_request ->> 'track_id' = '' OR pg_catalog.length(p_request ->> 'track_id') > 64
           OR (p_request ->> 'limit') !~ '^[0-9]+$'
           OR (p_request ->> 'limit')::integer NOT BETWEEN 1 AND 400 THEN
            RAISE EXCEPTION 'Invalid Labeling SceneTrack list request'
                USING ERRCODE = '22023';
        END IF;
        v_scene_id := (p_request ->> 'scene_id')::uuid;
        SELECT COALESCE(pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'id', listed.id,
                'kind', listed.kind,
                'status', listed.status,
                'created_at', listed.created_at,
                'completed_at', listed.completed_at,
                'response_json', listed.response_json,
                'task_ids', listed.task_ids
            ) ORDER BY listed.created_at DESC
        ), '[]'::jsonb)
        INTO v_operations
        FROM (
            SELECT operation.id, operation.kind, operation.status,
                   operation.created_at, operation.completed_at, operation.response_json,
                   COALESCE((
                       SELECT pg_catalog.jsonb_agg(pg_catalog.to_jsonb(task_id) ORDER BY task_id)
                       FROM (
                           SELECT DISTINCT (member.value ->> 'task_id')::uuid AS task_id
                           FROM (
                               SELECT operation.before_state AS state
                               UNION ALL
                               SELECT operation.after_state AS state
                           ) AS snapshots
                           CROSS JOIN LATERAL pg_catalog.jsonb_each(snapshots.state -> 'tracks') AS track(key, value)
                           CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(track.value -> 'members') AS member(value)
                           WHERE member.value ? 'task_id'
                       ) AS operation_tasks
                   ), '[]'::jsonb) AS task_ids
            FROM public.scene_track_operations AS operation
            WHERE operation.scene_id = v_scene_id
              AND (
                  operation.response_json ->> 'track_id' = p_request ->> 'track_id'
                  OR operation.response_json ->> 'secondary_track_id' = p_request ->> 'track_id'
                  OR operation.response_json ->> 'created_track_id' = p_request ->> 'track_id'
              )
              AND (
                  NOT (p_request ->> 'committed_only')::boolean
                  OR operation.status = 'committed'
              )
              AND (
                  NOT (p_request ->> 'exclude_reverts')::boolean
                  OR operation.kind <> 'revert'
              )
            ORDER BY operation.created_at DESC
            LIMIT (p_request ->> 'limit')::integer
        ) AS listed;
        v_data := pg_catalog.jsonb_build_object('operations', COALESCE(v_operations, '[]'::jsonb));

    ELSIF v_action = 'apply' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'scene_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'actor_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'idempotency_key') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'request_digest') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'expected_snapshot_token') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'command') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'preview') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'snapshot') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(p_request -> 'after_intervals') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(p_request -> 'created_track_attributes') IS NULL
           OR pg_catalog.jsonb_typeof(p_request -> 'created_track_attributes_meta') IS NULL
           OR pg_catalog.jsonb_typeof(p_request -> 'target_task_id') NOT IN ('null','string') THEN
            RAISE EXCEPTION 'Invalid Labeling SceneTrack apply request'
                USING ERRCODE = '22023';
        END IF;

        v_project_id := (p_request ->> 'project_id')::uuid;
        v_scene_id := (p_request ->> 'scene_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        v_idempotency_key := p_request ->> 'idempotency_key';
        v_request_digest := p_request ->> 'request_digest';
        v_expected_snapshot_token := p_request ->> 'expected_snapshot_token';
        v_snapshot_token := p_request #>> '{preview,snapshot_token}';
        v_kind := p_request #>> '{command,kind}';
        v_track_id := p_request #>> '{command,track_id}';
        v_secondary_track_id := NULLIF(p_request #>> '{command,secondary_track_id}', '');
        v_frame_index := NULLIF(p_request #>> '{command,frame_index}', '')::integer;
        v_resume_frame := NULLIF(p_request #>> '{command,resume_frame}', '')::integer;
        v_created_track_id := NULLIF(p_request ->> 'created_track_id', '');
        v_target_task_id := NULLIF(p_request ->> 'target_task_id', '')::uuid;
        v_source_revisions := p_request #> '{preview,source_revisions}';

        IF v_kind IS NULL OR v_kind NOT IN ('split','merge','mark_absent','resume','terminate')
           OR v_track_id IS NULL OR v_track_id = '' OR pg_catalog.length(v_track_id) > 64
           OR v_idempotency_key IS NULL OR pg_catalog.length(v_idempotency_key) NOT BETWEEN 8 AND 128
           OR v_request_digest IS NULL OR v_request_digest !~ '^[0-9a-f]{64}$'
           OR v_expected_snapshot_token IS NULL OR v_expected_snapshot_token !~ '^[0-9a-f]{64}$'
           OR v_snapshot_token IS NULL OR v_snapshot_token !~ '^[0-9a-f]{64}$'
           OR (v_kind = 'merge' AND (v_secondary_track_id IS NULL OR v_secondary_track_id = v_track_id))
           OR (v_kind <> 'merge' AND v_secondary_track_id IS NOT NULL)
           OR (v_kind IN ('split','mark_absent','terminate') AND v_frame_index IS NULL)
           OR (v_kind = 'resume' AND (
               v_resume_frame IS NULL OR p_request #>> '{command,source_annotation_id}' IS NULL
           ))
           OR p_request #>> '{preview,kind}' IS DISTINCT FROM v_kind
           OR p_request #>> '{preview,track_id}' IS DISTINCT FROM v_track_id
           OR NULLIF(p_request #>> '{preview,secondary_track_id}', '') IS DISTINCT FROM v_secondary_track_id
           OR NULLIF(p_request #>> '{preview,frame_index}', '')::integer IS DISTINCT FROM v_frame_index
           OR NULLIF(p_request #>> '{preview,resume_frame}', '')::integer IS DISTINCT FROM v_resume_frame
           OR (p_request #>> '{preview,scene_id}')::uuid IS DISTINCT FROM v_scene_id THEN
            RAISE EXCEPTION 'Invalid or inconsistent Labeling SceneTrack apply request'
                USING ERRCODE = '22023';
        END IF;

        PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
            'aap:scene-track-command:' || v_scene_id::text || ':' ||
            v_actor_id::text || ':' || v_idempotency_key, 0
        ));
        SELECT * INTO v_operation
        FROM public.scene_track_operations AS operation
        WHERE operation.scene_id = v_scene_id
          AND operation.actor_id = v_actor_id
          AND operation.idempotency_key = v_idempotency_key;
        IF FOUND THEN
            IF v_operation.request_digest IS DISTINCT FROM v_request_digest THEN
                v_error_status := 409;
                v_error_reason := 'idempotency_key_reused';
                v_error_message := 'idempotency key was already used for a different command';
            ELSE
                SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(task_id) ORDER BY task_id), '[]'::jsonb)
                INTO v_task_ids
                FROM (
                    SELECT DISTINCT (member.value ->> 'task_id')::uuid AS task_id
                    FROM (
                        SELECT v_operation.before_state AS state
                        UNION ALL
                        SELECT v_operation.after_state AS state
                    ) AS snapshots
                    CROSS JOIN LATERAL pg_catalog.jsonb_each(snapshots.state -> 'tracks') AS track(key, value)
                    CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(track.value -> 'members') AS member(value)
                    WHERE member.value ? 'task_id'
                ) AS operation_tasks;
                v_data := pg_catalog.jsonb_build_object(
                    'operation', pg_catalog.jsonb_build_object(
                        'id', v_operation.id, 'kind', v_operation.kind,
                        'status', v_operation.status, 'created_at', v_operation.created_at,
                        'completed_at', v_operation.completed_at,
                        'response_json', v_operation.response_json,
                        'task_ids', COALESCE(v_task_ids, '[]'::jsonb)
                    ),
                    'response_json', v_operation.response_json,
                    'task_ids', COALESCE(v_task_ids, '[]'::jsonb),
                    'before_state', v_operation.before_state,
                    'after_state', v_operation.after_state,
                    'replaced_track_ids', '[]'::jsonb,
                    'found', true
                );
            END IF;
        ELSE
            v_track_ids := ARRAY[v_track_id];
            IF v_secondary_track_id IS NOT NULL THEN
                v_track_ids := pg_catalog.array_append(v_track_ids, v_secondary_track_id);
            END IF;
            v_lock_reply := aap_labeling_v1.scene_track_command(
                pg_catalog.jsonb_build_object(
                    'schema', 'aap-labeling-scene-track-commands/v1',
                    'action', 'lock',
                    'project_id', v_project_id,
                    'scene_id', v_scene_id,
                    'track_ids', pg_catalog.to_jsonb(v_track_ids),
                    'resume_frame', v_resume_frame,
                    'target_task_id', v_target_task_id
                )
            );
            IF COALESCE((v_lock_reply ->> 'ok')::boolean, false) IS FALSE THEN
                v_error_status := (v_lock_reply #>> '{error,status_code}')::integer;
                v_error_reason := v_lock_reply #>> '{error,reason}';
                v_error_message := v_lock_reply #>> '{error,message}';
            ELSIF v_expected_snapshot_token IS DISTINCT FROM v_snapshot_token THEN
                v_error_status := 409;
                v_error_reason := 'track_snapshot_stale';
                v_error_message := 'Scene Track changed after preview; preview the command again';
            ELSIF COALESCE((p_request #>> '{preview,affected_members,requires_confirmation}')::boolean, false)
                  AND NOT COALESCE((p_request #>> '{command,confirm_member_deactivation}')::boolean, false) THEN
                v_error_status := 409;
                v_error_reason := 'member_deactivation_confirmation_required';
                v_error_message := 'keyframe or sample members require explicit confirmation';
            END IF;

            IF v_error_reason IS NULL THEN
                FOR v_entry IN
                    SELECT key, value
                    FROM pg_catalog.jsonb_each(v_source_revisions)
                LOOP
                    SELECT track.revision INTO v_actual_revision
                    FROM public.scene_tracks AS track
                    WHERE track.project_id = v_project_id
                      AND track.scene_id = v_scene_id
                      AND track.track_id = v_entry.key;
                    v_expected_revision := (v_entry.value #>> '{}')::integer;
                    IF NOT FOUND OR COALESCE(NULLIF(v_actual_revision, 0), 1) <> v_expected_revision THEN
                        v_error_status := 409;
                        v_error_reason := 'track_snapshot_stale';
                        v_error_message := 'Scene Track changed after preview; preview the command again';
                        EXIT;
                    END IF;
                END LOOP;
            END IF;

            IF v_error_reason IS NULL THEN
                SELECT COALESCE(pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_object(
                        'id', track.id::text,
                        'track_id', track.track_id,
                        'revision', CASE WHEN track.revision IS NULL OR track.revision = 0 THEN 1 ELSE track.revision END,
                        'presence_mode', track.presence_mode,
                        'retired_at', track.retired_at,
                        'intervals', COALESCE((
                            SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                                'id', interval.id::text,
                                'start_frame', interval.start_frame,
                                'end_frame', interval.end_frame,
                                'source', interval.source,
                                'version', CASE WHEN interval.version IS NULL OR interval.version = 0 THEN 1 ELSE interval.version END
                            ) ORDER BY interval.start_frame, interval.id)
                            FROM public.scene_track_intervals AS interval
                            WHERE interval.scene_track_id = track.id
                        ), '[]'::jsonb),
                        'members', COALESCE((
                            SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                                'id', member.id::text,
                                'version', CASE WHEN member.version IS NULL OR member.version = 0 THEN 1 ELSE member.version END,
                                'task_id', member.task_id::text,
                                'track_id', member.track_id,
                                'scene_track_id', member.scene_track_id::text,
                                'class_name', member.class_name,
                                'is_active', member.is_active,
                                'temporal_role', member.temporal_role,
                                'sensor_role', member.sensor_role
                            ) ORDER BY member.id)
                            FROM public.annotations AS member
                            WHERE member.scene_track_id = track.id
                              AND member.was_cancelled IS FALSE
                        ), '[]'::jsonb)
                    ) ORDER BY expected.ordinal
                ), '[]'::jsonb)
                INTO v_current_snapshot
                FROM pg_catalog.jsonb_array_elements(p_request -> 'snapshot')
                    WITH ORDINALITY AS expected(value, ordinal)
                LEFT JOIN public.scene_tracks AS track
                  ON track.project_id = v_project_id
                 AND track.scene_id = v_scene_id
                 AND track.track_id = expected.value ->> 'track_id';

                IF v_current_snapshot IS DISTINCT FROM p_request -> 'snapshot' THEN
                    v_error_status := 409;
                    v_error_reason := 'track_snapshot_stale';
                    v_error_message := 'Scene Track changed after preview; preview the command again';
                END IF;
            END IF;

            IF v_error_reason IS NULL THEN
                SELECT array_agg(DISTINCT expected.value ->> 'track_id' ORDER BY expected.value ->> 'track_id')
                INTO v_before_track_ids
                FROM pg_catalog.jsonb_array_elements(p_request -> 'snapshot') AS expected(value);
                v_after_track_ids := v_before_track_ids;
                IF v_kind = 'split' AND v_created_track_id IS NOT NULL THEN
                    v_after_track_ids := pg_catalog.array_append(v_after_track_ids, v_created_track_id);
                END IF;
                SELECT pg_catalog.array_agg(track_id ORDER BY track_id)
                INTO v_after_track_ids
                FROM pg_catalog.unnest(v_after_track_ids) AS track_ids(track_id);

                IF EXISTS (
                    SELECT 1
                    FROM public.scene_tracks AS track
                    WHERE track.project_id = v_project_id
                      AND track.scene_id = v_scene_id
                      AND track.track_id = ANY(v_before_track_ids)
                      AND track.retired_at IS NOT NULL
                ) THEN
                    v_error_status := 409;
                    v_error_reason := 'track_retired';
                    v_error_message := 'retired Scene Track cannot be changed';
                ELSIF EXISTS (
                    SELECT 1
                    FROM public.scene_tracks AS track
                    JOIN public.annotations AS member
                      ON member.scene_track_id = track.id
                    WHERE track.project_id = v_project_id
                      AND track.scene_id = v_scene_id
                      AND track.track_id = ANY(v_before_track_ids)
                      AND member.was_cancelled IS FALSE
                      AND (member.track_id IS DISTINCT FROM track.track_id
                           OR member.class_name IS DISTINCT FROM track.class_name)
                ) THEN
                    v_error_status := 409;
                    v_error_reason := 'member_identity_mismatch';
                    v_error_message := 'Scene Track member identity does not match its Track';
                ELSIF EXISTS (
                    SELECT 1
                    FROM public.scene_tracks AS track
                    JOIN public.annotations AS member
                      ON member.scene_track_id = track.id
                    WHERE track.project_id = v_project_id
                      AND track.scene_id = v_scene_id
                      AND track.track_id = ANY(v_before_track_ids)
                      AND member.was_cancelled IS FALSE
                      AND member.is_locked IS TRUE
                ) THEN
                    v_error_status := 409;
                    v_error_reason := 'annotation_locked';
                    v_error_message := 'unlock every Scene Track member before changing its lifecycle';
                ELSE
                    IF EXISTS (
                    SELECT 1
                    FROM public.scene_tracks AS track
                    JOIN public.annotations AS member
                      ON member.scene_track_id = track.id
                    WHERE track.project_id = v_project_id
                      AND track.scene_id = v_scene_id
                      AND track.track_id = ANY(v_before_track_ids)
                      AND member.was_cancelled IS FALSE
                    GROUP BY track.track_id
                    HAVING count(*) > 5000
                    ) THEN
                        v_error_status := 422;
                        v_error_reason := 'track_member_limit_exceeded';
                        v_error_message := 'Scene Track command cannot exceed 5000 members';
                    END IF;
                END IF;
            END IF;

            IF v_error_reason IS NULL THEN
                SELECT pg_catalog.array_agg(DISTINCT expected.value ->> 'track_id' ORDER BY expected.value ->> 'track_id')
                INTO v_track_ids
                FROM pg_catalog.jsonb_array_elements(p_request -> 'after_intervals') AS expected(value);
                IF v_track_ids IS DISTINCT FROM v_after_track_ids
                   OR EXISTS (
                       SELECT 1
                       FROM pg_catalog.jsonb_array_elements(p_request -> 'after_intervals') AS sets(value)
                       WHERE pg_catalog.jsonb_typeof(sets.value -> 'intervals') IS DISTINCT FROM 'array'
                   ) THEN
                    v_error_status := 422;
                    v_error_reason := 'interval_invalid';
                    v_error_message := 'Scene Track interval is invalid';
                END IF;
            END IF;

            IF v_error_reason IS NULL THEN
                SELECT pg_catalog.jsonb_build_object('tracks', COALESCE(pg_catalog.jsonb_object_agg(
                    track.track_id,
                    pg_catalog.jsonb_build_object(
                        'id', track.id::text,
                        'track_id', track.track_id,
                        'class_name', track.class_name,
                        'presence_mode', track.presence_mode,
                        'attributes', CASE WHEN track.attributes IS NULL OR track.attributes IN ('null'::jsonb, '{}'::jsonb, '[]'::jsonb, '""'::jsonb, '0'::jsonb, 'false'::jsonb) THEN '{}'::jsonb ELSE track.attributes END,
                        'attributes_meta', CASE WHEN track.attributes_meta IS NULL OR track.attributes_meta IN ('null'::jsonb, '{}'::jsonb, '[]'::jsonb, '""'::jsonb, '0'::jsonb, 'false'::jsonb) THEN '{}'::jsonb ELSE track.attributes_meta END,
                        'revision', CASE WHEN track.revision IS NULL OR track.revision = 0 THEN 1 ELSE track.revision END,
                        'retired_at', CASE WHEN track.retired_at IS NULL THEN NULL ELSE pg_catalog.to_jsonb(track.retired_at) END,
                        'intervals', COALESCE((
                            SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                                'id', interval.id::text,
                                'start_frame', interval.start_frame,
                                'end_frame', interval.end_frame,
                                'source', interval.source,
                                'version', CASE WHEN interval.version IS NULL OR interval.version = 0 THEN 1 ELSE interval.version END
                            ) ORDER BY interval.start_frame, interval.id)
                            FROM public.scene_track_intervals AS interval
                            WHERE interval.scene_track_id = track.id
                        ), '[]'::jsonb),
                        'members', COALESCE((
                            SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                                'id', member.id::text,
                                'task_id', member.task_id::text,
                                'track_id', member.track_id,
                                'scene_track_id', member.scene_track_id::text,
                                'class_name', member.class_name,
                                'is_active', member.is_active,
                                'is_hidden', member.is_hidden,
                                'temporal_role', member.temporal_role,
                                'sensor_role', member.sensor_role,
                                'version', CASE WHEN member.version IS NULL OR member.version = 0 THEN 1 ELSE member.version END
                            ) ORDER BY member.id)
                            FROM public.annotations AS member
                            WHERE member.scene_track_id = track.id
                              AND member.was_cancelled IS FALSE
                        ), '[]'::jsonb)
                    ) ORDER BY track.track_id
                ), '{}'::jsonb))
                INTO v_before_state
                FROM public.scene_tracks AS track
                WHERE track.project_id = v_project_id
                  AND track.scene_id = v_scene_id
                  AND track.track_id = ANY(v_before_track_ids);

                v_operation_id := pg_catalog.gen_random_uuid();
                INSERT INTO public.scene_track_operations (
                    id, scene_id, actor_id, kind, idempotency_key, request_digest,
                    snapshot_token, source_revisions, result_revisions, before_state,
                    after_state, inverse_payload, response_json, status
                ) VALUES (
                    v_operation_id, v_scene_id, v_actor_id, v_kind, v_idempotency_key,
                    v_request_digest, v_snapshot_token, v_source_revisions, '{}'::jsonb,
                    v_before_state, '{}'::jsonb,
                    pg_catalog.jsonb_build_object('restore', v_before_state),
                    '{}'::jsonb, 'committed'
                );

                SELECT * INTO v_primary
                FROM public.scene_tracks AS track
                WHERE track.project_id = v_project_id
                  AND track.scene_id = v_scene_id
                  AND track.track_id = v_track_id;
                v_primary_id := v_primary.id;
                IF v_secondary_track_id IS NOT NULL THEN
                    SELECT * INTO v_secondary
                    FROM public.scene_tracks AS track
                    WHERE track.project_id = v_project_id
                      AND track.scene_id = v_scene_id
                      AND track.track_id = v_secondary_track_id;
                    v_secondary_id := v_secondary.id;
                END IF;

                IF v_kind = 'split' THEN
                    IF v_created_track_id IS NULL THEN
                        RAISE EXCEPTION 'Split command is missing its created Track ID'
                            USING ERRCODE = '22023';
                    END IF;
                    INSERT INTO public.scene_tracks (
                        id, project_id, scene_id, track_id, class_name, presence_mode,
                        attributes, attributes_meta, revision, created_by
                    ) VALUES (
                        pg_catalog.gen_random_uuid(), v_primary.project_id, v_primary.scene_id,
                        v_created_track_id, v_primary.class_name, 'explicit',
                        p_request -> 'created_track_attributes',
                        p_request -> 'created_track_attributes_meta', 1, v_actor_id
                    ) RETURNING id INTO v_created_track_uuid;
                    UPDATE public.annotations AS member
                    SET track_id = v_created_track_id,
                        scene_track_id = v_created_track_uuid,
                        version = (CASE WHEN member.version IS NULL OR member.version = 0 THEN 1 ELSE member.version END) + 1,
                        updated_at = pg_catalog.transaction_timestamp()
                    FROM public.tasks AS task
                    LEFT JOIN aap_assets_v1.spatial_item_references AS direct_item
                      ON direct_item.item_id = task.dataset_item_id
                    LEFT JOIN LATERAL (
                        SELECT item.scene_id, item.frame_index
                        FROM public.task_dataset_item_links AS link
                        JOIN aap_assets_v1.spatial_item_references AS item
                          ON item.item_id = link.dataset_item_id
                        WHERE link.task_id = task.id
                          AND link.role = 'primary_lidar'
                          AND task.dataset_item_id IS NULL
                        LIMIT 1
                    ) AS linked_item ON true
                    WHERE member.task_id = task.id
                      AND member.scene_track_id = v_primary_id
                      AND member.was_cancelled IS FALSE
                      AND CASE WHEN task.dataset_item_id IS NOT NULL THEN direct_item.frame_index
                               ELSE linked_item.frame_index END > v_frame_index;
                    UPDATE public.annotations AS member
                    SET version = (CASE WHEN member.version IS NULL OR member.version = 0 THEN 1 ELSE member.version END) + 1,
                        updated_at = pg_catalog.transaction_timestamp()
                    FROM public.tasks AS task
                    LEFT JOIN aap_assets_v1.spatial_item_references AS direct_item
                      ON direct_item.item_id = task.dataset_item_id
                    LEFT JOIN LATERAL (
                        SELECT item.scene_id, item.frame_index
                        FROM public.task_dataset_item_links AS link
                        JOIN aap_assets_v1.spatial_item_references AS item
                          ON item.item_id = link.dataset_item_id
                        WHERE link.task_id = task.id
                          AND link.role = 'primary_lidar'
                          AND task.dataset_item_id IS NULL
                        LIMIT 1
                    ) AS linked_item ON true
                    WHERE member.task_id = task.id
                      AND member.scene_track_id = v_primary_id
                      AND member.was_cancelled IS FALSE
                      AND CASE WHEN task.dataset_item_id IS NOT NULL THEN direct_item.frame_index
                               ELSE linked_item.frame_index END <= v_frame_index;
                    UPDATE public.scene_tracks AS track
                    SET revision = (CASE WHEN track.revision IS NULL OR track.revision = 0 THEN 1 ELSE track.revision END) + 1,
                        presence_mode = 'explicit',
                        updated_at = pg_catalog.transaction_timestamp()
                    WHERE track.id = v_primary_id;

                ELSIF v_kind = 'merge' THEN
                    UPDATE public.annotations AS member
                    SET version = (CASE WHEN member.version IS NULL OR member.version = 0 THEN 1 ELSE member.version END) + 1,
                        updated_at = pg_catalog.transaction_timestamp()
                    WHERE member.scene_track_id = v_primary_id
                      AND member.was_cancelled IS FALSE;
                    UPDATE public.annotations AS member
                    SET track_id = v_primary.track_id,
                        scene_track_id = v_primary_id,
                        version = (CASE WHEN member.version IS NULL OR member.version = 0 THEN 1 ELSE member.version END) + 1,
                        updated_at = pg_catalog.transaction_timestamp()
                    WHERE member.scene_track_id = v_secondary_id
                      AND member.was_cancelled IS FALSE;
                    UPDATE public.scene_tracks AS track
                    SET revision = (CASE WHEN track.revision IS NULL OR track.revision = 0 THEN 1 ELSE track.revision END) + 1,
                        presence_mode = 'explicit',
                        updated_at = pg_catalog.transaction_timestamp()
                    WHERE track.id = v_primary_id;
                    UPDATE public.scene_tracks AS track
                    SET revision = (CASE WHEN track.revision IS NULL OR track.revision = 0 THEN 1 ELSE track.revision END) + 1,
                        presence_mode = 'explicit',
                        retired_at = pg_catalog.clock_timestamp(),
                        updated_at = pg_catalog.transaction_timestamp()
                    WHERE track.id = v_secondary_id;

                ELSIF v_kind IN ('mark_absent','terminate') THEN
                    UPDATE public.annotations AS member
                    SET is_active = false,
                        is_hidden = true,
                        version = (CASE WHEN member.version IS NULL OR member.version = 0 THEN 1 ELSE member.version END) + 1,
                        updated_at = pg_catalog.transaction_timestamp()
                    FROM public.tasks AS task
                    LEFT JOIN aap_assets_v1.spatial_item_references AS direct_item
                      ON direct_item.item_id = task.dataset_item_id
                    LEFT JOIN LATERAL (
                        SELECT item.scene_id, item.frame_index
                        FROM public.task_dataset_item_links AS link
                        JOIN aap_assets_v1.spatial_item_references AS item
                          ON item.item_id = link.dataset_item_id
                        WHERE link.task_id = task.id
                          AND link.role = 'primary_lidar'
                          AND task.dataset_item_id IS NULL
                        LIMIT 1
                    ) AS linked_item ON true
                    WHERE member.task_id = task.id
                      AND member.scene_track_id = v_primary_id
                      AND member.was_cancelled IS FALSE
                      AND member.is_active IS TRUE
                      AND (
                          (v_kind = 'terminate'
                           AND CASE WHEN task.dataset_item_id IS NOT NULL THEN direct_item.frame_index
                                    ELSE linked_item.frame_index END > v_frame_index)
                          OR
                          (v_kind = 'mark_absent'
                           AND CASE WHEN task.dataset_item_id IS NOT NULL THEN direct_item.frame_index
                                    ELSE linked_item.frame_index END >= v_frame_index
                           AND (v_resume_frame IS NULL OR CASE WHEN task.dataset_item_id IS NOT NULL THEN direct_item.frame_index
                                                              ELSE linked_item.frame_index END < v_resume_frame))
                      );
                    UPDATE public.scene_tracks AS track
                    SET revision = (CASE WHEN track.revision IS NULL OR track.revision = 0 THEN 1 ELSE track.revision END) + 1,
                        presence_mode = 'explicit',
                        updated_at = pg_catalog.transaction_timestamp()
                    WHERE track.id = v_primary_id;

                ELSE
                    v_target_task_id := (p_request ->> 'target_task_id')::uuid;
                    IF v_target_task_id IS NULL THEN
                        RAISE EXCEPTION 'Resume command is missing its target Task'
                            USING ERRCODE = '22023';
                    END IF;
                    INSERT INTO public.annotations (
                        id, task_id, project_id, user_id, source, annotation_type,
                        tool_unit_id, class_name, geometry, track_id, scene_track_id,
                        temporal_role, attributes, attributes_meta, z_order, is_active,
                        is_hidden, was_cancelled, ground_truth, is_locked, version
                    )
                    SELECT pg_catalog.gen_random_uuid(), v_target_task_id,
                           source.project_id, v_actor_id, 'manual', source.annotation_type,
                           source.tool_unit_id, source.class_name, source.geometry,
                           source.track_id, source.scene_track_id, 'keyframe',
                           CASE WHEN source.attributes IS NULL OR source.attributes IN ('null'::jsonb, '{}'::jsonb, '[]'::jsonb, '""'::jsonb, '0'::jsonb, 'false'::jsonb) THEN '{}'::jsonb ELSE source.attributes END,
                           CASE WHEN source.attributes_meta IS NULL OR source.attributes_meta IN ('null'::jsonb, '{}'::jsonb, '[]'::jsonb, '""'::jsonb, '0'::jsonb, 'false'::jsonb) THEN '{}'::jsonb ELSE source.attributes_meta END,
                           source.z_order, true, false, false, false, false, 1
                    FROM public.annotations AS source
                    WHERE source.id = (p_request #>> '{command,source_annotation_id}')::uuid;
                    UPDATE public.scene_tracks AS track
                    SET revision = (CASE WHEN track.revision IS NULL OR track.revision = 0 THEN 1 ELSE track.revision END) + 1,
                        presence_mode = 'explicit',
                        updated_at = pg_catalog.transaction_timestamp()
                    WHERE track.id = v_primary_id;
                END IF;

                FOR v_interval_set IN
                    SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'after_intervals') AS sets(value)
                LOOP
                    SELECT track.id INTO v_interval_track_id
                    FROM public.scene_tracks AS track
                    WHERE track.project_id = v_project_id
                      AND track.scene_id = v_scene_id
                      AND track.track_id = v_interval_set ->> 'track_id';
                    IF FOUND THEN
                        v_replaced_track_ids := pg_catalog.array_append(v_replaced_track_ids, v_interval_track_id);
                        IF EXISTS (
                            SELECT 1 FROM public.scene_track_intervals AS interval
                            WHERE interval.scene_track_id = v_interval_track_id
                        ) THEN
                            DELETE FROM public.scene_track_intervals AS interval
                            WHERE interval.scene_track_id = v_interval_track_id;
                        END IF;
                        IF pg_catalog.jsonb_array_length(v_interval_set -> 'intervals') > 0 THEN
                            INSERT INTO public.scene_track_intervals (
                                id, scene_track_id, start_frame, end_frame, source,
                                version, created_by, operation_id
                            )
                            SELECT pg_catalog.gen_random_uuid(), v_interval_track_id,
                                   (spec.value ->> 'start_frame')::integer,
                                   NULLIF(spec.value ->> 'end_frame', '')::integer,
                                   spec.value ->> 'source', 1, v_actor_id, v_operation_id
                            FROM pg_catalog.jsonb_array_elements(v_interval_set -> 'intervals')
                                WITH ORDINALITY AS spec(value, ordinal)
                            ORDER BY spec.ordinal;
                        END IF;
                    END IF;
                END LOOP;

                SELECT COALESCE(pg_catalog.jsonb_object_agg(
                    track.track_id,
                    CASE WHEN track.revision IS NULL OR track.revision = 0 THEN 1 ELSE track.revision END
                ), '{}'::jsonb)
                INTO v_result_revisions
                FROM public.scene_tracks AS track
                WHERE track.project_id = v_project_id
                  AND track.scene_id = v_scene_id
                  AND track.track_id = ANY(v_after_track_ids);

                SELECT pg_catalog.jsonb_build_object('tracks', COALESCE(pg_catalog.jsonb_object_agg(
                    track.track_id,
                    pg_catalog.jsonb_build_object(
                        'id', track.id::text,
                        'track_id', track.track_id,
                        'class_name', track.class_name,
                        'presence_mode', track.presence_mode,
                        'attributes', CASE WHEN track.attributes IS NULL OR track.attributes IN ('null'::jsonb, '{}'::jsonb, '[]'::jsonb, '""'::jsonb, '0'::jsonb, 'false'::jsonb) THEN '{}'::jsonb ELSE track.attributes END,
                        'attributes_meta', CASE WHEN track.attributes_meta IS NULL OR track.attributes_meta IN ('null'::jsonb, '{}'::jsonb, '[]'::jsonb, '""'::jsonb, '0'::jsonb, 'false'::jsonb) THEN '{}'::jsonb ELSE track.attributes_meta END,
                        'revision', CASE WHEN track.revision IS NULL OR track.revision = 0 THEN 1 ELSE track.revision END,
                        'retired_at', CASE WHEN track.retired_at IS NULL THEN NULL ELSE pg_catalog.to_jsonb(track.retired_at) END,
                        'intervals', COALESCE((
                            SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                                'id', interval.id::text,
                                'start_frame', interval.start_frame,
                                'end_frame', interval.end_frame,
                                'source', interval.source,
                                'version', CASE WHEN interval.version IS NULL OR interval.version = 0 THEN 1 ELSE interval.version END
                            ) ORDER BY interval.start_frame, interval.id)
                            FROM public.scene_track_intervals AS interval
                            WHERE interval.scene_track_id = track.id
                        ), '[]'::jsonb),
                        'members', COALESCE((
                            SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                                'id', member.id::text,
                                'task_id', member.task_id::text,
                                'track_id', member.track_id,
                                'scene_track_id', member.scene_track_id::text,
                                'class_name', member.class_name,
                                'is_active', member.is_active,
                                'is_hidden', member.is_hidden,
                                'temporal_role', member.temporal_role,
                                'sensor_role', member.sensor_role,
                                'version', CASE WHEN member.version IS NULL OR member.version = 0 THEN 1 ELSE member.version END
                            ) ORDER BY member.id)
                            FROM public.annotations AS member
                            WHERE member.scene_track_id = track.id
                              AND member.was_cancelled IS FALSE
                        ), '[]'::jsonb)
                    ) ORDER BY track.track_id
                ), '{}'::jsonb))
                INTO v_after_state
                FROM public.scene_tracks AS track
                WHERE track.project_id = v_project_id
                  AND track.scene_id = v_scene_id
                  AND track.track_id = ANY(v_after_track_ids);

                v_response := (p_request -> 'preview') || pg_catalog.jsonb_build_object(
                    'contract_version', 1,
                    'operation_id', v_operation_id::text,
                    'status', 'committed',
                    'created_track_id', v_created_track_id,
                    'result_revisions', v_result_revisions
                );
                UPDATE public.scene_track_operations AS operation
                SET result_revisions = v_result_revisions,
                    after_state = v_after_state,
                    response_json = v_response
                WHERE operation.id = v_operation_id
                RETURNING * INTO v_operation;

                SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(task_id) ORDER BY task_id), '[]'::jsonb)
                INTO v_task_ids
                FROM (
                    SELECT DISTINCT (member.value ->> 'task_id')::uuid AS task_id
                    FROM (
                        SELECT v_before_state AS state
                        UNION ALL
                        SELECT v_after_state AS state
                    ) AS snapshots
                    CROSS JOIN LATERAL pg_catalog.jsonb_each(snapshots.state -> 'tracks') AS track(key, value)
                    CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(track.value -> 'members') AS member(value)
                    WHERE member.value ? 'task_id'
                ) AS operation_tasks;
                v_data := pg_catalog.jsonb_build_object(
                    'operation', pg_catalog.jsonb_build_object(
                        'id', v_operation.id, 'kind', v_operation.kind,
                        'status', v_operation.status, 'created_at', v_operation.created_at,
                        'completed_at', v_operation.completed_at,
                        'response_json', v_operation.response_json,
                        'task_ids', COALESCE(v_task_ids, '[]'::jsonb)
                    ),
                    'response_json', v_response,
                    'task_ids', COALESCE(v_task_ids, '[]'::jsonb),
                    'before_state', v_before_state,
                    'after_state', v_after_state,
                    'replaced_track_ids', pg_catalog.to_jsonb(v_replaced_track_ids),
                    'found', false
                );
            END IF;
        END IF;

    ELSIF v_action = 'revert' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'scene_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'operation_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'actor_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request -> 'idempotency_key') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid Labeling SceneTrack revert request'
                USING ERRCODE = '22023';
        END IF;
        v_scene_id := (p_request ->> 'scene_id')::uuid;
        v_operation_id := (p_request ->> 'operation_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        v_idempotency_key := p_request ->> 'idempotency_key';
        IF pg_catalog.length(v_idempotency_key) NOT BETWEEN 8 AND 128 THEN
            RAISE EXCEPTION 'Invalid Labeling SceneTrack revert idempotency key'
                USING ERRCODE = '22023';
        END IF;
        PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
            'aap:scene-track-command:' || v_scene_id::text || ':' ||
            v_actor_id::text || ':' || v_idempotency_key, 0
        ));
        SELECT * INTO v_operation
        FROM public.scene_track_operations AS operation
        WHERE operation.scene_id = v_scene_id
          AND operation.actor_id = v_actor_id
          AND operation.idempotency_key = v_idempotency_key;
        IF FOUND THEN
            IF v_operation.kind <> 'revert'
               OR v_operation.request_digest IS DISTINCT FROM v_operation_id::text THEN
                v_error_status := 409;
                v_error_reason := 'idempotency_key_reused';
                v_error_message := 'idempotency key was already used for a different command';
            ELSE
                SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(task_id) ORDER BY task_id), '[]'::jsonb)
                INTO v_task_ids
                FROM (
                    SELECT DISTINCT (member.value ->> 'task_id')::uuid AS task_id
                    FROM (
                        SELECT v_operation.before_state AS state
                        UNION ALL
                        SELECT v_operation.after_state AS state
                    ) AS snapshots
                    CROSS JOIN LATERAL pg_catalog.jsonb_each(snapshots.state -> 'tracks') AS track(key, value)
                    CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(track.value -> 'members') AS member(value)
                    WHERE member.value ? 'task_id'
                ) AS operation_tasks;
                v_data := pg_catalog.jsonb_build_object(
                    'operation', pg_catalog.jsonb_build_object(
                        'id', v_operation.id, 'kind', v_operation.kind,
                        'status', v_operation.status, 'created_at', v_operation.created_at,
                        'completed_at', v_operation.completed_at,
                        'response_json', v_operation.response_json,
                        'task_ids', COALESCE(v_task_ids, '[]'::jsonb)
                    ),
                    'response_json', v_operation.response_json,
                    'task_ids', COALESCE(v_task_ids, '[]'::jsonb),
                    'before_state', v_operation.before_state,
                    'after_state', v_operation.after_state,
                    'replaced_track_ids', '[]'::jsonb,
                    'found', true
                );
            END IF;
        ELSE
            SELECT * INTO v_operation
            FROM public.scene_track_operations AS operation
            WHERE operation.id = v_operation_id
              AND operation.scene_id = v_scene_id;
            IF NOT FOUND THEN
                v_error_status := 404;
                v_error_reason := 'operation_not_found';
                v_error_message := 'Scene Track operation was not found';
            ELSIF v_operation.kind = 'revert' THEN
                v_error_status := 409;
                v_error_reason := 'revert_of_revert_unsupported';
                v_error_message := 'revert operations cannot be reverted';
            ELSIF v_operation.status <> 'committed' THEN
                v_error_status := 409;
                v_error_reason := 'operation_already_reverted';
                v_error_message := 'operation was already reverted';
            END IF;

            IF v_error_reason IS NULL THEN
                SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object('id', task_id) ORDER BY task_id), '[]'::jsonb)
                INTO v_task_ids
                FROM (
                    SELECT DISTINCT (member.value ->> 'task_id')::uuid AS task_id
                    FROM (
                        SELECT v_operation.before_state AS state
                        UNION ALL
                        SELECT v_operation.after_state AS state
                    ) AS snapshots
                    CROSS JOIN LATERAL pg_catalog.jsonb_each(snapshots.state -> 'tracks') AS track(key, value)
                    CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(track.value -> 'members') AS member(value)
                    WHERE member.value ? 'task_id'
                ) AS operation_tasks;

                BEGIN
                    FOR v_row IN
                        SELECT task.id
                        FROM public.tasks AS task
                        WHERE task.id IN (
                            SELECT (item.value ->> 'id')::uuid
                            FROM pg_catalog.jsonb_array_elements(COALESCE(v_task_ids, '[]'::jsonb)) AS item(value)
                        )
                        ORDER BY task.id
                        FOR UPDATE OF task NOWAIT
                    LOOP
                        NULL;
                    END LOOP;
                EXCEPTION
                    WHEN lock_not_available OR deadlock_detected THEN
                        v_error_status := 409;
                        v_error_reason := 'task_evidence_busy';
                        v_error_message := 'task is being updated; refresh and retry';
                END;
            END IF;

            IF v_error_reason IS NULL THEN
                SELECT * INTO v_operation
                FROM public.scene_track_operations AS operation
                WHERE operation.id = v_operation_id
                  AND operation.scene_id = v_scene_id
                FOR UPDATE;
                IF NOT FOUND OR v_operation.status <> 'committed' THEN
                    v_error_status := 409;
                    v_error_reason := 'operation_already_reverted';
                    v_error_message := 'operation was already reverted';
                END IF;
            END IF;

            IF v_error_reason IS NULL THEN
                SELECT pg_catalog.array_agg(DISTINCT key ORDER BY key)
                INTO v_before_track_ids
                FROM (
                    SELECT track.key
                    FROM pg_catalog.jsonb_each(v_operation.before_state -> 'tracks') AS track(key, value)
                    UNION
                    SELECT track.key
                    FROM pg_catalog.jsonb_each(v_operation.after_state -> 'tracks') AS track(key, value)
                ) AS track_keys;
                v_after_track_ids := v_before_track_ids;

                FOR v_row IN
                    SELECT track.id
                    FROM public.scene_tracks AS track
                    WHERE track.scene_id = v_scene_id
                      AND track.track_id = ANY(v_before_track_ids)
                    ORDER BY track.track_id
                    FOR UPDATE OF track
                LOOP
                    NULL;
                END LOOP;

                IF EXISTS (
                    SELECT 1
                    FROM pg_catalog.jsonb_each(v_operation.result_revisions) AS expected(track_id, revision)
                    LEFT JOIN public.scene_tracks AS track
                      ON track.scene_id = v_scene_id
                     AND track.track_id = expected.track_id
                    WHERE track.id IS NULL
                       OR (CASE WHEN track.revision IS NULL OR track.revision = 0 THEN 1 ELSE track.revision END)
                          <> (expected.revision #>> '{}')::integer
                ) THEN
                    v_error_status := 409;
                    v_error_reason := 'operation_revert_stale';
                    v_error_message := 'Scene Track changed after this operation and cannot be reverted safely';
                END IF;
            END IF;

            IF v_error_reason IS NULL THEN
                FOR v_row IN
                    SELECT annotation.id
                    FROM public.annotations AS annotation
                    WHERE annotation.id IN (
                        SELECT DISTINCT (member.value ->> 'id')::uuid
                        FROM (
                            SELECT v_operation.before_state AS state
                            UNION ALL
                            SELECT v_operation.after_state AS state
                        ) AS snapshots
                        CROSS JOIN LATERAL pg_catalog.jsonb_each(snapshots.state -> 'tracks') AS track(key, value)
                        CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(track.value -> 'members') AS member(value)
                    )
                    ORDER BY annotation.id
                    FOR UPDATE OF annotation
                LOOP
                    NULL;
                END LOOP;

                IF EXISTS (
                    SELECT 1
                    FROM pg_catalog.jsonb_each(v_operation.after_state -> 'tracks') AS track(key, value)
                    CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(track.value -> 'members') AS expected(value)
                    LEFT JOIN public.annotations AS annotation
                      ON annotation.id = (expected.value ->> 'id')::uuid
                    WHERE annotation.id IS NULL
                       OR (CASE WHEN annotation.version IS NULL OR annotation.version = 0 THEN 1 ELSE annotation.version END)
                          <> (expected.value ->> 'version')::integer
                ) THEN
                    v_error_status := 409;
                    v_error_reason := 'operation_revert_stale';
                    v_error_message := 'Scene Track member changed after this operation';
                END IF;
            END IF;

            IF v_error_reason IS NULL AND EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_each(v_operation.before_state -> 'tracks') AS track(key, value)
                LEFT JOIN public.scene_tracks AS current_track
                  ON current_track.scene_id = v_scene_id
                 AND current_track.track_id = track.key
                WHERE current_track.id IS NULL
            ) THEN
                v_error_status := 409;
                v_error_reason := 'operation_revert_track_missing';
                v_error_message := 'a required Scene Track was deleted after the operation';
            END IF;

            IF v_error_reason IS NULL AND EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_each(v_operation.before_state -> 'tracks') AS track(key, value)
                CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(track.value -> 'members') AS member(value)
                LEFT JOIN public.annotations AS current_member
                  ON current_member.id = (member.value ->> 'id')::uuid
                WHERE current_member.id IS NULL
            ) THEN
                v_error_status := 409;
                v_error_reason := 'operation_revert_member_missing';
                v_error_message := 'a required Scene Track member was deleted after the operation';
            END IF;

            IF v_error_reason IS NULL THEN
                v_before_state := v_operation.after_state;
                v_operation_id := pg_catalog.gen_random_uuid();
                INSERT INTO public.scene_track_operations (
                    id, scene_id, actor_id, kind, idempotency_key, request_digest,
                    snapshot_token, source_revisions, result_revisions, before_state,
                    after_state, inverse_payload, response_json, status
                ) VALUES (
                    v_operation_id, v_scene_id, v_actor_id, 'revert', v_idempotency_key,
                    v_operation.id::text, v_operation.snapshot_token,
                    v_operation.result_revisions, '{}'::jsonb, v_operation.after_state,
                    '{}'::jsonb,
                    pg_catalog.jsonb_build_object('operation_id', v_operation.id::text),
                    '{}'::jsonb, 'committed'
                );

                FOR v_entry IN
                    SELECT track.key AS track_id, track.value AS state
                    FROM pg_catalog.jsonb_each(v_operation.before_state -> 'tracks') AS track(key, value)
                    ORDER BY track.key
                LOOP
                    UPDATE public.scene_tracks AS current_track
                    SET class_name = v_entry.state ->> 'class_name',
                        presence_mode = COALESCE(v_entry.state ->> 'presence_mode', 'inferred'),
                        attributes = CASE
                            WHEN v_entry.state -> 'attributes' IS NULL OR v_entry.state -> 'attributes' = 'null'::jsonb
                            THEN '{}'::jsonb ELSE v_entry.state -> 'attributes' END,
                        attributes_meta = CASE
                            WHEN v_entry.state -> 'attributes_meta' IS NULL OR v_entry.state -> 'attributes_meta' = 'null'::jsonb
                            THEN '{}'::jsonb ELSE v_entry.state -> 'attributes_meta' END,
                        retired_at = NULLIF(v_entry.state ->> 'retired_at', '')::timestamptz,
                        revision = (CASE WHEN current_track.revision IS NULL OR current_track.revision = 0 THEN 1 ELSE current_track.revision END) + 1,
                        updated_at = pg_catalog.transaction_timestamp()
                    WHERE current_track.scene_id = v_scene_id
                      AND current_track.track_id = v_entry.track_id;
                END LOOP;

                UPDATE public.scene_tracks AS current_track
                SET revision = (CASE WHEN current_track.revision IS NULL OR current_track.revision = 0 THEN 1 ELSE current_track.revision END) + 1,
                    retired_at = pg_catalog.clock_timestamp(),
                    updated_at = pg_catalog.transaction_timestamp()
                WHERE current_track.scene_id = v_scene_id
                  AND current_track.track_id = ANY(v_after_track_ids)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pg_catalog.jsonb_each(v_operation.before_state -> 'tracks') AS before_track(key, value)
                      WHERE before_track.key = current_track.track_id
                  );

                FOR v_entry IN
                    SELECT member.value AS state
                    FROM pg_catalog.jsonb_each(v_operation.before_state -> 'tracks') AS track(key, value)
                    CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(track.value -> 'members') AS member(value)
                    ORDER BY (member.value ->> 'id')::uuid
                LOOP
                    UPDATE public.annotations AS current_member
                    SET track_id = v_entry.state ->> 'track_id',
                        scene_track_id = NULLIF(v_entry.state ->> 'scene_track_id', '')::uuid,
                        class_name = v_entry.state ->> 'class_name',
                        is_active = (v_entry.state ->> 'is_active')::boolean,
                        is_hidden = (v_entry.state ->> 'is_hidden')::boolean,
                        temporal_role = v_entry.state ->> 'temporal_role',
                        version = (CASE WHEN current_member.version IS NULL OR current_member.version = 0 THEN 1 ELSE current_member.version END) + 1,
                        updated_at = pg_catalog.transaction_timestamp()
                    WHERE current_member.id = (v_entry.state ->> 'id')::uuid;
                END LOOP;

                FOR v_entry IN
                    SELECT member.value AS state
                    FROM pg_catalog.jsonb_each(v_operation.after_state -> 'tracks') AS track(key, value)
                    CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(track.value -> 'members') AS member(value)
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM pg_catalog.jsonb_each(v_operation.before_state -> 'tracks') AS before_track(key, value)
                        CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(before_track.value -> 'members') AS before_member(value)
                        WHERE before_member.value ->> 'id' = member.value ->> 'id'
                    )
                    ORDER BY (member.value ->> 'id')::uuid
                LOOP
                    UPDATE public.annotations AS current_member
                    SET is_active = false,
                        is_hidden = true,
                        version = (CASE WHEN current_member.version IS NULL OR current_member.version = 0 THEN 1 ELSE current_member.version END) + 1,
                        updated_at = pg_catalog.transaction_timestamp()
                    WHERE current_member.id = (v_entry.state ->> 'id')::uuid;
                END LOOP;

                FOR v_entry IN
                    SELECT track.track_id, track.id,
                           before_track.value AS before_state
                    FROM public.scene_tracks AS track
                    LEFT JOIN pg_catalog.jsonb_each(v_operation.before_state -> 'tracks') AS before_track(key, value)
                      ON before_track.key = track.track_id
                    WHERE track.scene_id = v_scene_id
                      AND track.track_id = ANY(v_after_track_ids)
                    ORDER BY track.track_id
                LOOP
                    IF EXISTS (
                        SELECT 1 FROM public.scene_track_intervals AS interval
                        WHERE interval.scene_track_id = v_entry.id
                    ) THEN
                        DELETE FROM public.scene_track_intervals AS interval
                        WHERE interval.scene_track_id = v_entry.id;
                    END IF;
                    IF v_entry.before_state IS NOT NULL
                       AND pg_catalog.jsonb_array_length(v_entry.before_state -> 'intervals') > 0 THEN
                        INSERT INTO public.scene_track_intervals (
                            id, scene_track_id, start_frame, end_frame, source,
                            version, created_by, operation_id
                        )
                        SELECT pg_catalog.gen_random_uuid(), v_entry.id,
                               (interval.value ->> 'start_frame')::integer,
                               NULLIF(interval.value ->> 'end_frame', '')::integer,
                               interval.value ->> 'source', 1, v_actor_id, v_operation_id
                        FROM pg_catalog.jsonb_array_elements(v_entry.before_state -> 'intervals')
                            WITH ORDINALITY AS interval(value, ordinal)
                        ORDER BY interval.ordinal;
                    END IF;
                    v_replaced_track_ids := pg_catalog.array_append(v_replaced_track_ids, v_entry.id);
                END LOOP;

                UPDATE public.scene_track_operations AS original
                SET status = 'reverted', reverted_by_operation_id = v_operation_id
                WHERE original.id = v_operation.id;

                SELECT COALESCE(pg_catalog.jsonb_object_agg(
                    track.track_id,
                    CASE WHEN track.revision IS NULL OR track.revision = 0 THEN 1 ELSE track.revision END
                ), '{}'::jsonb)
                INTO v_result_revisions
                FROM public.scene_tracks AS track
                WHERE track.scene_id = v_scene_id
                  AND track.track_id = ANY(v_after_track_ids);

                SELECT pg_catalog.jsonb_build_object('tracks', COALESCE(pg_catalog.jsonb_object_agg(
                    track.track_id,
                    pg_catalog.jsonb_build_object(
                        'id', track.id::text,
                        'track_id', track.track_id,
                        'class_name', track.class_name,
                        'presence_mode', track.presence_mode,
                        'attributes', CASE WHEN track.attributes IS NULL OR track.attributes IN ('null'::jsonb, '{}'::jsonb, '[]'::jsonb, '""'::jsonb, '0'::jsonb, 'false'::jsonb) THEN '{}'::jsonb ELSE track.attributes END,
                        'attributes_meta', CASE WHEN track.attributes_meta IS NULL OR track.attributes_meta IN ('null'::jsonb, '{}'::jsonb, '[]'::jsonb, '""'::jsonb, '0'::jsonb, 'false'::jsonb) THEN '{}'::jsonb ELSE track.attributes_meta END,
                        'revision', CASE WHEN track.revision IS NULL OR track.revision = 0 THEN 1 ELSE track.revision END,
                        'retired_at', CASE WHEN track.retired_at IS NULL THEN NULL ELSE pg_catalog.to_jsonb(track.retired_at) END,
                        'intervals', COALESCE((
                            SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                                'id', interval.id::text,
                                'start_frame', interval.start_frame,
                                'end_frame', interval.end_frame,
                                'source', interval.source,
                                'version', CASE WHEN interval.version IS NULL OR interval.version = 0 THEN 1 ELSE interval.version END
                            ) ORDER BY interval.start_frame, interval.id)
                            FROM public.scene_track_intervals AS interval
                            WHERE interval.scene_track_id = track.id
                        ), '[]'::jsonb),
                        'members', COALESCE((
                            SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                                'id', member.id::text,
                                'task_id', member.task_id::text,
                                'track_id', member.track_id,
                                'scene_track_id', member.scene_track_id::text,
                                'class_name', member.class_name,
                                'is_active', member.is_active,
                                'is_hidden', member.is_hidden,
                                'temporal_role', member.temporal_role,
                                'sensor_role', member.sensor_role,
                                'version', CASE WHEN member.version IS NULL OR member.version = 0 THEN 1 ELSE member.version END
                            ) ORDER BY member.id)
                            FROM public.annotations AS member
                            WHERE member.scene_track_id = track.id
                              AND member.was_cancelled IS FALSE
                        ), '[]'::jsonb)
                    ) ORDER BY track.track_id
                ), '{}'::jsonb))
                INTO v_after_state
                FROM public.scene_tracks AS track
                WHERE track.scene_id = v_scene_id
                  AND track.track_id = ANY(v_after_track_ids);

                v_response := v_operation.response_json || pg_catalog.jsonb_build_object(
                    'kind', 'revert',
                    'operation_id', v_operation_id::text,
                    'status', 'committed',
                    'created_track_id', NULL,
                    'source_revisions', v_operation.result_revisions,
                    'result_revisions', v_result_revisions,
                    'before_intervals', v_operation.response_json -> 'after_intervals',
                    'after_intervals', v_operation.response_json -> 'before_intervals'
                );
                UPDATE public.scene_track_operations AS revert_operation
                SET result_revisions = v_result_revisions,
                    after_state = v_after_state,
                    response_json = v_response
                WHERE revert_operation.id = v_operation_id
                RETURNING * INTO v_operation;

                SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(task_id) ORDER BY task_id), '[]'::jsonb)
                INTO v_task_ids
                FROM (
                    SELECT DISTINCT (member.value ->> 'task_id')::uuid AS task_id
                    FROM (
                        SELECT v_before_state AS state
                        UNION ALL
                        SELECT v_after_state AS state
                    ) AS snapshots
                    CROSS JOIN LATERAL pg_catalog.jsonb_each(snapshots.state -> 'tracks') AS track(key, value)
                    CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(track.value -> 'members') AS member(value)
                    WHERE member.value ? 'task_id'
                ) AS operation_tasks;
                v_data := pg_catalog.jsonb_build_object(
                    'operation', pg_catalog.jsonb_build_object(
                        'id', v_operation.id, 'kind', v_operation.kind,
                        'status', v_operation.status, 'created_at', v_operation.created_at,
                        'completed_at', v_operation.completed_at,
                        'response_json', v_operation.response_json,
                        'task_ids', COALESCE(v_task_ids, '[]'::jsonb)
                    ),
                    'response_json', v_response,
                    'task_ids', COALESCE(v_task_ids, '[]'::jsonb),
                    'before_state', v_before_state,
                    'after_state', v_after_state,
                    'replaced_track_ids', pg_catalog.to_jsonb(v_replaced_track_ids),
                    'found', false
                );
            END IF;
        END IF;
    END IF;

    IF v_error_reason IS NOT NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-scene-track-commands/v1',
            'action', v_action,
            'ok', false,
            'data', NULL,
            'error', pg_catalog.jsonb_build_object(
                'status_code', v_error_status,
                'reason', v_error_reason,
                'message', v_error_message
            )
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-scene-track-commands/v1',
        'action', v_action,
        'ok', true,
        'data', v_data,
        'error', NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.scene_track_command(jsonb) FROM PUBLIC;
