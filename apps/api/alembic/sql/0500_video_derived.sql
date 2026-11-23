CREATE FUNCTION yoao_assets_v1.legacy_video_item(p_item uuid, p_lock text)
RETURNS TABLE (id uuid, file_type text, file_path text, width integer, height integer,
    video_output_id uuid, video_building_output_id uuid)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock = 'read' THEN
        RETURN QUERY SELECT item.id, item.file_type::text, item.file_path::text,
            item.width, item.height, item.video_output_id, item.video_building_output_id
        FROM public.dataset_items AS item WHERE item.id = p_item AND item.file_type = 'video';
    ELSIF p_lock = 'share' THEN
        RETURN QUERY SELECT item.id, item.file_type::text, item.file_path::text,
            item.width, item.height, item.video_output_id, item.video_building_output_id
        FROM public.dataset_items AS item WHERE item.id = p_item AND item.file_type = 'video'
        FOR SHARE OF item NOWAIT;
    ELSIF p_lock = 'update' THEN
        RETURN QUERY SELECT item.id, item.file_type::text, item.file_path::text,
            item.width, item.height, item.video_output_id, item.video_building_output_id
        FROM public.dataset_items AS item WHERE item.id = p_item AND item.file_type = 'video'
        FOR UPDATE OF item NOWAIT;
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_rebuild_items(
    p_items uuid[], p_dataset uuid, p_limit integer
)
RETURNS SETOF uuid
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT item.id FROM public.dataset_items AS item
    WHERE item.file_type = 'video'
      AND (p_items IS NULL OR item.id = ANY(p_items))
      AND (p_dataset IS NULL OR item.dataset_id = p_dataset)
    ORDER BY item.created_at
    LIMIT p_limit;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_slot(
    p_item uuid, p_kind text, p_spec jsonb, p_lock text
)
RETURNS TABLE (id uuid, dataset_item_id uuid, output_id uuid, status text)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_frame integer := (p_spec ->> 'frame_index')::integer;
    v_width integer := (p_spec ->> 'width')::integer;
    v_format text := p_spec ->> 'format';
    v_chunk integer := (p_spec ->> 'chunk_id')::integer;
BEGIN
    IF p_kind = 'frame' THEN
        IF p_lock = 'read' THEN
            RETURN QUERY SELECT slot.id, slot.dataset_item_id, slot.output_id, slot.status::text
            FROM public.video_frame_cache AS slot
            WHERE slot.dataset_item_id = p_item AND slot.frame_index = v_frame
              AND slot.width = v_width AND slot.format = v_format;
        ELSIF p_lock = 'share' THEN
            RETURN QUERY SELECT slot.id, slot.dataset_item_id, slot.output_id, slot.status::text
            FROM public.video_frame_cache AS slot
            WHERE slot.dataset_item_id = p_item AND slot.frame_index = v_frame
              AND slot.width = v_width AND slot.format = v_format
            FOR SHARE OF slot;
        ELSIF p_lock = 'update' THEN
            RETURN QUERY SELECT slot.id, slot.dataset_item_id, slot.output_id, slot.status::text
            FROM public.video_frame_cache AS slot
            WHERE slot.dataset_item_id = p_item AND slot.frame_index = v_frame
              AND slot.width = v_width AND slot.format = v_format
            FOR UPDATE OF slot;
        END IF;
    ELSIF p_kind = 'chunk' THEN
        IF p_lock = 'read' THEN
            RETURN QUERY SELECT slot.id, slot.dataset_item_id, slot.output_id, slot.status::text
            FROM public.video_chunks AS slot
            WHERE slot.dataset_item_id = p_item AND slot.chunk_id = v_chunk;
        ELSIF p_lock = 'share' THEN
            RETURN QUERY SELECT slot.id, slot.dataset_item_id, slot.output_id, slot.status::text
            FROM public.video_chunks AS slot
            WHERE slot.dataset_item_id = p_item AND slot.chunk_id = v_chunk
            FOR SHARE OF slot;
        ELSIF p_lock = 'update' THEN
            RETURN QUERY SELECT slot.id, slot.dataset_item_id, slot.output_id, slot.status::text
            FROM public.video_chunks AS slot
            WHERE slot.dataset_item_id = p_item AND slot.chunk_id = v_chunk
            FOR UPDATE OF slot;
        END IF;
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_create_slot(p_item uuid, p_kind text, p_spec jsonb)
RETURNS TABLE (id uuid, dataset_item_id uuid, output_id uuid, status text)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.dataset_items AS item
        WHERE item.id = p_item AND item.file_type = 'video') THEN
        RETURN;
    END IF;
    IF p_kind = 'frame' THEN
        RETURN QUERY INSERT INTO public.video_frame_cache AS slot (
            id, dataset_item_id, frame_index, width, format, status
        ) VALUES (pg_catalog.gen_random_uuid(), p_item, (p_spec ->> 'frame_index')::integer,
            (p_spec ->> 'width')::integer, p_spec ->> 'format', 'pending')
        RETURNING slot.id, slot.dataset_item_id, slot.output_id, slot.status::text;
    ELSIF p_kind = 'chunk' THEN
        RETURN QUERY INSERT INTO public.video_chunks AS slot (
            id, dataset_item_id, chunk_id, start_frame, end_frame,
            start_pts_ms, end_pts_ms, diagnostics, status
        ) VALUES (pg_catalog.gen_random_uuid(), p_item, (p_spec ->> 'chunk_id')::integer,
            (p_spec ->> 'start_frame')::integer, (p_spec ->> 'end_frame')::integer,
            (p_spec ->> 'start_pts_ms')::integer, (p_spec ->> 'end_pts_ms')::integer,
            '{}'::jsonb, 'pending')
        RETURNING slot.id, slot.dataset_item_id, slot.output_id, slot.status::text;
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_attempt(p_item uuid, p_kind text, p_output uuid, p_lock text)
RETURNS TABLE (id uuid, dataset_item_id uuid, kind text, spec jsonb, original_ref jsonb,
    auth_ref jsonb, metadata_output_id uuid, storage_ref jsonb, state text,
    lease_token uuid, lease_expires_at timestamptz, inventory jsonb, result jsonb,
    error_code text, created_at timestamptz, updated_at timestamptz, fixture_request_versions jsonb)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_output public.video_derivative_outputs%ROWTYPE;
