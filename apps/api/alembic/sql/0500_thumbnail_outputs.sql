CREATE FUNCTION yoao_labeling_v1._legacy_task_thumbnail_business(p_task uuid, p_lock boolean)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF p_lock THEN
        PERFORM task.id FROM public.tasks AS task WHERE task.id = p_task FOR UPDATE NOWAIT;
        IF NOT FOUND THEN RETURN NULL; END IF;
    END IF;
    RETURN (SELECT pg_catalog.jsonb_build_object(
                'id', task.id, 'dataset_id', NULL, 'dataset_item_id', task.dataset_item_id,
                'project_id', task.project_id, 'file_type', task.file_type, 'width', NULL, 'height', NULL,
                'thumbnail_output_id', task.thumbnail_output_id)
            FROM public.tasks AS task WHERE task.id = p_task);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1._legacy_task_thumbnail_referenced(p_output uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (SELECT 1 FROM public.tasks AS task WHERE task.thumbnail_output_id = p_output);
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1._publish_legacy_task_thumbnail(p_task uuid, p_output uuid, p_blurhash text)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    WITH ready AS MATERIALIZED (
        SELECT output.id, (output.storage_ref ->> 'prefix') || 'thumbnail.webp' AS object_key
        FROM public.thumbnail_outputs AS output
        JOIN public.tasks AS task ON task.id = p_task
        WHERE output.id = p_output AND output.owner_kind = 'task_thumbnail' AND output.owner_id = p_task
          AND output.state = 'ready' AND NOT output.uncertain_write AND task.file_type = 'image'
          AND output.input_ref #>> '{source,relation,task_id}' = task.id::text
          AND output.input_ref #>> '{source,relation,project_id}' = task.project_id::text
          AND output.input_ref #>> '{source,relation,direct_item_id}' IS NOT DISTINCT FROM task.dataset_item_id::text
    ), published AS (
        UPDATE public.tasks AS task
        SET thumbnail_output_id = ready.id, thumbnail_path = ready.object_key,
            blurhash = p_blurhash, updated_at = pg_catalog.now()
        FROM ready
        WHERE task.id = p_task AND (task.thumbnail_output_id, task.thumbnail_path, task.blurhash)
            IS DISTINCT FROM (ready.id, ready.object_key, p_blurhash)
        RETURNING task.id
    )
    SELECT EXISTS (SELECT 1 FROM ready);
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._legacy_thumbnail_facts(p_output public.thumbnail_outputs)
RETURNS jsonb
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', (p_output).id, 'owner_kind', (p_output).owner_kind,
        'owner_id', (p_output).owner_id, 'path_digest', (p_output).path_digest,
        'input_ref', (p_output).input_ref, 'storage_ref', (p_output).storage_ref,
        'state', (p_output).state, 'uncertain_write', (p_output).uncertain_write,
        'sha256', (p_output).sha256, 'size_bytes', (p_output).size_bytes,
        'etag', (p_output).etag, 'version_id', (p_output).version_id,
        'writer_until', (p_output).writer_until
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_thumbnail_output(p_id uuid, p_lock boolean)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF p_lock THEN
        RETURN (SELECT yoao_assets_v1._legacy_thumbnail_facts(output)
                FROM public.thumbnail_outputs AS output
                WHERE output.id = p_id AND output.state IN ('ready', 'retiring')
                FOR SHARE NOWAIT);
    END IF;
    RETURN (SELECT yoao_assets_v1._legacy_thumbnail_facts(output)
            FROM public.thumbnail_outputs AS output
            WHERE output.id = p_id AND output.state IN ('ready', 'retiring'));
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._legacy_media_cache_target_current(p_ref jsonb)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.storage_locations AS location
        JOIN public.storage_maintenance_gates AS gate ON gate.location_id = location.id
        LEFT JOIN public.storage_bindings AS binding
          ON binding.purpose = 'media-cache' AND binding.location_id = location.id
        LEFT JOIN public.storage_connections AS connection ON connection.id = location.connection_id
        LEFT JOIN yoao_assets_v1.fixture_native_cache_targets AS fixture ON fixture.location_id = location.id
        WHERE location.id = (p_ref ->> 'location_id')::uuid
          AND p_ref -> 'schema' = '2'::jsonb AND p_ref ->> 'purpose' = 'media-cache'
          AND location.kind = 's3' AND location.enabled AND gate.state = 'active'
          AND pg_catalog.to_jsonb(location.config_revision) = p_ref -> 'config_revision'
          AND p_ref ->> 'storage_identity' = pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
              pg_catalog.jsonb_build_array(p_ref ->> 'endpoint_url', p_ref ->> 'bucket')::text, 'UTF8'
          )), 'hex')
          AND (
              (fixture.location_id IS NOT NULL
               AND fixture.endpoint = p_ref ->> 'endpoint_url'
               AND fixture.bucket = p_ref ->> 'bucket'
               AND pg_catalog.to_jsonb(fixture.binding_generation) = p_ref -> 'binding_generation'
               AND fixture.storage_identity = p_ref ->> 'storage_identity'
               AND p_ref ->> 'key_prefix' = '_aap_managed/media-cache/')
              OR
              (fixture.location_id IS NULL AND binding.bucket = p_ref ->> 'bucket'
               AND pg_catalog.to_jsonb(binding.generation) = p_ref -> 'binding_generation'
               AND CASE WHEN location.id = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7' THEN
                   location.connection_id IS NULL AND p_ref ->> 'key_prefix' = ''
                   AND ((CASE WHEN location.config -> 'use_ssl' = 'true'::jsonb THEN 'https://' ELSE 'http://' END)
                       || (location.config ->> 'endpoint')) = p_ref ->> 'endpoint_url'
               ELSE
                   binding.generation > 1 AND connection.kind = 's3'
                   AND connection.scope = 'global' AND connection.project_id IS NULL
                   AND connection.config ->> 'bucket' = p_ref ->> 'bucket'
                   AND pg_catalog.rtrim((CASE
                       WHEN pg_catalog.strpos(connection.config ->> 'endpoint', '://') > 0 THEN ''
                       WHEN connection.config -> 'use_ssl' = 'true'::jsonb THEN 'https://' ELSE 'http://' END)
                       || (connection.config ->> 'endpoint'), '/') = p_ref ->> 'endpoint_url'
                   AND COALESCE(NULLIF(pg_catalog.regexp_replace(connection.config ->> 'base_prefix', '/$', ''), '') || '/', '')
                       || '_aap_managed/media-cache/' = p_ref ->> 'key_prefix'
               END)
          )
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.allocate_legacy_thumbnail(
    p_id uuid, p_kind text, p_owner uuid, p_path_digest text,
    p_input jsonb, p_ref jsonb, p_allocated_at timestamptz
) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NOT yoao_assets_v1._legacy_media_cache_target_current(p_ref) THEN
        RETURN false;
    END IF;
    INSERT INTO public.thumbnail_outputs (
        id, owner_kind, owner_id, path_digest, input_ref, storage_ref,
        writer_until, next_check_at, uncertain_write
    ) VALUES (
        p_id, p_kind, p_owner, p_path_digest, p_input, p_ref,
        p_allocated_at + interval '2 minutes', p_allocated_at + interval '2 minutes', true
    );
    RETURN true;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.record_legacy_thumbnail_result(
    p_id uuid, p_ref jsonb, p_sha text, p_size bigint, p_etag text, p_version text,
    p_ready boolean, p_uncertain boolean, p_at timestamptz
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_output public.thumbnail_outputs%ROWTYPE;
BEGIN
    SELECT output.* INTO v_output FROM public.thumbnail_outputs AS output
    WHERE output.id = p_id FOR UPDATE;
    IF NOT FOUND OR v_output.state <> 'building' OR v_output.storage_ref IS DISTINCT FROM p_ref
       OR p_ready IS NULL OR p_uncertain IS NULL
       OR (p_ready AND (p_uncertain OR p_sha IS NULL OR p_at IS NULL))
       OR (p_sha IS NULL AND (p_size IS NOT NULL OR p_etag IS NOT NULL OR p_version IS NOT NULL))
       OR (p_sha IS NOT NULL AND (
           v_output.input_ref -> 'encoded' IS DISTINCT FROM pg_catalog.jsonb_build_object('sha256', p_sha, 'size_bytes', p_size)
           OR NOT COALESCE(p_size BETWEEN 1 AND 2097152 AND pg_catalog.length(p_etag) BETWEEN 1 AND 1024, false)
           OR p_etag COLLATE pg_catalog."C" ~ '[[:cntrl:]]'
           OR (p_version IS NOT NULL AND (p_version = 'null' OR pg_catalog.length(p_version) NOT BETWEEN 1 AND 1024))
       )) THEN
        RETURN NULL;
    END IF;
    UPDATE public.thumbnail_outputs AS output
    SET sha256 = p_sha, size_bytes = p_size, etag = p_etag, version_id = p_version,
        state = CASE WHEN p_ready THEN 'ready' ELSE 'failed' END,
        ready_at = CASE WHEN p_ready THEN p_at END,
        uncertain_write = p_uncertain,
        error_code = CASE WHEN p_ready THEN NULL WHEN p_uncertain THEN 'thumbnail_write_unproven' ELSE 'thumbnail_write_rejected' END,
        updated_at = pg_catalog.now()
    WHERE output.id = p_id
    RETURNING output.* INTO v_output;
    RETURN yoao_assets_v1._legacy_thumbnail_facts(v_output);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_item_thumbnail_pointer(p_item uuid, p_path text)
RETURNS TABLE(output_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT item.thumbnail_output_id FROM public.dataset_items AS item
    WHERE item.id = p_item AND item.thumbnail_path = p_path;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_thumbnail_business(p_kind text, p_id uuid, p_lock boolean)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF p_kind = 'dataset_item' THEN
        IF p_lock THEN
            PERFORM item.id FROM public.dataset_items AS item WHERE item.id = p_id FOR UPDATE NOWAIT;
            IF NOT FOUND THEN RETURN NULL; END IF;
        END IF;
        RETURN (SELECT pg_catalog.jsonb_build_object(
                    'id', item.id, 'dataset_id', item.dataset_id, 'dataset_item_id', NULL,
                    'project_id', NULL, 'file_type', item.file_type, 'width', item.width, 'height', item.height,
                    'thumbnail_output_id', item.thumbnail_output_id)
                FROM public.dataset_items AS item WHERE item.id = p_id);
    END IF;
    IF p_kind = 'task' THEN
        RETURN yoao_labeling_v1._legacy_task_thumbnail_business(p_id, p_lock);
    END IF;
    RETURN NULL;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.set_legacy_item_dimensions(p_item uuid, p_width integer, p_height integer)
RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    UPDATE public.dataset_items AS item
    SET width = p_width, height = p_height, updated_at = pg_catalog.now()
    WHERE item.id = p_item AND item.file_type = 'image'
      AND (item.width, item.height) IS DISTINCT FROM (p_width, p_height);
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.record_legacy_item_thumbnail_error(p_item uuid, p_error text)
RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    UPDATE public.dataset_items AS item
    SET metadata = COALESCE(NULLIF(item.metadata, 'null'::jsonb), '{}'::jsonb)
        || pg_catalog.jsonb_build_object('thumbnail_error', p_error), updated_at = pg_catalog.now()
    WHERE item.id = p_item AND item.file_type = 'image'
      AND (item.metadata IS NULL OR item.metadata = 'null'::jsonb OR pg_catalog.jsonb_typeof(item.metadata) = 'object')
      AND item.metadata -> 'thumbnail_error' IS DISTINCT FROM COALESCE(pg_catalog.to_jsonb(p_error), 'null'::jsonb);
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.publish_legacy_thumbnail(
    p_kind text, p_owner uuid, p_id uuid, p_input jsonb, p_ref jsonb,
    p_blurhash text, p_at timestamptz
) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_owner jsonb;
    v_kind text;
    v_previous uuid;
    v_output public.thumbnail_outputs%ROWTYPE;
    v_key text;
BEGIN
    v_owner := yoao_assets_v1.legacy_thumbnail_business(p_kind, p_owner, true);
    IF v_owner ->> 'file_type' IS DISTINCT FROM 'image' OR p_at IS NULL THEN RETURN false; END IF;
    IF p_kind = 'dataset_item' THEN
        v_kind := 'dataset_thumbnail';
        IF p_input #>> '{source,item_id}' IS DISTINCT FROM p_owner::text THEN RETURN false; END IF;
    ELSE
        v_kind := 'task_thumbnail';
        IF p_input #>> '{source,relation,task_id}' IS DISTINCT FROM p_owner::text
           OR p_input #>> '{source,relation,project_id}' IS DISTINCT FROM (v_owner ->> 'project_id')
           OR p_input #>> '{source,relation,direct_item_id}' IS DISTINCT FROM (v_owner ->> 'dataset_item_id')
        THEN RETURN false; END IF;
    END IF;
    IF yoao_assets_v1.dataset_owns_directory_media((p_input #>> '{source,item_id}')::uuid) THEN
        RETURN false;
    END IF;
    SELECT output.* INTO v_output FROM public.thumbnail_outputs AS output
    WHERE output.id = p_id FOR SHARE NOWAIT;
    IF NOT FOUND OR v_output.state <> 'ready' OR v_output.uncertain_write
       OR (v_output.owner_kind, v_output.owner_id, v_output.input_ref, v_output.storage_ref)
          IS DISTINCT FROM (v_kind, p_owner, p_input, p_ref)
    THEN RETURN false; END IF;
    v_previous := (v_owner ->> 'thumbnail_output_id')::uuid;
    IF v_previous IS NOT NULL AND v_previous <> p_id THEN
        PERFORM output.id FROM public.thumbnail_outputs AS output
        WHERE output.id = v_previous FOR UPDATE NOWAIT;
        UPDATE public.thumbnail_outputs AS output
        SET state = 'retiring', retired_at = p_at, next_check_at = p_at, updated_at = pg_catalog.now()
        WHERE output.id = v_previous AND output.owner_kind = v_kind AND output.owner_id = p_owner
          AND output.state NOT IN ('retiring', 'deleted');
    END IF;
    IF p_kind = 'dataset_item' THEN
        v_key := (v_output.storage_ref ->> 'prefix') || 'thumbnail.webp';
        UPDATE public.dataset_items AS item
        SET thumbnail_output_id = p_id, thumbnail_path = v_key, blurhash = p_blurhash, updated_at = pg_catalog.now()
        WHERE item.id = p_owner AND (item.thumbnail_output_id, item.thumbnail_path, item.blurhash)
            IS DISTINCT FROM (p_id, v_key, p_blurhash);
    ELSE
        RETURN yoao_labeling_v1._publish_legacy_task_thumbnail(p_owner, p_id, p_blurhash);
    END IF;
    RETURN true;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_directory_thumbnail_candidate(
    p_id uuid, p_source uuid, p_path_digest text, p_directory jsonb
) RETURNS jsonb
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT yoao_assets_v1._legacy_thumbnail_facts(output)
    FROM public.thumbnail_outputs AS output
    WHERE output.id = p_id AND output.state = 'ready' AND NOT output.uncertain_write
      AND output.owner_kind = 'directory_preview' AND output.owner_id = p_source
      AND output.path_digest = p_path_digest AND output.input_ref -> 'directory' = p_directory
    FOR SHARE NOWAIT;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.retire_legacy_directory_thumbnail(p_id uuid, p_retired_at timestamptz, p_next_check_at timestamptz)
RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    PERFORM output.id FROM public.thumbnail_outputs AS output
    WHERE output.id = p_id AND output.owner_kind = 'directory_preview' FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN; END IF;
    UPDATE public.thumbnail_outputs AS output
    SET state = 'retiring', retired_at = p_retired_at, next_check_at = p_next_check_at, updated_at = pg_catalog.now()
    WHERE output.id = p_id AND output.state = 'ready'
      AND EXISTS (SELECT 1 FROM public.dataset_source_previews AS preview
                  WHERE preview.source_id = output.owner_id AND preview.path_digest = output.path_digest
                    AND preview.thumbnail_output_id = output.id);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_thumbnail_evidence(p_sources uuid[])
RETURNS TABLE(preview jsonb, output jsonb, source_location_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT pg_catalog.jsonb_build_object(
               'source_id', preview.source_id, 'path_digest', preview.path_digest,
               'thumbnail_output_id', preview.thumbnail_output_id,
               'source_sha256', preview.source_sha256, 'spec', preview.spec,
               'location_id', preview.location_id, 'bucket', preview.bucket,
               'object_key', preview.object_key, 'sha256', preview.sha256, 'size_bytes', preview.size_bytes),
           yoao_assets_v1._legacy_thumbnail_facts(output), source.location_id
    FROM public.dataset_source_previews AS preview
    JOIN public.dataset_sources AS source ON source.id = preview.source_id
    LEFT JOIN public.thumbnail_outputs AS output
      ON output.id = preview.thumbnail_output_id AND output.owner_kind = 'directory_preview'
         AND output.owner_id = preview.source_id AND output.path_digest = preview.path_digest
         AND output.state = 'ready' AND NOT output.uncertain_write
    WHERE preview.source_id = ANY(p_sources) AND preview.state = 'ready';
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._legacy_thumbnail_gc_clear(p_id uuid, p_excluding uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT yoao_assets_v1.fixture_media_ordinary_gc('thumbnail', p_id, 0)
       AND NOT EXISTS (SELECT 1 FROM public.dataset_items AS item WHERE item.thumbnail_output_id = p_id)
       AND NOT yoao_labeling_v1._legacy_task_thumbnail_referenced(p_id)
       AND NOT EXISTS (SELECT 1 FROM public.dataset_source_previews AS preview WHERE preview.thumbnail_output_id = p_id)
       AND NOT EXISTS (
           SELECT 1 FROM public.storage_operations AS operation
           WHERE operation.purpose = 'thumbnail_' || pg_catalog.replace(p_id::text, '-', '')
             AND operation.id IS DISTINCT FROM p_excluding
       );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_thumbnail_gc_candidates(p_limit integer, p_now timestamptz, p_grace integer)
RETURNS TABLE(id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT output.id FROM public.thumbnail_outputs AS output
    WHERE p_grace >= 3600 AND NOT output.uncertain_write
      AND output.state IN ('ready', 'retiring', 'failed') AND output.next_check_at <= p_now
      AND (output.state = 'retiring' OR output.updated_at <= p_now - pg_catalog.make_interval(secs => p_grace))
      AND yoao_assets_v1._legacy_thumbnail_gc_clear(output.id, NULL)
    ORDER BY output.next_check_at, output.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1000 THEN p_limit ELSE 0 END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_thumbnail_gc_start(p_id uuid, p_now timestamptz, p_grace integer)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_output public.thumbnail_outputs%ROWTYPE;
    v_grace interval := pg_catalog.make_interval(secs => p_grace);
BEGIN
    IF p_now IS NULL OR p_grace IS NULL OR p_grace < 3600 THEN RETURN NULL; END IF;
    SELECT output.* INTO v_output FROM public.thumbnail_outputs AS output
    WHERE output.id = p_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_output.state NOT IN ('ready', 'retiring', 'failed')
       OR v_output.writer_until > p_now OR v_output.next_check_at > p_now OR v_output.uncertain_write
       OR (v_output.state <> 'retiring' AND v_output.updated_at > p_now - v_grace)
       OR NOT yoao_assets_v1._legacy_thumbnail_gc_clear(p_id, NULL)
    THEN RETURN NULL; END IF;
    IF v_output.sha256 IS NULL THEN
        IF v_output.state <> 'failed' THEN RETURN NULL; END IF;
        UPDATE public.thumbnail_outputs SET state = 'deleted', updated_at = pg_catalog.now() WHERE id = p_id;
        RETURN pg_catalog.jsonb_build_object('action', 'checkpoint');
    END IF;
    IF v_output.state = 'ready' THEN
        UPDATE public.thumbnail_outputs
        SET state = 'retiring', retired_at = p_now, next_check_at = p_now + v_grace, updated_at = pg_catalog.now()
        WHERE id = p_id;
        RETURN pg_catalog.jsonb_build_object('action', 'checkpoint');
    END IF;
    IF v_output.state <> 'retiring' OR v_output.retired_at IS NULL OR v_output.retired_at > p_now - v_grace THEN
        RETURN NULL;
    END IF;
    RETURN pg_catalog.jsonb_build_object('action', 'remove', 'output', yoao_assets_v1._legacy_thumbnail_facts(v_output));
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._legacy_thumbnail_gc_recheck(
    p_id uuid, p_receipt jsonb, p_permit uuid, p_now timestamptz, p_grace integer
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_output public.thumbnail_outputs%ROWTYPE;
BEGIN
    IF p_now IS NULL OR p_grace IS NULL OR p_grace < 3600 THEN RETURN NULL; END IF;
    SELECT output.* INTO v_output FROM public.thumbnail_outputs AS output
    WHERE output.id = p_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_output.state <> 'retiring' OR v_output.uncertain_write
       OR v_output.writer_until > p_now OR v_output.sha256 IS NULL
       OR v_output.retired_at IS NULL OR v_output.retired_at > p_now - pg_catalog.make_interval(secs => p_grace)
       OR pg_catalog.jsonb_build_array(v_output.storage_ref, v_output.sha256, v_output.size_bytes, v_output.etag, v_output.version_id)
          IS DISTINCT FROM p_receipt
       OR NOT yoao_assets_v1._legacy_thumbnail_gc_clear(p_id, p_permit)
       OR (p_permit IS NOT NULL AND NOT EXISTS (
           SELECT 1 FROM public.storage_operations AS operation
           WHERE operation.id = p_permit AND operation.location_id = (v_output.storage_ref ->> 'location_id')::uuid
             AND operation.purpose = 'thumbnail_' || pg_catalog.replace(p_id::text, '-', '')
       ))
    THEN RETURN NULL; END IF;
    RETURN yoao_assets_v1._legacy_thumbnail_facts(v_output);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_thumbnail_gc_admit(
    p_id uuid, p_receipt jsonb, p_permit uuid, p_now timestamptz, p_grace integer
) RETURNS jsonb
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT yoao_assets_v1._legacy_thumbnail_gc_recheck(p_id, p_receipt, p_permit, p_now, p_grace)
    WHERE p_permit IS NOT NULL;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_thumbnail_gc_finish(p_id uuid, p_receipt jsonb, p_now timestamptz, p_grace integer)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF yoao_assets_v1._legacy_thumbnail_gc_recheck(p_id, p_receipt, NULL, p_now, p_grace) IS NULL THEN
        RETURN false;
    END IF;
    UPDATE public.thumbnail_outputs SET state = 'deleted', updated_at = pg_catalog.now() WHERE id = p_id;
    RETURN FOUND;
END;
$function$;
