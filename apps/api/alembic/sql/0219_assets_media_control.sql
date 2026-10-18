-- Public Assets media read/admission capabilities. The execution lease,
-- staging, publication and retention capabilities live in the companion
-- Assets media execution artifact.

CREATE FUNCTION aap_assets_v1._media_read_actor(p_actor_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor record;
BEGIN
    IF p_actor_id IS NULL THEN
        RETURN aap_assets_v1._media_reply(NULL, 'account_not_active');
    END IF;
    SELECT facts.actor_id, facts.platform_role, facts.is_active
    INTO v_actor
    FROM aap_identity_v1.read_actor_facts(p_actor_id) AS facts;
    IF NOT FOUND OR v_actor.is_active IS DISTINCT FROM true THEN
        RETURN aap_assets_v1._media_reply(NULL, 'account_not_active');
    END IF;
    IF v_actor.actor_id IS DISTINCT FROM p_actor_id
       OR v_actor.platform_role IS NULL
       OR v_actor.platform_role NOT IN (
            'super_admin', 'project_admin', 'employee', 'viewer'
       ) THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_account_role');
    END IF;
    RETURN aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'platform_role', v_actor.platform_role
        ),
        NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_read_actor(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._media_lock_actor(p_actor_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_data jsonb;
BEGIN
    v_actor := aap_identity_v1.lock_active_actor(p_actor_id);
    IF v_actor IS NULL
       OR v_actor ->> 'schema' IS DISTINCT FROM 'aap-active-actor/v1'
       OR pg_catalog.jsonb_typeof(v_actor -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_actor #>> '{error,code}'
            WHEN 'account_busy' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'account_busy');
            ELSE
                RETURN aap_assets_v1._media_reply(NULL, 'account_not_active');
        END CASE;
    END IF;
    v_data := v_actor -> 'data';
    IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
       OR (v_data - ARRAY['actor_id', 'platform_role']) IS DISTINCT FROM '{}'::jsonb
       OR v_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR pg_catalog.jsonb_typeof(v_data -> 'platform_role') IS DISTINCT FROM 'string'
       OR v_data ->> 'platform_role' NOT IN (
            'super_admin', 'project_admin', 'employee', 'viewer'
       ) THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_account_role');
    END IF;
    RETURN aap_assets_v1._media_reply(v_data, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_lock_actor(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._media_read_task_access(
    p_actor_id uuid, p_task_id uuid, p_dataset_item_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_data jsonb;
BEGIN
    v_reply := aap_labeling_v1.read_media_task_access(
        p_actor_id, p_task_id, p_dataset_item_id
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-media-task-access/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_reply #>> '{error,code}'
            WHEN 'account_not_active' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'account_not_active');
            WHEN 'account_busy' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'account_busy');
            WHEN 'task_busy' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'task_busy');
            WHEN 'invalid_account_role' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'invalid_account_role');
            WHEN 'permission_denied' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'permission_denied');
            WHEN 'task_not_visible' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'task_not_visible');
            ELSE
                RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
        END CASE;
    END IF;
    v_data := v_reply -> 'data';
    IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
       OR (v_data - ARRAY[
            'actor_id', 'task_id', 'dataset_item_id', 'project_id', 'file_type',
            'project_role', 'is_manager', 'annotation_write'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR v_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR v_data ->> 'task_id' IS DISTINCT FROM p_task_id::text
       OR v_data ->> 'dataset_item_id' IS DISTINCT FROM p_dataset_item_id::text
       OR pg_catalog.jsonb_typeof(v_data -> 'project_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_data -> 'file_type') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_data -> 'is_manager') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_data -> 'annotation_write') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    RETURN aap_assets_v1._media_reply(v_data, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_read_task_access(uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._media_read_visible_video_item(
    p_actor_id uuid,
    p_dataset_item_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_data jsonb;
    v_task_id uuid;
BEGIN
    v_reply := aap_labeling_v1.read_media_visible_video_item(
        p_actor_id, p_dataset_item_id
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-media-task-access/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_reply #>> '{error,code}'
            WHEN 'account_not_active' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'account_not_active');
            WHEN 'account_busy' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'account_busy');
            WHEN 'task_busy' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'task_busy');
            WHEN 'permission_denied' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'permission_denied');
            WHEN 'task_not_visible' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'task_not_visible');
            WHEN 'invalid_account_role' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'invalid_account_role');
            ELSE
                RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
        END CASE;
    END IF;
    v_data := v_reply -> 'data';
    IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
       OR (v_data - ARRAY[
            'actor_id', 'task_id', 'dataset_item_id', 'project_id', 'file_type',
            'project_role', 'is_manager', 'annotation_write'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR v_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR v_data ->> 'dataset_item_id' IS DISTINCT FROM p_dataset_item_id::text
       OR pg_catalog.jsonb_typeof(v_data -> 'task_id') IS DISTINCT FROM 'string' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    BEGIN
        v_task_id := (v_data ->> 'task_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END;
    RETURN aap_assets_v1._media_read_task_access(
        p_actor_id, v_task_id, p_dataset_item_id
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_read_visible_video_item(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._media_lock_task_annotation_write(
    p_actor_id uuid, p_task_id uuid, p_dataset_item_id uuid
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
BEGIN
    v_reply := aap_labeling_v1.lock_media_task_annotation_write(
        p_actor_id, p_task_id, p_dataset_item_id
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-media-task-write/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_reply #>> '{error,code}'
            WHEN 'account_not_active' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'account_not_active');
            WHEN 'account_busy' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'account_busy');
            WHEN 'task_busy' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'task_busy');
            WHEN 'task_not_visible' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'task_not_visible');
            WHEN 'permission_denied' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'permission_denied');
            ELSE
                RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
        END CASE;
    END IF;
    v_data := v_reply -> 'data';
    IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
       OR (v_data - ARRAY[
            'actor_id', 'task_id', 'dataset_item_id', 'project_id', 'annotation_write'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR v_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR v_data ->> 'task_id' IS DISTINCT FROM p_task_id::text
       OR v_data ->> 'dataset_item_id' IS DISTINCT FROM p_dataset_item_id::text
       OR v_data -> 'annotation_write' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    RETURN aap_assets_v1._media_reply(v_data, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_lock_task_annotation_write(uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._media_manager_item(
    p_actor_id uuid, p_dataset_item_id uuid, p_lock boolean
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
    v_actor_data jsonb;
    v_context jsonb;
    v_context_data jsonb;
    v_item jsonb;
    v_project_id uuid;
    v_project jsonb;
    v_project_data jsonb;
BEGIN
    IF p_actor_id IS NULL OR p_dataset_item_id IS NULL OR p_lock IS NULL THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    v_actor := CASE WHEN p_lock
        THEN aap_assets_v1._media_lock_actor(p_actor_id)
        ELSE aap_assets_v1._media_read_actor(p_actor_id)
    END;
    IF v_actor IS NULL OR v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN COALESCE(v_actor, aap_assets_v1._media_reply(NULL, 'invalid_response'));
    END IF;
    v_actor_data := v_actor -> 'data';
    IF v_actor_data ->> 'platform_role' NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_assets_v1._media_reply(NULL, 'permission_denied');
    END IF;
    v_context := aap_labeling_v1.read_media_failure_context(
        p_actor_id, ARRAY[p_dataset_item_id]
    );
    IF v_context IS NULL
       OR v_context ->> 'schema' IS DISTINCT FROM 'aap-labeling-media-failure-context/v1'
       OR pg_catalog.jsonb_typeof(v_context -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    IF v_context -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_context #>> '{error,code}'
            WHEN 'account_not_active' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'account_not_active');
            WHEN 'permission_denied' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'permission_denied');
            ELSE
                RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
        END CASE;
    END IF;
    v_context_data := v_context -> 'data';
    IF pg_catalog.jsonb_typeof(v_context_data) IS DISTINCT FROM 'object'
       OR v_context_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR pg_catalog.jsonb_typeof(v_context_data -> 'items') IS DISTINCT FROM 'array' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    SELECT item.value
    INTO v_item
    FROM pg_catalog.jsonb_array_elements(v_context_data -> 'items') AS item(value)
    WHERE item.value ->> 'dataset_item_id' = p_dataset_item_id::text
    LIMIT 1;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._media_reply(NULL, 'permission_denied');
    END IF;
    IF pg_catalog.jsonb_typeof(v_item) IS DISTINCT FROM 'object'
       OR (v_item - ARRAY[
            'dataset_item_id', 'task_id', 'task_display_id', 'project_id', 'project_name'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR v_item ->> 'dataset_item_id' IS DISTINCT FROM p_dataset_item_id::text THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    IF pg_catalog.jsonb_typeof(v_item -> 'project_id') = 'string' THEN
        BEGIN
            v_project_id := (v_item ->> 'project_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
        END;
        IF p_lock THEN
            v_project := aap_workspace_v1.lock_media_project_access(
                p_actor_id, v_project_id
            );
        ELSE
            v_project := aap_workspace_v1.read_media_project_access(
                p_actor_id, v_project_id
            );
        END IF;
        IF v_project IS NULL
           OR v_project ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
           OR v_project -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN aap_assets_v1._media_reply(
                NULL,
                CASE v_project #>> '{error,code}'
                    WHEN 'account_not_active' THEN 'account_not_active'
                    WHEN 'account_busy' THEN 'account_busy'
                    WHEN 'task_busy' THEN 'task_busy'
                    ELSE 'permission_denied'
                END
            );
        END IF;
        v_project_data := v_project -> 'data';
        IF v_project_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
           OR v_project_data ->> 'project_id' IS DISTINCT FROM v_project_id::text
           OR v_project_data -> 'is_manager' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN aap_assets_v1._media_reply(NULL, 'permission_denied');
        END IF;
    END IF;
    RETURN aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object(
            'actor_id', p_actor_id,
            'dataset_item_id', p_dataset_item_id,
            'task_id', v_item -> 'task_id',
            'project_id', v_item -> 'project_id'
        ),
        NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_manager_item(uuid, uuid, boolean)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.read_media_source(
    p_actor_id uuid,
    p_dataset_item_id uuid,
    p_task_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_task jsonb;
    v_item public.dataset_items%ROWTYPE;
    v_source jsonb;
BEGIN
    IF p_actor_id IS NULL OR p_dataset_item_id IS NULL THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    IF p_task_id IS NULL THEN
        v_actor := aap_assets_v1._media_read_actor(p_actor_id);
        IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN v_actor;
        END IF;
    ELSE
        v_task := aap_assets_v1._media_read_task_access(
            p_actor_id, p_task_id, p_dataset_item_id
        );
        IF v_task -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN v_task;
        END IF;
    END IF;
    SELECT item.* INTO v_item
    FROM public.dataset_items AS item
    WHERE item.id = p_dataset_item_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_missing');
    END IF;
    v_source := aap_assets_v1._media_source_json(v_item);
    IF pg_catalog.jsonb_typeof(v_source) IS DISTINCT FROM 'object'
       OR v_source ->> 'dataset_item_id' IS DISTINCT FROM p_dataset_item_id::text
       OR v_source ->> 'dataset_id' IS DISTINCT FROM v_item.dataset_id::text
       OR COALESCE(v_source ->> 'source_snapshot', '') !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(v_source -> 'source_generation') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_source -> 'timetable_revision') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_source -> 'materialized_generation') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_source -> 'source_drifted') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_source -> 'file_name') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_source -> 'file_path') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_source -> 'file_type') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_source -> 'video_metadata') IS DISTINCT FROM 'object' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    RETURN aap_assets_v1._media_reply(v_source, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_media_source(uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.read_media_summaries(p_item_ids uuid[])
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_items jsonb;
BEGIN
    IF p_item_ids IS NULL OR pg_catalog.array_position(p_item_ids, NULL) IS NOT NULL
       OR pg_catalog.cardinality(p_item_ids) > 500 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'dataset_item_id', item.id,
                'width', item.width,
                'height', item.height,
                'thumbnail_path', item.thumbnail_path,
                'blurhash', item.blurhash,
                'file_type', item.file_type,
                'metadata', CASE
                    WHEN pg_catalog.jsonb_typeof(item.metadata -> 'video') = 'object'
                    THEN item.metadata -> 'video'
                    ELSE '{}'::jsonb
                END,
                'source_generation', item.media_source_generation,
                'materialized_generation', item.media_materialized_generation,
                'source_drifted', item.media_source_drifted
            ) ORDER BY requested.ordinality
        ),
        '[]'::jsonb
    )
    INTO v_items
    FROM pg_catalog.unnest(p_item_ids) WITH ORDINALITY AS requested(item_id, ordinality)
    JOIN public.dataset_items AS item ON item.id = requested.item_id;
    RETURN aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object('items', v_items),
        NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_media_summaries(uuid[]) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.read_media_preferences()
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_preferences jsonb;
BEGIN
    v_preferences := aap_settings_v1.read_video_media_preferences();
    IF v_preferences IS NULL
       OR v_preferences ->> 'schema' IS DISTINCT FROM 'aap-settings-video-media-preferences/v1'
       OR v_preferences -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_preferences -> 'data') IS DISTINCT FROM 'object'
       OR (v_preferences -> 'data' - 'warmup_lookahead') IS DISTINCT FROM '{}'::jsonb
       OR NOT (v_preferences -> 'data' ? 'warmup_lookahead')
       OR (
            pg_catalog.jsonb_typeof(v_preferences #> '{data,warmup_lookahead}') = 'number'
            AND (
                (v_preferences #>> '{data,warmup_lookahead}') !~ '^(0|[1-9][0-9]*)$'
                OR pg_catalog.char_length(v_preferences #>> '{data,warmup_lookahead}') > 19
                OR (v_preferences #>> '{data,warmup_lookahead}')::numeric > 9223372036854775807::numeric
            )
       )
       OR pg_catalog.jsonb_typeof(v_preferences #> '{data,warmup_lookahead}') NOT IN ('number', 'null') THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    RETURN aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object(
            'warmup_lookahead', v_preferences #> '{data,warmup_lookahead}'
        ),
        NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_media_preferences() FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.resolve_internal_media_source(
    p_dataset_item_id uuid,
    p_source_snapshot text DEFAULT NULL,
    p_object_identity jsonb DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
 VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item public.dataset_items%ROWTYPE;
    v_reply jsonb;
    v_source jsonb;
BEGIN
    IF p_dataset_item_id IS NULL
       OR ((p_source_snapshot IS NULL) <> (p_object_identity IS NULL))
       OR (p_source_snapshot IS NOT NULL
           AND p_source_snapshot !~ '^[0-9a-f]{64}$') THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    IF p_object_identity IS NOT NULL AND (
        pg_catalog.jsonb_typeof(p_object_identity) IS DISTINCT FROM 'object'
        OR (p_object_identity - ARRAY['etag', 'byte_size', 'version_id']) IS DISTINCT FROM '{}'::jsonb
        OR pg_catalog.jsonb_typeof(p_object_identity -> 'etag') IS DISTINCT FROM 'string'
        OR pg_catalog.char_length(p_object_identity ->> 'etag') < 1
        OR pg_catalog.char_length(p_object_identity ->> 'etag') > 255
        OR pg_catalog.jsonb_typeof(p_object_identity -> 'byte_size') IS DISTINCT FROM 'number'
        OR (p_object_identity ->> 'byte_size') !~ '^(0|[1-9][0-9]*)$'
        OR pg_catalog.char_length(p_object_identity ->> 'byte_size') > 19
        OR (p_object_identity ->> 'byte_size')::numeric > 9223372036854775807::numeric
        OR (p_object_identity ? 'version_id'
            AND pg_catalog.jsonb_typeof(p_object_identity -> 'version_id') NOT IN ('string', 'null'))
        OR pg_catalog.char_length(COALESCE(p_object_identity ->> 'version_id', '')) > 255
    ) THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    IF p_source_snapshot IS NULL THEN
        SELECT item.* INTO v_item
        FROM public.dataset_items AS item
        WHERE item.id = p_dataset_item_id;
        IF NOT FOUND THEN
            RETURN aap_assets_v1._media_reply(NULL, 'source_missing');
        END IF;
        v_source := aap_assets_v1._media_source_json(v_item);
    ELSE
        v_reply := aap_assets_v1._media_reconcile_source(
            p_dataset_item_id, p_source_snapshot, p_object_identity, NULL
        );
        IF v_reply IS NULL
           OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-media/v1'
           OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR pg_catalog.jsonb_typeof(v_reply -> 'data') IS DISTINCT FROM 'object' THEN
            RETURN COALESCE(v_reply, aap_assets_v1._media_reply(NULL, 'invalid_response'));
        END IF;
        RETURN v_reply;
    END IF;
    IF pg_catalog.jsonb_typeof(v_source) IS DISTINCT FROM 'object'
       OR v_source ->> 'dataset_item_id' IS DISTINCT FROM p_dataset_item_id::text
       OR COALESCE(v_source ->> 'source_snapshot', '') !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(v_source -> 'video_metadata') IS DISTINCT FROM 'object' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    RETURN aap_assets_v1._media_reply(v_source, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.resolve_internal_media_source(uuid, text, jsonb)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.observe_media_source(
    p_actor_id uuid,
    p_dataset_item_id uuid,
    p_task_id uuid,
    p_source_snapshot text,
    p_object_identity jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_access jsonb;
    v_item public.dataset_items%ROWTYPE;
    v_reply jsonb;
BEGIN
    IF p_actor_id IS NULL OR p_dataset_item_id IS NULL
       OR p_source_snapshot IS NULL OR p_source_snapshot !~ '^[0-9a-f]{64}$'
       OR p_object_identity IS NULL
       OR pg_catalog.jsonb_typeof(p_object_identity) IS DISTINCT FROM 'object'
       OR (p_object_identity - ARRAY['etag', 'byte_size', 'version_id']) IS DISTINCT FROM '{}'::jsonb
       OR pg_catalog.jsonb_typeof(p_object_identity -> 'etag') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_object_identity ->> 'etag') NOT BETWEEN 1 AND 255
       OR pg_catalog.jsonb_typeof(p_object_identity -> 'byte_size') IS DISTINCT FROM 'number'
       OR (p_object_identity ->> 'byte_size') !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.char_length(p_object_identity ->> 'byte_size') > 19
       OR (p_object_identity ->> 'byte_size')::numeric > 9223372036854775807::numeric
       OR (p_object_identity ? 'version_id'
           AND pg_catalog.jsonb_typeof(p_object_identity -> 'version_id') NOT IN ('string', 'null'))
       OR pg_catalog.char_length(COALESCE(p_object_identity ->> 'version_id', '')) > 255 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    IF p_task_id IS NULL THEN
        v_access := aap_assets_v1._media_read_actor(p_actor_id);
    ELSE
        v_access := aap_assets_v1._media_read_task_access(
            p_actor_id, p_task_id, p_dataset_item_id
        );
    END IF;
    IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_access;
    END IF;
    SELECT item.* INTO v_item
    FROM public.dataset_items AS item
    WHERE item.id = p_dataset_item_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_missing');
    END IF;
    IF aap_assets_v1._media_source_snapshot(v_item) IS DISTINCT FROM p_source_snapshot THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
    END IF;
    v_reply := aap_assets_v1._media_reconcile_source(
        p_dataset_item_id, p_source_snapshot, p_object_identity, NULL
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-media/v1'
       OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_reply -> 'data') IS DISTINCT FROM 'object'
       OR v_reply #>> '{data,dataset_item_id}' IS DISTINCT FROM p_dataset_item_id::text THEN
        RETURN COALESCE(v_reply, aap_assets_v1._media_reply(NULL, 'invalid_response'));
    END IF;
    RETURN v_reply;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.observe_media_source(uuid, uuid, uuid, text, jsonb)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.request_video_media(
    p_operation_id uuid,
    p_actor_id uuid,
    p_dataset_item_id uuid,
    p_spec jsonb,
    p_force boolean
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
    v_reply jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_force IS NULL
       OR aap_assets_v1._media_work_spec_valid(p_spec) IS DISTINCT FROM true
       OR (p_dataset_item_id IS NULL AND p_force)
       OR ((p_dataset_item_id IS NULL) <> (p_spec ->> 'kind' = 'video_backfill')) THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    v_actor := aap_assets_v1._media_lock_actor(p_actor_id);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;
    IF v_actor #>> '{data,platform_role}' NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_assets_v1._media_reply(NULL, 'permission_denied');
    END IF;
    IF p_dataset_item_id IS NOT NULL THEN
        PERFORM 1 FROM public.dataset_items AS item
        WHERE item.id = p_dataset_item_id AND item.file_type = 'video';
        IF NOT FOUND THEN
            RETURN aap_assets_v1._media_reply(NULL, 'source_missing');
        END IF;
    END IF;
    v_reply := aap_assets_v1._media_enqueue(
        p_operation_id, p_actor_id, p_dataset_item_id, p_spec, p_force
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-media/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    RETURN v_reply;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.request_video_media(uuid, uuid, uuid, jsonb, boolean)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.schedule_video_media(
    p_operation_id uuid,
    p_dataset_item_id uuid,
    p_spec jsonb,
    p_force boolean
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
BEGIN
    IF p_force IS NULL
       OR aap_assets_v1._media_work_spec_valid(p_spec) IS DISTINCT FROM true
       OR (p_operation_id IS NULL AND p_spec ->> 'kind' = 'video_backfill')
       OR ((p_dataset_item_id IS NULL) <> (p_spec ->> 'kind' = 'video_backfill')) THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    IF p_dataset_item_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.dataset_items AS item
        WHERE item.id = p_dataset_item_id AND item.file_type = 'video'
    ) THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_missing');
    END IF;
    v_reply := aap_assets_v1._media_enqueue(
        p_operation_id, NULL, p_dataset_item_id, p_spec, p_force
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-media/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    RETURN v_reply;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.schedule_video_media(uuid, uuid, jsonb, boolean)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.read_video_work(p_job_id uuid, p_actor_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.asset_media_jobs%ROWTYPE;
    v_access jsonb;
BEGIN
    IF p_job_id IS NULL THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    SELECT job.* INTO v_job FROM public.asset_media_jobs AS job WHERE job.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_missing');
    END IF;
    IF p_actor_id IS NOT NULL THEN
        v_access := aap_assets_v1._media_read_actor(p_actor_id);
        IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN v_access;
        END IF;
        IF v_access #>> '{data,platform_role}' NOT IN ('super_admin', 'project_admin') THEN
            RETURN aap_assets_v1._media_reply(NULL, 'permission_denied');
        END IF;
    END IF;
    RETURN aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object(
            'work', pg_catalog.jsonb_build_object(
                'job_id', v_job.job_id,
                'generation', v_job.generation,
                'dataset_item_id', v_job.dataset_item_id,
                'source_generation', v_job.source_generation,
                'kind', v_job.kind,
                'status', v_job.status
            ),
            'spec', v_job.spec,
            'progress', v_job.progress,
            'error_code', v_job.error_code,
            'error_detail', v_job.error_detail,
            'lease_expires_at', v_job.lease_expires_at
        ),
        NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_video_work(uuid, uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.cancel_video_media(
    p_operation_id uuid,
    p_actor_id uuid,
    p_job_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.asset_media_jobs%ROWTYPE;
    v_actor jsonb;
    v_scope jsonb;
    v_intent jsonb;
    v_saved jsonb;
    v_result jsonb;
    v_canceled boolean := false;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_job_id IS NULL THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    v_actor := aap_assets_v1._media_lock_actor(p_actor_id);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;
    IF v_actor #>> '{data,platform_role}' NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_assets_v1._media_reply(NULL, 'permission_denied');
    END IF;
    SELECT job.* INTO v_job FROM public.asset_media_jobs AS job WHERE job.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_missing');
    END IF;
    v_scope := pg_catalog.jsonb_build_object(
        'job_id', p_job_id,
        'dataset_item_id', v_job.dataset_item_id,
        'kind', v_job.kind
    );
    v_intent := pg_catalog.jsonb_build_object('job_id', p_job_id);
    BEGIN
        PERFORM aap_assets_v1._media_operation_lock(p_operation_id);
        v_saved := aap_assets_v1._media_read_operation(
            p_operation_id, p_actor_id, 'cancel', v_scope, v_intent
        );
        IF v_saved IS NOT NULL THEN
            IF v_saved ->> 'schema' IS DISTINCT FROM 'aap-assets-media/v1'
               OR pg_catalog.jsonb_typeof(v_saved -> 'ok') IS DISTINCT FROM 'boolean' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
            END IF;
            RETURN v_saved;
        END IF;
        SELECT job.* INTO v_job
        FROM public.asset_media_jobs AS job
        WHERE job.job_id = p_job_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            RETURN aap_assets_v1._media_reply(NULL, 'source_missing');
        END IF;
        IF v_job.status IN ('pending', 'running') THEN
            v_canceled := true;
            IF v_job.lease_token IS NOT NULL THEN
                PERFORM aap_assets_v1._media_queue_prefix(
                    'media-assets/' || v_job.job_id::text || '/g'
                        || v_job.generation::text || '/a' || v_job.lease_token::text || '/',
                    'attempt_abandoned'
                );
            END IF;
            UPDATE public.asset_media_jobs
            SET status = 'canceled', lease_token = NULL, lease_expires_at = NULL,
                updated_at = pg_catalog.now(), completed_at = pg_catalog.now()
            WHERE job_id = p_job_id;
            DELETE FROM public.asset_media_timetable_staging
            WHERE job_id = p_job_id;
        END IF;
        v_result := pg_catalog.jsonb_build_object(
            'operation_id', p_operation_id,
            'job_id', p_job_id,
            'canceled', v_canceled
        );
        v_saved := aap_assets_v1._media_write_operation(
            p_operation_id, p_actor_id, 'cancel', v_scope, v_intent, v_result
        );
        IF v_saved IS NULL
           OR v_saved ->> 'schema' IS DISTINCT FROM 'aap-assets-media/v1'
           OR v_saved -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION 'media cancellation receipt rejected';
        END IF;
        RETURN v_saved;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._media_reply(NULL, 'operation_busy');
    WHEN OTHERS THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.cancel_video_media(uuid, uuid, uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.retry_video_media(
    p_operation_id uuid,
    p_actor_id uuid,
    p_spec jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item public.dataset_items%ROWTYPE;
    v_access jsonb;
    v_asset_type text;
    v_spec jsonb;
    v_snapshot text;
    v_reply jsonb;
    v_frame public.video_frame_cache%ROWTYPE;
    v_chunk public.video_chunks%ROWTYPE;
    v_chunk_size integer;
    v_index integer;
    v_width integer;
    v_format text;
    v_fields jsonb;
    v_error_code text;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL
       OR pg_catalog.jsonb_typeof(p_spec) IS DISTINCT FROM 'object'
       OR (p_spec - ARRAY['asset_type', 'dataset_item_id', 'chunk_id', 'frame_index', 'width', 'format'])
            IS DISTINCT FROM '{}'::jsonb
       OR pg_catalog.jsonb_typeof(p_spec -> 'dataset_item_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_spec -> 'asset_type') IS DISTINCT FROM 'string' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    IF (pg_catalog.jsonb_typeof(p_spec -> 'chunk_id') = 'number'
        AND COALESCE(p_spec ->> 'chunk_id', '') !~ '^(0|[1-9][0-9]*)$')
       OR (pg_catalog.jsonb_typeof(p_spec -> 'frame_index') = 'number'
           AND COALESCE(p_spec ->> 'frame_index', '') !~ '^(0|[1-9][0-9]*)$')
       OR (pg_catalog.jsonb_typeof(p_spec -> 'width') = 'number'
           AND COALESCE(p_spec ->> 'width', '') !~ '^[1-9][0-9]*$') THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        v_asset_type := p_spec ->> 'asset_type';
        IF v_asset_type NOT IN ('probe', 'poster', 'frame_timetable', 'chunk', 'frame') THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END IF;
        v_index := CASE WHEN pg_catalog.jsonb_typeof(p_spec -> 'chunk_id') = 'number'
                        THEN (p_spec ->> 'chunk_id')::integer
                        WHEN pg_catalog.jsonb_typeof(p_spec -> 'frame_index') = 'number'
                        THEN (p_spec ->> 'frame_index')::integer
                        ELSE NULL END;
        v_width := CASE WHEN pg_catalog.jsonb_typeof(p_spec -> 'width') = 'number'
                        THEN (p_spec ->> 'width')::integer ELSE NULL END;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END;
    IF ((p_spec ? 'chunk_id') AND pg_catalog.jsonb_typeof(p_spec -> 'chunk_id') NOT IN ('number', 'null'))
       OR ((p_spec ? 'frame_index') AND pg_catalog.jsonb_typeof(p_spec -> 'frame_index') NOT IN ('number', 'null'))
       OR ((p_spec ? 'width') AND pg_catalog.jsonb_typeof(p_spec -> 'width') NOT IN ('number', 'null'))
       OR ((p_spec ? 'format') AND pg_catalog.jsonb_typeof(p_spec -> 'format') NOT IN ('string', 'null')) THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    IF (v_asset_type IN ('probe', 'poster', 'frame_timetable')
        AND (v_index IS NOT NULL OR v_width IS NOT NULL OR p_spec ->> 'format' IS NOT NULL))
       OR (v_asset_type = 'chunk' AND (v_index IS NULL OR v_width IS NOT NULL OR p_spec ->> 'format' IS NOT NULL))
       OR (v_asset_type = 'frame' AND (v_index IS NULL OR v_width IS NULL
            OR p_spec ->> 'format' IS NULL OR p_spec ->> 'format' NOT IN ('webp', 'jpeg')))
       OR (v_index IS NOT NULL AND v_index NOT BETWEEN 0 AND 2147483647)
       OR (v_width IS NOT NULL AND v_width NOT BETWEEN 1 AND 4096) THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        v_snapshot := NULL;
        SELECT item.* INTO v_item
        FROM public.dataset_items AS item
        WHERE item.id = (p_spec ->> 'dataset_item_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END;
    IF NOT FOUND OR v_item.file_type IS DISTINCT FROM 'video' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_missing');
    END IF;
    v_access := aap_assets_v1._media_manager_item(p_actor_id, v_item.id, true);
    IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_access;
    END IF;
    IF v_item.media_source_drifted THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
    END IF;
    v_snapshot := aap_assets_v1._media_source_snapshot(v_item);
    IF v_asset_type IN ('probe', 'poster', 'frame_timetable') THEN
        v_spec := pg_catalog.jsonb_build_object(
            'kind', 'video_metadata',
            'generate_playback', v_asset_type = 'probe',
            'generate_poster', v_asset_type IN ('probe', 'poster'),
            'probe_timetable', v_asset_type IN ('probe', 'frame_timetable'),
            'expected_source_snapshot', v_snapshot
        );
    ELSIF v_asset_type = 'frame' THEN
        v_format := p_spec ->> 'format';
        SELECT frame.* INTO v_frame
        FROM public.video_frame_cache AS frame
        WHERE frame.dataset_item_id = v_item.id
          AND frame.frame_index = v_index
          AND frame.width = v_width
          AND frame.format = v_format;
        IF NOT FOUND THEN
            RETURN aap_assets_v1._media_reply(NULL, 'asset_missing');
        END IF;
        v_spec := pg_catalog.jsonb_build_object(
            'kind', 'frame', 'frame_index', v_index, 'width', v_width,
            'format', v_format, 'expected_source_snapshot', v_snapshot
        );
    ELSE
        SELECT chunk.* INTO v_chunk
        FROM public.video_chunks AS chunk
        WHERE chunk.dataset_item_id = v_item.id AND chunk.chunk_id = v_index;
        IF NOT FOUND THEN
            RETURN aap_assets_v1._media_reply(NULL, 'asset_missing');
        END IF;
        v_chunk_size := CASE WHEN v_index > 0
                             THEN v_chunk.start_frame / v_index
                             ELSE v_chunk.end_frame + 1 END;
        IF v_chunk_size NOT BETWEEN 1 AND 1000000 THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
        END IF;
        v_spec := pg_catalog.jsonb_build_object(
            'kind', 'chunk', 'chunk_id', v_index, 'chunk_size_frames', v_chunk_size,
            'expected_source_snapshot', v_snapshot
        );
    END IF;
    BEGIN
        v_reply := aap_assets_v1._media_enqueue(
            p_operation_id, p_actor_id, v_item.id, v_spec, true
        );
        IF v_reply IS NULL
           OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-media/v1'
           OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
        END IF;
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN v_reply;
        END IF;
        IF v_asset_type = 'frame' THEN
            UPDATE public.video_frame_cache
            SET status = 'pending', error = NULL,
                source_generation = v_item.media_source_generation,
                timetable_revision = v_item.media_timetable_revision,
                updated_at = pg_catalog.now()
            WHERE id = v_frame.id;
        ELSIF v_asset_type = 'chunk' THEN
            UPDATE public.video_chunks
            SET status = 'pending', error = NULL, generation_mode = NULL,
                diagnostics = '{}'::jsonb,
                source_generation = v_item.media_source_generation,
                timetable_revision = v_item.media_timetable_revision,
                updated_at = pg_catalog.now()
            WHERE id = v_chunk.id;
        END IF;
        IF v_asset_type IN ('frame', 'chunk') AND NOT FOUND THEN
            v_error_code := 'asset_missing';
            RAISE EXCEPTION 'retry asset disappeared';
        END IF;
        RETURN v_reply;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_busy');
    WHEN OTHERS THEN
        RETURN aap_assets_v1._media_reply(NULL, COALESCE(v_error_code, 'invalid_response'));
    END;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.retry_video_media(uuid, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.read_video_failures(
    p_actor_id uuid,
    "limit" integer,
    "offset" bigint
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_context jsonb;
    v_context_data jsonb;
    v_item_ids uuid[];
    v_total bigint;
    v_items jsonb;
BEGIN
    IF p_actor_id IS NULL OR "limit" IS NULL OR "limit" NOT BETWEEN 1 AND 200
       OR "offset" IS NULL OR "offset" < 0 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    v_actor := aap_assets_v1._media_read_actor(p_actor_id);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_actor;
    END IF;
    IF v_actor #>> '{data,platform_role}' NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_assets_v1._media_reply(NULL, 'permission_denied');
    END IF;
    SELECT COALESCE(pg_catalog.array_agg(DISTINCT failed.dataset_item_id), ARRAY[]::uuid[])
    INTO v_item_ids
    FROM (
        SELECT item.id AS dataset_item_id
        FROM public.dataset_items AS item
        WHERE item.file_type = 'video'
          AND (
              NULLIF(item.metadata #>> '{video,probe_error}', '') IS NOT NULL
              OR NULLIF(item.metadata #>> '{video,poster_error}', '') IS NOT NULL
              OR NULLIF(item.metadata #>> '{video,frame_timetable_error}', '') IS NOT NULL
          )
        UNION
        SELECT chunk.dataset_item_id
        FROM public.video_chunks AS chunk
        WHERE chunk.status = 'failed'
        UNION
        SELECT frame.dataset_item_id
        FROM public.video_frame_cache AS frame
        WHERE frame.status = 'failed'
    ) AS failed;
    v_context := aap_labeling_v1.read_media_failure_context(p_actor_id, v_item_ids);
    IF v_context IS NULL
       OR v_context ->> 'schema' IS DISTINCT FROM 'aap-labeling-media-failure-context/v1'
       OR pg_catalog.jsonb_typeof(v_context -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    IF v_context -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_context #>> '{error,code}'
            WHEN 'account_not_active' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'account_not_active');
            WHEN 'permission_denied' THEN
                RETURN aap_assets_v1._media_reply(NULL, 'permission_denied');
            ELSE
                RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
        END CASE;
    END IF;
    v_context_data := v_context -> 'data';
    IF pg_catalog.jsonb_typeof(v_context_data) IS DISTINCT FROM 'object'
       OR v_context_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR pg_catalog.jsonb_typeof(v_context_data -> 'items') IS DISTINCT FROM 'array' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    WITH context_items AS (
        SELECT item.dataset_item_id, item.task_id, item.task_display_id,
               item.project_id, item.project_name
        FROM pg_catalog.jsonb_to_recordset(v_context_data -> 'items') AS item(
            dataset_item_id uuid,
            task_id uuid,
            task_display_id text,
            project_id uuid,
            project_name text
        )
    ),
    failures AS (
        SELECT
            item.id::text || ':' || failures.asset_type AS asset_key,
            failures.asset_type,
            item.id AS dataset_item_id,
            item.file_name,
            failures.error,
            context.task_id,
            context.task_display_id,
            context.project_id,
            context.project_name,
            item.updated_at,
            NULL::integer AS chunk_id,
            NULL::integer AS frame_index,
            NULL::bigint AS width,
            NULL::text AS format
        FROM public.dataset_items AS item
        JOIN context_items AS context ON context.dataset_item_id = item.id
        CROSS JOIN LATERAL (
            VALUES
                ('probe'::text, NULLIF(item.metadata #>> '{video,probe_error}', '')),
                ('poster'::text, NULLIF(item.metadata #>> '{video,poster_error}', '')),
                ('frame_timetable'::text, NULLIF(item.metadata #>> '{video,frame_timetable_error}', ''))
        ) AS failures(asset_type, error)
        WHERE item.file_type = 'video' AND failures.error IS NOT NULL
        UNION ALL
        SELECT
            'chunk:' || chunk.id::text,
            'chunk'::text,
            item.id,
            item.file_name,
            COALESCE(chunk.error, 'Chunk generation failed'),
            context.task_id,
            context.task_display_id,
            context.project_id,
            context.project_name,
            chunk.updated_at,
            chunk.chunk_id,
            NULL::integer,
            NULL::bigint,
            NULL::text
        FROM public.video_chunks AS chunk
        JOIN public.dataset_items AS item ON item.id = chunk.dataset_item_id
        JOIN context_items AS context ON context.dataset_item_id = item.id
        WHERE item.file_type = 'video' AND chunk.status = 'failed'
        UNION ALL
        SELECT
            'frame:' || frame.id::text,
            'frame'::text,
            item.id,
            item.file_name,
            COALESCE(frame.error, 'Frame extraction failed'),
            context.task_id,
            context.task_display_id,
            context.project_id,
            context.project_name,
            frame.updated_at,
            NULL::integer,
            frame.frame_index,
            frame.width::bigint,
            frame.format
        FROM public.video_frame_cache AS frame
        JOIN public.dataset_items AS item ON item.id = frame.dataset_item_id
        JOIN context_items AS context ON context.dataset_item_id = item.id
        WHERE item.file_type = 'video' AND frame.status = 'failed'
    )
    SELECT
        count(*)::bigint,
        COALESCE(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'asset_key', failure.asset_key,
                    'asset_type', failure.asset_type,
                    'dataset_item_id', failure.dataset_item_id,
                    'file_name', failure.file_name,
                    'error', failure.error,
                    'task_id', failure.task_id,
                    'task_display_id', failure.task_display_id,
                    'project_id', failure.project_id,
                    'project_name', failure.project_name,
                    'updated_at', failure.updated_at,
                    'chunk_id', failure.chunk_id,
                    'frame_index', failure.frame_index,
                    'width', failure.width,
                    'format', failure.format
                ) ORDER BY failure.updated_at DESC NULLS LAST, failure.asset_key
            ) FILTER (WHERE failure.page_rank > "offset" AND failure.page_rank <= "offset" + "limit"),
            '[]'::jsonb
        )
    INTO v_total, v_items
    FROM (
        SELECT failure.*, row_number() OVER (
            ORDER BY failure.updated_at DESC NULLS LAST, failure.asset_key
        ) AS page_rank
        FROM failures AS failure
    ) AS failure;
    RETURN aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object(
            'items', v_items,
            'total', v_total,
            'limit', "limit",
            'offset', "offset"
        ),
        NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_video_failures(uuid, integer, bigint)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.invalidate_video_media(
    p_actor_id uuid,
    p_dataset_item_id uuid,
    p_task_id uuid,
    p_invalidation jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_access jsonb;
    v_access_data jsonb;
    v_item public.dataset_items%ROWTYPE;
    v_source jsonb;
    v_source_snapshot text;
    v_expected_snapshot text;
    v_expected_key text;
    v_kind text;
    v_index integer;
    v_width integer;
    v_format text;
    v_video jsonb;
    v_metadata jsonb;
    v_frame public.video_frame_cache%ROWTYPE;
    v_chunk public.video_chunks%ROWTYPE;
    v_spec jsonb;
    v_reply jsonb;
    v_changed boolean := false;
    v_error_code text;
BEGIN
    IF p_actor_id IS NULL OR p_dataset_item_id IS NULL
       OR pg_catalog.jsonb_typeof(p_invalidation) IS DISTINCT FROM 'object'
       OR (p_invalidation - ARRAY['source_snapshot', 'expected_key', 'kind',
                                  'frame_index', 'width', 'format', 'chunk_id']) IS DISTINCT FROM '{}'::jsonb
       OR COALESCE(p_invalidation ->> 'source_snapshot', '') !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_invalidation -> 'expected_key') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_invalidation ->> 'expected_key') < 1
       OR pg_catalog.jsonb_typeof(p_invalidation -> 'kind') IS DISTINCT FROM 'string'
       OR (p_invalidation ? 'frame_index'
           AND pg_catalog.jsonb_typeof(p_invalidation -> 'frame_index') NOT IN ('number', 'null'))
       OR (p_invalidation ? 'chunk_id'
           AND pg_catalog.jsonb_typeof(p_invalidation -> 'chunk_id') NOT IN ('number', 'null'))
       OR (p_invalidation ? 'width'
           AND pg_catalog.jsonb_typeof(p_invalidation -> 'width') NOT IN ('number', 'null'))
       OR (p_invalidation ? 'format'
           AND pg_catalog.jsonb_typeof(p_invalidation -> 'format') NOT IN ('string', 'null')) THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    v_kind := p_invalidation ->> 'kind';
    v_expected_snapshot := p_invalidation ->> 'source_snapshot';
    v_expected_key := p_invalidation ->> 'expected_key';
    IF v_kind NOT IN ('poster', 'playback', 'frame', 'chunk') THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    IF (pg_catalog.jsonb_typeof(p_invalidation -> 'frame_index') = 'number'
        AND COALESCE(p_invalidation ->> 'frame_index', '') !~ '^(0|[1-9][0-9]*)$')
       OR (pg_catalog.jsonb_typeof(p_invalidation -> 'chunk_id') = 'number'
           AND COALESCE(p_invalidation ->> 'chunk_id', '') !~ '^(0|[1-9][0-9]*)$')
       OR (pg_catalog.jsonb_typeof(p_invalidation -> 'width') = 'number'
           AND COALESCE(p_invalidation ->> 'width', '') !~ '^(0|[1-9][0-9]*)$') THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        IF p_invalidation -> 'frame_index' IS NOT NULL
           AND pg_catalog.jsonb_typeof(p_invalidation -> 'frame_index') <> 'null' THEN
            v_index := (p_invalidation ->> 'frame_index')::integer;
        END IF;
        IF p_invalidation -> 'chunk_id' IS NOT NULL
           AND pg_catalog.jsonb_typeof(p_invalidation -> 'chunk_id') <> 'null' THEN
            v_index := (p_invalidation ->> 'chunk_id')::integer;
        END IF;
        IF p_invalidation -> 'width' IS NOT NULL
           AND pg_catalog.jsonb_typeof(p_invalidation -> 'width') <> 'null' THEN
            v_width := (p_invalidation ->> 'width')::integer;
        END IF;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END;
    IF (v_kind IN ('poster', 'playback')
        AND (v_index IS NOT NULL OR v_width IS NOT NULL
             OR p_invalidation ->> 'format' IS NOT NULL
             OR (p_invalidation -> 'chunk_id' IS NOT NULL
                 AND pg_catalog.jsonb_typeof(p_invalidation -> 'chunk_id') <> 'null')))
       OR (v_kind = 'frame'
           AND (v_index IS NULL OR v_width IS NULL
                OR p_invalidation ->> 'format' IS NULL
                OR p_invalidation ->> 'format' NOT IN ('webp', 'jpeg')
                OR (p_invalidation -> 'chunk_id' IS NOT NULL
                    AND pg_catalog.jsonb_typeof(p_invalidation -> 'chunk_id') <> 'null')))
       OR (v_kind = 'chunk'
           AND (v_index IS NULL OR v_width IS NOT NULL OR p_invalidation ->> 'format' IS NOT NULL
                OR (p_invalidation -> 'frame_index' IS NOT NULL
                    AND pg_catalog.jsonb_typeof(p_invalidation -> 'frame_index') <> 'null')))
       OR (v_index IS NOT NULL AND v_index < 0)
       OR (v_width IS NOT NULL AND v_width NOT BETWEEN 1 AND 4096) THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    v_format := p_invalidation ->> 'format';
    IF p_task_id IS NULL THEN
        v_access := aap_assets_v1._media_read_visible_video_item(
            p_actor_id, p_dataset_item_id
        );
    ELSE
        v_access := aap_assets_v1._media_read_task_access(p_actor_id, p_task_id, p_dataset_item_id);
    END IF;
    IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_access;
    END IF;
    v_access_data := v_access -> 'data';
    IF v_access_data ->> 'file_type' IS DISTINCT FROM 'video' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'task_not_visible');
    END IF;
    BEGIN
        SELECT item.* INTO v_item FROM public.dataset_items AS item
        WHERE item.id = p_dataset_item_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_busy');
    END;
    IF NOT FOUND OR v_item.file_type IS DISTINCT FROM 'video' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_missing');
    END IF;
    v_source_snapshot := aap_assets_v1._media_source_snapshot(v_item);
    IF v_item.media_source_drifted THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
    END IF;
    v_source_snapshot := aap_assets_v1._media_source_snapshot(v_item);
    IF v_source_snapshot IS DISTINCT FROM v_expected_snapshot THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
    END IF;
    v_video := COALESCE(v_item.metadata -> 'video', '{}'::jsonb);
    IF v_kind = 'poster' THEN
        v_changed := COALESCE(v_video ->> 'poster_frame_path' = v_expected_key, false);
        v_spec := pg_catalog.jsonb_build_object(
            'kind', 'video_metadata', 'generate_playback', false,
            'generate_poster', true, 'probe_timetable', false,
            'expected_source_snapshot', v_source_snapshot
        );
    ELSIF v_kind = 'playback' THEN
        v_changed := COALESCE(v_video ->> 'playback_path' = v_expected_key, false);
        v_spec := pg_catalog.jsonb_build_object(
            'kind', 'video_metadata', 'generate_playback', true,
            'generate_poster', false, 'probe_timetable', false,
            'expected_source_snapshot', v_source_snapshot
        );
    ELSIF v_kind = 'frame' THEN
        BEGIN
            SELECT frame.* INTO v_frame
            FROM public.video_frame_cache AS frame
            WHERE frame.dataset_item_id = p_dataset_item_id
              AND frame.frame_index = v_index
              AND frame.width = v_width
              AND frame.format = v_format
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._media_reply(NULL, 'source_busy');
        END;
        v_changed := FOUND AND COALESCE(v_frame.storage_key = v_expected_key, false)
            AND v_frame.source_generation = v_item.media_source_generation
            AND v_frame.timetable_revision = v_item.media_timetable_revision;
        v_spec := pg_catalog.jsonb_build_object(
            'kind', 'frame', 'frame_index', v_index, 'width', v_width,
            'format', v_format, 'expected_source_snapshot', v_source_snapshot
        );
    ELSE
        BEGIN
            SELECT chunk.* INTO v_chunk
            FROM public.video_chunks AS chunk
            WHERE chunk.dataset_item_id = p_dataset_item_id AND chunk.chunk_id = v_index
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._media_reply(NULL, 'source_busy');
        END;
        v_changed := FOUND AND COALESCE(v_chunk.storage_key = v_expected_key, false)
            AND v_chunk.source_generation = v_item.media_source_generation
            AND v_chunk.timetable_revision = v_item.media_timetable_revision;
        IF NOT FOUND THEN
            v_spec := NULL;
        ELSE
            v_spec := pg_catalog.jsonb_build_object(
                'kind', 'chunk', 'chunk_id', v_index,
                'chunk_size_frames', CASE WHEN v_index > 0
                    THEN GREATEST(1, v_chunk.start_frame / v_index)
                    ELSE GREATEST(1, v_chunk.end_frame + 1) END,
                'expected_source_snapshot', v_source_snapshot
            );
        END IF;
    END IF;
    IF v_changed THEN
        BEGIN
            IF v_kind = 'poster' THEN
                v_metadata := pg_catalog.jsonb_set(
                    COALESCE(v_item.metadata, '{}'::jsonb), '{video}',
                    v_video - 'poster_frame_path', true
                );
                UPDATE public.dataset_items SET metadata = v_metadata, updated_at = pg_catalog.now()
                WHERE id = p_dataset_item_id;
            ELSIF v_kind = 'playback' THEN
                v_metadata := pg_catalog.jsonb_set(
                    COALESCE(v_item.metadata, '{}'::jsonb), '{video}',
                    v_video - 'playback_path', true
                );
                UPDATE public.dataset_items SET metadata = v_metadata, updated_at = pg_catalog.now()
                WHERE id = p_dataset_item_id;
            ELSIF v_kind = 'frame' THEN
                UPDATE public.video_frame_cache
                SET storage_key = NULL, byte_size = NULL, status = 'pending', error = NULL,
                    source_generation = v_item.media_source_generation,
                    timetable_revision = v_item.media_timetable_revision,
                    updated_at = pg_catalog.now()
                WHERE id = v_frame.id;
            ELSE
                UPDATE public.video_chunks
                SET storage_key = NULL, byte_size = NULL, status = 'pending', error = NULL,
                    generation_mode = NULL, diagnostics = '{}'::jsonb,
                    source_generation = v_item.media_source_generation,
                    timetable_revision = v_item.media_timetable_revision,
                    updated_at = pg_catalog.now()
                WHERE id = v_chunk.id;
            END IF;
            v_reply := aap_assets_v1._media_enqueue(
                NULL, p_actor_id, p_dataset_item_id, v_spec, true
            );
            IF v_reply IS NULL OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                v_error_code := COALESCE(v_reply #>> '{error,code}', 'invalid_response');
                RAISE EXCEPTION 'media invalidation enqueue rejected';
            END IF;
            SELECT item.* INTO v_item FROM public.dataset_items AS item WHERE item.id = p_dataset_item_id;
            v_source := aap_assets_v1._media_source_json(v_item);
        EXCEPTION WHEN OTHERS THEN
            RETURN aap_assets_v1._media_reply(NULL, COALESCE(v_error_code, 'invalid_response'));
        END;
    ELSE
        v_source := aap_assets_v1._media_source_json(v_item);
    END IF;
    RETURN aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object('source', v_source, 'changed', v_changed), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.invalidate_video_media(uuid, uuid, uuid, jsonb)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.read_internal_video_frame(
    p_dataset_item_id uuid,
    p_frame_index integer,
    p_width integer,
    p_format text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item public.dataset_items%ROWTYPE;
    v_frame public.video_frame_cache%ROWTYPE;
    v_source jsonb;
    v_snapshot text;
    v_spec jsonb;
    v_work jsonb;
    v_error_code text;
BEGIN
    IF p_dataset_item_id IS NULL OR p_frame_index IS NULL OR p_frame_index < 0
       OR p_width IS NULL OR p_width NOT BETWEEN 1 AND 4096
       OR p_format IS NULL OR p_format NOT IN ('webp', 'jpeg') THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    SELECT item.* INTO v_item FROM public.dataset_items AS item WHERE item.id = p_dataset_item_id;
    IF NOT FOUND OR v_item.file_type IS DISTINCT FROM 'video' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_missing');
    END IF;
    IF v_item.media_source_drifted THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
    END IF;
    IF v_item.media_materialized_generation <> v_item.media_source_generation
       OR COALESCE(v_item.metadata #>> '{video,frame_count}', '') !~ '^[1-9][0-9]*$' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'metadata_not_ready');
    END IF;
    IF p_frame_index >= (v_item.metadata #>> '{video,frame_count}')::integer THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_frame_range');
    END IF;
    v_snapshot := aap_assets_v1._media_source_snapshot(v_item);
    v_source := aap_assets_v1._media_source_json(v_item);
    v_spec := pg_catalog.jsonb_build_object(
        'kind', 'frame', 'frame_index', p_frame_index, 'width', p_width,
        'format', p_format, 'expected_source_snapshot', v_snapshot
    );
    BEGIN
        SELECT frame.* INTO v_frame FROM public.video_frame_cache AS frame
        WHERE frame.dataset_item_id = p_dataset_item_id
          AND frame.frame_index = p_frame_index AND frame.width = p_width AND frame.format = p_format;
        IF NOT FOUND OR v_frame.source_generation <> v_item.media_source_generation
           OR v_frame.timetable_revision <> v_item.media_timetable_revision THEN
            IF FOUND AND v_frame.storage_key IS NOT NULL THEN
                INSERT INTO public.asset_media_garbage(prefix, keys, reason, created_at)
                VALUES (NULL, pg_catalog.jsonb_build_array(v_frame.storage_key), 'cache_expired', pg_catalog.now());
            END IF;
            INSERT INTO public.video_frame_cache(
                id, dataset_item_id, frame_index, width, format, storage_key, byte_size,
                status, error, source_generation, timetable_revision, created_at, updated_at
            ) VALUES (
                pg_catalog.gen_random_uuid(), p_dataset_item_id, p_frame_index, p_width, p_format,
                NULL, NULL, 'pending', NULL, v_item.media_source_generation,
                v_item.media_timetable_revision, pg_catalog.now(), pg_catalog.now()
            )
            ON CONFLICT (dataset_item_id, frame_index, width, format) DO UPDATE SET
                storage_key = NULL, byte_size = NULL, status = 'pending', error = NULL,
                source_generation = EXCLUDED.source_generation,
                timetable_revision = EXCLUDED.timetable_revision,
                updated_at = pg_catalog.now();
        END IF;
        v_work := aap_assets_v1.schedule_video_media(NULL, p_dataset_item_id, v_spec, false);
        IF v_work IS NULL OR v_work -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_error_code := COALESCE(v_work #>> '{error,code}', 'invalid_response');
            RAISE EXCEPTION 'internal frame work admission failed';
        END IF;
        SELECT frame.* INTO v_frame FROM public.video_frame_cache AS frame
        WHERE frame.dataset_item_id = p_dataset_item_id
          AND frame.frame_index = p_frame_index AND frame.width = p_width AND frame.format = p_format;
        RETURN aap_assets_v1._media_reply(
            pg_catalog.jsonb_build_object(
                'source', v_source,
                'assets', pg_catalog.jsonb_build_array(aap_assets_v1._media_frame_json(v_frame))
            ),
            NULL
        );
    EXCEPTION WHEN OTHERS THEN
        RETURN aap_assets_v1._media_reply(NULL, COALESCE(v_error_code, 'invalid_response'));
    END;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_internal_video_frame(uuid, integer, integer, text)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.scan_video_backfill(
    p_selection jsonb,
    p_after_item_id uuid,
    p_page_size integer
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_dataset_id uuid;
    v_item_ids uuid[];
    v_all_items boolean;
    v_cutoff timestamptz;
    v_total bigint;
    v_page jsonb;
    v_count bigint;
    v_next uuid;
    v_complete boolean;
    v_digest text;
    v_snapshot jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_selection) IS DISTINCT FROM 'object'
       OR (p_selection - ARRAY['dataset_id', 'dataset_item_ids', 'all_items',
                               'only_missing', 'accepted_before']) IS DISTINCT FROM '{}'::jsonb
       OR (p_selection ? 'only_missing'
           AND pg_catalog.jsonb_typeof(p_selection -> 'only_missing') IS DISTINCT FROM 'boolean')
       OR (p_selection ? 'dataset_id'
           AND pg_catalog.jsonb_typeof(p_selection -> 'dataset_id') NOT IN ('string', 'null'))
       OR (p_selection ? 'dataset_item_ids'
           AND pg_catalog.jsonb_typeof(p_selection -> 'dataset_item_ids') IS DISTINCT FROM 'array')
       OR (p_selection ? 'all_items'
           AND pg_catalog.jsonb_typeof(p_selection -> 'all_items') IS DISTINCT FROM 'boolean')
       OR (p_selection ? 'accepted_before'
           AND pg_catalog.jsonb_typeof(p_selection -> 'accepted_before') NOT IN ('string', 'null'))
       OR p_page_size IS NULL OR p_page_size NOT BETWEEN 1 AND 500 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    IF (CASE WHEN pg_catalog.jsonb_typeof(p_selection -> 'dataset_id') = 'string' THEN 1 ELSE 0 END)
       + (CASE WHEN pg_catalog.jsonb_typeof(p_selection -> 'dataset_item_ids') = 'array'
                    AND pg_catalog.jsonb_array_length(p_selection -> 'dataset_item_ids') > 0 THEN 1 ELSE 0 END)
       + (CASE WHEN p_selection -> 'all_items' = 'true'::jsonb THEN 1 ELSE 0 END) <> 1 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    IF p_selection -> 'dataset_id' IS NOT NULL
       AND pg_catalog.jsonb_typeof(p_selection -> 'dataset_id') = 'string' THEN
        IF (p_selection ->> 'dataset_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END IF;
        BEGIN
            v_dataset_id := (p_selection ->> 'dataset_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END;
    END IF;
    IF pg_catalog.jsonb_typeof(p_selection -> 'dataset_item_ids') = 'array' THEN
        IF pg_catalog.jsonb_array_length(p_selection -> 'dataset_item_ids') > 500
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_array_elements(p_selection -> 'dataset_item_ids') AS entry(value)
               WHERE pg_catalog.jsonb_typeof(entry.value) IS DISTINCT FROM 'string'
                  OR COALESCE(entry.value #>> '{}', '') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           )
           OR (SELECT count(DISTINCT entry.value #>> '{}')
               FROM pg_catalog.jsonb_array_elements(p_selection -> 'dataset_item_ids') AS entry(value))
              <> pg_catalog.jsonb_array_length(p_selection -> 'dataset_item_ids') THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END IF;
        BEGIN
            SELECT COALESCE(pg_catalog.array_agg(DISTINCT (entry.value #>> '{}')::uuid), ARRAY[]::uuid[])
            INTO v_item_ids
            FROM pg_catalog.jsonb_array_elements(p_selection -> 'dataset_item_ids') AS entry(value);
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.unnest(v_item_ids) AS requested(item_id)
            WHERE NOT EXISTS (
                SELECT 1 FROM public.dataset_items AS item
                WHERE item.id = requested.item_id AND item.file_type = 'video'
            )
        ) THEN
            RETURN aap_assets_v1._media_reply(NULL, 'source_missing');
        END IF;
    END IF;
    IF pg_catalog.jsonb_typeof(p_selection -> 'all_items') = 'boolean' THEN
        v_all_items := p_selection -> 'all_items' = 'true'::jsonb;
    ELSE
        v_all_items := false;
    END IF;
    v_cutoff := pg_catalog.transaction_timestamp();
    IF p_selection ? 'accepted_before'
       AND pg_catalog.jsonb_typeof(p_selection -> 'accepted_before') = 'string' THEN
        IF (p_selection ->> 'accepted_before') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T.*(Z|[+-][0-9]{2}:[0-9]{2})$' THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END IF;
        BEGIN
            v_cutoff := (p_selection ->> 'accepted_before')::timestamptz;
        EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END;
    END IF;
    v_snapshot := aap_assets_v1._media_backfill_snapshot(p_selection, v_cutoff);
    IF v_snapshot IS NULL
       OR pg_catalog.jsonb_typeof(v_snapshot) IS DISTINCT FROM 'object'
       OR COALESCE(v_snapshot ->> 'snapshot_digest', '') !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(v_snapshot -> 'total') IS DISTINCT FROM 'number'
       OR COALESCE(v_snapshot ->> 'total', '') !~ '^(0|[1-9][0-9]*)$' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    v_total := (v_snapshot ->> 'total')::bigint;
    v_digest := v_snapshot ->> 'snapshot_digest';
    SELECT COALESCE(
        pg_catalog.jsonb_agg(aap_assets_v1._media_source_json(page.item) ORDER BY page.id),
        '[]'::jsonb
    )
    INTO v_page
    FROM (
        SELECT item.*
        FROM public.dataset_items AS item
        WHERE item.file_type = 'video'
          AND item.created_at <= v_cutoff
          AND (p_after_item_id IS NULL OR item.id > p_after_item_id)
          AND (
              (v_dataset_id IS NOT NULL AND item.dataset_id = v_dataset_id)
              OR (v_item_ids IS NOT NULL AND item.id = ANY(v_item_ids))
              OR v_all_items
          )
        ORDER BY item.id
        LIMIT p_page_size
    ) AS page;
    v_count := pg_catalog.jsonb_array_length(v_page);
    IF v_count > 0 THEN
        v_next := (v_page -> (v_count - 1) ->> 'dataset_item_id')::uuid;
    ELSE
        v_next := NULL;
    END IF;
    v_complete := NOT EXISTS (
        SELECT 1 FROM public.dataset_items AS item
        WHERE item.file_type = 'video'
          AND item.created_at <= v_cutoff
          AND (p_after_item_id IS NULL OR item.id > p_after_item_id)
          AND (v_next IS NULL OR item.id > v_next)
          AND (
              (v_dataset_id IS NOT NULL AND item.dataset_id = v_dataset_id)
              OR (v_item_ids IS NOT NULL AND item.id = ANY(v_item_ids))
              OR v_all_items
          )
    );
    RETURN aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object(
            'items', v_page,
            'snapshot_digest', v_digest,
            'total', v_total,
            'accepted_before', v_cutoff,
            'after_item_id', p_after_item_id,
            'next_item_id', CASE WHEN v_complete THEN NULL ELSE v_next END,
            'complete', v_complete
        ),
        NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.scan_video_backfill(jsonb, uuid, integer)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.read_video_timetable(
    p_actor_id uuid,
    p_dataset_item_id uuid,
    p_task_id uuid,
    p_from_frame integer,
    p_to_frame integer,
    p_after_frame integer,
    p_page_size integer
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_access jsonb;
    v_access_data jsonb;
    v_item public.dataset_items%ROWTYPE;
    v_source jsonb;
    v_from integer;
    v_total bigint;
    v_all jsonb;
    v_frames jsonb;
    v_count bigint;
    v_next integer;
    v_complete boolean;
    v_digest text;
BEGIN
    IF p_actor_id IS NULL OR p_dataset_item_id IS NULL
       OR (p_from_frame IS NOT NULL AND p_from_frame < 0)
       OR (p_to_frame IS NOT NULL AND p_to_frame < 0)
       OR (p_after_frame IS NOT NULL AND p_after_frame < 0)
       OR p_page_size IS NULL OR p_page_size NOT BETWEEN 1 AND 500 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    IF p_from_frame IS NOT NULL AND p_to_frame IS NOT NULL
       AND p_to_frame < p_from_frame THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_frame_range');
    END IF;
    IF p_task_id IS NULL THEN
        v_access := aap_assets_v1._media_read_visible_video_item(
            p_actor_id, p_dataset_item_id
        );
    ELSE
        v_access := aap_assets_v1._media_read_task_access(
            p_actor_id, p_task_id, p_dataset_item_id
        );
    END IF;
    IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_access;
    END IF;
    v_access_data := v_access -> 'data';
    IF v_access_data ->> 'file_type' IS DISTINCT FROM 'video' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'task_not_visible');
    END IF;
    SELECT item.* INTO v_item
    FROM public.dataset_items AS item
    WHERE item.id = p_dataset_item_id;
    IF NOT FOUND OR v_item.file_type IS DISTINCT FROM 'video' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_missing');
    END IF;
    IF v_item.media_source_drifted THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
    END IF;
    IF v_item.media_materialized_generation <> v_item.media_source_generation THEN
        RETURN aap_assets_v1._media_reply(NULL, 'metadata_not_ready');
    END IF;
    v_from := COALESCE(p_from_frame, 0);
    SELECT
        count(*)::bigint,
        COALESCE(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'frame_index', frame.frame_index,
                    'pts_ms', frame.pts_ms,
                    'is_keyframe', frame.is_keyframe,
                    'pict_type', frame.pict_type,
                    'byte_offset', frame.byte_offset
                ) ORDER BY frame.frame_index
            ),
            '[]'::jsonb
        )
    INTO v_total, v_all
    FROM public.video_frame_indices AS frame
    WHERE frame.dataset_item_id = p_dataset_item_id
      AND frame.frame_index >= v_from
      AND (p_to_frame IS NULL OR frame.frame_index <= p_to_frame);
    SELECT COALESCE(
        pg_catalog.jsonb_agg(page.value ORDER BY (page.value ->> 'frame_index')::integer),
        '[]'::jsonb
    )
    INTO v_frames
    FROM (
        SELECT pg_catalog.jsonb_build_object(
            'frame_index', frame.frame_index,
            'pts_ms', frame.pts_ms,
            'is_keyframe', frame.is_keyframe,
            'pict_type', frame.pict_type,
            'byte_offset', frame.byte_offset
        ) AS value
        FROM public.video_frame_indices AS frame
        WHERE frame.dataset_item_id = p_dataset_item_id
          AND frame.frame_index >= v_from
          AND (p_to_frame IS NULL OR frame.frame_index <= p_to_frame)
          AND (p_after_frame IS NULL OR frame.frame_index > p_after_frame)
        ORDER BY frame.frame_index
        LIMIT p_page_size
    ) AS page;
    v_count := pg_catalog.jsonb_array_length(v_frames);
    IF v_count > 0 THEN
        v_next := (v_frames -> (v_count - 1) ->> 'frame_index')::integer;
    ELSE
        v_next := NULL;
    END IF;
    v_complete := NOT EXISTS (
        SELECT 1 FROM public.video_frame_indices AS frame
        WHERE frame.dataset_item_id = p_dataset_item_id
          AND frame.frame_index >= v_from
          AND (p_to_frame IS NULL OR frame.frame_index <= p_to_frame)
          AND (p_after_frame IS NULL OR frame.frame_index > p_after_frame)
          AND (v_next IS NULL OR frame.frame_index > v_next)
    );
    v_source := aap_assets_v1._media_source_json(v_item);
    v_digest := pg_catalog.encode(
        public.digest(
            pg_catalog.convert_to(
                pg_catalog.jsonb_build_object(
                    'source_snapshot', aap_assets_v1._media_source_snapshot(v_item),
                    'timetable_revision', v_item.media_timetable_revision,
                    'from_frame', v_from,
                    'to_frame', p_to_frame,
                    'frames', v_all
                )::text,
                'UTF8'
            ),
            'sha256'
        ),
        'hex'
    );
    RETURN aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object(
            'source', v_source,
            'snapshot_digest', v_digest,
            'total', v_total,
            'after_frame', p_after_frame,
            'next_frame', CASE WHEN v_complete THEN NULL ELSE v_next END,
            'complete', v_complete,
            'frames', v_frames
        ),
        NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_video_timetable(uuid, uuid, uuid, integer, integer, integer, integer)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.read_video_samples(
    p_actor_id uuid,
    p_dataset_item_id uuid,
    p_task_id uuid,
    p_chunk_id integer,
    p_after_sample integer,
    p_page_size integer
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_access jsonb;
    v_access_data jsonb;
    v_item public.dataset_items%ROWTYPE;
    v_chunk public.video_chunks%ROWTYPE;
    v_samples jsonb;
    v_page jsonb;
    v_total bigint;
    v_count bigint;
    v_next integer;
    v_complete boolean;
    v_digest text;
    v_codec text;
    v_description text;
    v_width bigint;
    v_height bigint;
BEGIN
    IF p_actor_id IS NULL OR p_dataset_item_id IS NULL
       OR p_chunk_id IS NULL OR p_chunk_id < 0
       OR (p_after_sample IS NOT NULL AND p_after_sample < 0)
       OR p_page_size IS NULL OR p_page_size NOT BETWEEN 1 AND 500 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    IF p_task_id IS NULL THEN
        v_access := aap_assets_v1._media_read_visible_video_item(
            p_actor_id, p_dataset_item_id
        );
    ELSE
        v_access := aap_assets_v1._media_read_task_access(
            p_actor_id, p_task_id, p_dataset_item_id
        );
    END IF;
    IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_access;
    END IF;
    v_access_data := v_access -> 'data';
    IF v_access_data ->> 'file_type' IS DISTINCT FROM 'video' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'task_not_visible');
    END IF;
    SELECT item.* INTO v_item
    FROM public.dataset_items AS item
    WHERE item.id = p_dataset_item_id;
    IF NOT FOUND OR v_item.file_type IS DISTINCT FROM 'video' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_missing');
    END IF;
    IF v_item.media_source_drifted THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
    END IF;
    IF v_item.media_materialized_generation <> v_item.media_source_generation THEN
        RETURN aap_assets_v1._media_reply(NULL, 'metadata_not_ready');
    END IF;
    SELECT chunk.* INTO v_chunk
    FROM public.video_chunks AS chunk
    WHERE chunk.dataset_item_id = p_dataset_item_id
      AND chunk.chunk_id = p_chunk_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._media_reply(NULL, 'asset_missing');
    END IF;
    IF v_chunk.status IS DISTINCT FROM 'ready'
       OR v_chunk.source_generation IS DISTINCT FROM v_item.media_source_generation
       OR v_chunk.timetable_revision IS DISTINCT FROM v_item.media_timetable_revision
       OR v_chunk.storage_key IS NULL THEN
        RETURN aap_assets_v1._media_reply(NULL, 'metadata_not_ready');
    END IF;
    IF pg_catalog.jsonb_typeof(v_chunk.diagnostics -> 'samples') NOT IN ('array', 'null')
       AND v_chunk.diagnostics ? 'samples' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    v_samples := CASE
        WHEN pg_catalog.jsonb_typeof(v_chunk.diagnostics -> 'samples') = 'array'
            THEN v_chunk.diagnostics -> 'samples'
        ELSE '[]'::jsonb
    END;
    IF pg_catalog.jsonb_typeof(v_samples) IS DISTINCT FROM 'array' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    SELECT count(*)::bigint INTO v_total
    FROM pg_catalog.jsonb_array_elements(v_samples) AS entry(value);
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_array_elements(v_samples) AS entry(value)
        WHERE pg_catalog.jsonb_typeof(entry.value) IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(entry.value -> 'frame_index') IS DISTINCT FROM 'number'
           OR COALESCE(entry.value ->> 'frame_index', '') !~ '^(0|[1-9][0-9]*)$'
           OR pg_catalog.jsonb_typeof(entry.value -> 'pts_ms') IS DISTINCT FROM 'number'
           OR COALESCE(entry.value ->> 'pts_ms', '') !~ '^-?(0|[1-9][0-9]*)$'
           OR pg_catalog.jsonb_typeof(entry.value -> 'duration_ms') IS DISTINCT FROM 'number'
           OR COALESCE(entry.value ->> 'duration_ms', '') !~ '^(0|[1-9][0-9]*)$'
           OR entry.value -> 'is_keyframe' NOT IN ('true'::jsonb, 'false'::jsonb)
           OR pg_catalog.jsonb_typeof(entry.value -> 'size_bytes') IS DISTINCT FROM 'number'
           OR COALESCE(entry.value ->> 'size_bytes', '') !~ '^(0|[1-9][0-9]*)$'
           OR pg_catalog.jsonb_typeof(entry.value -> 'offset_in_chunk') IS DISTINCT FROM 'number'
           OR COALESCE(entry.value ->> 'offset_in_chunk', '') !~ '^(0|[1-9][0-9]*)$'
           OR (entry.value ->> 'frame_index')::numeric > 2147483647
           OR (entry.value ->> 'duration_ms')::numeric > 9223372036854775807
           OR (entry.value ->> 'size_bytes')::numeric > 9223372036854775807
           OR (entry.value ->> 'offset_in_chunk')::numeric > 9223372036854775807
    ) THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    IF v_total > 0 AND (
        (
            SELECT count(DISTINCT (entry.value ->> 'frame_index')::integer)
            FROM pg_catalog.jsonb_array_elements(v_samples) AS entry(value)
        ) <> v_total
        OR (
            SELECT min((entry.value ->> 'frame_index')::integer)
            FROM pg_catalog.jsonb_array_elements(v_samples) AS entry(value)
        ) <> v_chunk.start_frame
        OR (
            SELECT max((entry.value ->> 'frame_index')::integer)
            FROM pg_catalog.jsonb_array_elements(v_samples) AS entry(value)
        ) <> v_chunk.start_frame::bigint + v_total - 1
        OR v_chunk.start_frame::bigint + v_total - 1 > v_chunk.end_frame
    ) THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(page.value ORDER BY page.sample_ordinal), '[]'::jsonb)
    INTO v_page
    FROM (
        SELECT
            (entry.ordinality - 1)::integer AS sample_ordinal,
            pg_catalog.jsonb_build_object(
                'frame_index', entry.value -> 'frame_index',
                'pts_ms', entry.value -> 'pts_ms',
                'duration_ms', entry.value -> 'duration_ms',
                'is_keyframe', entry.value -> 'is_keyframe',
                'size_bytes', entry.value -> 'size_bytes',
                'offset_in_chunk', entry.value -> 'offset_in_chunk'
            ) AS value
        FROM pg_catalog.jsonb_array_elements(v_samples) WITH ORDINALITY AS entry(value, ordinality)
        WHERE p_after_sample IS NULL
           OR entry.ordinality - 1 > p_after_sample
        ORDER BY entry.ordinality
        LIMIT p_page_size
    ) AS page;
    v_count := pg_catalog.jsonb_array_length(v_page);
    IF v_count > 0 THEN
        v_next := COALESCE(p_after_sample, -1) + v_count;
        v_complete := v_next >= v_total - 1;
    ELSE
        v_next := NULL;
        v_complete := true;
    END IF;
    v_codec := COALESCE(v_chunk.diagnostics ->> 'codec_string', '');
    v_description := v_chunk.diagnostics ->> 'description';
    IF pg_catalog.jsonb_typeof(v_chunk.diagnostics -> 'codec_string') NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(v_chunk.diagnostics -> 'description') NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(v_chunk.diagnostics -> 'width') IS DISTINCT FROM 'number'
       OR COALESCE(v_chunk.diagnostics ->> 'width', '') !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.jsonb_typeof(v_chunk.diagnostics -> 'height') IS DISTINCT FROM 'number'
       OR COALESCE(v_chunk.diagnostics ->> 'height', '') !~ '^(0|[1-9][0-9]*)$'
       OR (v_chunk.diagnostics ->> 'width')::numeric > 9223372036854775807
       OR (v_chunk.diagnostics ->> 'height')::numeric > 9223372036854775807 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    v_width := (v_chunk.diagnostics ->> 'width')::bigint;
    v_height := (v_chunk.diagnostics ->> 'height')::bigint;
    v_digest := pg_catalog.encode(
        public.digest(
            pg_catalog.convert_to(
                pg_catalog.jsonb_build_object(
                    'dataset_item_id', p_dataset_item_id,
                    'chunk_id', p_chunk_id,
                    'source_generation', v_chunk.source_generation,
                    'timetable_revision', v_chunk.timetable_revision,
                    'samples', v_samples,
                    'codec_string', v_codec,
                    'description', v_description,
                    'width', v_width,
                    'height', v_height
                )::text,
                'UTF8'
            ),
            'sha256'
        ),
        'hex'
    );
    RETURN aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object(
            'dataset_item_id', p_dataset_item_id,
            'chunk_id', p_chunk_id,
            'source_generation', v_chunk.source_generation,
            'timetable_revision', v_chunk.timetable_revision,
            'snapshot_digest', v_digest,
            'codec_string', v_codec,
            'description', v_description,
            'width', v_width,
            'height', v_height,
            'total', v_total,
            'after_sample', p_after_sample,
            'next_sample', CASE WHEN v_complete THEN NULL ELSE v_next END,
            'complete', v_complete,
            'samples', v_page
        ),
        NULL
    );
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_video_samples(uuid, uuid, uuid, integer, integer, integer)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.ensure_video_media(
    p_actor_id uuid,
    p_dataset_item_id uuid,
    p_task_id uuid,
    p_selection jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_access jsonb;
    v_access_data jsonb;
    v_item public.dataset_items%ROWTYPE;
    v_source jsonb;
    v_source_snapshot text;
    v_kind text;
    v_intent text;
    v_force boolean;
    v_failed_only boolean;
    v_indices jsonb;
    v_width integer;
    v_format text;
    v_chunk_size integer;
    v_frame_count integer;
    v_fps numeric;
    v_index integer;
    v_start integer;
    v_end integer;
    v_old_key text;
    v_spec jsonb;
    v_reply jsonb;
    v_error_code text;
    v_assets jsonb := '[]'::jsonb;
    v_frame public.video_frame_cache%ROWTYPE;
    v_chunk public.video_chunks%ROWTYPE;
    v_exists boolean;
    v_expected_snapshot text;
BEGIN
    IF p_actor_id IS NULL OR p_dataset_item_id IS NULL
       OR pg_catalog.jsonb_typeof(p_selection) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_selection -> 'kind') IS DISTINCT FROM 'string' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    v_kind := p_selection ->> 'kind';
    v_intent := COALESCE(p_selection ->> 'intent', 'access');
    IF v_kind = 'frames' THEN
        IF (p_selection - ARRAY['kind', 'intent', 'expected_source_snapshot', 'frame_indices', 'width', 'format', 'force', 'failed_only'])
                IS DISTINCT FROM '{}'::jsonb
           OR (p_selection ? 'expected_source_snapshot'
               AND pg_catalog.jsonb_typeof(p_selection -> 'expected_source_snapshot') NOT IN ('string', 'null'))
           OR ((p_selection ->> 'expected_source_snapshot') IS NOT NULL
               AND (p_selection ->> 'expected_source_snapshot') !~ '^[0-9a-f]{64}$')
           OR pg_catalog.jsonb_typeof(p_selection -> 'frame_indices') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_selection -> 'frame_indices') > 500
           OR pg_catalog.jsonb_typeof(p_selection -> 'width') IS DISTINCT FROM 'number'
           OR COALESCE(p_selection ->> 'width', '') !~ '^[1-9][0-9]*$'
           OR (p_selection ->> 'width')::numeric > 4096
           OR pg_catalog.jsonb_typeof(p_selection -> 'format') IS DISTINCT FROM 'string'
           OR p_selection ->> 'format' NOT IN ('webp', 'jpeg')
           OR pg_catalog.jsonb_typeof(p_selection -> 'force') IS DISTINCT FROM 'boolean'
           OR pg_catalog.jsonb_typeof(p_selection -> 'failed_only') IS DISTINCT FROM 'boolean'
           OR (p_selection ? 'intent' AND pg_catalog.jsonb_typeof(p_selection -> 'intent') IS DISTINCT FROM 'string')
           OR v_intent NOT IN ('access', 'task_frame_retry')
           OR (v_intent = 'task_frame_retry' AND (
               p_task_id IS NULL
               OR (p_selection -> 'force' = 'true'::jsonb
                   AND p_selection -> 'failed_only' = 'true'::jsonb)
           ))
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_array_elements(p_selection -> 'frame_indices') AS entry(value)
               WHERE pg_catalog.jsonb_typeof(entry.value) IS DISTINCT FROM 'number'
                  OR COALESCE(entry.value #>> '{}', '') !~ '^(0|[1-9][0-9]*)$'
                  OR (entry.value #>> '{}')::numeric > 2147483647
           )
           OR (SELECT count(DISTINCT entry.value #>> '{}')
               FROM pg_catalog.jsonb_array_elements(p_selection -> 'frame_indices') AS entry(value))
              <> pg_catalog.jsonb_array_length(p_selection -> 'frame_indices') THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END IF;
        v_indices := p_selection -> 'frame_indices';
        v_width := (p_selection ->> 'width')::integer;
        v_format := p_selection ->> 'format';
        v_force := (p_selection ->> 'force')::boolean;
        v_failed_only := (p_selection ->> 'failed_only')::boolean;
    ELSIF v_kind = 'chunks' THEN
        IF (p_selection - ARRAY['kind', 'expected_source_snapshot', 'chunk_ids', 'chunk_size_frames', 'force', 'failed_only'])
                IS DISTINCT FROM '{}'::jsonb
           OR (p_selection ? 'expected_source_snapshot'
               AND pg_catalog.jsonb_typeof(p_selection -> 'expected_source_snapshot') NOT IN ('string', 'null'))
           OR ((p_selection ->> 'expected_source_snapshot') IS NOT NULL
               AND (p_selection ->> 'expected_source_snapshot') !~ '^[0-9a-f]{64}$')
           OR pg_catalog.jsonb_typeof(p_selection -> 'chunk_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_selection -> 'chunk_ids') > 500
           OR pg_catalog.jsonb_typeof(p_selection -> 'chunk_size_frames') IS DISTINCT FROM 'number'
           OR COALESCE(p_selection ->> 'chunk_size_frames', '') !~ '^[1-9][0-9]*$'
           OR (p_selection ->> 'chunk_size_frames')::numeric > 1000000
           OR pg_catalog.jsonb_typeof(p_selection -> 'force') IS DISTINCT FROM 'boolean'
           OR pg_catalog.jsonb_typeof(p_selection -> 'failed_only') IS DISTINCT FROM 'boolean'
           OR (p_selection ? 'intent')
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_array_elements(p_selection -> 'chunk_ids') AS entry(value)
               WHERE pg_catalog.jsonb_typeof(entry.value) IS DISTINCT FROM 'number'
                  OR COALESCE(entry.value #>> '{}', '') !~ '^(0|[1-9][0-9]*)$'
                  OR (entry.value #>> '{}')::numeric > 2147483647
           )
           OR (SELECT count(DISTINCT entry.value #>> '{}')
               FROM pg_catalog.jsonb_array_elements(p_selection -> 'chunk_ids') AS entry(value))
              <> pg_catalog.jsonb_array_length(p_selection -> 'chunk_ids') THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END IF;
        v_indices := p_selection -> 'chunk_ids';
        v_chunk_size := (p_selection ->> 'chunk_size_frames')::integer;
        v_force := (p_selection ->> 'force')::boolean;
        v_failed_only := (p_selection ->> 'failed_only')::boolean;
    ELSE
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    IF v_intent = 'task_frame_retry' THEN
        v_access := aap_assets_v1._media_lock_task_annotation_write(
            p_actor_id, p_task_id, p_dataset_item_id
        );
    ELSIF p_task_id IS NULL THEN
        v_access := aap_assets_v1._media_read_visible_video_item(
            p_actor_id, p_dataset_item_id
        );
    ELSE
        v_access := aap_assets_v1._media_read_task_access(
            p_actor_id, p_task_id, p_dataset_item_id
        );
    END IF;
    IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_access;
    END IF;
    v_access_data := v_access -> 'data';
    IF v_intent = 'access'
       AND v_access_data ->> 'file_type' IS DISTINCT FROM 'video' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'task_not_visible');
    END IF;
    SELECT item.* INTO v_item
    FROM public.dataset_items AS item
    WHERE item.id = p_dataset_item_id;
    IF NOT FOUND OR v_item.file_type IS DISTINCT FROM 'video' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_missing');
    END IF;
    v_source_snapshot := aap_assets_v1._media_source_snapshot(v_item);
    IF p_selection ->> 'expected_source_snapshot' IS NOT NULL
       AND p_selection ->> 'expected_source_snapshot' IS DISTINCT FROM v_source_snapshot THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
    END IF;
    IF v_item.media_source_drifted THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
    END IF;
    IF v_kind = 'frames' AND pg_catalog.jsonb_array_length(v_indices) = 0 THEN
        SELECT COALESCE(pg_catalog.jsonb_agg(frame_index ORDER BY frame_index), '[]'::jsonb)
        INTO v_indices
        FROM (
            SELECT frame.frame_index
            FROM public.video_frame_cache AS frame
            WHERE frame.dataset_item_id = p_dataset_item_id
              AND frame.status = 'failed'
              AND frame.width = v_width
              AND frame.format = v_format
            GROUP BY frame.frame_index
            ORDER BY frame.frame_index
            LIMIT 500
        ) AS failed_frames;
    END IF;
    IF pg_catalog.jsonb_array_length(v_indices) > 0 THEN
        IF v_item.media_materialized_generation <> v_item.media_source_generation
           OR pg_catalog.jsonb_typeof(v_item.metadata -> 'video') IS DISTINCT FROM 'object'
           OR COALESCE(v_item.metadata #>> '{video,frame_count}', '') !~ '^[1-9][0-9]*$'
           OR (v_item.metadata #>> '{video,frame_count}')::numeric > 2147483647
           OR pg_catalog.jsonb_typeof(v_item.metadata #> '{video,fps}') IS DISTINCT FROM 'number' THEN
            RETURN aap_assets_v1._media_reply(NULL, 'metadata_not_ready');
        END IF;
        v_frame_count := (v_item.metadata #>> '{video,frame_count}')::integer;
        v_fps := (v_item.metadata #>> '{video,fps}')::numeric;
        IF v_fps <= 0 THEN
            RETURN aap_assets_v1._media_reply(NULL, 'metadata_not_ready');
        END IF;
        IF v_kind = 'frames' AND EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(v_indices) AS entry(value)
            WHERE (entry.value #>> '{}')::integer >= v_frame_count
        ) THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_frame_range');
        END IF;
    END IF;
    v_source := aap_assets_v1._media_source_json(v_item);
    IF COALESCE(v_source ->> 'source_snapshot', '') !~ '^[0-9a-f]{64}$'
       OR v_source ->> 'dataset_item_id' IS DISTINCT FROM p_dataset_item_id::text THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    BEGIN
        FOR v_index IN
            SELECT (entry.value #>> '{}')::integer
            FROM pg_catalog.jsonb_array_elements(v_indices) WITH ORDINALITY AS entry(value, ordinality)
            ORDER BY entry.ordinality
        LOOP
            IF v_kind = 'frames' THEN
                SELECT frame.* INTO v_frame
                FROM public.video_frame_cache AS frame
                WHERE frame.dataset_item_id = p_dataset_item_id
                  AND frame.frame_index = v_index
                  AND frame.width = v_width
                  AND frame.format = v_format;
                v_exists := FOUND;
                IF v_failed_only AND (NOT v_exists OR v_frame.status IS DISTINCT FROM 'failed') THEN
                    CONTINUE;
                END IF;
                v_old_key := CASE WHEN v_exists THEN v_frame.storage_key ELSE NULL END;
                INSERT INTO public.video_frame_cache(
                    id, dataset_item_id, frame_index, width, format, storage_key,
                    byte_size, status, error, source_generation, timetable_revision,
                    created_at, updated_at
                ) VALUES (
                    pg_catalog.gen_random_uuid(), p_dataset_item_id, v_index, v_width, v_format,
                    NULL, NULL, 'pending', NULL, v_item.media_source_generation,
                    v_item.media_timetable_revision, pg_catalog.now(), pg_catalog.now()
                )
                ON CONFLICT (dataset_item_id, frame_index, width, format) DO UPDATE SET
                    storage_key = CASE
                        WHEN v_force OR public.video_frame_cache.source_generation <> EXCLUDED.source_generation
                          OR public.video_frame_cache.timetable_revision <> EXCLUDED.timetable_revision
                        THEN NULL ELSE public.video_frame_cache.storage_key END,
                    byte_size = CASE
                        WHEN v_force OR public.video_frame_cache.source_generation <> EXCLUDED.source_generation
                          OR public.video_frame_cache.timetable_revision <> EXCLUDED.timetable_revision
                        THEN NULL ELSE public.video_frame_cache.byte_size END,
                    status = CASE
                        WHEN v_force OR public.video_frame_cache.source_generation <> EXCLUDED.source_generation
                          OR public.video_frame_cache.timetable_revision <> EXCLUDED.timetable_revision
                        THEN 'pending' ELSE public.video_frame_cache.status END,
                    error = CASE WHEN v_force OR public.video_frame_cache.source_generation <> EXCLUDED.source_generation
                                  OR public.video_frame_cache.timetable_revision <> EXCLUDED.timetable_revision
                                 THEN NULL ELSE public.video_frame_cache.error END,
                    source_generation = EXCLUDED.source_generation,
                    timetable_revision = EXCLUDED.timetable_revision,
                    updated_at = pg_catalog.now();
                IF v_old_key IS NOT NULL AND (v_force OR v_frame.source_generation <> v_item.media_source_generation
                    OR v_frame.timetable_revision <> v_item.media_timetable_revision) THEN
                    INSERT INTO public.asset_media_garbage(prefix, keys, reason, created_at)
                    VALUES (NULL, pg_catalog.jsonb_build_array(v_old_key), 'cache_expired', pg_catalog.now());
                END IF;
                v_spec := pg_catalog.jsonb_build_object(
                    'kind', 'frame', 'frame_index', v_index, 'width', v_width,
                    'format', v_format, 'expected_source_snapshot', v_source_snapshot
                );
                v_reply := aap_assets_v1._media_enqueue(NULL, p_actor_id, p_dataset_item_id, v_spec, v_force);
                IF v_reply IS NULL OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                    v_error_code := COALESCE(v_reply #>> '{error,code}', 'invalid_response');
                    RAISE EXCEPTION 'media enqueue rejected';
                END IF;
                SELECT frame.* INTO v_frame
                FROM public.video_frame_cache AS frame
                WHERE frame.dataset_item_id = p_dataset_item_id
                  AND frame.frame_index = v_index
                  AND frame.width = v_width
                  AND frame.format = v_format;
                v_assets := v_assets || pg_catalog.jsonb_build_array(
                    aap_assets_v1._media_frame_json(v_frame)
                );
            ELSE
                IF v_index::numeric * v_chunk_size > 2147483647 THEN
                    v_error_code := 'invalid_frame_range';
                    RAISE EXCEPTION 'chunk range overflow';
                END IF;
                v_start := v_index * v_chunk_size;
                IF v_start >= v_frame_count THEN
                    v_error_code := 'invalid_frame_range';
                    RAISE EXCEPTION 'chunk starts past video end';
                END IF;
                v_end := LEAST(
                    v_frame_count::bigint - 1,
                    v_start::bigint + v_chunk_size - 1
                )::integer;
                SELECT chunk.* INTO v_chunk
                FROM public.video_chunks AS chunk
                WHERE chunk.dataset_item_id = p_dataset_item_id AND chunk.chunk_id = v_index;
                v_exists := FOUND;
                IF v_failed_only AND (NOT v_exists OR v_chunk.status IS DISTINCT FROM 'failed') THEN
                    CONTINUE;
                END IF;
                v_old_key := CASE WHEN v_exists THEN v_chunk.storage_key ELSE NULL END;
                INSERT INTO public.video_chunks(
                    id, dataset_item_id, chunk_id, start_frame, end_frame, start_pts_ms,
                    end_pts_ms, storage_key, byte_size, generation_mode, diagnostics,
                    status, error, source_generation, timetable_revision, created_at, updated_at
                ) VALUES (
                    pg_catalog.gen_random_uuid(), p_dataset_item_id, v_index, v_start, v_end,
                    NULL, NULL, NULL, NULL, NULL, '{}'::jsonb, 'pending', NULL,
                    v_item.media_source_generation, v_item.media_timetable_revision,
                    pg_catalog.now(), pg_catalog.now()
                )
                ON CONFLICT (dataset_item_id, chunk_id) DO UPDATE SET
                    start_frame = EXCLUDED.start_frame,
                    end_frame = EXCLUDED.end_frame,
                    storage_key = CASE
                        WHEN v_force OR public.video_chunks.start_frame <> EXCLUDED.start_frame
                          OR public.video_chunks.end_frame <> EXCLUDED.end_frame
                          OR public.video_chunks.source_generation <> EXCLUDED.source_generation
                          OR public.video_chunks.timetable_revision <> EXCLUDED.timetable_revision
                        THEN NULL ELSE public.video_chunks.storage_key END,
                    byte_size = CASE
                        WHEN v_force OR public.video_chunks.start_frame <> EXCLUDED.start_frame
                          OR public.video_chunks.end_frame <> EXCLUDED.end_frame
                          OR public.video_chunks.source_generation <> EXCLUDED.source_generation
                          OR public.video_chunks.timetable_revision <> EXCLUDED.timetable_revision
                        THEN NULL ELSE public.video_chunks.byte_size END,
                    status = CASE
                        WHEN v_force OR public.video_chunks.start_frame <> EXCLUDED.start_frame
                          OR public.video_chunks.end_frame <> EXCLUDED.end_frame
                          OR public.video_chunks.source_generation <> EXCLUDED.source_generation
                          OR public.video_chunks.timetable_revision <> EXCLUDED.timetable_revision
                        THEN 'pending' ELSE public.video_chunks.status END,
                    error = CASE WHEN v_force OR public.video_chunks.start_frame <> EXCLUDED.start_frame
                                  OR public.video_chunks.end_frame <> EXCLUDED.end_frame
                                  OR public.video_chunks.source_generation <> EXCLUDED.source_generation
                                  OR public.video_chunks.timetable_revision <> EXCLUDED.timetable_revision
                                 THEN NULL ELSE public.video_chunks.error END,
                    generation_mode = CASE WHEN v_force THEN NULL ELSE public.video_chunks.generation_mode END,
                    diagnostics = CASE WHEN v_force THEN '{}'::jsonb ELSE public.video_chunks.diagnostics END,
                    source_generation = EXCLUDED.source_generation,
                    timetable_revision = EXCLUDED.timetable_revision,
                    updated_at = pg_catalog.now();
                IF v_old_key IS NOT NULL AND (v_force OR v_chunk.start_frame <> v_start OR v_chunk.end_frame <> v_end
                    OR v_chunk.source_generation <> v_item.media_source_generation
                    OR v_chunk.timetable_revision <> v_item.media_timetable_revision) THEN
                    INSERT INTO public.asset_media_garbage(prefix, keys, reason, created_at)
                    VALUES (NULL, pg_catalog.jsonb_build_array(v_old_key), 'cache_expired', pg_catalog.now());
                END IF;
                v_spec := pg_catalog.jsonb_build_object(
                    'kind', 'chunk', 'chunk_id', v_index, 'chunk_size_frames', v_chunk_size,
                    'expected_source_snapshot', v_source_snapshot
                );
                v_reply := aap_assets_v1._media_enqueue(NULL, p_actor_id, p_dataset_item_id, v_spec, v_force);
                IF v_reply IS NULL OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                    v_error_code := COALESCE(v_reply #>> '{error,code}', 'invalid_response');
                    RAISE EXCEPTION 'media enqueue rejected';
                END IF;
                SELECT chunk.* INTO v_chunk
                FROM public.video_chunks AS chunk
                WHERE chunk.dataset_item_id = p_dataset_item_id AND chunk.chunk_id = v_index;
                v_assets := v_assets || pg_catalog.jsonb_build_array(
                    aap_assets_v1._media_chunk_json(v_chunk)
                );
            END IF;
        END LOOP;
    EXCEPTION WHEN OTHERS THEN
        RETURN aap_assets_v1._media_reply(NULL, COALESCE(v_error_code, 'invalid_response'));
    END;
    RETURN aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object('source', v_source, 'assets', v_assets), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.ensure_video_media(uuid, uuid, uuid, jsonb)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.read_video_media_totals(p_item_ids uuid[])
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_request_bytes integer;
    v_duration_total numeric := 0;
    v_frame_total numeric := 0;
    v_matched_items bigint := 0;
    v_invalid_metadata boolean := false;
    v_reply jsonb;
BEGIN
    IF p_item_ids IS NULL THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    IF pg_catalog.cardinality(p_item_ids) > 200000
       OR (
           pg_catalog.cardinality(p_item_ids) > 0
           AND pg_catalog.array_ndims(p_item_ids) IS DISTINCT FROM 1
       )
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.unnest(p_item_ids) AS requested(item_id)
           WHERE requested.item_id IS NULL
       ) THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    v_request_bytes := pg_catalog.octet_length(
        pg_catalog.jsonb_build_object(
            'item_ids', pg_catalog.to_jsonb(p_item_ids)
        )::text
    );
    IF v_request_bytes > 8388608 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'resource_budget_exceeded');
    END IF;

    WITH requested AS (
        SELECT requested.item_id, pg_catalog.count(*)::bigint AS weight
        FROM pg_catalog.unnest(p_item_ids) AS requested(item_id)
        GROUP BY requested.item_id
    ),
    resolved AS (
        SELECT item.id AS matched_item_id,
               requested.weight,
               CASE
                   WHEN item.file_type = 'video'
                    AND (
                        item.media_source_drifted IS DISTINCT FROM false
                        OR item.media_materialized_generation
                           IS DISTINCT FROM item.media_source_generation
                    ) THEN NULL
                   WHEN pg_catalog.jsonb_typeof(item.metadata -> 'video') = 'object'
                       THEN item.metadata #>> '{video,duration_ms}'
                   ELSE NULL
               END AS duration_text,
               CASE
                   WHEN item.file_type = 'video'
                    AND (
                        item.media_source_drifted IS DISTINCT FROM false
                        OR item.media_materialized_generation
                           IS DISTINCT FROM item.media_source_generation
                    ) THEN NULL
                   WHEN pg_catalog.jsonb_typeof(item.metadata -> 'video') = 'object'
                       THEN item.metadata #>> '{video,frame_count}'
                   ELSE NULL
               END AS frame_count_text
        FROM requested
        LEFT JOIN public.dataset_items AS item
            ON item.id = requested.item_id
    ),
    parsed AS (
        SELECT resolved.*,
               CASE
                   WHEN resolved.duration_text IS NULL THEN 0::numeric
                   WHEN resolved.duration_text ~ '^[[:space:]]*[+-]?[0-9]+[[:space:]]*$'
                       THEN pg_catalog.regexp_replace(
                           resolved.duration_text,
                           '^[[:space:]]+|[[:space:]]+$',
                           '',
                           'g'
                       )::numeric
                   ELSE NULL::numeric
               END AS duration_value,
               CASE
                   WHEN resolved.frame_count_text IS NULL THEN 0::numeric
                   WHEN resolved.frame_count_text ~ '^[[:space:]]*[+-]?[0-9]+[[:space:]]*$'
                       THEN pg_catalog.regexp_replace(
                           resolved.frame_count_text,
                           '^[[:space:]]+|[[:space:]]+$',
                           '',
                           'g'
                       )::numeric
                   ELSE NULL::numeric
               END AS frame_count_value
        FROM resolved
    )
    SELECT COALESCE(
               pg_catalog.sum(parsed.weight)
                   FILTER (WHERE parsed.matched_item_id IS NOT NULL),
               0
           )::bigint,
           COALESCE(
               pg_catalog.sum(
                   CASE
                       WHEN parsed.matched_item_id IS NOT NULL
                        AND parsed.duration_value BETWEEN 0 AND 9223372036854775807
                           THEN parsed.duration_value * parsed.weight::numeric
                       ELSE 0::numeric
                   END
               ),
               0
           ),
           COALESCE(
               pg_catalog.sum(
                   CASE
                       WHEN parsed.matched_item_id IS NOT NULL
                        AND parsed.frame_count_value BETWEEN 0 AND 9223372036854775807
                           THEN parsed.frame_count_value * parsed.weight::numeric
                       ELSE 0::numeric
                   END
               ),
               0
           ),
           COALESCE(
               pg_catalog.bool_or(
                   parsed.matched_item_id IS NOT NULL
                   AND parsed.duration_text IS NOT NULL
                   AND (
                       parsed.duration_value IS NULL
                       OR parsed.duration_value < 0
                       OR parsed.duration_value > 9223372036854775807
                   )
                   OR parsed.matched_item_id IS NOT NULL
                   AND parsed.frame_count_text IS NOT NULL
                   AND (
                       parsed.frame_count_value IS NULL
                       OR parsed.frame_count_value < 0
                       OR parsed.frame_count_value > 9223372036854775807
                   )
               ),
               false
           )
    INTO v_matched_items, v_duration_total, v_frame_total, v_invalid_metadata
    FROM parsed;
    IF v_invalid_metadata
       OR v_duration_total > 9223372036854775807
       OR v_frame_total > 9223372036854775807 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;

    v_reply := aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object(
            'frame_count', v_frame_total::bigint,
            'duration_ms', v_duration_total::bigint,
            'matched_items', v_matched_items
        ),
        NULL
    );
    IF pg_catalog.octet_length(v_reply::text) > 16777216 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'resource_budget_exceeded');
    END IF;
    RETURN v_reply;
EXCEPTION
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_video_media_totals(uuid[])
    FROM PUBLIC;
-- aap-statement-break
