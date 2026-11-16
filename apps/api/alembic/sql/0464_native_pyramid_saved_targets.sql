-- Saved-output read responsibility and target context for dynamic media-cache
-- bindings.  A binding switch affects new allocations only; reads and
-- retirement always use the immutable allocation saved on the output row.
--
-- This asset supersedes the current 0463 helper bodies without editing their
-- frozen files.  It keeps the exact saved prefix, Native source snapshots,
-- valid empty projections, durable GC policy, ready->retiring URL wait and the
-- absence of a legacy garbage fallback.

DROP FUNCTION IF EXISTS aap_assets_v1.claim_native_pyramid_retirement(uuid, jsonb, integer);
-- aap-statement-break
DROP FUNCTION IF EXISTS aap_assets_v1._native_pyramid_retirement_store_valid(jsonb);
-- aap-statement-break
DROP INDEX IF EXISTS aap_assets_v1.ix_native_pyramid_retirement_targets_store;
-- aap-statement-break
-- Fixture-owned outputs belong to the sealed fixture owner.  Ordinary Native
-- contexts must never sign or read them, and an ordinary producer reading a
-- fixture original is not thereby fixture-owned.
CREATE OR REPLACE FUNCTION aap_assets_v1._native_pyramid_output_fixture_owned(
    p_output uuid
)
RETURNS boolean
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_output IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid native pyramid output';
    END IF;
    RETURN EXISTS (
        SELECT 1
        FROM aap_assets_v1.image_pyramid_native_attempts AS attempt
        JOIN aap_assets_v1.fixture_native_media_jobs AS fixture
          ON fixture.family = 'image' AND fixture.job_id = attempt.job_id
        WHERE attempt.output_id = p_output
    ) OR EXISTS (
        SELECT 1
        FROM aap_assets_v1.fixture_media_births AS birth
        WHERE birth.owner_kind = 'pyramid_output' AND birth.owner_id = p_output
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._native_pyramid_output_fixture_owned(uuid) FROM PUBLIC;
-- aap-statement-break
-- The immutable saved allocation is the only addressable scope.  A saved
-- output survives current bindings and business rows; a missing or mutated
-- receipt is a refusal, never permission to redirect to the current purpose.
CREATE OR REPLACE FUNCTION aap_assets_v1._image_saved_output_target(
    p_asset_id uuid, p_generation integer, p_output_id uuid, p_ready_only boolean
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_ref jsonb;
BEGIN
    IF p_asset_id IS NULL OR p_generation IS NULL OR p_generation < 1
       OR p_output_id IS NULL OR p_ready_only IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid saved image output scope';
    END IF;
    SELECT output.storage_ref
    INTO v_ref
    FROM public.image_pyramid_outputs AS output
    WHERE output.id = p_output_id
      AND output.asset_id = p_asset_id
      AND output.generation = p_generation
      -- A live saved output (or one whose exact retirement already started)
      -- is its own durable obligation; only the ready read needs live bytes.
      AND ((p_ready_only AND output.state = 'ready')
           OR (NOT p_ready_only AND output.state IN (
                'building', 'ready', 'retiring', 'deleted')))
      AND aap_assets_v1._native_pyramid_retirement_target_valid(
            output.storage_ref, output.id
          )
    FOR SHARE NOWAIT;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    RETURN v_ref;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_saved_output_target(uuid, integer, uuid, boolean) FROM PUBLIC;
-- aap-statement-break
-- Existing 0463 callers keep their exact saved-prefix proof through this thin
-- scope reader; no legacy bucket-root pattern is accepted.
CREATE OR REPLACE FUNCTION aap_assets_v1._image_saved_output_prefix(
    p_asset_id uuid, p_generation integer, p_output_id uuid, p_ready_only boolean
) RETURNS text
LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT aap_assets_v1._image_saved_output_target(
        p_asset_id, p_generation, p_output_id, p_ready_only
    ) ->> 'prefix'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_saved_output_prefix(uuid, integer, uuid, boolean) FROM PUBLIC;
-- aap-statement-break
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
    v_native boolean := false;
    v_native_ref jsonb;
    v_output_state text;
    v_native_target jsonb;
    v_retirement_url_expiry integer;
    v_fixture_owned boolean := false;
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
            'tile_count', 0, 'retained_bytes', 0,
            'native', false, 'native_output', NULL
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
    -- Native ownership is the durable saved-output attempt receipt, never a
    -- current purpose binding or a key-prefix guess.  Only a ready projection
    -- exposes the saved target; a broken receipt keeps the generation visible
    -- as inconsistent rather than silently falling back to a legacy read.
    IF v_generation.output_id IS NOT NULL THEN
        SELECT output.state, output.storage_ref
        INTO v_output_state, v_native_target
        FROM public.image_pyramid_outputs AS output
        WHERE output.id = v_generation.output_id;
        v_native := FOUND AND EXISTS (
            SELECT 1
            FROM aap_assets_v1.image_pyramid_native_attempts AS attempt
            WHERE attempt.output_id = v_generation.output_id
              AND attempt.asset_id = v_generation.asset_id
              AND attempt.generation = v_generation.generation
        );
    END IF;
    IF v_status = 'ready' AND v_native THEN
        IF v_output_state IS DISTINCT FROM 'ready'
           OR NOT aap_assets_v1._native_pyramid_retirement_target_valid(
                v_native_target, v_generation.output_id
              ) THEN
            v_status := 'inconsistent';
            v_retryable := true;
            v_error_code := COALESCE(v_error_code, 'inconsistent_ready');
        ELSE
            v_policy := aap_assets_v1._image_policy(
                COALESCE(v_generation.policy, '{}'::jsonb)
            );
            IF v_policy IS NULL
               OR NOT aap_assets_v1._native_pyramid_retirement_policy_valid(v_policy) THEN
                v_status := 'inconsistent';
                v_retryable := true;
                v_error_code := COALESCE(v_error_code, 'inconsistent_ready');
            ELSE
                v_retirement_url_expiry := (v_policy ->> 'url_expiry_seconds')::integer;
                v_fixture_owned := aap_assets_v1._native_pyramid_output_fixture_owned(
                    v_generation.output_id
                );
                v_native_ref := pg_catalog.jsonb_build_object(
                    'output_id', v_generation.output_id,
                    'storage_ref', v_native_target,
                    'url_expiry_seconds', v_retirement_url_expiry,
                    'fixture_owned', v_fixture_owned
                );
            END IF;
        END IF;
    END IF;
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
        'retained_bytes', COALESCE(v_generation.retained_bytes, 0),
        'native', v_native, 'native_output', v_native_ref
    );
END;
$function$;
-- aap-statement-break
-- bo0455 already declares this exact signature, so this body supersedes it.
CREATE OR REPLACE FUNCTION aap_assets_v1.allocate_native_pyramid_output(
    p_job uuid,p_job_generation bigint,p_lease uuid,p_asset uuid,p_generation integer
) RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_job public.asset_image_jobs%ROWTYPE;
    v_asset public.image_pyramid_assets%ROWTYPE;
    v_generation public.image_pyramid_generations%ROWTYPE;
    v_output public.image_pyramid_outputs%ROWTYPE;
    v_fixture aap_assets_v1.fixture_native_media_jobs%ROWTYPE;
    v_target aap_assets_v1.fixture_native_cache_targets%ROWTYPE;
    v_binding public.storage_bindings%ROWTYPE;
    v_location public.storage_locations%ROWTYPE;
    v_connection public.storage_connections%ROWTYPE;
    v_fixture_job boolean := false;
    v_output_id uuid := pg_catalog.gen_random_uuid();
    v_endpoint text;
    v_bucket text;
    v_key_prefix text;
    v_location_id uuid;
    v_config_revision integer;
    v_binding_generation bigint;
    v_storage_identity text;
    v_prefix text;
    v_ref jsonb;
    v_gate record;
BEGIN
    IF p_job IS NULL OR p_job_generation IS NULL OR p_job_generation<1
       OR p_lease IS NULL OR p_asset IS NULL OR p_generation IS NULL OR p_generation<1 THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT * INTO v_job FROM public.asset_image_jobs WHERE job_id=p_job FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    END;
    IF NOT FOUND OR v_job.kind<>'pyramid' OR v_job.status<>'running'
       OR v_job.generation<>p_job_generation
       OR v_job.lease_token IS DISTINCT FROM p_lease
       OR v_job.lease_expires_at IS NULL OR v_job.lease_expires_at<=pg_catalog.clock_timestamp()
       OR v_job.pyramid_asset_id IS DISTINCT FROM p_asset
       OR v_job.pyramid_generation IS DISTINCT FROM p_generation THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    SELECT * INTO v_asset FROM public.image_pyramid_assets WHERE id=p_asset FOR UPDATE NOWAIT;
    SELECT * INTO v_generation FROM public.image_pyramid_generations
    WHERE asset_id=p_asset AND generation=p_generation FOR UPDATE NOWAIT;
    IF v_asset.id IS NULL OR v_generation.asset_id IS NULL
       OR v_asset.building_generation IS DISTINCT FROM p_generation
       OR v_generation.status<>'building'
       OR v_generation.job_id IS DISTINCT FROM p_job
       OR v_generation.job_generation IS DISTINCT FROM p_job_generation
       OR v_generation.lease_token IS DISTINCT FROM p_lease
       OR v_generation.lease_expires_at IS NULL
       OR v_generation.lease_expires_at<=pg_catalog.clock_timestamp()
       OR v_generation.source_generation IS DISTINCT FROM v_job.source_generation
       OR v_generation.source_snapshot IS DISTINCT FROM v_job.source_snapshot
       OR v_asset.profile_version IS DISTINCT FROM (v_job.spec #>> '{policy,profile_version}') THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    -- An earlier attempt on another lease keeps its own saved output untouched.
    IF v_generation.output_id IS NOT NULL THEN
        SELECT * INTO v_output FROM public.image_pyramid_outputs
        WHERE id=v_generation.output_id FOR UPDATE NOWAIT;
        IF FOUND AND v_output.state='building' AND v_output.lease_token=p_lease
           AND v_output.asset_id=p_asset AND v_output.generation=p_generation THEN
            RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
                'output_id',v_output.id,'storage_ref',v_output.storage_ref,
                'job_id',p_job,'job_generation',p_job_generation,'lease_token',p_lease,
                'asset_id',p_asset,'generation',p_generation
            ), NULL);
        END IF;
    END IF;
    SELECT * INTO v_fixture FROM aap_assets_v1.fixture_native_media_jobs
    WHERE family='image' AND job_id=p_job;
    v_fixture_job := FOUND;
    IF v_fixture_job THEN
        IF v_fixture.job_kind<>'pyramid' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM aap_assets_v1.fixture_native_media_claims AS claim
            WHERE claim.family='image' AND claim.job_id=p_job
              AND claim.job_generation=p_job_generation AND claim.lease_token=p_lease
              AND claim.state='claimed'
        ) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
        END IF;
        IF (v_fixture.owner_kind='task' AND (v_asset.task_id IS DISTINCT FROM v_fixture.owner_id
              OR v_asset.dataset_item_id IS NOT NULL))
           OR (v_fixture.owner_kind<>'task' AND (v_asset.dataset_item_id IS DISTINCT FROM v_fixture.item_id
              OR v_asset.task_id IS NOT NULL)) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'dependency_changed');
        END IF;
        SELECT * INTO v_target FROM aap_assets_v1.fixture_native_cache_targets
        WHERE run_id=v_fixture.run_id FOR SHARE;
        IF NOT FOUND THEN
            RETURN aap_assets_v1._image_reply(NULL, 'dependency_changed');
        END IF;
        v_endpoint := v_target.endpoint;
        v_bucket := v_target.bucket;
        v_storage_identity := v_target.storage_identity;
        v_location_id := v_target.location_id;
        v_binding_generation := v_target.binding_generation;
        v_key_prefix := '_aap_managed/media-cache/';
        SELECT location.config_revision INTO v_config_revision
        FROM public.storage_locations AS location WHERE location.id=v_location_id;
    ELSE
        SELECT * INTO v_binding FROM public.storage_bindings WHERE purpose='media-cache' FOR SHARE;
        IF NOT FOUND THEN
            RETURN aap_assets_v1._image_reply(NULL, 'storage_maintenance');
        END IF;
        SELECT * INTO v_location FROM public.storage_locations WHERE id=v_binding.location_id FOR SHARE;
        IF NOT FOUND OR v_location.kind<>'s3' OR v_location.enabled IS NOT TRUE
           OR v_location.config_revision<1 THEN
            RETURN aap_assets_v1._image_reply(NULL, 'storage_maintenance');
        END IF;
        IF v_location.id='4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7' THEN
            IF pg_catalog.jsonb_typeof(v_location.config -> 'use_ssl') IS DISTINCT FROM 'boolean'
               OR COALESCE(v_location.config ->> 'endpoint','') = '' THEN
                RETURN aap_assets_v1._image_reply(NULL, 'storage_maintenance');
            END IF;
            v_endpoint := CASE WHEN (v_location.config ->> 'use_ssl')::boolean
                THEN 'https://' ELSE 'http://' END || (v_location.config ->> 'endpoint');
            v_key_prefix := '';
        ELSE
            SELECT * INTO v_connection FROM public.storage_connections
            WHERE id=v_location.connection_id;
            IF NOT FOUND OR v_connection.kind<>'s3' OR v_connection.scope<>'global'
               OR v_connection.project_id IS NOT NULL OR v_connection.revision<1
               OR v_connection.secret_enc IS NULL
               -- The saved bucket must be the bucket this registered connection
               -- actually addresses.  A mutated connection config is a refusal,
               -- never permission to mint a target the saved-location lane
               -- would subsequently be unable to read or retire.
               OR pg_catalog.jsonb_typeof(v_connection.config -> 'bucket')
                    IS DISTINCT FROM 'string'
               OR v_connection.config ->> 'bucket' IS DISTINCT FROM v_binding.bucket
               OR pg_catalog.jsonb_typeof(v_connection.config -> 'endpoint')
                    IS DISTINCT FROM 'string' THEN
                RETURN aap_assets_v1._image_reply(NULL, 'storage_maintenance');
            END IF;
            v_endpoint := v_connection.config ->> 'endpoint';
            v_key_prefix := pg_catalog.rtrim(COALESCE(v_connection.config ->> 'base_prefix',''),'/');
            IF v_key_prefix<>'' THEN
                IF v_key_prefix ~ '(^|/)[.][.]?(/|$)|//' THEN
                    RETURN aap_assets_v1._image_reply(NULL, 'storage_maintenance');
                END IF;
                v_key_prefix := v_key_prefix || '/';
            END IF;
            v_key_prefix := v_key_prefix || '_aap_managed/media-cache/';
        END IF;
        v_bucket := v_binding.bucket;
        v_location_id := v_location.id;
        v_config_revision := v_location.config_revision;
        v_binding_generation := v_binding.generation;
        v_storage_identity := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
            pg_catalog.jsonb_build_array(
                pg_catalog.rtrim(v_endpoint,'/'),v_bucket
            )::text,'UTF8')),'hex');
    END IF;
    IF v_endpoint IS NULL OR v_endpoint !~ '^https?://[^[:space:]/?#@]+$'
       OR position(chr(92) in v_endpoint)>0
       OR v_bucket IS NULL OR pg_catalog.length(v_bucket) NOT BETWEEN 1 AND 255
       OR v_bucket ~ '[[:space:][:cntrl:]/?#@%:]' OR position(chr(92) in v_bucket)>0
       OR v_storage_identity IS NULL OR v_storage_identity !~ '^[0-9a-f]{64}$'
       OR v_location_id IS NULL OR v_config_revision IS NULL OR v_config_revision<1
       OR v_binding_generation IS NULL OR v_binding_generation<1
       OR v_key_prefix IS NULL OR pg_catalog.octet_length(v_key_prefix)>436 THEN
        RETURN aap_assets_v1._image_reply(NULL, 'storage_maintenance');
    END IF;
    INSERT INTO public.storage_maintenance_gates(location_id,state,revision)
    VALUES (v_location_id,'active',1) ON CONFLICT DO NOTHING;
    SELECT gate.state,gate.revision INTO v_gate FROM public.storage_maintenance_gates AS gate
    WHERE gate.location_id=v_location_id FOR SHARE;
    IF NOT FOUND OR v_gate.state<>'active' OR v_gate.revision<1 THEN
        RETURN aap_assets_v1._image_reply(NULL, 'storage_maintenance');
    END IF;
    v_prefix := v_key_prefix || 'image-pyramids/objects/' || v_output_id::text || '/';
    v_ref := pg_catalog.jsonb_build_object(
        'schema',2,'purpose','media-cache','location_id',v_location_id::text,
        'config_revision',v_config_revision,'binding_generation',v_binding_generation,
        'endpoint_url',pg_catalog.rtrim(v_endpoint,'/'),'bucket',v_bucket,
        'key_prefix',v_key_prefix,'prefix',v_prefix,'storage_identity',v_storage_identity
    );
    INSERT INTO public.image_pyramid_outputs(
        id,asset_id,generation,lease_token,storage_ref,state
    ) VALUES (v_output_id,p_asset,p_generation,p_lease,v_ref,'building');
    INSERT INTO aap_assets_v1.image_pyramid_native_attempts(
        output_id,job_id,job_generation,lease_token,asset_id,generation,
        source_generation,source_snapshot,profile_version,gate_revision
    ) VALUES (
        v_output_id,p_job,p_job_generation,p_lease,p_asset,p_generation,
        v_job.source_generation,v_job.source_snapshot,
        v_asset.profile_version,v_gate.revision
    );
    UPDATE public.image_pyramid_generations
    SET output_id=v_output_id,output_prefix=v_prefix,updated_at=pg_catalog.clock_timestamp()
    WHERE asset_id=p_asset AND generation=p_generation;
    -- The saved output owns its bytes; legacy prefix garbage must not remove them.
    UPDATE public.asset_image_jobs SET output_prefix=NULL WHERE job_id=p_job;
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
        'output_id',v_output_id,'storage_ref',v_ref,
        'job_id',p_job,'job_generation',p_job_generation,'lease_token',p_lease,
        'asset_id',p_asset,'generation',p_generation
    ), NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
