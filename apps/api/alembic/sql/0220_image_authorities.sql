-- Labeling-owned source facts and derived-thumbnail publication for direct
-- image Tasks. Assets receives these projections by EXECUTE only.
ALTER TABLE public.tasks
    ADD COLUMN media_source_generation bigint NOT NULL DEFAULT 1,
    ADD COLUMN media_object_identity jsonb NULL,
    ADD COLUMN media_source_drifted boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT ck_tasks_media_source_generation
        CHECK (media_source_generation > 0),
    ADD CONSTRAINT ck_tasks_media_object_identity
        CHECK (
            media_object_identity IS NULL
            OR pg_catalog.jsonb_typeof(media_object_identity) = 'object'
        );
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._image_source_snapshot(p_task public.tasks)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.encode(
        pg_catalog.sha256(pg_catalog.convert_to(
            pg_catalog.jsonb_build_object(
                'task_id', (p_task).id,
                'created_at', (p_task).created_at,
                'project_id', (p_task).project_id,
                'dataset_item_id', (p_task).dataset_item_id,
                'file_type', (p_task).file_type,
                'file_path', (p_task).file_path,
                'source_generation', (p_task).media_source_generation
            )::text,
            'UTF8'
        )),
        'hex'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._image_source_snapshot(public.tasks)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._image_object_identity_valid(p_identity jsonb)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT COALESCE(
        pg_catalog.jsonb_typeof(p_identity) = 'object'
        AND NOT EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_object_keys(p_identity) AS key(name)
            WHERE key.name <> ALL (ARRAY['etag', 'byte_size', 'version_id'])
        )
        AND pg_catalog.jsonb_typeof(p_identity -> 'etag') = 'string'
        AND pg_catalog.char_length(p_identity ->> 'etag') BETWEEN 1 AND 255
        AND pg_catalog.jsonb_typeof(p_identity -> 'byte_size') = 'number'
        AND p_identity ->> 'byte_size' ~ '^(0|[1-9][0-9]*)$'
        AND (p_identity ->> 'byte_size')::numeric <= 9223372036854775807
        AND (
            NOT (p_identity ? 'version_id')
            OR p_identity -> 'version_id' = 'null'::jsonb
            OR (
                pg_catalog.jsonb_typeof(p_identity -> 'version_id') = 'string'
                AND pg_catalog.char_length(p_identity ->> 'version_id') <= 255
            )
        ),
        false
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._image_object_identity_valid(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._image_source_reply(p_data jsonb, p_code text)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE
        WHEN p_code IS NULL THEN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-image-source/v1',
            'ok', true,
            'data', p_data
        )
        ELSE pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-image-source/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', p_code)
        )
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._image_source_reply(jsonb, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._image_workspace_failure(p_access jsonb)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_code text;
BEGIN
    IF p_access IS NULL
       OR p_access ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
       OR pg_catalog.jsonb_typeof(p_access -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN 'invalid_response';
    END IF;
    IF p_access -> 'ok' IS DISTINCT FROM 'false'::jsonb THEN
        RETURN NULL;
    END IF;
    v_code := p_access #>> '{error,code}';
    CASE v_code
        WHEN 'account_not_active' THEN RETURN 'account_not_active';
        WHEN 'account_busy' THEN RETURN 'account_busy';
        WHEN 'invalid_account_role' THEN RETURN 'invalid_account_role';
        WHEN 'task_busy' THEN RETURN 'source_busy';
        WHEN 'project_missing' THEN RETURN 'task_not_visible';
        ELSE RETURN 'invalid_response';
    END CASE;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._image_workspace_failure(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._image_thumbnail_key_valid(p_key text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT COALESCE(
        p_key ~ '^image-assets/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g[1-9][0-9]*/a[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/thumbnail\.webp$'
        OR p_key ~ '^image-pyramids/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g[1-9][0-9]*/a[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/overview\.webp$',
        false
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._image_thumbnail_key_valid(text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._image_task_source_changed()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NEW.file_path IS NOT DISTINCT FROM OLD.file_path
       AND NEW.file_type IS NOT DISTINCT FROM OLD.file_type
       AND NEW.project_id IS NOT DISTINCT FROM OLD.project_id
       AND NEW.dataset_item_id IS NOT DISTINCT FROM OLD.dataset_item_id THEN
        RETURN NEW;
    END IF;
    IF OLD.media_source_generation = 9223372036854775807 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22003',
            MESSAGE = 'image Task source generation exhausted';
    END IF;

    NEW.media_source_generation := OLD.media_source_generation + 1;
    NEW.media_object_identity := NULL;
    NEW.media_source_drifted := false;
    NEW.thumbnail_path := NULL;
    NEW.blurhash := NULL;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._image_task_source_changed()
    FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_tasks_image_source_generation
BEFORE UPDATE OF file_path, file_type, project_id, dataset_item_id
ON public.tasks
FOR EACH ROW
EXECUTE FUNCTION aap_labeling_v1._image_task_source_changed();
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.read_image_task_source(
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
            SELECT task.* INTO v_task
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
        SELECT task.* INTO v_task
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
REVOKE ALL ON FUNCTION aap_labeling_v1.read_image_task_source(uuid, uuid, boolean)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.observe_image_task_source(
    p_task_id uuid,
    p_expected_snapshot text,
    p_identity jsonb
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
    v_current_snapshot text;
    v_drift boolean;
    v_identity jsonb;
BEGIN
    IF p_task_id IS NULL
       OR p_expected_snapshot IS NULL
       OR p_expected_snapshot !~ '^[0-9a-f]{64}$'
       OR NOT aap_labeling_v1._image_object_identity_valid(p_identity) THEN
        RETURN aap_labeling_v1._image_source_reply(NULL, 'invalid_request');
    END IF;
    v_identity := pg_catalog.jsonb_build_object(
        'etag', p_identity -> 'etag',
        'byte_size', p_identity -> 'byte_size',
        'version_id', COALESCE(p_identity -> 'version_id', 'null'::jsonb)
    );
    SELECT task.* INTO v_task
    FROM public.tasks AS task
    WHERE task.id = p_task_id;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._image_source_reply(NULL, 'source_missing');
    END IF;
    IF v_task.file_type IS DISTINCT FROM 'image'
       OR v_task.dataset_item_id IS NOT NULL THEN
        RETURN aap_labeling_v1._image_source_reply(NULL, 'source_changed');
    END IF;
    IF v_task.status IS NULL OR v_task.status = 'uploading' THEN
        RETURN aap_labeling_v1._image_source_reply(NULL, 'source_not_ready');
    END IF;
    v_current_snapshot := aap_labeling_v1._image_source_snapshot(v_task);
    IF v_current_snapshot IS DISTINCT FROM p_expected_snapshot THEN
        RETURN aap_labeling_v1._image_source_reply(NULL, 'source_changed');
    END IF;
    IF v_task.media_source_drifted
       OR v_task.media_object_identity = v_identity THEN
        RETURN aap_labeling_v1._image_source_reply(
            pg_catalog.jsonb_build_object(
                'task_id', v_task.id,
                'project_id', v_task.project_id,
                'dataset_item_id', v_task.dataset_item_id,
                'source_generation', v_task.media_source_generation,
                'source_snapshot', v_current_snapshot,
                'object_identity', v_task.media_object_identity,
                'source_drifted', v_task.media_source_drifted
            ), NULL
        );
    END IF;

    BEGIN
        SELECT task.* INTO v_task
        FROM public.tasks AS task
        WHERE task.id = p_task_id
        FOR UPDATE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_labeling_v1._image_source_reply(NULL, 'source_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._image_source_reply(NULL, 'source_missing');
    END IF;
    IF v_task.file_type IS DISTINCT FROM 'image'
       OR v_task.dataset_item_id IS NOT NULL
       OR v_task.status IS NULL OR v_task.status = 'uploading'
       OR aap_labeling_v1._image_source_snapshot(v_task) IS DISTINCT FROM p_expected_snapshot THEN
        RETURN aap_labeling_v1._image_source_reply(NULL, 'source_changed');
    END IF;
    IF v_task.media_source_drifted THEN
        RETURN aap_labeling_v1._image_source_reply(
            pg_catalog.jsonb_build_object(
                'task_id', v_task.id,
                'project_id', v_task.project_id,
                'dataset_item_id', v_task.dataset_item_id,
                'source_generation', v_task.media_source_generation,
                'source_snapshot', aap_labeling_v1._image_source_snapshot(v_task),
                'object_identity', v_task.media_object_identity,
                'source_drifted', true
            ), NULL
        );
    END IF;
    v_drift := v_task.media_object_identity IS NOT NULL
        AND v_task.media_object_identity IS DISTINCT FROM v_identity;
    IF v_drift THEN
        IF v_task.media_source_generation = 9223372036854775807 THEN
            RAISE EXCEPTION USING
                ERRCODE = '22003',
                MESSAGE = 'image Task source generation exhausted';
        END IF;
        UPDATE public.tasks AS task
        SET media_source_generation = v_task.media_source_generation + 1,
            media_object_identity = v_identity,
            media_source_drifted = true,
            thumbnail_path = NULL,
            blurhash = NULL,
            updated_at = pg_catalog.clock_timestamp()
        WHERE task.id = p_task_id
        RETURNING task.* INTO v_task;
    ELSE
        UPDATE public.tasks AS task
        SET media_object_identity = v_identity,
            updated_at = pg_catalog.clock_timestamp()
        WHERE task.id = p_task_id
        RETURNING task.* INTO v_task;
    END IF;
    RETURN aap_labeling_v1._image_source_reply(
        pg_catalog.jsonb_build_object(
            'task_id', v_task.id,
            'project_id', v_task.project_id,
            'dataset_item_id', v_task.dataset_item_id,
            'source_generation', v_task.media_source_generation,
            'source_snapshot', aap_labeling_v1._image_source_snapshot(v_task),
            'object_identity', v_task.media_object_identity,
            'source_drifted', v_task.media_source_drifted
        ), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.observe_image_task_source(uuid, text, jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.publish_task_image_thumbnail(
    p_task_id uuid,
    p_expected_snapshot text,
    p_source_generation bigint,
    p_key text,
    p_blurhash text
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
BEGIN
    IF p_task_id IS NULL
       OR p_expected_snapshot IS NULL
       OR p_expected_snapshot !~ '^[0-9a-f]{64}$'
       OR p_source_generation IS NULL OR p_source_generation <= 0
       OR p_key IS NULL OR NOT aap_labeling_v1._image_thumbnail_key_valid(p_key)
       OR (p_blurhash IS NOT NULL AND pg_catalog.char_length(p_blurhash) > 64) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-image-thumbnail/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    BEGIN
        SELECT task.* INTO v_task
        FROM public.tasks AS task
        WHERE task.id = p_task_id
        FOR UPDATE NOWAIT;
    EXCEPTION
        WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-labeling-image-thumbnail/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'source_busy')
            );
    END;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-image-thumbnail/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'task_id', p_task_id,
                'applied', false
            )
        );
    END IF;
    IF v_task.file_type IS DISTINCT FROM 'image'
       OR v_task.dataset_item_id IS NOT NULL
       OR v_task.status IS NULL OR v_task.status = 'uploading'
       OR v_task.media_source_drifted
       OR v_task.media_object_identity IS NULL
       OR v_task.media_source_generation IS DISTINCT FROM p_source_generation
       OR aap_labeling_v1._image_source_snapshot(v_task) IS DISTINCT FROM p_expected_snapshot THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-image-thumbnail/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'task_id', p_task_id,
                'applied', false
            )
        );
    END IF;
    UPDATE public.tasks AS task
    SET thumbnail_path = p_key,
        blurhash = CASE WHEN p_blurhash IS NULL THEN task.blurhash ELSE p_blurhash END,
        updated_at = pg_catalog.clock_timestamp()
    WHERE task.id = p_task_id;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-image-thumbnail/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'task_id', p_task_id,
            'applied', true
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.publish_task_image_thumbnail(
    uuid, text, bigint, text, text
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.image_thumbnail_references(p_prefix text)
RETURNS boolean
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_prefix IS NULL
       OR NOT (
            p_prefix ~ '^image-assets/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g[1-9][0-9]*/a[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/$'
            OR p_prefix ~ '^image-pyramids/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/$'
            OR p_prefix ~ '^image-pyramids/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g[1-9][0-9]*/$'
            OR p_prefix ~ '^image-pyramids/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g[1-9][0-9]*/a[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/$'
       ) THEN
        RETURN true;
    END IF;
    RETURN EXISTS (
        SELECT 1
        FROM public.tasks AS task
        WHERE pg_catalog.left(task.thumbnail_path, pg_catalog.char_length(p_prefix)) = p_prefix
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.image_thumbnail_references(text)
    FROM PUBLIC;
