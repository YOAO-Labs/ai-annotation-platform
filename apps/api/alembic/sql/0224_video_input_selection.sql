-- Labeling owns bounded video Task selection; Assets receives it through EXECUTE.
CREATE FUNCTION aap_labeling_v1._video_input_selection_reply(
    p_data jsonb, p_code text
)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_code IS NULL THEN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-video-input-selection/v1',
        'ok', true, 'data', p_data
    ) ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-video-input-selection/v1',
        'ok', false, 'error', pg_catalog.jsonb_build_object('code', p_code)
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_input_selection_reply(jsonb, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._video_input_manifest_selection_valid(
    p_selection jsonb
)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_task_ids jsonb;
    v_sampling jsonb;
    v_entry jsonb;
    v_id text;
    v_previous text;
    v_count integer;
BEGIN
    IF pg_catalog.jsonb_typeof(p_selection) IS DISTINCT FROM 'object'
       OR (p_selection - ARRAY[
            'batch_id', 'task_ids', 'predict_mode', 'execution_scope', 'sampling'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_selection ?& ARRAY[
            'batch_id', 'task_ids', 'predict_mode', 'execution_scope', 'sampling'
       ])
       OR pg_catalog.jsonb_typeof(p_selection -> 'batch_id') NOT IN ('string', 'null')
       OR (p_selection -> 'batch_id' <> 'null'::jsonb
           AND p_selection ->> 'batch_id' !~
               '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
       OR pg_catalog.jsonb_typeof(p_selection -> 'task_ids') NOT IN ('array', 'null')
       OR pg_catalog.jsonb_typeof(p_selection -> 'predict_mode') IS DISTINCT FROM 'string'
       OR p_selection ->> 'predict_mode' NOT IN ('skip_predicted', 'overwrite', 'append')
       OR pg_catalog.jsonb_typeof(p_selection -> 'execution_scope') IS DISTINCT FROM 'string'
       OR p_selection ->> 'execution_scope' NOT IN ('bulk', 'workbench') THEN
        RETURN false;
    END IF;

    v_task_ids := p_selection -> 'task_ids';
    IF pg_catalog.jsonb_typeof(v_task_ids) = 'null' THEN
        IF p_selection ->> 'execution_scope' = 'workbench' THEN RETURN false; END IF;
    ELSIF pg_catalog.jsonb_typeof(v_task_ids) = 'array' THEN
        v_count := pg_catalog.jsonb_array_length(v_task_ids);
        IF v_count NOT BETWEEN 1 AND 200 THEN RETURN false; END IF;
        v_previous := NULL;
        FOR v_entry IN SELECT value FROM pg_catalog.jsonb_array_elements(v_task_ids)
        LOOP
            IF pg_catalog.jsonb_typeof(v_entry) IS DISTINCT FROM 'string' THEN
                RETURN false;
            END IF;
            v_id := v_entry #>> '{}';
            IF v_id !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR (v_previous IS NOT NULL AND v_id <= v_previous) THEN
                RETURN false;
            END IF;
            v_previous := v_id;
        END LOOP;
        IF p_selection ->> 'execution_scope' = 'workbench' AND v_count <> 1 THEN
            RETURN false;
        END IF;
    ELSE
        RETURN false;
    END IF;
    IF p_selection ->> 'execution_scope' = 'workbench'
       AND p_selection -> 'batch_id' IS DISTINCT FROM 'null'::jsonb THEN
        RETURN false;
    END IF;

    v_sampling := p_selection -> 'sampling';
    IF pg_catalog.jsonb_typeof(v_sampling) IS DISTINCT FROM 'object'
       OR (v_sampling - ARRAY['max_frames', 'step', 'width', 'format'])
            IS DISTINCT FROM '{}'::jsonb
       OR NOT (v_sampling ?& ARRAY['max_frames', 'step', 'width', 'format'])
       OR pg_catalog.jsonb_typeof(v_sampling -> 'max_frames') IS DISTINCT FROM 'number'
       OR NOT (CASE
            WHEN (v_sampling ->> 'max_frames') ~ '^[1-9][0-9]*$'
                 AND pg_catalog.char_length(v_sampling ->> 'max_frames') <= 10
            THEN (v_sampling ->> 'max_frames')::numeric <= 2147483647
            ELSE false
       END)
       OR pg_catalog.jsonb_typeof(v_sampling -> 'step') IS DISTINCT FROM 'number'
       OR NOT (CASE
            WHEN (v_sampling ->> 'step') ~ '^[1-9][0-9]*$'
                 AND pg_catalog.char_length(v_sampling ->> 'step') <= 10
            THEN (v_sampling ->> 'step')::numeric <= 2147483647
            ELSE false
       END)
       OR pg_catalog.jsonb_typeof(v_sampling -> 'width') IS DISTINCT FROM 'number'
       OR NOT (CASE
            WHEN (v_sampling ->> 'width') ~ '^[1-9][0-9]*$'
                 AND pg_catalog.char_length(v_sampling ->> 'width') <= 4
            THEN (v_sampling ->> 'width')::numeric <= 4096
            ELSE false
       END)
       OR pg_catalog.jsonb_typeof(v_sampling -> 'format') IS DISTINCT FROM 'string'
       OR v_sampling ->> 'format' NOT IN ('jpeg', 'webp') THEN
        RETURN false;
    END IF;
    RETURN true;
EXCEPTION WHEN OTHERS THEN
    RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_input_manifest_selection_valid(jsonb)
    FROM PUBLIC;
-- aap-statement-break

-- Raw cursor members may preserve a missing DatasetItem link; only the ready
-- TaskVideoBinding projection in 0223 requires that link.
CREATE FUNCTION aap_labeling_v1._video_input_captured_task_valid(p_task jsonb)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF pg_catalog.jsonb_typeof(p_task) IS DISTINCT FROM 'object'
       OR (p_task - ARRAY[
            'task_id', 'project_id', 'batch_id', 'task_version',
            'task_source_generation', 'dataset_item_id', 'task_source_snapshot',
            'file_path', 'file_type'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_task ?& ARRAY[
            'task_id', 'project_id', 'batch_id', 'task_version',
            'task_source_generation', 'dataset_item_id', 'task_source_snapshot',
            'file_path', 'file_type'
       ])
       OR pg_catalog.jsonb_typeof(p_task -> 'batch_id') NOT IN ('string', 'null')
       OR (p_task -> 'batch_id' <> 'null'::jsonb
           AND p_task ->> 'batch_id' !~
               '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
        RETURN false;
    END IF;
    IF p_task -> 'dataset_item_id' <> 'null'::jsonb THEN
        RETURN pg_catalog.jsonb_typeof(p_task -> 'dataset_item_id') = 'string'
           AND aap_labeling_v1._video_input_task_binding_valid(p_task - 'batch_id');
    END IF;
    RETURN COALESCE(p_task ->> 'task_id' ~
               '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       AND p_task ->> 'project_id' ~
               '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       AND pg_catalog.jsonb_typeof(p_task -> 'task_version') = 'number'
       AND (p_task ->> 'task_version') ~ '^[1-9][0-9]*$'
       AND (p_task ->> 'task_version')::numeric <= 9007199254740991
       AND pg_catalog.jsonb_typeof(p_task -> 'task_source_generation') = 'number'
       AND (p_task ->> 'task_source_generation') ~ '^[1-9][0-9]*$'
       AND (p_task ->> 'task_source_generation')::numeric <= 9007199254740991
       AND p_task ->> 'task_source_snapshot' ~ '^[0-9a-f]{64}$'
       AND pg_catalog.jsonb_typeof(p_task -> 'file_path') = 'string'
       AND pg_catalog.char_length(p_task ->> 'file_path') <= 1024
       AND p_task ->> 'file_type' = 'video', false);
EXCEPTION WHEN OTHERS THEN
    RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_input_captured_task_valid(jsonb)
    FROM PUBLIC;
-- aap-statement-break

-- Preserve current Task-level assignment/visibility rules while Assets streams
-- only the captured video facts it needs.
CREATE FUNCTION aap_labeling_v1._video_input_selection_task_access(
    p_actor_id uuid,
    p_project_id uuid,
    p_task_id uuid,
    p_dataset_item_id uuid,
    p_file_type text
)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_data jsonb;
BEGIN
    IF p_actor_id IS NULL OR p_project_id IS NULL OR p_task_id IS NULL THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_request');
    END IF;
    IF p_dataset_item_id IS NOT NULL THEN
        v_reply := aap_labeling_v1.read_media_task_access(
            p_actor_id, p_task_id, p_dataset_item_id
        );
        IF v_reply IS NULL
           OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-media-task-access/v1'
           OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
        END IF;
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            CASE v_reply #>> '{error,code}'
                WHEN 'invalid_request' THEN
                    RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_request');
                WHEN 'account_not_active' THEN
                    RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'account_not_active');
                WHEN 'account_busy' THEN
                    RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'account_busy');
                WHEN 'invalid_account_role' THEN
                    RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_account_role');
                WHEN 'task_busy' THEN
                    RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_busy');
                WHEN 'task_not_visible' THEN
                    RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_not_visible');
                WHEN 'permission_denied' THEN
                    RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'permission_denied');
                ELSE
                    RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
            END CASE;
        END IF;
        v_data := v_reply -> 'data';
        IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
           OR (v_data - ARRAY[
                'actor_id', 'task_id', 'dataset_item_id', 'project_id', 'file_type',
                'project_role', 'is_manager', 'annotation_write'
           ]) IS DISTINCT FROM '{}'::jsonb
           OR NOT (v_data ?& ARRAY[
                'actor_id', 'task_id', 'dataset_item_id', 'project_id', 'file_type',
                'project_role', 'is_manager', 'annotation_write'
           ])
           OR v_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
           OR v_data ->> 'task_id' IS DISTINCT FROM p_task_id::text
           OR v_data ->> 'dataset_item_id' IS DISTINCT FROM p_dataset_item_id::text
           OR v_data ->> 'project_id' IS DISTINCT FROM p_project_id::text
           OR v_data ->> 'file_type' IS DISTINCT FROM p_file_type
           OR pg_catalog.jsonb_typeof(v_data -> 'project_role') NOT IN ('string', 'null')
           OR pg_catalog.jsonb_typeof(v_data -> 'is_manager') IS DISTINCT FROM 'boolean'
           OR pg_catalog.jsonb_typeof(v_data -> 'annotation_write') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
        END IF;
    ELSE
        v_reply := aap_workspace_v1.read_media_project_access(p_actor_id, p_project_id);
        IF v_reply IS NULL
           OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
           OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
        END IF;
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            CASE v_reply #>> '{error,code}'
                WHEN 'account_not_active' THEN
                    RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'account_not_active');
                WHEN 'account_busy' THEN
                    RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'account_busy');
                WHEN 'invalid_account_role' THEN
                    RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_account_role');
                WHEN 'task_busy' THEN
                    RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_busy');
                WHEN 'project_missing' THEN
                    RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_not_visible');
                ELSE
                    RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
            END CASE;
        END IF;
        v_data := v_reply -> 'data';
        IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
           OR (v_data - ARRAY[
                'actor_id', 'project_id', 'platform_role', 'project_role', 'is_manager',
                'is_super_admin', 'video_collaboration_enabled', 'project_name'
           ]) IS DISTINCT FROM '{}'::jsonb
           OR NOT (v_data ?& ARRAY[
                'actor_id', 'project_id', 'platform_role', 'project_role', 'is_manager',
                'is_super_admin', 'video_collaboration_enabled', 'project_name'
           ])
           OR v_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
           OR v_data ->> 'project_id' IS DISTINCT FROM p_project_id::text
           OR pg_catalog.jsonb_typeof(v_data -> 'platform_role') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_data -> 'project_role') NOT IN ('string', 'null')
           OR pg_catalog.jsonb_typeof(v_data -> 'is_manager') IS DISTINCT FROM 'boolean'
           OR pg_catalog.jsonb_typeof(v_data -> 'is_super_admin') IS DISTINCT FROM 'boolean'
           OR pg_catalog.jsonb_typeof(v_data -> 'video_collaboration_enabled') IS DISTINCT FROM 'boolean'
           OR pg_catalog.jsonb_typeof(v_data -> 'project_name') IS DISTINCT FROM 'string' THEN
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
        END IF;
        IF v_data -> 'is_manager' IS DISTINCT FROM 'true'::jsonb
           AND NOT (p_file_type = 'video'
                    AND v_data -> 'video_collaboration_enabled' = 'true'::jsonb) THEN
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_not_visible');
        END IF;
    END IF;
    RETURN aap_labeling_v1._video_input_selection_reply(
        pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id, 'project_id', p_project_id, 'task_id', p_task_id
        ), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_input_selection_task_access(
    uuid, uuid, uuid, uuid, text
) FROM PUBLIC;
-- aap-statement-break