WHEN unique_violation THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
END;
$function$;
-- aap-statement-break
-- Durable read responsibility.  A permit is opened with caller-chosen
-- operation/token identifiers and closed after the executor drained physical
-- reads.  A close that arrives before admission leaves a tombstone, so a
-- delayed admission can never reopen the same permit.
CREATE TABLE aap_assets_v1.native_pyramid_read_permits (
    operation_id uuid PRIMARY KEY,
    token uuid NOT NULL UNIQUE,
    state text NOT NULL CHECK (state IN ('open', 'closed')),
    actor_id uuid,
    task_id uuid,
    dataset_item_id uuid,
    output_id uuid
        REFERENCES public.image_pyramid_outputs(id) ON DELETE RESTRICT,
    asset_id uuid,
    generation integer CHECK (generation IS NULL OR generation > 0),
    source_generation bigint CHECK (source_generation IS NULL OR source_generation > 0),
    source_snapshot text CHECK (
        source_snapshot IS NULL OR source_snapshot ~ '^[0-9a-f]{64}$'
    ),
    profile_version text CHECK (
        profile_version IS NULL
        OR pg_catalog.char_length(profile_version) BETWEEN 1 AND 64
    ),
    location_id uuid,
    gate_revision integer CHECK (gate_revision IS NULL OR gate_revision > 0),
    storage_ref jsonb,
    pyramid jsonb,
    url_expiry_seconds integer CHECK (
        url_expiry_seconds IS NULL OR url_expiry_seconds > 0
    ),
    fixture_owned boolean,
    opened_at timestamptz,
    closed_at timestamptz,
    CHECK ((state = 'open') = (closed_at IS NULL)),
    CHECK ((state = 'open') = (opened_at IS NOT NULL)),
    CHECK (
        state = 'closed'
        OR (
            actor_id IS NOT NULL
            AND ((task_id IS NULL) <> (dataset_item_id IS NULL))
            AND output_id IS NOT NULL
            AND asset_id IS NOT NULL
            AND generation IS NOT NULL
            AND source_generation IS NOT NULL
            AND source_snapshot IS NOT NULL
            AND profile_version IS NOT NULL
            AND location_id IS NOT NULL
            AND gate_revision IS NOT NULL
            AND pg_catalog.jsonb_typeof(storage_ref) = 'object'
            AND pg_catalog.jsonb_typeof(pyramid) = 'object'
            AND url_expiry_seconds IS NOT NULL
            AND fixture_owned IS NOT NULL
        )
    )
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.native_pyramid_read_permits FROM PUBLIC;
-- aap-statement-break
CREATE INDEX ix_native_pyramid_read_permits_open
    ON aap_assets_v1.native_pyramid_read_permits(output_id)
    WHERE state = 'open';
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_native_pyramid_read_permit()
RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'native_pyramid_read_permit_retained';
    END IF;
    IF NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
    IF (NEW.operation_id, NEW.token, NEW.actor_id, NEW.task_id,
        NEW.dataset_item_id, NEW.output_id, NEW.asset_id, NEW.generation,
        NEW.source_generation, NEW.source_snapshot, NEW.profile_version,
        NEW.location_id, NEW.gate_revision, NEW.storage_ref, NEW.pyramid,
        NEW.url_expiry_seconds, NEW.fixture_owned, NEW.opened_at)
       IS DISTINCT FROM
       (OLD.operation_id, OLD.token, OLD.actor_id, OLD.task_id,
        OLD.dataset_item_id, OLD.output_id, OLD.asset_id, OLD.generation,
        OLD.source_generation, OLD.source_snapshot, OLD.profile_version,
        OLD.location_id, OLD.gate_revision, OLD.storage_ref, OLD.pyramid,
        OLD.url_expiry_seconds, OLD.fixture_owned, OLD.opened_at) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'native_pyramid_read_permit_immutable';
    END IF;
    IF OLD.state <> 'open' OR NEW.state <> 'closed' THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'native_pyramid_read_permit_transition';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_native_pyramid_read_permit() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER native_pyramid_read_permit_guard
BEFORE UPDATE OR DELETE ON aap_assets_v1.native_pyramid_read_permits
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_native_pyramid_read_permit();
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.begin_native_pyramid_read(
    p_actor_id uuid, p_task_id uuid, p_dataset_item_id uuid,
    p_generation integer, p_profile_version text,
    p_operation_id uuid, p_token uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_source_reply jsonb;
    v_source jsonb;
    v_projection jsonb;
    v_selected_profile text;
    v_output public.image_pyramid_outputs%ROWTYPE;
    v_output_id uuid;
    v_asset_id uuid;
    v_target aap_assets_v1.native_pyramid_retirement_targets%ROWTYPE;
    v_ref jsonb;
    v_gate record;
    v_existing aap_assets_v1.native_pyramid_read_permits%ROWTYPE;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_actor_id IS NULL
       OR ((p_task_id IS NULL) = (p_dataset_item_id IS NULL))
       OR p_generation IS NULL OR p_generation < 1
       OR (p_profile_version IS NOT NULL
           AND pg_catalog.char_length(p_profile_version) NOT BETWEEN 1 AND 64)
       OR p_operation_id IS NULL OR p_token IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_selected_profile := COALESCE(p_profile_version, 'pyramid-v1');
    SELECT *
    INTO v_existing
    FROM aap_assets_v1.native_pyramid_read_permits AS permit
    WHERE permit.operation_id = p_operation_id
    FOR UPDATE;
    IF FOUND THEN
        IF v_existing.token IS DISTINCT FROM p_token THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        IF v_existing.state = 'closed' THEN
            -- A tombstone or an already closed permit is settled work.
            RETURN aap_assets_v1._image_reply(NULL, 'operation_conflict');
        END IF;
        RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
            'operation_id', v_existing.operation_id,
            'token', v_existing.token,
            'output_id', v_existing.output_id,
            'storage_ref', v_existing.storage_ref,
            'pyramid', v_existing.pyramid,
            'url_expiry_seconds', v_existing.url_expiry_seconds,
            'fixture_owned', v_existing.fixture_owned
        ), NULL);
    END IF;
    v_source_reply := aap_assets_v1._image_owner_source(
        p_actor_id, p_task_id, p_dataset_item_id, false
    );
    IF v_source_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_source_reply;
    END IF;
    v_source := v_source_reply -> 'data';
    IF NOT aap_assets_v1._image_source_data_valid(v_source) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    v_projection := aap_assets_v1._image_pyramid_projection(
        v_source, p_generation, v_selected_profile
    );
    IF v_projection IS NULL
       OR v_projection ->> 'status' IS DISTINCT FROM 'ready'
       OR v_projection ->> 'generation' IS DISTINCT FROM p_generation::text THEN
        RETURN aap_assets_v1._image_reply(NULL, 'asset_missing');
    END IF;
    IF v_projection ->> 'native' IS DISTINCT FROM 'true'
       OR pg_catalog.jsonb_typeof(v_projection -> 'native_output')
            IS DISTINCT FROM 'object' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'asset_missing');
    END IF;
    IF (v_projection #>> '{native_output,fixture_owned}') IS DISTINCT FROM 'false' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'permission_denied');
    END IF;
    BEGIN
        v_output_id := (v_projection #>> '{native_output,output_id}')::uuid;
        v_asset_id := (v_projection ->> 'asset_id')::uuid;
        SELECT output.* INTO v_output
        FROM public.image_pyramid_outputs AS output
        WHERE output.id = v_output_id
        FOR SHARE NOWAIT;
        IF NOT FOUND THEN
            RETURN aap_assets_v1._image_reply(NULL, 'asset_missing');
        END IF;
        SELECT target.* INTO v_target
        FROM aap_assets_v1.native_pyramid_retirement_targets AS target
        WHERE target.output_id = v_output_id
        FOR SHARE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    END;
    IF v_output.state IS DISTINCT FROM 'ready'
       OR v_output.asset_id IS DISTINCT FROM v_asset_id
       OR v_output.generation IS DISTINCT FROM p_generation
       OR v_output.storage_ref IS DISTINCT FROM
            (v_projection #> '{native_output,storage_ref}')
       OR NOT aap_assets_v1._native_pyramid_retirement_target_valid(
            v_output.storage_ref, v_output_id
          ) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM aap_assets_v1.image_pyramid_native_attempts AS attempt
        WHERE attempt.output_id = v_output_id
          AND attempt.asset_id = v_output.asset_id
          AND attempt.generation = v_output.generation
    ) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    IF v_target.output_id IS DISTINCT FROM v_output_id
       OR v_target.asset_id IS DISTINCT FROM v_output.asset_id
       OR v_target.generation IS DISTINCT FROM v_output.generation
       OR NOT aap_assets_v1._native_pyramid_retirement_policy_valid(v_target.policy)
       OR v_target.url_expiry_seconds IS DISTINCT FROM
            (v_projection #>> '{native_output,url_expiry_seconds}')::integer THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    v_ref := pg_catalog.jsonb_build_object(
        'schema', 2,
        'purpose', 'media-cache',
        'location_id', v_target.location_id::text,
        'config_revision', v_target.config_revision,
        'binding_generation', v_target.binding_generation,
        'endpoint_url', v_target.endpoint_url,
        'bucket', v_target.bucket,
        'key_prefix', v_target.key_prefix,
        'prefix', v_target.prefix,
        'storage_identity', v_target.storage_identity
    );
    IF v_ref IS DISTINCT FROM v_output.storage_ref THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    SELECT gate.state, gate.revision INTO v_gate
    FROM public.storage_maintenance_gates AS gate
    WHERE gate.location_id = v_target.location_id
    FOR SHARE;
    IF NOT FOUND OR v_gate.state <> 'active' OR v_gate.revision < 1 THEN
        RETURN aap_assets_v1._image_reply(NULL, 'storage_unavailable');
    END IF;
    INSERT INTO aap_assets_v1.native_pyramid_read_permits(
        operation_id, token, state, actor_id, task_id, dataset_item_id,
        output_id, asset_id, generation, source_generation, source_snapshot,
        profile_version, location_id, gate_revision, storage_ref, pyramid,
        url_expiry_seconds, fixture_owned, opened_at
    ) VALUES (
        p_operation_id, p_token, 'open', p_actor_id, p_task_id, p_dataset_item_id,
        v_output_id, v_output.asset_id, v_output.generation,
        (v_projection ->> 'source_generation')::bigint,
        v_projection ->> 'source_snapshot', v_selected_profile,
        v_target.location_id, v_gate.revision, v_output.storage_ref,
        v_projection, v_target.url_expiry_seconds, false, v_now
    );
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
        'operation_id', p_operation_id,
        'token', p_token,
        'output_id', v_output_id,
        'storage_ref', v_output.storage_ref,
        'pyramid', v_projection,
        'url_expiry_seconds', v_target.url_expiry_seconds,
        'fixture_owned', false
    ), NULL);
