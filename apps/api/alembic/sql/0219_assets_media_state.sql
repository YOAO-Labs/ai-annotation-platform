-- Assets owns durable video work state and its media-specific source fence.
ALTER TABLE public.dataset_items
    ADD COLUMN media_source_generation bigint NOT NULL DEFAULT 1,
    ADD COLUMN media_timetable_revision bigint NOT NULL DEFAULT 0,
    ADD COLUMN media_materialized_generation bigint NOT NULL DEFAULT 0,
    ADD COLUMN media_object_identity jsonb,
    ADD COLUMN media_source_drifted boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT ck_dataset_items_media_source_generation
        CHECK (media_source_generation > 0),
    ADD CONSTRAINT ck_dataset_items_media_timetable_revision
        CHECK (media_timetable_revision >= 0),
    ADD CONSTRAINT ck_dataset_items_media_materialized_generation
        CHECK (media_materialized_generation >= 0),
    ADD CONSTRAINT ck_dataset_items_media_object_identity
        CHECK (media_object_identity IS NULL OR jsonb_typeof(media_object_identity) = 'object');
-- aap-statement-break
ALTER TABLE public.video_frame_cache
    ADD COLUMN source_generation bigint NOT NULL DEFAULT 0,
    ADD COLUMN timetable_revision bigint NOT NULL DEFAULT 0,
    ADD CONSTRAINT ck_video_frame_cache_source_generation CHECK (source_generation >= 0),
    ADD CONSTRAINT ck_video_frame_cache_timetable_revision CHECK (timetable_revision >= 0);
-- aap-statement-break
ALTER TABLE public.video_chunks
    ADD COLUMN source_generation bigint NOT NULL DEFAULT 0,
    ADD COLUMN timetable_revision bigint NOT NULL DEFAULT 0,
    ADD CONSTRAINT ck_video_chunks_source_generation CHECK (source_generation >= 0),
    ADD CONSTRAINT ck_video_chunks_timetable_revision CHECK (timetable_revision >= 0);
-- aap-statement-break
CREATE TABLE public.asset_media_jobs (
    job_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
    kind text NOT NULL CHECK (kind IN (
        'video_metadata', 'video_timetable', 'frame', 'chunk', 'video_backfill'
    )),
    dataset_item_id uuid,
    slot_key text NOT NULL CHECK (pg_catalog.char_length(slot_key) BETWEEN 1 AND 255),
    generation bigint NOT NULL DEFAULT 1 CHECK (generation >= 1),
    spec jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(spec) = 'object'),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'ready', 'failed', 'canceled')),
    source_generation bigint NOT NULL DEFAULT 0 CHECK (source_generation >= 0),
    timetable_revision bigint NOT NULL DEFAULT 0 CHECK (timetable_revision >= 0),
    actor_id uuid,
    progress jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (pg_catalog.jsonb_typeof(progress) = 'object'),
    error_code text,
    error_detail text,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_token uuid,
    lease_expires_at timestamptz,
    last_lease_token uuid,
    last_outcome text CHECK (last_outcome IN (
        'published', 'failed', 'lease_lost', 'source_changed', 'dependency_changed'
    )),
    accepted_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    completed_at timestamptz,
    backfill_selection jsonb,
    backfill_cutoff timestamptz,
    backfill_cursor uuid,
    CONSTRAINT ck_asset_media_jobs_lease_pair CHECK (
        (lease_token IS NULL) = (lease_expires_at IS NULL)
    ),
    CONSTRAINT ck_asset_media_jobs_last_outcome_pair CHECK (
        (last_lease_token IS NULL) = (last_outcome IS NULL)
    ),
    CONSTRAINT ck_asset_media_jobs_backfill_scope CHECK (
        (kind = 'video_backfill' AND dataset_item_id IS NULL
         AND backfill_selection IS NOT NULL
         AND pg_catalog.jsonb_typeof(backfill_selection) = 'object'
         AND backfill_cutoff IS NOT NULL)
        OR
        (kind <> 'video_backfill' AND dataset_item_id IS NOT NULL
         AND backfill_selection IS NULL AND backfill_cutoff IS NULL
         AND backfill_cursor IS NULL)
    )
);
-- aap-statement-break
CREATE UNIQUE INDEX uq_asset_media_jobs_item_slot
    ON public.asset_media_jobs(dataset_item_id, kind, slot_key)
    WHERE dataset_item_id IS NOT NULL;
-- aap-statement-break
CREATE UNIQUE INDEX uq_asset_media_jobs_backfill_slot
    ON public.asset_media_jobs(slot_key)
    WHERE dataset_item_id IS NULL;
-- aap-statement-break
CREATE INDEX ix_asset_media_jobs_claim
    ON public.asset_media_jobs(status, lease_expires_at, accepted_at);
-- aap-statement-break
CREATE INDEX ix_asset_media_jobs_item_status
    ON public.asset_media_jobs(dataset_item_id, status);
-- aap-statement-break
REVOKE ALL ON TABLE public.asset_media_jobs FROM PUBLIC;
-- aap-statement-break
COMMENT ON TABLE public.asset_media_jobs IS
    'Assets-owned retained video work slots and expiring worker leases; no live source foreign keys.';
-- aap-statement-break
CREATE TABLE public.asset_media_operations (
    operation_id uuid PRIMARY KEY,
    actor_id uuid,
    action text NOT NULL CHECK (action IN ('request', 'retry', 'cancel')),
    scope jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(scope) = 'object'),
    intent jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(intent) = 'object'),
    result jsonb NOT NULL CHECK (
        pg_catalog.jsonb_typeof(result) = 'object'
        AND result ->> 'schema' = 'aap-assets-media/v1'
        AND result -> 'ok' = 'true'::jsonb
    ),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now()
);
-- aap-statement-break
REVOKE ALL ON TABLE public.asset_media_operations FROM PUBLIC;
-- aap-statement-break
COMMENT ON TABLE public.asset_media_operations IS
    'Assets-owned immutable successful video acceptance/cancel receipts; no live source foreign keys.';
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_operation_immutable()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'media operation receipts are immutable';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_operation_immutable() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_asset_media_operations_immutable
BEFORE UPDATE OR DELETE ON public.asset_media_operations
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._media_operation_immutable();
-- aap-statement-break
CREATE TABLE public.asset_media_timetable_staging (
    job_id uuid NOT NULL REFERENCES public.asset_media_jobs(job_id) ON DELETE CASCADE,
    generation bigint NOT NULL CHECK (generation >= 1),
    frame_index integer NOT NULL CHECK (frame_index >= 0),
    lease_token uuid NOT NULL,
    pts_ms bigint NOT NULL,
    is_keyframe boolean NOT NULL,
    pict_type varchar(8),
    byte_offset bigint CHECK (byte_offset IS NULL OR byte_offset >= 0),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    PRIMARY KEY (job_id, generation, frame_index)
);
-- aap-statement-break
CREATE INDEX ix_asset_media_timetable_staging_lease
    ON public.asset_media_timetable_staging(job_id, generation, lease_token);
