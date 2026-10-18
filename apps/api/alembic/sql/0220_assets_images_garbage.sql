-- Keep the existing cleanup row/bool ABI and retain responsibility for late PUTs.
ALTER TABLE public.asset_storage_deletions
    ADD COLUMN retained boolean NOT NULL DEFAULT false;
-- aap-statement-break
DROP INDEX public.ix_asset_storage_deletions_pending_bucket_created;
-- aap-statement-break
CREATE INDEX ix_asset_storage_deletions_pending_bucket_created
    ON public.asset_storage_deletions(bucket, created_at, operation_id, asset_id)
    INCLUDE (not_before) WHERE completed_at IS NULL OR retained;
-- aap-statement-break
DROP INDEX public.ix_asset_storage_deletions_pending_created;
-- aap-statement-break
CREATE INDEX ix_asset_storage_deletions_pending_created
    ON public.asset_storage_deletions(created_at, operation_id, asset_id)
    WHERE completed_at IS NULL OR retained;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.pending_storage_deletions_v2(
    p_bucket text, p_after_created_at timestamptz,
    p_after_operation uuid, p_after_asset uuid, p_limit integer
)
RETURNS TABLE(operation_id uuid, asset_id uuid, bucket text, prefix text, created_at timestamptz)
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_bucket IS NULL OR pg_catalog.char_length(p_bucket) NOT BETWEEN 3 AND 63
       OR p_bucket !~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$'
       OR p_bucket ~ '^[0-9]+(\.[0-9]+){3}$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid storage deletion bucket', DETAIL = '{"code":"invalid_request"}';
    END IF;
    IF NOT (
        (p_after_created_at IS NULL AND p_after_operation IS NULL AND p_after_asset IS NULL)
        OR (p_after_created_at IS NOT NULL AND p_after_operation IS NOT NULL AND p_after_asset IS NOT NULL)
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid storage deletion cursor', DETAIL = '{"code":"invalid_cursor"}';
    END IF;
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid storage deletion page limit', DETAIL = '{"code":"invalid_request"}';
    END IF;
    RETURN QUERY SELECT deletion.operation_id, deletion.asset_id,
        deletion.bucket::text, deletion.prefix::text, deletion.created_at
    FROM public.asset_storage_deletions AS deletion
    WHERE (deletion.completed_at IS NULL OR deletion.retained)
      AND deletion.bucket = p_bucket
      AND deletion.not_before <= pg_catalog.statement_timestamp()
      AND (p_after_created_at IS NULL OR
           (deletion.created_at, deletion.operation_id, deletion.asset_id)
                > (p_after_created_at, p_after_operation, p_after_asset))
    ORDER BY deletion.created_at, deletion.operation_id, deletion.asset_id
    LIMIT p_limit;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.pending_storage_deletions_v2(text, timestamptz, uuid, uuid, integer) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.complete_storage_deletion(p_operation_id uuid, p_asset_id uuid)
RETURNS boolean
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_updated bigint;
BEGIN
    IF p_operation_id IS NULL OR p_asset_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid storage deletion completion request', DETAIL = '{"code":"invalid_request"}';
    END IF;
    UPDATE public.asset_storage_deletions AS deletion
    SET completed_at = CASE WHEN deletion.retained THEN pg_catalog.clock_timestamp()
                            ELSE COALESCE(deletion.completed_at, pg_catalog.now()) END,
        not_before = CASE WHEN deletion.retained
            THEN pg_catalog.clock_timestamp() + interval '5 minutes' ELSE deletion.not_before END
    WHERE deletion.operation_id = p_operation_id AND deletion.asset_id = p_asset_id
      AND deletion.not_before <= pg_catalog.statement_timestamp();
    GET DIAGNOSTICS v_updated = ROW_COUNT;
    IF v_updated > 0 THEN RETURN true; END IF;
    RETURN EXISTS (
        SELECT 1 FROM public.asset_storage_deletions AS deletion
        WHERE deletion.operation_id = p_operation_id AND deletion.asset_id = p_asset_id
          AND deletion.completed_at IS NOT NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.complete_storage_deletion(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._image_prefix_referenced(p_prefix text)
RETURNS boolean
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT aap_assets_v1._image_garbage_prefix_valid(p_prefix) THEN RETURN true; END IF;
    IF EXISTS (
        SELECT 1 FROM public.dataset_items AS item
        WHERE pg_catalog.left(item.thumbnail_path, pg_catalog.char_length(p_prefix)) = p_prefix
    ) OR aap_labeling_v1.image_thumbnail_references(p_prefix) THEN RETURN true; END IF;
    IF EXISTS (
        SELECT 1 FROM public.image_pyramid_assets AS asset
        WHERE p_prefix = 'image-pyramids/' || asset.id::text || '/'
    ) THEN RETURN true; END IF;
    IF EXISTS (
        SELECT 1 FROM public.image_pyramid_generations AS generation
        JOIN public.image_pyramid_assets AS asset ON asset.id = generation.asset_id
        WHERE (
            generation.generation IN (asset.active_generation, asset.building_generation)
            OR generation.lease_expires_at > pg_catalog.statement_timestamp()
        ) AND (
            p_prefix = 'image-pyramids/' || generation.asset_id::text || '/g' || generation.generation::text || '/'
            OR p_prefix = generation.output_prefix
            OR pg_catalog.left('image-pyramids/' || generation.asset_id::text || '/g' || generation.generation::text || '/', pg_catalog.char_length(p_prefix)) = p_prefix
        )
    ) THEN RETURN true; END IF;
    RETURN EXISTS (
        SELECT 1 FROM public.asset_image_jobs AS job
        WHERE job.status = 'running' AND job.lease_expires_at > pg_catalog.statement_timestamp()
          AND job.output_prefix IS NOT NULL
          AND (pg_catalog.left(job.output_prefix, pg_catalog.char_length(p_prefix)) = p_prefix
               OR pg_catalog.left(p_prefix, pg_catalog.char_length(job.output_prefix)) = job.output_prefix)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_prefix_referenced(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.claim_image_garbage(
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
        WHERE garbage.not_before <= pg_catalog.statement_timestamp()
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
REVOKE ALL ON FUNCTION aap_assets_v1.claim_image_garbage(uuid, integer, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.complete_image_garbage(p_garbage_id uuid, p_lease_token uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_garbage public.asset_image_garbage%ROWTYPE;
    v_completed boolean := false;
BEGIN
    IF p_garbage_id IS NULL OR p_lease_token IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    SELECT garbage.* INTO v_garbage FROM public.asset_image_garbage AS garbage
    WHERE garbage.garbage_id = p_garbage_id FOR UPDATE NOWAIT;
    IF FOUND THEN
        IF v_garbage.last_ack_token = p_lease_token THEN
            v_completed := true;
        ELSIF v_garbage.lease_token = p_lease_token
           AND v_garbage.lease_expires_at > pg_catalog.clock_timestamp()
           AND NOT aap_assets_v1._image_prefix_referenced(v_garbage.prefix) THEN
            UPDATE public.asset_image_garbage AS garbage
            SET completed_at = pg_catalog.clock_timestamp(),
                not_before = pg_catalog.clock_timestamp() + interval '5 minutes',
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                last_ack_token = p_lease_token
            WHERE garbage.garbage_id = p_garbage_id;
            v_completed := true;
        END IF;
    END IF;
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
        'garbage_id', p_garbage_id, 'completed', v_completed
    ), NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.complete_image_garbage(uuid, uuid) FROM PUBLIC;