EXCEPTION
    WHEN unique_violation THEN
        BEGIN
            SELECT *
            INTO v_existing
            FROM aap_assets_v1.native_pyramid_read_permits AS permit
            WHERE permit.operation_id = p_operation_id
            FOR UPDATE;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        END;
        IF NOT FOUND OR v_existing.token IS DISTINCT FROM p_token THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        IF v_existing.state = 'closed' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_conflict');
        END IF;
        RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
            'operation_id', v_existing.operation_id,
            'token', v_existing.token,
            'output_id', v_existing.output_id,
            'storage_ref', v_existing.storage_ref,
            'pyramid', v_existing.pyramid,
            'url_expiry_seconds', v_existing.url_expiry_seconds,
            'fixture_owned', v_existing.fixture_owned
        ), NULL);
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_native_pyramid_read(uuid, uuid, uuid, integer, text, uuid, uuid) FROM PUBLIC;
-- aap-statement-break
-- Closure is physical evidence from the executor.  It is independent of
-- whether the source still authorizes a new reference; a later owner read is
-- the authority fence for publication.  A late admission cannot reopen a
-- permit that was already closed/tombstoned under the same caller identifiers.
CREATE OR REPLACE FUNCTION aap_assets_v1.complete_native_pyramid_read(
    p_operation_id uuid, p_token uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_permit aap_assets_v1.native_pyramid_read_permits%ROWTYPE;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_operation_id IS NULL OR p_token IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        INSERT INTO aap_assets_v1.native_pyramid_read_permits(
            operation_id, token, state, closed_at
        ) VALUES (p_operation_id, p_token, 'closed', v_now)
        ON CONFLICT (operation_id) DO NOTHING;
    EXCEPTION WHEN unique_violation THEN
        -- The token is already bound to another operation.
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END;
    SELECT *
    INTO v_permit
    FROM aap_assets_v1.native_pyramid_read_permits AS permit
    WHERE permit.operation_id = p_operation_id
    FOR UPDATE;
    IF NOT FOUND OR v_permit.token IS DISTINCT FROM p_token THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    IF v_permit.state = 'open' THEN
        UPDATE aap_assets_v1.native_pyramid_read_permits AS permit
        SET state = 'closed', closed_at = v_now
        WHERE permit.operation_id = p_operation_id AND permit.state = 'open';
        IF NOT FOUND THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        END IF;
        -- A signed URL issued before closure must outlive its advertised
        -- lifetime; the frozen allocation policy owns that bound.
        UPDATE aap_assets_v1.native_pyramid_retirements AS retirement
        SET not_before = GREATEST(
                retirement.not_before,
                v_now + aap_assets_v1._native_pyramid_retirement_grace(target.policy)
            ),
            updated_at = v_now
        FROM aap_assets_v1.native_pyramid_retirement_targets AS target
        WHERE retirement.output_id = v_permit.output_id
          AND target.output_id = v_permit.output_id
          AND retirement.closed_at IS NULL;
    END IF;
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
        'operation_id', p_operation_id,
        'token', p_token,
        'closed', true
    ), NULL);
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.complete_native_pyramid_read(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
-- bo0463 already declares this exact signature, so this body supersedes it.
CREATE OR REPLACE FUNCTION aap_assets_v1._native_pyramid_retirement_claimable(p_output uuid)
RETURNS boolean
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1
        FROM public.image_pyramid_outputs AS output
        JOIN aap_assets_v1.native_pyramid_retirements AS retirement
          ON retirement.output_id = output.id
        WHERE output.id = p_output
          AND output.state IN ('building', 'ready', 'retiring')
          AND retirement.closed_at IS NULL
          -- An open read permit is a live storage responsibility; expiry or
          -- process loss alone never proves that physical reads stopped.
          AND NOT EXISTS (
              SELECT 1
              FROM aap_assets_v1.native_pyramid_read_permits AS permit
              WHERE permit.output_id = output.id AND permit.state = 'open'
          )
          AND NOT EXISTS (
              SELECT 1
              FROM aap_assets_v1.image_pyramid_native_attempts AS attempt
              JOIN aap_assets_v1.fixture_native_media_jobs AS fixture
                ON fixture.family = 'image' AND fixture.job_id = attempt.job_id
              WHERE attempt.output_id = output.id
          )
          AND NOT EXISTS (
              SELECT 1
              FROM aap_assets_v1.fixture_media_births AS birth
              WHERE birth.owner_kind = 'pyramid_output' AND birth.owner_id = output.id
          )
          AND NOT EXISTS (
              SELECT 1
              FROM aap_assets_v1.image_pyramid_native_members AS member
              WHERE member.output_id = output.id
                AND (member.state NOT IN ('written', 'verified', 'not_written')
                     OR (member.state IN ('written', 'verified') AND (
                         member.etag IS NULL
                         OR member.etag !~ '^"[^"]+"$'
                         OR pg_catalog.length(member.etag) > 1024
                         OR (member.version_id IS NOT NULL
                             AND pg_catalog.length(member.version_id) > 1024))))
          )
          -- A published inventory is the object history; every listed object
          -- must still have its exact immutable receipt and vice versa.
          AND (
              output.inventory IS NULL
              OR (
                  pg_catalog.jsonb_typeof(output.inventory) = 'object'
                  AND pg_catalog.jsonb_typeof(output.inventory -> 'objects') = 'object'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pg_catalog.jsonb_each(
                          output.inventory -> 'objects'
                      ) AS inventory_object(key, receipt)
                      LEFT JOIN aap_assets_v1.image_pyramid_native_members AS member
                        ON member.output_id = output.id
                       AND member.state = 'verified'
                       AND (output.storage_ref ->> 'prefix') || member.relative_key
                           = inventory_object.key
                       AND member.sha256 = inventory_object.receipt ->> 'sha256'
                       AND pg_catalog.to_jsonb(member.size_bytes)
                           = inventory_object.receipt -> 'size_bytes'
                      WHERE pg_catalog.left(
                                inventory_object.key,
                                pg_catalog.length(output.storage_ref ->> 'prefix')
                            ) IS DISTINCT FROM (output.storage_ref ->> 'prefix')
                         OR NOT aap_assets_v1._media_object_has_only(
                                inventory_object.receipt,
                                ARRAY['sha256', 'size_bytes']
                            )
                         OR NOT (inventory_object.receipt ?& ARRAY['sha256', 'size_bytes'])
                         OR member.operation_id IS NULL
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM aap_assets_v1.image_pyramid_native_members AS member
                      WHERE member.output_id = output.id
                        AND member.state = 'verified'
                        AND NOT (output.inventory -> 'objects' ? (
                            (output.storage_ref ->> 'prefix') || member.relative_key
                        ))
                  )
              )
          )
          -- A member PUT permit is a durable storage-operation barrier.  A
          -- recorded receipt does not prove that an earlier PUT body/client or
          -- a verification GET has closed, so written members never bypass it.
          AND NOT EXISTS (
              SELECT 1
              FROM public.storage_operations AS operation
              WHERE operation.location_id = (
                        SELECT target.location_id
                        FROM aap_assets_v1.native_pyramid_retirement_targets AS target
                        WHERE target.output_id = output.id
                    )
                AND operation.purpose IN (
                    'image_pyramid_' || pg_catalog.replace(output.id::text, '-', ''),
                    'image_pyramid_retirement_' || pg_catalog.replace(output.id::text, '-', '')
                )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM aap_assets_v1.native_pyramid_retirement_pages AS page
              WHERE page.output_id = output.id AND page.state = 'prepared'
          )
          -- The legacy bucket-root lane must never share this exact prefix.
          AND NOT EXISTS (
              SELECT 1
              FROM public.asset_image_garbage AS garbage
              JOIN aap_assets_v1.native_pyramid_retirement_targets AS target
                ON target.output_id = output.id
              WHERE garbage.prefix = target.prefix
          )
          AND NOT EXISTS (
              SELECT 1
              FROM public.image_pyramid_generations AS generation
              WHERE generation.output_id = output.id
                AND (
                    generation.status IN ('pending', 'building')
                    OR (generation.status = 'ready' AND EXISTS (
                        SELECT 1
                        FROM public.image_pyramid_assets AS asset
                        WHERE asset.id = generation.asset_id
                          AND generation.generation IN (
                              asset.active_generation, asset.building_generation
                          )
                    ))
                )
          )
    )