-- aap-statement-break
REVOKE ALL ON TABLE public.asset_media_timetable_staging FROM PUBLIC;
-- aap-statement-break
COMMENT ON TABLE public.asset_media_timetable_staging IS
    'Bounded, attempt-token-fenced staging for atomic video timetable publication.';
-- aap-statement-break
CREATE TABLE public.asset_media_garbage (
    garbage_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
    prefix text,
    keys jsonb NOT NULL DEFAULT '[]'::jsonb,
    reason text NOT NULL CHECK (reason IN (
        'source_changed', 'dependency_changed', 'cache_expired',
        'missing_cache', 'attempt_abandoned'
    )),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    not_before timestamptz NOT NULL DEFAULT pg_catalog.now(),
    lease_token uuid,
    lease_expires_at timestamptz,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    completed_at timestamptz,
    last_ack_token uuid,
    CONSTRAINT ck_asset_media_garbage_scope CHECK (
        (prefix IS NOT NULL AND pg_catalog.jsonb_typeof(keys) = 'array'
         AND pg_catalog.jsonb_array_length(keys) = 0)
        OR
        (prefix IS NULL AND pg_catalog.jsonb_typeof(keys) = 'array'
         AND pg_catalog.jsonb_array_length(keys) BETWEEN 1 AND 500)
    ),
    CONSTRAINT ck_asset_media_garbage_lease_pair CHECK (
        (lease_token IS NULL) = (lease_expires_at IS NULL)
    )
);
-- aap-statement-break
CREATE INDEX ix_asset_media_garbage_pending
    ON public.asset_media_garbage(not_before, created_at, garbage_id, lease_expires_at)
    WHERE completed_at IS NULL;
-- aap-statement-break
CREATE UNIQUE INDEX uq_asset_media_garbage_attempt_prefix
    ON public.asset_media_garbage(prefix)
    WHERE prefix LIKE 'media-assets/%';