BEGIN
    IF p_lock = 'read' THEN
        SELECT output.* INTO v_output FROM public.video_derivative_outputs AS output
        WHERE output.id = p_output AND output.dataset_item_id = p_item AND output.kind = p_kind;
    ELSIF p_lock = 'share' THEN
        SELECT output.* INTO v_output FROM public.video_derivative_outputs AS output
        WHERE output.id = p_output AND output.dataset_item_id = p_item AND output.kind = p_kind
        FOR SHARE OF output NOWAIT;
    ELSIF p_lock = 'update' THEN
        SELECT output.* INTO v_output FROM public.video_derivative_outputs AS output
        WHERE output.id = p_output AND output.dataset_item_id = p_item AND output.kind = p_kind
        FOR UPDATE OF output NOWAIT;
    END IF;
    IF v_output.id IS NOT NULL THEN
        RETURN QUERY SELECT v_output.id, v_output.dataset_item_id, v_output.kind::text,
            v_output.spec, v_output.original_ref, v_output.auth_ref, v_output.metadata_output_id,
            v_output.storage_ref, v_output.state::text, v_output.lease_token, v_output.lease_expires_at,
            v_output.inventory, v_output.result, v_output.error_code::text,
            v_output.created_at, v_output.updated_at, v_output.fixture_request_versions;
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_cache_output(p_output uuid, p_lock boolean)
RETURNS TABLE (id uuid, state text, storage_ref jsonb, inventory jsonb, fixture_request_versions jsonb)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock THEN
        RETURN QUERY SELECT output.id, output.state::text, output.storage_ref, output.inventory,
            output.fixture_request_versions
        FROM public.video_derivative_outputs AS output
        WHERE output.id = p_output AND output.state IN ('ready', 'retiring')
        FOR SHARE OF output NOWAIT;
    ELSE
        RETURN QUERY SELECT output.id, output.state::text, output.storage_ref, output.inventory,
            output.fixture_request_versions
        FROM public.video_derivative_outputs AS output
        WHERE output.id = p_output AND output.state IN ('ready', 'retiring');
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_dispatch_sources(p_outputs uuid[])
RETURNS TABLE (id uuid, dataset_item_id uuid, original_ref jsonb)
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT output.id, output.dataset_item_id, output.original_ref
    FROM public.video_derivative_outputs AS output WHERE output.id = ANY(p_outputs);
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_allocate_output(
    p_output uuid, p_item uuid, p_kind text, p_spec jsonb, p_original jsonb, p_auth jsonb,
    p_metadata uuid, p_target jsonb
)
RETURNS TABLE (storage_ref jsonb, created_at timestamptz, updated_at timestamptz,
    fixture_request_versions jsonb)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_kind IS NULL OR p_kind NOT IN ('metadata', 'frame', 'chunk')
       OR yoao_assets_v1._legacy_media_cache_target_current(p_target) IS DISTINCT FROM true
       OR NOT EXISTS (
            SELECT 1 FROM public.dataset_items AS item
            WHERE item.id = p_item AND item.file_type = 'video'
              AND (p_kind = 'metadata' OR item.video_output_id = p_metadata)
       ) OR (p_kind <> 'metadata' AND NOT EXISTS (
            SELECT 1 FROM public.video_derivative_outputs AS generation
            WHERE generation.id = p_metadata AND generation.dataset_item_id = p_item
              AND generation.kind = 'metadata' AND generation.state = 'ready'
              AND generation.original_ref = p_original
       )) THEN
        RETURN;
    END IF;
    INSERT INTO public.video_derivative_outputs (
        id, dataset_item_id, kind, spec, original_ref, auth_ref, metadata_output_id, storage_ref
    ) VALUES (p_output, p_item, p_kind, p_spec, p_original, p_auth, p_metadata, p_target);
    RETURN QUERY SELECT output.storage_ref, output.created_at, output.updated_at,
        output.fixture_request_versions
    FROM public.video_derivative_outputs AS output WHERE output.id = p_output;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_attach_output(p_output uuid, p_slot uuid, p_previous uuid)