$function$;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.claim_native_pyramid_retirement(
    p_worker_id uuid, p_lease_seconds integer
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_candidate record;
    v_retirement aap_assets_v1.native_pyramid_retirements%ROWTYPE;
    v_target aap_assets_v1.native_pyramid_retirement_targets%ROWTYPE;
    v_output public.image_pyramid_outputs%ROWTYPE;
    v_scope record;
    v_gate record;
    v_lease_expires timestamptz;
    v_token uuid;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_not_before timestamptz;
    v_last_accessed timestamptz;
    v_live boolean;
    v_skip boolean;
    v_output_state text;
    v_ref jsonb;
BEGIN
    IF p_worker_id IS NULL OR p_lease_seconds IS NULL
       OR p_lease_seconds NOT BETWEEN 30 AND 3600 THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    FOR v_candidate IN
        SELECT retirement.output_id
        FROM aap_assets_v1.native_pyramid_retirements AS retirement
        JOIN aap_assets_v1.native_pyramid_retirement_targets AS target
          ON target.output_id = retirement.output_id
        WHERE retirement.closed_at IS NULL
          AND retirement.not_before <= v_now
          AND (retirement.lease_expires_at IS NULL
               OR retirement.lease_expires_at <= v_now)
          AND aap_assets_v1._native_pyramid_retirement_claimable(retirement.output_id)
        ORDER BY retirement.not_before, retirement.output_id
        LIMIT 20
        FOR UPDATE OF retirement SKIP LOCKED
    LOOP
        v_skip := false;
        BEGIN
            SELECT retirement.* INTO v_retirement
            FROM aap_assets_v1.native_pyramid_retirements AS retirement
            WHERE retirement.output_id = v_candidate.output_id
            FOR UPDATE;
            IF NOT FOUND OR v_retirement.closed_at IS NOT NULL THEN
                v_skip := true;
            END IF;
            IF NOT v_skip THEN
                SELECT target.* INTO v_target
                FROM aap_assets_v1.native_pyramid_retirement_targets AS target
                WHERE target.output_id = v_candidate.output_id;
                IF NOT FOUND THEN v_skip := true; END IF;
                IF NOT v_skip THEN
                    SELECT output.* INTO v_output
                    FROM public.image_pyramid_outputs AS output
                    WHERE output.id = v_candidate.output_id
                    FOR UPDATE NOWAIT;
                    IF NOT FOUND THEN v_skip := true; END IF;
                END IF;
            END IF;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            v_skip := true;
        END;
        CONTINUE WHEN v_skip;
        -- A new purpose binding must never redirect this saved allocation.
        v_ref := pg_catalog.jsonb_build_object(
            'schema', 2,
            'purpose', 'media-cache',
            'location_id', v_target.location_id::text,
            'config_revision', v_target.config_revision,
            'binding_generation', v_target.binding_generation,
            'endpoint_url', v_target.endpoint_url,
            'bucket', v_target.bucket,
            'key_prefix', v_target.key_prefix,
            'prefix', v_target.prefix,
            'storage_identity', v_target.storage_identity
        );
        IF v_output.storage_ref IS DISTINCT FROM v_ref THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        CONTINUE WHEN v_output.state NOT IN ('building', 'ready', 'retiring');
        CONTINUE WHEN NOT aap_assets_v1._native_pyramid_retirement_claimable(
            v_target.output_id);
        v_live := false;
        v_skip := false;
        BEGIN
            FOR v_scope IN
                SELECT generation.asset_id, generation.generation,
                       generation.status
                FROM public.image_pyramid_generations AS generation
                WHERE generation.output_id = v_target.output_id
                ORDER BY generation.asset_id, generation.generation
                FOR UPDATE NOWAIT
            LOOP
                IF v_scope.status IN ('pending', 'building') THEN
                    -- A building generation is a retained reference even when
                    -- its lease looks expired: only the owner can settle it.
                    v_live := true;
                ELSIF v_scope.status = 'ready' THEN
                    IF EXISTS (
                        SELECT 1
                        FROM public.image_pyramid_assets AS asset
                        WHERE asset.id = v_scope.asset_id
                          AND v_scope.generation IN (
                              asset.active_generation, asset.building_generation
                          )
                    ) THEN
                        v_live := true;
                    END IF;
                END IF;
            END LOOP;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            v_skip := true;
        END;
        CONTINUE WHEN v_skip OR v_live;
        v_not_before := v_retirement.not_before;
        SELECT generation.last_accessed_at INTO v_last_accessed
        FROM public.image_pyramid_generations AS generation
        WHERE generation.asset_id = v_target.asset_id
          AND generation.generation = v_target.generation;
        IF FOUND AND v_last_accessed IS NOT NULL THEN
            v_not_before := GREATEST(
                v_not_before,
                v_last_accessed
                    + aap_assets_v1._native_pyramid_retirement_grace(v_target.policy)
            );
        END IF;
        IF v_not_before > v_now THEN
            UPDATE aap_assets_v1.native_pyramid_retirements AS retirement
            SET not_before = v_not_before, updated_at = v_now
            WHERE retirement.output_id = v_target.output_id;
            CONTINUE;
        END IF;
        SELECT gate.state, gate.revision INTO v_gate
        FROM public.storage_maintenance_gates AS gate
        WHERE gate.location_id = v_target.location_id
        FOR SHARE;
        IF NOT FOUND OR v_gate.state <> 'active' THEN
            CONTINUE;
        END IF;
        UPDATE public.image_pyramid_outputs AS output
        SET state = 'retiring', updated_at = v_now
        WHERE output.id = v_target.output_id
          AND output.state IN ('building', 'ready');
        IF FOUND AND v_output.state = 'ready' THEN
            -- Stop new reads first, then outlive any URL issued before this fence.
            -- last_accessed_at alone is insufficient because reads throttle it.
            v_not_before := GREATEST(
                v_not_before,
                pg_catalog.clock_timestamp() + pg_catalog.make_interval(
                    secs => (v_target.policy ->> 'url_expiry_seconds')::integer
                )
            );
            UPDATE aap_assets_v1.native_pyramid_retirements AS retirement
            SET not_before = v_not_before, updated_at = pg_catalog.clock_timestamp()
            WHERE retirement.output_id = v_target.output_id;
            CONTINUE;
        ELSIF NOT FOUND THEN
            SELECT output.state INTO v_output_state
            FROM public.image_pyramid_outputs AS output
            WHERE output.id = v_target.output_id;
            CONTINUE WHEN v_output_state IS DISTINCT FROM 'retiring';
        END IF;
        v_token := pg_catalog.gen_random_uuid();
        v_lease_expires := v_now
            + pg_catalog.make_interval(secs => p_lease_seconds);
        UPDATE aap_assets_v1.native_pyramid_retirements AS retirement
        SET lease_owner = p_worker_id, lease_token = v_token,
            lease_expires_at = v_lease_expires, not_before = v_not_before,
            updated_at = v_now
        WHERE retirement.output_id = v_target.output_id
          AND retirement.closed_at IS NULL;
        RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
            'claim', pg_catalog.jsonb_build_object(
                'output_id', v_target.output_id,
                'lease_token', v_token,
                'lease_expires_at', v_lease_expires,
                'storage_ref', v_ref,
                'after_operation_id', v_retirement.after_operation_id
            )
        ), NULL);
    END LOOP;
    RETURN aap_assets_v1._image_reply(
        pg_catalog.jsonb_build_object('claim', NULL), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.claim_native_pyramid_retirement(uuid, integer) FROM PUBLIC;
-- aap-statement-break
-- The exact member receipts a prepared page addresses, in durable cursor order.
-- A written member with a complete exact saved receipt may appear in a page;
-- the separate durable storage-operation barrier still keeps an unsettled
-- verification/write from being retired.  Unknown states and invalid receipts
-- never appear in a page.
CREATE OR REPLACE FUNCTION aap_assets_v1._native_pyramid_retirement_page_items(
    p_output uuid, p_member_ids uuid[]
)
RETURNS jsonb
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
        'operation_id', member.operation_id,
        'relative_key', member.relative_key,
        'sha256', member.sha256,
        'size_bytes', member.size_bytes,
        'etag', member.etag,
        'version_id', member.version_id
    ) ORDER BY requested.ordinality), '[]'::jsonb)
    FROM pg_catalog.unnest(p_member_ids) WITH ORDINALITY AS requested(operation_id, ordinality)
    JOIN aap_assets_v1.image_pyramid_native_members AS member
      ON member.operation_id = requested.operation_id
     AND member.output_id = p_output
     AND member.state IN ('written', 'verified')
     AND member.etag IS NOT NULL
     AND member.etag ~ '^"[^"]+"$'
     AND pg_catalog.length(member.etag) <= 1024
     AND (member.version_id IS NULL
          OR pg_catalog.length(member.version_id) <= 1024)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._native_pyramid_retirement_page_items(uuid, uuid[]) FROM PUBLIC;