-- aap-statement-break
REVOKE ALL ON TABLE public.asset_media_garbage FROM PUBLIC;
-- aap-statement-break
COMMENT ON TABLE public.asset_media_garbage IS
    'Assets-owned immutable exact object deletion scopes with mutable claim/ack state; never source keys.';
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_garbage_immutable_scope()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'media garbage scopes are retained';
    END IF;
    IF NEW.garbage_id IS DISTINCT FROM OLD.garbage_id
       OR NEW.prefix IS DISTINCT FROM OLD.prefix
       OR NEW.keys IS DISTINCT FROM OLD.keys
       OR NEW.reason IS DISTINCT FROM OLD.reason
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'media garbage scope is immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_garbage_immutable_scope() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_asset_media_garbage_immutable_scope
BEFORE UPDATE OR DELETE ON public.asset_media_garbage
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._media_garbage_immutable_scope();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_queue_prefix(p_prefix text, p_reason text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_prefix IS NULL
       OR p_prefix !~ '^media-assets/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g[1-9][0-9]*/a[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/$'
       OR p_reason IS NULL
       OR p_reason NOT IN (
            'source_changed', 'dependency_changed', 'cache_expired',
            'missing_cache', 'attempt_abandoned'
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid media attempt prefix';
    END IF;
    INSERT INTO public.asset_media_garbage(prefix, keys, reason, not_before)
    VALUES (p_prefix, '[]'::jsonb, p_reason, pg_catalog.clock_timestamp())
    ON CONFLICT (prefix) WHERE prefix LIKE 'media-assets/%'
    DO UPDATE SET not_before = pg_catalog.clock_timestamp(),
                  lease_token = NULL,
                  lease_expires_at = NULL,
                  last_ack_token = NULL,
                  completed_at = NULL;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_queue_prefix(text, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_reply(p_data jsonb, p_code text)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_code IS NOT NULL AND p_code NOT IN (
        'invalid_request', 'invalid_response', 'storage_unavailable',
        'account_not_active', 'account_busy', 'invalid_account_role',
        'source_missing', 'source_changed', 'source_busy', 'task_not_visible',
        'task_busy', 'permission_denied', 'metadata_not_ready',
        'invalid_frame_range', 'asset_missing', 'operation_conflict',
        'operation_busy', 'lease_lost', 'dependency_changed',
        'resource_budget_exceeded', 'object_missing'
    ) THEN
        p_code := 'invalid_response';
    END IF;
    IF p_code IS NOT NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-assets-media/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', p_code)
        );
    END IF;
    IF p_data IS NULL OR pg_catalog.jsonb_typeof(p_data) IS DISTINCT FROM 'object' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-assets-media/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_response')
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-media/v1', 'ok', true, 'data', p_data
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_reply(jsonb, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_source_snapshot(p_item public.dataset_items)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.encode(
        pg_catalog.sha256(pg_catalog.convert_to(pg_catalog.jsonb_build_object(
            'dataset_item_id', (p_item).id,
            'dataset_id', (p_item).dataset_id,
            'file_name', (p_item).file_name,
            'file_path', (p_item).file_path,
            'file_type', (p_item).file_type,
            'file_size', (p_item).file_size,
            'content_hash', (p_item).content_hash,
            'source_generation', (p_item).media_source_generation,
            'source_drifted', (p_item).media_source_drifted
        )::text, 'UTF8')),
        'hex'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_source_snapshot(public.dataset_items) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_source_json(p_item public.dataset_items)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'dataset_item_id', (p_item).id,
        'dataset_id', (p_item).dataset_id,
        'source_generation', (p_item).media_source_generation,
        'source_snapshot', aap_assets_v1._media_source_snapshot(p_item),
        'timetable_revision', (p_item).media_timetable_revision,
        'materialized_generation', (p_item).media_materialized_generation,
        'object_identity', (p_item).media_object_identity,
        'source_drifted', (p_item).media_source_drifted,
        'file_name', (p_item).file_name,
        'file_path', (p_item).file_path,
        'file_type', (p_item).file_type,
        'file_size', (p_item).file_size,
        'content_hash', (p_item).content_hash,
        'width', (p_item).width,
        'height', (p_item).height,
        'thumbnail_path', (p_item).thumbnail_path,
        'blurhash', (p_item).blurhash,
        'video_metadata', CASE
            WHEN pg_catalog.jsonb_typeof((p_item).metadata -> 'video') = 'object'
                THEN (p_item).metadata -> 'video'
            ELSE '{}'::jsonb
        END
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_source_json(public.dataset_items) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_source_generation_changed()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_keys text[];
    v_key text;
    v_video jsonb;
    v_job record;
BEGIN
    IF NEW.file_path IS NOT DISTINCT FROM OLD.file_path
       AND NEW.file_type IS NOT DISTINCT FROM OLD.file_type
       AND NEW.file_size IS NOT DISTINCT FROM OLD.file_size
       AND NEW.content_hash IS NOT DISTINCT FROM OLD.content_hash THEN
        RETURN NEW;
    END IF;
    IF OLD.media_source_generation = 9223372036854775807 THEN
        RAISE EXCEPTION USING ERRCODE = '22003', MESSAGE = 'media source generation exhausted';
    END IF;

    SELECT pg_catalog.array_agg(DISTINCT candidate.object_key)
    INTO v_keys
    FROM (
        SELECT OLD.metadata -> 'video' ->> 'poster_frame_path' AS object_key
        UNION ALL
        SELECT OLD.metadata -> 'video' ->> 'playback_path'
        UNION ALL
        SELECT CASE WHEN OLD.file_type = 'video' THEN OLD.thumbnail_path END
        UNION ALL
        SELECT cache.storage_key
        FROM public.video_frame_cache AS cache
        WHERE cache.dataset_item_id = OLD.id
        UNION ALL
        SELECT chunk.storage_key
        FROM public.video_chunks AS chunk
        WHERE chunk.dataset_item_id = OLD.id
    ) AS candidate
    WHERE candidate.object_key IS NOT NULL
      AND (candidate.object_key LIKE 'videos/%'
           OR candidate.object_key LIKE 'media-assets/%');

    IF v_keys IS NOT NULL THEN
        FOREACH v_key IN ARRAY v_keys LOOP
            INSERT INTO public.asset_media_garbage(prefix, keys, reason)
            VALUES (NULL, pg_catalog.jsonb_build_array(v_key), 'source_changed');
        END LOOP;
    END IF;

    FOR v_job IN
        SELECT job.job_id, job.generation, job.lease_token
        FROM public.asset_media_jobs AS job
        WHERE job.dataset_item_id = OLD.id
          AND job.status = 'running'
          AND job.lease_token IS NOT NULL
    LOOP
        PERFORM aap_assets_v1._media_queue_prefix(
            'media-assets/' || v_job.job_id::text || '/g'
                || v_job.generation::text || '/a' || v_job.lease_token::text || '/',
            'source_changed'
        );
    END LOOP;

    UPDATE public.asset_media_jobs AS job
    SET status = 'canceled',
        lease_token = NULL,
        lease_expires_at = NULL,
        last_lease_token = NULL,
        last_outcome = NULL,
        completed_at = pg_catalog.clock_timestamp(),
        updated_at = pg_catalog.clock_timestamp()
    WHERE job.dataset_item_id = OLD.id
      AND job.status IN ('pending', 'running');
    DELETE FROM public.asset_media_timetable_staging AS staging
    WHERE staging.job_id IN (
        SELECT job.job_id FROM public.asset_media_jobs AS job
        WHERE job.dataset_item_id = OLD.id AND job.status = 'canceled'
    );

    UPDATE public.video_frame_cache AS cache
    SET status = 'pending',
        storage_key = NULL,
        byte_size = NULL,
        error = NULL,
        source_generation = OLD.media_source_generation + 1,
        timetable_revision = 0,
        last_accessed_at = NULL,
        updated_at = pg_catalog.clock_timestamp()
    WHERE cache.dataset_item_id = OLD.id;
    UPDATE public.video_chunks AS chunk
    SET status = 'pending',
        storage_key = NULL,
        byte_size = NULL,
        error = NULL,
        generation_mode = NULL,
        diagnostics = '{}'::jsonb,
        source_generation = OLD.media_source_generation + 1,
        timetable_revision = 0,
        last_accessed_at = NULL,
        updated_at = pg_catalog.clock_timestamp()
    WHERE chunk.dataset_item_id = OLD.id;
    DELETE FROM public.video_frame_indices AS frame
    WHERE frame.dataset_item_id = OLD.id;

    NEW.media_source_generation := OLD.media_source_generation + 1;
    NEW.media_timetable_revision := 0;
    NEW.media_materialized_generation := 0;
    NEW.media_object_identity := NULL;
    NEW.media_source_drifted := false;

    IF pg_catalog.jsonb_typeof(NEW.metadata) = 'object' THEN
        v_video := NEW.metadata -> 'video';
        IF pg_catalog.jsonb_typeof(v_video) = 'object' THEN
            v_video := v_video - ARRAY[
                'duration_ms', 'fps', 'frame_count', 'width', 'height', 'codec',
                'playback_path', 'playback_codec', 'playback_error',
                'poster_frame_path', 'probe_error', 'poster_error',
                'frame_timetable_frame_count', 'frame_timetable_error'
            ];
            IF v_video = '{}'::jsonb THEN
                NEW.metadata := NEW.metadata - 'video';
            ELSE
                NEW.metadata := pg_catalog.jsonb_set(NEW.metadata, '{video}', v_video, true);
            END IF;
        END IF;
    END IF;
    IF OLD.file_type = 'video'
       AND (OLD.thumbnail_path LIKE 'videos/%'
            OR OLD.thumbnail_path LIKE 'media-assets/%') THEN
        NEW.thumbnail_path := NULL;
        NEW.blurhash := NULL;
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_source_generation_changed() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_dataset_items_media_source_generation
BEFORE UPDATE OF file_path, file_type, file_size, content_hash
ON public.dataset_items
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._media_source_generation_changed();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_source_deleted()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_keys text[];
    v_key text;
    v_job record;
BEGIN
    SELECT pg_catalog.array_agg(DISTINCT candidate.object_key)
    INTO v_keys
    FROM (
        SELECT OLD.metadata -> 'video' ->> 'poster_frame_path' AS object_key
        UNION ALL
        SELECT OLD.metadata -> 'video' ->> 'playback_path'
        UNION ALL
        SELECT CASE WHEN OLD.file_type = 'video' THEN OLD.thumbnail_path END
        UNION ALL
        SELECT cache.storage_key
        FROM public.video_frame_cache AS cache
        WHERE cache.dataset_item_id = OLD.id
        UNION ALL
        SELECT chunk.storage_key
        FROM public.video_chunks AS chunk
        WHERE chunk.dataset_item_id = OLD.id
    ) AS candidate
    WHERE candidate.object_key IS NOT NULL
      AND (candidate.object_key LIKE 'videos/%'
           OR candidate.object_key LIKE 'media-assets/%');
    IF v_keys IS NOT NULL THEN
        FOREACH v_key IN ARRAY v_keys LOOP
            INSERT INTO public.asset_media_garbage(prefix, keys, reason)
            VALUES (NULL, pg_catalog.jsonb_build_array(v_key), 'source_changed');
        END LOOP;
    END IF;
    FOR v_job IN
        SELECT job.job_id, job.generation, job.lease_token
        FROM public.asset_media_jobs AS job
        WHERE job.dataset_item_id = OLD.id
          AND job.status = 'running'
          AND job.lease_token IS NOT NULL
    LOOP
        PERFORM aap_assets_v1._media_queue_prefix(
            'media-assets/' || v_job.job_id::text || '/g'
                || v_job.generation::text || '/a' || v_job.lease_token::text || '/',
            'source_changed'
        );
    END LOOP;
    UPDATE public.asset_media_jobs AS job
    SET status = 'canceled', lease_token = NULL, lease_expires_at = NULL,
        last_lease_token = NULL, last_outcome = NULL,
        completed_at = pg_catalog.clock_timestamp(),
        updated_at = pg_catalog.clock_timestamp()
    WHERE job.dataset_item_id = OLD.id
      AND job.status IN ('pending', 'running');
    DELETE FROM public.asset_media_timetable_staging AS staging
    WHERE staging.job_id IN (
        SELECT job.job_id FROM public.asset_media_jobs AS job
        WHERE job.dataset_item_id = OLD.id AND job.status = 'canceled'
    );
    RETURN OLD;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_source_deleted() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_dataset_items_media_source_deleted
BEFORE DELETE ON public.dataset_items
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._media_source_deleted();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_work_reference(p_job public.asset_media_jobs)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'job_id', (p_job).job_id,
        'generation', (p_job).generation,
        'dataset_item_id', (p_job).dataset_item_id,
        'source_generation', (p_job).source_generation,
        'kind', (p_job).kind,
        'status', (p_job).status
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_work_reference(public.asset_media_jobs) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_work_spec_valid(p_spec jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_kind text;
    v_selection jsonb;
    v_item_count integer;
    v_selector_count integer;
BEGIN
    IF pg_catalog.jsonb_typeof(p_spec) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_spec -> 'kind') IS DISTINCT FROM 'string' THEN
        RETURN false;
    END IF;
    v_kind := p_spec ->> 'kind';
    IF v_kind = 'video_metadata' THEN
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_object_keys(p_spec) AS key(name)
            WHERE key.name <> ALL (ARRAY[
                'kind', 'generate_playback', 'generate_poster',
                'probe_timetable', 'expected_source_snapshot'
            ])
        ) OR (p_spec ? 'generate_playback' AND
              pg_catalog.jsonb_typeof(p_spec -> 'generate_playback') <> 'boolean')
          OR (p_spec ? 'generate_poster' AND
              pg_catalog.jsonb_typeof(p_spec -> 'generate_poster') <> 'boolean')
          OR (p_spec ? 'probe_timetable' AND
              pg_catalog.jsonb_typeof(p_spec -> 'probe_timetable') <> 'boolean') THEN
            RETURN false;
        END IF;
    ELSIF v_kind = 'video_timetable' THEN
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_object_keys(p_spec) AS key(name)
            WHERE key.name <> ALL (ARRAY[
                'kind', 'sparse_stride', 'expected_source_snapshot'
            ])
        ) OR (p_spec ? 'sparse_stride' AND (
            pg_catalog.jsonb_typeof(p_spec -> 'sparse_stride') <> 'number'
            OR p_spec ->> 'sparse_stride' !~ '^[1-9][0-9]*$'
            OR (p_spec ->> 'sparse_stride')::numeric NOT BETWEEN 1 AND 1000000
        )) THEN
            RETURN false;
        END IF;
    ELSIF v_kind = 'frame' THEN
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_object_keys(p_spec) AS key(name)
            WHERE key.name <> ALL (ARRAY[
                'kind', 'frame_index', 'width', 'format', 'expected_source_snapshot'
            ])
        ) OR NOT (p_spec ? 'frame_index')
          OR pg_catalog.jsonb_typeof(p_spec -> 'frame_index') <> 'number'
          OR p_spec ->> 'frame_index' !~ '^(0|[1-9][0-9]*)$'
          OR (p_spec ->> 'frame_index')::numeric > 2147483647
          OR (p_spec ? 'width' AND (
              pg_catalog.jsonb_typeof(p_spec -> 'width') <> 'number'
              OR p_spec ->> 'width' !~ '^[1-9][0-9]*$'
              OR (p_spec ->> 'width')::numeric NOT BETWEEN 1 AND 4096
          ))
          OR (p_spec ? 'format' AND (
              pg_catalog.jsonb_typeof(p_spec -> 'format') <> 'string'
              OR p_spec ->> 'format' NOT IN ('webp', 'jpeg')
          )) THEN
            RETURN false;
        END IF;
    ELSIF v_kind = 'chunk' THEN
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_object_keys(p_spec) AS key(name)
            WHERE key.name <> ALL (ARRAY[
                'kind', 'chunk_id', 'chunk_size_frames', 'expected_source_snapshot'
            ])
        ) OR NOT (p_spec ? 'chunk_id')
          OR pg_catalog.jsonb_typeof(p_spec -> 'chunk_id') <> 'number'
          OR p_spec ->> 'chunk_id' !~ '^(0|[1-9][0-9]*)$'
          OR (p_spec ->> 'chunk_id')::numeric > 2147483647
          OR (p_spec ? 'chunk_size_frames' AND (
              pg_catalog.jsonb_typeof(p_spec -> 'chunk_size_frames') <> 'number'
              OR p_spec ->> 'chunk_size_frames' !~ '^[1-9][0-9]*$'
              OR (p_spec ->> 'chunk_size_frames')::numeric NOT BETWEEN 1 AND 1000000
          )) THEN
            RETURN false;
        END IF;
    ELSIF v_kind = 'video_backfill' THEN
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_object_keys(p_spec) AS key(name)
            WHERE key.name <> ALL (ARRAY['kind', 'selection'])
        ) OR pg_catalog.jsonb_typeof(p_spec -> 'selection') IS DISTINCT FROM 'object' THEN
            RETURN false;
        END IF;
        v_selection := p_spec -> 'selection';
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_object_keys(v_selection) AS key(name)
            WHERE key.name <> ALL (ARRAY[
                'dataset_id', 'dataset_item_ids', 'all_items',
                'only_missing', 'accepted_before'
            ])
        ) OR (v_selection ? 'dataset_id' AND
              pg_catalog.jsonb_typeof(v_selection -> 'dataset_id') NOT IN ('string', 'null'))
          OR (v_selection ? 'dataset_item_ids' AND
              pg_catalog.jsonb_typeof(v_selection -> 'dataset_item_ids') <> 'array')
          OR (v_selection ? 'all_items' AND
              pg_catalog.jsonb_typeof(v_selection -> 'all_items') <> 'boolean')
          OR (v_selection ? 'only_missing' AND
              pg_catalog.jsonb_typeof(v_selection -> 'only_missing') <> 'boolean')
          OR (v_selection ? 'accepted_before' AND
              pg_catalog.jsonb_typeof(v_selection -> 'accepted_before') NOT IN ('string', 'null')) THEN
            RETURN false;
        END IF;
        v_item_count := COALESCE(pg_catalog.jsonb_array_length(v_selection -> 'dataset_item_ids'), 0);
        IF v_item_count > 500 THEN
            RETURN false;
        END IF;
        IF v_item_count > 0 AND EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(v_selection -> 'dataset_item_ids') AS item(value)
            WHERE pg_catalog.jsonb_typeof(item.value) <> 'string'
               OR item.value #>> '{}' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        ) THEN
            RETURN false;
        END IF;
        IF v_item_count > 0 AND (
            SELECT pg_catalog.count(DISTINCT item.value #>> '{}')
            FROM pg_catalog.jsonb_array_elements(v_selection -> 'dataset_item_ids') AS item(value)
        ) <> v_item_count THEN
            RETURN false;
        END IF;
        v_selector_count :=
            CASE WHEN v_selection ? 'dataset_id'
                       AND v_selection -> 'dataset_id' <> 'null'::jsonb THEN 1 ELSE 0 END
            + CASE WHEN v_item_count > 0 THEN 1 ELSE 0 END
            + CASE WHEN COALESCE(v_selection -> 'all_items' = 'true'::jsonb, false)
                   THEN 1 ELSE 0 END;
        IF v_selector_count <> 1 THEN
            RETURN false;
        END IF;
        IF v_selection ? 'dataset_id' AND v_selection ->> 'dataset_id' IS NOT NULL THEN
            BEGIN
                PERFORM (v_selection ->> 'dataset_id')::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RETURN false;
            END;
        END IF;
        IF v_selection ? 'accepted_before' AND v_selection ->> 'accepted_before' IS NOT NULL THEN
            BEGIN
                PERFORM (v_selection ->> 'accepted_before')::timestamptz;
            EXCEPTION WHEN invalid_text_representation OR datetime_field_overflow THEN
                RETURN false;
            END;
        END IF;
    ELSE
        RETURN false;
    END IF;
    IF p_spec ? 'expected_source_snapshot' THEN
        IF p_spec -> 'expected_source_snapshot' <> 'null'::jsonb
           AND (pg_catalog.jsonb_typeof(p_spec -> 'expected_source_snapshot') <> 'string'
                OR p_spec ->> 'expected_source_snapshot' !~ '^[0-9a-f]{64}$') THEN
            RETURN false;
        END IF;
    END IF;
    RETURN true;
EXCEPTION
    WHEN numeric_value_out_of_range OR invalid_text_representation
       OR datetime_field_overflow THEN
        RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_work_spec_valid(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_object_identity_valid(p_identity jsonb)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT COALESCE(pg_catalog.jsonb_typeof(p_identity) = 'object'
       AND NOT EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_object_keys(p_identity) AS key(name)
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
       ), false)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_object_identity_valid(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_reconcile_source(
    p_dataset_item_id uuid,
    p_expected_snapshot text,
    p_object_identity jsonb,
    p_observed_sha256 text
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
    v_item public.dataset_items%ROWTYPE;
    v_key text;
    v_keys text[];
    v_job record;
    v_drift boolean := false;
    v_video jsonb;
BEGIN
    IF p_dataset_item_id IS NULL
       OR ((p_expected_snapshot IS NULL) <> (p_object_identity IS NULL))
       OR (p_expected_snapshot IS NOT NULL
           AND p_expected_snapshot !~ '^[0-9a-f]{64}$')
       OR (p_object_identity IS NOT NULL
           AND NOT aap_assets_v1._media_object_identity_valid(p_object_identity))
       OR (p_object_identity IS NULL AND p_observed_sha256 IS NOT NULL)
       OR (p_observed_sha256 IS NOT NULL
           AND p_observed_sha256 !~ '^[0-9a-f]{64}$') THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT item.* INTO v_item
        FROM public.dataset_items AS item
        WHERE item.id = p_dataset_item_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_busy');
    END;
    IF NOT FOUND OR v_item.file_type <> 'video' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_missing');
    END IF;
    IF p_expected_snapshot IS NULL THEN
        RETURN aap_assets_v1._media_reply(aap_assets_v1._media_source_json(v_item), NULL);
    END IF;
    IF aap_assets_v1._media_source_snapshot(v_item) IS DISTINCT FROM p_expected_snapshot THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
    END IF;
    IF v_item.media_source_drifted THEN
        RETURN aap_assets_v1._media_reply(aap_assets_v1._media_source_json(v_item), NULL);
    END IF;
    v_drift := (
        v_item.file_size IS NOT NULL
        AND v_item.file_size <> (p_object_identity ->> 'byte_size')::bigint
    ) OR (
        v_item.media_object_identity IS NOT NULL
        AND v_item.media_object_identity IS DISTINCT FROM p_object_identity
    ) OR (
        p_observed_sha256 IS NOT NULL
        AND v_item.content_hash IS NOT NULL
        AND v_item.content_hash ~* '^[0-9a-f]{64}$'
        AND pg_catalog.lower(v_item.content_hash) IS DISTINCT FROM p_observed_sha256
    );

    IF v_drift THEN
        IF v_item.media_source_generation = 9223372036854775807 THEN
            RAISE EXCEPTION USING ERRCODE = '22003', MESSAGE = 'media source generation exhausted';
        END IF;
        SELECT pg_catalog.array_agg(DISTINCT candidate.object_key)
        INTO v_keys
        FROM (
            SELECT v_item.metadata -> 'video' ->> 'poster_frame_path' AS object_key
            UNION ALL
            SELECT v_item.metadata -> 'video' ->> 'playback_path'
            UNION ALL
            SELECT CASE WHEN v_item.file_type = 'video' THEN v_item.thumbnail_path END
            UNION ALL
            SELECT cache.storage_key
            FROM public.video_frame_cache AS cache
            WHERE cache.dataset_item_id = v_item.id
            UNION ALL
            SELECT chunk.storage_key
            FROM public.video_chunks AS chunk
            WHERE chunk.dataset_item_id = v_item.id
        ) AS candidate
        WHERE candidate.object_key IS NOT NULL
          AND (candidate.object_key LIKE 'videos/%'
               OR candidate.object_key LIKE 'media-assets/%');
        IF v_keys IS NOT NULL THEN
            FOREACH v_key IN ARRAY v_keys LOOP
                INSERT INTO public.asset_media_garbage(prefix, keys, reason)
                VALUES (NULL, pg_catalog.jsonb_build_array(v_key), 'source_changed');
            END LOOP;
        END IF;
        FOR v_job IN
            SELECT job.job_id, job.generation, job.lease_token
            FROM public.asset_media_jobs AS job
            WHERE job.dataset_item_id = v_item.id
              AND job.status = 'running'
              AND job.lease_token IS NOT NULL
        LOOP
            PERFORM aap_assets_v1._media_queue_prefix(
                'media-assets/' || v_job.job_id::text || '/g'
                    || v_job.generation::text || '/a' || v_job.lease_token::text || '/',
                'source_changed'
            );
        END LOOP;
        UPDATE public.asset_media_jobs AS job
        SET status = 'canceled',
            lease_token = NULL,
            lease_expires_at = NULL,
            last_lease_token = NULL,
            last_outcome = NULL,
            completed_at = pg_catalog.clock_timestamp(),
            updated_at = pg_catalog.clock_timestamp()
        WHERE job.dataset_item_id = v_item.id
          AND job.status IN ('pending', 'running');
        DELETE FROM public.asset_media_timetable_staging AS staging
        WHERE staging.job_id IN (
            SELECT job.job_id FROM public.asset_media_jobs AS job
            WHERE job.dataset_item_id = v_item.id AND job.status = 'canceled'
        );
        UPDATE public.video_frame_cache AS cache
        SET status = 'pending', storage_key = NULL, byte_size = NULL, error = NULL,
            source_generation = v_item.media_source_generation + 1,
            timetable_revision = 0, last_accessed_at = NULL,
            updated_at = pg_catalog.clock_timestamp()
        WHERE cache.dataset_item_id = v_item.id;
        UPDATE public.video_chunks AS chunk
        SET status = 'pending', storage_key = NULL, byte_size = NULL, error = NULL,
            generation_mode = NULL, diagnostics = '{}'::jsonb,
            source_generation = v_item.media_source_generation + 1,
            timetable_revision = 0, last_accessed_at = NULL,
            updated_at = pg_catalog.clock_timestamp()
        WHERE chunk.dataset_item_id = v_item.id;
        DELETE FROM public.video_frame_indices AS frame
        WHERE frame.dataset_item_id = v_item.id;
        v_video := v_item.metadata -> 'video';
        IF pg_catalog.jsonb_typeof(v_video) = 'object' THEN
            v_video := v_video - ARRAY[
                'duration_ms', 'fps', 'frame_count', 'width', 'height', 'codec',
                'playback_path', 'playback_codec', 'playback_error',
                'poster_frame_path', 'probe_error', 'poster_error',
                'frame_timetable_frame_count', 'frame_timetable_error'
            ];
            IF v_video = '{}'::jsonb THEN
                v_item.metadata := v_item.metadata - 'video';
            ELSE
                v_item.metadata := pg_catalog.jsonb_set(v_item.metadata, '{video}', v_video, true);
            END IF;
        END IF;
        UPDATE public.dataset_items AS item
        SET media_source_generation = v_item.media_source_generation + 1,
            media_timetable_revision = 0,
            media_materialized_generation = 0,
            media_object_identity = p_object_identity,
            media_source_drifted = true,
            metadata = v_item.metadata,
            thumbnail_path = CASE
                WHEN item.file_type = 'video'
                 AND (item.thumbnail_path LIKE 'videos/%'
                      OR item.thumbnail_path LIKE 'media-assets/%') THEN NULL
                ELSE item.thumbnail_path
            END,
            blurhash = CASE
                WHEN item.file_type = 'video'
                 AND (item.thumbnail_path LIKE 'videos/%'
                      OR item.thumbnail_path LIKE 'media-assets/%') THEN NULL
                ELSE item.blurhash
            END,
            updated_at = pg_catalog.clock_timestamp()
        WHERE item.id = v_item.id
        RETURNING item.* INTO v_item;
        RETURN aap_assets_v1._media_reply(aap_assets_v1._media_source_json(v_item), NULL);
    END IF;

    IF v_item.media_object_identity IS NULL THEN
        UPDATE public.dataset_items AS item
        SET media_object_identity = p_object_identity,
            updated_at = pg_catalog.clock_timestamp()
        WHERE item.id = v_item.id
        RETURNING item.* INTO v_item;
    END IF;
    RETURN aap_assets_v1._media_reply(aap_assets_v1._media_source_json(v_item), NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_reconcile_source(uuid, text, jsonb, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_frame_json(p_row public.video_frame_cache)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_item public.dataset_items%ROWTYPE;
    v_job public.asset_media_jobs%ROWTYPE;
    v_slot text;
    v_status text;
BEGIN
    SELECT item.* INTO v_item
    FROM public.dataset_items AS item
    WHERE item.id = (p_row).dataset_item_id;
    v_slot := 'frame:' || (p_row).frame_index::text || ':'
        || (p_row).width::text || ':' || (p_row).format;
    SELECT job.* INTO v_job
    FROM public.asset_media_jobs AS job
    WHERE job.dataset_item_id = (p_row).dataset_item_id
      AND job.kind = 'frame'
      AND job.slot_key = v_slot;
    v_status := (p_row).status;
    IF (p_row).source_generation = 0 OR v_item.id IS NULL
       OR COALESCE(v_item.media_source_drifted, true)
       OR (p_row).source_generation <> v_item.media_source_generation
       OR (p_row).timetable_revision <> v_item.media_timetable_revision THEN
        v_status := 'pending';
    END IF;
    IF v_status = 'running' THEN
        v_status := 'pending';
    END IF;
    IF v_status NOT IN ('pending', 'ready', 'failed')
       OR (v_status = 'ready' AND (p_row).storage_key IS NULL) THEN
        v_status := 'pending';
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'row_id', (p_row).id,
        'dataset_item_id', (p_row).dataset_item_id,
        'kind', 'frame',
        'source_generation', COALESCE(NULLIF((p_row).source_generation, 0), v_item.media_source_generation, 1),
        'timetable_revision', (p_row).timetable_revision,
        'status', v_status,
        'storage_key', CASE WHEN v_status = 'ready' THEN (p_row).storage_key END,
        'byte_size', CASE WHEN v_status = 'ready' THEN (p_row).byte_size END,
        'error', CASE WHEN v_status = 'failed' THEN (p_row).error END,
        'job_id', CASE WHEN v_job.job_id IS NOT NULL THEN v_job.job_id END,
        'generation', COALESCE(v_job.generation, 0),
        'frame_index', (p_row).frame_index,
        'width', (p_row).width,
        'format', (p_row).format,
        'diagnostics', '{}'::jsonb,
        'updated_at', (p_row).updated_at
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_frame_json(public.video_frame_cache) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_chunk_json(p_row public.video_chunks)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_item public.dataset_items%ROWTYPE;
    v_job public.asset_media_jobs%ROWTYPE;
    v_slot text;
    v_status text;
    v_diagnostics jsonb;
BEGIN
    SELECT item.* INTO v_item
    FROM public.dataset_items AS item
    WHERE item.id = (p_row).dataset_item_id;
    v_slot := 'chunk:' || (p_row).chunk_id::text;
    SELECT job.* INTO v_job
    FROM public.asset_media_jobs AS job
    WHERE job.dataset_item_id = (p_row).dataset_item_id
      AND job.kind = 'chunk'
      AND job.slot_key = v_slot;
    v_status := (p_row).status;
    IF (p_row).source_generation = 0 OR v_item.id IS NULL
       OR COALESCE(v_item.media_source_drifted, true)
       OR (p_row).source_generation <> v_item.media_source_generation
       OR (p_row).timetable_revision <> v_item.media_timetable_revision THEN
        v_status := 'pending';
    END IF;
    IF v_status = 'running' THEN
        v_status := 'pending';
    END IF;
    IF v_status NOT IN ('pending', 'ready', 'failed')
       OR (v_status = 'ready' AND (p_row).storage_key IS NULL) THEN
        v_status := 'pending';
    END IF;
    v_diagnostics := pg_catalog.jsonb_build_object(
        'source_codec', (p_row).diagnostics -> 'source_codec',
        'output_codec', (p_row).diagnostics -> 'output_codec',
        'keyframe_aligned', (p_row).diagnostics -> 'keyframe_aligned',
        'start_byte_offset', (p_row).diagnostics -> 'start_byte_offset',
        'end_byte_offset', (p_row).diagnostics -> 'end_byte_offset',
        'smart_copy_eligible', (p_row).diagnostics -> 'smart_copy_eligible',
        'fallback_reason', (p_row).diagnostics -> 'fallback_reason'
    );
    RETURN pg_catalog.jsonb_build_object(
        'row_id', (p_row).id,
        'dataset_item_id', (p_row).dataset_item_id,
        'kind', 'chunk',
        'source_generation', COALESCE(NULLIF((p_row).source_generation, 0), v_item.media_source_generation, 1),
        'timetable_revision', (p_row).timetable_revision,
        'status', v_status,
        'storage_key', CASE WHEN v_status = 'ready' THEN (p_row).storage_key END,
        'byte_size', CASE WHEN v_status = 'ready' THEN (p_row).byte_size END,
        'error', CASE WHEN v_status = 'failed' THEN (p_row).error END,
        'job_id', CASE WHEN v_job.job_id IS NOT NULL THEN v_job.job_id END,
        'generation', COALESCE(v_job.generation, 0),
        'chunk_id', (p_row).chunk_id,
        'start_frame', (p_row).start_frame,
        'end_frame', (p_row).end_frame,
        'start_pts_ms', (p_row).start_pts_ms,
        'end_pts_ms', (p_row).end_pts_ms,
        'generation_mode', CASE
            WHEN (p_row).generation_mode IN ('smart_copy', 'transcode')
                THEN (p_row).generation_mode
        END,
        'diagnostics', v_diagnostics,
        'updated_at', (p_row).updated_at
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_chunk_json(public.video_chunks) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_operation_lock(p_operation_id uuid)
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_operation_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'media operation id required';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-assets-media:' || p_operation_id::text, 0)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_operation_lock(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_read_operation(
    p_operation_id uuid,
    p_actor_id uuid,
    p_action text,
    p_scope jsonb,
    p_intent jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_saved public.asset_media_operations%ROWTYPE;
BEGIN
    PERFORM aap_assets_v1._media_operation_lock(p_operation_id);
    SELECT operation.* INTO v_saved
    FROM public.asset_media_operations AS operation
    WHERE operation.operation_id = p_operation_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF v_saved.actor_id IS DISTINCT FROM p_actor_id
       OR v_saved.action IS DISTINCT FROM p_action
       OR v_saved.scope IS DISTINCT FROM p_scope
       OR v_saved.intent IS DISTINCT FROM p_intent THEN
        RETURN aap_assets_v1._media_reply(NULL, 'operation_conflict');
    END IF;
    IF v_saved.result ->> 'schema' IS DISTINCT FROM 'aap-assets-media/v1'
       OR v_saved.result -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_saved.result -> 'data') IS DISTINCT FROM 'object' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    RETURN v_saved.result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_read_operation(uuid, uuid, text, jsonb, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_enqueue(
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
#variable_conflict use_variable
DECLARE
    v_kind text;
    v_action text;
    v_scope jsonb;
    v_intent jsonb;
    v_saved jsonb;
    v_spec jsonb;
    v_item public.dataset_items%ROWTYPE;
    v_job public.asset_media_jobs%ROWTYPE;
    v_source jsonb;
    v_slot_key text;
    v_cutoff timestamptz;
    v_operation_id uuid := p_operation_id;
    v_work jsonb;
BEGIN
    IF p_force IS NULL OR NOT aap_assets_v1._media_work_spec_valid(p_spec) THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    v_kind := p_spec ->> 'kind';
    IF (v_kind = 'video_backfill') <> (p_dataset_item_id IS NULL)
       OR (v_kind = 'video_backfill' AND p_force) THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    v_spec := CASE v_kind
        WHEN 'video_metadata' THEN
            pg_catalog.jsonb_build_object(
                'kind', v_kind, 'generate_playback', true,
                'generate_poster', true, 'probe_timetable', true
            ) || p_spec
        WHEN 'video_timetable' THEN
            pg_catalog.jsonb_build_object('kind', v_kind, 'sparse_stride', 1) || p_spec
        WHEN 'frame' THEN
            pg_catalog.jsonb_build_object(
                'kind', v_kind, 'frame_index', p_spec -> 'frame_index',
                'width', 512, 'format', 'webp'
            ) || p_spec
        WHEN 'chunk' THEN
            pg_catalog.jsonb_build_object(
                'kind', v_kind, 'chunk_id', p_spec -> 'chunk_id',
                'chunk_size_frames', 60
            ) || p_spec
        ELSE
            pg_catalog.jsonb_build_object(
                'kind', v_kind,
                'selection', pg_catalog.jsonb_build_object(
                    'dataset_id', NULL,
                    'dataset_item_ids', '[]'::jsonb,
                    'all_items', false,
                    'only_missing', true,
                    'accepted_before', NULL
                ) || (p_spec -> 'selection')
            )
    END;
    v_action := CASE WHEN p_force THEN 'retry' ELSE 'request' END;
    IF v_kind = 'video_backfill' THEN
        v_scope := pg_catalog.jsonb_build_object(
            'dataset_id', v_spec #> '{selection,dataset_id}',
            'dataset_item_ids', v_spec #> '{selection,dataset_item_ids}',
            'all_items', v_spec #> '{selection,all_items}'
        );
    ELSE
        v_scope := pg_catalog.jsonb_build_object('dataset_item_id', p_dataset_item_id);
    END IF;
    v_intent := pg_catalog.jsonb_build_object(
        'dataset_item_id', p_dataset_item_id,
        'spec', v_spec,
        'force', p_force
    );
    IF p_operation_id IS NOT NULL THEN
        v_saved := aap_assets_v1._media_read_operation(
            p_operation_id, p_actor_id, v_action, v_scope, v_intent
        );
        IF v_saved IS NOT NULL THEN
            RETURN v_saved;
        END IF;
    END IF;

    IF v_kind = 'video_backfill' THEN
        v_cutoff := pg_catalog.clock_timestamp();
        v_spec := pg_catalog.jsonb_set(
            v_spec,
            '{selection,accepted_before}',
            pg_catalog.to_jsonb(v_cutoff),
            true
        );
        v_slot_key := 'backfill:' || COALESCE(p_operation_id, pg_catalog.gen_random_uuid())::text;
    ELSE
        BEGIN
            SELECT item.* INTO v_item
            FROM public.dataset_items AS item
            WHERE item.id = p_dataset_item_id
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._media_reply(NULL, 'source_busy');
        END;
        IF NOT FOUND OR v_item.file_type <> 'video' THEN
            RETURN aap_assets_v1._media_reply(NULL, 'source_missing');
        END IF;
        IF v_item.media_source_drifted THEN
            RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
        END IF;
        IF v_spec ? 'expected_source_snapshot'
           AND v_spec -> 'expected_source_snapshot' <> 'null'::jsonb
           AND v_spec ->> 'expected_source_snapshot'
                IS DISTINCT FROM aap_assets_v1._media_source_snapshot(v_item) THEN
            RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
        END IF;
        v_source := aap_assets_v1._media_source_json(v_item);
        v_slot_key := CASE v_kind
            WHEN 'video_metadata' THEN 'video_metadata'
            WHEN 'video_timetable' THEN 'video_timetable'
            WHEN 'frame' THEN 'frame:' || (v_spec ->> 'frame_index') || ':'
                || (v_spec ->> 'width') || ':' || (v_spec ->> 'format')
            WHEN 'chunk' THEN 'chunk:' || (v_spec ->> 'chunk_id')
        END;
    END IF;

    BEGIN
        SELECT job.* INTO v_job
        FROM public.asset_media_jobs AS job
        WHERE job.dataset_item_id IS NOT DISTINCT FROM p_dataset_item_id
          AND job.kind = v_kind
          AND job.slot_key = v_slot_key
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._media_reply(NULL, 'operation_busy');
    END;
    IF NOT FOUND THEN
        INSERT INTO public.asset_media_jobs (
            kind, dataset_item_id, slot_key, generation, spec, status,
            source_generation, timetable_revision, actor_id,
            backfill_selection, backfill_cutoff
        ) VALUES (
            v_kind,
            p_dataset_item_id,
            v_slot_key,
            1,
            v_spec,
            'pending',
            COALESCE(v_item.media_source_generation, 0),
            COALESCE(v_item.media_timetable_revision, 0),
            p_actor_id,
            CASE WHEN v_kind = 'video_backfill' THEN v_spec -> 'selection' END,
            v_cutoff
        ) RETURNING * INTO v_job;
    ELSE
        IF v_job.source_generation IS DISTINCT FROM COALESCE(v_item.media_source_generation, 0)
           OR (v_kind IN ('frame', 'chunk')
               AND v_job.timetable_revision IS DISTINCT FROM v_item.media_timetable_revision)
           OR v_job.spec IS DISTINCT FROM v_spec
           OR p_force
           OR v_job.status = 'canceled'
           OR (v_kind = 'video_metadata'
               AND v_job.status IN ('ready', 'failed')
               AND COALESCE(v_item.media_materialized_generation, 0)
                   < COALESCE(v_item.media_source_generation, 0)) THEN
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
            DELETE FROM public.asset_media_timetable_staging AS staging
            WHERE staging.job_id = v_job.job_id;
            UPDATE public.asset_media_jobs AS job
            SET generation = v_job.generation + 1,
                spec = v_spec,
                status = 'pending',
                source_generation = COALESCE(v_item.media_source_generation, 0),
                timetable_revision = COALESCE(v_item.media_timetable_revision, 0),
                actor_id = p_actor_id,
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
                completed_at = NULL,
                backfill_selection = CASE
                    WHEN v_kind = 'video_backfill' THEN v_spec -> 'selection'
                END,
                backfill_cutoff = v_cutoff,
                backfill_cursor = NULL
            WHERE job.job_id = v_job.job_id
            RETURNING job.* INTO v_job;
        END IF;
    END IF;

    v_work := aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object(
            'operation_id', v_operation_id,
            'source', v_source,
            'work', aap_assets_v1._media_work_reference(v_job),
            'spec', v_spec
        ),
        NULL
    );
    IF p_operation_id IS NOT NULL THEN
        RETURN aap_assets_v1._media_write_operation(
            p_operation_id, p_actor_id, v_action, v_scope, v_intent, v_work
        );
    END IF;
    RETURN v_work;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_enqueue(uuid, uuid, uuid, jsonb, boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_write_operation(
    p_operation_id uuid,
    p_actor_id uuid,
    p_action text,
    p_scope jsonb,
    p_intent jsonb,
    p_result jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_operation_id IS NULL OR p_action IS NULL
       OR p_action NOT IN ('request', 'retry', 'cancel')
       OR pg_catalog.jsonb_typeof(p_scope) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_intent) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_result) IS DISTINCT FROM 'object'
       OR p_result ->> 'schema' IS DISTINCT FROM 'aap-assets-media/v1'
       OR p_result -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid media receipt';
    END IF;
    INSERT INTO public.asset_media_operations (
        operation_id, actor_id, action, scope, intent, result
    ) VALUES (
        p_operation_id, p_actor_id, p_action, p_scope, p_intent, p_result
    );
    RETURN p_result;
EXCEPTION
    WHEN unique_violation THEN
        RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'media receipt collision';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_write_operation(uuid, uuid, text, jsonb, jsonb, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._media_backfill_snapshot(
    p_selection jsonb,
    p_accepted_before timestamptz
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_item public.dataset_items%ROWTYPE;
    v_selection jsonb;
    v_spec jsonb;
    v_hash bytea;
    v_total bigint := 0;
BEGIN
    IF p_accepted_before IS NULL THEN
        RETURN NULL;
    END IF;
    v_spec := pg_catalog.jsonb_build_object('kind', 'video_backfill', 'selection', p_selection);
    IF NOT aap_assets_v1._media_work_spec_valid(v_spec) THEN
        RETURN NULL;
    END IF;
    IF p_selection ? 'accepted_before'
       AND p_selection ->> 'accepted_before' IS NOT NULL
       AND (p_selection ->> 'accepted_before')::timestamptz IS DISTINCT FROM p_accepted_before THEN
        RETURN NULL;
    END IF;
    v_selection := pg_catalog.jsonb_build_object(
        'dataset_id', COALESCE(p_selection -> 'dataset_id', 'null'::jsonb),
        'dataset_item_ids', COALESCE(p_selection -> 'dataset_item_ids', '[]'::jsonb),
        'all_items', COALESCE(p_selection -> 'all_items', 'false'::jsonb),
        'only_missing', COALESCE(p_selection -> 'only_missing', 'true'::jsonb)
    );
    v_hash := pg_catalog.sha256(pg_catalog.convert_to(
        'aap-assets-media-backfill/v1:' || v_selection::text || ':'
            || p_accepted_before::text,
        'UTF8'
    ));
    FOR v_item IN
        SELECT item.*
        FROM public.dataset_items AS item
        WHERE item.file_type = 'video'
          AND item.created_at <= p_accepted_before
          AND (
              (v_selection -> 'dataset_id' <> 'null'::jsonb
               AND item.dataset_id = (v_selection ->> 'dataset_id')::uuid)
              OR EXISTS (
                  SELECT 1
                  FROM pg_catalog.jsonb_array_elements_text(
                      v_selection -> 'dataset_item_ids'
                  ) AS selected(item_id)
                  WHERE selected.item_id::uuid = item.id
              )
              OR v_selection -> 'all_items' = 'true'::jsonb
          )
        ORDER BY item.id
    LOOP
        v_hash := pg_catalog.sha256(
            v_hash || pg_catalog.convert_to(
                v_item.id::text || ':' || aap_assets_v1._media_source_snapshot(v_item),
                'UTF8'
            )
        );
        v_total := v_total + 1;
    END LOOP;
    RETURN pg_catalog.jsonb_build_object(
        'snapshot_digest', pg_catalog.encode(v_hash, 'hex'),
        'total', v_total
    );
EXCEPTION
    WHEN invalid_text_representation OR datetime_field_overflow THEN
        RETURN NULL;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._media_backfill_snapshot(jsonb, timestamptz) FROM PUBLIC;
