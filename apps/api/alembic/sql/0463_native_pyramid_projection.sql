-- An asset with no remaining generations is unavailable, not a malformed read.
-- Explicit missing generations and dangling active/building references still refuse.
CREATE OR REPLACE FUNCTION aap_assets_v1._image_pyramid_projection(
    p_source jsonb, p_generation integer, p_profile_version text
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_owner_kind text;
    v_owner_id uuid;
    v_source_generation bigint;
    v_source_snapshot text;
    v_source_drifted boolean;
    v_upload_ready boolean;
    v_profile_version text;
    v_registered_width integer;
    v_registered_height integer;
    v_width integer;
    v_height integer;
    v_asset public.image_pyramid_assets%ROWTYPE;
    v_generation public.image_pyramid_generations%ROWTYPE;
    v_building public.image_pyramid_generations%ROWTYPE;
    v_dimension_job public.asset_image_jobs%ROWTYPE;
    v_policy jsonb;
    v_status text;
    v_building_status text;
    v_required boolean := false;
    v_retryable boolean := false;
    v_retry_after bigint;
    v_error_code text;
    v_current boolean := false;
    v_manifest jsonb;
    v_manifest_key text;
    v_manifest_digest text;
    v_overview_key text;
    v_overview_digest text;
    v_has_generation boolean := false;
BEGIN
    v_profile_version := COALESCE(p_profile_version, 'pyramid-v1');
    IF pg_catalog.jsonb_typeof(p_source) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_source -> 'owner') IS DISTINCT FROM 'object'
       OR p_source #>> '{owner,kind}' NOT IN ('dataset_item', 'task')
       OR p_source #>> '{owner,id}' IS NULL
       OR p_source ->> 'file_type' IS DISTINCT FROM 'image'
       OR COALESCE(p_source ->> 'source_snapshot', '') !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_source -> 'source_generation') IS DISTINCT FROM 'number'
       OR p_source ->> 'source_generation' !~ '^[1-9][0-9]*$'
       OR pg_catalog.jsonb_typeof(p_source -> 'source_drifted') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(p_source -> 'upload_ready') IS DISTINCT FROM 'boolean'
       OR pg_catalog.char_length(v_profile_version) NOT BETWEEN 1 AND 64 THEN
        RETURN NULL;
    END IF;
    BEGIN
        v_owner_kind := p_source #>> '{owner,kind}';
        v_owner_id := (p_source #>> '{owner,id}')::uuid;
        v_source_generation := (p_source ->> 'source_generation')::bigint;
        v_source_snapshot := p_source ->> 'source_snapshot';
        v_source_drifted := (p_source ->> 'source_drifted')::boolean;
        v_upload_ready := (p_source ->> 'upload_ready')::boolean;
        v_registered_width := CASE WHEN p_source -> 'width' = 'null'::jsonb THEN NULL
            ELSE (p_source ->> 'width')::integer END;
        v_registered_height := CASE WHEN p_source -> 'height' = 'null'::jsonb THEN NULL
            ELSE (p_source ->> 'height')::integer END;
        v_width := v_registered_width;
        v_height := v_registered_height;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN NULL;
    END;
    v_policy := aap_assets_v1._image_policy('{}'::jsonb);
    IF v_owner_kind = 'dataset_item' THEN
        SELECT asset.* INTO v_asset
        FROM public.image_pyramid_assets AS asset
        WHERE asset.dataset_item_id = v_owner_id
          AND asset.profile_version = v_profile_version
        ORDER BY asset.updated_at DESC, asset.id DESC
        LIMIT 1;
    ELSE
        SELECT asset.* INTO v_asset
        FROM public.image_pyramid_assets AS asset
        WHERE asset.task_id = v_owner_id
          AND asset.profile_version = v_profile_version
        ORDER BY asset.updated_at DESC, asset.id DESC
        LIMIT 1;
    END IF;
    IF FOUND THEN
        IF p_generation IS NOT NULL THEN
            IF p_generation < 1 THEN RETURN NULL; END IF;
            SELECT generation.* INTO v_generation
            FROM public.image_pyramid_generations AS generation
            WHERE generation.asset_id = v_asset.id
              AND generation.generation = p_generation;
            IF NOT FOUND THEN RETURN NULL; END IF;
        ELSIF v_asset.active_generation IS NOT NULL THEN
            SELECT generation.* INTO v_generation
            FROM public.image_pyramid_generations AS generation
            WHERE generation.asset_id = v_asset.id
              AND generation.generation = v_asset.active_generation;
        ELSIF v_asset.building_generation IS NOT NULL THEN
            SELECT generation.* INTO v_generation
            FROM public.image_pyramid_generations AS generation
            WHERE generation.asset_id = v_asset.id
              AND generation.generation = v_asset.building_generation;
        ELSE
            SELECT generation.* INTO v_generation
            FROM public.image_pyramid_generations AS generation
            WHERE generation.asset_id = v_asset.id
            ORDER BY generation.generation DESC
            LIMIT 1;
        END IF;
        v_has_generation := FOUND;
        IF v_asset.building_generation IS NOT NULL THEN
            SELECT generation.* INTO v_building
            FROM public.image_pyramid_generations AS generation
            WHERE generation.asset_id = v_asset.id
              AND generation.generation = v_asset.building_generation;
        END IF;
        IF NOT v_has_generation AND (v_asset.active_generation IS NOT NULL
            OR v_asset.building_generation IS NOT NULL) THEN
            RETURN NULL;
        END IF;
    END IF;
    IF NOT v_has_generation THEN
        v_status := CASE
            WHEN v_width IS NOT NULL AND v_height IS NOT NULL
             AND v_width::numeric * v_height::numeric >= (v_policy ->> 'optional_pixels')::numeric
                THEN 'missing'
            ELSE 'not_available'
        END;
        v_retryable := v_status = 'missing';
        v_required := v_width IS NOT NULL AND v_height IS NOT NULL
            AND v_width::numeric * v_height::numeric >= (v_policy ->> 'required_pixels')::numeric;
        IF (v_width IS NULL OR v_height IS NULL)
           AND NOT v_source_drifted AND v_upload_ready
           AND p_source -> 'object_identity' <> 'null'::jsonb THEN
            SELECT job.* INTO v_dimension_job
            FROM public.asset_image_jobs AS job
            WHERE job.owner_kind = v_owner_kind
              AND job.owner_id = v_owner_id
              AND job.kind = 'dimensions'
              AND job.slot_key IN ('dimensions', 'dimensions:preview')
              AND job.status IN ('pending', 'running', 'failed')
              AND job.source_generation = v_source_generation
              AND job.source_snapshot = v_source_snapshot
              AND job.source_facts #>> '{owner,kind}' = v_owner_kind
              AND job.source_facts #>> '{owner,id}' = v_owner_id::text
              AND job.spec ->> 'kind' = 'dimensions'
              AND job.spec ->> 'expected_source_snapshot' = v_source_snapshot
              AND job.source_facts ->> 'source_generation' = v_source_generation::text
              AND job.source_facts ->> 'source_snapshot' = v_source_snapshot
              AND job.source_facts -> 'source_drifted' = 'false'::jsonb
              AND job.source_facts -> 'upload_ready' = 'true'::jsonb
              AND aap_assets_v1._media_object_identity_valid(
                    job.source_facts -> 'object_identity'
              )
              AND aap_assets_v1._image_normalize_identity(
                    job.source_facts -> 'object_identity'
              ) = aap_assets_v1._image_normalize_identity(
                    p_source -> 'object_identity'
              )
            ORDER BY job.updated_at DESC, job.job_id DESC
            LIMIT 1;
            IF FOUND THEN
                IF v_dimension_job.status = 'pending' THEN
                    v_status := 'pending';
                    v_building_status := 'pending';
                    v_retry_after := 2000;
                ELSIF v_dimension_job.status = 'running' THEN
                    v_status := 'building';
                    v_building_status := 'building';
                    v_retry_after := 2000;
                ELSE
                    v_status := 'failed';
                    v_error_code := v_dimension_job.error_code;
                END IF;
            END IF;
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'asset_id', v_asset.id, 'status', v_status, 'required', v_required,
            'profile_version', v_profile_version,
            'generation', NULL, 'building_generation', NULL,
            'building_status', v_building_status,
            'source_generation', NULL, 'source_snapshot', NULL,
            'width', v_width, 'height', v_height, 'tile_size', 512,
            'format', NULL, 'retryable', v_retryable, 'retry_after_ms', v_retry_after,
            'error_code', v_error_code, 'manifest', NULL, 'manifest_key', NULL,
            'manifest_digest', NULL, 'overview_key', NULL, 'overview_digest', NULL,
            'tile_count', 0, 'retained_bytes', 0
        );
    END IF;
    IF v_generation.policy IS NOT NULL THEN
        v_policy := aap_assets_v1._image_policy(v_generation.policy);
        IF v_policy IS NULL THEN RETURN NULL; END IF;
    END IF;
    v_width := COALESCE(v_generation.width, v_width);
    v_height := COALESCE(v_generation.height, v_height);
    v_required := v_width IS NOT NULL AND v_height IS NOT NULL
        AND v_width::numeric * v_height::numeric >= (v_policy ->> 'required_pixels')::numeric;
    v_status := v_generation.status;
    v_current := COALESCE(v_generation.source_generation = v_source_generation
        AND v_generation.source_snapshot = v_source_snapshot
        AND v_generation.source_identity = aap_assets_v1._image_exec_legacy_identity(
            p_source -> 'object_identity'
        )
        AND (v_registered_width IS NULL OR v_generation.width = v_registered_width)
        AND (v_registered_height IS NULL OR v_generation.height = v_registered_height)
        AND NOT v_source_drifted AND v_upload_ready
        AND p_source -> 'object_identity' <> 'null'::jsonb, false);
    IF v_status = 'ready' AND NOT v_current THEN
        v_status := 'stale';
    ELSIF v_status = 'ready' AND (
        pg_catalog.jsonb_typeof(v_generation.manifest) IS DISTINCT FROM 'object'
        OR v_generation.manifest_key IS NULL
        OR v_generation.manifest_digest IS NULL
        OR v_generation.manifest_digest !~ '^sha256:[0-9a-f]{64}$'
        OR v_generation.overview_key IS NULL
        OR v_generation.overview_digest IS NULL
        OR v_generation.overview_digest !~ '^sha256:[0-9a-f]{64}$'
        OR (v_generation.manifest ->> 'generation') IS DISTINCT FROM v_generation.generation::text
    ) THEN
        v_status := 'inconsistent';
    END IF;
    v_retryable := v_status IN ('missing', 'failed', 'stale', 'inconsistent');
    v_error_code := CASE WHEN v_status = 'stale' THEN 'source_changed'
        WHEN v_status = 'inconsistent' AND v_generation.error_code IS NULL THEN 'inconsistent_ready'
        ELSE v_generation.error_code END;
    IF v_status IN ('pending', 'building') THEN
        v_retry_after := 2000;
    ELSIF v_status = 'failed' THEN
        v_retry_after := GREATEST(0::numeric,
            (v_policy ->> 'retry_cooldown_seconds')::numeric * 1000
            - GREATEST(0, EXTRACT(EPOCH FROM (pg_catalog.clock_timestamp() - v_generation.updated_at)) * 1000)
        )::bigint;
    END IF;
    IF v_status = 'ready' THEN
        v_manifest := v_generation.manifest;
        v_manifest_key := v_generation.manifest_key;
        v_manifest_digest := v_generation.manifest_digest;
        v_overview_key := v_generation.overview_key;
        v_overview_digest := v_generation.overview_digest;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'asset_id', v_asset.id,
        'status', v_status,
        'required', v_required,
        'profile_version', v_asset.profile_version,
        'generation', v_generation.generation,
        'building_generation', v_asset.building_generation,
        'building_status', CASE WHEN v_building.status IN ('pending', 'building')
            THEN v_building.status END,
        'source_generation', v_generation.source_generation,
        'source_snapshot', v_generation.source_snapshot,
        'width', v_generation.width,
        'height', v_generation.height,
        'tile_size', 512,
        'format', CASE WHEN v_status = 'ready' THEN v_generation.format END,
        'retryable', v_retryable,
        'retry_after_ms', v_retry_after,
        'error_code', v_error_code,
        'manifest', v_manifest,
        'manifest_key', v_manifest_key,
        'manifest_digest', v_manifest_digest,
        'overview_key', v_overview_key,
        'overview_digest', v_overview_digest,
        'tile_count', COALESCE(v_generation.tile_count, 0),
        'retained_bytes', COALESCE(v_generation.retained_bytes, 0)
    );
END;
$function$;
