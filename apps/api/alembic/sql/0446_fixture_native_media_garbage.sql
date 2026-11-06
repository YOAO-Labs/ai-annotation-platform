-- Ordinary collectors must not claim a fixture-born native output scope.
CREATE FUNCTION aap_assets_v1.fixture_native_cache_scope_owned(p_prefix text,p_keys jsonb)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    WITH requested AS (
        SELECT p_prefix AS value WHERE p_prefix IS NOT NULL
        UNION ALL
        SELECT value FROM pg_catalog.jsonb_array_elements_text(
            CASE WHEN pg_catalog.jsonb_typeof(p_keys)='array' THEN p_keys ELSE '[]'::jsonb END
        ) AS key(value)
    ), parsed AS (
        SELECT pg_catalog.split_part(value,'/',1) AS family,
            CASE WHEN pg_catalog.split_part(value,'/',2) ~
              '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN pg_catalog.split_part(value,'/',2)::uuid ELSE NULL END AS owner_id
        FROM requested
    )
    SELECT EXISTS (
        SELECT 1 FROM parsed WHERE family IN ('image-assets','image-pyramids','media-assets','videos')
          AND (owner_id IS NULL
            OR (family='image-assets' AND EXISTS (
                SELECT 1 FROM aap_assets_v1.fixture_native_media_jobs AS job
                WHERE job.family='image' AND job.job_id=parsed.owner_id))
            OR (family='media-assets' AND EXISTS (
                SELECT 1 FROM aap_assets_v1.fixture_native_media_jobs AS job
                WHERE job.family='media' AND job.job_id=parsed.owner_id))
            OR (family='image-pyramids' AND EXISTS (
                SELECT 1 FROM aap_assets_v1.fixture_media_births AS birth
                WHERE birth.owner_kind='pyramid_asset' AND birth.owner_id=parsed.owner_id))
            OR (family='videos' AND EXISTS (
                SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS item
                WHERE item.kind='item' AND item.resource_id=parsed.owner_id)))
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_native_cache_scope_owned(text,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.claim_image_garbage(
    p_worker_id uuid, p_lease_seconds integer, p_batch_size integer
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_garbage public.asset_image_garbage%ROWTYPE;
    v_items jsonb := '[]'::jsonb;
    v_token uuid;
BEGIN
    IF p_worker_id IS NULL OR p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 30 AND 3600
       OR p_batch_size IS NULL OR p_batch_size NOT BETWEEN 1 AND 100 THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    FOR v_garbage IN
        SELECT garbage.* FROM public.asset_image_garbage AS garbage
        WHERE NOT aap_assets_v1.fixture_native_cache_scope_owned(garbage.prefix,'[]'::jsonb)
          AND garbage.not_before <= pg_catalog.statement_timestamp()
          AND (garbage.lease_expires_at IS NULL OR garbage.lease_expires_at <= pg_catalog.statement_timestamp())
        ORDER BY garbage.not_before, garbage.garbage_id
        LIMIT p_batch_size FOR UPDATE SKIP LOCKED
    LOOP
        IF aap_assets_v1._image_prefix_referenced(v_garbage.prefix) THEN
            UPDATE public.asset_image_garbage AS garbage
            SET not_before = pg_catalog.clock_timestamp() + interval '5 minutes',
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
            WHERE garbage.garbage_id = v_garbage.garbage_id;
            CONTINUE;
        END IF;
        v_token := pg_catalog.gen_random_uuid();
        UPDATE public.asset_image_garbage AS garbage
        SET lease_owner = p_worker_id, lease_token = v_token,
            lease_expires_at = pg_catalog.clock_timestamp() + pg_catalog.make_interval(secs => p_lease_seconds)
        WHERE garbage.garbage_id = v_garbage.garbage_id;
        v_items := v_items || pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object(
            'garbage_id', v_garbage.garbage_id, 'prefix', v_garbage.prefix, 'lease_token', v_token
        ));
    END LOOP;
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object('items', v_items), NULL);
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.claim_video_media_garbage(
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
          AND NOT aap_assets_v1.fixture_native_cache_scope_owned(garbage.prefix,garbage.keys)
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