RETURNS boolean
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_output public.video_derivative_outputs%ROWTYPE;
BEGIN
    SELECT output.* INTO v_output FROM public.video_derivative_outputs AS output
    WHERE output.id = p_output AND output.state = 'building' AND output.lease_token IS NULL;
    IF NOT FOUND OR (v_output.kind = 'metadata') IS DISTINCT FROM (p_slot IS NULL) THEN
        RETURN false;
    END IF;
    IF v_output.kind = 'metadata' THEN
        UPDATE public.dataset_items AS item
        SET video_building_output_id = p_output, updated_at = pg_catalog.now()
        WHERE item.id = v_output.dataset_item_id AND item.file_type = 'video'
          AND item.video_building_output_id IS NOT DISTINCT FROM p_previous;
    ELSIF NOT EXISTS (
        SELECT 1 FROM public.dataset_items AS item
        WHERE item.id = v_output.dataset_item_id AND item.file_type = 'video'
          AND item.video_output_id = v_output.metadata_output_id
    ) THEN
        RETURN false;
    ELSIF v_output.kind = 'frame' THEN
        UPDATE public.video_frame_cache AS slot
        SET output_id = p_output, status = 'pending', storage_key = NULL,
            byte_size = NULL, error = NULL, updated_at = pg_catalog.now()
        WHERE slot.id = p_slot AND slot.dataset_item_id = v_output.dataset_item_id
          AND slot.frame_index = (v_output.spec ->> 'frame_index')::integer
          AND slot.width = (v_output.spec ->> 'width')::integer
          AND slot.format = v_output.spec ->> 'format'
          AND slot.output_id IS NOT DISTINCT FROM p_previous;
    ELSE
        UPDATE public.video_chunks AS slot
        SET output_id = p_output, status = 'pending', storage_key = NULL,
            byte_size = NULL, error = NULL, generation_mode = NULL, diagnostics = '{}'::jsonb,
            start_frame = (v_output.spec ->> 'start_frame')::integer,
            end_frame = (v_output.spec ->> 'end_frame')::integer,
            start_pts_ms = (v_output.spec ->> 'start_pts_ms')::integer,
            end_pts_ms = (v_output.spec ->> 'end_pts_ms')::integer, updated_at = pg_catalog.now()
        WHERE slot.id = p_slot AND slot.dataset_item_id = v_output.dataset_item_id
          AND slot.chunk_id = (v_output.spec ->> 'chunk_id')::integer
          AND slot.output_id IS NOT DISTINCT FROM p_previous;
    END IF;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_pts(p_item uuid, p_frame integer)
RETURNS integer
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT frame.pts_ms FROM public.video_frame_indices AS frame
    WHERE frame.dataset_item_id = p_item AND frame.frame_index = p_frame;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_failed_frames(
    p_item uuid, p_width integer, p_format text, p_frames integer[]
)
RETURNS SETOF integer
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT frame.frame_index FROM public.video_frame_cache AS frame
    WHERE frame.dataset_item_id = p_item AND frame.width = p_width AND frame.format = p_format
      AND frame.status = 'failed' AND (p_frames IS NULL OR frame.frame_index = ANY(p_frames))
    ORDER BY frame.frame_index LIMIT 500;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_touch_slot(
    p_kind text, p_item uuid, p_slot uuid, p_output uuid, p_at timestamptz
)
RETURNS boolean
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_kind = 'frame' THEN
        UPDATE public.video_frame_cache AS slot
        SET last_accessed_at = p_at, updated_at = pg_catalog.now()
        WHERE slot.id = p_slot AND slot.dataset_item_id = p_item
          AND slot.output_id = p_output AND slot.status = 'ready';
    ELSIF p_kind = 'chunk' THEN
        UPDATE public.video_chunks AS slot
        SET last_accessed_at = p_at, updated_at = pg_catalog.now()
        WHERE slot.id = p_slot AND slot.dataset_item_id = p_item
          AND slot.output_id = p_output AND slot.status = 'ready';
    ELSE
        RETURN false;
    END IF;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_ready_bytes()
