-- Dataset unlink Assets and Diagnostics owner capabilities.
-- The caller owns the transaction; no function commits or performs storage IO.
CREATE TABLE public.asset_storage_deletions (
    operation_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    bucket varchar(63) NOT NULL,
    prefix text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    completed_at timestamptz NULL,
    CONSTRAINT pk_asset_storage_deletions PRIMARY KEY (operation_id, asset_id),
    CONSTRAINT ck_asset_storage_deletions_bucket CHECK (
        pg_catalog.char_length(bucket) BETWEEN 3 AND 63
        AND bucket ~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$'
        AND bucket !~ '^[0-9]+(\.[0-9]+){3}$'
    ),
    CONSTRAINT ck_asset_storage_deletions_prefix CHECK (
        prefix ~ '^image-pyramids/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/$'
    )
);
-- aap-statement-break
REVOKE ALL ON TABLE public.asset_storage_deletions FROM PUBLIC;
-- aap-statement-break
CREATE INDEX ix_asset_storage_deletions_pending_created
ON public.asset_storage_deletions (created_at, operation_id, asset_id)
WHERE completed_at IS NULL;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.dataset_item_references(
    p_dataset_id uuid,
    p_item_id uuid
)
RETURNS TABLE(item_id uuid)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_dataset_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid dataset item reference request',
            DETAIL = '{"code":"invalid_request"}';
    END IF;

    RETURN QUERY
    SELECT item.id
    FROM public.dataset_items AS item
    WHERE item.dataset_id = p_dataset_id
      AND (p_item_id IS NULL OR item.id = p_item_id)
    ORDER BY item.id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.dataset_item_references(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.dataset_scene_references(p_dataset_id uuid)
RETURNS TABLE(scene_id uuid)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_dataset_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid dataset scene reference request',
            DETAIL = '{"code":"invalid_request"}';
    END IF;

    RETURN QUERY
    SELECT scene.id
    FROM public.scenes AS scene
    WHERE scene.dataset_id = p_dataset_id
    ORDER BY scene.id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.dataset_scene_references(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.remove_dataset_association(
    p_dataset_id uuid,
    p_project_id uuid,
    p_association_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_deleted bigint;
BEGIN
    IF p_dataset_id IS NULL OR p_project_id IS NULL OR p_association_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid dataset association removal request',
            DETAIL = '{"code":"invalid_request"}';
    END IF;

    DELETE FROM public.project_datasets AS association
    WHERE association.id = p_association_id
      AND association.dataset_id = p_dataset_id
      AND association.project_id = p_project_id;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN v_deleted > 0;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.remove_dataset_association(uuid, uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.prepare_dataset_unlink_cleanup(
    p_operation_id uuid,
    p_dataset_id uuid,
    p_project_id uuid,
    p_bucket text
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_asset_count bigint := 0;
    v_conflicting_count bigint := 0;
BEGIN
    IF p_operation_id IS NULL OR p_dataset_id IS NULL OR p_project_id IS NULL
       OR p_bucket IS NULL
       OR pg_catalog.char_length(p_bucket) NOT BETWEEN 3 AND 63
       OR p_bucket !~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$'
       OR p_bucket ~ '^[0-9]+(\.[0-9]+){3}$' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid dataset unlink cleanup request',
            DETAIL = '{"code":"invalid_request"}';
    END IF;

    -- Publication holds Asset before its Task projection; do not wait in reverse.
    PERFORM asset.id
    FROM public.image_pyramid_assets AS asset
    WHERE asset.task_id IN (
        SELECT reference.task_id
        FROM aap_labeling_v1.dataset_task_references(p_dataset_id, p_project_id) AS reference
    )
    ORDER BY asset.id
    FOR UPDATE NOWAIT;

    -- Reconciliation can hold Generation before Asset; fence the cascade too.
    PERFORM generation.asset_id
    FROM public.image_pyramid_generations AS generation
    JOIN public.image_pyramid_assets AS asset ON asset.id = generation.asset_id
    WHERE asset.task_id IN (
        SELECT reference.task_id
        FROM aap_labeling_v1.dataset_task_references(p_dataset_id, p_project_id) AS reference
    )
    ORDER BY generation.asset_id, generation.generation
    FOR UPDATE OF generation NOWAIT;

    WITH candidate_assets AS MATERIALIZED (
        SELECT asset.id AS asset_id
        FROM public.image_pyramid_assets AS asset
        WHERE asset.task_id IN (
            SELECT reference.task_id
            FROM aap_labeling_v1.dataset_task_references(p_dataset_id, p_project_id) AS reference
        )
    ),
    inserted AS (
        INSERT INTO public.asset_storage_deletions (
            operation_id, asset_id, bucket, prefix
        )
        SELECT
            p_operation_id,
            candidate.asset_id,
            p_bucket,
            'image-pyramids/' || candidate.asset_id::text || '/'
        FROM candidate_assets AS candidate
        ON CONFLICT (operation_id, asset_id) DO NOTHING
        RETURNING asset_id
    ),
    conflicting_assets AS (
        SELECT candidate.asset_id
        FROM candidate_assets AS candidate
        JOIN public.asset_storage_deletions AS deletion
          ON deletion.operation_id = p_operation_id
         AND deletion.asset_id = candidate.asset_id
        WHERE deletion.bucket IS DISTINCT FROM p_bucket
           OR deletion.prefix IS DISTINCT FROM (
                'image-pyramids/' || candidate.asset_id::text || '/'
           )
    )
    SELECT
        (SELECT pg_catalog.count(*) FROM candidate_assets),
        (SELECT pg_catalog.count(*) FROM conflicting_assets)
    INTO v_asset_count, v_conflicting_count;

    IF v_conflicting_count > 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'dataset unlink cleanup operation conflicts with recorded storage scope',
            DETAIL = '{"code":"operation_conflict"}';
    END IF;

    DELETE FROM public.image_pyramid_assets AS asset
    WHERE asset.task_id IN (
        SELECT reference.task_id
        FROM aap_labeling_v1.dataset_task_references(p_dataset_id, p_project_id) AS reference
    );

    RETURN v_asset_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.prepare_dataset_unlink_cleanup(uuid, uuid, uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.pending_storage_deletions(
    p_operation_id uuid,
    p_limit integer
)
RETURNS TABLE(operation_id uuid, asset_id uuid, bucket text, prefix text)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 1000 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid storage deletion page limit',
            DETAIL = '{"code":"invalid_request"}';
    END IF;

    RETURN QUERY
    SELECT
        deletion.operation_id,
        deletion.asset_id,
        deletion.bucket::text,
        deletion.prefix::text
    FROM public.asset_storage_deletions AS deletion
    WHERE deletion.completed_at IS NULL
      AND (p_operation_id IS NULL OR deletion.operation_id = p_operation_id)
    ORDER BY deletion.created_at, deletion.operation_id, deletion.asset_id
    LIMIT p_limit;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.pending_storage_deletions(uuid, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.complete_storage_deletion(
    p_operation_id uuid,
    p_asset_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_updated bigint;
BEGIN
    IF p_operation_id IS NULL OR p_asset_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid storage deletion completion request',
            DETAIL = '{"code":"invalid_request"}';
    END IF;

    UPDATE public.asset_storage_deletions AS deletion
    SET completed_at = COALESCE(deletion.completed_at, pg_catalog.now())
    WHERE deletion.operation_id = p_operation_id
      AND deletion.asset_id = p_asset_id;
    GET DIAGNOSTICS v_updated = ROW_COUNT;
    RETURN v_updated > 0;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.complete_storage_deletion(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_diagnostics_v1.detach_dataset_task_references(
    p_dataset_id uuid,
    p_project_id uuid
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_detached bigint;
BEGIN
    IF p_dataset_id IS NULL OR p_project_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid dataset diagnostics detach request',
            DETAIL = '{"code":"invalid_request"}';
    END IF;

    UPDATE public.bug_reports AS report
    SET task_id = NULL
    WHERE report.task_id IN (
        SELECT reference.task_id
        FROM aap_labeling_v1.dataset_task_references(p_dataset_id, p_project_id) AS reference
    );
    GET DIAGNOSTICS v_detached = ROW_COUNT;
    RETURN v_detached;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_diagnostics_v1.detach_dataset_task_references(uuid, uuid) FROM PUBLIC;
