CREATE FUNCTION yoao_assets_v1.filtered_dataset_catalog(
    p_actor_id uuid, p_search text, p_types text[], p_has_scenes boolean, p_state text,
    p_location_id uuid, p_limit integer, p_offset bigint
)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH candidates AS MATERIALIZED (
        SELECT d.id, d.created_at, d.archived_at, d.archived_by, d.lifecycle_revision
        FROM public.datasets AS d
        WHERE (p_state = 'all' OR ((p_state = 'archived') = (d.archived_at IS NOT NULL)))
          AND (p_search IS NULL OR d.name ILIKE ('%' || p_search || '%'))
          AND (p_types IS NULL OR d.data_type = ANY (p_types))
          AND (p_has_scenes IS NULL OR p_has_scenes = EXISTS (
              SELECT 1 FROM public.scenes AS s WHERE s.dataset_id = d.id
          ))
          AND (p_location_id IS NULL OR EXISTS (
              SELECT 1 FROM public.dataset_sources AS s
              WHERE s.dataset_id = d.id AND s.location_id = p_location_id
                AND yoao_assets_v1.dataset_source_visible(p_actor_id, s.id)
          ))
    ), page AS (
        SELECT * FROM candidates ORDER BY created_at DESC, id
        LIMIT LEAST(GREATEST(p_limit, 0), 200) OFFSET GREATEST(p_offset, 0)
    )
    SELECT pg_catalog.jsonb_build_object(
        'items', COALESCE((SELECT pg_catalog.jsonb_agg(
            yoao_assets_v1._catalog_dataset_summary(page.id) || pg_catalog.jsonb_build_object(
                'archived_at', page.archived_at,
                'archived_by', page.archived_by,
                'lifecycle_revision', page.lifecycle_revision
            ) ORDER BY page.created_at DESC, page.id
        ) FROM page), '[]'::jsonb),
        'total', (SELECT pg_catalog.count(*) FROM candidates)
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.filtered_dataset_catalog(uuid, text, text[], boolean, text, uuid, integer, bigint) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.pin_legacy_dataset_prefix(p_dataset_id uuid, p_revision integer)
RETURNS void
LANGUAGE sql VOLATILE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.datasets AS d SET storage_scan_prefix = d.name || '/', updated_at = pg_catalog.now()
    WHERE d.id = p_dataset_id AND d.lifecycle_revision = p_revision
      AND d.archived_at IS NULL AND d.storage_scan_prefix IS NULL
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.pin_legacy_dataset_prefix(uuid, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_display_item_page(
    p_dataset_id uuid, p_ids uuid[], p_limit integer, p_offset bigint
)
RETURNS TABLE (
    id uuid, dataset_id uuid, file_name text, file_path text, file_type text,
    file_size bigint, content_hash text, width integer, height integer,
    blurhash text, metadata_ jsonb, created_at timestamptz, thumbnail_path text,
    directory_owned boolean
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT i.id, i.dataset_id, i.file_name::text, i.file_path::text, i.file_type::text,
           i.file_size, i.content_hash::text, i.width, i.height, i.blurhash::text, i.metadata,
           i.created_at, i.thumbnail_path::text, yoao_assets_v1.dataset_owns_directory_media(i.id)
    FROM public.dataset_items AS i
    WHERE i.dataset_id = p_dataset_id AND (p_ids IS NULL OR i.id = ANY (p_ids))
    ORDER BY i.created_at DESC, i.id
    LIMIT LEAST(GREATEST(p_limit, 0), 200) OFFSET GREATEST(p_offset, 0)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_display_item_page(uuid, uuid[], integer, bigint) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_item_count(p_dataset_id uuid)
RETURNS bigint
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.count(*) FROM public.dataset_items AS i WHERE i.dataset_id = p_dataset_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_item_count(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_item_path_facts(p_dataset_id uuid)
RETURNS TABLE (file_name text, file_path text, metadata_ jsonb)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT i.file_name::text, i.file_path::text,
           pg_catalog.jsonb_build_object('storage_relative_path', i.metadata -> 'storage_relative_path')
    FROM public.dataset_items AS i WHERE i.dataset_id = p_dataset_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_item_path_facts(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_item_at_object_key(p_dataset_id uuid, p_file_path text)
RETURNS TABLE (id uuid)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT i.id FROM public.dataset_items AS i
    WHERE i.dataset_id = p_dataset_id AND i.file_path = p_file_path
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_item_at_object_key(uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.publish_managed_dataset_item(
    p_dataset_id uuid, p_revision integer, p_file_name text, p_file_path text,
    p_file_type text, p_file_size bigint, p_content_hash text,
    p_width integer, p_height integer, p_logical_path text, p_directory_owned_media boolean
)
RETURNS TABLE (
    id uuid, dataset_id uuid, file_name text, file_path text, file_type text,
    file_size bigint, content_hash text, width integer, height integer, metadata_ jsonb,
    created_at timestamptz
)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_file_path IS NULL OR pg_catalog.starts_with(p_file_path, '__aap_reference__/') THEN
        RAISE EXCEPTION 'invalid managed dataset Item publication' USING ERRCODE = '22023';
    END IF;
    UPDATE public.datasets AS d SET file_count = COALESCE(d.file_count, 0) + 1,
        updated_at = pg_catalog.now()
    WHERE d.id = p_dataset_id AND d.lifecycle_revision = p_revision AND d.archived_at IS NULL;
    IF NOT FOUND THEN RETURN; END IF;
    RETURN QUERY
    INSERT INTO public.dataset_items AS i (
        id, dataset_id, file_name, file_path, file_type, file_size, content_hash, width, height, metadata
    ) VALUES (
        pg_catalog.gen_random_uuid(), p_dataset_id, p_file_name, p_file_path, p_file_type,
        p_file_size, p_content_hash, p_width, p_height,
        CASE WHEN p_logical_path IS NULL THEN '{}'::jsonb
             ELSE pg_catalog.jsonb_build_object('storage_relative_path', p_logical_path) END
        || CASE WHEN p_directory_owned_media THEN '{"directory_owned_media": true}'::jsonb
                ELSE '{}'::jsonb END
    ) RETURNING i.id, i.dataset_id, i.file_name::text, i.file_path::text, i.file_type::text,
                i.file_size, i.content_hash::text, i.width, i.height, i.metadata, i.created_at;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.publish_managed_dataset_item(uuid, integer, text, text, text, bigint, text, integer, integer, text, boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_managed_item_asset_slot(p_item_id uuid, p_file_path text)
RETURNS boolean
LANGUAGE sql VOLATILE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT COALESCE((SELECT true FROM public.dataset_items AS i
                     WHERE i.id = p_item_id AND i.file_path = p_file_path
                       AND NOT pg_catalog.starts_with(i.file_path, '__aap_reference__/')
                     FOR UPDATE), false)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.lock_managed_item_asset_slot(uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.register_managed_item_asset(
    p_item_id uuid, p_asset_id uuid, p_file_path text, p_location_id uuid,
    p_bucket text, p_storage_ref jsonb, p_sha256 text, p_size_bytes bigint,
    p_etag text, p_verified_at timestamptz
)
RETURNS TABLE (
    id uuid, dataset_item_id uuid, media_type text, checksum_algorithm text,
    checksum_value text, size_bytes bigint, verification_state text
)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    item public.dataset_items%ROWTYPE;
    asset public.dataset_assets%ROWTYPE;
    locator public.dataset_asset_locations%ROWTYPE;
BEGIN
    SELECT i.* INTO item FROM public.dataset_items AS i
    WHERE i.id = p_item_id FOR UPDATE;
    IF NOT FOUND OR item.file_path IS DISTINCT FROM p_file_path
       OR pg_catalog.starts_with(item.file_path, '__aap_reference__/') THEN
        RAISE EXCEPTION 'dataset locator differs from its pinned Item' USING ERRCODE = '22023';
    END IF;
    SELECT a.* INTO asset FROM public.dataset_assets AS a WHERE a.dataset_item_id = item.id;
    IF NOT FOUND THEN
        INSERT INTO public.dataset_assets AS a
            (id, dataset_item_id, media_type, size_bytes, legacy_hash_hint, verification_state)
        VALUES (p_asset_id, item.id, item.file_type, item.file_size, item.content_hash, 'unverified')
        RETURNING a.* INTO asset;
    END IF;
    IF p_sha256 IS NOT NULL THEN
        IF asset.verification_state = 'verified' AND (
            asset.checksum_value IS DISTINCT FROM p_sha256 OR asset.size_bytes IS DISTINCT FROM p_size_bytes
        ) THEN
            RAISE EXCEPTION 'dataset content differs from verified identity' USING ERRCODE = '22023';
        END IF;
        UPDATE public.dataset_assets AS a
        SET checksum_algorithm = 'sha256', checksum_value = p_sha256,
            size_bytes = p_size_bytes, verification_state = 'verified'
        WHERE a.id = asset.id RETURNING a.* INTO asset;
    END IF;
    SELECT l.* INTO locator FROM public.dataset_asset_locations AS l
    WHERE l.asset_id = asset.id AND l.location_id = p_location_id AND l.bucket = p_bucket;
    IF NOT FOUND THEN
        INSERT INTO public.dataset_asset_locations (
            asset_id, location_id, bucket, object_key, storage_ref, reference_state, etag, verified_at
        ) VALUES (
            asset.id, p_location_id, p_bucket, item.file_path, p_storage_ref,
            CASE WHEN p_sha256 IS NULL THEN 'unverified' ELSE 'present' END,
            p_etag, CASE WHEN p_sha256 IS NOT NULL THEN p_verified_at END
        );
    ELSE
        IF locator.object_key IS DISTINCT FROM p_file_path OR locator.storage_ref IS DISTINCT FROM p_storage_ref THEN
            RAISE EXCEPTION 'dataset locator differs from its pinned mapping' USING ERRCODE = '22023';
        END IF;
        IF p_sha256 IS NOT NULL OR p_etag IS NOT NULL THEN
            UPDATE public.dataset_asset_locations AS l
            SET reference_state = CASE WHEN p_sha256 IS NOT NULL THEN 'present' ELSE l.reference_state END,
                verified_at = CASE WHEN p_sha256 IS NOT NULL THEN p_verified_at ELSE l.verified_at END,
                etag = COALESCE(p_etag, l.etag)
            WHERE l.asset_id = asset.id AND l.location_id = p_location_id AND l.bucket = p_bucket;
        END IF;
    END IF;
    RETURN QUERY SELECT asset.id, asset.dataset_item_id, asset.media_type::text,
        asset.checksum_algorithm::text, asset.checksum_value::text, asset.size_bytes, asset.verification_state::text;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.register_managed_item_asset(uuid, uuid, text, uuid, text, jsonb, text, bigint, text, timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_managed_duplicate_candidates(
    p_dataset_id uuid, p_exclude_item_id uuid, p_sha256 text, p_legacy_md5 text
)
RETURNS TABLE (
    id uuid, dataset_id uuid, file_path text, file_type text, file_size bigint,
    content_hash text, verified boolean
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH verified_candidate AS (
        SELECT i.id, i.dataset_id, i.file_path::text, i.file_type::text, i.file_size, i.content_hash::text, true AS verified
        FROM public.dataset_items AS i JOIN public.dataset_assets AS a ON a.dataset_item_id = i.id
        WHERE i.dataset_id = p_dataset_id AND i.id IS DISTINCT FROM p_exclude_item_id
          AND NOT pg_catalog.starts_with(i.file_path, '__aap_reference__/')
          AND a.verification_state = 'verified' AND a.checksum_value = p_sha256
        LIMIT 1
    )
    SELECT * FROM verified_candidate
    UNION ALL
    SELECT i.id, i.dataset_id, i.file_path::text, i.file_type::text, i.file_size, i.content_hash::text, false
    FROM public.dataset_items AS i LEFT JOIN public.dataset_assets AS a ON a.dataset_item_id = i.id
    WHERE p_legacy_md5 IS NOT NULL AND NOT EXISTS (SELECT 1 FROM verified_candidate)
      AND i.dataset_id = p_dataset_id AND i.id IS DISTINCT FROM p_exclude_item_id
      AND NOT pg_catalog.starts_with(i.file_path, '__aap_reference__/') AND i.content_hash = p_legacy_md5
      AND (a.id IS NULL OR a.verification_state = 'unverified')
    ORDER BY id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_managed_duplicate_candidates(uuid, uuid, text, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_cover_facts(p_item_id uuid)
RETURNS TABLE (file_type text, thumbnail_path text, blurhash text, directory_owned boolean)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT i.file_type::text, i.thumbnail_path::text, i.blurhash::text,
           yoao_assets_v1.dataset_owns_directory_media(i.id)
    FROM public.dataset_items AS i WHERE i.id = p_item_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_cover_facts(uuid) FROM PUBLIC;
