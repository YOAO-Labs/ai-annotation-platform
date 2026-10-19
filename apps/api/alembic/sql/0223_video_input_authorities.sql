-- Labeling and Jobs expose the narrow authorities consumed by video input admission.
CREATE FUNCTION aap_labeling_v1._video_input_task_source_reply(
    p_data jsonb, p_code text
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_code IS NULL THEN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-video-input-source/v1',
        'ok', true, 'data', p_data
    ) ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-video-input-source/v1',
        'ok', false, 'error', pg_catalog.jsonb_build_object('code', p_code)
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_input_task_source_reply(jsonb, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._video_input_task_binding_valid(p_binding jsonb)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE
        WHEN pg_catalog.jsonb_typeof(p_binding) IS DISTINCT FROM 'object' THEN false
        ELSE
            (p_binding - ARRAY[
                'task_id', 'project_id', 'task_version', 'task_source_generation',
                'dataset_item_id', 'task_source_snapshot', 'file_path', 'file_type'
            ]) = '{}'::jsonb
            AND p_binding ?& ARRAY[
                'task_id', 'project_id', 'task_version', 'task_source_generation',
                'dataset_item_id', 'task_source_snapshot', 'file_path', 'file_type'
            ]
            AND pg_catalog.jsonb_typeof(p_binding -> 'task_id') = 'string'
            AND p_binding ->> 'task_id' ~
                '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            AND pg_catalog.jsonb_typeof(p_binding -> 'project_id') = 'string'
            AND p_binding ->> 'project_id' ~
                '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            AND pg_catalog.jsonb_typeof(p_binding -> 'dataset_item_id') = 'string'
            AND p_binding ->> 'dataset_item_id' ~
                '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            AND pg_catalog.jsonb_typeof(p_binding -> 'task_version') = 'number'
            AND CASE
                WHEN (p_binding ->> 'task_version') ~ '^[1-9][0-9]*$'
                     AND pg_catalog.char_length(p_binding ->> 'task_version') <= 16
                THEN (p_binding ->> 'task_version')::numeric <= 9007199254740991
                ELSE false
            END
            AND pg_catalog.jsonb_typeof(p_binding -> 'task_source_generation') = 'number'
            AND CASE
                WHEN (p_binding ->> 'task_source_generation') ~ '^[1-9][0-9]*$'
                     AND pg_catalog.char_length(p_binding ->> 'task_source_generation') <= 16
                THEN (p_binding ->> 'task_source_generation')::numeric <= 9007199254740991
                ELSE false
            END
            AND pg_catalog.jsonb_typeof(p_binding -> 'task_source_snapshot') = 'string'
            AND p_binding ->> 'task_source_snapshot' ~ '^[0-9a-f]{64}$'
            AND pg_catalog.jsonb_typeof(p_binding -> 'file_path') = 'string'
            AND pg_catalog.char_length(p_binding ->> 'file_path') <= 1024
            AND p_binding ->> 'file_type' = 'video'
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_input_task_binding_valid(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.read_video_input_task_source(
    p_task_id uuid, p_actor_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_task record;
    v_reply jsonb;
    v_access_data jsonb;
    v_code text;
    v_binding jsonb;
    v_snapshot text;
    v_upload_ready boolean;
BEGIN
    IF p_task_id IS NULL OR p_actor_id IS NULL THEN
        RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_request');
    END IF;
    SELECT task.id, task.project_id, task.dataset_item_id, task.file_type,
           task.file_path, task.created_at, task.version,
           task.media_source_generation, task.status
    INTO v_task
    FROM public.tasks AS task
    WHERE task.id = p_task_id;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'task_not_visible');
    END IF;

    IF v_task.dataset_item_id IS NOT NULL THEN
        v_reply := aap_labeling_v1.read_media_task_access(
            p_actor_id, p_task_id, v_task.dataset_item_id
        );
        IF v_reply IS NULL
           OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-media-task-access/v1'
           OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_response');
        END IF;
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_code := v_reply #>> '{error,code}';
            IF v_code IN (
                'invalid_request', 'account_not_active', 'account_busy', 'task_busy',
                'invalid_account_role', 'permission_denied', 'task_not_visible'
            ) THEN
                RETURN aap_labeling_v1._video_input_task_source_reply(NULL, v_code);
            END IF;
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_response');
        END IF;
        v_access_data := v_reply -> 'data';
        IF pg_catalog.jsonb_typeof(v_access_data) IS DISTINCT FROM 'object'
           OR (v_access_data - ARRAY[
                'actor_id', 'task_id', 'dataset_item_id', 'project_id', 'file_type',
                'project_role', 'is_manager', 'annotation_write'
           ]) IS DISTINCT FROM '{}'::jsonb
           OR NOT (v_access_data ?& ARRAY[
                'actor_id', 'task_id', 'dataset_item_id', 'project_id', 'file_type',
                'project_role', 'is_manager', 'annotation_write'
           ])
           OR v_access_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
           OR v_access_data ->> 'task_id' IS DISTINCT FROM p_task_id::text
           OR v_access_data ->> 'dataset_item_id' IS DISTINCT FROM v_task.dataset_item_id::text
           OR v_access_data ->> 'project_id' IS DISTINCT FROM v_task.project_id::text
           OR v_access_data ->> 'file_type' IS DISTINCT FROM v_task.file_type THEN
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'source_changed');
        END IF;
    ELSE
        -- With no linked item the canonical media access projection cannot be called.
        -- Report absence only where the current Workspace facts establish visibility.
        v_reply := aap_workspace_v1.read_media_project_access(
            p_actor_id, v_task.project_id
        );
        IF v_reply IS NULL
           OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
           OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_response');
        END IF;
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_code := v_reply #>> '{error,code}';
            IF v_code IN (
                'invalid_request', 'account_not_active', 'account_busy', 'task_busy',
                'invalid_account_role'
            ) THEN
                RETURN aap_labeling_v1._video_input_task_source_reply(NULL, v_code);
            END IF;
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'task_not_visible');
        END IF;
        v_access_data := v_reply -> 'data';
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
           OR v_access_data ->> 'project_id' IS DISTINCT FROM v_task.project_id::text
           OR pg_catalog.jsonb_typeof(v_access_data -> 'platform_role') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_access_data -> 'is_manager') IS DISTINCT FROM 'boolean'
           OR pg_catalog.jsonb_typeof(v_access_data -> 'is_super_admin') IS DISTINCT FROM 'boolean'
           OR pg_catalog.jsonb_typeof(v_access_data -> 'video_collaboration_enabled')
                IS DISTINCT FROM 'boolean' THEN
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_response');
        END IF;
        IF v_access_data -> 'is_manager' IS DISTINCT FROM 'true'::jsonb
           AND NOT (
                v_task.file_type = 'video'
                AND v_access_data -> 'video_collaboration_enabled' = 'true'::jsonb
           ) THEN
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'task_not_visible');
        END IF;
        IF v_task.file_type IS DISTINCT FROM 'video' THEN
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'not_video');
        END IF;
        RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'source_missing');
    END IF;

    IF v_task.file_type IS DISTINCT FROM 'video' THEN
        RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'not_video');
    END IF;
    IF v_task.file_path IS NULL OR pg_catalog.char_length(v_task.file_path) > 1024 THEN
        RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'source_missing');
    END IF;
    IF v_task.project_id IS NULL OR v_task.dataset_item_id IS NULL
       OR v_task.version IS NULL OR v_task.version <= 0
       OR v_task.media_source_generation IS NULL OR v_task.media_source_generation <= 0
       OR v_task.created_at IS NULL THEN
        RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_response');
    END IF;
    v_snapshot := aap_labeling_v1._image_source_snapshot_fields(
        v_task.id, v_task.created_at, v_task.project_id, v_task.dataset_item_id,
        v_task.file_type, v_task.file_path, v_task.media_source_generation
    );
    v_binding := pg_catalog.jsonb_build_object(
        'task_id', v_task.id,
        'project_id', v_task.project_id,
        'task_version', v_task.version,
        'task_source_generation', v_task.media_source_generation,
        'dataset_item_id', v_task.dataset_item_id,
        'task_source_snapshot', v_snapshot,
        'file_path', v_task.file_path,
        'file_type', v_task.file_type
    );
    IF NOT aap_labeling_v1._video_input_task_binding_valid(v_binding) THEN
        RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_response');
    END IF;
    v_upload_ready := v_task.status IS NOT NULL
        AND v_task.status IS DISTINCT FROM 'uploading';
    RETURN aap_labeling_v1._video_input_task_source_reply(
        pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'binding', v_binding,
            'upload_ready', v_upload_ready
        ), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.read_video_input_task_source(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.lock_video_input_task_source(
    p_task_id uuid, p_actor_id uuid, p_expected_binding jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_task record;
    v_reply jsonb;
    v_access_data jsonb;
    v_project_access jsonb;
    v_authorized_project_id uuid;
    v_code text;
    v_binding jsonb;
    v_snapshot text;
    v_upload_ready boolean;
    v_locked_project_id uuid;
BEGIN
    IF p_task_id IS NULL OR p_actor_id IS NULL
       OR NOT aap_labeling_v1._video_input_task_binding_valid(p_expected_binding)
       OR p_expected_binding ->> 'task_id' IS DISTINCT FROM p_task_id::text THEN
        RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_request');
    END IF;
    SELECT task.id, task.project_id, task.dataset_item_id, task.file_type,
           task.file_path, task.created_at, task.version,
           task.media_source_generation, task.status
    INTO v_task
    FROM public.tasks AS task
    WHERE task.id = p_task_id;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'task_not_visible');
    END IF;

    IF v_task.dataset_item_id IS NOT NULL THEN
        v_reply := aap_labeling_v1.read_media_task_access(
            p_actor_id, p_task_id, v_task.dataset_item_id
        );
        IF v_reply IS NULL
           OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-media-task-access/v1'
           OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_response');
        END IF;
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_code := v_reply #>> '{error,code}';
            IF v_code IN (
                'invalid_request', 'account_not_active', 'account_busy', 'task_busy',
                'invalid_account_role', 'permission_denied', 'task_not_visible'
            ) THEN
                RETURN aap_labeling_v1._video_input_task_source_reply(NULL, v_code);
            END IF;
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_response');
        END IF;
        v_access_data := v_reply -> 'data';
        IF pg_catalog.jsonb_typeof(v_access_data) IS DISTINCT FROM 'object'
           OR (v_access_data - ARRAY[
                'actor_id', 'task_id', 'dataset_item_id', 'project_id', 'file_type',
                'project_role', 'is_manager', 'annotation_write'
           ]) IS DISTINCT FROM '{}'::jsonb
           OR NOT (v_access_data ?& ARRAY[
                'actor_id', 'task_id', 'dataset_item_id', 'project_id', 'file_type',
                'project_role', 'is_manager', 'annotation_write'
           ])
           OR v_access_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
           OR v_access_data ->> 'task_id' IS DISTINCT FROM p_task_id::text
           OR v_access_data ->> 'dataset_item_id' IS DISTINCT FROM v_task.dataset_item_id::text
           OR pg_catalog.jsonb_typeof(v_access_data -> 'project_id') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_access_data -> 'file_type') IS DISTINCT FROM 'string' THEN
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_response');
        END IF;
        v_authorized_project_id := (v_access_data ->> 'project_id')::uuid;
    ELSE
        v_project_access := aap_workspace_v1.read_media_project_access(
            p_actor_id, v_task.project_id
        );
        IF v_project_access IS NULL
           OR v_project_access ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
           OR pg_catalog.jsonb_typeof(v_project_access -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_response');
        END IF;
        IF v_project_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_code := v_project_access #>> '{error,code}';
            IF v_code IN (
                'invalid_request', 'account_not_active', 'account_busy', 'task_busy',
                'invalid_account_role'
            ) THEN
                RETURN aap_labeling_v1._video_input_task_source_reply(NULL, v_code);
            END IF;
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'task_not_visible');
        END IF;
        v_access_data := v_project_access -> 'data';
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
           OR v_access_data ->> 'project_id' IS DISTINCT FROM v_task.project_id::text
           OR pg_catalog.jsonb_typeof(v_access_data -> 'is_manager') IS DISTINCT FROM 'boolean'
           OR pg_catalog.jsonb_typeof(v_access_data -> 'video_collaboration_enabled')
                IS DISTINCT FROM 'boolean' THEN
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_response');
        END IF;
        IF v_access_data -> 'is_manager' IS DISTINCT FROM 'true'::jsonb
           AND NOT (
                v_task.file_type = 'video'
                AND v_access_data -> 'video_collaboration_enabled' = 'true'::jsonb
           ) THEN
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'task_not_visible');
        END IF;
        v_authorized_project_id := v_task.project_id;
    END IF;

    v_reply := aap_workspace_v1.lock_media_project_access(
        p_actor_id, v_authorized_project_id
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_reply #>> '{error,code}';
        IF v_code IN (
            'invalid_request', 'account_not_active', 'account_busy', 'task_busy',
            'invalid_account_role'
        ) THEN
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, v_code);
        END IF;
        RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'task_not_visible');
    END IF;
    v_access_data := v_reply -> 'data';
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
       OR v_access_data ->> 'project_id' IS DISTINCT FROM v_authorized_project_id::text
       OR pg_catalog.jsonb_typeof(v_access_data -> 'is_manager') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_access_data -> 'video_collaboration_enabled')
            IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_response');
    END IF;
    v_locked_project_id := (v_access_data ->> 'project_id')::uuid;
    BEGIN
        SELECT task.id, task.project_id, task.dataset_item_id, task.file_type,
               task.file_path, task.created_at, task.version,
               task.media_source_generation, task.status
        INTO v_task
        FROM public.tasks AS task
        WHERE task.id = p_task_id
        FOR SHARE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'task_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'task_not_visible');
    END IF;
    IF v_task.dataset_item_id IS NOT NULL THEN
        v_reply := aap_labeling_v1.read_media_task_access(
            p_actor_id, p_task_id, v_task.dataset_item_id
        );
        IF v_reply IS NULL
           OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-media-task-access/v1'
           OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_response');
        END IF;
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_code := v_reply #>> '{error,code}';
            IF v_code IN (
                'invalid_request', 'account_not_active', 'account_busy', 'task_busy',
                'invalid_account_role', 'permission_denied', 'task_not_visible'
            ) THEN
                RETURN aap_labeling_v1._video_input_task_source_reply(NULL, v_code);
            END IF;
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_response');
        END IF;
        v_access_data := v_reply -> 'data';
        IF pg_catalog.jsonb_typeof(v_access_data) IS DISTINCT FROM 'object'
           OR (v_access_data - ARRAY[
                'actor_id', 'task_id', 'dataset_item_id', 'project_id', 'file_type',
                'project_role', 'is_manager', 'annotation_write'
           ]) IS DISTINCT FROM '{}'::jsonb
           OR NOT (v_access_data ?& ARRAY[
                'actor_id', 'task_id', 'dataset_item_id', 'project_id', 'file_type',
                'project_role', 'is_manager', 'annotation_write'
           ])
           OR v_access_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
           OR v_access_data ->> 'task_id' IS DISTINCT FROM p_task_id::text
           OR v_access_data ->> 'dataset_item_id' IS DISTINCT FROM v_task.dataset_item_id::text
           OR v_access_data ->> 'project_id' IS DISTINCT FROM v_task.project_id::text
           OR v_access_data ->> 'file_type' IS DISTINCT FROM v_task.file_type THEN
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'source_changed');
        END IF;
    ELSE
        v_reply := aap_workspace_v1.read_media_project_access(
            p_actor_id, v_task.project_id
        );
        IF v_reply IS NULL
           OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
           OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_response');
        END IF;
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_code := v_reply #>> '{error,code}';
            IF v_code IN (
                'invalid_request', 'account_not_active', 'account_busy', 'task_busy',
                'invalid_account_role'
            ) THEN
                RETURN aap_labeling_v1._video_input_task_source_reply(NULL, v_code);
            END IF;
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'task_not_visible');
        END IF;
        v_access_data := v_reply -> 'data';
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
           OR v_access_data ->> 'project_id' IS DISTINCT FROM v_task.project_id::text
           OR pg_catalog.jsonb_typeof(v_access_data -> 'is_manager') IS DISTINCT FROM 'boolean'
           OR pg_catalog.jsonb_typeof(v_access_data -> 'video_collaboration_enabled')
                IS DISTINCT FROM 'boolean' THEN
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_response');
        END IF;
        IF v_access_data -> 'is_manager' IS DISTINCT FROM 'true'::jsonb
           AND NOT (
                v_task.file_type = 'video'
                AND v_access_data -> 'video_collaboration_enabled' = 'true'::jsonb
           ) THEN
            RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'task_not_visible');
        END IF;
    END IF;

    IF v_task.project_id IS DISTINCT FROM v_locked_project_id THEN
        RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'source_changed');
    END IF;

    IF v_task.dataset_item_id IS NULL OR v_task.file_type IS DISTINCT FROM 'video'
       OR v_task.file_path IS NULL OR pg_catalog.char_length(v_task.file_path) > 1024 THEN
        RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'source_changed');
    END IF;
    IF v_task.project_id IS NULL OR v_task.version IS NULL OR v_task.version <= 0
       OR v_task.media_source_generation IS NULL OR v_task.media_source_generation <= 0
       OR v_task.created_at IS NULL THEN
        RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_response');
    END IF;
    v_snapshot := aap_labeling_v1._image_source_snapshot_fields(
        v_task.id, v_task.created_at, v_task.project_id, v_task.dataset_item_id,
        v_task.file_type, v_task.file_path, v_task.media_source_generation
    );
    v_binding := pg_catalog.jsonb_build_object(
        'task_id', v_task.id,
        'project_id', v_task.project_id,
        'task_version', v_task.version,
        'task_source_generation', v_task.media_source_generation,
        'dataset_item_id', v_task.dataset_item_id,
        'task_source_snapshot', v_snapshot,
        'file_path', v_task.file_path,
        'file_type', v_task.file_type
    );
    IF NOT aap_labeling_v1._video_input_task_binding_valid(v_binding) THEN
        RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_response');
    END IF;
    IF (v_binding - 'task_version') IS DISTINCT FROM
       (p_expected_binding - 'task_version') THEN
        RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'source_changed');
    END IF;
    v_upload_ready := v_task.status IS NOT NULL
        AND v_task.status IS DISTINCT FROM 'uploading';
    IF NOT v_upload_ready THEN
        RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'source_not_ready');
    END IF;
    RETURN aap_labeling_v1._video_input_task_source_reply(
        pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'binding', v_binding,
            'upload_ready', true
        ), NULL
    );
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'source_changed');
WHEN OTHERS THEN
    RETURN aap_labeling_v1._video_input_task_source_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_video_input_task_source(uuid, uuid, jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1._video_input_job_reply(p_data jsonb, p_code text)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_code IS NULL THEN pg_catalog.jsonb_build_object(
        'schema', 'aap-jobs-video-input-job/v1', 'ok', true, 'data', p_data
    ) ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-jobs-video-input-job/v1', 'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._video_input_job_reply(jsonb, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1._video_input_selection_valid(p_selection jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL UNSAFE
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
       OR p_selection ->> 'predict_mode' NOT IN ('skip_predicted', 'overwrite', 'append')
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
REVOKE ALL ON FUNCTION aap_jobs_v1._video_input_selection_valid(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.project_video_input_job(
    p_job_id uuid,
    p_task_id uuid,
    p_actor_id uuid,
    p_project_id uuid,
    p_selection jsonb,
    p_selection_digest text,
    p_require_active boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job_id uuid;
    v_kind text;
    v_celery_task_id text;
    v_actor_id uuid;
    v_project_id uuid;
    v_retained_project_id uuid;
    v_status text;
    v_payload jsonb;
    v_cancel_requested boolean := false;
    v_active boolean;
BEGIN
    IF p_job_id IS NULL OR p_actor_id IS NULL OR p_project_id IS NULL
       OR p_require_active IS NULL
       OR (p_selection_digest IS NOT NULL
           AND p_selection_digest !~ '^[0-9a-f]{64}$')
       OR aap_jobs_v1._video_input_selection_valid(p_selection) IS DISTINCT FROM true THEN
        RETURN aap_jobs_v1._video_input_job_reply(NULL, 'invalid_request');
    END IF;
    IF p_task_id IS NOT NULL AND p_selection -> 'task_ids' <> 'null'::jsonb
       AND NOT EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(p_selection -> 'task_ids') AS entry(value)
            WHERE entry.value #>> '{}' = p_task_id::text
       ) THEN
        RETURN aap_jobs_v1._video_input_job_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT job.id, job.kind, job.celery_task_id, job.user_id,
               job.project_id, job.retained_project_id, job.status, job.payload
        INTO v_job_id, v_kind, v_celery_task_id, v_actor_id,
             v_project_id, v_retained_project_id, v_status, v_payload
        FROM public.async_jobs AS job
        WHERE job.id = p_job_id
        FOR SHARE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_jobs_v1._video_input_job_reply(NULL, 'job_busy');
    END;
    IF NOT FOUND THEN
        IF p_require_active THEN
            RETURN aap_jobs_v1._video_input_job_reply(NULL, 'job_not_active');
        END IF;
        RETURN aap_jobs_v1._video_input_job_reply(
            pg_catalog.jsonb_build_object('job', NULL), NULL
        );
    END IF;
    IF v_kind IS DISTINCT FROM 'batch_predict'
       OR v_celery_task_id IS DISTINCT FROM v_job_id::text
       OR pg_catalog.jsonb_typeof(v_payload) IS DISTINCT FROM 'object'
       OR v_payload ->> 'execution_unit' IS DISTINCT FROM 'frame'
       OR v_payload ->> 'execution_scope'
            IS DISTINCT FROM (p_selection ->> 'execution_scope')
       OR v_payload ->> 'accepted_video_inputs_protocol'
            IS DISTINCT FROM 'aap-assets-video-inputs/v1'
       OR v_payload ->> 'accepted_video_manifest_id' IS DISTINCT FROM v_job_id::text
       OR v_payload -> 'accepted_video_selection' IS DISTINCT FROM p_selection
       OR NOT (v_payload ? 'batch_id')
       OR (v_payload -> 'batch_id') IS DISTINCT FROM (p_selection -> 'batch_id')
       OR v_payload ?| ARRAY[
            'accepted_inputs_protocol', 'accepted_input_ids',
            'accepted_input_manifest_protocol', 'accepted_input_manifest_id',
            'accepted_input_manifest_digest'
       ] THEN
        RETURN aap_jobs_v1._video_input_job_reply(NULL, 'manifest_scope_mismatch');
    END IF;
    IF v_payload ? 'accepted_video_selection_digest' THEN
        IF pg_catalog.jsonb_typeof(v_payload -> 'accepted_video_selection_digest')
                IS DISTINCT FROM 'string'
           OR v_payload ->> 'accepted_video_selection_digest' !~ '^[0-9a-f]{64}$'
           OR (p_selection_digest IS NOT NULL
               AND v_payload ->> 'accepted_video_selection_digest'
                    IS DISTINCT FROM p_selection_digest) THEN
            RETURN aap_jobs_v1._video_input_job_reply(NULL, 'manifest_scope_mismatch');
        END IF;
    ELSIF p_selection_digest IS NOT NULL THEN
        RETURN aap_jobs_v1._video_input_job_reply(NULL, 'manifest_scope_mismatch');
    END IF;
    IF p_selection -> 'task_ids' = 'null'::jsonb THEN
        IF v_payload ? 'task_ids' OR v_payload ? 'scope' THEN
            RETURN aap_jobs_v1._video_input_job_reply(NULL, 'manifest_scope_mismatch');
        END IF;
    ELSIF pg_catalog.jsonb_typeof(v_payload -> 'task_ids') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_payload -> 'scope') IS DISTINCT FROM 'object'
       OR ((v_payload -> 'scope') - 'task_ids') IS DISTINCT FROM '{}'::jsonb
       OR (v_payload -> 'task_ids') IS DISTINCT FROM (p_selection -> 'task_ids')
       OR (v_payload -> 'scope' -> 'task_ids') IS DISTINCT FROM (p_selection -> 'task_ids') THEN
        RETURN aap_jobs_v1._video_input_job_reply(NULL, 'manifest_scope_mismatch');
    END IF;
    IF (v_actor_id IS NOT NULL AND v_actor_id IS DISTINCT FROM p_actor_id)
       OR (v_project_id IS NOT NULL AND v_project_id IS DISTINCT FROM p_project_id)
       OR (v_project_id IS NULL AND v_retained_project_id IS DISTINCT FROM p_project_id)
       OR (v_retained_project_id IS NOT NULL
           AND v_retained_project_id IS DISTINCT FROM p_project_id) THEN
        RETURN aap_jobs_v1._video_input_job_reply(NULL, 'manifest_scope_mismatch');
    END IF;
    IF v_status NOT IN ('pending', 'running', 'completed', 'failed', 'cancelled') THEN
        RETURN aap_jobs_v1._video_input_job_reply(NULL, 'invalid_response');
    END IF;
    IF v_payload ? 'cancel_requested' THEN
        IF pg_catalog.jsonb_typeof(v_payload -> 'cancel_requested') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_jobs_v1._video_input_job_reply(NULL, 'invalid_response');
        END IF;
        v_cancel_requested := v_payload -> 'cancel_requested' = 'true'::jsonb;
    END IF;
    v_active := COALESCE(
        v_actor_id = p_actor_id AND v_project_id = p_project_id
        AND v_status IN ('pending', 'running') AND NOT v_cancel_requested,
        false
    );
    IF p_require_active AND NOT v_active THEN
        RETURN aap_jobs_v1._video_input_job_reply(NULL, 'job_not_active');
    END IF;
    RETURN aap_jobs_v1._video_input_job_reply(
        pg_catalog.jsonb_build_object(
            'job', pg_catalog.jsonb_build_object(
                'job_id', v_job_id,
                'task_id', p_task_id,
                'actor_id', v_actor_id,
                'project_id', v_project_id,
                'retained_project_id', v_retained_project_id,
                'selection', p_selection,
                'status', v_status,
                'cancel_requested', v_cancel_requested,
                'active', v_active
            )
        ), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_jobs_v1._video_input_job_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.project_video_input_job(
    uuid, uuid, uuid, uuid, jsonb, text, boolean
) FROM PUBLIC;
