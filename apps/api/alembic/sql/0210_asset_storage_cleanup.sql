-- Bucket-scoped keyset paging lets a cleanup client continue after failed rows.
CREATE INDEX ix_asset_storage_deletions_pending_bucket_created
ON public.asset_storage_deletions (bucket, created_at, operation_id, asset_id)
INCLUDE (not_before)
WHERE completed_at IS NULL;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.pending_storage_deletions_v2(
    p_bucket text,
    p_after_created_at timestamptz,
    p_after_operation uuid,
    p_after_asset uuid,
    p_limit integer
)
RETURNS TABLE(
    operation_id uuid,
    asset_id uuid,
    bucket text,
    prefix text,
    created_at timestamptz
)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_bucket IS NULL
       OR pg_catalog.char_length(p_bucket) NOT BETWEEN 3 AND 63
       OR p_bucket !~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$'
       OR p_bucket ~ '^[0-9]+(\.[0-9]+){3}$' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid storage deletion bucket',
            DETAIL = '{"code":"invalid_request"}';
    END IF;

    IF NOT (
        (p_after_created_at IS NULL AND p_after_operation IS NULL AND p_after_asset IS NULL)
        OR (p_after_created_at IS NOT NULL AND p_after_operation IS NOT NULL AND p_after_asset IS NOT NULL)
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid storage deletion cursor',
            DETAIL = '{"code":"invalid_cursor"}';
    END IF;

    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 1000 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid storage deletion page limit',
            DETAIL = '{"code":"invalid_request"}';
    END IF;

    IF p_after_created_at IS NULL THEN
        RETURN QUERY
        SELECT
            deletion.operation_id,
            deletion.asset_id,
            deletion.bucket::text,
            deletion.prefix::text,
            deletion.created_at
        FROM public.asset_storage_deletions AS deletion
        WHERE deletion.completed_at IS NULL
          AND deletion.bucket = p_bucket
          AND deletion.not_before <= pg_catalog.statement_timestamp()
        ORDER BY deletion.created_at, deletion.operation_id, deletion.asset_id
        LIMIT p_limit;
    ELSE
        RETURN QUERY
        SELECT
            deletion.operation_id,
            deletion.asset_id,
            deletion.bucket::text,
            deletion.prefix::text,
            deletion.created_at
        FROM public.asset_storage_deletions AS deletion
        WHERE deletion.completed_at IS NULL
          AND deletion.bucket = p_bucket
          AND deletion.not_before <= pg_catalog.statement_timestamp()
          AND (deletion.created_at, deletion.operation_id, deletion.asset_id)
              > (p_after_created_at, p_after_operation, p_after_asset)
        ORDER BY deletion.created_at, deletion.operation_id, deletion.asset_id
        LIMIT p_limit;
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.pending_storage_deletions_v2(
    text, timestamptz, uuid, uuid, integer
) FROM PUBLIC;
