CREATE FUNCTION yoao_assets_v1.directory_preview_slot(p_source_id uuid, p_path_digest text)
RETURNS TABLE (
    source_id uuid, path_digest text, asset_id uuid, source_sha256 text, spec text,
    policy_revision integer, generation integer, state text, error_code text,
    location_id uuid, bucket text, object_key text, sha256 text, size_bytes bigint,
    measured_at timestamptz, thumbnail_output_id uuid
)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT p.source_id, p.path_digest::text, p.asset_id, p.source_sha256::text, p.spec::text,
           p.policy_revision, p.generation, p.state::text, p.error_code::text, p.location_id,
           p.bucket::text, p.object_key::text, p.sha256::text, p.size_bytes,
           p.measured_at, p.thumbnail_output_id
    FROM public.dataset_source_previews AS p
    WHERE p.source_id = p_source_id AND p.path_digest = p_path_digest
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_preview_slot(uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_preview_is_current(
    p_source_id uuid, p_path_digest text, p_asset_id uuid, p_sha256 text
)
RETURNS boolean
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.dataset_source_items AS head
        JOIN public.dataset_assets AS a ON a.id = head.asset_id
        WHERE head.source_id = p_source_id AND head.path_digest = p_path_digest
          AND a.id = p_asset_id AND a.checksum_value = p_sha256 AND head.content_sha256 = p_sha256
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_preview_is_current(uuid, text, uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.publish_directory_preview(
    p_source_id uuid, p_path_digest text, p_asset_id uuid, p_sha256 text,
    p_source_revision integer, p_policy_revision integer, p_location_revision integer,
    p_old_generation integer, p_measured_at timestamptz, p_output_id uuid
)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    candidate record;
    changed boolean;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.dataset_sources AS s
        JOIN public.storage_locations AS l ON l.id = s.location_id
        JOIN public.dataset_source_items AS h ON h.source_id = s.id AND h.path_digest = p_path_digest
        WHERE s.id = p_source_id AND s.enabled AND s.unregistered_at IS NULL AND l.enabled
          AND s.config_revision = p_source_revision AND s.policy_revision = p_policy_revision
          AND l.config_revision = p_location_revision AND h.asset_id = p_asset_id
          AND h.content_sha256 = p_sha256 AND s.cache_policy IN ('preview', 'on_demand')
          AND COALESCE(l.config ->> 'cache_policy', 'preview') IN ('preview', 'on_demand')
    ) THEN RETURN false; END IF;
    SELECT o.storage_ref, o.sha256, o.size_bytes INTO candidate
    FROM public.thumbnail_outputs AS o
    WHERE o.id = p_output_id AND o.owner_kind = 'directory_preview' AND o.owner_id = p_source_id
      AND o.path_digest = p_path_digest AND o.state = 'ready' AND NOT o.uncertain_write
      AND o.input_ref -> 'directory' = pg_catalog.jsonb_build_object(
          'source_id', p_source_id, 'path_digest', p_path_digest,
          'source_sha256', p_sha256, 'spec', 'webp-256-exif-q80'
      )
      AND o.input_ref -> 'encoded' = pg_catalog.jsonb_build_object('sha256', o.sha256, 'size_bytes', o.size_bytes);
    IF p_output_id IS NOT NULL AND NOT FOUND THEN RETURN false; END IF;
    INSERT INTO public.dataset_source_previews AS p (
        source_id, path_digest, asset_id, source_sha256, spec, policy_revision, generation, state,
        error_code, location_id, bucket, object_key, sha256, size_bytes, measured_at, thumbnail_output_id
    ) VALUES (
        p_source_id, p_path_digest, p_asset_id, p_sha256, 'webp-256-exif-q80', p_policy_revision,
        p_old_generation + 1,
        CASE WHEN p_output_id IS NULL THEN 'failed' ELSE 'ready' END,
        CASE WHEN p_output_id IS NULL THEN 'preview_generation_failed' END,
        (candidate.storage_ref ->> 'location_id')::uuid, candidate.storage_ref ->> 'bucket',
        (candidate.storage_ref ->> 'prefix') || 'thumbnail.webp', candidate.sha256,
        candidate.size_bytes, p_measured_at, p_output_id
    ) ON CONFLICT (source_id, path_digest) DO UPDATE
    SET asset_id = EXCLUDED.asset_id, source_sha256 = EXCLUDED.source_sha256,
        spec = EXCLUDED.spec, policy_revision = EXCLUDED.policy_revision,
        generation = EXCLUDED.generation, state = EXCLUDED.state, error_code = EXCLUDED.error_code,
        location_id = EXCLUDED.location_id, bucket = EXCLUDED.bucket, object_key = EXCLUDED.object_key,
        sha256 = EXCLUDED.sha256, size_bytes = EXCLUDED.size_bytes, measured_at = EXCLUDED.measured_at,
        thumbnail_output_id = EXCLUDED.thumbnail_output_id
    WHERE p.generation = p_old_generation
    RETURNING true INTO changed;
    RETURN COALESCE(changed, false);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.publish_directory_preview(uuid, text, uuid, text, integer, integer, integer, integer, timestamptz, uuid) FROM PUBLIC;
