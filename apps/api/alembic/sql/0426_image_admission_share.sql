-- Admission reads Task authority under SHARE; mutating source operations retain UPDATE.
CREATE FUNCTION aap_labeling_v1.read_image_task_source_share(
    p_task_id uuid,
    p_actor_id uuid,
    p_lock_source boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_task public.tasks%ROWTYPE;
    v_project_id uuid;
    v_access jsonb;
    v_access_data jsonb;
    v_access_code text;
    v_project_role text;
    v_manager boolean;
    v_batch_status text;
    v_batch_annotator_id uuid;
    v_visible boolean := false;
    v_assigned boolean := false;
    v_data jsonb;
    v_upload_ready boolean;
    v_missing_code text;
BEGIN
    IF p_task_id IS NULL OR p_lock_source IS NULL THEN
        RETURN aap_labeling_v1._image_source_reply(NULL, 'invalid_request');
    END IF;
    v_missing_code := CASE
        WHEN p_actor_id IS NULL THEN 'source_missing'
        ELSE 'task_not_visible'
    END;

    IF p_lock_source THEN
        SELECT task.project_id
        INTO v_project_id
        FROM public.tasks AS task
        WHERE task.id = p_task_id;
        IF NOT FOUND THEN
            RETURN aap_labeling_v1._image_source_reply(NULL, v_missing_code);
        END IF;
        IF p_actor_id IS NOT NULL THEN
            v_access := aap_workspace_v1.read_media_project_access(
                p_actor_id, v_project_id
            );
            v_access_code := aap_labeling_v1._image_workspace_failure(v_access);
            IF v_access_code IS NOT NULL THEN
                RETURN aap_labeling_v1._image_source_reply(NULL, v_access_code);
            END IF;
        END IF;
        BEGIN
            SELECT task.id, task.project_id, task.dataset_item_id, task.batch_id,
                   task.status, task.file_type, task.file_path, task.file_name,
                   task.created_at, task.assignee_id, task.reviewer_id,
                   task.media_source_generation, task.media_object_identity,
                   task.media_source_drifted, task.thumbnail_path, task.blurhash
            INTO v_task.id, v_task.project_id, v_task.dataset_item_id, v_task.batch_id,
                 v_task.status, v_task.file_type, v_task.file_path, v_task.file_name,
                 v_task.created_at, v_task.assignee_id, v_task.reviewer_id,
                 v_task.media_source_generation, v_task.media_object_identity,
                 v_task.media_source_drifted, v_task.thumbnail_path, v_task.blurhash
            FROM public.tasks AS task
            WHERE task.id = p_task_id
            FOR SHARE NOWAIT;
        EXCEPTION
            WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN aap_labeling_v1._image_source_reply(NULL, 'source_busy');
        END;
        IF NOT FOUND THEN
            RETURN aap_labeling_v1._image_source_reply(NULL, v_missing_code);
        END IF;
        IF v_task.project_id IS DISTINCT FROM v_project_id THEN
            RETURN aap_labeling_v1._image_source_reply(NULL, 'source_changed');
        END IF;
    ELSE
        SELECT task.id, task.project_id, task.dataset_item_id, task.batch_id,
               task.status, task.file_type, task.file_path, task.file_name,
               task.created_at, task.assignee_id, task.reviewer_id,
               task.media_source_generation, task.media_object_identity,
               task.media_source_drifted, task.thumbnail_path, task.blurhash
        INTO v_task.id, v_task.project_id, v_task.dataset_item_id, v_task.batch_id,
             v_task.status, v_task.file_type, v_task.file_path, v_task.file_name,
             v_task.created_at, v_task.assignee_id, v_task.reviewer_id,
             v_task.media_source_generation, v_task.media_object_identity,
             v_task.media_source_drifted, v_task.thumbnail_path, v_task.blurhash
        FROM public.tasks AS task
        WHERE task.id = p_task_id;
        IF NOT FOUND THEN
            RETURN aap_labeling_v1._image_source_reply(NULL, v_missing_code);
        END IF;
        IF p_actor_id IS NOT NULL THEN
            v_access := aap_workspace_v1.read_media_project_access(
                p_actor_id, v_task.project_id
            );
            v_access_code := aap_labeling_v1._image_workspace_failure(v_access);
            IF v_access_code IS NOT NULL THEN
                RETURN aap_labeling_v1._image_source_reply(NULL, v_access_code);
            END IF;
        END IF;
    END IF;

    IF p_actor_id IS NOT NULL THEN
        v_access_data := v_access -> 'data';
        IF pg_catalog.jsonb_typeof(v_access_data) IS DISTINCT FROM 'object'
           OR (v_access_data - ARRAY[
                'actor_id', 'project_id', 'platform_role', 'project_role',
                'is_manager', 'is_super_admin', 'video_collaboration_enabled',
                'project_name'
           ]) IS DISTINCT FROM '{}'::jsonb
           OR NOT (v_access_data ? 'project_role')
           OR v_access_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
           OR v_access_data ->> 'project_id' IS DISTINCT FROM v_task.project_id::text
           OR pg_catalog.jsonb_typeof(v_access_data -> 'platform_role') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_access_data -> 'is_manager') IS DISTINCT FROM 'boolean'
           OR pg_catalog.jsonb_typeof(v_access_data -> 'is_super_admin') IS DISTINCT FROM 'boolean'
           OR pg_catalog.jsonb_typeof(v_access_data -> 'video_collaboration_enabled') IS DISTINCT FROM 'boolean'
           OR pg_catalog.jsonb_typeof(v_access_data -> 'project_name') IS DISTINCT FROM 'string'
           OR (
                v_access_data -> 'project_role' IS DISTINCT FROM 'null'::jsonb
                AND pg_catalog.jsonb_typeof(v_access_data -> 'project_role') IS DISTINCT FROM 'string'
           ) THEN
            RETURN aap_labeling_v1._image_source_reply(NULL, 'invalid_response');
        END IF;
        v_project_role := v_access_data ->> 'project_role';
        v_manager := (v_access_data ->> 'is_manager')::boolean;
        IF v_access_data ->> 'platform_role' NOT IN (
                'super_admin', 'project_admin', 'employee', 'viewer'
           )
           OR (v_manager AND v_access_data -> 'project_role' <> 'null'::jsonb)
           OR (v_manager AND v_access_data ->> 'platform_role' NOT IN (
                'super_admin', 'project_admin'
           ))
           OR (NOT v_manager AND (
                v_project_role IS NULL
                OR v_project_role NOT IN ('annotator', 'reviewer', 'viewer')
           ))
           OR ((v_access_data ->> 'is_super_admin')::boolean
                IS DISTINCT FROM (v_access_data ->> 'platform_role' = 'super_admin')) THEN
            RETURN aap_labeling_v1._image_source_reply(NULL, 'invalid_response');
        END IF;
        IF v_manager THEN
            v_visible := true;
        ELSIF v_project_role = 'annotator'
           OR v_project_role = 'reviewer'
           OR v_project_role = 'viewer' THEN
            IF v_task.batch_id IS NULL THEN
                v_visible := (v_project_role = 'annotator'
                    AND v_task.assignee_id = p_actor_id)
                    OR (v_project_role = 'reviewer'
                    AND v_task.reviewer_id = p_actor_id);
            ELSE
                SELECT batch.status, batch.annotator_id
                INTO v_batch_status, v_batch_annotator_id
                FROM public.task_batches AS batch
                WHERE batch.id = v_task.batch_id;
                IF FOUND THEN
                    IF v_project_role = 'reviewer' THEN
                        v_visible := v_batch_status IN ('active', 'annotating', 'reviewing');
                    ELSE
                        v_assigned := (
                            v_task.assignee_id IS NOT NULL
                            AND v_task.assignee_id = p_actor_id
                        ) OR (
                            v_task.assignee_id IS NULL
                            AND v_batch_annotator_id = p_actor_id
                        );
                        v_visible := v_batch_status IN ('active', 'annotating', 'rejected');
                        IF v_batch_status = 'reviewing'
                           AND v_project_role = 'annotator'
                           AND COALESCE(v_task.assignee_id, v_batch_annotator_id) = p_actor_id
                           AND v_task.status IN ('rejected', 'in_progress') THEN
                            v_visible := true;
                        END IF;
                        IF v_batch_status = 'rejected' AND NOT v_assigned THEN
                            v_visible := false;
                        END IF;
                        IF v_task.assignee_id IS NOT NULL
                           AND v_task.assignee_id <> p_actor_id THEN
                            v_visible := false;
                        END IF;
                        IF v_batch_annotator_id IS NOT NULL AND NOT v_assigned THEN
                            v_visible := false;
                        END IF;
                    END IF;
                END IF;
            END IF;
        END IF;
        IF NOT v_visible THEN
            RETURN aap_labeling_v1._image_source_reply(NULL, 'task_not_visible');
        END IF;
    END IF;

    IF v_task.file_type IS DISTINCT FROM 'image' THEN
        RETURN aap_labeling_v1._image_source_reply(NULL, 'not_image');
    END IF;
    v_upload_ready := v_task.status IS NOT NULL
        AND v_task.status IS DISTINCT FROM 'uploading';
    IF p_actor_id IS NULL AND NOT v_upload_ready THEN
        RETURN aap_labeling_v1._image_source_reply(NULL, 'source_not_ready');
    END IF;
    IF v_task.media_source_generation IS NULL OR v_task.media_source_generation <= 0
       OR v_task.media_source_drifted IS NULL
       OR (v_task.media_object_identity IS NOT NULL
           AND NOT aap_labeling_v1._image_object_identity_valid(v_task.media_object_identity)) THEN
        RETURN aap_labeling_v1._image_source_reply(NULL, 'invalid_response');
    END IF;

    v_data := pg_catalog.jsonb_build_object(
        'task_id', v_task.id,
        'project_id', v_task.project_id,
        'dataset_item_id', v_task.dataset_item_id,
        'file_type', v_task.file_type,
        'file_path', v_task.file_path,
        'file_name', v_task.file_name,
        'created_at', v_task.created_at,
        'source_generation', v_task.media_source_generation,
        'source_snapshot', aap_labeling_v1._image_source_snapshot(v_task),
        'object_identity', v_task.media_object_identity,
        'source_drifted', v_task.media_source_drifted,
        'upload_ready', v_upload_ready,
        'thumbnail_path', v_task.thumbnail_path,
        'blurhash', v_task.blurhash,
        'thumbnail_source_generation', CASE
            WHEN v_task.thumbnail_path IS NOT NULL
             AND aap_labeling_v1._image_thumbnail_key_valid(v_task.thumbnail_path)
             AND v_task.media_object_identity IS NOT NULL
             AND NOT v_task.media_source_drifted
                THEN v_task.media_source_generation
            ELSE NULL
        END
    );
    RETURN aap_labeling_v1._image_source_reply(v_data, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.read_image_task_source_share(uuid, uuid, boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._image_labeling_task_share(
    p_task_id uuid, p_actor_id uuid, p_lock_source boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_data jsonb;
    v_project_id uuid;
    v_workspace jsonb;
    v_workspace_data jsonb;
BEGIN
    v_reply := aap_labeling_v1.read_image_task_source(
        p_task_id, p_actor_id,
        CASE WHEN p_actor_id IS NOT NULL AND p_lock_source THEN false ELSE p_lock_source END
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-image-source/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_reply #>> '{error,code}'
            WHEN 'account_not_active' THEN RETURN aap_assets_v1._image_reply(NULL, 'account_not_active');
            WHEN 'account_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'account_busy');
            WHEN 'task_not_visible' THEN RETURN aap_assets_v1._image_reply(NULL, 'task_not_visible');
            WHEN 'source_missing' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_missing');
            WHEN 'source_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
            WHEN 'source_changed' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
            WHEN 'source_not_ready' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_not_ready');
            WHEN 'not_image' THEN RETURN aap_assets_v1._image_reply(NULL, 'not_image');
            WHEN 'invalid_account_role' THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_account_role');
            ELSE RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END CASE;
    END IF;
    v_data := v_reply -> 'data';
    IF aap_assets_v1._image_task_source_json(v_data) IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    IF p_actor_id IS NOT NULL AND p_lock_source THEN
        IF pg_catalog.jsonb_typeof(v_data -> 'project_id') IS DISTINCT FROM 'string' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        BEGIN
            v_project_id := (v_data ->> 'project_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END;
        v_workspace := aap_workspace_v1.lock_media_project_access(
            p_actor_id, v_project_id
        );
        IF v_workspace IS NULL
           OR v_workspace ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
           OR pg_catalog.jsonb_typeof(v_workspace -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_workspace -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            CASE v_workspace #>> '{error,code}'
                WHEN 'account_not_active' THEN RETURN aap_assets_v1._image_reply(NULL, 'account_not_active');
                WHEN 'account_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'account_busy');
                WHEN 'task_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
                WHEN 'project_missing' THEN RETURN aap_assets_v1._image_reply(NULL, 'task_not_visible');
                WHEN 'permission_denied' THEN RETURN aap_assets_v1._image_reply(NULL, 'task_not_visible');
                WHEN 'invalid_account_role' THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_account_role');
                ELSE RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END CASE;
        END IF;
        v_workspace_data := v_workspace -> 'data';
        IF pg_catalog.jsonb_typeof(v_workspace_data) IS DISTINCT FROM 'object'
           OR v_workspace_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
           OR v_workspace_data ->> 'project_id' IS DISTINCT FROM v_project_id::text
           OR pg_catalog.jsonb_typeof(v_workspace_data -> 'platform_role') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_workspace_data -> 'is_manager') IS DISTINCT FROM 'boolean'
           OR (v_workspace_data -> 'project_role' <> 'null'::jsonb
               AND pg_catalog.jsonb_typeof(v_workspace_data -> 'project_role') IS DISTINCT FROM 'string') THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        v_reply := aap_labeling_v1.read_image_task_source_share(p_task_id, p_actor_id, true);
        IF v_reply IS NULL
           OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-image-source/v1'
           OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            CASE v_reply #>> '{error,code}'
                WHEN 'account_not_active' THEN RETURN aap_assets_v1._image_reply(NULL, 'account_not_active');
                WHEN 'account_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'account_busy');
                WHEN 'task_not_visible' THEN RETURN aap_assets_v1._image_reply(NULL, 'task_not_visible');
                WHEN 'source_missing' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_missing');
                WHEN 'source_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
                WHEN 'source_changed' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
                WHEN 'source_not_ready' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_not_ready');
                WHEN 'not_image' THEN RETURN aap_assets_v1._image_reply(NULL, 'not_image');
                WHEN 'invalid_account_role' THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_account_role');
                ELSE RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END CASE;
        END IF;
        v_data := v_reply -> 'data';
        IF v_data ->> 'project_id' IS DISTINCT FROM v_project_id::text
           OR aap_assets_v1._image_task_source_json(v_data) IS NULL THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
        END IF;
    END IF;
    RETURN aap_assets_v1._image_reply(v_data, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_labeling_task_share(uuid, uuid, boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._image_owner_source_share(
    p_actor_id uuid,
    p_task_id uuid,
    p_dataset_item_id uuid,
    p_lock_source boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_task_reply jsonb;
    v_task_data jsonb;
    v_item public.dataset_items%ROWTYPE;
    v_source jsonb;
BEGIN
    IF ((p_task_id IS NULL) = (p_dataset_item_id IS NULL))
       OR p_lock_source IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    IF p_actor_id IS NOT NULL THEN
        v_actor := aap_assets_v1._image_actor(p_actor_id, p_lock_source);
        IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_actor; END IF;
    END IF;
    IF p_task_id IS NOT NULL THEN
        v_task_reply := aap_assets_v1._image_labeling_task_share(
            p_task_id, p_actor_id, p_lock_source
        );
        IF v_task_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN v_task_reply;
        END IF;
        v_task_data := v_task_reply -> 'data';
        IF v_task_data -> 'dataset_item_id' = 'null'::jsonb THEN
            v_source := aap_assets_v1._image_task_source_json(v_task_data);
            IF v_source IS NULL THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_response'); END IF;
            RETURN aap_assets_v1._image_reply(v_source, NULL);
        END IF;
        BEGIN
            IF p_lock_source THEN
                SELECT item.* INTO v_item
                FROM public.dataset_items AS item
                WHERE item.id = (v_task_data ->> 'dataset_item_id')::uuid
                FOR UPDATE NOWAIT;
            ELSE
                SELECT item.* INTO v_item
                FROM public.dataset_items AS item
                WHERE item.id = (v_task_data ->> 'dataset_item_id')::uuid;
            END IF;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
        WHEN invalid_text_representation THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END;
        IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'source_missing'); END IF;
        IF v_item.file_type IS DISTINCT FROM 'image' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'not_image');
        END IF;
        IF v_item.media_source_generation IS NULL OR v_item.media_source_generation <= 0
           OR v_item.media_source_drifted IS NULL
           OR (v_item.media_object_identity IS NOT NULL
               AND NOT aap_assets_v1._media_object_identity_valid(v_item.media_object_identity)) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        v_source := aap_assets_v1._image_item_source_json(
            v_item, (v_task_data ->> 'task_id')::uuid,
            CASE WHEN v_task_data -> 'project_id' = 'null'::jsonb
                THEN NULL ELSE (v_task_data ->> 'project_id')::uuid END
        );
        IF pg_catalog.jsonb_typeof(v_source) IS DISTINCT FROM 'object'
           OR v_source ->> 'owner' IS NULL
           OR v_source #>> '{owner,kind}' IS DISTINCT FROM 'dataset_item'
           OR v_source #>> '{owner,id}' IS DISTINCT FROM v_item.id::text
           OR v_source ->> 'task_id' IS DISTINCT FROM p_task_id::text
           OR v_source ->> 'file_type' IS DISTINCT FROM 'image' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        RETURN aap_assets_v1._image_reply(v_source, NULL);
    END IF;
    BEGIN
        IF p_lock_source THEN
            SELECT item.* INTO v_item FROM public.dataset_items AS item
            WHERE item.id = p_dataset_item_id FOR UPDATE NOWAIT;
        ELSE
            SELECT item.* INTO v_item FROM public.dataset_items AS item
            WHERE item.id = p_dataset_item_id;
        END IF;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
    END;
    IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'source_missing'); END IF;
    IF v_item.file_type IS DISTINCT FROM 'image' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'not_image');
    END IF;
    IF v_item.media_source_generation IS NULL OR v_item.media_source_generation <= 0
       OR v_item.media_source_drifted IS NULL
       OR (v_item.media_object_identity IS NOT NULL
           AND NOT aap_assets_v1._media_object_identity_valid(v_item.media_object_identity)) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    v_source := aap_assets_v1._image_item_source_json(v_item);
    RETURN aap_assets_v1._image_reply(v_source, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_owner_source_share(uuid, uuid, uuid, boolean) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1._image_accept_source(
    p_operation_id uuid,
    p_actor_id uuid,
    p_task_id uuid,
    p_dataset_item_id uuid,
    p_purpose text,
    p_expected_source_snapshot text,
    p_policy jsonb,
    p_force boolean,
    p_action text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_policy jsonb;
    v_source_reply jsonb;
    v_source jsonb;
    v_access jsonb;
    v_spec jsonb;
    v_scope jsonb;
    v_intent jsonb;
    v_saved jsonb;
    v_result jsonb;
    v_data jsonb;
    v_job public.asset_image_jobs%ROWTYPE;
    v_projection jsonb;
    v_owner_kind text;
    v_owner_id uuid;
    v_slot text;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_force IS NULL
       OR p_action NOT IN ('request', 'retry')
       OR p_purpose NOT IN ('dimensions', 'thumbnail', 'pyramid')
       OR ((p_task_id IS NULL) = (p_dataset_item_id IS NULL))
       OR (p_expected_source_snapshot IS NOT NULL
           AND p_expected_source_snapshot !~ '^[0-9a-f]{64}$') THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_policy := aap_assets_v1._image_policy(COALESCE(p_policy, '{}'::jsonb));
    IF v_policy IS NULL THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    IF p_task_id IS NULL THEN
        v_access := aap_assets_v1._image_manager_item(p_actor_id, p_dataset_item_id, true);
        IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_access; END IF;
    ELSE
        v_access := aap_assets_v1._image_actor(p_actor_id, true);
        IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_access; END IF;
    END IF;
    v_source_reply := aap_assets_v1._image_owner_source_share(
        p_actor_id, p_task_id, p_dataset_item_id, true
    );
    IF v_source_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_source_reply; END IF;
    v_source := v_source_reply -> 'data';
    IF NOT aap_assets_v1._image_source_data_valid(v_source) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    v_spec := pg_catalog.jsonb_build_object(
        'kind', p_purpose,
        'expected_source_snapshot', v_source -> 'source_snapshot',
        'policy', v_policy
    );
    v_spec := aap_assets_v1._image_spec(v_spec);
    IF v_spec IS NULL THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_response'); END IF;
    v_scope := pg_catalog.jsonb_build_object(
        'task_id', p_task_id,
        'dataset_item_id', p_dataset_item_id,
        'purpose', p_purpose
    );
    v_intent := pg_catalog.jsonb_build_object(
        'expected_source_snapshot', p_expected_source_snapshot,
        'policy', v_policy,
        'force', p_force
    );
    v_saved := aap_assets_v1._image_read_operation(
        p_operation_id, p_actor_id, p_action, v_scope, v_intent
    );
    IF v_saved IS NOT NULL THEN
        IF v_saved ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
           OR pg_catalog.jsonb_typeof(v_saved -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_saved -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_saved; END IF;
        v_data := v_saved -> 'data';
        IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
           OR (v_data - ARRAY['operation_id', 'source', 'work', 'spec']) IS DISTINCT FROM '{}'::jsonb
           OR v_data ->> 'operation_id' IS DISTINCT FROM p_operation_id::text
           OR NOT aap_assets_v1._image_source_data_valid(v_data -> 'source')
           OR aap_assets_v1._image_spec(v_data -> 'spec') IS DISTINCT FROM v_data -> 'spec'
           OR pg_catalog.jsonb_typeof(v_data -> 'work') IS DISTINCT FROM 'object'
           OR v_data #>> '{spec,kind}' IS DISTINCT FROM p_purpose
           OR v_data #>> '{work,kind}' IS DISTINCT FROM p_purpose
           OR v_data #>> '{spec,expected_source_snapshot}' IS DISTINCT FROM v_data #>> '{source,source_snapshot}'
           OR v_data #> '{spec,policy}' IS DISTINCT FROM v_policy
           OR v_data #>> '{work,owner,kind}' IS DISTINCT FROM v_data #>> '{source,owner,kind}'
           OR v_data #>> '{work,owner,id}' IS DISTINCT FROM v_data #>> '{source,owner,id}'
           OR v_data #>> '{work,source_generation}' IS DISTINCT FROM v_data #>> '{source,source_generation}' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_data #>> '{source,owner,kind}' IS DISTINCT FROM v_source #>> '{owner,kind}'
           OR v_data #>> '{source,owner,id}' IS DISTINCT FROM v_source #>> '{owner,id}'
           OR v_data #>> '{source,source_snapshot}' IS DISTINCT FROM v_source ->> 'source_snapshot'
           OR v_data #>> '{source,source_generation}' IS DISTINCT FROM v_source ->> 'source_generation' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
        END IF;
        RETURN v_saved;
    END IF;
    IF v_source -> 'source_drifted' = 'true'::jsonb THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    IF v_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb
       OR v_source -> 'object_identity' = 'null'::jsonb THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_not_ready');
    END IF;
    IF p_expected_source_snapshot IS NOT NULL
       AND v_source ->> 'source_snapshot' IS DISTINCT FROM p_expected_source_snapshot THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    IF p_purpose = 'pyramid'
       AND v_source -> 'width' <> 'null'::jsonb
       AND v_source -> 'height' <> 'null'::jsonb
       AND (v_source ->> 'width')::numeric * (v_source ->> 'height')::numeric
            < (v_policy ->> 'optional_pixels')::numeric THEN
        RETURN aap_assets_v1._image_reply(NULL, 'resource_limit');
    END IF;
    IF p_action = 'retry' THEN
        v_owner_kind := v_source #>> '{owner,kind}';
        v_owner_id := (v_source #>> '{owner,id}')::uuid;
        v_slot := CASE WHEN p_purpose = 'pyramid'
            THEN 'pyramid:' || (v_policy ->> 'profile_version') ELSE p_purpose END;
        IF p_purpose = 'dimensions'
           AND v_source -> 'width' <> 'null'::jsonb
           AND v_source -> 'height' <> 'null'::jsonb THEN
            RETURN aap_assets_v1._image_reply(NULL, 'already_ready');
        ELSIF p_purpose = 'thumbnail'
           AND v_source -> 'thumbnail_path' <> 'null'::jsonb
           AND v_source -> 'thumbnail_source_generation' = v_source -> 'source_generation' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'already_ready');
        ELSIF p_purpose = 'pyramid' THEN
            v_projection := aap_assets_v1._image_pyramid_projection(
                v_source, NULL, v_policy ->> 'profile_version'
            );
            IF v_projection IS NULL THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            IF v_projection ->> 'status' = 'ready' THEN
                RETURN aap_assets_v1._image_reply(NULL, 'already_ready');
            END IF;
            IF v_source -> 'width' <> 'null'::jsonb
               AND v_source -> 'height' <> 'null'::jsonb
               AND (v_source ->> 'width')::numeric * (v_source ->> 'height')::numeric
                    < (v_policy ->> 'optional_pixels')::numeric THEN
                RETURN aap_assets_v1._image_reply(NULL, 'not_eligible');
            END IF;
        END IF;
        SELECT job.* INTO v_job
        FROM public.asset_image_jobs AS job
        WHERE job.owner_kind = v_owner_kind
          AND job.owner_id = v_owner_id
          AND job.slot_key = v_slot;
        IF FOUND AND v_job.status = 'failed'
           AND v_job.source_generation = (v_source ->> 'source_generation')::bigint
           AND v_job.source_snapshot = v_source ->> 'source_snapshot'
           AND v_job.spec = v_spec
           AND pg_catalog.clock_timestamp() < COALESCE(v_job.completed_at, v_job.updated_at)
               + pg_catalog.make_interval(secs => (v_policy ->> 'retry_cooldown_seconds')::integer) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'retry_cooldown');
        END IF;
    END IF;
    v_result := aap_assets_v1._image_enqueue(
        p_operation_id, p_actor_id, v_source, v_spec, p_force
    );
    IF v_result ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
       OR pg_catalog.jsonb_typeof(v_result -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'image enqueue envelope mismatch';
    END IF;
    IF v_result -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_result; END IF;
    v_data := v_result -> 'data';
    IF v_data ->> 'operation_id' IS DISTINCT FROM p_operation_id::text
       OR NOT aap_assets_v1._image_source_data_valid(v_data -> 'source')
       OR v_data #>> '{source,owner,kind}' IS DISTINCT FROM v_source #>> '{owner,kind}'
       OR v_data #>> '{source,owner,id}' IS DISTINCT FROM v_source #>> '{owner,id}'
       OR v_data #>> '{source,source_snapshot}' IS DISTINCT FROM v_source ->> 'source_snapshot'
       OR v_data #>> '{spec,kind}' IS DISTINCT FROM p_purpose
       OR v_data #>> '{work,kind}' IS DISTINCT FROM p_purpose
       OR v_data #>> '{spec,expected_source_snapshot}' IS DISTINCT FROM v_source ->> 'source_snapshot'
       OR v_data #>> '{work,owner,kind}' IS DISTINCT FROM v_source #>> '{owner,kind}'
       OR v_data #>> '{work,owner,id}' IS DISTINCT FROM v_source #>> '{owner,id}'
       OR v_data #>> '{work,source_generation}' IS DISTINCT FROM v_source ->> 'source_generation' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'image enqueue result escaped request';
    END IF;
    v_saved := aap_assets_v1._image_write_operation(
        p_operation_id, p_actor_id, p_action, v_scope, v_intent, v_result
    );
    IF v_saved IS NULL OR v_saved IS DISTINCT FROM v_result THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'image operation receipt mismatch';
    END IF;
    RETURN v_saved;
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
WHEN unique_violation THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
DO $owner$
DECLARE
    labeling_owner name;
    assets_owner name;
    labeling_create boolean;
    assets_create boolean;
BEGIN
    SELECT pg_catalog.pg_get_userbyid(proowner) INTO STRICT labeling_owner
    FROM pg_catalog.pg_proc
    WHERE oid = 'aap_labeling_v1.read_image_task_source(uuid,uuid,boolean)'::regprocedure;
    SELECT pg_catalog.pg_get_userbyid(proowner) INTO STRICT assets_owner
    FROM pg_catalog.pg_proc
    WHERE oid = 'aap_assets_v1._image_owner_source(uuid,uuid,uuid,boolean)'::regprocedure;
    labeling_create := pg_catalog.has_schema_privilege(labeling_owner, 'aap_labeling_v1', 'CREATE');
    assets_create := pg_catalog.has_schema_privilege(assets_owner, 'aap_assets_v1', 'CREATE');
    IF NOT labeling_create THEN EXECUTE pg_catalog.format('GRANT CREATE ON SCHEMA aap_labeling_v1 TO %I', labeling_owner); END IF;
    IF NOT assets_create THEN EXECUTE pg_catalog.format('GRANT CREATE ON SCHEMA aap_assets_v1 TO %I', assets_owner); END IF;
    EXECUTE pg_catalog.format('ALTER FUNCTION aap_labeling_v1.read_image_task_source_share(uuid,uuid,boolean) OWNER TO %I', labeling_owner);
    EXECUTE pg_catalog.format('ALTER FUNCTION aap_assets_v1._image_labeling_task_share(uuid,uuid,boolean) OWNER TO %I', assets_owner);
    EXECUTE pg_catalog.format('ALTER FUNCTION aap_assets_v1._image_owner_source_share(uuid,uuid,uuid,boolean) OWNER TO %I', assets_owner);
    EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION aap_labeling_v1.read_image_task_source_share(uuid,uuid,boolean) TO %I', assets_owner);
    IF NOT labeling_create THEN EXECUTE pg_catalog.format('REVOKE CREATE ON SCHEMA aap_labeling_v1 FROM %I', labeling_owner); END IF;
    IF NOT assets_create THEN EXECUTE pg_catalog.format('REVOKE CREATE ON SCHEMA aap_assets_v1 FROM %I', assets_owner); END IF;
END;
$owner$;