RETURNS TABLE (frame_bytes numeric, chunk_bytes numeric)
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT COALESCE((SELECT sum(slot.byte_size) FROM public.video_frame_cache AS slot
        WHERE slot.status = 'ready'), 0),
        COALESCE((SELECT sum(slot.byte_size) FROM public.video_chunks AS slot
        WHERE slot.status = 'ready'), 0);
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_locate_pending(p_output uuid)
RETURNS TABLE (id uuid, dataset_item_id uuid, kind text, spec jsonb, auth_ref jsonb, original_ref jsonb)
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT output.id, output.dataset_item_id, output.kind::text, output.spec, output.auth_ref, output.original_ref
    FROM public.video_derivative_outputs AS output
    WHERE output.id = p_output AND output.state = 'building' AND output.lease_token IS NULL;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._legacy_video_building_link(p_output public.video_derivative_outputs)
RETURNS boolean
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.dataset_items AS item
        WHERE item.id = (p_output).dataset_item_id AND item.file_type = 'video'
          AND CASE (p_output).kind
            WHEN 'metadata' THEN item.video_building_output_id = (p_output).id
            WHEN 'frame' THEN item.video_output_id = (p_output).metadata_output_id AND EXISTS (
                SELECT 1 FROM public.video_frame_cache AS slot
                WHERE slot.dataset_item_id = item.id AND slot.output_id = (p_output).id
                  AND slot.frame_index = ((p_output).spec ->> 'frame_index')::integer
                  AND slot.width = ((p_output).spec ->> 'width')::integer
                  AND slot.format = (p_output).spec ->> 'format'
            )
            WHEN 'chunk' THEN item.video_output_id = (p_output).metadata_output_id AND EXISTS (
                SELECT 1 FROM public.video_chunks AS slot
                WHERE slot.dataset_item_id = item.id AND slot.output_id = (p_output).id
                  AND slot.chunk_id = ((p_output).spec ->> 'chunk_id')::integer
            )
            ELSE false
          END
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_claim_attempt(p_output uuid, p_token uuid, p_ttl integer)
RETURNS TABLE (lease_expires_at timestamptz, updated_at timestamptz)
LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.video_derivative_outputs AS output
    SET lease_token = p_token,
        lease_expires_at = pg_catalog.clock_timestamp() + pg_catalog.make_interval(secs => p_ttl),
        updated_at = pg_catalog.now()
    WHERE output.id = p_output AND output.state = 'building' AND p_token IS NOT NULL
      AND p_ttl BETWEEN 1 AND 1800 AND output.lease_token IS NULL AND output.lease_expires_at IS NULL
      AND yoao_assets_v1._legacy_video_building_link(output)
    RETURNING output.lease_expires_at, output.updated_at;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_fail_attempt(p_output uuid, p_token uuid, p_error text)
RETURNS TABLE (id uuid, state text, lease_token uuid, error_code text, result jsonb)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_output public.video_derivative_outputs%ROWTYPE;
    v_slot uuid;
BEGIN
    IF p_error IS NULL OR p_error !~ '^[a-z][a-z0-9_]{0,127}$' THEN
        RETURN;
    END IF;
    SELECT output.* INTO v_output FROM public.video_derivative_outputs AS output
    WHERE output.id = p_output;
    IF NOT FOUND THEN RETURN; END IF;
    PERFORM 1 FROM public.dataset_items AS item WHERE item.id = v_output.dataset_item_id
    FOR UPDATE OF item NOWAIT;
    IF FOUND AND v_output.kind = 'frame' THEN
        SELECT slot.id INTO v_slot FROM public.video_frame_cache AS slot
        WHERE slot.dataset_item_id = v_output.dataset_item_id
          AND slot.frame_index = (v_output.spec ->> 'frame_index')::integer
          AND slot.width = (v_output.spec ->> 'width')::integer
          AND slot.format = v_output.spec ->> 'format'
        FOR SHARE OF slot NOWAIT;
    ELSIF FOUND AND v_output.kind = 'chunk' THEN
        SELECT slot.id INTO v_slot FROM public.video_chunks AS slot
        WHERE slot.dataset_item_id = v_output.dataset_item_id
          AND slot.chunk_id = (v_output.spec ->> 'chunk_id')::integer
        FOR SHARE OF slot NOWAIT;
    END IF;
    SELECT output.* INTO v_output FROM public.video_derivative_outputs AS output
    WHERE output.id = p_output FOR UPDATE OF output NOWAIT;
    IF NOT FOUND OR v_output.state <> 'building'
       OR v_output.lease_token IS DISTINCT FROM p_token THEN
        RETURN;
    END IF;
    IF v_output.kind = 'metadata' THEN
        UPDATE public.dataset_items AS item
        SET video_building_output_id = NULL,
            metadata = CASE WHEN item.video_output_id IS NULL THEN
                COALESCE(NULLIF(item.metadata, 'null'::jsonb), '{}'::jsonb) ||
                pg_catalog.jsonb_build_object('video',
                    COALESCE(NULLIF(item.metadata -> 'video', 'null'::jsonb), '{}'::jsonb) ||
                    pg_catalog.jsonb_build_object('probe_error', p_error))
                ELSE item.metadata END,
            updated_at = pg_catalog.now()
        WHERE item.id = v_output.dataset_item_id AND item.video_building_output_id = p_output;
    ELSIF v_output.kind = 'frame' THEN
        UPDATE public.video_frame_cache AS slot
        SET output_id = NULL, status = 'failed', error = p_error,
            storage_key = NULL, byte_size = NULL, updated_at = pg_catalog.now()
        WHERE slot.id = v_slot AND slot.output_id = p_output;
    ELSE
        UPDATE public.video_chunks AS slot
        SET output_id = NULL, status = 'failed', error = p_error, generation_mode = NULL,
            diagnostics = '{}'::jsonb, storage_key = NULL, byte_size = NULL, updated_at = pg_catalog.now()
        WHERE slot.id = v_slot AND slot.output_id = p_output;
    END IF;
    RETURN QUERY UPDATE public.video_derivative_outputs AS output
    SET state = 'retiring', error_code = p_error, updated_at = pg_catalog.now()
    WHERE output.id = p_output
    RETURNING output.id, output.state::text, output.lease_token, output.error_code::text, output.result;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_publish_attempt(
    p_output uuid, p_token uuid, p_result jsonb, p_inventory jsonb, p_accessed timestamptz,
    p_width integer, p_height integer, p_poster text
)
RETURNS TABLE (updated_at timestamptz, fixture_request_versions jsonb)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_output public.video_derivative_outputs%ROWTYPE;
    v_key text;
    v_size bigint;
    v_stride integer;
    v_last integer;
    v_video jsonb;