-- A cursor keeps full project/batch membership out of Job payloads and JSON arrays.
CREATE FUNCTION aap_labeling_v1.open_video_input_manifest_members(
    p_actor_id uuid, p_project_id uuid, p_selection jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_access jsonb;
    v_data jsonb;
    v_code text;
    v_batch_id uuid;
    v_task_ids uuid[] := ARRAY[]::uuid[];
    v_task_id uuid;
    v_task record;
    v_task_access jsonb;
    v_has_explicit boolean;
    v_request_count integer := 0;
    v_cursor_name text;
    v_cursor refcursor;
BEGIN
    IF p_actor_id IS NULL OR p_project_id IS NULL
       OR p_selection IS NULL
       OR pg_catalog.octet_length(pg_catalog.convert_to(p_selection::text, 'UTF8')) > 65536
       OR NOT aap_labeling_v1._video_input_manifest_selection_valid(p_selection) THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        IF p_selection -> 'batch_id' <> 'null'::jsonb THEN
            v_batch_id := (p_selection ->> 'batch_id')::uuid;
        END IF;
        v_has_explicit := p_selection -> 'task_ids' <> 'null'::jsonb;
        IF v_has_explicit THEN
            v_request_count := pg_catalog.jsonb_array_length(p_selection -> 'task_ids');
            SELECT COALESCE(pg_catalog.array_agg(
                (entry.value #>> '{}')::uuid ORDER BY (entry.value #>> '{}')::uuid
            ), ARRAY[]::uuid[])
            INTO v_task_ids
            FROM pg_catalog.jsonb_array_elements(p_selection -> 'task_ids') AS entry(value);
        END IF;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_request');
    END;

    v_access := aap_workspace_v1.read_media_project_access(p_actor_id, p_project_id);
    IF v_access IS NULL
       OR v_access ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
       OR pg_catalog.jsonb_typeof(v_access -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
    END IF;
    IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_access #>> '{error,code}';
        IF v_code IN ('account_not_active', 'account_busy', 'invalid_account_role', 'task_busy') THEN
            RETURN aap_labeling_v1._video_input_selection_reply(
                NULL, CASE WHEN v_code = 'task_busy' THEN 'source_busy' ELSE v_code END
            );
        ELSIF v_code = 'project_missing' THEN
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_not_visible');
        END IF;
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
    END IF;
    v_data := v_access -> 'data';
    IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
       OR (v_data - ARRAY[
            'actor_id', 'project_id', 'platform_role', 'project_role', 'is_manager',
            'is_super_admin', 'video_collaboration_enabled', 'project_name'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (v_data ?& ARRAY[
            'actor_id', 'project_id', 'platform_role', 'project_role', 'is_manager',
            'is_super_admin', 'video_collaboration_enabled', 'project_name'
       ])
       OR v_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR v_data ->> 'project_id' IS DISTINCT FROM p_project_id::text
       OR pg_catalog.jsonb_typeof(v_data -> 'platform_role') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_data -> 'project_role') NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(v_data -> 'is_manager') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
    END IF;
    IF pg_catalog.jsonb_typeof(v_data -> 'is_super_admin') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_data -> 'video_collaboration_enabled') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_data -> 'project_name') IS DISTINCT FROM 'string' THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
    END IF;
    IF v_data -> 'is_manager' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'permission_denied');
    END IF;

    IF p_selection ->> 'execution_scope' = 'workbench' THEN
        v_task_id := v_task_ids[1];
        SELECT task.id, task.project_id, task.dataset_item_id, task.file_type
        INTO v_task
        FROM public.tasks AS task
        WHERE task.id = v_task_id;
        IF NOT FOUND OR v_task.project_id IS DISTINCT FROM p_project_id THEN
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
        END IF;
        v_task_access := aap_labeling_v1._video_input_selection_task_access(
            p_actor_id, p_project_id, v_task.id, v_task.dataset_item_id, v_task.file_type
        );
        IF v_task_access IS NULL
           OR v_task_access ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-input-selection/v1'
           OR pg_catalog.jsonb_typeof(v_task_access -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
        END IF;
        IF v_task_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_code := v_task_access #>> '{error,code}';
            IF v_code IN (
                'account_not_active', 'account_busy', 'invalid_account_role',
                'task_busy', 'task_not_visible', 'permission_denied'
            ) THEN
                RETURN aap_labeling_v1._video_input_selection_reply(NULL, v_code);
            END IF;
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
        END IF;
        v_data := v_task_access -> 'data';
        IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
           OR (v_data - ARRAY['actor_id', 'project_id', 'task_id']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (v_data ?& ARRAY['actor_id', 'project_id', 'task_id'])
           OR v_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
           OR v_data ->> 'project_id' IS DISTINCT FROM p_project_id::text
           OR v_data ->> 'task_id' IS DISTINCT FROM v_task.id::text THEN
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
        END IF;
    END IF;

    v_cursor_name := 'aap_video_members_' || pg_catalog.replace(
        pg_catalog.gen_random_uuid()::text, '-', ''
    );
    v_cursor := v_cursor_name;
    OPEN v_cursor FOR
        WITH eligible AS MATERIALIZED (
            SELECT task.id, task.project_id, task.dataset_item_id, task.batch_id,
                   task.file_type, task.file_path, task.created_at, task.version,
                   task.media_source_generation, task.total_predictions,
                   aap_labeling_v1._image_source_snapshot_fields(
                       task.id, task.created_at, task.project_id,
                       task.dataset_item_id, task.file_type, task.file_path,
                       task.media_source_generation
                   ) AS task_source_snapshot
            FROM public.tasks AS task
            LEFT JOIN public.task_batches AS batch ON batch.id = task.batch_id
            WHERE task.project_id = p_project_id
              AND (NOT v_has_explicit OR task.id = ANY(v_task_ids))
              AND (v_batch_id IS NULL OR task.batch_id = v_batch_id)
              AND (
                  (p_selection ->> 'execution_scope' = 'bulk'
                   AND task.status = 'pending'
                   AND (task.batch_id IS NULL OR (
                       batch.id IS NOT NULL AND batch.project_id = p_project_id
                       AND NOT batch.admin_locked
                       AND (batch.status = 'active' OR (
                           batch.status = 'draft'
                           AND batch.annotator_id IS NULL
                           AND batch.reviewer_id IS NULL
                       ))
                   )))
                  OR
                  (p_selection ->> 'execution_scope' = 'workbench'
                   AND task.status IN ('pending', 'in_progress', 'rejected')
                   AND (task.batch_id IS NULL OR (
                       batch.id IS NOT NULL AND batch.project_id = p_project_id
                       AND NOT batch.admin_locked
                   )))
              )
              AND NOT EXISTS (
                  SELECT 1 FROM public.task_locks AS task_lock
                  WHERE task_lock.task_id = task.id
                    AND task_lock.expire_at > pg_catalog.clock_timestamp()
                    AND (p_selection ->> 'execution_scope' = 'bulk'
                         OR task_lock.user_id <> p_actor_id)
              )
        ), counts AS MATERIALIZED (
            SELECT pg_catalog.count(*)::bigint AS candidate_count,
                   pg_catalog.count(*) FILTER (
                       WHERE p_selection ->> 'predict_mode' <> 'skip_predicted'
                          OR COALESCE(eligible.total_predictions, 0) = 0
                   )::bigint AS selected_count,
                   COALESCE(pg_catalog.bool_or(eligible.file_type IS DISTINCT FROM 'video'), false)
                       AS unsupported_media,
                   COALESCE(pg_catalog.bool_or(
                       eligible.version IS NULL OR eligible.version <= 0
                       OR eligible.media_source_generation IS NULL
                       OR eligible.media_source_generation <= 0
                       OR eligible.file_path IS NULL
                       OR pg_catalog.char_length(eligible.file_path) > 1024
                       OR eligible.created_at IS NULL
                       OR eligible.task_source_snapshot !~ '^[0-9a-f]{64}$'
                   ), false) AS invalid_capture,
                   CASE WHEN v_batch_id IS NULL THEN true ELSE EXISTS (
                       SELECT 1 FROM public.task_batches AS batch
                       WHERE batch.id = v_batch_id
                         AND batch.project_id = p_project_id
                         AND NOT batch.admin_locked
                         AND (
                             p_selection ->> 'execution_scope' = 'workbench'
                             OR batch.status = 'active'
                             OR (batch.status = 'draft'
                                 AND batch.annotator_id IS NULL
                                 AND batch.reviewer_id IS NULL)
                         )
                   ) END AS batch_valid,
                   v_has_explicit AS has_explicit,
                   v_request_count::bigint AS requested_count
            FROM eligible
        ), selected AS MATERIALIZED (
            SELECT eligible.*,
                   pg_catalog.row_number() OVER (ORDER BY eligible.id)::bigint AS ordinal
            FROM eligible
            WHERE p_selection ->> 'predict_mode' <> 'skip_predicted'
               OR COALESCE(eligible.total_predictions, 0) = 0
        ), rows AS (
            SELECT 0 AS row_kind, 0::bigint AS ordinal,
                   CASE
                       WHEN NOT counts.batch_valid THEN pg_catalog.jsonb_build_object(
                           'kind', 'error', 'code', 'task_scope_changed')
                       WHEN counts.has_explicit
                            AND counts.candidate_count <> counts.requested_count
                           THEN pg_catalog.jsonb_build_object(
                               'kind', 'error', 'code', 'task_scope_changed')
                       WHEN counts.invalid_capture THEN pg_catalog.jsonb_build_object(
                           'kind', 'error', 'code', 'invalid_response')
                       WHEN counts.unsupported_media THEN pg_catalog.jsonb_build_object(
                           'kind', 'error', 'code', 'unsupported_media')
                       WHEN counts.candidate_count > 9007199254740991
                            OR counts.selected_count > 9007199254740991
                           THEN pg_catalog.jsonb_build_object(
                               'kind', 'error', 'code', 'resource_limit')
                       ELSE pg_catalog.jsonb_build_object(
                           'kind', 'header',
                           'candidate_count', counts.candidate_count,
                           'selected_count', counts.selected_count
                       )
                   END AS row_data
            FROM counts
            UNION ALL
            SELECT 1, selected.ordinal,
                   pg_catalog.jsonb_build_object(
                       'kind', 'member',
                       'ordinal', selected.ordinal,
                       'task', pg_catalog.jsonb_build_object(
                           'task_id', selected.id,
                           'project_id', selected.project_id,
                           'batch_id', selected.batch_id,
                           'task_version', selected.version,
                           'task_source_generation', selected.media_source_generation,
                           'dataset_item_id', selected.dataset_item_id,
                           'task_source_snapshot', selected.task_source_snapshot,
                           'file_path', selected.file_path,
                           'file_type', selected.file_type
                       )
                   )
            FROM selected
            CROSS JOIN counts
            WHERE counts.batch_valid
              AND (NOT counts.has_explicit
                   OR counts.candidate_count = counts.requested_count)
              AND NOT counts.invalid_capture
              AND NOT counts.unsupported_media
              AND counts.candidate_count <= 9007199254740991
              AND counts.selected_count <= 9007199254740991
        )
        SELECT rows.row_data FROM rows ORDER BY rows.row_kind, rows.ordinal;
    RETURN aap_labeling_v1._video_input_selection_reply(
        pg_catalog.jsonb_build_object('cursor', v_cursor::text), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'source_busy');
WHEN OTHERS THEN
    RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.open_video_input_manifest_members(uuid, uuid, jsonb)
    FROM PUBLIC;
-- aap-statement-break

-- Recheck current authority before NOWAIT locks, then freeze the Task source and
-- bulk/workbench eligibility facts for one admitted member.
CREATE FUNCTION aap_labeling_v1.lock_video_input_manifest_member(
    p_actor_id uuid,
    p_project_id uuid,
    p_task_id uuid,
    p_selection jsonb,
    p_captured_task jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_access jsonb;
    v_access_data jsonb;
    v_code text;
    v_initial record;
    v_task record;
    v_batch record;
    v_task_access jsonb;
    v_captured_binding jsonb;
    v_current_binding jsonb;
    v_snapshot text;
BEGIN
    IF p_actor_id IS NULL OR p_project_id IS NULL OR p_task_id IS NULL
       OR p_selection IS NULL OR p_captured_task IS NULL
       OR pg_catalog.octet_length(pg_catalog.convert_to(
            p_selection::text || p_captured_task::text, 'UTF8'
       )) > 65536
       OR NOT aap_labeling_v1._video_input_manifest_selection_valid(p_selection)
       OR NOT aap_labeling_v1._video_input_captured_task_valid(p_captured_task) THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_request');
    END IF;
    v_captured_binding := p_captured_task - 'batch_id';
    IF NOT aap_labeling_v1._video_input_task_binding_valid(v_captured_binding)
       AND p_captured_task -> 'dataset_item_id' <> 'null'::jsonb THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;
    IF p_captured_task ->> 'task_id' IS DISTINCT FROM p_task_id::text
       OR p_captured_task ->> 'project_id' IS DISTINCT FROM p_project_id::text
       OR (p_selection -> 'batch_id' <> 'null'::jsonb
           AND p_captured_task -> 'batch_id' IS DISTINCT FROM p_selection -> 'batch_id') THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;
    IF p_selection -> 'task_ids' <> 'null'::jsonb THEN
        IF NOT EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(p_selection -> 'task_ids') AS entry(value)
            WHERE entry.value #>> '{}' = p_task_id::text
        ) THEN
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
        END IF;
    END IF;

    v_access := aap_workspace_v1.lock_media_project_access(p_actor_id, p_project_id);
    IF v_access IS NULL
       OR v_access ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
       OR pg_catalog.jsonb_typeof(v_access -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
    END IF;
    IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_access #>> '{error,code}';
        IF v_code IN ('account_not_active', 'account_busy', 'invalid_account_role', 'task_busy') THEN
            RETURN aap_labeling_v1._video_input_selection_reply(
                NULL, CASE WHEN v_code = 'task_busy' THEN 'source_busy' ELSE v_code END
            );
        ELSIF v_code = 'project_missing' THEN
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_not_visible');
        END IF;
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
    END IF;
    v_access_data := v_access -> 'data';
    IF pg_catalog.jsonb_typeof(v_access_data) IS DISTINCT FROM 'object'
       OR (v_access_data - ARRAY[
            'actor_id', 'project_id', 'platform_role', 'project_role', 'is_manager',
            'is_super_admin', 'video_collaboration_enabled', 'project_name'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (v_access_data ?& ARRAY[
            'actor_id', 'project_id', 'platform_role', 'project_role', 'is_manager',
            'is_super_admin', 'video_collaboration_enabled', 'project_name'
       ])
       OR v_access_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR v_access_data ->> 'project_id' IS DISTINCT FROM p_project_id::text
       OR pg_catalog.jsonb_typeof(v_access_data -> 'platform_role') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_access_data -> 'project_role') NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(v_access_data -> 'is_manager') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
    END IF;
    IF pg_catalog.jsonb_typeof(v_access_data -> 'is_super_admin') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_access_data -> 'video_collaboration_enabled') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_access_data -> 'project_name') IS DISTINCT FROM 'string' THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
    END IF;
    IF v_access_data -> 'is_manager' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'permission_denied');
    END IF;

    SELECT task.id, task.project_id, task.dataset_item_id, task.batch_id,
           task.file_type, task.file_path, task.created_at, task.version,
           task.media_source_generation, task.status, task.total_predictions
    INTO v_initial
    FROM public.tasks AS task
    WHERE task.id = p_task_id;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'source_missing');
    END IF;
    IF v_initial.project_id IS DISTINCT FROM p_project_id THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;
    v_task_access := aap_labeling_v1._video_input_selection_task_access(
        p_actor_id, p_project_id, p_task_id, v_initial.dataset_item_id, v_initial.file_type
    );
    IF v_task_access IS NULL
       OR v_task_access ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-input-selection/v1'
       OR pg_catalog.jsonb_typeof(v_task_access -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
    END IF;
    IF v_task_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_task_access #>> '{error,code}';
        IF v_code IN (
            'account_not_active', 'account_busy', 'invalid_account_role',
            'task_busy', 'task_not_visible', 'permission_denied'
        ) THEN
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, v_code);
        END IF;
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
    END IF;
    v_access_data := v_task_access -> 'data';
    IF pg_catalog.jsonb_typeof(v_access_data) IS DISTINCT FROM 'object'
       OR (v_access_data - ARRAY['actor_id', 'project_id', 'task_id']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (v_access_data ?& ARRAY['actor_id', 'project_id', 'task_id'])
       OR v_access_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR v_access_data ->> 'project_id' IS DISTINCT FROM p_project_id::text
       OR v_access_data ->> 'task_id' IS DISTINCT FROM p_task_id::text THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
    END IF;

    v_snapshot := aap_labeling_v1._image_source_snapshot_fields(
        v_initial.id, v_initial.created_at, v_initial.project_id,
        v_initial.dataset_item_id, v_initial.file_type, v_initial.file_path,
        v_initial.media_source_generation
    );
    IF v_initial.batch_id IS DISTINCT FROM
            (CASE WHEN p_captured_task -> 'batch_id' = 'null'::jsonb
                 THEN NULL ELSE (p_captured_task ->> 'batch_id')::uuid END)
       OR (p_captured_task - ARRAY['batch_id', 'task_version'])
            IS DISTINCT FROM (pg_catalog.jsonb_build_object(
                'task_id', v_initial.id,
                'project_id', v_initial.project_id,
                'task_source_generation', v_initial.media_source_generation,
                'dataset_item_id', v_initial.dataset_item_id,
                'task_source_snapshot', v_snapshot,
                'file_path', v_initial.file_path,
                'file_type', v_initial.file_type
            )) THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;
    IF v_initial.status IS NULL OR v_initial.status = 'uploading' THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'source_not_ready');
    END IF;
    IF p_selection ->> 'execution_scope' = 'bulk'
       AND (v_initial.status IS DISTINCT FROM 'pending'
            OR (p_selection -> 'batch_id' <> 'null'::jsonb
                AND v_initial.batch_id IS DISTINCT FROM (p_selection ->> 'batch_id')::uuid)) THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    ELSIF p_selection ->> 'execution_scope' = 'workbench'
       AND v_initial.status NOT IN ('pending', 'in_progress', 'rejected') THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;
    IF p_selection ->> 'predict_mode' = 'skip_predicted'
       AND COALESCE(v_initial.total_predictions, 0) <> 0 THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;

    IF v_initial.batch_id IS NOT NULL THEN
        BEGIN
            SELECT batch.id, batch.project_id, batch.status, batch.admin_locked,
                   batch.annotator_id, batch.reviewer_id
            INTO v_batch
            FROM public.task_batches AS batch
            WHERE batch.id = v_initial.batch_id
            FOR SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_busy');
        END;
        IF NOT FOUND OR v_batch.project_id IS DISTINCT FROM p_project_id
           OR v_batch.admin_locked THEN
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
        END IF;
        IF p_selection ->> 'execution_scope' = 'bulk'
           AND NOT (
                v_batch.status = 'active'
                OR (v_batch.status = 'draft'
                    AND v_batch.annotator_id IS NULL
                    AND v_batch.reviewer_id IS NULL)
           ) THEN
            RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
        END IF;
    END IF;

    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended('aap:task-edit-lock:' || p_task_id::text, 0)
    ) THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_busy');
    END IF;
    BEGIN
        SELECT task.id, task.project_id, task.dataset_item_id, task.batch_id,
               task.file_type, task.file_path, task.created_at, task.version,
               task.media_source_generation, task.status, task.total_predictions
        INTO v_task
        FROM public.tasks AS task
        WHERE task.id = p_task_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'source_missing');
    END IF;
    v_snapshot := aap_labeling_v1._image_source_snapshot_fields(
        v_task.id, v_task.created_at, v_task.project_id, v_task.dataset_item_id,
        v_task.file_type, v_task.file_path, v_task.media_source_generation
    );
    v_current_binding := pg_catalog.jsonb_build_object(
        'task_id', v_task.id,
        'project_id', v_task.project_id,
        'task_version', v_task.version,
        'task_source_generation', v_task.media_source_generation,
        'dataset_item_id', v_task.dataset_item_id,
        'task_source_snapshot', v_snapshot,
        'file_path', v_task.file_path,
        'file_type', v_task.file_type
    );
    IF v_task.project_id IS DISTINCT FROM p_project_id
       OR v_task.batch_id IS DISTINCT FROM v_initial.batch_id
       OR (v_current_binding - 'task_version') IS DISTINCT FROM
          (v_captured_binding - 'task_version') THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;
    IF v_task.status IS NULL OR v_task.status = 'uploading' THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'source_not_ready');
    END IF;
    IF p_selection ->> 'execution_scope' = 'bulk'
       AND (v_task.status IS DISTINCT FROM 'pending'
            OR (p_selection -> 'batch_id' <> 'null'::jsonb
                AND v_task.batch_id IS DISTINCT FROM (p_selection ->> 'batch_id')::uuid)) THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    ELSIF p_selection ->> 'execution_scope' = 'workbench'
       AND v_task.status NOT IN ('pending', 'in_progress', 'rejected') THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;
    IF p_selection ->> 'predict_mode' = 'skip_predicted'
       AND COALESCE(v_task.total_predictions, 0) <> 0 THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.task_locks AS task_lock
        WHERE task_lock.task_id = p_task_id
          AND task_lock.expire_at > pg_catalog.clock_timestamp()
          AND (p_selection ->> 'execution_scope' = 'bulk'
               OR task_lock.user_id <> p_actor_id)
    ) THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;
    IF v_task.dataset_item_id IS NULL THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'source_missing');
    END IF;
    IF v_task.file_type IS DISTINCT FROM 'video' THEN
        RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
    END IF;
    RETURN aap_labeling_v1._video_input_selection_reply(
        pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'task', v_current_binding || pg_catalog.jsonb_build_object(
                'batch_id', v_task.batch_id
            ),
            'upload_ready', true
        ), NULL
    );
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'task_scope_changed');
WHEN OTHERS THEN
    RETURN aap_labeling_v1._video_input_selection_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_video_input_manifest_member(uuid, uuid, uuid, jsonb, jsonb)
    FROM PUBLIC;