-- aap-statement-break
-- A page may include a written member once its complete exact receipt exists;
-- every other state (submitted, unknown) and every invalid receipt still
-- refuses preparation, preserving the unknown-write barrier.  Claimability
-- separately requires the member's durable storage operation to be closed.
CREATE OR REPLACE FUNCTION aap_assets_v1.prepare_native_pyramid_retirement_page(
    p_output_id uuid, p_lease_token uuid, p_page_size integer
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_retirement aap_assets_v1.native_pyramid_retirements%ROWTYPE;
    v_target aap_assets_v1.native_pyramid_retirement_targets%ROWTYPE;
    v_output public.image_pyramid_outputs%ROWTYPE;
    v_page aap_assets_v1.native_pyramid_retirement_pages%ROWTYPE;
    v_member record;
    v_ref jsonb;
    v_member_ids uuid[] := ARRAY[]::uuid[];
    v_after uuid;
    v_page_id uuid;
    v_collected integer := 0;
    v_batch integer;
    v_gate record;
    v_early_skip boolean := false;
    v_missing boolean := false;
    v_busy boolean := false;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_output_id IS NULL OR p_lease_token IS NULL OR p_page_size IS NULL
       OR p_page_size NOT BETWEEN 1 AND 100 THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT retirement.* INTO v_retirement
        FROM aap_assets_v1.native_pyramid_retirements AS retirement
        WHERE retirement.output_id = p_output_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND OR v_retirement.closed_at IS NOT NULL THEN
            v_missing := true;
        END IF;
        IF NOT v_missing THEN
            SELECT target.* INTO v_target
            FROM aap_assets_v1.native_pyramid_retirement_targets AS target
            WHERE target.output_id = p_output_id;
            IF NOT FOUND THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            SELECT output.* INTO v_output
            FROM public.image_pyramid_outputs AS output
            WHERE output.id = p_output_id
            FOR UPDATE NOWAIT;
            IF NOT FOUND THEN v_missing := true; END IF;
        END IF;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        v_busy := true;
    END;
    IF v_busy THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    END IF;
    IF v_missing OR v_retirement.lease_token IS DISTINCT FROM p_lease_token
       OR v_retirement.lease_expires_at IS NULL
       OR v_retirement.lease_expires_at <= v_now THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    IF v_output.state IS DISTINCT FROM 'retiring' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    v_ref := pg_catalog.jsonb_build_object(
        'schema', 2,
        'purpose', 'media-cache',
        'location_id', v_target.location_id::text,
        'config_revision', v_target.config_revision,
        'binding_generation', v_target.binding_generation,
        'endpoint_url', v_target.endpoint_url,
        'bucket', v_target.bucket,
        'key_prefix', v_target.key_prefix,
        'prefix', v_target.prefix,
        'storage_identity', v_target.storage_identity
    );
    IF v_output.storage_ref IS DISTINCT FROM v_ref THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    SELECT page.* INTO v_page
    FROM aap_assets_v1.native_pyramid_retirement_pages AS page
    WHERE page.output_id = p_output_id AND page.state = 'prepared'
    ORDER BY page.prepared_at, page.page_id
    LIMIT 1
    FOR UPDATE;
    IF FOUND THEN
        IF v_page.lease_token IS DISTINCT FROM p_lease_token THEN
            -- A lease timeout alone does not close an unsettled physical page.
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        END IF;
        RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
            'output_id', p_output_id,
            'lease_token', p_lease_token,
            'page_id', v_page.page_id,
            'lease_expires_at', v_retirement.lease_expires_at,
            'after_operation_id', v_page.after_operation_id,
            'items', aap_assets_v1._native_pyramid_retirement_page_items(
                p_output_id, v_page.member_ids),
            'exhausted', v_page.terminal
        ), NULL);
    END IF;
    v_after := v_retirement.after_operation_id;
    LOOP
        v_batch := 0;
        FOR v_member IN
            SELECT member.operation_id, member.state, member.etag, member.version_id
            FROM aap_assets_v1.image_pyramid_native_members AS member
            WHERE member.output_id = p_output_id
              AND (v_after IS NULL OR member.operation_id > v_after)
            ORDER BY member.operation_id
            LIMIT 200
        LOOP
            v_batch := v_batch + 1;
            v_after := v_member.operation_id;
            IF v_member.state NOT IN ('written', 'verified', 'not_written') THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            -- A written member with a complete exact receipt may be paged;
            -- submitted/unknown members and invalid receipts block.  The
            -- durable storage-operation barrier is enforced by claimability.
            IF v_member.state IN ('written', 'verified') THEN
                IF v_member.etag IS NULL
                   OR v_member.etag !~ '^"[^"]+"$'
                   OR pg_catalog.length(v_member.etag) > 1024
                   OR (v_member.version_id IS NOT NULL
                       AND pg_catalog.length(v_member.version_id) > 1024) THEN
                    RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
                END IF;
                v_member_ids := v_member_ids || v_member.operation_id;
                v_collected := v_collected + 1;
                EXIT WHEN v_collected >= p_page_size;
            END IF;
        END LOOP;
        EXIT WHEN v_collected >= p_page_size;
        EXIT WHEN v_batch < 200;
    END LOOP;
    v_page_id := pg_catalog.gen_random_uuid();
    BEGIN
        SELECT gate.state, gate.revision INTO v_gate
        FROM public.storage_maintenance_gates AS gate
        WHERE gate.location_id = v_target.location_id
        FOR SHARE;
        IF NOT FOUND OR v_gate.state <> 'active' THEN
            v_early_skip := true;
        ELSE
            INSERT INTO public.storage_operations(id, location_id, revision, purpose, node_id)
            VALUES (
                v_page_id, v_target.location_id, v_gate.revision,
                'image_pyramid_retirement_'
                    || pg_catalog.replace(p_output_id::text, '-', ''),
                'native-pyramid-retirement:' || v_page_id::text
            );
        END IF;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        v_early_skip := true;
    END;
    IF v_early_skip THEN
        RETURN aap_assets_v1._image_reply(NULL, 'storage_unavailable');
    END IF;
    INSERT INTO aap_assets_v1.native_pyramid_retirement_pages(
        page_id, output_id, lease_token, after_operation_id, gate_revision,
        terminal, member_ids
    ) VALUES (
        v_page_id, p_output_id, p_lease_token, v_after, v_gate.revision,
        v_collected = 0, v_member_ids
    );
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
        'output_id', p_output_id,
        'lease_token', p_lease_token,
        'page_id', v_page_id,
        'lease_expires_at', v_retirement.lease_expires_at,
        'after_operation_id', v_after,
        'items', aap_assets_v1._native_pyramid_retirement_page_items(
            p_output_id, v_member_ids),
        'exhausted', v_collected = 0
    ), NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
