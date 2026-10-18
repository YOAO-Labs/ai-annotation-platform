-- Assets worker-only durable video claims, publication, expiry and backfill.
CREATE FUNCTION aap_assets_v1._media_worker_outcome(
    p_job_id uuid,
    p_generation bigint,
    p_outcome text
)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_job_id IS NULL OR p_generation IS NULL OR p_generation < 1
       OR p_outcome NOT IN (
            'published', 'replayed', 'failed', 'lease_lost',
            'source_changed', 'dependency_changed'
       ) THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    RETURN aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object(
            'job_id', p_job_id,
            'generation', p_generation,
            'outcome', p_outcome
        ),
        NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_worker_outcome(uuid, bigint, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_object_has_only(p_value jsonb, p_keys text[])
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_typeof(p_value) = 'object'
       AND NOT EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_object_keys(p_value) AS key(name)
            WHERE key.name <> ALL (p_keys)
       )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_object_has_only(jsonb, text[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_artifact_valid(
    p_artifact jsonb,
    p_expected_key text
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT aap_assets_v1._media_object_has_only(
            p_artifact, ARRAY['key', 'byte_size', 'sha256']
       )
       AND pg_catalog.jsonb_typeof(p_artifact -> 'key') = 'string'
       AND p_artifact ->> 'key' = p_expected_key
       AND pg_catalog.jsonb_typeof(p_artifact -> 'byte_size') = 'number'
       AND p_artifact ->> 'byte_size' ~ '^(0|[1-9][0-9]*)$'
       AND (p_artifact ->> 'byte_size')::numeric <= 9223372036854775807
       AND pg_catalog.jsonb_typeof(p_artifact -> 'sha256') = 'string'
       AND p_artifact ->> 'sha256' ~ '^[0-9a-f]{64}$'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_artifact_valid(jsonb, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_queue_key(p_key text, p_reason text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_key IS NULL OR p_key = '' OR p_reason NOT IN (
        'source_changed', 'dependency_changed', 'cache_expired',
        'missing_cache', 'attempt_abandoned'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid media garbage scope';
    END IF;
    IF p_key LIKE 'videos/%' OR p_key LIKE 'media-assets/%' THEN
        INSERT INTO public.asset_media_garbage(prefix, keys, reason)
        VALUES (NULL, pg_catalog.jsonb_build_array(p_key), p_reason);
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_queue_key(text, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_requeue_timetable_dependents(
    p_dataset_item_id uuid,
    p_source_generation bigint,
    p_timetable_revision bigint
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_key text;
    v_job record;
    v_item public.dataset_items%ROWTYPE;
BEGIN
    SELECT item.* INTO v_item
    FROM public.dataset_items AS item
    WHERE item.id = p_dataset_item_id;
    FOR v_key IN
        SELECT DISTINCT candidate.object_key
        FROM (
            SELECT cache.storage_key AS object_key
            FROM public.video_frame_cache AS cache
            WHERE cache.dataset_item_id = p_dataset_item_id
              AND cache.storage_key IS NOT NULL
            UNION ALL
            SELECT chunk.storage_key
            FROM public.video_chunks AS chunk
            WHERE chunk.dataset_item_id = p_dataset_item_id
              AND chunk.storage_key IS NOT NULL
        ) AS candidate
        WHERE (candidate.object_key LIKE 'videos/%'
               OR candidate.object_key LIKE 'media-assets/%')
          AND candidate.object_key IS DISTINCT FROM
              v_item.metadata -> 'video' ->> 'poster_frame_path'
          AND candidate.object_key IS DISTINCT FROM v_item.thumbnail_path
    LOOP
        PERFORM aap_assets_v1._media_queue_key(v_key, 'dependency_changed');
    END LOOP;
    FOR v_job IN
        SELECT job.job_id, job.generation, job.lease_token
        FROM public.asset_media_jobs AS job
        WHERE job.dataset_item_id = p_dataset_item_id
          AND job.kind IN ('frame', 'chunk')
          AND job.status IN ('pending', 'running', 'ready', 'failed')
          AND job.timetable_revision IS DISTINCT FROM p_timetable_revision
    LOOP
        IF v_job.generation = 9223372036854775807 THEN
            RAISE EXCEPTION USING ERRCODE = '22003', MESSAGE = 'media job generation exhausted';
        END IF;
        IF v_job.lease_token IS NOT NULL THEN
            PERFORM aap_assets_v1._media_queue_prefix(
                'media-assets/' || v_job.job_id::text || '/g'
                    || v_job.generation::text || '/a' || v_job.lease_token::text || '/',
                'attempt_abandoned'
            );
        END IF;
    END LOOP;
    UPDATE public.asset_media_jobs AS job
    SET generation = job.generation + 1,
        status = 'pending',
        source_generation = p_source_generation,
        timetable_revision = p_timetable_revision,
        progress = '{}'::jsonb,
        error_code = NULL,
        error_detail = NULL,
        attempt_count = 0,
        lease_token = NULL,
        lease_expires_at = NULL,
        last_lease_token = NULL,
        last_outcome = NULL,
        accepted_at = pg_catalog.clock_timestamp(),
        updated_at = pg_catalog.clock_timestamp(),
        completed_at = NULL
    WHERE job.dataset_item_id = p_dataset_item_id
      AND job.kind IN ('frame', 'chunk')
      AND job.status IN ('pending', 'running', 'ready', 'failed')
      AND job.timetable_revision IS DISTINCT FROM p_timetable_revision
      AND NOT (
          job.kind = 'frame'
          AND job.slot_key = 'frame:0:512:webp'
          AND job.status = 'ready'
          AND EXISTS (
              SELECT 1 FROM public.video_frame_cache AS cache
              WHERE cache.dataset_item_id = p_dataset_item_id
                AND cache.frame_index = 0 AND cache.width = 512 AND cache.format = 'webp'
                AND cache.status = 'ready'
                AND cache.source_generation = p_source_generation
                AND cache.storage_key IS NOT NULL
                AND (cache.storage_key = v_item.thumbnail_path
                     OR cache.storage_key = v_item.metadata -> 'video' ->> 'poster_frame_path')
          )
      );
    UPDATE public.asset_media_jobs AS job
    SET timetable_revision = p_timetable_revision,
        updated_at = pg_catalog.clock_timestamp()
    WHERE job.dataset_item_id = p_dataset_item_id
      AND job.kind = 'frame'
      AND job.slot_key = 'frame:0:512:webp'
      AND job.status = 'ready'
      AND job.timetable_revision IS DISTINCT FROM p_timetable_revision
      AND EXISTS (
          SELECT 1 FROM public.video_frame_cache AS cache
          WHERE cache.dataset_item_id = p_dataset_item_id
            AND cache.frame_index = 0 AND cache.width = 512 AND cache.format = 'webp'
            AND cache.status = 'ready'
            AND cache.source_generation = p_source_generation
            AND cache.storage_key IS NOT NULL
            AND (cache.storage_key = v_item.thumbnail_path
                 OR cache.storage_key = v_item.metadata -> 'video' ->> 'poster_frame_path')
      );
    UPDATE public.video_frame_cache AS cache
    SET source_generation = p_source_generation,
        timetable_revision = p_timetable_revision,
        updated_at = pg_catalog.clock_timestamp()
    WHERE cache.dataset_item_id = p_dataset_item_id
      AND cache.frame_index = 0 AND cache.width = 512 AND cache.format = 'webp'
      AND cache.status = 'ready'
      AND cache.source_generation = p_source_generation
      AND cache.storage_key IS NOT NULL
      AND (cache.storage_key = v_item.thumbnail_path
           OR cache.storage_key = v_item.metadata -> 'video' ->> 'poster_frame_path');
    UPDATE public.video_frame_cache AS cache
    SET status = 'pending', storage_key = NULL, byte_size = NULL, error = NULL,
        source_generation = p_source_generation,
        timetable_revision = p_timetable_revision,
        last_accessed_at = NULL,
        updated_at = pg_catalog.clock_timestamp()
    WHERE cache.dataset_item_id = p_dataset_item_id
      AND NOT (
          cache.frame_index = 0 AND cache.width = 512 AND cache.format = 'webp'
          AND cache.status = 'ready'
          AND cache.source_generation = p_source_generation
          AND cache.storage_key IS NOT NULL
          AND (cache.storage_key = v_item.thumbnail_path
               OR cache.storage_key = v_item.metadata -> 'video' ->> 'poster_frame_path')
      );
    UPDATE public.video_chunks AS chunk
    SET status = 'pending', storage_key = NULL, byte_size = NULL, error = NULL,
        generation_mode = NULL, diagnostics = '{}'::jsonb,
        source_generation = p_source_generation,
        timetable_revision = p_timetable_revision,
        last_accessed_at = NULL,
        updated_at = pg_catalog.clock_timestamp()
    WHERE chunk.dataset_item_id = p_dataset_item_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_requeue_timetable_dependents(uuid, bigint, bigint) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.claim_video_media(
    p_worker_id uuid,
    p_job_id uuid,
    p_lease_seconds integer,
    p_kinds text[]
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_candidate record;
    v_item public.dataset_items%ROWTYPE;
    v_job public.asset_media_jobs%ROWTYPE;
    v_kind text;
    v_busy boolean := false;
    v_frame_count integer;
    v_start bigint;
    v_end bigint;
    v_target integer;
    v_size integer;
    v_token uuid;
    v_expiry timestamptz;
    v_anchors jsonb;
    v_is_keyframe boolean;
    v_timing jsonb;
    v_prefix text;
    v_claim jsonb;
BEGIN
    IF p_worker_id IS NULL OR p_lease_seconds NOT BETWEEN 30 AND 3600
       OR p_kinds IS NULL OR pg_catalog.cardinality(p_kinds) NOT BETWEEN 1 AND 5
       OR EXISTS (
            SELECT 1 FROM pg_catalog.unnest(p_kinds) AS kind(value)
            WHERE kind.value IS NULL OR kind.value NOT IN (
                'video_metadata', 'video_timetable', 'frame', 'chunk', 'video_backfill'
            )
       )
       OR pg_catalog.cardinality(p_kinds) <> (
            SELECT pg_catalog.count(DISTINCT kind.value)
            FROM pg_catalog.unnest(p_kinds) AS kind(value)
       ) THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    FOR v_candidate IN
        SELECT job.job_id, job.dataset_item_id
        FROM public.asset_media_jobs AS job
        WHERE job.kind = ANY (p_kinds)
          AND (p_job_id IS NULL OR job.job_id = p_job_id)
          AND (
              job.status = 'pending'
              OR (job.status = 'running' AND job.lease_expires_at <= pg_catalog.clock_timestamp())
          )
        ORDER BY job.accepted_at, job.job_id
        LIMIT 100
    LOOP
        v_item := NULL;
        BEGIN
            IF v_candidate.dataset_item_id IS NOT NULL THEN
                SELECT item.* INTO v_item
                FROM public.dataset_items AS item
                WHERE item.id = v_candidate.dataset_item_id
                FOR UPDATE NOWAIT;
            END IF;
            SELECT job.* INTO v_job
            FROM public.asset_media_jobs AS job
            WHERE job.job_id = v_candidate.job_id
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            v_busy := true;
            CONTINUE;
        END;
        IF NOT FOUND OR v_job.status NOT IN ('pending', 'running')
           OR (v_job.status = 'running'
               AND v_job.lease_expires_at > pg_catalog.clock_timestamp()) THEN
            CONTINUE;
        END IF;
        IF v_job.dataset_item_id IS NOT NULL
           AND (v_item.id IS NULL OR v_item.file_type <> 'video'
                OR v_item.media_source_drifted
                OR v_item.media_source_generation <> v_job.source_generation) THEN
            IF v_job.lease_token IS NOT NULL THEN
                PERFORM aap_assets_v1._media_queue_prefix(
                    'media-assets/' || v_job.job_id::text || '/g'
                        || v_job.generation::text || '/a' || v_job.lease_token::text || '/',
                    'attempt_abandoned'
                );
            END IF;
            UPDATE public.asset_media_jobs AS job
            SET status = 'canceled', lease_token = NULL, lease_expires_at = NULL,
                last_lease_token = NULL, last_outcome = NULL,
                completed_at = pg_catalog.clock_timestamp(),
                updated_at = pg_catalog.clock_timestamp()
            WHERE job.job_id = v_job.job_id;
            CONTINUE;
        END IF;
        IF v_job.kind IN ('frame', 'chunk')
           AND v_job.timetable_revision IS DISTINCT FROM v_item.media_timetable_revision THEN
            IF v_job.lease_token IS NOT NULL THEN
                PERFORM aap_assets_v1._media_queue_prefix(
                    'media-assets/' || v_job.job_id::text || '/g'
                        || v_job.generation::text || '/a' || v_job.lease_token::text || '/',
                    'attempt_abandoned'
                );
            END IF;
            IF v_job.generation = 9223372036854775807 THEN
                RAISE EXCEPTION USING ERRCODE = '22003', MESSAGE = 'media job generation exhausted';
            END IF;
            UPDATE public.asset_media_jobs AS job
            SET generation = v_job.generation + 1,
                timetable_revision = v_item.media_timetable_revision,
                status = 'pending', progress = '{}'::jsonb,
                error_code = NULL, error_detail = NULL, attempt_count = 0,
                lease_token = NULL, lease_expires_at = NULL,
                last_lease_token = NULL, last_outcome = NULL,
                accepted_at = pg_catalog.clock_timestamp(),
                updated_at = pg_catalog.clock_timestamp(), completed_at = NULL
            WHERE job.job_id = v_job.job_id
            RETURNING job.* INTO v_job;
        END IF;
        v_timing := pg_catalog.jsonb_build_object(
            'start_frame', NULL, 'end_frame', NULL, 'anchors', '[]'::jsonb,
            'is_keyframe', NULL
        );
        IF v_job.kind IN ('frame', 'chunk') THEN
            BEGIN
                v_frame_count := NULLIF(v_item.metadata -> 'video' ->> 'frame_count', '')::integer;
            EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
                v_frame_count := NULL;
            END;
            IF v_frame_count IS NULL OR v_frame_count <= 0 THEN
                CONTINUE;
            END IF;
            IF v_job.kind = 'frame' THEN
                v_target := (v_job.spec ->> 'frame_index')::integer;
                v_start := v_target;
                v_end := v_target;
            ELSE
                v_size := (v_job.spec ->> 'chunk_size_frames')::integer;
                v_start := (v_job.spec ->> 'chunk_id')::bigint * v_size;
                IF v_start >= v_frame_count THEN
                    UPDATE public.asset_media_jobs AS job
                    SET status = 'failed', error_code = 'invalid_frame_range',
                        error_detail = NULL,
                        last_lease_token = NULL, last_outcome = NULL,
                        completed_at = pg_catalog.clock_timestamp(),
                        updated_at = pg_catalog.clock_timestamp()
                    WHERE job.job_id = v_job.job_id;
                    CONTINUE;
                END IF;
                v_end := LEAST(v_frame_count - 1, v_start + v_size - 1);
                v_target := v_start::integer;
            END IF;
            IF v_start >= v_frame_count THEN
                UPDATE public.asset_media_jobs AS job
                SET status = 'failed', error_code = 'invalid_frame_range',
                    error_detail = NULL,
                    last_lease_token = NULL, last_outcome = NULL,
                    completed_at = pg_catalog.clock_timestamp(),
                    updated_at = pg_catalog.clock_timestamp()
                WHERE job.job_id = v_job.job_id;
                CONTINUE;
            END IF;
            IF v_job.kind = 'frame' THEN
                SELECT COALESCE(
                    pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                        'frame_index', frame.frame_index,
                        'pts_ms', frame.pts_ms,
                        'is_keyframe', frame.is_keyframe,
                        'pict_type', frame.pict_type,
                        'byte_offset', frame.byte_offset
                    ) ORDER BY pg_catalog.abs(frame.frame_index - v_target), frame.frame_index),
                    '[]'::jsonb
                )
                INTO v_anchors
                FROM (
                    SELECT frame.*
                    FROM public.video_frame_indices AS frame
                    WHERE frame.dataset_item_id = v_item.id
                    ORDER BY pg_catalog.abs(frame.frame_index - v_target), frame.frame_index
                    LIMIT 4
                ) AS frame;
            ELSE
                SELECT COALESCE(
                    pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                        'frame_index', frame.frame_index,
                        'pts_ms', frame.pts_ms,
                        'is_keyframe', frame.is_keyframe,
                        'pict_type', frame.pict_type,
                        'byte_offset', frame.byte_offset
                    ) ORDER BY frame.frame_index),
                    '[]'::jsonb
                )
                INTO v_anchors
                FROM (
                    SELECT frame.*
                    FROM public.video_frame_indices AS frame
                    WHERE frame.dataset_item_id = v_item.id
                    ORDER BY LEAST(
                        pg_catalog.abs(frame.frame_index - v_start::integer),
                        pg_catalog.abs(frame.frame_index - v_end::integer)
                    ), frame.frame_index
                    LIMIT 4
                ) AS frame;
            END IF;
            SELECT frame.is_keyframe INTO v_is_keyframe
            FROM public.video_frame_indices AS frame
            WHERE frame.dataset_item_id = v_item.id
              AND frame.frame_index = v_start::integer;
            v_timing := pg_catalog.jsonb_build_object(
                'start_frame', v_start,
                'end_frame', v_end,
                'anchors', COALESCE(v_anchors, '[]'::jsonb),
                'is_keyframe', v_is_keyframe
            );
        END IF;
        IF v_job.lease_token IS NOT NULL THEN
            PERFORM aap_assets_v1._media_queue_prefix(
                'media-assets/' || v_job.job_id::text || '/g'
                    || v_job.generation::text || '/a' || v_job.lease_token::text || '/',
                'attempt_abandoned'
            );
        END IF;
        DELETE FROM public.asset_media_timetable_staging AS staging
        WHERE staging.job_id = v_job.job_id
          AND staging.generation = v_job.generation;
        v_token := pg_catalog.gen_random_uuid();
        v_expiry := pg_catalog.clock_timestamp()
            + pg_catalog.make_interval(secs => p_lease_seconds);
        UPDATE public.asset_media_jobs AS job
        SET status = 'running',
            lease_token = v_token,
            lease_expires_at = v_expiry,
            progress = job.progress - ARRAY[
                'last_advance_lease_token', 'last_advance_batch_size', 'last_advance_result'
            ],
            last_lease_token = NULL,
            last_outcome = NULL,
            attempt_count = job.attempt_count + 1,
            updated_at = pg_catalog.clock_timestamp()
        WHERE job.job_id = v_job.job_id
        RETURNING job.* INTO v_job;
        v_prefix := CASE WHEN v_job.kind = 'video_backfill' THEN NULL ELSE
            'media-assets/' || v_job.job_id::text || '/g'
                || v_job.generation::text || '/a' || v_token::text || '/'
        END;
        v_claim := pg_catalog.jsonb_build_object(
            'job_id', v_job.job_id,
            'generation', v_job.generation,
            'lease_token', v_token,
            'lease_expires_at', v_expiry,
            'source', CASE WHEN v_job.dataset_item_id IS NULL THEN NULL
                ELSE aap_assets_v1._media_source_json(v_item) END,
            'spec', v_job.spec,
            'output_prefix', v_prefix,
            'timetable_revision', COALESCE(v_item.media_timetable_revision, 0),
            'timing', v_timing
        );
        RETURN aap_assets_v1._media_reply(
            pg_catalog.jsonb_build_object('claim', v_claim), NULL
        );
    END LOOP;
    IF v_busy THEN
        RETURN aap_assets_v1._media_reply(NULL, 'operation_busy');
    END IF;
    RETURN aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object('claim', NULL), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.claim_video_media(uuid, uuid, integer, text[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.heartbeat_video_media(
    p_job_id uuid,
    p_generation bigint,
    p_lease_token uuid,
    p_lease_seconds integer
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_job public.asset_media_jobs%ROWTYPE;
    v_expiry timestamptz;
BEGIN
    IF p_job_id IS NULL OR p_generation IS NULL OR p_generation < 1
       OR p_lease_token IS NULL OR p_lease_seconds NOT BETWEEN 30 AND 3600 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT job.* INTO v_job
        FROM public.asset_media_jobs AS job
        WHERE job.job_id = p_job_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._media_reply(NULL, 'operation_busy');
    END;
    IF NOT FOUND OR v_job.status <> 'running'
       OR v_job.generation <> p_generation
       OR v_job.lease_token IS DISTINCT FROM p_lease_token
       OR v_job.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._media_reply(NULL, 'lease_lost');
    END IF;
    v_expiry := pg_catalog.clock_timestamp()
        + pg_catalog.make_interval(secs => p_lease_seconds);
    UPDATE public.asset_media_jobs AS job
    SET lease_expires_at = v_expiry,
        updated_at = pg_catalog.clock_timestamp()
    WHERE job.job_id = p_job_id;
    RETURN aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object(
            'job_id', p_job_id,
            'generation', p_generation,
            'lease_token', p_lease_token,
            'active', true,
            'lease_expires_at', v_expiry
        ),
        NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.heartbeat_video_media(uuid, bigint, uuid, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.stage_video_timetable(
    p_job_id uuid,
    p_generation bigint,
    p_lease_token uuid,
    p_rows jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_job public.asset_media_jobs%ROWTYPE;
    v_count bigint;
    v_rows_count integer;
BEGIN
    IF p_job_id IS NULL OR p_generation IS NULL OR p_generation < 1
       OR p_lease_token IS NULL
       OR pg_catalog.jsonb_typeof(p_rows) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(p_rows) NOT BETWEEN 1 AND 500
       OR pg_catalog.octet_length(pg_catalog.convert_to(p_rows::text, 'UTF8')) > 8388608 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(p_rows) AS entry(value)
        WHERE NOT aap_assets_v1._media_object_has_only(
            entry.value,
            ARRAY['frame_index', 'pts_ms', 'is_keyframe', 'pict_type', 'byte_offset']
        )
           OR pg_catalog.jsonb_typeof(entry.value -> 'frame_index') <> 'number'
           OR entry.value ->> 'frame_index' !~ '^(0|[1-9][0-9]*)$'
           OR (entry.value ->> 'frame_index')::numeric > 2147483647
           OR pg_catalog.jsonb_typeof(entry.value -> 'pts_ms') <> 'number'
           OR entry.value ->> 'pts_ms' !~ '^-?(0|[1-9][0-9]*)$'
           OR (entry.value ->> 'pts_ms')::numeric NOT BETWEEN -2147483648 AND 2147483647
           OR pg_catalog.jsonb_typeof(entry.value -> 'is_keyframe') <> 'boolean'
           OR (entry.value ? 'pict_type' AND
               entry.value -> 'pict_type' <> 'null'::jsonb AND (
                   pg_catalog.jsonb_typeof(entry.value -> 'pict_type') <> 'string'
                   OR pg_catalog.char_length(entry.value ->> 'pict_type') > 8
               ))
           OR (entry.value ? 'byte_offset' AND
               entry.value -> 'byte_offset' <> 'null'::jsonb AND (
                   pg_catalog.jsonb_typeof(entry.value -> 'byte_offset') <> 'number'
                   OR entry.value ->> 'byte_offset' !~ '^(0|[1-9][0-9]*)$'
                   OR (entry.value ->> 'byte_offset')::numeric > 9223372036854775807
               ))
    ) THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    SELECT pg_catalog.count(*) INTO v_rows_count
    FROM pg_catalog.jsonb_array_elements(p_rows) AS entry(value);
    IF (
        SELECT pg_catalog.count(DISTINCT entry.value ->> 'frame_index')
        FROM pg_catalog.jsonb_array_elements(p_rows) AS entry(value)
    ) <> v_rows_count THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT job.* INTO v_job
        FROM public.asset_media_jobs AS job
        WHERE job.job_id = p_job_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._media_reply(NULL, 'operation_busy');
    END;
    IF NOT FOUND OR v_job.status <> 'running'
       OR v_job.generation <> p_generation
       OR v_job.lease_token IS DISTINCT FROM p_lease_token
       OR v_job.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._media_reply(NULL, 'lease_lost');
    END IF;
    IF v_job.kind NOT IN ('video_timetable', 'video_metadata')
       OR (v_job.kind = 'video_metadata'
           AND COALESCE(v_job.spec -> 'probe_timetable' = 'true'::jsonb, false) IS NOT TRUE) THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(p_rows) AS entry(value)
        JOIN public.asset_media_timetable_staging AS staged
          ON staged.job_id = p_job_id
         AND staged.generation = p_generation
         AND staged.frame_index = (entry.value ->> 'frame_index')::integer
        WHERE staged.lease_token IS DISTINCT FROM p_lease_token
           OR staged.pts_ms IS DISTINCT FROM (entry.value ->> 'pts_ms')::bigint
           OR staged.is_keyframe IS DISTINCT FROM (entry.value ->> 'is_keyframe')::boolean
           OR staged.pict_type IS DISTINCT FROM NULLIF(entry.value ->> 'pict_type', '')
           OR staged.byte_offset IS DISTINCT FROM NULLIF(entry.value ->> 'byte_offset', '')::bigint
    ) THEN
        RETURN aap_assets_v1._media_reply(NULL, 'operation_conflict');
    END IF;
    INSERT INTO public.asset_media_timetable_staging (
        job_id, generation, frame_index, lease_token, pts_ms,
        is_keyframe, pict_type, byte_offset
    )
    SELECT
        p_job_id,
        p_generation,
        (entry.value ->> 'frame_index')::integer,
        p_lease_token,
        (entry.value ->> 'pts_ms')::bigint,
        (entry.value ->> 'is_keyframe')::boolean,
        NULLIF(entry.value ->> 'pict_type', ''),
        NULLIF(entry.value ->> 'byte_offset', '')::bigint
    FROM pg_catalog.jsonb_array_elements(p_rows) AS entry(value)
    ON CONFLICT (job_id, generation, frame_index) DO NOTHING;
    SELECT pg_catalog.count(*) INTO v_count
    FROM public.asset_media_timetable_staging AS staged
    WHERE staged.job_id = p_job_id
      AND staged.generation = p_generation
      AND staged.lease_token = p_lease_token;
    UPDATE public.asset_media_jobs AS job
    SET progress = pg_catalog.jsonb_build_object('staged_count', v_count),
        updated_at = pg_catalog.clock_timestamp()
    WHERE job.job_id = p_job_id;
    RETURN aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object(
            'job_id', p_job_id,
            'generation', p_generation,
            'lease_token', p_lease_token,
            'staged_count', v_count
        ),
        NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.stage_video_timetable(uuid, bigint, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_video_media(
    p_job_id uuid,
    p_generation bigint,
    p_lease_token uuid,
    p_result jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_candidate public.asset_media_jobs%ROWTYPE;
    v_job public.asset_media_jobs%ROWTYPE;
    v_item public.dataset_items%ROWTYPE;
    v_result_kind text;
    v_prefix text;
    v_expected_key text;
    v_old_key text;
    v_poster_old_key text;
    v_video jsonb;
    v_metadata jsonb;
    v_old_poster_path text;
    v_old_playback_path text;
    v_new_thumbnail text;
    v_new_blurhash text;
    v_certified boolean;
    v_full_success boolean;
    v_source_reply jsonb;
    v_timetable_total bigint;
    v_timetable_count bigint;
    v_expected_count bigint;
    v_grid_count bigint;
    v_timetable_error_only boolean := false;
    v_stride integer := 1;
    v_next_timetable_revision bigint;
    v_replay text;
    v_width integer;
    v_height integer;
    v_chunk_id integer;
    v_chunk_start integer;
    v_chunk_end integer;
    v_chunk_size integer;
    v_source_generation bigint;
    v_timetable_revision bigint;
BEGIN
    IF p_job_id IS NULL OR p_generation IS NULL OR p_generation < 1
       OR p_lease_token IS NULL OR pg_catalog.jsonb_typeof(p_result) IS DISTINCT FROM 'object'
       OR pg_catalog.octet_length(pg_catalog.convert_to(p_result::text, 'UTF8')) > 8388608
       OR pg_catalog.jsonb_typeof(p_result -> 'kind') IS DISTINCT FROM 'string'
       OR NOT aap_assets_v1._media_object_identity_valid(p_result -> 'source_identity')
       OR pg_catalog.jsonb_typeof(p_result -> 'source_sha256') IS DISTINCT FROM 'string'
       OR p_result ->> 'source_sha256' !~ '^[0-9a-f]{64}$' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    v_result_kind := p_result ->> 'kind';
    IF v_result_kind IN ('video_metadata', 'video_timetable') THEN
        IF NOT aap_assets_v1._media_object_has_only(p_result, ARRAY[
            'kind', 'source_identity', 'source_sha256', 'metadata_patch',
            'clear_errors', 'width', 'height', 'timetable_total_frames',
            'timetable_staged_count', 'poster', 'playback'
        ])
           OR pg_catalog.jsonb_typeof(p_result -> 'metadata_patch') IS DISTINCT FROM 'object'
           OR (p_result ? 'clear_errors' AND
               pg_catalog.jsonb_typeof(p_result -> 'clear_errors') <> 'array')
           OR (p_result ? 'clear_errors' AND
               CASE WHEN pg_catalog.jsonb_typeof(p_result -> 'clear_errors') = 'array'
                    THEN pg_catalog.jsonb_array_length(p_result -> 'clear_errors') > 4
                    ELSE false END)
           OR (p_result ? 'width' AND p_result -> 'width' <> 'null'::jsonb AND (
               pg_catalog.jsonb_typeof(p_result -> 'width') <> 'number'
               OR p_result ->> 'width' !~ '^(0|[1-9][0-9]*)$'
               OR (p_result ->> 'width')::numeric > 2147483647
           ))
           OR (p_result ? 'height' AND p_result -> 'height' <> 'null'::jsonb AND (
               pg_catalog.jsonb_typeof(p_result -> 'height') <> 'number'
               OR p_result ->> 'height' !~ '^(0|[1-9][0-9]*)$'
               OR (p_result ->> 'height')::numeric > 2147483647
           ))
           OR (p_result ? 'timetable_total_frames' AND
               p_result -> 'timetable_total_frames' <> 'null'::jsonb AND (
                   pg_catalog.jsonb_typeof(p_result -> 'timetable_total_frames') <> 'number'
                   OR p_result ->> 'timetable_total_frames' !~ '^(0|[1-9][0-9]*)$'
                   OR (p_result ->> 'timetable_total_frames')::numeric > 2147483647
               ))
           OR (p_result ? 'timetable_staged_count' AND
               p_result -> 'timetable_staged_count' <> 'null'::jsonb AND (
                   pg_catalog.jsonb_typeof(p_result -> 'timetable_staged_count') <> 'number'
                   OR p_result ->> 'timetable_staged_count' !~ '^(0|[1-9][0-9]*)$'
                   OR (p_result ->> 'timetable_staged_count')::numeric > 2147483647
               ))
           OR ((p_result -> 'timetable_total_frames' = 'null'::jsonb
                OR NOT (p_result ? 'timetable_total_frames'))
               <> (p_result -> 'timetable_staged_count' = 'null'::jsonb
                   OR NOT (p_result ? 'timetable_staged_count'))) THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(
                COALESCE(p_result -> 'clear_errors', '[]'::jsonb)
            ) AS error(value)
            WHERE pg_catalog.jsonb_typeof(error.value) <> 'string'
               OR error.value #>> '{}' NOT IN (
                    'probe_error', 'playback_error', 'poster_error', 'frame_timetable_error'
               )
        ) OR (
            SELECT pg_catalog.count(DISTINCT error.value #>> '{}')
            FROM pg_catalog.jsonb_array_elements(
                COALESCE(p_result -> 'clear_errors', '[]'::jsonb)
            ) AS error(value)
        ) <> COALESCE(pg_catalog.jsonb_array_length(p_result -> 'clear_errors'), 0) THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END IF;
        IF (p_result ? 'poster' AND p_result -> 'poster' <> 'null'::jsonb
            AND NOT aap_assets_v1._media_artifact_valid(
                p_result -> 'poster', p_result #>> '{poster,key}'
            ))
           OR (p_result ? 'playback' AND p_result -> 'playback' <> 'null'::jsonb
            AND NOT aap_assets_v1._media_artifact_valid(
                p_result -> 'playback', p_result #>> '{playback,key}'
            )) THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END IF;
    ELSIF v_result_kind = 'frame' THEN
        IF NOT aap_assets_v1._media_object_has_only(p_result, ARRAY[
            'kind', 'source_identity', 'source_sha256', 'artifact'
        ]) OR pg_catalog.jsonb_typeof(p_result -> 'artifact') IS DISTINCT FROM 'object' THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END IF;
    ELSIF v_result_kind = 'chunk' THEN
        IF NOT aap_assets_v1._media_object_has_only(p_result, ARRAY[
            'kind', 'source_identity', 'source_sha256', 'artifact',
            'generation_mode', 'diagnostics', 'start_pts_ms', 'end_pts_ms'
        ])
           OR pg_catalog.jsonb_typeof(p_result -> 'artifact') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_result -> 'diagnostics') IS DISTINCT FROM 'object'
           OR p_result ->> 'generation_mode' NOT IN ('smart_copy', 'transcode')
           OR (p_result ? 'start_pts_ms' AND p_result -> 'start_pts_ms' <> 'null'::jsonb AND (
               pg_catalog.jsonb_typeof(p_result -> 'start_pts_ms') <> 'number'
               OR p_result ->> 'start_pts_ms' !~ '^-?(0|[1-9][0-9]*)$'
               OR (p_result ->> 'start_pts_ms')::numeric NOT BETWEEN -2147483648 AND 2147483647
           ))
           OR (p_result ? 'end_pts_ms' AND p_result -> 'end_pts_ms' <> 'null'::jsonb AND (
               pg_catalog.jsonb_typeof(p_result -> 'end_pts_ms') <> 'number'
               OR p_result ->> 'end_pts_ms' !~ '^-?(0|[1-9][0-9]*)$'
               OR (p_result ->> 'end_pts_ms')::numeric NOT BETWEEN -2147483648 AND 2147483647
           )) THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END IF;
    ELSE
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;

    SELECT job.* INTO v_candidate
    FROM public.asset_media_jobs AS job
    WHERE job.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._media_reply(NULL, 'lease_lost');
    END IF;
    IF v_candidate.dataset_item_id IS NOT NULL THEN
        BEGIN
            SELECT item.* INTO v_item
            FROM public.dataset_items AS item
            WHERE item.id = v_candidate.dataset_item_id
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._media_reply(NULL, 'source_busy');
        END;
    END IF;
    BEGIN
        SELECT job.* INTO v_job
        FROM public.asset_media_jobs AS job
        WHERE job.job_id = p_job_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._media_reply(NULL, 'operation_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._media_reply(NULL, 'lease_lost');
    END IF;
    IF v_job.status <> 'running'
       AND v_job.generation = p_generation
       AND v_job.last_lease_token = p_lease_token THEN
        IF v_job.last_outcome = 'published' THEN
            PERFORM aap_assets_v1._media_queue_prefix(
                'media-assets/' || p_job_id::text || '/g'
                    || p_generation::text || '/a' || p_lease_token::text || '/',
                'attempt_abandoned'
            );
            RETURN aap_assets_v1._media_worker_outcome(p_job_id, p_generation, 'replayed');
        ELSIF v_job.last_outcome = 'failed'
           AND v_job.error_code = 'frame_timetable_error' THEN
            PERFORM aap_assets_v1._media_queue_prefix(
                'media-assets/' || p_job_id::text || '/g'
                    || p_generation::text || '/a' || p_lease_token::text || '/',
                'attempt_abandoned'
            );
            RETURN aap_assets_v1._media_worker_outcome(p_job_id, p_generation, 'failed');
        ELSIF v_job.last_outcome IN ('source_changed', 'dependency_changed') THEN
            PERFORM aap_assets_v1._media_queue_prefix(
                'media-assets/' || p_job_id::text || '/g'
                    || p_generation::text || '/a' || p_lease_token::text || '/',
                'attempt_abandoned'
            );
            RETURN aap_assets_v1._media_worker_outcome(p_job_id, p_generation, v_job.last_outcome);
        END IF;
    END IF;
    IF v_job.status <> 'running'
       OR v_job.generation <> p_generation
       OR v_job.lease_token IS DISTINCT FROM p_lease_token
       OR v_job.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        PERFORM aap_assets_v1._media_queue_prefix(
            'media-assets/' || p_job_id::text || '/g'
                || p_generation::text || '/a' || p_lease_token::text || '/',
            'attempt_abandoned'
        );
        RETURN aap_assets_v1._media_worker_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    IF v_job.dataset_item_id IS NULL OR v_job.kind = 'video_backfill' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    IF v_item.id IS NULL THEN
        PERFORM aap_assets_v1._media_queue_prefix(
            'media-assets/' || v_job.job_id::text || '/g'
                || v_job.generation::text || '/a' || v_job.lease_token::text || '/',
            'source_changed'
        );
        UPDATE public.asset_media_jobs AS job
        SET status = 'canceled', lease_token = NULL, lease_expires_at = NULL,
            last_lease_token = p_lease_token, last_outcome = 'source_changed',
            completed_at = pg_catalog.clock_timestamp(),
            updated_at = pg_catalog.clock_timestamp()
        WHERE job.job_id = p_job_id;
        RETURN aap_assets_v1._media_worker_outcome(p_job_id, p_generation, 'source_changed');
    END IF;
    IF v_item.media_source_drifted
       OR v_item.media_source_generation <> v_job.source_generation THEN
        PERFORM aap_assets_v1._media_queue_prefix(
            'media-assets/' || v_job.job_id::text || '/g'
                || v_job.generation::text || '/a' || v_job.lease_token::text || '/',
            'source_changed'
        );
        UPDATE public.asset_media_jobs AS job
        SET status = 'canceled', lease_token = NULL, lease_expires_at = NULL,
            last_lease_token = p_lease_token, last_outcome = 'source_changed',
            completed_at = pg_catalog.clock_timestamp(),
            updated_at = pg_catalog.clock_timestamp()
        WHERE job.job_id = p_job_id;
        RETURN aap_assets_v1._media_worker_outcome(p_job_id, p_generation, 'source_changed');
    END IF;
    IF v_job.kind IN ('frame', 'chunk')
       AND v_item.media_timetable_revision <> v_job.timetable_revision THEN
        IF v_job.generation = 9223372036854775807 THEN
            RAISE EXCEPTION USING ERRCODE = '22003', MESSAGE = 'media job generation exhausted';
        END IF;
        PERFORM aap_assets_v1._media_queue_prefix(
            'media-assets/' || v_job.job_id::text || '/g'
                || v_job.generation::text || '/a' || v_job.lease_token::text || '/',
            'dependency_changed'
        );
        UPDATE public.asset_media_jobs AS job
        SET generation = job.generation + 1,
            status = 'pending',
            timetable_revision = v_item.media_timetable_revision,
            progress = '{}'::jsonb,
            error_code = NULL,
            error_detail = NULL,
            attempt_count = 0,
            lease_token = NULL,
            lease_expires_at = NULL,
            last_lease_token = NULL,
            last_outcome = NULL,
            accepted_at = pg_catalog.clock_timestamp(),
            updated_at = pg_catalog.clock_timestamp(),
            completed_at = NULL
        WHERE job.job_id = p_job_id;
        RETURN aap_assets_v1._media_worker_outcome(p_job_id, p_generation, 'dependency_changed');
    END IF;
    IF (v_job.kind IN ('video_metadata', 'video_timetable'))
       <> (v_result_kind IN ('video_metadata', 'video_timetable'))
       OR v_job.kind <> v_result_kind THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    v_prefix := 'media-assets/' || v_job.job_id::text || '/g'
        || v_job.generation::text || '/a' || v_job.lease_token::text || '/';
    IF v_result_kind = 'frame' THEN
        v_expected_key := v_prefix || 'frame.' || (v_job.spec ->> 'format');
        IF NOT aap_assets_v1._media_artifact_valid(p_result -> 'artifact', v_expected_key) THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END IF;
    ELSIF v_result_kind = 'chunk' THEN
        v_expected_key := v_prefix || 'chunk.mp4';
        IF NOT aap_assets_v1._media_artifact_valid(p_result -> 'artifact', v_expected_key) THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END IF;
    ELSE
        IF p_result -> 'poster' <> 'null'::jsonb
           AND NOT aap_assets_v1._media_artifact_valid(p_result -> 'poster', v_prefix || 'poster.webp') THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END IF;
        IF p_result -> 'playback' <> 'null'::jsonb
           AND NOT aap_assets_v1._media_artifact_valid(p_result -> 'playback', v_prefix || 'playback.mp4') THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END IF;
        IF (p_result -> 'metadata_patch' ? 'poster_frame_path'
            AND p_result -> 'metadata_patch' ->> 'poster_frame_path'
                IS DISTINCT FROM p_result #>> '{poster,key}')
           OR (p_result -> 'metadata_patch' ? 'playback_path'
            AND p_result -> 'metadata_patch' ->> 'playback_path'
                IS DISTINCT FROM p_result #>> '{playback,key}') THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END IF;
        IF v_job.kind = 'video_timetable'
           AND (p_result -> 'timetable_total_frames' IS NULL
                OR p_result -> 'timetable_total_frames' = 'null'::jsonb) THEN
            IF pg_catalog.jsonb_typeof(
                    p_result -> 'metadata_patch' -> 'frame_timetable_error'
               ) IS DISTINCT FROM 'string'
               OR pg_catalog.char_length(
                    p_result -> 'metadata_patch' ->> 'frame_timetable_error'
               ) NOT BETWEEN 1 AND 2000
               OR (p_result -> 'metadata_patch' - ARRAY['frame_timetable_error'])
                    IS DISTINCT FROM '{}'::jsonb
               OR EXISTS (
                    SELECT 1
                    FROM pg_catalog.jsonb_array_elements(
                        COALESCE(p_result -> 'clear_errors', '[]'::jsonb)
                    ) AS error(value)
                    WHERE error.value #>> '{}' = 'frame_timetable_error'
               ) THEN
                RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
            END IF;
            v_timetable_error_only := true;
        END IF;
        IF p_result -> 'timetable_total_frames' <> 'null'::jsonb THEN
            v_timetable_total := (p_result ->> 'timetable_total_frames')::bigint;
            v_timetable_count := (p_result ->> 'timetable_staged_count')::bigint;
            IF v_job.kind = 'video_timetable' THEN
                v_stride := (v_job.spec ->> 'sparse_stride')::integer;
            ELSE
                v_stride := 1;
            END IF;
            IF v_timetable_total < 1 OR v_stride < 1
               OR v_timetable_count < pg_catalog.ceil(v_timetable_total::numeric / v_stride)::bigint
               OR v_timetable_count > v_timetable_total
               OR ((p_result -> 'metadata_patch') ? 'frame_count'
                   AND (p_result -> 'metadata_patch') ->> 'frame_count'
                       IS DISTINCT FROM v_timetable_total::text) THEN
                RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
            END IF;
            SELECT pg_catalog.count(*),
                   pg_catalog.count(*) FILTER (WHERE staged.frame_index % v_stride = 0)
            INTO v_expected_count, v_grid_count
            FROM public.asset_media_timetable_staging AS staged
            WHERE staged.job_id = p_job_id
              AND staged.generation = p_generation
              AND staged.lease_token = p_lease_token;
            IF v_expected_count <> v_timetable_count
               OR v_grid_count <> pg_catalog.ceil(v_timetable_total::numeric / v_stride)::bigint
               OR NOT EXISTS (
                    SELECT 1
                    FROM public.asset_media_timetable_staging AS staged
                    WHERE staged.job_id = p_job_id
                      AND staged.generation = p_generation
                      AND staged.lease_token = p_lease_token
                      AND staged.frame_index = v_timetable_total - 1
               )
               OR EXISTS (
                SELECT 1
                FROM (
                    SELECT staged.frame_index, staged.pts_ms,
                           staged.is_keyframe,
                           pg_catalog.lag(staged.pts_ms) OVER (ORDER BY staged.frame_index) AS previous_pts
                    FROM public.asset_media_timetable_staging AS staged
                    WHERE staged.job_id = p_job_id
                      AND staged.generation = p_generation
                      AND staged.lease_token = p_lease_token
                ) AS ordered
                WHERE ordered.frame_index >= v_timetable_total
                   OR (ordered.frame_index % v_stride <> 0
                       AND ordered.frame_index <> v_timetable_total - 1
                       AND NOT ordered.is_keyframe)
                   OR (ordered.previous_pts IS NOT NULL AND ordered.pts_ms < ordered.previous_pts)
            ) THEN
                RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
            END IF;
        ELSIF EXISTS (
            SELECT 1 FROM public.asset_media_timetable_staging AS staged
            WHERE staged.job_id = p_job_id AND staged.generation = p_generation
              AND staged.lease_token = p_lease_token
        ) THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END IF;
    END IF;

    IF v_job.kind = 'frame' THEN
        BEGIN
            SELECT cache.storage_key INTO v_old_key
            FROM public.video_frame_cache AS cache
            WHERE cache.dataset_item_id = v_item.id
              AND cache.frame_index = (v_job.spec ->> 'frame_index')::integer
              AND cache.width = (v_job.spec ->> 'width')::integer
              AND cache.format = v_job.spec ->> 'format'
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._media_reply(NULL, 'operation_busy');
        END;
    ELSIF v_job.kind = 'chunk' THEN
        BEGIN
            SELECT chunk.storage_key INTO v_old_key
            FROM public.video_chunks AS chunk
            WHERE chunk.dataset_item_id = v_item.id
              AND chunk.chunk_id = (v_job.spec ->> 'chunk_id')::integer
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._media_reply(NULL, 'operation_busy');
        END;
    ELSE
        BEGIN
            SELECT cache.storage_key INTO v_poster_old_key
            FROM public.video_frame_cache AS cache
            WHERE cache.dataset_item_id = v_item.id
              AND cache.frame_index = 0 AND cache.width = 512 AND cache.format = 'webp'
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._media_reply(NULL, 'operation_busy');
        END;
    END IF;

    IF v_job.kind = 'chunk' THEN
        BEGIN
            v_timetable_count := NULLIF(v_item.metadata -> 'video' ->> 'frame_count', '')::bigint;
        EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            v_timetable_count := NULL;
        END;
        IF v_timetable_count IS NULL OR v_timetable_count <= 0 THEN
            RETURN aap_assets_v1._media_reply(NULL, 'metadata_not_ready');
        END IF;
        v_chunk_id := (v_job.spec ->> 'chunk_id')::integer;
        v_chunk_size := (v_job.spec ->> 'chunk_size_frames')::integer;
        v_timetable_total := v_chunk_id::bigint * v_chunk_size::bigint;
        IF v_timetable_total > 2147483647 OR v_timetable_total >= v_timetable_count THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_frame_range');
        END IF;
        v_chunk_start := v_timetable_total::integer;
        v_chunk_end := LEAST(
            v_timetable_count - 1,
            v_timetable_total + v_chunk_size - 1
        )::integer;
    END IF;

    v_source_reply := aap_assets_v1._media_reconcile_source(
        v_item.id,
        aap_assets_v1._media_source_snapshot(v_item),
        p_result -> 'source_identity',
        p_result ->> 'source_sha256'
    );
    IF v_source_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-media/v1'
       OR v_source_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_source_reply -> 'data') IS DISTINCT FROM 'object' THEN
        RETURN v_source_reply;
    END IF;
    IF (v_source_reply #> '{data,source_drifted}') = 'true'::jsonb THEN
        PERFORM aap_assets_v1._media_queue_prefix(
            'media-assets/' || v_job.job_id::text || '/g'
                || v_job.generation::text || '/a' || v_job.lease_token::text || '/',
            'source_changed'
        );
        RETURN aap_assets_v1._media_worker_outcome(p_job_id, p_generation, 'source_changed');
    END IF;
    SELECT item.* INTO v_item
    FROM public.dataset_items AS item
    WHERE item.id = v_job.dataset_item_id;
    v_source_generation := v_item.media_source_generation;
    v_timetable_revision := v_item.media_timetable_revision;

    IF v_job.kind = 'frame' THEN
        IF v_old_key IS NOT NULL AND v_old_key IS DISTINCT FROM v_expected_key THEN
            PERFORM aap_assets_v1._media_queue_key(v_old_key, 'source_changed');
        END IF;
        INSERT INTO public.video_frame_cache (
            id, dataset_item_id, frame_index, width, format, storage_key, byte_size,
            status, error, source_generation, timetable_revision, last_accessed_at
        ) VALUES (
            pg_catalog.gen_random_uuid(),
            v_item.id,
            (v_job.spec ->> 'frame_index')::integer,
            (v_job.spec ->> 'width')::integer,
            v_job.spec ->> 'format',
            p_result #>> '{artifact,key}',
            (p_result #>> '{artifact,byte_size}')::bigint,
            'ready', NULL, v_source_generation, v_timetable_revision,
            pg_catalog.clock_timestamp()
        )
        ON CONFLICT (dataset_item_id, frame_index, width, format) DO UPDATE
        SET storage_key = EXCLUDED.storage_key,
            byte_size = EXCLUDED.byte_size,
            status = 'ready', error = NULL,
            source_generation = EXCLUDED.source_generation,
            timetable_revision = EXCLUDED.timetable_revision,
            last_accessed_at = EXCLUDED.last_accessed_at,
            updated_at = pg_catalog.clock_timestamp();
    ELSIF v_job.kind = 'chunk' THEN
        IF v_old_key IS NOT NULL AND v_old_key IS DISTINCT FROM v_expected_key THEN
            PERFORM aap_assets_v1._media_queue_key(v_old_key, 'source_changed');
        END IF;
        INSERT INTO public.video_chunks (
            id, dataset_item_id, chunk_id, start_frame, end_frame,
            start_pts_ms, end_pts_ms, storage_key, byte_size, generation_mode,
            diagnostics, status, error, source_generation, timetable_revision,
            last_accessed_at
        ) VALUES (
            pg_catalog.gen_random_uuid(),
            v_item.id, v_chunk_id, v_chunk_start, v_chunk_end,
            NULLIF(p_result ->> 'start_pts_ms', '')::integer,
            NULLIF(p_result ->> 'end_pts_ms', '')::integer,
            p_result #>> '{artifact,key}',
            (p_result #>> '{artifact,byte_size}')::bigint,
            p_result ->> 'generation_mode',
            p_result -> 'diagnostics',
            'ready', NULL, v_source_generation, v_timetable_revision,
            pg_catalog.clock_timestamp()
        )
        ON CONFLICT (dataset_item_id, chunk_id) DO UPDATE
        SET start_frame = EXCLUDED.start_frame,
            end_frame = EXCLUDED.end_frame,
            start_pts_ms = EXCLUDED.start_pts_ms,
            end_pts_ms = EXCLUDED.end_pts_ms,
            storage_key = EXCLUDED.storage_key,
            byte_size = EXCLUDED.byte_size,
            generation_mode = EXCLUDED.generation_mode,
            diagnostics = EXCLUDED.diagnostics,
            status = 'ready', error = NULL,
            source_generation = EXCLUDED.source_generation,
            timetable_revision = EXCLUDED.timetable_revision,
            last_accessed_at = EXCLUDED.last_accessed_at,
            updated_at = pg_catalog.clock_timestamp();
    ELSE
        v_metadata := COALESCE(v_item.metadata, '{}'::jsonb);
        v_video := v_metadata -> 'video';
        IF pg_catalog.jsonb_typeof(v_video) IS DISTINCT FROM 'object' THEN
            v_video := '{}'::jsonb;
        END IF;
        v_certified := v_item.media_materialized_generation = v_item.media_source_generation;
        v_new_thumbnail := v_item.thumbnail_path;
        v_new_blurhash := v_item.blurhash;
        v_old_poster_path := v_video ->> 'poster_frame_path';
        v_old_playback_path := v_video ->> 'playback_path';
        IF NOT v_certified THEN
            IF v_old_poster_path IS NOT NULL
               AND v_old_poster_path NOT LIKE 'media-assets/%' THEN
                PERFORM aap_assets_v1._media_queue_key(v_old_poster_path, 'source_changed');
                v_video := v_video - 'poster_frame_path';
            END IF;
            IF v_old_playback_path IS NOT NULL
               AND v_old_playback_path NOT LIKE 'media-assets/%' THEN
                v_video := v_video - ARRAY['playback_path', 'playback_codec'];
            END IF;
            IF v_new_thumbnail IS NOT NULL
               AND v_new_thumbnail NOT LIKE 'media-assets/%' THEN
                PERFORM aap_assets_v1._media_queue_key(v_new_thumbnail, 'source_changed');
                v_new_thumbnail := NULL;
                v_new_blurhash := NULL;
            END IF;
        END IF;
        v_video := v_video || (p_result -> 'metadata_patch');
        IF p_result -> 'poster' IS NOT NULL AND p_result -> 'poster' <> 'null'::jsonb THEN
            IF v_old_poster_path IS NOT NULL
               AND v_old_poster_path IS DISTINCT FROM p_result #>> '{poster,key}' THEN
                PERFORM aap_assets_v1._media_queue_key(v_old_poster_path, 'source_changed');
            END IF;
            IF v_poster_old_key IS NOT NULL
               AND v_poster_old_key IS DISTINCT FROM v_old_poster_path
               AND v_poster_old_key IS DISTINCT FROM p_result #>> '{poster,key}' THEN
                PERFORM aap_assets_v1._media_queue_key(v_poster_old_key, 'source_changed');
            END IF;
            v_video := v_video || pg_catalog.jsonb_build_object(
                'poster_frame_path', p_result #>> '{poster,key}'
            );
            v_new_thumbnail := p_result #>> '{poster,key}';
            v_new_blurhash := NULL;
        END IF;
        IF p_result -> 'playback' IS NOT NULL AND p_result -> 'playback' <> 'null'::jsonb THEN
            IF v_old_playback_path IS NOT NULL
               AND v_old_playback_path IS DISTINCT FROM p_result #>> '{playback,key}' THEN
                PERFORM aap_assets_v1._media_queue_key(v_old_playback_path, 'source_changed');
            END IF;
            v_video := v_video || pg_catalog.jsonb_build_object(
                'playback_path', p_result #>> '{playback,key}'
            );
        END IF;
        FOR v_replay IN
            SELECT error.value #>> '{}'
            FROM pg_catalog.jsonb_array_elements(
                COALESCE(p_result -> 'clear_errors', '[]'::jsonb)
            ) AS error(value)
        LOOP
            v_video := v_video - v_replay;
        END LOOP;
        IF p_result -> 'timetable_total_frames' IS NOT NULL
           AND p_result -> 'timetable_total_frames' <> 'null'::jsonb THEN
            v_video := v_video || pg_catalog.jsonb_build_object(
                'frame_timetable_frame_count', p_result -> 'timetable_total_frames'
            ) - 'frame_timetable_error';
            IF v_item.media_timetable_revision = 9223372036854775807 THEN
                RAISE EXCEPTION USING ERRCODE = '22003', MESSAGE = 'media timetable revision exhausted';
            END IF;
            v_next_timetable_revision := v_item.media_timetable_revision + 1;
            DELETE FROM public.video_frame_indices AS frame
            WHERE frame.dataset_item_id = v_item.id;
            INSERT INTO public.video_frame_indices (
                id, dataset_item_id, frame_index, pts_ms, is_keyframe, pict_type, byte_offset
            )
            SELECT
                pg_catalog.gen_random_uuid(), v_item.id,
                staged.frame_index, staged.pts_ms, staged.is_keyframe,
                staged.pict_type, staged.byte_offset
            FROM public.asset_media_timetable_staging AS staged
            WHERE staged.job_id = p_job_id
              AND staged.generation = p_generation
              AND staged.lease_token = p_lease_token
            ORDER BY staged.frame_index;
            DELETE FROM public.asset_media_timetable_staging AS staged
            WHERE staged.job_id = p_job_id AND staged.generation = p_generation;
        END IF;
        IF p_result -> 'width' IS NOT NULL AND p_result -> 'width' <> 'null'::jsonb THEN
            v_width := (p_result ->> 'width')::integer;
        ELSE
            v_width := v_item.width;
        END IF;
        IF p_result -> 'height' IS NOT NULL AND p_result -> 'height' <> 'null'::jsonb THEN
            v_height := (p_result ->> 'height')::integer;
        ELSE
            v_height := v_item.height;
        END IF;
        v_metadata := CASE WHEN v_video = '{}'::jsonb
            THEN v_metadata - 'video'
            ELSE pg_catalog.jsonb_set(v_metadata, '{video}', v_video, true)
        END;
        v_full_success := v_job.kind = 'video_metadata'
            AND NOT (v_video ? 'probe_error')
            AND v_video ->> 'frame_count' IS NOT NULL
            AND v_video ->> 'fps' IS NOT NULL;
        IF p_result -> 'poster' IS NOT NULL AND p_result -> 'poster' <> 'null'::jsonb THEN
            INSERT INTO public.video_frame_cache (
                id, dataset_item_id, frame_index, width, format, storage_key, byte_size,
                status, error, source_generation, timetable_revision, last_accessed_at
            ) VALUES (
                pg_catalog.gen_random_uuid(), v_item.id, 0, 512, 'webp',
                p_result #>> '{poster,key}',
                (p_result #>> '{poster,byte_size}')::bigint,
                'ready', NULL, v_item.media_source_generation,
                COALESCE(v_next_timetable_revision, v_item.media_timetable_revision),
                pg_catalog.clock_timestamp()
            )
            ON CONFLICT (dataset_item_id, frame_index, width, format) DO UPDATE
            SET storage_key = EXCLUDED.storage_key,
                byte_size = EXCLUDED.byte_size,
                status = 'ready', error = NULL,
                source_generation = EXCLUDED.source_generation,
                timetable_revision = EXCLUDED.timetable_revision,
                last_accessed_at = EXCLUDED.last_accessed_at,
                updated_at = pg_catalog.clock_timestamp();
        ELSIF NOT v_certified
           AND v_poster_old_key IS NOT NULL
           AND v_poster_old_key NOT LIKE 'media-assets/%' THEN
            PERFORM aap_assets_v1._media_queue_key(v_poster_old_key, 'source_changed');
            UPDATE public.video_frame_cache AS cache
            SET status = 'pending', storage_key = NULL, byte_size = NULL,
                error = NULL, source_generation = v_item.media_source_generation,
                timetable_revision = COALESCE(v_next_timetable_revision, v_item.media_timetable_revision),
                last_accessed_at = NULL, updated_at = pg_catalog.clock_timestamp()
            WHERE cache.dataset_item_id = v_item.id
              AND cache.frame_index = 0 AND cache.width = 512 AND cache.format = 'webp';
        END IF;
        IF v_next_timetable_revision IS NOT NULL THEN
            UPDATE public.dataset_items AS item
            SET metadata = v_metadata,
                width = v_width,
                height = v_height,
                thumbnail_path = v_new_thumbnail,
                blurhash = v_new_blurhash,
                media_timetable_revision = v_next_timetable_revision,
                media_materialized_generation = CASE WHEN v_full_success
                    THEN item.media_source_generation
                    ELSE item.media_materialized_generation END,
                updated_at = pg_catalog.clock_timestamp()
            WHERE item.id = v_item.id;
            PERFORM aap_assets_v1._media_requeue_timetable_dependents(
                v_item.id, v_item.media_source_generation, v_next_timetable_revision
            );
        ELSE
            UPDATE public.dataset_items AS item
            SET metadata = v_metadata,
                width = v_width,
                height = v_height,
                thumbnail_path = v_new_thumbnail,
                blurhash = v_new_blurhash,
                media_materialized_generation = CASE WHEN v_full_success
                    THEN item.media_source_generation
                    ELSE item.media_materialized_generation END,
                updated_at = pg_catalog.clock_timestamp()
            WHERE item.id = v_item.id;
        END IF;
    END IF;

    IF v_job.kind = 'frame' THEN
        UPDATE public.asset_media_jobs AS job
        SET status = 'ready', progress = '{}'::jsonb,
            error_code = NULL, error_detail = NULL,
            lease_token = NULL, lease_expires_at = NULL,
            last_lease_token = p_lease_token, last_outcome = 'published',
            completed_at = pg_catalog.clock_timestamp(),
            updated_at = pg_catalog.clock_timestamp()
        WHERE job.job_id = p_job_id;
    ELSIF v_job.kind = 'chunk' THEN
        UPDATE public.asset_media_jobs AS job
        SET status = 'ready', progress = '{}'::jsonb,
            error_code = NULL, error_detail = NULL,
            lease_token = NULL, lease_expires_at = NULL,
            last_lease_token = p_lease_token, last_outcome = 'published',
            completed_at = pg_catalog.clock_timestamp(),
            updated_at = pg_catalog.clock_timestamp()
        WHERE job.job_id = p_job_id;
    ELSIF v_timetable_error_only THEN
        UPDATE public.asset_media_jobs AS job
        SET status = 'failed', progress = '{}'::jsonb,
            error_code = 'frame_timetable_error',
            error_detail = p_result -> 'metadata_patch' ->> 'frame_timetable_error',
            lease_token = NULL, lease_expires_at = NULL,
            last_lease_token = p_lease_token, last_outcome = 'failed',
            completed_at = pg_catalog.clock_timestamp(),
            updated_at = pg_catalog.clock_timestamp()
        WHERE job.job_id = p_job_id;
    ELSE
        UPDATE public.asset_media_jobs AS job
        SET status = 'ready', progress = CASE
                WHEN p_result -> 'timetable_staged_count' IS NOT NULL
                 AND p_result -> 'timetable_staged_count' <> 'null'::jsonb
                    THEN pg_catalog.jsonb_build_object(
                        'timetable_rows', (p_result ->> 'timetable_staged_count')::bigint,
                        'timetable_total_frames', (p_result ->> 'timetable_total_frames')::bigint
                    )
                ELSE '{}'::jsonb
            END,
            error_code = NULL, error_detail = NULL,
            lease_token = NULL, lease_expires_at = NULL,
            last_lease_token = p_lease_token, last_outcome = 'published',
            completed_at = pg_catalog.clock_timestamp(),
            updated_at = pg_catalog.clock_timestamp()
        WHERE job.job_id = p_job_id;
    END IF;
    PERFORM aap_assets_v1._media_queue_prefix(v_prefix, 'attempt_abandoned');
    RETURN aap_assets_v1._media_worker_outcome(
        p_job_id,
        p_generation,
        CASE WHEN v_timetable_error_only THEN 'failed' ELSE 'published' END
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_video_media(uuid, bigint, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fail_video_media(
    p_job_id uuid,
    p_generation bigint,
    p_lease_token uuid,
    p_error_code text,
    p_error_detail text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_candidate public.asset_media_jobs%ROWTYPE;
    v_job public.asset_media_jobs%ROWTYPE;
    v_item public.dataset_items%ROWTYPE;
    v_frame public.video_frame_cache%ROWTYPE;
    v_chunk public.video_chunks%ROWTYPE;
    v_has_frame boolean := false;
    v_has_chunk boolean := false;
BEGIN
    IF p_job_id IS NULL OR p_generation IS NULL OR p_generation < 1
       OR p_lease_token IS NULL
       OR p_error_code IS NULL OR pg_catalog.char_length(p_error_code) NOT BETWEEN 1 AND 64
       OR p_error_detail IS NULL OR pg_catalog.char_length(p_error_detail) > 2000 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    SELECT job.* INTO v_candidate
    FROM public.asset_media_jobs AS job
    WHERE job.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._media_worker_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    IF v_candidate.dataset_item_id IS NOT NULL THEN
        BEGIN
            SELECT item.* INTO v_item
            FROM public.dataset_items AS item
            WHERE item.id = v_candidate.dataset_item_id
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._media_reply(NULL, 'operation_busy');
        END;
    END IF;
    BEGIN
        SELECT job.* INTO v_job
        FROM public.asset_media_jobs AS job
        WHERE job.job_id = p_job_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._media_reply(NULL, 'operation_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._media_worker_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    IF v_job.status = 'failed'
       AND v_job.generation = p_generation
       AND v_job.last_lease_token = p_lease_token
       AND v_job.last_outcome = 'failed' THEN
        PERFORM aap_assets_v1._media_queue_prefix(
            'media-assets/' || p_job_id::text || '/g'
                || p_generation::text || '/a' || p_lease_token::text || '/',
            'attempt_abandoned'
        );
        RETURN aap_assets_v1._media_worker_outcome(p_job_id, p_generation, 'replayed');
    END IF;
    IF v_job.status <> 'running'
       OR v_job.generation <> p_generation
       OR v_job.lease_token IS DISTINCT FROM p_lease_token
       OR v_job.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        PERFORM aap_assets_v1._media_queue_prefix(
            'media-assets/' || p_job_id::text || '/g'
                || p_generation::text || '/a' || p_lease_token::text || '/',
            'attempt_abandoned'
        );
        RETURN aap_assets_v1._media_worker_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    IF v_item.id IS NOT NULL
       AND v_item.file_type = 'video'
       AND NOT v_item.media_source_drifted
       AND v_item.media_source_generation = v_job.source_generation
       AND v_candidate.dataset_item_id IS NOT DISTINCT FROM v_job.dataset_item_id
       AND v_job.kind IN ('frame', 'chunk')
       AND v_item.media_timetable_revision = v_job.timetable_revision THEN
        IF v_job.kind = 'frame'
           AND v_job.slot_key = 'frame:' || (v_job.spec ->> 'frame_index') || ':'
                || (v_job.spec ->> 'width') || ':' || (v_job.spec ->> 'format') THEN
            BEGIN
                SELECT cache.* INTO v_frame
                FROM public.video_frame_cache AS cache
                WHERE cache.dataset_item_id = v_item.id
                  AND cache.frame_index = (v_job.spec ->> 'frame_index')::integer
                  AND cache.width = (v_job.spec ->> 'width')::integer
                  AND cache.format = v_job.spec ->> 'format'
                FOR UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN aap_assets_v1._media_reply(NULL, 'operation_busy');
            END;
            v_has_frame := FOUND
                AND v_frame.source_generation = v_job.source_generation
                AND v_frame.timetable_revision = v_job.timetable_revision;
        ELSIF v_job.kind = 'chunk'
           AND v_job.slot_key = 'chunk:' || (v_job.spec ->> 'chunk_id') THEN
            BEGIN
                SELECT chunk.* INTO v_chunk
                FROM public.video_chunks AS chunk
                WHERE chunk.dataset_item_id = v_item.id
                  AND chunk.chunk_id = (v_job.spec ->> 'chunk_id')::integer
                FOR UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN aap_assets_v1._media_reply(NULL, 'operation_busy');
            END;
            v_has_chunk := FOUND
                AND v_chunk.source_generation = v_job.source_generation
                AND v_chunk.timetable_revision = v_job.timetable_revision;
        END IF;
    END IF;
    PERFORM aap_assets_v1._media_queue_prefix(
        'media-assets/' || v_job.job_id::text || '/g'
            || v_job.generation::text || '/a' || v_job.lease_token::text || '/',
        'attempt_abandoned'
    );
    IF v_has_frame THEN
        UPDATE public.video_frame_cache AS cache
        SET status = CASE WHEN v_frame.storage_key IS NOT NULL THEN 'ready' ELSE 'failed' END,
            error = CASE WHEN v_frame.storage_key IS NOT NULL THEN NULL ELSE p_error_detail END,
            updated_at = pg_catalog.clock_timestamp()
        WHERE cache.id = v_frame.id
          AND cache.source_generation = v_job.source_generation
          AND cache.timetable_revision = v_job.timetable_revision;
    ELSIF v_has_chunk THEN
        UPDATE public.video_chunks AS chunk
        SET status = CASE WHEN v_chunk.storage_key IS NOT NULL THEN 'ready' ELSE 'failed' END,
            error = CASE WHEN v_chunk.storage_key IS NOT NULL THEN NULL ELSE p_error_detail END,
            updated_at = pg_catalog.clock_timestamp()
        WHERE chunk.id = v_chunk.id
          AND chunk.source_generation = v_job.source_generation
          AND chunk.timetable_revision = v_job.timetable_revision;
    END IF;
    UPDATE public.asset_media_jobs AS job
    SET status = 'failed',
        progress = '{}'::jsonb,
        error_code = p_error_code,
        error_detail = p_error_detail,
        lease_token = NULL,
        lease_expires_at = NULL,
        last_lease_token = p_lease_token,
        last_outcome = 'failed',
        completed_at = pg_catalog.clock_timestamp(),
        updated_at = pg_catalog.clock_timestamp()
    WHERE job.job_id = p_job_id;
    RETURN aap_assets_v1._media_worker_outcome(p_job_id, p_generation, 'failed');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fail_video_media(uuid, bigint, uuid, text, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.expire_video_media(
    p_frame_ttl_days integer,
    p_chunk_ttl_days integer,
    p_batch_size integer
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_candidate record;
    v_item public.dataset_items%ROWTYPE;
    v_frame public.video_frame_cache%ROWTYPE;
    v_chunk public.video_chunks%ROWTYPE;
    v_job public.asset_media_jobs%ROWTYPE;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_frame_cutoff timestamptz;
    v_chunk_cutoff timestamptz;
    v_frames integer := 0;
    v_chunks integer := 0;
    v_busy boolean := false;
    v_more boolean := false;
BEGIN
    IF p_frame_ttl_days NOT BETWEEN 1 AND 3650
       OR p_chunk_ttl_days NOT BETWEEN 1 AND 3650
       OR p_batch_size NOT BETWEEN 1 AND 500 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    v_frame_cutoff := v_now - pg_catalog.make_interval(days => p_frame_ttl_days);
    v_chunk_cutoff := v_now - pg_catalog.make_interval(days => p_chunk_ttl_days);
    FOR v_candidate IN
        SELECT cache.id, cache.dataset_item_id
        FROM public.video_frame_cache AS cache
        JOIN public.dataset_items AS item ON item.id = cache.dataset_item_id
        WHERE cache.status = 'ready'
          AND cache.storage_key IS NOT NULL
          AND COALESCE(cache.last_accessed_at, cache.created_at) <= v_frame_cutoff
          AND cache.source_generation = item.media_source_generation
          AND cache.timetable_revision = item.media_timetable_revision
          AND cache.storage_key IS DISTINCT FROM item.thumbnail_path
          AND cache.storage_key IS DISTINCT FROM item.metadata -> 'video' ->> 'poster_frame_path'
          AND NOT EXISTS (
              SELECT 1 FROM public.asset_media_jobs AS job
              WHERE job.dataset_item_id = cache.dataset_item_id
                AND job.kind = 'frame'
                AND job.slot_key = 'frame:' || cache.frame_index::text || ':'
                    || cache.width::text || ':' || cache.format
                AND job.status = 'running'
                AND job.lease_expires_at > v_now
          )
        ORDER BY COALESCE(cache.last_accessed_at, cache.created_at), cache.id
        LIMIT p_batch_size
    LOOP
        v_item := NULL;
        v_frame := NULL;
        BEGIN
            SELECT item.* INTO v_item
            FROM public.dataset_items AS item
            WHERE item.id = v_candidate.dataset_item_id
            FOR UPDATE NOWAIT;
            IF FOUND THEN
                SELECT cache.* INTO v_frame
                FROM public.video_frame_cache AS cache
                WHERE cache.id = v_candidate.id
                FOR UPDATE NOWAIT;
            END IF;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            v_busy := true;
            CONTINUE;
        END;
        IF v_item.id IS NULL OR v_frame.id IS NULL THEN
            CONTINUE;
        END IF;
        IF v_frame.status <> 'ready'
           OR v_frame.storage_key IS NULL
           OR COALESCE(v_frame.last_accessed_at, v_frame.created_at) > v_frame_cutoff
           OR v_frame.source_generation <> v_item.media_source_generation
           OR v_frame.timetable_revision <> v_item.media_timetable_revision
           OR v_frame.storage_key IS NOT DISTINCT FROM v_item.thumbnail_path
           OR v_frame.storage_key IS NOT DISTINCT FROM v_item.metadata -> 'video' ->> 'poster_frame_path' THEN
            CONTINUE;
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.asset_media_jobs AS job
            WHERE job.dataset_item_id = v_frame.dataset_item_id
              AND job.kind = 'frame'
              AND job.slot_key = 'frame:' || v_frame.frame_index::text || ':'
                  || v_frame.width::text || ':' || v_frame.format
              AND job.status = 'running'
              AND job.lease_expires_at > v_now
        ) THEN
            CONTINUE;
        END IF;
        BEGIN
            SELECT job.* INTO v_job
            FROM public.asset_media_jobs AS job
            WHERE job.dataset_item_id = v_frame.dataset_item_id
              AND job.kind = 'frame'
              AND job.slot_key = 'frame:' || v_frame.frame_index::text || ':'
                  || v_frame.width::text || ':' || v_frame.format
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            v_busy := true;
            CONTINUE;
        END;
        IF FOUND THEN
            IF v_job.generation = 9223372036854775807 THEN
                RAISE EXCEPTION USING ERRCODE = '22003', MESSAGE = 'media job generation exhausted';
            END IF;
            IF v_job.lease_token IS NOT NULL THEN
                PERFORM aap_assets_v1._media_queue_prefix(
                    'media-assets/' || v_job.job_id::text || '/g'
                        || v_job.generation::text || '/a' || v_job.lease_token::text || '/',
                    'attempt_abandoned'
                );
            END IF;
            UPDATE public.asset_media_jobs AS job
            SET generation = job.generation + 1,
                status = 'pending', source_generation = v_item.media_source_generation,
                timetable_revision = v_item.media_timetable_revision,
                progress = '{}'::jsonb, error_code = NULL, error_detail = NULL,
                attempt_count = 0, lease_token = NULL, lease_expires_at = NULL,
                last_lease_token = NULL, last_outcome = NULL,
                accepted_at = v_now, updated_at = v_now, completed_at = NULL
            WHERE job.job_id = v_job.job_id;
        END IF;
        PERFORM aap_assets_v1._media_queue_key(v_frame.storage_key, 'cache_expired');
        UPDATE public.video_frame_cache AS cache
        SET storage_key = NULL, byte_size = NULL, status = 'pending', error = NULL,
            last_accessed_at = NULL, updated_at = v_now
        WHERE cache.id = v_frame.id;
        v_frames := v_frames + 1;
    END LOOP;

    FOR v_candidate IN
        SELECT chunk.id, chunk.dataset_item_id
        FROM public.video_chunks AS chunk
        JOIN public.dataset_items AS item ON item.id = chunk.dataset_item_id
        WHERE chunk.status = 'ready'
          AND chunk.storage_key IS NOT NULL
          AND COALESCE(chunk.last_accessed_at, chunk.created_at) <= v_chunk_cutoff
          AND chunk.source_generation = item.media_source_generation
          AND chunk.timetable_revision = item.media_timetable_revision
          AND NOT EXISTS (
              SELECT 1 FROM public.asset_media_jobs AS job
              WHERE job.dataset_item_id = chunk.dataset_item_id
                AND job.kind = 'chunk'
                AND job.slot_key = 'chunk:' || chunk.chunk_id::text
                AND job.status = 'running'
                AND job.lease_expires_at > v_now
          )
        ORDER BY COALESCE(chunk.last_accessed_at, chunk.created_at), chunk.id
        LIMIT p_batch_size
    LOOP
        v_item := NULL;
        v_chunk := NULL;
        BEGIN
            SELECT item.* INTO v_item
            FROM public.dataset_items AS item
            WHERE item.id = v_candidate.dataset_item_id
            FOR UPDATE NOWAIT;
            IF FOUND THEN
                SELECT chunk.* INTO v_chunk
                FROM public.video_chunks AS chunk
                WHERE chunk.id = v_candidate.id
                FOR UPDATE NOWAIT;
            END IF;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            v_busy := true;
            CONTINUE;
        END;
        IF v_item.id IS NULL OR v_chunk.id IS NULL THEN
            CONTINUE;
        END IF;
        IF v_chunk.status <> 'ready'
           OR v_chunk.storage_key IS NULL
           OR COALESCE(v_chunk.last_accessed_at, v_chunk.created_at) > v_chunk_cutoff
           OR v_chunk.source_generation <> v_item.media_source_generation
           OR v_chunk.timetable_revision <> v_item.media_timetable_revision THEN
            CONTINUE;
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.asset_media_jobs AS job
            WHERE job.dataset_item_id = v_chunk.dataset_item_id
              AND job.kind = 'chunk'
              AND job.slot_key = 'chunk:' || v_chunk.chunk_id::text
              AND job.status = 'running'
              AND job.lease_expires_at > v_now
        ) THEN
            CONTINUE;
        END IF;
        BEGIN
            SELECT job.* INTO v_job
            FROM public.asset_media_jobs AS job
            WHERE job.dataset_item_id = v_chunk.dataset_item_id
              AND job.kind = 'chunk'
              AND job.slot_key = 'chunk:' || v_chunk.chunk_id::text
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            v_busy := true;
            CONTINUE;
        END;
        IF FOUND THEN
            IF v_job.generation = 9223372036854775807 THEN
                RAISE EXCEPTION USING ERRCODE = '22003', MESSAGE = 'media job generation exhausted';
            END IF;
            IF v_job.lease_token IS NOT NULL THEN
                PERFORM aap_assets_v1._media_queue_prefix(
                    'media-assets/' || v_job.job_id::text || '/g'
                        || v_job.generation::text || '/a' || v_job.lease_token::text || '/',
                    'attempt_abandoned'
                );
            END IF;
            UPDATE public.asset_media_jobs AS job
            SET generation = job.generation + 1,
                status = 'pending', source_generation = v_item.media_source_generation,
                timetable_revision = v_item.media_timetable_revision,
                progress = '{}'::jsonb, error_code = NULL, error_detail = NULL,
                attempt_count = 0, lease_token = NULL, lease_expires_at = NULL,
                last_lease_token = NULL, last_outcome = NULL,
                accepted_at = v_now, updated_at = v_now, completed_at = NULL
            WHERE job.job_id = v_job.job_id;
        END IF;
        PERFORM aap_assets_v1._media_queue_key(v_chunk.storage_key, 'cache_expired');
        UPDATE public.video_chunks AS chunk
        SET storage_key = NULL, byte_size = NULL, status = 'pending', error = NULL,
            generation_mode = NULL, diagnostics = '{}'::jsonb,
            last_accessed_at = NULL, updated_at = v_now
        WHERE chunk.id = v_chunk.id;
        v_chunks := v_chunks + 1;
    END LOOP;
    SELECT EXISTS (
        SELECT 1 FROM public.video_frame_cache AS cache
        JOIN public.dataset_items AS item ON item.id = cache.dataset_item_id
        WHERE cache.status = 'ready' AND cache.storage_key IS NOT NULL
          AND COALESCE(cache.last_accessed_at, cache.created_at) <= v_frame_cutoff
          AND cache.source_generation = item.media_source_generation
          AND cache.timetable_revision = item.media_timetable_revision
          AND cache.storage_key IS DISTINCT FROM item.thumbnail_path
          AND cache.storage_key IS DISTINCT FROM item.metadata -> 'video' ->> 'poster_frame_path'
    ) OR EXISTS (
        SELECT 1 FROM public.video_chunks AS chunk
        JOIN public.dataset_items AS item ON item.id = chunk.dataset_item_id
        WHERE chunk.status = 'ready' AND chunk.storage_key IS NOT NULL
          AND COALESCE(chunk.last_accessed_at, chunk.created_at) <= v_chunk_cutoff
          AND chunk.source_generation = item.media_source_generation
          AND chunk.timetable_revision = item.media_timetable_revision
    ) INTO v_more;
    IF v_busy AND v_frames = 0 AND v_chunks = 0 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'operation_busy');
    END IF;
    RETURN aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object(
            'frames_expired', v_frames,
            'chunks_expired', v_chunks,
            'more', v_more
        ),
        NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.expire_video_media(integer, integer, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.claim_video_media_garbage(
    p_worker_id uuid,
    p_lease_seconds integer,
    p_batch_size integer
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_row public.asset_media_garbage%ROWTYPE;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_token uuid;
    v_expiry timestamptz;
    v_item jsonb;
    v_items jsonb := '[]'::jsonb;
    v_test jsonb;
    v_is_live boolean;
BEGIN
    IF p_worker_id IS NULL OR p_lease_seconds NOT BETWEEN 30 AND 3600
       OR p_batch_size NOT BETWEEN 1 AND 100 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    FOR v_row IN
        SELECT garbage.*
        FROM public.asset_media_garbage AS garbage
        WHERE garbage.completed_at IS NULL
          AND garbage.not_before <= v_now
          AND (garbage.lease_token IS NULL OR garbage.lease_expires_at <= v_now)
        ORDER BY garbage.not_before, garbage.created_at, garbage.garbage_id
        LIMIT p_batch_size
        FOR UPDATE SKIP LOCKED
    LOOP
        IF v_row.prefix IS NOT NULL THEN
            IF v_row.prefix NOT LIKE 'media-assets/%'
               AND v_row.prefix NOT LIKE 'videos/%' THEN
                UPDATE public.asset_media_garbage AS garbage
                SET completed_at = v_now,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    last_ack_token = NULL
                WHERE garbage.garbage_id = v_row.garbage_id;
                CONTINUE;
            END IF;
            v_is_live := EXISTS (
                SELECT 1 FROM public.dataset_items AS item
                WHERE (item.thumbnail_path LIKE v_row.prefix || '%')
                   OR ((item.metadata -> 'video' ->> 'poster_frame_path') LIKE v_row.prefix || '%')
                   OR ((item.metadata -> 'video' ->> 'playback_path') LIKE v_row.prefix || '%')
            ) OR EXISTS (
                SELECT 1 FROM public.video_frame_cache AS cache
                WHERE cache.storage_key LIKE v_row.prefix || '%'
            ) OR EXISTS (
                SELECT 1 FROM public.video_chunks AS chunk
                WHERE chunk.storage_key LIKE v_row.prefix || '%'
            ) OR EXISTS (
                SELECT 1 FROM public.asset_media_jobs AS job
                WHERE job.status = 'running'
                  AND job.lease_expires_at > v_now
                  AND job.dataset_item_id IS NOT NULL
                  AND v_row.prefix = 'media-assets/' || job.job_id::text || '/g'
                      || job.generation::text || '/a' || job.lease_token::text || '/'
            );
        ELSE
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements_text(v_row.keys) AS key(value)
                WHERE key.value NOT LIKE 'videos/%'
                  AND key.value NOT LIKE 'media-assets/%'
            ) THEN
                UPDATE public.asset_media_garbage AS garbage
                SET completed_at = v_now,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    last_ack_token = NULL
                WHERE garbage.garbage_id = v_row.garbage_id;
                CONTINUE;
            END IF;
            v_is_live := EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements_text(v_row.keys) AS key(value)
                JOIN public.dataset_items AS item
                  ON item.thumbnail_path = key.value
                  OR item.metadata -> 'video' ->> 'poster_frame_path' = key.value
                  OR item.metadata -> 'video' ->> 'playback_path' = key.value
            ) OR EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements_text(v_row.keys) AS key(value)
                JOIN public.video_frame_cache AS cache ON cache.storage_key = key.value
            ) OR EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements_text(v_row.keys) AS key(value)
                JOIN public.video_chunks AS chunk ON chunk.storage_key = key.value
            ) OR EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements_text(v_row.keys) AS key(value)
                JOIN public.asset_media_jobs AS job
                  ON job.status = 'running'
                 AND job.lease_expires_at > v_now
                 AND job.dataset_item_id IS NOT NULL
                 AND key.value LIKE 'media-assets/' || job.job_id::text || '/g'
                     || job.generation::text || '/a' || job.lease_token::text || '/%'
            );
        END IF;
        IF v_is_live THEN
            IF v_row.prefix IS NULL THEN
                UPDATE public.asset_media_garbage AS garbage
                SET completed_at = v_now,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    last_ack_token = NULL
                WHERE garbage.garbage_id = v_row.garbage_id;
            ELSE
                UPDATE public.asset_media_garbage AS garbage
                SET not_before = v_now + INTERVAL '5 minutes',
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    last_ack_token = NULL
                WHERE garbage.garbage_id = v_row.garbage_id;
            END IF;
            CONTINUE;
        END IF;
        v_token := pg_catalog.gen_random_uuid();
        v_expiry := v_now + pg_catalog.make_interval(secs => p_lease_seconds);
        v_item := pg_catalog.jsonb_build_object(
            'garbage_id', v_row.garbage_id,
            'lease_token', v_token,
            'prefix', v_row.prefix,
            'keys', v_row.keys
        );
        v_test := v_items || pg_catalog.jsonb_build_array(v_item);
        IF pg_catalog.octet_length(pg_catalog.convert_to(v_test::text, 'UTF8')) > 15728640 THEN
            UPDATE public.asset_media_garbage AS garbage
            SET not_before = v_now + INTERVAL '5 minutes',
                lease_token = NULL,
                lease_expires_at = NULL,
                last_ack_token = NULL
            WHERE garbage.garbage_id = v_row.garbage_id;
            EXIT;
        END IF;
        UPDATE public.asset_media_garbage AS garbage
        SET lease_token = v_token,
            lease_expires_at = v_expiry,
            not_before = v_expiry,
            last_ack_token = NULL,
            attempt_count = garbage.attempt_count + 1
        WHERE garbage.garbage_id = v_row.garbage_id;
        v_items := v_test;
    END LOOP;
    RETURN aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object('items', v_items), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.claim_video_media_garbage(uuid, integer, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.complete_video_media_garbage(
    p_garbage_id uuid,
    p_lease_token uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_garbage public.asset_media_garbage%ROWTYPE;
    v_next_sweep timestamptz;
BEGIN
    IF p_garbage_id IS NULL OR p_lease_token IS NULL THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT garbage.* INTO v_garbage
        FROM public.asset_media_garbage AS garbage
        WHERE garbage.garbage_id = p_garbage_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._media_reply(NULL, 'operation_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._media_reply(
            pg_catalog.jsonb_build_object('garbage_id', p_garbage_id, 'completed', false), NULL
        );
    END IF;
    IF v_garbage.prefix LIKE 'media-assets/%'
       AND v_garbage.last_ack_token = p_lease_token THEN
        RETURN aap_assets_v1._media_reply(
            pg_catalog.jsonb_build_object('garbage_id', p_garbage_id, 'completed', true), NULL
        );
    END IF;
    IF v_garbage.completed_at IS NOT NULL
       AND v_garbage.lease_token = p_lease_token THEN
        RETURN aap_assets_v1._media_reply(
            pg_catalog.jsonb_build_object('garbage_id', p_garbage_id, 'completed', true), NULL
        );
    END IF;
    IF v_garbage.completed_at IS NOT NULL
       OR v_garbage.lease_token IS DISTINCT FROM p_lease_token
       OR v_garbage.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._media_reply(
            pg_catalog.jsonb_build_object('garbage_id', p_garbage_id, 'completed', false), NULL
        );
    END IF;
    IF v_garbage.prefix LIKE 'media-assets/%' THEN
        v_next_sweep := pg_catalog.clock_timestamp() + INTERVAL '5 minutes';
        UPDATE public.asset_media_garbage AS garbage
        SET not_before = v_next_sweep,
            lease_expires_at = v_next_sweep,
            last_ack_token = p_lease_token
        WHERE garbage.garbage_id = p_garbage_id;
    ELSE
        UPDATE public.asset_media_garbage AS garbage
        SET completed_at = pg_catalog.clock_timestamp(),
            last_ack_token = p_lease_token
        WHERE garbage.garbage_id = p_garbage_id;
    END IF;
    RETURN aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object('garbage_id', p_garbage_id, 'completed', true), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.complete_video_media_garbage(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_video_media_usage()
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_frame_bytes numeric;
    v_chunk_bytes numeric;
    v_pending bigint;
    v_running bigint;
    v_failed bigint;
BEGIN
    SELECT COALESCE(pg_catalog.sum(cache.byte_size), 0)
    INTO v_frame_bytes
    FROM public.video_frame_cache AS cache
    JOIN public.dataset_items AS item ON item.id = cache.dataset_item_id
    WHERE cache.status = 'ready'
      AND cache.storage_key IS NOT NULL
      AND cache.byte_size IS NOT NULL
      AND cache.source_generation = item.media_source_generation
      AND cache.timetable_revision = item.media_timetable_revision
      AND NOT item.media_source_drifted;
    SELECT COALESCE(pg_catalog.sum(chunk.byte_size), 0)
    INTO v_chunk_bytes
    FROM public.video_chunks AS chunk
    JOIN public.dataset_items AS item ON item.id = chunk.dataset_item_id
    WHERE chunk.status = 'ready'
      AND chunk.storage_key IS NOT NULL
      AND chunk.byte_size IS NOT NULL
      AND chunk.source_generation = item.media_source_generation
      AND chunk.timetable_revision = item.media_timetable_revision
      AND NOT item.media_source_drifted;
    IF v_frame_bytes > 9223372036854775807
       OR v_chunk_bytes > 9223372036854775807 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'resource_budget_exceeded');
    END IF;
    SELECT
        pg_catalog.count(*) FILTER (WHERE job.status = 'pending'),
        pg_catalog.count(*) FILTER (WHERE job.status = 'running'),
        pg_catalog.count(*) FILTER (WHERE job.status = 'failed')
    INTO v_pending, v_running, v_failed
    FROM public.asset_media_jobs AS job;
    RETURN aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object(
            'frame_bytes', v_frame_bytes::bigint,
            'chunk_bytes', v_chunk_bytes::bigint,
            'pending', v_pending,
            'running', v_running,
            'failed', v_failed
        ),
        NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_video_media_usage() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.advance_video_backfill(
    p_job_id uuid,
    p_generation bigint,
    p_lease_token uuid,
    p_batch_size integer
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_job public.asset_media_jobs%ROWTYPE;
    v_item public.dataset_items%ROWTYPE;
    v_snapshot jsonb;
    v_check_snapshot jsonb;
    v_progress jsonb;
    v_child_spec jsonb;
    v_enqueue jsonb;
    v_saved_result jsonb;
    v_error text;
    v_next_cursor uuid;
    v_scheduled bigint;
    v_skipped bigint;
    v_page_scheduled bigint := 0;
    v_page_skipped bigint := 0;
    v_total bigint;
    v_only_missing boolean;
    v_has_more boolean := false;
    v_complete boolean;
    v_data jsonb;
    v_final_result jsonb;
    v_item_snapshot text;
BEGIN
    IF p_job_id IS NULL OR p_generation IS NULL OR p_generation < 1
       OR p_lease_token IS NULL OR p_batch_size NOT BETWEEN 1 AND 500 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT job.* INTO v_job
        FROM public.asset_media_jobs AS job
        WHERE job.job_id = p_job_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._media_reply(NULL, 'operation_busy');
    END;
    IF NOT FOUND OR v_job.kind <> 'video_backfill' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'lease_lost');
    END IF;
    v_progress := COALESCE(v_job.progress, '{}'::jsonb);
    IF v_progress ->> 'last_advance_lease_token' = p_lease_token::text
       AND v_progress ->> 'last_advance_batch_size' = p_batch_size::text
       AND pg_catalog.jsonb_typeof(v_progress -> 'last_advance_result') = 'object' THEN
        RETURN aap_assets_v1._media_reply(v_progress -> 'last_advance_result', NULL);
    END IF;
    IF v_job.status <> 'running'
       OR v_job.generation <> p_generation
       OR v_job.lease_token IS DISTINCT FROM p_lease_token
       OR v_job.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._media_reply(NULL, 'lease_lost');
    END IF;
    v_snapshot := aap_assets_v1._media_backfill_snapshot(
        v_job.backfill_selection,
        v_job.backfill_cutoff
    );
    IF pg_catalog.jsonb_typeof(v_snapshot) IS DISTINCT FROM 'object'
       OR v_snapshot ->> 'snapshot_digest' !~ '^[0-9a-f]{64}$'
       OR v_snapshot ->> 'total' IS NULL THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    v_total := (v_snapshot ->> 'total')::bigint;
    v_only_missing := COALESCE(
        v_job.backfill_selection -> 'only_missing' = 'true'::jsonb,
        true
    );
    IF v_progress ? 'snapshot_digest'
       AND (v_progress ->> 'snapshot_digest' IS DISTINCT FROM v_snapshot ->> 'snapshot_digest'
            OR (v_progress ->> 'total')::bigint IS DISTINCT FROM v_total) THEN
        UPDATE public.asset_media_jobs AS job
        SET status = 'canceled', lease_token = NULL, lease_expires_at = NULL,
            last_lease_token = p_lease_token, last_outcome = 'source_changed',
            completed_at = pg_catalog.clock_timestamp(),
            updated_at = pg_catalog.clock_timestamp()
        WHERE job.job_id = p_job_id;
        RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
    END IF;
    IF NOT (v_progress ? 'snapshot_digest') THEN
        v_progress := v_progress || pg_catalog.jsonb_build_object(
            'snapshot_digest', v_snapshot ->> 'snapshot_digest',
            'total', v_total,
            'scheduled', 0,
            'skipped', 0
        );
    END IF;
    v_scheduled := COALESCE((v_progress ->> 'scheduled')::bigint, 0);
    v_skipped := COALESCE((v_progress ->> 'skipped')::bigint, 0);
    v_next_cursor := v_job.backfill_cursor;
    BEGIN
        FOR v_item IN
            SELECT item.*
            FROM public.dataset_items AS item
            WHERE item.file_type = 'video'
              AND item.created_at <= v_job.backfill_cutoff
              AND (v_job.backfill_cursor IS NULL OR item.id > v_job.backfill_cursor)
              AND (
                  (v_job.backfill_selection -> 'dataset_id' <> 'null'::jsonb
                   AND item.dataset_id = (v_job.backfill_selection ->> 'dataset_id')::uuid)
                  OR EXISTS (
                      SELECT 1
                      FROM pg_catalog.jsonb_array_elements_text(
                          v_job.backfill_selection -> 'dataset_item_ids'
                      ) AS selected(item_id)
                      WHERE selected.item_id::uuid = item.id
                  )
                  OR v_job.backfill_selection -> 'all_items' = 'true'::jsonb
              )
            ORDER BY item.id
            LIMIT p_batch_size
        LOOP
            v_next_cursor := v_item.id;
            IF v_only_missing
               AND v_item.media_materialized_generation >= v_item.media_source_generation THEN
                v_page_skipped := v_page_skipped + 1;
                CONTINUE;
            END IF;
            v_item_snapshot := aap_assets_v1._media_source_snapshot(v_item);
            v_child_spec := pg_catalog.jsonb_build_object(
                'kind', 'video_metadata',
                'generate_playback', true,
                'generate_poster', true,
                'probe_timetable', true,
                'expected_source_snapshot', v_item_snapshot
            );
            v_enqueue := aap_assets_v1._media_enqueue(
                NULL,
                NULL,
                v_item.id,
                v_child_spec,
                NOT v_only_missing
            );
            IF v_enqueue -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                v_error := v_enqueue #>> '{error,code}';
                IF v_error IN ('source_busy', 'operation_busy') THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'media_source_busy';
                END IF;
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'media_source_changed';
            END IF;
            IF v_enqueue #>> '{data,work,status}' IN ('pending', 'running') THEN
                v_page_scheduled := v_page_scheduled + 1;
            ELSE
                v_page_skipped := v_page_skipped + 1;
            END IF;
        END LOOP;
        v_check_snapshot := aap_assets_v1._media_backfill_snapshot(
            v_job.backfill_selection,
            v_job.backfill_cutoff
        );
        IF v_check_snapshot IS NULL
           OR v_check_snapshot ->> 'snapshot_digest'
                IS DISTINCT FROM v_snapshot ->> 'snapshot_digest'
           OR (v_check_snapshot ->> 'total')::bigint IS DISTINCT FROM v_total THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'media_source_changed';
        END IF;
        SELECT EXISTS (
            SELECT 1
            FROM public.dataset_items AS item
            WHERE item.file_type = 'video'
              AND item.created_at <= v_job.backfill_cutoff
              AND (v_next_cursor IS NULL OR item.id > v_next_cursor)
              AND (
                  (v_job.backfill_selection -> 'dataset_id' <> 'null'::jsonb
                   AND item.dataset_id = (v_job.backfill_selection ->> 'dataset_id')::uuid)
                  OR EXISTS (
                      SELECT 1
                      FROM pg_catalog.jsonb_array_elements_text(
                          v_job.backfill_selection -> 'dataset_item_ids'
                      ) AS selected(item_id)
                      WHERE selected.item_id::uuid = item.id
                  )
                  OR v_job.backfill_selection -> 'all_items' = 'true'::jsonb
              )
        ) INTO v_has_more;
        v_complete := NOT v_has_more;
        v_scheduled := v_scheduled + v_page_scheduled;
        v_skipped := v_skipped + v_page_skipped;
        v_data := pg_catalog.jsonb_build_object(
            'job_id', p_job_id,
            'generation', p_generation,
            'lease_token', p_lease_token,
            'scheduled', v_scheduled,
            'skipped', v_skipped,
            'complete', v_complete,
            'next_item_id', v_next_cursor
        );
        v_progress := v_progress || pg_catalog.jsonb_build_object(
            'scheduled', v_scheduled,
            'skipped', v_skipped,
            'last_advance_lease_token', p_lease_token,
            'last_advance_batch_size', p_batch_size,
            'last_advance_result', v_data
        );
        UPDATE public.asset_media_jobs AS job
        SET status = CASE WHEN v_complete THEN 'ready' ELSE 'pending' END,
            backfill_cursor = v_next_cursor,
            progress = v_progress,
            lease_token = NULL,
            lease_expires_at = NULL,
            last_lease_token = p_lease_token,
            last_outcome = 'published',
            completed_at = CASE WHEN v_complete THEN pg_catalog.clock_timestamp() END,
            updated_at = pg_catalog.clock_timestamp()
        WHERE job.job_id = p_job_id;
        v_final_result := aap_assets_v1._media_reply(v_data, NULL);
    EXCEPTION
        WHEN SQLSTATE 'P0001' THEN
            v_error := SQLERRM;
    END;
    IF v_error = 'media_source_busy' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_busy');
    ELSIF v_error = 'media_source_changed' THEN
        UPDATE public.asset_media_jobs AS job
        SET status = 'canceled', lease_token = NULL, lease_expires_at = NULL,
            last_lease_token = p_lease_token, last_outcome = 'source_changed',
            completed_at = pg_catalog.clock_timestamp(),
            updated_at = pg_catalog.clock_timestamp()
        WHERE job.job_id = p_job_id;
        RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
    END IF;
    RETURN v_final_result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.advance_video_backfill(uuid, bigint, uuid, integer) FROM PUBLIC;
