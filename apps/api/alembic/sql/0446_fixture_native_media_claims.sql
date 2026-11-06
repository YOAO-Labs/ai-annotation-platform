CREATE OR REPLACE FUNCTION aap_assets_v1.claim_image_media(
    p_worker_id uuid, p_job_id uuid, p_lease_seconds integer, p_kinds text[]
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_candidate record;
    v_job public.asset_image_jobs%ROWTYPE;
    v_asset public.image_pyramid_assets%ROWTYPE;
    v_generation public.image_pyramid_generations%ROWTYPE;
    v_asset_present boolean;
    v_generation_present boolean;
    v_source_reply jsonb;
    v_source jsonb;
    v_code text;
    v_spec jsonb;
    v_policy jsonb;
    v_lease_seconds integer;
    v_token uuid;
    v_expires timestamptz;
    v_prefix text;
    v_claim jsonb;
    v_busy boolean := false;
    v_modified boolean := false;
BEGIN
    IF p_worker_id IS NULL OR p_lease_seconds IS NULL
       OR p_lease_seconds NOT BETWEEN 30 AND 2147483647
       OR p_kinds IS NULL OR pg_catalog.cardinality(p_kinds) NOT BETWEEN 1 AND 4
       OR EXISTS (
            SELECT 1 FROM pg_catalog.unnest(p_kinds) AS kind(value)
            WHERE kind.value IS NULL OR kind.value NOT IN (
                'dimensions', 'thumbnail', 'pyramid', 'image_backfill'
            )
       ) OR pg_catalog.cardinality(p_kinds) <> (
            SELECT pg_catalog.count(DISTINCT kind.value)
            FROM pg_catalog.unnest(p_kinds) AS kind(value)
       ) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    FOR v_candidate IN
        SELECT job.job_id, job.owner_kind, job.owner_id, job.kind
        FROM public.asset_image_jobs AS job
        WHERE job.kind = ANY (p_kinds)
          AND aap_assets_v1.fixture_native_claim_allowed('image',job.job_id,pg_catalog.pg_backend_pid())
          AND (p_job_id IS NULL OR job.job_id = p_job_id)
          AND (job.status = 'pending'
               OR (job.status = 'running'
                   AND job.lease_expires_at <= pg_catalog.clock_timestamp()))
        ORDER BY job.accepted_at, job.job_id
        LIMIT 100
    LOOP
        v_source := NULL;
        v_code := NULL;
        v_asset_present := false;
        v_generation_present := false;
        IF v_candidate.kind <> 'image_backfill' THEN
            v_source_reply := aap_assets_v1._image_resolve_owner(
                v_candidate.owner_kind, v_candidate.owner_id, true
            );
            IF v_source_reply IS NULL
               OR v_source_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
               OR pg_catalog.jsonb_typeof(v_source_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_claim_invalid_response';
            END IF;
            IF v_source_reply -> 'ok' = 'true'::jsonb THEN
                v_source := v_source_reply -> 'data';
                IF pg_catalog.jsonb_typeof(v_source) IS DISTINCT FROM 'object'
                   OR pg_catalog.jsonb_typeof(v_source -> 'owner') IS DISTINCT FROM 'object'
                   OR v_source #>> '{owner,kind}' IS DISTINCT FROM v_candidate.owner_kind
                   OR v_source #>> '{owner,id}' IS DISTINCT FROM v_candidate.owner_id::text THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_claim_invalid_response';
                END IF;
            ELSE
                v_code := v_source_reply #>> '{error,code}';
                IF v_code IN ('source_busy', 'task_busy') THEN
                    v_busy := true;
                    CONTINUE;
                ELSIF v_code IS NULL THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_claim_invalid_response';
                END IF;
            END IF;
        END IF;
        BEGIN
            SELECT job.* INTO v_job
            FROM public.asset_image_jobs AS job
            WHERE job.job_id = v_candidate.job_id
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            v_busy := true;
            CONTINUE;
        END;
        IF NOT FOUND OR v_job.kind <> v_candidate.kind
           OR v_job.status NOT IN ('pending', 'running')
           OR (v_job.status = 'running'
               AND v_job.lease_expires_at > pg_catalog.clock_timestamp()) THEN
            CONTINUE;
        END IF;
        IF v_job.kind = 'image_backfill' THEN
            v_source := NULL;
        ELSIF v_code = 'source_not_ready' THEN
            CONTINUE;
        ELSIF v_code IS NOT NULL THEN
            IF v_code NOT IN ('source_missing', 'source_changed', 'owner_changed', 'not_image') THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_claim_invalid_response';
            END IF;
        ELSIF v_source -> 'source_drifted' = 'true'::jsonb
           OR v_source -> 'file_type' IS DISTINCT FROM '"image"'::jsonb
           OR v_source ->> 'source_snapshot' IS DISTINCT FROM v_job.source_snapshot
           OR (v_source ->> 'source_generation')::bigint IS DISTINCT FROM v_job.source_generation
           OR NOT aap_assets_v1._image_exec_identity_equal(
                v_source -> 'object_identity', v_job.source_facts -> 'object_identity'
           ) OR (v_source -> 'file_size' IS NOT NULL
                 AND v_source -> 'file_size' <> 'null'::jsonb
                 AND (v_source ->> 'file_size')::bigint
                    IS DISTINCT FROM (v_source #>> '{object_identity,byte_size}')::bigint) THEN
            v_code := 'source_changed';
        ELSIF v_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb
           OR v_source -> 'object_identity' = 'null'::jsonb THEN
            CONTINUE;
        ELSE
            v_code := NULL;
        END IF;
        v_spec := aap_assets_v1._image_spec(v_job.spec);
        IF v_spec IS NULL OR v_spec IS DISTINCT FROM v_job.spec
           OR v_spec ->> 'kind' IS DISTINCT FROM v_job.kind THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_claim_invalid_response';
        END IF;
        v_policy := v_spec -> 'policy';
        IF v_code IS NOT NULL THEN
            IF v_job.kind = 'pyramid'
               AND v_job.pyramid_asset_id IS NOT NULL
               AND v_job.pyramid_generation IS NOT NULL THEN
                BEGIN
                    SELECT asset.* INTO v_asset
                    FROM public.image_pyramid_assets AS asset
                    WHERE asset.id = v_job.pyramid_asset_id
                    FOR UPDATE NOWAIT;
                EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                    v_busy := true;
                    CONTINUE;
                END;
                v_asset_present := FOUND;
                IF v_asset_present THEN
                    BEGIN
                        SELECT generation.* INTO v_generation
                        FROM public.image_pyramid_generations AS generation
                        WHERE generation.asset_id = v_job.pyramid_asset_id
                          AND generation.generation = v_job.pyramid_generation
                        FOR UPDATE NOWAIT;
                    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                        v_busy := true;
                        CONTINUE;
                    END;
                    v_generation_present := FOUND;
                END IF;
            END IF;
            v_modified := true;
            IF v_job.output_prefix IS NOT NULL AND v_job.lease_token IS NOT NULL THEN
                PERFORM aap_assets_v1._queue_image_garbage(
                    v_job.output_prefix, 'source_changed',
                    GREATEST(pg_catalog.clock_timestamp(), v_job.lease_expires_at)
                        + pg_catalog.make_interval(hours => (v_policy ->> 'gc_grace_hours')::integer)
                );
            END IF;
            IF v_generation_present THEN
                UPDATE public.image_pyramid_generations AS generation
                SET status = 'failed', error_code = 'source_changed',
                    lease_token = NULL, lease_expires_at = NULL,
                    updated_at = pg_catalog.clock_timestamp()
                WHERE generation.asset_id = v_job.pyramid_asset_id
                  AND generation.generation = v_job.pyramid_generation
                  AND generation.job_id = v_job.job_id
                  AND generation.job_generation = v_job.generation
                  AND generation.source_generation = v_job.source_generation
                  AND generation.source_snapshot = v_job.source_snapshot
                  AND generation.status IN ('pending', 'building');
                UPDATE public.image_pyramid_assets AS asset
                SET building_generation = NULL, updated_at = pg_catalog.clock_timestamp()
                WHERE asset.id = v_job.pyramid_asset_id
                  AND asset.building_generation = v_job.pyramid_generation;
            END IF;
            UPDATE public.asset_image_jobs AS job
            SET status = 'canceled', error_code = 'source_changed',
                error_detail = COALESCE(v_code, 'source_changed'),
                last_lease_token = CASE WHEN v_job.lease_token IS NOT NULL
                    THEN v_job.lease_token ELSE job.last_lease_token END,
                last_outcome = CASE WHEN v_job.lease_token IS NOT NULL
                    THEN 'source_changed' ELSE job.last_outcome END,
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                completed_at = pg_catalog.clock_timestamp(),
                updated_at = pg_catalog.clock_timestamp()
            WHERE job.job_id = v_job.job_id;
            CONTINUE;
        END IF;
        v_asset_present := false;
        v_generation_present := false;
        IF v_job.kind = 'pyramid' THEN
            IF v_job.pyramid_asset_id IS NULL OR v_job.pyramid_generation IS NULL THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_claim_invalid_response';
            END IF;
            BEGIN
                SELECT asset.* INTO v_asset
                FROM public.image_pyramid_assets AS asset
                WHERE asset.id = v_job.pyramid_asset_id
                FOR UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                v_busy := true;
                CONTINUE;
            END;
            v_asset_present := FOUND;
            IF v_asset_present THEN
                BEGIN
                    SELECT generation.* INTO v_generation
                    FROM public.image_pyramid_generations AS generation
                    WHERE generation.asset_id = v_job.pyramid_asset_id
                      AND generation.generation = v_job.pyramid_generation
                    FOR UPDATE NOWAIT;
                EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                    v_busy := true;
                    CONTINUE;
                END;
                v_generation_present := FOUND;
            END IF;
            IF v_job.lease_token IS NOT NULL AND v_job.output_prefix IS NOT NULL THEN
                v_modified := true;
                PERFORM aap_assets_v1._queue_image_garbage(
                    v_job.output_prefix,
                    CASE WHEN NOT v_asset_present OR NOT v_generation_present
                        THEN 'dependency_changed' ELSE 'attempt_abandoned' END,
                    GREATEST(pg_catalog.clock_timestamp(), v_job.lease_expires_at)
                        + pg_catalog.make_interval(hours => (v_policy ->> 'gc_grace_hours')::integer)
                );
            END IF;
            IF NOT v_asset_present OR NOT v_generation_present
               OR v_asset.building_generation IS DISTINCT FROM v_job.pyramid_generation
               OR v_generation.job_id IS DISTINCT FROM v_job.job_id
               OR v_generation.job_generation IS DISTINCT FROM v_job.generation
               OR v_generation.source_generation IS DISTINCT FROM v_job.source_generation
               OR v_generation.source_snapshot IS DISTINCT FROM v_job.source_snapshot
               OR v_generation.status NOT IN ('pending', 'building') THEN
                v_modified := true;
                IF v_generation_present AND v_generation.status IN ('pending', 'building') THEN
                    UPDATE public.image_pyramid_generations AS generation
                    SET status = 'failed', error_code = 'dependency_changed',
                        lease_token = NULL, lease_expires_at = NULL,
                        updated_at = pg_catalog.clock_timestamp()
                    WHERE generation.asset_id = v_job.pyramid_asset_id
                      AND generation.generation = v_job.pyramid_generation
                      AND generation.job_id = v_job.job_id
                      AND generation.job_generation = v_job.generation
                      AND generation.source_generation = v_job.source_generation
                      AND generation.source_snapshot = v_job.source_snapshot;
                    UPDATE public.image_pyramid_assets AS asset
                    SET building_generation = NULL, updated_at = pg_catalog.clock_timestamp()
                    WHERE asset.id = v_job.pyramid_asset_id
                      AND asset.building_generation = v_job.pyramid_generation;
                END IF;
                UPDATE public.asset_image_jobs AS job
                SET status = 'canceled', error_code = 'dependency_changed',
                    error_detail = 'pyramid generation no longer belongs to job',
                    last_lease_token = CASE WHEN v_job.lease_token IS NOT NULL
                        THEN v_job.lease_token ELSE job.last_lease_token END,
                    last_outcome = CASE WHEN v_job.lease_token IS NOT NULL
                        THEN 'dependency_changed' ELSE job.last_outcome END,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    completed_at = pg_catalog.clock_timestamp(),
                    updated_at = pg_catalog.clock_timestamp()
                WHERE job.job_id = v_job.job_id;
                CONTINUE;
            END IF;
        ELSIF v_job.lease_token IS NOT NULL AND v_job.output_prefix IS NOT NULL THEN
            v_modified := true;
            PERFORM aap_assets_v1._queue_image_garbage(
                v_job.output_prefix, 'attempt_abandoned',
                GREATEST(pg_catalog.clock_timestamp(), v_job.lease_expires_at)
                    + pg_catalog.make_interval(hours => (v_policy ->> 'gc_grace_hours')::integer)
            );
        END IF;
        IF v_job.attempt_count >= 3 THEN
            v_modified := true;
            IF v_job.kind = 'pyramid' AND v_generation_present THEN
                UPDATE public.image_pyramid_generations AS generation
                SET status = 'failed', error_code = 'timeout',
                    lease_token = NULL, lease_expires_at = NULL,
                    updated_at = pg_catalog.clock_timestamp()
                WHERE generation.asset_id = v_job.pyramid_asset_id
                  AND generation.generation = v_job.pyramid_generation;
                UPDATE public.image_pyramid_assets AS asset
                SET building_generation = NULL, updated_at = pg_catalog.clock_timestamp()
                WHERE asset.id = v_job.pyramid_asset_id
                  AND asset.building_generation = v_job.pyramid_generation;
            END IF;
            UPDATE public.asset_image_jobs AS job
            SET status = 'failed', error_code = 'timeout',
                error_detail = 'image worker attempt limit reached',
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                last_lease_token = CASE WHEN v_job.lease_token IS NOT NULL
                    THEN v_job.lease_token ELSE job.last_lease_token END,
                last_outcome = CASE WHEN v_job.lease_token IS NOT NULL
                    THEN 'failed' ELSE job.last_outcome END,
                completed_at = pg_catalog.clock_timestamp(),
                updated_at = pg_catalog.clock_timestamp()
            WHERE job.job_id = v_job.job_id;
            CONTINUE;
        END IF;
        IF v_job.kind = 'pyramid' AND v_generation_present
           AND v_generation.status = 'building' THEN
            v_modified := true;
            UPDATE public.image_pyramid_generations AS generation
            SET status = 'pending', error_code = NULL,
                lease_token = NULL, lease_expires_at = NULL, output_prefix = NULL,
                updated_at = pg_catalog.clock_timestamp()
            WHERE generation.asset_id = v_job.pyramid_asset_id
              AND generation.generation = v_job.pyramid_generation;
            UPDATE public.image_pyramid_assets AS asset
            SET building_generation = v_job.pyramid_generation,
                updated_at = pg_catalog.clock_timestamp()
            WHERE asset.id = v_job.pyramid_asset_id;
        END IF;
        v_token := pg_catalog.gen_random_uuid();
        v_lease_seconds := GREATEST(
            p_lease_seconds, (v_policy ->> 'lease_seconds')::integer
        );
        v_expires := pg_catalog.clock_timestamp()
            + pg_catalog.make_interval(secs => v_lease_seconds);
        v_prefix := CASE v_job.kind
            WHEN 'thumbnail' THEN 'image-assets/' || v_job.job_id::text
                || '/g' || v_job.generation::text || '/a' || v_token::text || '/'
            WHEN 'pyramid' THEN 'image-pyramids/' || v_job.pyramid_asset_id::text
                || '/g' || v_job.pyramid_generation::text || '/a' || v_token::text || '/'
            ELSE NULL
        END;
        IF v_job.kind = 'pyramid' THEN
            v_modified := true;
            UPDATE public.image_pyramid_generations AS generation
            SET status = 'building', lease_token = v_token,
                lease_expires_at = v_expires, output_prefix = v_prefix,
                error_code = NULL, updated_at = pg_catalog.clock_timestamp()
            WHERE generation.asset_id = v_job.pyramid_asset_id
              AND generation.generation = v_job.pyramid_generation;
            UPDATE public.image_pyramid_assets AS asset
            SET building_generation = v_job.pyramid_generation,
                updated_at = pg_catalog.clock_timestamp()
            WHERE asset.id = v_job.pyramid_asset_id;
        END IF;
        UPDATE public.asset_image_jobs AS job
        SET status = 'running', lease_owner = p_worker_id,
            lease_token = v_token, lease_expires_at = v_expires,
            output_prefix = v_prefix, attempt_count = job.attempt_count + 1,
            progress = CASE
                WHEN job.kind = 'image_backfill' THEN job.progress - 'pending_batch'
                WHEN job.kind = 'thumbnail'
                 AND pg_catalog.jsonb_typeof(job.progress -> 'retained_publication') = 'object'
                    THEN pg_catalog.jsonb_build_object(
                        'retained_publication', job.progress -> 'retained_publication'
                    )
                ELSE '{}'::jsonb
            END,
            last_lease_token = NULL, last_outcome = NULL,
            completed_at = NULL, updated_at = pg_catalog.clock_timestamp()
        WHERE job.job_id = v_job.job_id
        RETURNING job.* INTO v_job;
        v_claim := pg_catalog.jsonb_build_object(
            'job_id', v_job.job_id,
            'generation', v_job.generation,
            'lease_token', v_token,
            'lease_expires_at', v_expires,
            'owner', CASE WHEN v_job.owner_id IS NULL THEN NULL
                ELSE pg_catalog.jsonb_build_object('kind', v_job.owner_kind, 'id', v_job.owner_id) END,
            'source', v_source,
            'spec', v_job.spec,
            'output_prefix', v_prefix,
            'pyramid_asset_id', v_job.pyramid_asset_id,
            'pyramid_generation', v_job.pyramid_generation
        );
        RETURN aap_assets_v1._image_reply(
            pg_catalog.jsonb_build_object('claim', v_claim), NULL
        );
    END LOOP;
    IF v_busy AND v_modified THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_claim_busy';
    ELSIF v_busy THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    END IF;
    RETURN aap_assets_v1._image_reply(
        pg_catalog.jsonb_build_object('claim', NULL), NULL
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    WHEN SQLSTATE 'P0001' THEN
        IF SQLERRM = 'image_claim_busy' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        END IF;
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.claim_video_media(
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
          AND aap_assets_v1.fixture_native_claim_allowed('media',job.job_id,pg_catalog.pg_backend_pid())
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