WHEN unique_violation THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.prepare_native_pyramid_retirement_page(uuid, uuid, integer) FROM PUBLIC;
-- aap-statement-break
-- Recording a known, fully drained PUT result advances its already-admitted
-- immutable member operation to a durable written receipt.  This is physical-
-- fact registration, not new I/O authorization: it never rechecks the writer
-- lease, source or gate, and never demands a caller-invented current version.
-- The member's storage-operation permit stays open; it is not proof that a PUT
-- body/client or a later verification GET has closed.
CREATE OR REPLACE FUNCTION aap_assets_v1.write_native_pyramid_member(
    p_operation uuid,p_etag text,p_version text
) RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_member aap_assets_v1.image_pyramid_native_members%ROWTYPE;
    v_scope jsonb;
    v_key text;
    v_key_digest bytea;
BEGIN
    -- A literal "null" version is a real recorded S3 version id.  It is
    -- stored verbatim and stays distinct from an omitted (SQL NULL) version;
    -- raw HTTP receipts are never normalized or invented here.
    IF p_etag IS NULL OR p_etag='' OR pg_catalog.length(p_etag)>1024
       OR (p_version IS NOT NULL AND (p_version='' OR pg_catalog.length(p_version)>1024)) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    SELECT * INTO v_member FROM aap_assets_v1.image_pyramid_native_members
    WHERE operation_id=p_operation FOR UPDATE;
    IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    IF v_member.state='written' THEN
        IF v_member.etag IS DISTINCT FROM p_etag OR v_member.version_id IS DISTINCT FROM p_version THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
            'output_id',v_member.output_id,'operation_id',v_member.operation_id,
            'relative_key',v_member.relative_key,'sha256',v_member.sha256,
            'size_bytes',v_member.size_bytes,'state',v_member.state
        ), NULL);
    END IF;
    IF v_member.state<>'submitted' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    UPDATE aap_assets_v1.image_pyramid_native_members
    SET state='written',etag=p_etag,version_id=p_version,updated_at=pg_catalog.clock_timestamp()
    WHERE operation_id=p_operation AND state='submitted';
    IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    v_scope := aap_assets_v1._native_pyramid_fixture_scope(v_member.output_id);
    IF v_scope IS NOT NULL THEN
        SELECT (storage_ref ->> 'prefix') || v_member.relative_key INTO v_key
        FROM public.image_pyramid_outputs WHERE id=v_member.output_id;
        v_key_digest := pg_catalog.sha256(pg_catalog.convert_to(v_key,'UTF8'));
        INSERT INTO aap_assets_v1.fixture_media_versions(
            id,run_id,dataset_id,item_id,task_id,execution_kind,execution_id,execution_token,
            owner_kind,owner_id,object_key,key_digest,sha256,size_bytes,state,etag,version_id
        ) VALUES (
            p_operation,(v_scope ->> 'run_id')::uuid,(v_scope ->> 'dataset_id')::uuid,
            (v_scope ->> 'item_id')::uuid,(v_scope ->> 'task_id')::uuid,'pyramid',
            (v_scope ->> 'job_id')::uuid,(v_scope ->> 'lease_token')::uuid,
            'pyramid_output',v_member.output_id,v_key,
            v_key_digest,v_member.sha256,v_member.size_bytes,'written',p_etag,p_version
        );
    END IF;
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
        'output_id',v_member.output_id,'operation_id',v_member.operation_id,
        'relative_key',v_member.relative_key,'sha256',v_member.sha256,
        'size_bytes',v_member.size_bytes,'state','written'
    ), NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.write_native_pyramid_member(uuid, text, text) FROM PUBLIC;