BEGIN
    SELECT output.* INTO v_output FROM public.video_derivative_outputs AS output
    WHERE output.id = p_output;
    IF NOT FOUND OR p_token IS NULL OR v_output.state <> 'building'
       OR v_output.lease_token IS DISTINCT FROM p_token
       OR v_output.lease_expires_at <= pg_catalog.clock_timestamp()
       OR p_result ->> 'kind' IS DISTINCT FROM v_output.kind
       OR NOT yoao_assets_v1._legacy_video_building_link(v_output) THEN
        RETURN;
    END IF;
    IF v_output.kind = 'metadata' THEN
        IF p_width IS NULL OR p_width <= 0 OR p_height IS NULL OR p_height <= 0
           OR (p_poster IS NOT NULL AND p_poster <> ''
               AND p_poster IS DISTINCT FROM (v_output.storage_ref ->> 'prefix') || 'poster.webp') THEN
            RETURN;
        END IF;
        v_stride := COALESCE((v_output.spec ->> 'timetable_stride')::integer, 1);
        v_last := (p_result #>> '{frames,-1,frame_index}')::integer;
        v_video := p_result -> 'video';
        DELETE FROM public.video_frame_indices AS anchor WHERE anchor.dataset_item_id = v_output.dataset_item_id;
        INSERT INTO public.video_frame_indices (
            id, dataset_item_id, frame_index, pts_ms, is_keyframe, pict_type, byte_offset
        ) SELECT pg_catalog.gen_random_uuid(), v_output.dataset_item_id, frame.frame_index,
            frame.pts_ms, frame.is_keyframe, frame.pict_type, frame.byte_offset
        FROM pg_catalog.jsonb_to_recordset(p_result -> 'frames') AS frame (
            frame_index integer, pts_ms integer, is_keyframe boolean, pict_type text, byte_offset bigint
        ) WHERE v_stride <= 1 OR frame.frame_index % v_stride = 0
            OR frame.frame_index = v_last OR frame.is_keyframe;
        UPDATE public.dataset_items AS item
        SET width = p_width, height = p_height, thumbnail_path = p_poster,
            metadata = COALESCE(NULLIF(item.metadata, 'null'::jsonb), '{}'::jsonb)
                || pg_catalog.jsonb_build_object('video', v_video),
            video_output_id = p_output, video_building_output_id = NULL, updated_at = pg_catalog.now()
        WHERE item.id = v_output.dataset_item_id AND item.video_building_output_id = p_output;
    ELSE
        v_key := (v_output.storage_ref ->> 'prefix') || CASE v_output.kind
            WHEN 'frame' THEN 'frame.' || (v_output.spec ->> 'format') ELSE 'chunk.mp4' END;
        v_size := (p_inventory -> 'objects' -> v_key ->> 'size_bytes')::bigint;
        IF v_size IS NULL OR v_size <= 0 OR p_result -> 'spec' IS DISTINCT FROM v_output.spec
           OR p_result ->> 'metadata_output_id' IS DISTINCT FROM v_output.metadata_output_id::text THEN
            RETURN;
        END IF;
        IF v_output.kind = 'frame' THEN
            UPDATE public.video_frame_cache AS slot
            SET storage_key = v_key, byte_size = v_size, status = 'ready', error = NULL,
                last_accessed_at = p_accessed, updated_at = pg_catalog.now()
            WHERE slot.dataset_item_id = v_output.dataset_item_id AND slot.output_id = p_output
              AND slot.frame_index = (v_output.spec ->> 'frame_index')::integer
              AND slot.width = (v_output.spec ->> 'width')::integer AND slot.format = v_output.spec ->> 'format';
        ELSE
            UPDATE public.video_chunks AS slot
            SET storage_key = v_key, byte_size = v_size, status = 'ready', error = NULL,
                generation_mode = p_result ->> 'generation_mode', diagnostics = p_result -> 'diagnostics',
                last_accessed_at = p_accessed, updated_at = pg_catalog.now()
            WHERE slot.dataset_item_id = v_output.dataset_item_id AND slot.output_id = p_output
              AND slot.chunk_id = (v_output.spec ->> 'chunk_id')::integer;
        END IF;
    END IF;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '40001', MESSAGE = 'video_derivative_changed';
    END IF;
    UPDATE public.video_derivative_outputs AS output
    SET state = 'ready', result = p_result, inventory = p_inventory, updated_at = pg_catalog.now()
    WHERE output.id = p_output AND output.state = 'building' AND output.lease_token = p_token
    RETURNING output.* INTO v_output;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '40001', MESSAGE = 'video_derivative_lease_lost';
    END IF;
    RETURN QUERY SELECT v_output.updated_at, v_output.fixture_request_versions;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._legacy_video_idle(p_output uuid)
RETURNS boolean
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT NOT EXISTS (
        SELECT 1 FROM public.storage_operations AS operation
        WHERE operation.purpose = 'video_derivative_' || pg_catalog.replace(p_output::text, '-', '')
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._legacy_video_unreferenced(p_output uuid)
RETURNS boolean
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT NOT EXISTS (
        SELECT 1 FROM public.dataset_items AS item
        WHERE item.video_output_id = p_output OR item.video_building_output_id = p_output
    ) AND NOT EXISTS (
        SELECT 1 FROM public.video_frame_cache AS slot WHERE slot.output_id = p_output
    ) AND NOT EXISTS (
        SELECT 1 FROM public.video_chunks AS slot WHERE slot.output_id = p_output
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._legacy_video_expired(
    p_output public.video_derivative_outputs, p_now timestamptz
)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN (p_output).lease_expires_at IS NOT NULL
        THEN (p_output).lease_expires_at <= p_now
        ELSE (p_output).lease_token IS NULL
            AND (p_output).updated_at < p_now - interval '1800 seconds'
        END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_expired_slots(p_kind text, p_cutoff timestamptz, p_limit integer)
RETURNS TABLE (dataset_item_id uuid, slot_id uuid, output_id uuid)
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 1000 THEN RETURN; END IF;
    IF p_kind = 'frame' THEN
        RETURN QUERY SELECT slot.dataset_item_id, slot.id, output.id
        FROM public.video_frame_cache AS slot
        JOIN public.video_derivative_outputs AS output ON output.id = slot.output_id
        WHERE slot.status = 'ready' AND slot.last_accessed_at < p_cutoff
          AND output.state = 'ready' AND output.kind = 'frame'
          AND output.dataset_item_id = slot.dataset_item_id
          AND yoao_assets_v1.fixture_media_ordinary_gc('video_output', output.id, 0)
          AND yoao_assets_v1._legacy_video_idle(output.id)
        ORDER BY slot.last_accessed_at, slot.id LIMIT p_limit;
    ELSIF p_kind = 'chunk' THEN
        RETURN QUERY SELECT slot.dataset_item_id, slot.id, output.id
        FROM public.video_chunks AS slot
        JOIN public.video_derivative_outputs AS output ON output.id = slot.output_id
        WHERE slot.status = 'ready' AND slot.last_accessed_at < p_cutoff
          AND output.state = 'ready' AND output.kind = 'chunk'
          AND output.dataset_item_id = slot.dataset_item_id
          AND yoao_assets_v1.fixture_media_ordinary_gc('video_output', output.id, 0)
          AND yoao_assets_v1._legacy_video_idle(output.id)
        ORDER BY slot.last_accessed_at, slot.id LIMIT p_limit;
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_expire_slot(
    p_kind text, p_item uuid, p_slot uuid, p_output uuid, p_cutoff timestamptz
)
RETURNS TABLE (id uuid, storage_ref jsonb)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_slot record;
    v_output public.video_derivative_outputs%ROWTYPE;
BEGIN
    IF p_kind IS NULL OR p_kind NOT IN ('frame', 'chunk') THEN RETURN; END IF;
    PERFORM 1 FROM public.dataset_items AS item WHERE item.id = p_item FOR UPDATE OF item NOWAIT;
    IF p_kind = 'frame' THEN
        SELECT slot.output_id, slot.status, slot.last_accessed_at INTO v_slot
        FROM public.video_frame_cache AS slot WHERE slot.id = p_slot AND slot.dataset_item_id = p_item
        FOR UPDATE OF slot NOWAIT;
    ELSE
        SELECT slot.output_id, slot.status, slot.last_accessed_at INTO v_slot
        FROM public.video_chunks AS slot WHERE slot.id = p_slot AND slot.dataset_item_id = p_item
        FOR UPDATE OF slot NOWAIT;
    END IF;
    SELECT output.* INTO v_output FROM public.video_derivative_outputs AS output
    WHERE output.id = p_output FOR UPDATE OF output NOWAIT;
    IF NOT FOUND OR v_output.state <> 'ready' OR v_output.kind <> p_kind
       OR v_output.dataset_item_id <> p_item OR v_slot.output_id IS DISTINCT FROM p_output
       OR v_slot.status IS DISTINCT FROM 'ready' OR v_slot.last_accessed_at IS NULL
       OR p_cutoff IS NULL OR v_slot.last_accessed_at >= p_cutoff
       OR NOT yoao_assets_v1.fixture_media_ordinary_gc('video_output', p_output, 0)
       OR NOT yoao_assets_v1._legacy_video_idle(p_output) THEN
        RETURN;
    END IF;
    IF p_kind = 'frame' THEN
        UPDATE public.video_frame_cache AS slot
        SET output_id = NULL, status = 'pending', storage_key = NULL,
            byte_size = NULL, error = NULL, updated_at = pg_catalog.now()
        WHERE slot.id = p_slot;
    ELSE
        UPDATE public.video_chunks AS slot
        SET output_id = NULL, status = 'pending', storage_key = NULL, byte_size = NULL,
            error = NULL, generation_mode = NULL, diagnostics = '{}'::jsonb, updated_at = pg_catalog.now()
        WHERE slot.id = p_slot;
    END IF;
    RETURN QUERY SELECT v_output.id, v_output.storage_ref;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_abandoned_attempts(p_now timestamptz, p_limit integer)
RETURNS SETOF uuid
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT output.id FROM public.video_derivative_outputs AS output
    WHERE output.state = 'building' AND yoao_assets_v1._legacy_video_expired(output, p_now)
      AND yoao_assets_v1._legacy_video_idle(output.id)
      AND yoao_assets_v1.fixture_media_ordinary_gc('video_output', output.id, 0)
    ORDER BY output.updated_at, output.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1000 THEN p_limit ELSE 0 END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._legacy_video_gc_lock(p_output uuid)
RETURNS public.video_derivative_outputs
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item uuid;
    v_output public.video_derivative_outputs%ROWTYPE;
BEGIN
    SELECT output.dataset_item_id INTO v_item
    FROM public.video_derivative_outputs AS output WHERE output.id = p_output;
    IF NOT FOUND OR NOT yoao_assets_v1.fixture_media_ordinary_gc('video_output', p_output, 0) THEN
        RETURN NULL;
    END IF;
    PERFORM 1 FROM public.dataset_items AS item WHERE item.id = v_item FOR UPDATE OF item NOWAIT;
    PERFORM 1 FROM public.video_frame_cache AS slot
    WHERE slot.dataset_item_id = v_item AND slot.output_id = p_output
    ORDER BY slot.id FOR UPDATE OF slot NOWAIT;
    PERFORM 1 FROM public.video_chunks AS slot
    WHERE slot.dataset_item_id = v_item AND slot.output_id = p_output
    ORDER BY slot.id FOR UPDATE OF slot NOWAIT;
    SELECT output.* INTO v_output FROM public.video_derivative_outputs AS output
    WHERE output.id = p_output FOR UPDATE OF output NOWAIT;
    RETURN v_output;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_expire_attempt(p_output uuid, p_now timestamptz)
RETURNS TABLE (id uuid, state text, lease_token uuid, error_code text, result jsonb, storage_ref jsonb)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_output public.video_derivative_outputs%ROWTYPE;
BEGIN
    v_output := yoao_assets_v1._legacy_video_gc_lock(p_output);
    IF v_output.id IS NULL OR v_output.state <> 'building'
       OR yoao_assets_v1._legacy_video_expired(v_output, p_now) IS DISTINCT FROM true
       OR NOT yoao_assets_v1._legacy_video_idle(p_output) THEN
        RETURN;
    END IF;
    IF v_output.kind = 'metadata' THEN
        UPDATE public.dataset_items AS item
        SET video_building_output_id = NULL, updated_at = pg_catalog.now()
        WHERE item.id = v_output.dataset_item_id AND item.video_building_output_id = p_output;
    ELSIF v_output.kind = 'frame' THEN
        UPDATE public.video_frame_cache AS slot
        SET output_id = NULL, status = 'pending', storage_key = NULL,
            byte_size = NULL, error = NULL, updated_at = pg_catalog.now()
        WHERE slot.dataset_item_id = v_output.dataset_item_id
          AND slot.output_id = p_output AND slot.status = 'pending';
    ELSE
        UPDATE public.video_chunks AS slot
        SET output_id = NULL, status = 'pending', storage_key = NULL, byte_size = NULL,
            error = NULL, generation_mode = NULL, diagnostics = '{}'::jsonb, updated_at = pg_catalog.now()
        WHERE slot.dataset_item_id = v_output.dataset_item_id
          AND slot.output_id = p_output AND slot.status = 'pending';
    END IF;
    RETURN QUERY UPDATE public.video_derivative_outputs AS output
    SET state = 'retiring', error_code = COALESCE(NULLIF(output.error_code, ''), 'lease_expired')
    WHERE output.id = p_output
    RETURNING output.id, output.state::text, output.lease_token, output.error_code::text,
        output.result, output.storage_ref;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_retirement_candidates(
    p_now timestamptz, p_cutoff timestamptz, p_limit integer
)
RETURNS SETOF uuid
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT output.id FROM public.video_derivative_outputs AS output
    WHERE output.state <> 'deleted' AND output.updated_at < p_cutoff
      AND (output.state <> 'building' OR yoao_assets_v1._legacy_video_expired(output, p_now))
      AND yoao_assets_v1._legacy_video_unreferenced(output.id)
      AND yoao_assets_v1._legacy_video_idle(output.id)
      AND yoao_assets_v1.fixture_media_ordinary_gc('video_output', output.id, 0)
    ORDER BY output.updated_at, output.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1000 THEN p_limit ELSE 0 END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_begin_sweep(
    p_output uuid, p_now timestamptz, p_cutoff timestamptz
)
RETURNS TABLE (id uuid, dataset_item_id uuid, kind text, spec jsonb, original_ref jsonb,
    auth_ref jsonb, metadata_output_id uuid, storage_ref jsonb, state text,
    lease_token uuid, lease_expires_at timestamptz, inventory jsonb, result jsonb,
    error_code text, created_at timestamptz, updated_at timestamptz, fixture_request_versions jsonb,
    published boolean)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_output public.video_derivative_outputs%ROWTYPE;
BEGIN
    v_output := yoao_assets_v1._legacy_video_gc_lock(p_output);
    IF v_output.id IS NULL OR v_output.state = 'deleted' OR p_cutoff IS NULL
       OR p_now IS NULL OR p_cutoff > p_now - interval '3600 seconds'
       OR v_output.updated_at >= p_cutoff
       OR (v_output.state = 'building' AND v_output.lease_expires_at > p_now)
       OR NOT yoao_assets_v1._legacy_video_unreferenced(p_output)
       OR NOT yoao_assets_v1._legacy_video_idle(p_output) THEN
        RETURN;
    END IF;
    RETURN QUERY UPDATE public.video_derivative_outputs AS output
    SET state = 'retiring', updated_at = CASE WHEN output.state = 'ready' THEN p_now ELSE output.updated_at END
    WHERE output.id = p_output
    RETURNING output.id, output.dataset_item_id, output.kind::text, output.spec, output.original_ref,
        output.auth_ref, output.metadata_output_id, output.storage_ref, output.state::text,
        output.lease_token, output.lease_expires_at, output.inventory, output.result,
        output.error_code::text, output.created_at, output.updated_at, output.fixture_request_versions,
        v_output.state = 'ready';
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_sweep_attempt(p_output uuid)
RETURNS TABLE (id uuid, dataset_item_id uuid, kind text, spec jsonb, original_ref jsonb,
    auth_ref jsonb, metadata_output_id uuid, storage_ref jsonb, state text,
    lease_token uuid, lease_expires_at timestamptz, inventory jsonb, result jsonb,
    error_code text, created_at timestamptz, updated_at timestamptz, fixture_request_versions jsonb)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_output public.video_derivative_outputs%ROWTYPE;
BEGIN
    v_output := yoao_assets_v1._legacy_video_gc_lock(p_output);
    IF v_output.state = 'retiring' AND yoao_assets_v1._legacy_video_unreferenced(p_output) THEN
        RETURN QUERY SELECT v_output.id, v_output.dataset_item_id, v_output.kind::text,
            v_output.spec, v_output.original_ref, v_output.auth_ref, v_output.metadata_output_id,
            v_output.storage_ref, v_output.state::text, v_output.lease_token, v_output.lease_expires_at,
            v_output.inventory, v_output.result, v_output.error_code::text,
            v_output.created_at, v_output.updated_at, v_output.fixture_request_versions;
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_sweep_permit(p_output uuid, p_revision integer)
RETURNS boolean
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT count(*) = 1 AND min(operation.revision) = p_revision
    FROM public.storage_operations AS operation
    WHERE operation.purpose = 'video_derivative_' || pg_catalog.replace(p_output::text, '-', '');
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.legacy_video_finish_sweep(p_output uuid, p_updated_at timestamptz)
RETURNS boolean
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.video_derivative_outputs AS output
    SET state = 'deleted', updated_at = pg_catalog.now()
    WHERE output.id = p_output AND output.state = 'retiring' AND output.updated_at = p_updated_at
      AND yoao_assets_v1._legacy_video_unreferenced(output.id)
      AND yoao_assets_v1._legacy_video_idle(output.id)
      AND yoao_assets_v1.fixture_media_ordinary_gc('video_output', output.id, 0);
    RETURN FOUND;
END;
$function$;