-- aap-statement-break
-- Written-member physical closure.  The existing abandon outcome gains one
-- bounded value: "written" is accepted only for an already-written member with
-- a complete exact receipt, retains that receipt, and closes only the member's
-- own open storage operation.  It is idempotent, never closes unknown, and
-- never fabricates a receipt or absence.  The durable operation barrier is
-- unchanged: nothing is retirable while the permit is still open.
CREATE OR REPLACE FUNCTION aap_assets_v1.abandon_native_pyramid_member(
    p_operation uuid, p_state text
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_member aap_assets_v1.image_pyramid_native_members%ROWTYPE;
    v_location_id uuid;
BEGIN
    IF p_operation IS NULL OR p_state IS NULL
       OR p_state NOT IN ('written', 'not_written', 'unknown') THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    SELECT * INTO v_member FROM aap_assets_v1.image_pyramid_native_members
    WHERE operation_id=p_operation FOR UPDATE;
    IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    IF p_state='written' THEN
        -- Only the recorded written receipt can be physically closed.  An
        -- unknown member, an unsubmitted reservation or an already verified
        -- member has no such settled outcome.
        IF v_member.state<>'written'
           OR v_member.etag IS NULL
           OR v_member.etag !~ '^"[^"]+"$'
           OR pg_catalog.length(v_member.etag)>1024
           OR (v_member.version_id IS NOT NULL
               AND pg_catalog.length(v_member.version_id)>1024) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        -- Close exactly this member's admitted operation; an already closed
        -- permit makes the repeat a successful no-op.
        SELECT (output.storage_ref ->> 'location_id')::uuid INTO v_location_id
        FROM public.image_pyramid_outputs AS output
        WHERE output.id=v_member.output_id;
        DELETE FROM public.storage_operations AS operation
        WHERE operation.id=v_member.operation_permit
          AND operation.location_id=v_location_id
          AND operation.purpose = (
                'image_pyramid_'
                    || pg_catalog.replace(v_member.output_id::text, '-', '')
          );
        RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
            'output_id',v_member.output_id,'operation_id',v_member.operation_id,
            'relative_key',v_member.relative_key,'sha256',v_member.sha256,
            'size_bytes',v_member.size_bytes,'state','written'
        ), NULL);
    END IF;
    IF v_member.state IN ('written','verified') THEN
        RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
            'output_id',v_member.output_id,'operation_id',v_member.operation_id,
            'relative_key',v_member.relative_key,'sha256',v_member.sha256,
            'size_bytes',v_member.size_bytes,'state',v_member.state
        ), NULL);
    END IF;
    IF v_member.state IN ('not_written','unknown') THEN
        RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
            'output_id',v_member.output_id,'operation_id',v_member.operation_id,
            'relative_key',v_member.relative_key,'sha256',v_member.sha256,
            'size_bytes',v_member.size_bytes,'state',v_member.state
        ), NULL);
    END IF;
    UPDATE aap_assets_v1.image_pyramid_native_members
    SET state=p_state,updated_at=pg_catalog.clock_timestamp()
    WHERE operation_id=p_operation AND state IN ('prepared','submitted');
    IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    IF p_state='not_written' THEN
        DELETE FROM public.storage_operations WHERE id=v_member.operation_permit;
    END IF;
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
        'output_id',v_member.output_id,'operation_id',v_member.operation_id,
        'relative_key',v_member.relative_key,'sha256',v_member.sha256,
        'size_bytes',v_member.size_bytes,'state',p_state
    ), NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.abandon_native_pyramid_member(uuid, text) FROM PUBLIC;
-- aap-statement-break
-- A verification is the success outcome of the same durable member operation.
-- Without its open permit there is no admitted verification left to settle, so
-- a closed written member can never be reopened as verified.  Replayed verified
-- results stay idempotent.
CREATE OR REPLACE FUNCTION aap_assets_v1.verify_native_pyramid_member(p_operation uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_member aap_assets_v1.image_pyramid_native_members%ROWTYPE;
    v_location_id uuid;
BEGIN
    SELECT * INTO v_member FROM aap_assets_v1.image_pyramid_native_members
    WHERE operation_id=p_operation FOR UPDATE;
    IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    IF v_member.state='verified' THEN
        RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
            'output_id',v_member.output_id,'operation_id',v_member.operation_id,
            'relative_key',v_member.relative_key,'sha256',v_member.sha256,
            'size_bytes',v_member.size_bytes,'state',v_member.state
        ), NULL);
    END IF;
    IF v_member.state<>'written' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    SELECT (output.storage_ref ->> 'location_id')::uuid INTO v_location_id
    FROM public.image_pyramid_outputs AS output
    WHERE output.id=v_member.output_id;
    IF NOT EXISTS (
        SELECT 1
        FROM public.storage_operations AS operation
        WHERE operation.id=v_member.operation_permit
          AND operation.location_id=v_location_id
          AND operation.purpose = (
                'image_pyramid_'
                    || pg_catalog.replace(v_member.output_id::text, '-', '')
          )
    ) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    UPDATE aap_assets_v1.image_pyramid_native_members
    SET state='verified',updated_at=pg_catalog.clock_timestamp()
    WHERE operation_id=p_operation AND state='written';
    IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    UPDATE aap_assets_v1.fixture_media_versions SET state='verified'
    WHERE id=p_operation AND state='written';
    DELETE FROM public.storage_operations WHERE id=v_member.operation_permit;
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
        'output_id',v_member.output_id,'operation_id',v_member.operation_id,
        'relative_key',v_member.relative_key,'sha256',v_member.sha256,
        'size_bytes',v_member.size_bytes,'state','verified'
    ), NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.verify_native_pyramid_member(uuid) FROM PUBLIC;
-- aap-statement-break
-- Fixture control resolves an already-authorized cache key through the fixture
-- owner.  Verified fixture pyramid members resolve only through their exact
-- saved output prefix, fixture run/job/claim attempt and real member receipt;
-- there is no prefix inference, ordinary fallback or table grant.
CREATE OR REPLACE FUNCTION aap_assets_v1.resolve_fixture_native_cache(p_key text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_suite uuid; v_write record; v_origin text;
BEGIN
    SELECT principal.suite_id INTO v_suite FROM aap_assets_v1.fixture_native_resolver_principals AS principal
    JOIN pg_catalog.pg_roles AS role ON role.oid=principal.principal_oid
    WHERE principal.state='active' AND principal.principal_name=role.rolname AND role.rolname=SESSION_USER;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_native_resolver_required'; END IF;
    IF p_key IS NULL OR p_key='' OR pg_catalog.char_length(p_key)>2048 THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='fixture_native_output_key_invalid';
    END IF;
    BEGIN
        SELECT candidate.* INTO STRICT v_write FROM (
            SELECT write.id,write.run_id,write.state
            FROM aap_assets_v1.fixture_native_cache_writes AS write
            WHERE write.object_key=p_key
            UNION ALL
            SELECT version.id,version.run_id,version.state
            FROM aap_assets_v1.fixture_media_versions AS version
            JOIN aap_assets_v1.fixture_independent_video_attempts AS attempt
              ON attempt.delivery_id=version.execution_id AND attempt.output_id=version.owner_id
             AND version.execution_kind='independent' AND version.owner_kind='video_output'
            JOIN aap_assets_v1.fixture_independent_media_births AS birth
              ON birth.delivery_id=attempt.delivery_id AND birth.run_id=version.run_id
            JOIN aap_assets_v1.fixture_independent_media_executions AS execution
              ON execution.delivery_id=attempt.delivery_id AND execution.token=version.execution_token
             AND execution.state='settled'
            JOIN public.video_derivative_outputs AS output
              ON output.id=attempt.output_id AND output.lease_token=attempt.lease_token
            JOIN public.dataset_items AS item
              ON item.id=output.dataset_item_id AND item.video_output_id=output.id
            WHERE version.object_key=p_key AND output.state='ready'
              AND output.result->'video'->>'poster_frame_path'=p_key
              AND output.inventory->'objects'->p_key=pg_catalog.jsonb_build_object(
                  'sha256',version.sha256,'size_bytes',version.size_bytes)
            UNION ALL
            -- Fixture Native pyramid members are addressed by their exact
            -- saved output prefix and real member receipt; the fixture job,
            -- claim attempt and version row must prove the same owner scope.
            SELECT version.id,version.run_id,version.state
            FROM aap_assets_v1.fixture_media_versions AS version
            JOIN public.image_pyramid_outputs AS output
              ON output.id=version.owner_id AND output.state='ready'
            JOIN aap_assets_v1.image_pyramid_native_attempts AS pyramid_attempt
              ON pyramid_attempt.output_id=output.id
             AND pyramid_attempt.job_id=version.execution_id
             AND pyramid_attempt.lease_token=version.execution_token
             AND pyramid_attempt.asset_id=output.asset_id
             AND pyramid_attempt.generation=output.generation
            JOIN aap_assets_v1.fixture_native_media_jobs AS job
              ON job.family='image' AND job.job_id=pyramid_attempt.job_id
             AND job.run_id=version.run_id AND job.job_kind='pyramid'
             AND version.run_id=job.run_id
             AND version.dataset_id IS NOT DISTINCT FROM job.dataset_id
             AND version.item_id IS NOT DISTINCT FROM job.item_id
             AND version.task_id IS NOT DISTINCT FROM
                 CASE WHEN job.owner_kind='task' THEN job.owner_id END
            JOIN aap_assets_v1.image_pyramid_native_members AS member
              ON member.operation_id=version.id AND member.output_id=output.id
             AND member.state='verified'
             AND version.object_key=(output.storage_ref ->> 'prefix')
                 || member.relative_key
             AND version.sha256=member.sha256
             AND version.size_bytes=member.size_bytes
             AND version.etag=member.etag
             AND version.version_id IS NOT DISTINCT FROM member.version_id
            WHERE version.object_key=p_key
              AND version.execution_kind='pyramid'
              AND version.owner_kind='pyramid_output'
              AND version.state='verified'
              AND version.key_digest=pg_catalog.sha256(pg_catalog.convert_to(
                  version.object_key,'UTF8'))
              AND output.inventory->'objects'->version.object_key
                  =pg_catalog.jsonb_build_object(
                      'sha256',version.sha256,'size_bytes',version.size_bytes)
              AND aap_assets_v1._native_pyramid_retirement_target_valid(
                  output.storage_ref,output.id)
        ) AS candidate
        JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=candidate.run_id
          AND COALESCE(run.parent_run_id,run.id)=v_suite;
    EXCEPTION WHEN no_data_found THEN
        IF EXISTS (
            SELECT 1 FROM public.video_derivative_outputs AS output
            JOIN aap_assets_v1.fixture_media_births AS birth
              ON birth.owner_kind='video_output' AND birth.owner_id=output.id AND birth.generation=0
            JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=birth.run_id
              AND COALESCE(run.parent_run_id,run.id)=v_suite
            WHERE output.result->'video'->>'poster_frame_path'=p_key
        ) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_output_unavailable';
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.image_pyramid_outputs AS output
            JOIN aap_assets_v1.fixture_media_births AS birth
              ON birth.owner_kind='pyramid_output' AND birth.owner_id=output.id
             AND birth.generation=0
            JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=birth.run_id
              AND COALESCE(run.parent_run_id,run.id)=v_suite
            JOIN aap_assets_v1.image_pyramid_native_members AS member
              ON member.output_id=output.id
             AND (output.storage_ref ->> 'prefix') || member.relative_key=p_key
        ) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_output_unavailable';
        END IF;
        RETURN NULL;
    WHEN too_many_rows THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_output_ambiguous';
    END;
    SELECT reader.proxy_origin INTO v_origin FROM yoao_fixture_v1.fixture_runs AS run
    JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=COALESCE(run.parent_run_id,run.id)
    JOIN aap_assets_v1.fixture_native_read_principals AS reader
      ON reader.run_id=run.id AND reader.state='active'
    WHERE run.id=v_write.run_id AND root.id=v_suite AND run.state='open' AND root.state='open';
    IF NOT FOUND OR v_write.state<>'verified' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_output_unavailable';
    END IF;
    RETURN pg_catalog.jsonb_build_object('run_id',v_write.run_id,'write_id',v_write.id,'proxy_origin',v_origin);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.resolve_fixture_native_cache(text) FROM PUBLIC;
-- aap-statement-break
-- The ordinary grace-based collector owns only rows the Images owner never
-- admitted.  A Native generation is admitted before output allocation by its
-- produced work generation; a saved Native output keeps its immutable attempt
-- and retirement target even after the generation and job rows disappear.
-- Fixture-owned media keeps its own exclusion, and a prefix shape is never an
-- admission proof.
CREATE FUNCTION aap_assets_v1.legacy_pyramid_gc_eligible(
    p_kind text,p_owner_id uuid,p_generation integer
) RETURNS boolean LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF p_kind IS NULL OR p_owner_id IS NULL OR p_generation IS NULL
       OR p_kind NOT IN ('pyramid_generation','pyramid_output')
       OR p_generation < 0
       OR (p_kind='pyramid_generation') <> (p_generation > 0) THEN
        RAISE EXCEPTION USING ERRCODE='22023',
            MESSAGE='legacy_pyramid_gc_kind_invalid';
    END IF;
    IF aap_assets_v1.fixture_media_ordinary_gc(
        p_kind,p_owner_id,p_generation
    ) IS NOT TRUE THEN
        RETURN false;
    END IF;
    IF p_kind='pyramid_generation' THEN
        RETURN NOT EXISTS (
            SELECT 1
            FROM public.image_pyramid_generations AS generation
            WHERE generation.asset_id=p_owner_id
              AND generation.generation=p_generation
              AND generation.job_id IS NOT NULL
        ) AND NOT EXISTS (
            SELECT 1
            FROM public.asset_image_jobs AS job
            WHERE job.kind='pyramid'
              AND job.pyramid_asset_id=p_owner_id
              AND job.pyramid_generation=p_generation
        ) AND NOT EXISTS (
            SELECT 1
            FROM aap_assets_v1.fixture_native_pyramid_generations AS binding
            WHERE binding.asset_id=p_owner_id
              AND binding.generation=p_generation
        );
    END IF;
    RETURN NOT EXISTS (
        SELECT 1
        FROM aap_assets_v1.image_pyramid_native_attempts AS attempt
        WHERE attempt.output_id=p_owner_id
    ) AND NOT EXISTS (
        SELECT 1
        FROM aap_assets_v1.native_pyramid_retirement_targets AS target
        WHERE target.output_id=p_owner_id
    ) AND NOT EXISTS (
        SELECT 1
        FROM public.image_pyramid_generations AS generation
        WHERE generation.output_id=p_owner_id
          AND generation.job_id IS NOT NULL
    ) AND NOT EXISTS (
        SELECT 1
        FROM public.image_pyramid_outputs AS output
        JOIN aap_assets_v1.fixture_native_pyramid_generations AS binding
          ON binding.asset_id=output.asset_id
         AND binding.generation=output.generation
        WHERE output.id=p_owner_id
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.legacy_pyramid_gc_eligible(text,uuid,integer) FROM PUBLIC;
-- aap-statement-break

DO $owner$
DECLARE
    v_owner name;
    v_signature text;
    v_grant record;
BEGIN
    SELECT pg_catalog.pg_get_userbyid(proowner) INTO STRICT v_owner
    FROM pg_catalog.pg_proc
    WHERE oid =
          'aap_assets_v1.read_image_generation(uuid,uuid,uuid,integer,text)'::regprocedure;
    EXECUTE pg_catalog.format(
        'ALTER TABLE aap_assets_v1.native_pyramid_read_permits OWNER TO %I', v_owner
    );
    FOREACH v_signature IN ARRAY ARRAY[
        '_native_pyramid_output_fixture_owned(uuid)',
        '_image_saved_output_target(uuid,integer,uuid,boolean)',
        '_image_saved_output_prefix(uuid,integer,uuid,boolean)',
        '_image_pyramid_projection(jsonb,integer,text)',
        'allocate_native_pyramid_output(uuid,bigint,uuid,uuid,integer)',
        'guard_native_pyramid_read_permit()',
        'begin_native_pyramid_read(uuid,uuid,uuid,integer,text,uuid,uuid)',
        'complete_native_pyramid_read(uuid,uuid)',
        '_native_pyramid_retirement_claimable(uuid)',
        'claim_native_pyramid_retirement(uuid,integer)',
        'legacy_pyramid_gc_eligible(text,uuid,integer)'
    ] LOOP
        EXECUTE pg_catalog.format(
            'ALTER FUNCTION aap_assets_v1.%s OWNER TO %I', v_signature, v_owner
        );
    END LOOP;
    -- Control owns exactly the same non-PUBLIC EXECUTE set as the existing
    -- generation read; no worker or cleanup role gains read permits.
    FOR v_grant IN
        SELECT role.rolname, acl.is_grantable
        FROM pg_catalog.pg_proc AS procedure
        CROSS JOIN LATERAL pg_catalog.aclexplode(procedure.proacl) AS acl
        JOIN pg_catalog.pg_roles AS role ON role.oid = acl.grantee
        WHERE procedure.oid =
              'aap_assets_v1.read_image_generation(uuid,uuid,uuid,integer,text)'::regprocedure
          AND acl.privilege_type = 'EXECUTE'
          AND acl.grantee <> procedure.proowner
    LOOP
        FOREACH v_signature IN ARRAY ARRAY[
            'begin_native_pyramid_read(uuid,uuid,uuid,integer,text,uuid,uuid)',
            'complete_native_pyramid_read(uuid,uuid)'
        ] LOOP
            EXECUTE pg_catalog.format(
                'GRANT EXECUTE ON FUNCTION aap_assets_v1.%s TO %I%s',
                v_signature, v_grant.rolname,
                CASE WHEN v_grant.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END
            );
        END LOOP;
    END LOOP;
    -- Cleanup keeps exactly its existing claim/page grant set.
    FOR v_grant IN
        SELECT role.rolname, acl.is_grantable
        FROM pg_catalog.pg_proc AS procedure
        CROSS JOIN LATERAL pg_catalog.aclexplode(procedure.proacl) AS acl
        JOIN pg_catalog.pg_roles AS role ON role.oid = acl.grantee
        WHERE procedure.oid =
              'aap_assets_v1.prepare_native_pyramid_retirement_page(uuid,uuid,integer)'::regprocedure
          AND acl.privilege_type = 'EXECUTE'
          AND acl.grantee <> procedure.proowner
    LOOP
        EXECUTE pg_catalog.format(
            'GRANT EXECUTE ON FUNCTION aap_assets_v1.claim_native_pyramid_retirement(uuid,integer) TO %I%s',
            v_grant.rolname,
            CASE WHEN v_grant.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END
        );
    END LOOP;
END;
$owner$;
