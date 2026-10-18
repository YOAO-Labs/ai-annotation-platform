-- Worker-only image leases, source-bound publication and bounded backfill.
-- Object IO stays outside these short caller-owned database transactions.
CREATE FUNCTION aap_assets_v1._image_exec_outcome(
    p_job_id uuid, p_generation bigint, p_outcome text
)
RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_job_id IS NULL OR p_generation IS NULL OR p_generation < 1
       OR p_outcome NOT IN (
            'published', 'replayed', 'failed', 'lease_lost',
            'source_changed', 'dependency_changed'
       ) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    RETURN aap_assets_v1._image_reply(
        pg_catalog.jsonb_build_object(
            'job_id', p_job_id,
            'generation', p_generation,
            'outcome', p_outcome
        ),
        NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_exec_outcome(uuid, bigint, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._image_exec_integer_valid(
    p_value jsonb, p_min bigint, p_max bigint
)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_text text;
    v_value numeric;
BEGIN
    IF p_min IS NULL OR p_max IS NULL OR p_min > p_max
       OR pg_catalog.jsonb_typeof(p_value) IS DISTINCT FROM 'number' THEN
        RETURN false;
    END IF;
    v_text := p_value #>> '{}';
    IF v_text !~ '^(0|[1-9][0-9]*)$' THEN RETURN false; END IF;
    v_value := v_text::numeric;
    RETURN v_value BETWEEN p_min AND p_max;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_exec_integer_valid(jsonb, bigint, bigint) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._image_exec_identity_equal(p_left jsonb, p_right jsonb)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT aap_assets_v1._media_object_identity_valid(p_left)
       OR NOT aap_assets_v1._media_object_identity_valid(p_right) THEN
        RETURN false;
    END IF;
    RETURN p_left ->> 'etag' IS NOT DISTINCT FROM p_right ->> 'etag'
       AND (p_left ->> 'byte_size')::bigint
            IS NOT DISTINCT FROM (p_right ->> 'byte_size')::bigint
       AND NULLIF(p_left ->> 'version_id', '')
            IS NOT DISTINCT FROM NULLIF(p_right ->> 'version_id', '');
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_exec_identity_equal(jsonb, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._image_exec_legacy_identity(p_identity jsonb)
RETURNS text
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_legacy text;
BEGIN
    IF NOT aap_assets_v1._media_object_identity_valid(p_identity) THEN RETURN NULL; END IF;
    v_legacy := 'etag:' || (p_identity ->> 'etag')
        || ':bytes:' || (p_identity ->> 'byte_size');
    IF COALESCE(p_identity ->> 'version_id', '') <> '' THEN
        v_legacy := v_legacy || ':version:' || (p_identity ->> 'version_id');
    END IF;
    IF pg_catalog.char_length(v_legacy) <= 255 THEN RETURN v_legacy; END IF;
    RETURN 'sha256:' || pg_catalog.encode(
        pg_catalog.sha256(pg_catalog.convert_to(p_identity::text, 'UTF8')),
        'hex'
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_exec_legacy_identity(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._image_exec_manifest_valid(
    p_manifest jsonb, p_generation bigint, p_source_sha256 text, p_policy jsonb
)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_width integer;
    v_height integer;
    v_level integer := 0;
    v_scale bigint := 1;
    v_level_width integer;
    v_level_height integer;
    v_columns integer;
    v_rows integer;
    v_total_tiles bigint := 0;
    v_expected_overview_width integer := 0;
    v_expected_overview_height integer := 0;
    v_levels jsonb;
    v_entry jsonb;
    v_overview jsonb;
BEGIN
    IF NOT aap_assets_v1._media_object_has_only(
        p_manifest,
        ARRAY[
            'schema', 'generation', 'sourceFingerprint', 'normalizationVersion',
            'width', 'height', 'tileSize', 'overlap', 'format', 'levels', 'overview'
        ]
    ) OR p_manifest -> 'schema' IS DISTINCT FROM '"aap-image-pyramid/v1"'::jsonb
       OR NOT aap_assets_v1._image_exec_integer_valid(
            p_manifest -> 'generation', 1, 2147483647
       ) OR (p_manifest ->> 'generation')::bigint <> p_generation
       OR pg_catalog.jsonb_typeof(p_manifest -> 'sourceFingerprint') IS DISTINCT FROM 'string'
       OR p_manifest ->> 'sourceFingerprint' IS DISTINCT FROM 'sha256:' || p_source_sha256
       OR p_manifest ->> 'normalizationVersion'
            IS DISTINCT FROM p_policy ->> 'normalization_version'
       OR p_manifest -> 'tileSize' IS DISTINCT FROM '512'::jsonb
       OR p_manifest -> 'overlap' IS DISTINCT FROM '1'::jsonb
       OR p_manifest -> 'format' IS DISTINCT FROM '"webp"'::jsonb
       OR NOT aap_assets_v1._image_exec_integer_valid(
            p_manifest -> 'width', 1, 2147483647
       ) OR NOT aap_assets_v1._image_exec_integer_valid(
            p_manifest -> 'height', 1, 2147483647
       ) THEN
        RETURN false;
    END IF;
    v_width := (p_manifest ->> 'width')::integer;
    v_height := (p_manifest ->> 'height')::integer;
    IF v_width > (p_policy ->> 'max_dimension')::integer
       OR v_height > (p_policy ->> 'max_dimension')::integer
       OR v_width::numeric * v_height::numeric > (p_policy ->> 'max_pixels')::numeric THEN
        RETURN false;
    END IF;
    v_levels := p_manifest -> 'levels';
    IF pg_catalog.jsonb_typeof(v_levels) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(v_levels) NOT BETWEEN 1 AND 32 THEN
        RETURN false;
    END IF;
    WHILE true LOOP
        v_level_width := GREATEST(1, ((v_width::bigint + v_scale - 1) / v_scale)::integer);
        v_level_height := GREATEST(1, ((v_height::bigint + v_scale - 1) / v_scale)::integer);
        v_columns := (v_level_width + 511) / 512;
        v_rows := (v_level_height + 511) / 512;
        v_entry := v_levels -> v_level;
        IF v_entry IS NULL
           OR NOT aap_assets_v1._media_object_has_only(
                v_entry, ARRAY['level', 'scaleFactor', 'width', 'height', 'columns', 'rows']
           )
           OR NOT aap_assets_v1._image_exec_integer_valid(v_entry -> 'level', 0, 2147483647)
           OR (v_entry ->> 'level')::integer <> v_level
           OR NOT aap_assets_v1._image_exec_integer_valid(v_entry -> 'scaleFactor', 1, 2147483647)
           OR (v_entry ->> 'scaleFactor')::integer <> v_scale
           OR NOT aap_assets_v1._image_exec_integer_valid(v_entry -> 'width', 1, 2147483647)
           OR (v_entry ->> 'width')::integer <> v_level_width
           OR NOT aap_assets_v1._image_exec_integer_valid(v_entry -> 'height', 1, 2147483647)
           OR (v_entry ->> 'height')::integer <> v_level_height
           OR NOT aap_assets_v1._image_exec_integer_valid(v_entry -> 'columns', 1, 2147483647)
           OR (v_entry ->> 'columns')::integer <> v_columns
           OR NOT aap_assets_v1._image_exec_integer_valid(v_entry -> 'rows', 1, 2147483647)
           OR (v_entry ->> 'rows')::integer <> v_rows THEN
            RETURN false;
        END IF;
        v_total_tiles := v_total_tiles + v_columns::bigint * v_rows::bigint;
        IF v_expected_overview_width = 0
           AND v_level_width <= 512 AND v_level_height <= 512 THEN
            v_expected_overview_width := v_level_width;
            v_expected_overview_height := v_level_height;
        END IF;
        EXIT WHEN v_level_width = 1 AND v_level_height = 1;
        v_level := v_level + 1;
        v_scale := v_scale * 2;
        IF v_level >= 32 THEN RETURN false; END IF;
    END LOOP;
    IF pg_catalog.jsonb_array_length(v_levels) <> v_level + 1
       OR v_total_tiles > (p_policy ->> 'max_tiles')::bigint THEN
        RETURN false;
    END IF;
    v_overview := p_manifest -> 'overview';
    IF NOT aap_assets_v1._media_object_has_only(
        v_overview, ARRAY['width', 'height', 'contentDigest']
    ) OR NOT aap_assets_v1._image_exec_integer_valid(
            v_overview -> 'width', 1, 2147483647
       ) OR (v_overview ->> 'width')::integer <> v_expected_overview_width
       OR NOT aap_assets_v1._image_exec_integer_valid(
            v_overview -> 'height', 1, 2147483647
       ) OR (v_overview ->> 'height')::integer <> v_expected_overview_height
       OR pg_catalog.jsonb_typeof(v_overview -> 'contentDigest') IS DISTINCT FROM 'string'
       OR v_overview ->> 'contentDigest' !~ '^sha256:[0-9a-f]{64}$' THEN
        RETURN false;
    END IF;
    RETURN true;
EXCEPTION
    WHEN invalid_text_representation OR numeric_value_out_of_range
       OR division_by_zero THEN
        RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_exec_manifest_valid(jsonb, bigint, text, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.claim_image_media(
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
            progress = CASE WHEN job.kind = 'image_backfill'
                THEN job.progress - 'pending_batch' ELSE '{}'::jsonb END,
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
REVOKE ALL ON FUNCTION aap_assets_v1.claim_image_media(uuid, uuid, integer, text[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.heartbeat_image_media(
    p_job_id uuid, p_generation bigint, p_lease_token uuid, p_lease_seconds integer
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_job public.asset_image_jobs%ROWTYPE;
    v_asset public.image_pyramid_assets%ROWTYPE;
    v_generation public.image_pyramid_generations%ROWTYPE;
    v_spec jsonb;
    v_lease_seconds integer;
    v_expires timestamptz;
BEGIN
    IF p_job_id IS NULL OR p_generation IS NULL OR p_generation < 1
       OR p_lease_token IS NULL OR p_lease_seconds IS NULL
       OR p_lease_seconds NOT BETWEEN 30 AND 2147483647 THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT job.* INTO v_job FROM public.asset_image_jobs AS job
        WHERE job.job_id = p_job_id FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    END;
    IF NOT FOUND OR v_job.status <> 'running'
       OR v_job.generation <> p_generation
       OR v_job.lease_token IS DISTINCT FROM p_lease_token
       OR v_job.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    v_spec := aap_assets_v1._image_spec(v_job.spec);
    IF v_spec IS NULL OR v_spec IS DISTINCT FROM v_job.spec THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    v_lease_seconds := GREATEST(
        p_lease_seconds, (v_spec #>> '{policy,lease_seconds}')::integer
    );
    v_expires := pg_catalog.clock_timestamp()
        + pg_catalog.make_interval(secs => v_lease_seconds);
    IF v_job.kind = 'pyramid' THEN
        BEGIN
            SELECT asset.* INTO v_asset
            FROM public.image_pyramid_assets AS asset
            WHERE asset.id = v_job.pyramid_asset_id
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        END;
        IF NOT FOUND OR v_asset.building_generation IS DISTINCT FROM v_job.pyramid_generation THEN
            RETURN aap_assets_v1._image_reply(NULL, 'dependency_changed');
        END IF;
        BEGIN
            SELECT generation.* INTO v_generation
            FROM public.image_pyramid_generations AS generation
            WHERE generation.asset_id = v_job.pyramid_asset_id
              AND generation.generation = v_job.pyramid_generation
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        END;
        IF NOT FOUND OR v_generation.job_id IS DISTINCT FROM p_job_id
           OR v_generation.job_generation IS DISTINCT FROM p_generation
           OR v_generation.lease_token IS DISTINCT FROM p_lease_token
           OR v_generation.lease_expires_at <= pg_catalog.clock_timestamp()
           OR v_generation.status <> 'building' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
        END IF;
        UPDATE public.image_pyramid_generations AS generation
        SET lease_expires_at = v_expires, updated_at = pg_catalog.clock_timestamp()
        WHERE generation.asset_id = v_job.pyramid_asset_id
          AND generation.generation = v_job.pyramid_generation;
    END IF;
    UPDATE public.asset_image_jobs AS job
    SET lease_expires_at = v_expires, updated_at = pg_catalog.clock_timestamp()
    WHERE job.job_id = p_job_id;
    RETURN aap_assets_v1._image_reply(
        pg_catalog.jsonb_build_object(
            'job_id', p_job_id,
            'generation', p_generation,
            'lease_token', p_lease_token,
            'active', true,
            'lease_expires_at', v_expires
        ), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.heartbeat_image_media(uuid, bigint, uuid, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fail_image_media(
    p_job_id uuid, p_generation bigint, p_lease_token uuid,
    p_error_code text, p_error_detail text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_candidate public.asset_image_jobs%ROWTYPE;
    v_job public.asset_image_jobs%ROWTYPE;
    v_asset public.image_pyramid_assets%ROWTYPE;
    v_generation public.image_pyramid_generations%ROWTYPE;
    v_source_reply jsonb;
    v_source jsonb;
    v_source_code text;
    v_spec jsonb;
    v_prefix text;
    v_reason text := 'attempt_abandoned';
    v_outcome text := 'failed';
BEGIN
    IF p_job_id IS NULL OR p_generation IS NULL OR p_generation < 1
       OR p_lease_token IS NULL
       OR p_error_code IS NULL OR pg_catalog.char_length(p_error_code) NOT BETWEEN 1 AND 64
       OR p_error_code NOT IN (
            'invalid_request', 'invalid_response', 'storage_unavailable',
            'account_not_active', 'account_busy', 'invalid_account_role',
            'source_missing', 'source_changed', 'source_busy', 'source_not_ready',
            'task_not_visible', 'task_busy', 'permission_denied', 'not_image',
            'asset_missing', 'operation_conflict', 'operation_busy', 'lease_lost',
            'dependency_changed', 'resource_budget_exceeded', 'object_missing',
            'owner_changed', 'unsupported_dimensions', 'resource_limit',
            'invalid_coordinate', 'timeout', 'source_unavailable',
            'color_profile_unavailable', 'multi_page_unsupported',
            'dimension_mismatch', 'generator_unavailable', 'decode_failed',
            'encode_failed', 'tile_grid_mismatch', 'upload_verification_failed',
            'inconsistent_ready'
       ) OR p_error_detail IS NULL OR pg_catalog.char_length(p_error_detail) > 2000 THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    IF p_error_code IN ('source_busy', 'task_busy') THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
    ELSIF p_error_code = 'operation_busy' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    ELSIF p_error_code = 'lease_lost' THEN
        RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    SELECT job.* INTO v_candidate
    FROM public.asset_image_jobs AS job
    WHERE job.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    IF v_candidate.generation = p_generation
       AND v_candidate.last_lease_token = p_lease_token
       AND v_candidate.last_outcome IS NOT NULL THEN
        RETURN aap_assets_v1._image_exec_outcome(
            p_job_id, p_generation,
            CASE WHEN v_candidate.last_outcome IN ('source_changed', 'dependency_changed')
                 THEN v_candidate.last_outcome ELSE 'replayed' END
        );
    END IF;
    IF v_candidate.status <> 'running'
       OR v_candidate.generation <> p_generation
       OR v_candidate.lease_token IS DISTINCT FROM p_lease_token THEN
        RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    IF v_candidate.kind <> 'image_backfill' THEN
        v_source_reply := aap_assets_v1._image_resolve_owner(
            v_candidate.owner_kind, v_candidate.owner_id, true
        );
        IF v_source_reply IS NULL
           OR v_source_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
           OR pg_catalog.jsonb_typeof(v_source_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_source_reply -> 'ok' = 'true'::jsonb THEN
            v_source := v_source_reply -> 'data';
            IF pg_catalog.jsonb_typeof(v_source) IS DISTINCT FROM 'object'
               OR v_source #>> '{owner,kind}' IS DISTINCT FROM v_candidate.owner_kind
               OR v_source #>> '{owner,id}' IS DISTINCT FROM v_candidate.owner_id::text THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            IF v_source -> 'source_drifted' = 'true'::jsonb
               OR v_source ->> 'source_snapshot' IS DISTINCT FROM v_candidate.source_snapshot
               OR (v_source ->> 'source_generation')::bigint IS DISTINCT FROM v_candidate.source_generation
               OR NOT aap_assets_v1._image_exec_identity_equal(
                    v_source -> 'object_identity', v_candidate.source_facts -> 'object_identity'
               ) THEN
                v_source_code := 'source_changed';
            END IF;
        ELSE
            v_source_code := v_source_reply #>> '{error,code}';
            IF v_source_code IN ('source_busy', 'task_busy') THEN
                RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
            ELSIF v_source_code IS NULL THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
        END IF;
        IF v_source_code IS NULL AND (
            v_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb
            OR v_source -> 'file_type' IS DISTINCT FROM '"image"'::jsonb
            OR v_source -> 'object_identity' IS NULL
            OR v_source -> 'object_identity' = 'null'::jsonb
        ) THEN
            v_source_code := 'source_changed';
        END IF;
    END IF;
    BEGIN
        SELECT job.* INTO v_job
        FROM public.asset_image_jobs AS job
        WHERE job.job_id = p_job_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    IF v_job.status = 'running'
       AND v_job.generation = p_generation
       AND v_job.lease_token = p_lease_token
       AND v_job.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        IF v_job.output_prefix IS NOT NULL THEN
            v_spec := aap_assets_v1._image_spec(v_job.spec);
            IF v_spec IS NULL THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            PERFORM aap_assets_v1._queue_image_garbage(
                v_job.output_prefix, 'attempt_abandoned',
                GREATEST(pg_catalog.clock_timestamp(), v_job.lease_expires_at)
                    + pg_catalog.make_interval(hours => (v_spec #>> '{policy,gc_grace_hours}')::integer)
            );
        END IF;
        RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    IF v_job.status <> 'running'
       OR v_job.generation <> p_generation
       OR v_job.lease_token IS DISTINCT FROM p_lease_token
       OR v_job.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    v_spec := aap_assets_v1._image_spec(v_job.spec);
    IF v_spec IS NULL OR v_spec IS DISTINCT FROM v_job.spec THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    IF v_source_code IS NOT NULL OR p_error_code = 'source_changed'
       OR p_error_code = 'source_missing' OR p_error_code = 'owner_changed' THEN
        v_outcome := 'source_changed';
        v_reason := 'source_changed';
    END IF;
    IF v_job.kind = 'pyramid' THEN
        BEGIN
            SELECT asset.* INTO v_asset
            FROM public.image_pyramid_assets AS asset
            WHERE asset.id = v_job.pyramid_asset_id
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        END;
        IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'dependency_changed'); END IF;
        BEGIN
            SELECT generation.* INTO v_generation
            FROM public.image_pyramid_generations AS generation
            WHERE generation.asset_id = v_job.pyramid_asset_id
              AND generation.generation = v_job.pyramid_generation
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        END;
        IF NOT FOUND OR v_generation.job_id IS DISTINCT FROM p_job_id
           OR v_generation.job_generation IS DISTINCT FROM p_generation
           OR v_generation.lease_token IS DISTINCT FROM p_lease_token
           OR v_generation.status <> 'building'
           OR v_asset.building_generation IS DISTINCT FROM v_job.pyramid_generation THEN
            RETURN aap_assets_v1._image_reply(NULL, 'dependency_changed');
        END IF;
    END IF;
    v_prefix := v_job.output_prefix;
    IF v_prefix IS NOT NULL THEN
        PERFORM aap_assets_v1._queue_image_garbage(
            v_prefix, v_reason,
            GREATEST(pg_catalog.clock_timestamp(), v_job.lease_expires_at)
                + pg_catalog.make_interval(hours => (v_spec #>> '{policy,gc_grace_hours}')::integer)
        );
    END IF;
    IF v_job.kind = 'pyramid' THEN
        UPDATE public.image_pyramid_generations AS generation
        SET status = 'failed', error_code = CASE WHEN v_outcome = 'failed'
                THEN p_error_code ELSE 'source_changed' END,
            lease_token = NULL, lease_expires_at = NULL,
            updated_at = pg_catalog.clock_timestamp()
        WHERE generation.asset_id = v_job.pyramid_asset_id
          AND generation.generation = v_job.pyramid_generation
          AND generation.status IN ('pending', 'building');
        UPDATE public.image_pyramid_assets AS asset
        SET building_generation = NULL, updated_at = pg_catalog.clock_timestamp()
        WHERE asset.id = v_job.pyramid_asset_id
          AND asset.building_generation = v_job.pyramid_generation;
    END IF;
    UPDATE public.asset_image_jobs AS job
    SET status = CASE WHEN v_outcome = 'failed' THEN 'failed' ELSE 'canceled' END,
        error_code = CASE WHEN v_outcome = 'failed' THEN p_error_code ELSE 'source_changed' END,
        error_detail = CASE WHEN v_outcome = 'failed' THEN p_error_detail
            ELSE COALESCE(v_source_code, p_error_code) END,
        lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
        last_lease_token = p_lease_token, last_outcome = v_outcome,
        completed_at = pg_catalog.clock_timestamp(),
        updated_at = pg_catalog.clock_timestamp()
    WHERE job.job_id = p_job_id;
    RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, v_outcome);
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fail_image_media(uuid, bigint, uuid, text, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_image_media(
    p_job_id uuid, p_generation bigint, p_lease_token uuid, p_result jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_candidate public.asset_image_jobs%ROWTYPE;
    v_job public.asset_image_jobs%ROWTYPE;
    v_source_reply jsonb;
    v_source jsonb;
    v_source_code text;
    v_spec jsonb;
    v_policy jsonb;
    v_result_kind text;
    v_source_sha256 text;
    v_output_prefix text;
    v_expected_key text;
    v_identity_text text;
    v_width integer;
    v_height integer;
    v_source_width integer;
    v_source_height integer;
    v_artifact_bytes numeric := 0;
    v_retained_bytes bigint;
    v_expected_tiles bigint := 0;
    v_actual_tiles bigint;
    v_distinct_tiles bigint;
    v_tile jsonb;
    v_level_object jsonb;
    v_level integer;
    v_x integer;
    v_y integer;
    v_columns integer;
    v_rows integer;
    v_tile_key text;
    v_now timestamptz;
    v_outcome text;
    v_failure_code text;
    v_asset public.image_pyramid_assets%ROWTYPE;
    v_generation_row public.image_pyramid_generations%ROWTYPE;
    v_old_generation public.image_pyramid_generations%ROWTYPE;
    v_asset_present boolean := false;
    v_generation_present boolean := false;
    v_publish jsonb;
    v_publish_data jsonb;
    v_preview_reply jsonb;
    v_preview_source jsonb;
    v_child_spec jsonb;
    v_child_enqueue jsonb;
    v_child_data jsonb;
    v_child_kind text;
    v_pixel_count numeric;
    v_old_active integer;
    v_updated bigint;
BEGIN
    IF p_job_id IS NULL OR p_generation IS NULL OR p_generation < 1
       OR p_lease_token IS NULL OR pg_catalog.jsonb_typeof(p_result) IS DISTINCT FROM 'object'
       OR pg_catalog.octet_length(pg_catalog.convert_to(p_result::text, 'UTF8')) > 8388608
       OR pg_catalog.jsonb_typeof(p_result -> 'kind') IS DISTINCT FROM 'string'
       OR NOT aap_assets_v1._media_object_identity_valid(p_result -> 'source_identity') THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_result_kind := p_result ->> 'kind';
    IF v_result_kind = 'dimensions' THEN
        IF NOT aap_assets_v1._media_object_has_only(
            p_result, ARRAY['kind', 'source_identity', 'source_sha256', 'width', 'height']
        ) OR NOT (p_result ?& ARRAY['kind', 'source_identity', 'width', 'height'])
           OR NOT aap_assets_v1._image_exec_integer_valid(p_result -> 'width', 1, 2147483647)
           OR NOT aap_assets_v1._image_exec_integer_valid(p_result -> 'height', 1, 2147483647)
           OR (p_result ? 'source_sha256' AND p_result -> 'source_sha256' <> 'null'::jsonb
               AND (pg_catalog.jsonb_typeof(p_result -> 'source_sha256') <> 'string'
                    OR p_result ->> 'source_sha256' !~ '^[0-9a-f]{64}$')) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
    ELSIF v_result_kind = 'thumbnail' THEN
        IF NOT aap_assets_v1._media_object_has_only(
            p_result, ARRAY[
                'kind', 'source_identity', 'source_sha256', 'width', 'height',
                'blurhash', 'artifact'
            ]
        ) OR NOT (p_result ?& ARRAY[
            'kind', 'source_identity', 'source_sha256', 'width', 'height', 'blurhash', 'artifact'
        ]) OR pg_catalog.jsonb_typeof(p_result -> 'source_sha256') IS DISTINCT FROM 'string'
           OR p_result ->> 'source_sha256' !~ '^[0-9a-f]{64}$'
           OR NOT aap_assets_v1._image_exec_integer_valid(p_result -> 'width', 1, 2147483647)
           OR NOT aap_assets_v1._image_exec_integer_valid(p_result -> 'height', 1, 2147483647)
           OR pg_catalog.jsonb_typeof(p_result -> 'blurhash') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(p_result ->> 'blurhash') NOT BETWEEN 1 AND 64
           OR NOT aap_assets_v1._media_artifact_valid(p_result -> 'artifact', p_result #>> '{artifact,key}')
           OR (p_result #>> '{artifact,byte_size}')::numeric <= 0 THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
    ELSIF v_result_kind = 'pyramid' THEN
        IF NOT aap_assets_v1._media_object_has_only(
            p_result, ARRAY[
                'kind', 'source_identity', 'source_sha256', 'manifest',
                'manifest_artifact', 'overview_artifact', 'tile_artifacts',
                'tile_count', 'retained_bytes'
            ]
        ) OR NOT (p_result ?& ARRAY[
            'kind', 'source_identity', 'source_sha256', 'manifest',
            'manifest_artifact', 'overview_artifact', 'tile_artifacts',
            'tile_count', 'retained_bytes'
        ]) OR pg_catalog.jsonb_typeof(p_result -> 'source_sha256') IS DISTINCT FROM 'string'
           OR p_result ->> 'source_sha256' !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(p_result -> 'manifest') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_result -> 'tile_artifacts') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_result -> 'tile_artifacts') > 20000
           OR NOT aap_assets_v1._image_exec_integer_valid(p_result -> 'tile_count', 0, 9223372036854775807)
           OR NOT aap_assets_v1._image_exec_integer_valid(p_result -> 'retained_bytes', 0, 9223372036854775807)
           OR NOT aap_assets_v1._media_artifact_valid(
                p_result -> 'manifest_artifact', p_result #>> '{manifest_artifact,key}'
           ) OR NOT aap_assets_v1._media_artifact_valid(
                p_result -> 'overview_artifact', p_result #>> '{overview_artifact,key}'
           ) OR (p_result #>> '{manifest_artifact,byte_size}')::numeric <= 0
           OR (p_result #>> '{overview_artifact,byte_size}')::numeric <= 0 THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
    ELSE
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;

    SELECT job.* INTO v_candidate
    FROM public.asset_image_jobs AS job
    WHERE job.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    IF v_candidate.generation = p_generation
       AND v_candidate.last_lease_token = p_lease_token
       AND v_candidate.last_outcome IS NOT NULL THEN
        RETURN aap_assets_v1._image_exec_outcome(
            p_job_id, p_generation,
            CASE WHEN v_candidate.last_outcome IN ('source_changed', 'dependency_changed')
                 THEN v_candidate.last_outcome ELSE 'replayed' END
        );
    END IF;
    IF v_candidate.status <> 'running'
       OR v_candidate.generation <> p_generation
       OR v_candidate.lease_token IS DISTINCT FROM p_lease_token THEN
        RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    IF v_candidate.kind = 'image_backfill' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_source_reply := aap_assets_v1._image_resolve_owner(
        v_candidate.owner_kind, v_candidate.owner_id, true
    );
    IF v_source_reply IS NULL
       OR v_source_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
       OR pg_catalog.jsonb_typeof(v_source_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    IF v_source_reply -> 'ok' = 'true'::jsonb THEN
        v_source := v_source_reply -> 'data';
        IF pg_catalog.jsonb_typeof(v_source) IS DISTINCT FROM 'object'
           OR v_source #>> '{owner,kind}' IS DISTINCT FROM v_candidate.owner_kind
           OR v_source #>> '{owner,id}' IS DISTINCT FROM v_candidate.owner_id::text THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
    ELSE
        v_source_code := v_source_reply #>> '{error,code}';
        IF v_source_code IN ('source_busy', 'task_busy') THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
        ELSIF v_source_code IS NULL THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
    END IF;
    BEGIN
        SELECT job.* INTO v_job
        FROM public.asset_image_jobs AS job
        WHERE job.job_id = p_job_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    IF v_job.status <> 'running'
       OR v_job.generation <> p_generation
       OR v_job.lease_token IS DISTINCT FROM p_lease_token
       OR v_job.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        IF v_job.lease_token = p_lease_token AND v_job.output_prefix IS NOT NULL THEN
            v_spec := aap_assets_v1._image_spec(v_job.spec);
            IF v_spec IS NULL THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            PERFORM aap_assets_v1._queue_image_garbage(
                v_job.output_prefix, 'attempt_abandoned',
                GREATEST(pg_catalog.clock_timestamp(), v_job.lease_expires_at)
                    + pg_catalog.make_interval(hours => (v_spec #>> '{policy,gc_grace_hours}')::integer)
            );
        END IF;
        RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    v_spec := aap_assets_v1._image_spec(v_job.spec);
    IF v_spec IS NULL OR v_spec IS DISTINCT FROM v_job.spec THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    v_policy := v_spec -> 'policy';
    IF v_spec ->> 'kind' IS DISTINCT FROM v_result_kind THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    IF v_result_kind = 'thumbnail' THEN
        v_expected_key := 'image-assets/' || v_job.job_id::text || '/g'
            || v_job.generation::text || '/a' || p_lease_token::text || '/thumbnail.webp';
        IF v_job.output_prefix IS DISTINCT FROM pg_catalog.left(
                v_expected_key, pg_catalog.char_length(v_expected_key) - pg_catalog.char_length('thumbnail.webp')
           ) OR NOT aap_assets_v1._media_artifact_valid(p_result -> 'artifact', v_expected_key)
           OR (p_result #>> '{artifact,byte_size}')::numeric <= 0
           OR (p_result #>> '{artifact,byte_size}')::numeric > (v_policy ->> 'max_derived_bytes')::numeric
           OR v_job.pyramid_asset_id IS NOT NULL OR v_job.pyramid_generation IS NOT NULL THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
    ELSIF v_result_kind = 'pyramid' THEN
        IF v_job.pyramid_asset_id IS NULL OR v_job.pyramid_generation IS NULL
           OR v_job.pyramid_generation < 1 THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        v_output_prefix := 'image-pyramids/' || v_job.pyramid_asset_id::text
            || '/g' || v_job.pyramid_generation::text || '/a' || p_lease_token::text || '/';
        IF v_job.output_prefix IS DISTINCT FROM v_output_prefix THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
    ELSIF v_job.output_prefix IS NOT NULL OR v_job.pyramid_asset_id IS NOT NULL
       OR v_job.pyramid_generation IS NOT NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    IF v_source_code IS NOT NULL
       OR v_source -> 'source_drifted' IS DISTINCT FROM 'false'::jsonb
       OR v_source ->> 'source_snapshot' IS DISTINCT FROM v_job.source_snapshot
       OR (v_source ->> 'source_generation')::bigint IS DISTINCT FROM v_job.source_generation
       OR v_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb
       OR v_source -> 'file_type' IS DISTINCT FROM '"image"'::jsonb
       OR NOT aap_assets_v1._image_exec_identity_equal(
            v_source -> 'object_identity', v_job.source_facts -> 'object_identity'
       ) OR (v_source -> 'file_size' IS NOT NULL
             AND v_source -> 'file_size' <> 'null'::jsonb
             AND (v_source ->> 'file_size')::bigint
                    IS DISTINCT FROM (v_source #>> '{object_identity,byte_size}')::bigint) THEN
        v_source_code := COALESCE(v_source_code, 'source_changed');
    ELSIF NOT aap_assets_v1._image_exec_identity_equal(
        p_result -> 'source_identity', v_source -> 'object_identity'
    ) THEN
        v_source_code := 'source_changed';
    END IF;
    IF v_source_code IS NULL
       AND v_source ->> 'content_hash' ~* '^[0-9a-f]{64}$'
       AND p_result -> 'source_sha256' IS NOT NULL
       AND p_result -> 'source_sha256' <> 'null'::jsonb
       AND pg_catalog.lower(v_source ->> 'content_hash') IS DISTINCT FROM p_result ->> 'source_sha256' THEN
        v_source_code := 'source_changed';
    END IF;
    IF v_source_code IS NULL THEN
        IF v_result_kind = 'pyramid' THEN
            v_width := (p_result #>> '{manifest,width}')::integer;
            v_height := (p_result #>> '{manifest,height}')::integer;
        ELSE
            v_width := (p_result ->> 'width')::integer;
            v_height := (p_result ->> 'height')::integer;
        END IF;
        IF v_source -> 'width' <> 'null'::jsonb THEN
            v_source_width := (v_source ->> 'width')::integer;
        END IF;
        IF v_source -> 'height' <> 'null'::jsonb THEN
            v_source_height := (v_source ->> 'height')::integer;
        END IF;
        IF (v_source_width IS NOT NULL AND v_source_width <> v_width)
           OR (v_source_height IS NOT NULL AND v_source_height <> v_height) THEN
            v_failure_code := 'dimension_mismatch';
        END IF;
    END IF;
    IF v_result_kind = 'thumbnail' AND v_source_code IS NULL
       AND (p_result #>> '{artifact,byte_size}')::numeric > (v_policy ->> 'max_derived_bytes')::numeric THEN
        v_failure_code := 'resource_limit';
    END IF;
    IF v_result_kind = 'pyramid' AND v_source_code IS NULL THEN
        IF v_job.pyramid_asset_id IS NULL OR v_job.pyramid_generation IS NULL
           OR NOT aap_assets_v1._image_exec_manifest_valid(
                p_result -> 'manifest', v_job.pyramid_generation,
                p_result ->> 'source_sha256', v_policy
           ) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        IF (p_result -> 'manifest_artifact' ->> 'key')
                IS DISTINCT FROM v_output_prefix || 'manifest.json'
           OR (p_result -> 'overview_artifact' ->> 'key')
                IS DISTINCT FROM v_output_prefix || 'overview.webp'
           OR 'sha256:' || (p_result -> 'overview_artifact' ->> 'sha256')
                IS DISTINCT FROM p_result #>> '{manifest,overview,contentDigest}' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        SELECT COALESCE(pg_catalog.sum(
            (level.value ->> 'columns')::bigint * (level.value ->> 'rows')::bigint
        ), 0) INTO v_expected_tiles
        FROM pg_catalog.jsonb_array_elements(p_result #> '{manifest,levels}') AS level(value);
        v_actual_tiles := pg_catalog.jsonb_array_length(p_result -> 'tile_artifacts');
        IF (p_result ->> 'tile_count')::bigint <> v_expected_tiles
           OR v_actual_tiles <> v_expected_tiles THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        v_artifact_bytes := (p_result #>> '{manifest_artifact,byte_size}')::numeric
            + (p_result #>> '{overview_artifact,byte_size}')::numeric;
        FOR v_tile IN
            SELECT tile.value FROM pg_catalog.jsonb_array_elements(
                p_result -> 'tile_artifacts'
            ) AS tile(value)
        LOOP
            IF NOT aap_assets_v1._media_object_has_only(
                    v_tile, ARRAY['level', 'x', 'y', 'artifact']
               ) OR NOT (v_tile ?& ARRAY['level', 'x', 'y', 'artifact'])
               OR NOT aap_assets_v1._image_exec_integer_valid(v_tile -> 'level', 0, 2147483647)
               OR NOT aap_assets_v1._image_exec_integer_valid(v_tile -> 'x', 0, 2147483647)
               OR NOT aap_assets_v1._image_exec_integer_valid(v_tile -> 'y', 0, 2147483647) THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
            END IF;
            v_level := (v_tile ->> 'level')::integer;
            v_x := (v_tile ->> 'x')::integer;
            v_y := (v_tile ->> 'y')::integer;
            v_level_object := p_result #> ARRAY['manifest', 'levels', v_level::text];
            IF v_level_object IS NULL THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
            END IF;
            v_columns := (v_level_object ->> 'columns')::integer;
            v_rows := (v_level_object ->> 'rows')::integer;
            IF v_x >= v_columns OR v_y >= v_rows THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
            END IF;
            v_tile_key := v_output_prefix || 'tiles/' || v_level::text || '/'
                || v_x::text || '/' || v_y::text || '.webp';
            IF NOT aap_assets_v1._media_artifact_valid(v_tile -> 'artifact', v_tile_key)
               OR (v_tile #>> '{artifact,byte_size}')::numeric <= 0 THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
            END IF;
            v_artifact_bytes := v_artifact_bytes + (v_tile #>> '{artifact,byte_size}')::numeric;
        END LOOP;
        SELECT pg_catalog.count(DISTINCT
            (tile.value ->> 'level') || ':' || (tile.value ->> 'x') || ':' || (tile.value ->> 'y')
        ) INTO v_distinct_tiles
        FROM pg_catalog.jsonb_array_elements(p_result -> 'tile_artifacts') AS tile(value);
        v_retained_bytes := (p_result ->> 'retained_bytes')::bigint;
        IF v_distinct_tiles <> v_expected_tiles
           OR v_artifact_bytes <> v_retained_bytes
           OR v_artifact_bytes > (v_policy ->> 'max_derived_bytes')::numeric
           OR (p_result #>> '{manifest_artifact,byte_size}')::numeric <= 0 THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
    END IF;

    IF v_source_code IS NOT NULL THEN
        v_outcome := 'source_changed';
        v_failure_code := 'source_changed';
    ELSIF v_failure_code IS NOT NULL THEN
        v_outcome := 'failed';
    ELSE
        v_outcome := 'published';
    END IF;

    IF v_job.kind = 'pyramid' THEN
        BEGIN
            SELECT asset.* INTO v_asset
            FROM public.image_pyramid_assets AS asset
            WHERE asset.id = v_job.pyramid_asset_id
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        END;
        v_asset_present := FOUND;
        IF NOT v_asset_present THEN
            v_outcome := 'dependency_changed';
        ELSE
            BEGIN
                SELECT generation.* INTO v_generation_row
                FROM public.image_pyramid_generations AS generation
                WHERE generation.asset_id = v_job.pyramid_asset_id
                  AND generation.generation = v_job.pyramid_generation
                FOR UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
            END;
            v_generation_present := FOUND;
            IF NOT v_generation_present OR v_generation_row.job_id IS DISTINCT FROM p_job_id
               OR v_generation_row.job_generation IS DISTINCT FROM p_generation
               OR v_generation_row.source_generation IS DISTINCT FROM v_job.source_generation
               OR v_generation_row.source_snapshot IS DISTINCT FROM v_job.source_snapshot
               OR v_generation_row.lease_token IS DISTINCT FROM p_lease_token
               OR v_generation_row.lease_expires_at <= pg_catalog.clock_timestamp()
               OR v_asset.building_generation IS DISTINCT FROM v_job.pyramid_generation
               OR v_generation_row.status <> 'building' THEN
                v_outcome := 'dependency_changed';
            END IF;
        END IF;
    END IF;

    IF v_outcome = 'source_changed' OR v_outcome = 'dependency_changed' THEN
        IF v_job.output_prefix IS NOT NULL THEN
            PERFORM aap_assets_v1._queue_image_garbage(
                v_job.output_prefix,
                CASE WHEN v_outcome = 'source_changed' THEN 'source_changed' ELSE 'dependency_changed' END,
                GREATEST(pg_catalog.clock_timestamp(), v_job.lease_expires_at)
                    + pg_catalog.make_interval(hours => (v_policy ->> 'gc_grace_hours')::integer)
            );
        END IF;
        IF v_job.kind = 'pyramid' AND v_generation_present THEN
            UPDATE public.image_pyramid_generations AS generation
            SET status = 'failed', error_code = v_outcome,
                lease_token = NULL, lease_expires_at = NULL,
                updated_at = pg_catalog.clock_timestamp()
            WHERE generation.asset_id = v_job.pyramid_asset_id
              AND generation.generation = v_job.pyramid_generation
              AND generation.job_id = p_job_id
              AND generation.job_generation = p_generation
              AND generation.source_generation = v_job.source_generation
              AND generation.source_snapshot = v_job.source_snapshot
              AND generation.status IN ('pending', 'building');
            UPDATE public.image_pyramid_assets AS asset
            SET building_generation = NULL, updated_at = pg_catalog.clock_timestamp()
            WHERE asset.id = v_job.pyramid_asset_id
              AND asset.building_generation = v_job.pyramid_generation;
        END IF;
        UPDATE public.asset_image_jobs AS job
        SET status = 'canceled', error_code = v_outcome,
            error_detail = COALESCE(v_source_code, v_outcome),
            lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
            last_lease_token = p_lease_token, last_outcome = v_outcome,
            completed_at = pg_catalog.clock_timestamp(),
            updated_at = pg_catalog.clock_timestamp()
        WHERE job.job_id = p_job_id;
        RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, v_outcome);
    END IF;

    IF v_outcome = 'failed' THEN
        IF v_job.output_prefix IS NOT NULL THEN
            PERFORM aap_assets_v1._queue_image_garbage(
                v_job.output_prefix, 'attempt_abandoned',
                GREATEST(pg_catalog.clock_timestamp(), v_job.lease_expires_at)
                    + pg_catalog.make_interval(hours => (v_policy ->> 'gc_grace_hours')::integer)
            );
        END IF;
        IF v_job.kind = 'pyramid' THEN
            UPDATE public.image_pyramid_generations AS generation
            SET status = 'failed', error_code = v_failure_code,
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
        SET status = 'failed', error_code = v_failure_code,
            error_detail = v_failure_code,
            lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
            last_lease_token = p_lease_token, last_outcome = 'failed',
            completed_at = pg_catalog.clock_timestamp(),
            updated_at = pg_catalog.clock_timestamp()
        WHERE job.job_id = p_job_id;
        RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'failed');
    END IF;

    IF v_result_kind = 'thumbnail' AND v_job.owner_kind = 'task' THEN
        v_publish := aap_labeling_v1.publish_task_image_thumbnail(
            v_job.owner_id, v_job.source_snapshot, v_job.source_generation,
            v_expected_key, p_result ->> 'blurhash'
        );
        IF v_publish IS NULL
           OR v_publish ->> 'schema' IS DISTINCT FROM 'aap-labeling-image-thumbnail/v1'
           OR pg_catalog.jsonb_typeof(v_publish -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_finish_invalid_response';
        END IF;
        IF v_publish -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            IF v_publish #>> '{error,code}' = 'source_busy' THEN
                RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
            END IF;
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_finish_invalid_response';
        END IF;
        v_publish_data := v_publish -> 'data';
        IF NOT aap_assets_v1._media_object_has_only(
            v_publish_data, ARRAY['task_id', 'applied']
        ) OR v_publish_data ->> 'task_id' IS DISTINCT FROM v_job.owner_id::text
           OR pg_catalog.jsonb_typeof(v_publish_data -> 'applied') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_finish_invalid_response';
        END IF;
        IF v_publish_data -> 'applied' IS DISTINCT FROM 'true'::jsonb THEN
            PERFORM aap_assets_v1._queue_image_garbage(
                v_job.output_prefix, 'source_changed',
                GREATEST(pg_catalog.clock_timestamp(), v_job.lease_expires_at)
                    + pg_catalog.make_interval(hours => (v_policy ->> 'gc_grace_hours')::integer)
            );
            UPDATE public.asset_image_jobs AS job
            SET status = 'canceled', error_code = 'source_changed',
                error_detail = 'Labeling rejected stale thumbnail publication',
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                last_lease_token = p_lease_token, last_outcome = 'source_changed',
                completed_at = pg_catalog.clock_timestamp(),
                updated_at = pg_catalog.clock_timestamp()
            WHERE job.job_id = p_job_id;
            RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'source_changed');
        END IF;
    ELSIF v_result_kind = 'thumbnail' AND v_job.owner_kind = 'dataset_item' THEN
        UPDATE public.dataset_items AS item
        SET thumbnail_path = v_expected_key,
            blurhash = p_result ->> 'blurhash',
            width = COALESCE(item.width, v_width),
            height = COALESCE(item.height, v_height),
            media_materialized_generation = v_job.source_generation,
            updated_at = pg_catalog.clock_timestamp()
        WHERE item.id = v_job.owner_id
          AND item.media_source_generation = v_job.source_generation
          AND NOT item.media_source_drifted;
        GET DIAGNOSTICS v_updated = ROW_COUNT;
        IF v_updated <> 1 THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
        END IF;
    ELSIF v_result_kind = 'dimensions' AND v_job.owner_kind = 'dataset_item' THEN
        UPDATE public.dataset_items AS item
        SET width = COALESCE(item.width, v_width),
            height = COALESCE(item.height, v_height),
            media_materialized_generation = v_job.source_generation,
            updated_at = pg_catalog.clock_timestamp()
        WHERE item.id = v_job.owner_id
          AND item.media_source_generation = v_job.source_generation
          AND NOT item.media_source_drifted;
        GET DIAGNOSTICS v_updated = ROW_COUNT;
        IF v_updated <> 1 THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
        END IF;
    ELSIF v_result_kind = 'pyramid' AND v_job.owner_kind = 'dataset_item' THEN
        UPDATE public.dataset_items AS item
        SET width = COALESCE(item.width, v_width),
            height = COALESCE(item.height, v_height),
            media_materialized_generation = v_job.source_generation,
            updated_at = pg_catalog.clock_timestamp()
        WHERE item.id = v_job.owner_id
          AND item.media_source_generation = v_job.source_generation
          AND NOT item.media_source_drifted;
        GET DIAGNOSTICS v_updated = ROW_COUNT;
        IF v_updated <> 1 THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
        END IF;
    END IF;

    IF v_result_kind = 'pyramid' THEN
        v_old_active := v_asset.active_generation;
        IF v_old_active IS NOT NULL AND v_old_active <> v_job.pyramid_generation THEN
            BEGIN
                SELECT generation.* INTO v_old_generation
                FROM public.image_pyramid_generations AS generation
                WHERE generation.asset_id = v_job.pyramid_asset_id
                  AND generation.generation = v_old_active
                FOR UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RAISE EXCEPTION USING ERRCODE = '40001', MESSAGE = 'image_old_generation_busy';
            END;
            IF FOUND AND v_old_generation.status = 'ready' THEN
                PERFORM aap_assets_v1._queue_image_garbage(
                    'image-pyramids/' || v_job.pyramid_asset_id::text
                        || '/g' || v_old_active::text || '/',
                    'generation_retired',
                    pg_catalog.clock_timestamp()
                        + pg_catalog.make_interval(hours => COALESCE(
                            (v_old_generation.policy ->> 'gc_grace_hours')::integer,
                            (v_policy ->> 'gc_grace_hours')::integer
                        ))
                );
            END IF;
        END IF;
        UPDATE public.image_pyramid_generations AS generation
        SET status = 'ready', source_identity = aap_assets_v1._image_exec_legacy_identity(p_result -> 'source_identity'),
            source_fingerprint = 'sha256:' || (p_result ->> 'source_sha256'),
            source_generation = v_job.source_generation,
            source_snapshot = v_job.source_snapshot,
            output_prefix = v_output_prefix,
            manifest = p_result -> 'manifest', policy = v_policy,
            lease_token = NULL, lease_expires_at = NULL,
            width = v_width, height = v_height,
            tile_size = 512, overlap = 1,
            max_level = pg_catalog.jsonb_array_length(p_result #> '{manifest,levels}') - 1,
            format = 'webp', normalization_version = v_policy ->> 'normalization_version',
            manifest_key = v_output_prefix || 'manifest.json',
            manifest_digest = 'sha256:' || (p_result #>> '{manifest_artifact,sha256}'),
            overview_key = v_output_prefix || 'overview.webp',
            overview_width = (p_result #>> '{manifest,overview,width}')::integer,
            overview_height = (p_result #>> '{manifest,overview,height}')::integer,
            overview_digest = p_result #>> '{manifest,overview,contentDigest}',
            tile_count = (p_result ->> 'tile_count')::integer,
            retained_bytes = v_retained_bytes,
            error_code = NULL,
            updated_at = pg_catalog.clock_timestamp()
        WHERE generation.asset_id = v_job.pyramid_asset_id
          AND generation.generation = v_job.pyramid_generation;
        UPDATE public.image_pyramid_assets AS asset
        SET active_generation = v_job.pyramid_generation,
            building_generation = NULL,
            updated_at = pg_catalog.clock_timestamp()
        WHERE asset.id = v_job.pyramid_asset_id;
    END IF;
    UPDATE public.asset_image_jobs AS job
    SET status = 'ready', result = p_result,
        error_code = NULL, error_detail = NULL,
        lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
        last_lease_token = p_lease_token, last_outcome = 'published',
        completed_at = pg_catalog.clock_timestamp(),
        updated_at = pg_catalog.clock_timestamp()
    WHERE job.job_id = p_job_id;
    IF v_result_kind = 'dimensions'
       AND COALESCE(v_spec -> 'generate_preview' = 'true'::jsonb, false) THEN
        v_preview_reply := aap_assets_v1._image_resolve_owner(
            v_job.owner_kind, v_job.owner_id, true
        );
        IF v_preview_reply IS NULL
           OR v_preview_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
           OR pg_catalog.jsonb_typeof(v_preview_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_finish_invalid_response';
        END IF;
        IF v_preview_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_source_code := v_preview_reply #>> '{error,code}';
            IF v_source_code IN ('source_busy', 'task_busy', 'operation_busy') THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_finish_busy';
            END IF;
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_finish_source_changed';
        END IF;
        v_preview_source := v_preview_reply -> 'data';
        IF pg_catalog.jsonb_typeof(v_preview_source) IS DISTINCT FROM 'object'
           OR v_preview_source #>> '{owner,kind}' IS DISTINCT FROM v_job.owner_kind
           OR v_preview_source #>> '{owner,id}' IS DISTINCT FROM v_job.owner_id::text
           OR v_preview_source -> 'source_drifted' IS DISTINCT FROM 'false'::jsonb
           OR v_preview_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb
           OR v_preview_source -> 'file_type' IS DISTINCT FROM '"image"'::jsonb
           OR v_preview_source ->> 'source_snapshot' IS DISTINCT FROM v_job.source_snapshot
           OR (v_preview_source ->> 'source_generation')::bigint IS DISTINCT FROM v_job.source_generation
           OR NOT aap_assets_v1._image_exec_identity_equal(
                v_preview_source -> 'object_identity', v_job.source_facts -> 'object_identity'
           ) THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_finish_source_changed';
        END IF;
        IF v_preview_source -> 'width' = 'null'::jsonb
           OR v_preview_source -> 'height' = 'null'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_finish_invalid_response';
        END IF;
        IF (v_preview_source ->> 'width')::integer IS DISTINCT FROM v_width
           OR (v_preview_source ->> 'height')::integer IS DISTINCT FROM v_height THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_finish_source_changed';
        END IF;
        v_pixel_count := v_width::numeric * v_height::numeric;
        IF v_pixel_count < (v_policy ->> 'optional_pixels')::numeric THEN
            v_child_kind := 'thumbnail';
        ELSIF v_policy -> 'auto_generate_pyramids' = 'true'::jsonb THEN
            v_child_kind := 'pyramid';
        ELSE
            v_child_kind := NULL;
        END IF;
        IF v_child_kind IS NOT NULL THEN
            v_child_spec := pg_catalog.jsonb_build_object(
                'kind', v_child_kind,
                'expected_source_snapshot', v_job.source_snapshot,
                'policy', v_policy
            );
            v_child_enqueue := aap_assets_v1._image_enqueue(
                NULL, NULL, v_preview_source, v_child_spec, false
            );
            IF v_child_enqueue IS NULL
               OR v_child_enqueue ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
               OR pg_catalog.jsonb_typeof(v_child_enqueue -> 'ok') IS DISTINCT FROM 'boolean' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_finish_invalid_response';
            END IF;
            IF v_child_enqueue -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                v_source_code := v_child_enqueue #>> '{error,code}';
                IF v_source_code IN ('source_busy', 'operation_busy', 'operation_conflict') THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_finish_busy';
                END IF;
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_finish_source_changed';
            END IF;
            v_child_data := v_child_enqueue -> 'data';
            IF pg_catalog.jsonb_typeof(v_child_data) IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_child_data -> 'work') IS DISTINCT FROM 'object'
               OR v_child_data #>> '{work,kind}' IS DISTINCT FROM v_child_kind
               OR v_child_data #>> '{work,job_id}' IS NULL THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_finish_invalid_response';
            END IF;
        END IF;
    END IF;
    RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'published');
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        IF SQLERRM = 'image_old_generation_busy' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        END IF;
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    WHEN SQLSTATE 'P0001' THEN
        IF SQLERRM = 'image_finish_busy' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        ELSIF SQLERRM = 'image_finish_source_changed' THEN
            RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'source_changed');
        END IF;
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_image_media(uuid, bigint, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.advance_image_backfill(
    p_job_id uuid, p_generation bigint, p_lease_token uuid,
    p_batch_size integer, p_observations jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_candidate public.asset_image_jobs%ROWTYPE;
    v_job public.asset_image_jobs%ROWTYPE;
    v_progress jsonb;
    v_pending jsonb;
    v_existing_pending jsonb;
    v_spec jsonb;
    v_selection jsonb;
    v_page_reply jsonb;
    v_page jsonb;
    v_page_items jsonb;
    v_observation jsonb;
    v_candidate_source jsonb;
    v_owner_reply jsonb;
    v_owner_data jsonb;
    v_owner_id uuid;
    v_owner_kind text;
    v_source_snapshot text;
    v_identity jsonb;
    v_error_code text;
    v_child_spec jsonb;
    v_enqueue jsonb;
    v_enqueue_data jsonb;
    v_source_error text;
    v_snapshot_digest text;
    v_observations_digest text;
    v_total bigint;
    v_scheduled bigint;
    v_skipped bigint;
    v_page_scheduled bigint := 0;
    v_page_skipped bigint := 0;
    v_batch_token uuid;
    v_next_kind text;
    v_next_id uuid;
    v_complete boolean;
    v_only_missing boolean;
    v_purpose text;
    v_child_kind text;
    v_child_generate_preview boolean := false;
    v_pixels numeric;
    v_need_work boolean;
    v_asset_id uuid;
    v_active_generation integer;
    v_generation public.image_pyramid_generations%ROWTYPE;
    v_data jsonb;
    v_saved_result jsonb;
    v_failure text;
    v_cursor_kind text;
    v_cursor_id uuid;
    v_accepted_before timestamptz;
    v_item_count integer;
BEGIN
    IF p_job_id IS NULL OR p_generation IS NULL OR p_generation < 1
       OR p_lease_token IS NULL OR p_batch_size IS NULL OR p_batch_size NOT BETWEEN 1 AND 500
       OR (p_observations IS NOT NULL AND (
            pg_catalog.jsonb_typeof(p_observations) IS DISTINCT FROM 'object'
            OR pg_catalog.octet_length(pg_catalog.convert_to(p_observations::text, 'UTF8')) > 8388608
       )) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    SELECT job.* INTO v_candidate
    FROM public.asset_image_jobs AS job
    WHERE job.job_id = p_job_id;
    IF NOT FOUND OR v_candidate.kind <> 'image_backfill' THEN
        RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    v_progress := COALESCE(v_candidate.progress, '{}'::jsonb);
    IF p_observations IS NOT NULL THEN
        v_observations_digest := pg_catalog.encode(
            pg_catalog.sha256(pg_catalog.convert_to(p_observations::text, 'UTF8')),
            'hex'
        );
        IF v_progress ->> 'last_advance_lease_token' = p_lease_token::text
           AND v_progress ->> 'last_advance_batch_token' = p_observations ->> 'batch_token'
           AND pg_catalog.jsonb_typeof(v_progress -> 'last_advance_result') = 'object' THEN
            IF v_progress ->> 'last_advance_observations_digest'
                    IS DISTINCT FROM v_observations_digest THEN
                RETURN aap_assets_v1._image_reply(NULL, 'operation_conflict');
            END IF;
            RETURN aap_assets_v1._image_reply(v_progress -> 'last_advance_result', NULL);
        END IF;
    END IF;
    IF p_observations IS NULL
       AND v_progress ->> 'last_advance_lease_token' = p_lease_token::text
       AND v_progress ->> 'last_advance_batch_size' = p_batch_size::text
       AND pg_catalog.jsonb_typeof(v_progress -> 'last_advance_result') = 'object'
       AND v_candidate.status = 'ready' THEN
        RETURN aap_assets_v1._image_reply(v_progress -> 'last_advance_result', NULL);
    END IF;
    IF v_candidate.status <> 'running'
       OR v_candidate.generation <> p_generation
       OR v_candidate.lease_token IS DISTINCT FROM p_lease_token
       OR v_candidate.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    v_spec := aap_assets_v1._image_spec(v_candidate.spec);
    IF v_spec IS NULL OR v_spec IS DISTINCT FROM v_candidate.spec
       OR v_candidate.backfill_cutoff IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    v_selection := pg_catalog.jsonb_set(
        v_spec -> 'selection', '{accepted_before}',
        pg_catalog.to_jsonb(v_candidate.backfill_cutoff), true
    );
    v_purpose := v_selection ->> 'purpose';
    v_only_missing := COALESCE(v_selection -> 'only_missing' = 'true'::jsonb, true);
    IF v_purpose NOT IN ('dimensions', 'thumbnail', 'pyramid') THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;

    IF p_observations IS NULL THEN
        IF pg_catalog.jsonb_typeof(v_progress -> 'pending_batch') = 'object'
           AND v_progress #>> '{pending_batch,lease_token}' = p_lease_token::text
           AND v_progress #>> '{pending_batch,batch_size}' = p_batch_size::text THEN
            v_pending := v_progress -> 'pending_batch';
            RETURN aap_assets_v1._image_reply(
                pg_catalog.jsonb_build_object(
                    'job_id', p_job_id,
                    'generation', p_generation,
                    'lease_token', p_lease_token,
                    'scheduled', 0,
                    'skipped', 0,
                    'complete', false,
                    'next_owner_kind', v_pending -> 'next_owner_kind',
                    'next_owner_id', v_pending -> 'next_owner_id',
                    'batch_token', v_pending -> 'batch_token',
                    'candidates', v_pending -> 'items',
                    'needs_observation', true
                ), NULL
            );
        END IF;
        v_page_reply := aap_assets_v1.scan_image_backfill(
            v_selection,
            v_candidate.backfill_cursor_kind,
            v_candidate.backfill_cursor_id,
            p_batch_size
        );
        IF v_page_reply IS NULL
           OR v_page_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
           OR pg_catalog.jsonb_typeof(v_page_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_page_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN v_page_reply;
        END IF;
        v_page := v_page_reply -> 'data';
        IF NOT aap_assets_v1._media_object_has_only(
            v_page, ARRAY[
                'items', 'snapshot_digest', 'total', 'accepted_before',
                'after_owner_kind', 'after_owner_id', 'next_owner_kind', 'next_owner_id', 'complete'
            ]
        ) OR NOT (v_page ?& ARRAY[
            'items', 'snapshot_digest', 'total', 'accepted_before',
            'after_owner_kind', 'after_owner_id', 'next_owner_kind', 'next_owner_id', 'complete'
        ]) OR pg_catalog.jsonb_typeof(v_page -> 'items') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(v_page -> 'items') > p_batch_size
           OR pg_catalog.jsonb_typeof(v_page -> 'snapshot_digest') IS DISTINCT FROM 'string'
           OR v_page ->> 'snapshot_digest' !~ '^[0-9a-f]{64}$'
           OR NOT aap_assets_v1._image_exec_integer_valid(v_page -> 'total', 0, 9223372036854775807)
           OR pg_catalog.jsonb_typeof(v_page -> 'accepted_before') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_page -> 'complete') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        BEGIN
            v_accepted_before := (v_page ->> 'accepted_before')::timestamptz;
        EXCEPTION WHEN invalid_text_representation OR datetime_field_overflow THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END;
        IF v_accepted_before IS DISTINCT FROM v_candidate.backfill_cutoff
           OR ((v_page -> 'after_owner_kind' = 'null'::jsonb)
                <> (v_page -> 'after_owner_id' = 'null'::jsonb))
           OR ((v_page -> 'next_owner_kind' = 'null'::jsonb)
                <> (v_page -> 'next_owner_id' = 'null'::jsonb))
           OR (v_page -> 'after_owner_kind' <> 'null'::jsonb
               AND (v_page ->> 'after_owner_kind' IS DISTINCT FROM v_candidate.backfill_cursor_kind
                    OR (v_page ->> 'after_owner_id')::uuid IS DISTINCT FROM v_candidate.backfill_cursor_id)) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        v_page_items := v_page -> 'items';
        v_item_count := pg_catalog.jsonb_array_length(v_page_items);
        IF v_item_count = 0 AND v_page -> 'complete' = 'false'::jsonb THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_item_count > 0 AND v_page -> 'next_owner_kind' <> 'null'::jsonb
           AND v_page ->> 'next_owner_kind' IS DISTINCT FROM v_selection ->> 'owner_kind' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        FOR v_candidate_source IN
            SELECT item.value FROM pg_catalog.jsonb_array_elements(v_page_items) AS item(value)
        LOOP
            IF pg_catalog.jsonb_typeof(v_candidate_source) IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_candidate_source -> 'owner') IS DISTINCT FROM 'object'
               OR v_candidate_source #>> '{owner,kind}' IS DISTINCT FROM v_selection ->> 'owner_kind'
               OR pg_catalog.jsonb_typeof(v_candidate_source #> '{owner,id}') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_candidate_source -> 'source_snapshot') IS DISTINCT FROM 'string'
               OR v_candidate_source ->> 'source_snapshot' !~ '^[0-9a-f]{64}$'
               OR pg_catalog.jsonb_typeof(v_candidate_source -> 'file_type') IS DISTINCT FROM 'string'
               OR NOT aap_assets_v1._image_exec_integer_valid(
                    v_candidate_source -> 'source_generation', 1, 9223372036854775807
               ) THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            BEGIN
                v_owner_id := (v_candidate_source #>> '{owner,id}')::uuid;
                PERFORM (v_candidate_source ->> 'source_generation')::bigint;
            EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END;
        END LOOP;
        v_snapshot_digest := v_page ->> 'snapshot_digest';
        v_total := (v_page ->> 'total')::bigint;
        v_scheduled := COALESCE((v_progress ->> 'scheduled')::bigint, 0);
        v_skipped := COALESCE((v_progress ->> 'skipped')::bigint, 0);
        IF v_item_count = 0 AND v_page -> 'complete' = 'true'::jsonb THEN
            v_data := pg_catalog.jsonb_build_object(
                'job_id', p_job_id,
                'generation', p_generation,
                'lease_token', p_lease_token,
                'scheduled', v_scheduled,
                'skipped', v_skipped,
                'complete', true,
                'next_owner_kind', NULL,
                'next_owner_id', NULL,
                'batch_token', NULL,
                'candidates', '[]'::jsonb,
                'needs_observation', false
            );
            v_progress := v_progress || pg_catalog.jsonb_build_object(
                'snapshot_digest', v_snapshot_digest, 'total', v_total,
                'scheduled', v_scheduled, 'skipped', v_skipped,
                'last_advance_lease_token', p_lease_token,
                'last_advance_batch_size', p_batch_size,
                'last_advance_result', v_data
            );
            BEGIN
                SELECT job.* INTO v_job FROM public.asset_image_jobs AS job
                WHERE job.job_id = p_job_id FOR UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
            END;
            IF NOT FOUND OR v_job.status <> 'running'
               OR v_job.generation <> p_generation
               OR v_job.lease_token IS DISTINCT FROM p_lease_token
               OR v_job.lease_expires_at <= pg_catalog.clock_timestamp() THEN
                RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
            END IF;
            UPDATE public.asset_image_jobs AS job
            SET status = 'ready', progress = v_progress,
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                last_lease_token = p_lease_token, last_outcome = 'published',
                completed_at = pg_catalog.clock_timestamp(), updated_at = pg_catalog.clock_timestamp()
            WHERE job.job_id = p_job_id;
            RETURN aap_assets_v1._image_reply(v_data, NULL);
        END IF;
        v_batch_token := pg_catalog.gen_random_uuid();
        v_pending := pg_catalog.jsonb_build_object(
            'batch_token', v_batch_token,
            'lease_token', p_lease_token,
            'batch_size', p_batch_size,
            'items', v_page_items,
            'after_owner_kind', v_page -> 'after_owner_kind',
            'after_owner_id', v_page -> 'after_owner_id',
            'next_owner_kind', v_page -> 'next_owner_kind',
            'next_owner_id', v_page -> 'next_owner_id',
            'complete', v_page -> 'complete',
            'snapshot_digest', v_snapshot_digest,
            'total', v_total
        );
        v_progress := v_progress || pg_catalog.jsonb_build_object(
            'snapshot_digest', v_snapshot_digest,
            'total', v_total,
            'scheduled', v_scheduled,
            'skipped', v_skipped,
            'pending_batch', v_pending
        );
        BEGIN
            SELECT job.* INTO v_job FROM public.asset_image_jobs AS job
            WHERE job.job_id = p_job_id FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        END;
        IF NOT FOUND OR v_job.status <> 'running'
           OR v_job.generation <> p_generation
           OR v_job.lease_token IS DISTINCT FROM p_lease_token
           OR v_job.lease_expires_at <= pg_catalog.clock_timestamp() THEN
            IF FOUND AND v_job.status = 'ready'
               AND v_job.generation = p_generation
               AND v_job.progress ->> 'last_advance_lease_token' = p_lease_token::text
               AND v_job.progress ->> 'last_advance_batch_size' = p_batch_size::text
               AND pg_catalog.jsonb_typeof(v_job.progress -> 'last_advance_result') = 'object' THEN
                RETURN aap_assets_v1._image_reply(
                    v_job.progress -> 'last_advance_result', NULL
                );
            END IF;
            RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
        END IF;
        v_existing_pending := v_job.progress -> 'pending_batch';
        IF pg_catalog.jsonb_typeof(v_existing_pending) = 'object'
           AND v_existing_pending ->> 'lease_token' = p_lease_token::text THEN
            IF v_existing_pending ->> 'batch_size' IS DISTINCT FROM p_batch_size::text THEN
                RETURN aap_assets_v1._image_reply(NULL, 'operation_conflict');
            END IF;
            v_data := pg_catalog.jsonb_build_object(
                'job_id', p_job_id,
                'generation', p_generation,
                'lease_token', p_lease_token,
                'scheduled', COALESCE((v_job.progress ->> 'scheduled')::bigint, 0),
                'skipped', COALESCE((v_job.progress ->> 'skipped')::bigint, 0),
                'complete', false,
                'next_owner_kind', v_existing_pending -> 'next_owner_kind',
                'next_owner_id', v_existing_pending -> 'next_owner_id',
                'batch_token', v_existing_pending -> 'batch_token',
                'candidates', v_existing_pending -> 'items',
                'needs_observation', true
            );
            RETURN aap_assets_v1._image_reply(v_data, NULL);
        END IF;
        v_data := pg_catalog.jsonb_build_object(
            'job_id', p_job_id,
            'generation', p_generation,
            'lease_token', p_lease_token,
            'scheduled', 0,
            'skipped', 0,
            'complete', false,
            'next_owner_kind', v_pending -> 'next_owner_kind',
            'next_owner_id', v_pending -> 'next_owner_id',
            'batch_token', v_batch_token,
            'candidates', v_page_items,
            'needs_observation', true
        );
        IF pg_catalog.octet_length(pg_catalog.convert_to(v_data::text, 'UTF8')) > 15728640 THEN
            RETURN aap_assets_v1._image_reply(NULL, 'resource_budget_exceeded');
        END IF;
        UPDATE public.asset_image_jobs AS job
        SET progress = v_progress, updated_at = pg_catalog.clock_timestamp()
        WHERE job.job_id = p_job_id;
        RETURN aap_assets_v1._image_reply(v_data, NULL);
    END IF;

    -- Validate the observation batch and its one-to-one relation with the staged page.
    IF NOT aap_assets_v1._media_object_has_only(
        p_observations, ARRAY['batch_token', 'items']
    ) OR NOT (p_observations ?& ARRAY['batch_token', 'items'])
       OR pg_catalog.jsonb_typeof(p_observations -> 'batch_token') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_observations -> 'items') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(p_observations -> 'items') NOT BETWEEN 1 AND p_batch_size THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        v_batch_token := (p_observations ->> 'batch_token')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END;
    v_pending := v_progress -> 'pending_batch';
    IF pg_catalog.jsonb_typeof(v_pending) IS DISTINCT FROM 'object'
       OR v_pending ->> 'batch_token' IS DISTINCT FROM v_batch_token::text
       OR v_pending ->> 'lease_token' IS DISTINCT FROM p_lease_token::text
       OR v_pending ->> 'batch_size' IS DISTINCT FROM p_batch_size::text THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_conflict');
    END IF;
    v_page_items := v_pending -> 'items';
    IF pg_catalog.jsonb_array_length(v_page_items)
       <> pg_catalog.jsonb_array_length(p_observations -> 'items') THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_array_elements(p_observations -> 'items') AS entry(value)
        WHERE NOT aap_assets_v1._media_object_has_only(
            entry.value, ARRAY['owner', 'source_snapshot', 'object_identity', 'error_code']
        ) OR NOT (entry.value ?& ARRAY['owner', 'source_snapshot', 'object_identity', 'error_code'])
          OR pg_catalog.jsonb_typeof(entry.value -> 'owner') IS DISTINCT FROM 'object'
          OR NOT aap_assets_v1._media_object_has_only(
                entry.value -> 'owner', ARRAY['kind', 'id']
          ) OR NOT (entry.value -> 'owner' ?& ARRAY['kind', 'id'])
          OR entry.value #>> '{owner,kind}' IS DISTINCT FROM v_selection ->> 'owner_kind'
          OR pg_catalog.jsonb_typeof(entry.value #> '{owner,id}') IS DISTINCT FROM 'string'
          OR pg_catalog.jsonb_typeof(entry.value -> 'source_snapshot') IS DISTINCT FROM 'string'
          OR entry.value ->> 'source_snapshot' !~ '^[0-9a-f]{64}$'
          OR (entry.value -> 'object_identity' = 'null'::jsonb)
                = (entry.value -> 'error_code' = 'null'::jsonb)
          OR (entry.value -> 'object_identity' <> 'null'::jsonb
              AND NOT aap_assets_v1._media_object_identity_valid(entry.value -> 'object_identity'))
          OR (entry.value -> 'error_code' <> 'null'::jsonb
              AND (pg_catalog.jsonb_typeof(entry.value -> 'error_code') IS DISTINCT FROM 'string'
                   OR entry.value ->> 'error_code' NOT IN ('source_missing', 'source_changed')))
    ) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_array_elements(v_page_items) AS candidate(value)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(p_observations -> 'items') AS observed(value)
            WHERE observed.value #>> '{owner,id}' = candidate.value #>> '{owner,id}'
              AND observed.value #>> '{owner,kind}' = candidate.value #>> '{owner,kind}'
              AND observed.value ->> 'source_snapshot' = candidate.value ->> 'source_snapshot'
        )
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_array_elements(p_observations -> 'items') AS observed(value)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(v_page_items) AS candidate(value)
            WHERE candidate.value #>> '{owner,id}' = observed.value #>> '{owner,id}'
              AND candidate.value #>> '{owner,kind}' = observed.value #>> '{owner,kind}'
              AND candidate.value ->> 'source_snapshot' = observed.value ->> 'source_snapshot'
        )
    ) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;

    v_snapshot_digest := v_pending ->> 'snapshot_digest';
    v_total := (v_pending ->> 'total')::bigint;
    v_scheduled := COALESCE((v_progress ->> 'scheduled')::bigint, 0);
    v_skipped := COALESCE((v_progress ->> 'skipped')::bigint, 0);
    v_accepted_before := v_candidate.backfill_cutoff;
    v_cursor_kind := v_pending ->> 'after_owner_kind';
    IF v_pending -> 'after_owner_id' <> 'null'::jsonb THEN
        BEGIN v_cursor_id := (v_pending ->> 'after_owner_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END;
    END IF;
    v_next_kind := v_pending ->> 'next_owner_kind';
    IF v_pending -> 'next_owner_id' <> 'null'::jsonb THEN
        BEGIN v_next_id := (v_pending ->> 'next_owner_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END;
    END IF;
    v_complete := v_pending -> 'complete' = 'true'::jsonb;
    v_owner_kind := v_selection ->> 'owner_kind';
    v_purpose := v_selection ->> 'purpose';
    v_only_missing := COALESCE(v_selection -> 'only_missing' = 'true'::jsonb, true);

    BEGIN
        -- Observe and lock the complete staged batch before admitting children.
        -- Drifted, removed and relinked items are skipped in the second pass;
        -- source contention rolls this whole savepoint back for a retry.
        FOR v_observation IN
            SELECT entry.value
            FROM pg_catalog.jsonb_array_elements(p_observations -> 'items') AS entry(value)
            ORDER BY entry.value #>> '{owner,kind}', (entry.value #>> '{owner,id}')::uuid
        LOOP
            v_owner_kind := v_observation #>> '{owner,kind}';
            v_owner_id := (v_observation #>> '{owner,id}')::uuid;
            v_source_snapshot := v_observation ->> 'source_snapshot';
            v_identity := v_observation -> 'object_identity';
            IF v_identity IS NOT NULL AND v_identity <> 'null'::jsonb THEN
                v_owner_reply := aap_assets_v1._image_observe_owner(
                    v_owner_kind, v_owner_id, v_source_snapshot, v_identity
                );
            ELSE
                v_owner_reply := aap_assets_v1._image_resolve_owner(
                    v_owner_kind, v_owner_id, true
                );
            END IF;
            IF v_owner_reply IS NULL
               OR v_owner_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
               OR pg_catalog.jsonb_typeof(v_owner_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
            END IF;
            IF v_owner_reply -> 'ok' = 'false'::jsonb THEN
                v_source_error := v_owner_reply #>> '{error,code}';
                IF v_source_error IN ('source_busy', 'task_busy') THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_source_busy';
                ELSIF v_source_error IS NULL THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
                ELSIF v_source_error NOT IN (
                    'source_missing', 'source_changed', 'owner_changed',
                    'source_not_ready', 'not_image'
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
                END IF;
                CONTINUE;
            END IF;
            v_owner_data := v_owner_reply -> 'data';
            IF NOT aap_assets_v1._image_source_data_valid(v_owner_data)
               OR v_owner_data #>> '{owner,kind}' IS DISTINCT FROM v_owner_kind
               OR v_owner_data #>> '{owner,id}' IS DISTINCT FROM v_owner_id::text THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
            END IF;
        END LOOP;

        FOR v_observation IN
            SELECT entry.value
            FROM pg_catalog.jsonb_array_elements(p_observations -> 'items') AS entry(value)
            ORDER BY entry.value #>> '{owner,kind}', (entry.value #>> '{owner,id}')::uuid
        LOOP
            v_source_error := NULL;
            v_owner_kind := v_observation #>> '{owner,kind}';
            v_owner_id := (v_observation #>> '{owner,id}')::uuid;
            v_source_snapshot := v_observation ->> 'source_snapshot';
            v_identity := v_observation -> 'object_identity';
            v_error_code := v_observation ->> 'error_code';
            v_owner_data := NULL;
            IF v_identity IS NOT NULL AND v_identity <> 'null'::jsonb THEN
                v_owner_reply := aap_assets_v1._image_observe_owner(
                    v_owner_kind, v_owner_id, v_source_snapshot, v_identity
                );
            ELSE
                v_owner_reply := aap_assets_v1._image_resolve_owner(
                    v_owner_kind, v_owner_id, true
                );
            END IF;
            IF v_owner_reply IS NULL
               OR v_owner_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
               OR pg_catalog.jsonb_typeof(v_owner_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
            END IF;
            IF v_owner_reply -> 'ok' = 'true'::jsonb THEN
                v_owner_data := v_owner_reply -> 'data';
                IF pg_catalog.jsonb_typeof(v_owner_data) IS DISTINCT FROM 'object'
                   OR v_owner_data #>> '{owner,kind}' IS DISTINCT FROM v_owner_kind
                   OR v_owner_data #>> '{owner,id}' IS DISTINCT FROM v_owner_id::text THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
                END IF;
            ELSE
                v_source_error := v_owner_reply #>> '{error,code}';
                IF v_source_error IN ('source_busy', 'task_busy') THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_source_busy';
                ELSIF v_source_error IS NULL OR v_source_error NOT IN (
                    'source_missing', 'source_changed', 'owner_changed',
                    'source_not_ready', 'not_image'
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
                END IF;
            END IF;
            IF v_owner_data IS NULL
               OR v_source_error IN ('source_missing', 'source_changed', 'owner_changed', 'not_image')
               OR v_observation -> 'error_code' <> 'null'::jsonb
               OR v_owner_data -> 'source_drifted' = 'true'::jsonb
               OR v_owner_data -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb
               OR v_owner_data -> 'file_type' IS DISTINCT FROM '"image"'::jsonb
               OR v_owner_data ->> 'source_snapshot' IS DISTINCT FROM v_source_snapshot
               OR (v_identity IS NOT NULL AND v_identity <> 'null'::jsonb
                   AND NOT aap_assets_v1._image_exec_identity_equal(
                        v_owner_data -> 'object_identity', v_identity
                   )) THEN
                v_page_skipped := v_page_skipped + 1;
                v_source_error := NULL;
                CONTINUE;
            END IF;
            v_child_kind := v_purpose;
            v_child_generate_preview := false;
            v_need_work := true;
            IF v_purpose = 'thumbnail' THEN
                IF v_owner_data -> 'width' = 'null'::jsonb
                   OR v_owner_data -> 'height' = 'null'::jsonb THEN
                    v_child_kind := 'dimensions';
                    v_child_generate_preview := true;
                ELSE
                    v_pixels := (v_owner_data ->> 'width')::numeric
                        * (v_owner_data ->> 'height')::numeric;
                    IF v_pixels >= (v_spec #>> '{policy,optional_pixels}')::numeric THEN
                        IF v_spec #>> '{policy,auto_generate_pyramids}' = 'true' THEN
                            v_child_kind := 'pyramid';
                        ELSE
                            v_need_work := false;
                        END IF;
                    END IF;
                END IF;
            END IF;
            IF v_only_missing AND v_need_work THEN
                IF v_child_kind = 'dimensions' THEN
                    v_need_work := v_owner_data -> 'width' = 'null'::jsonb
                        OR v_owner_data -> 'height' = 'null'::jsonb;
                ELSIF v_child_kind = 'thumbnail' THEN
                    v_need_work := v_owner_data -> 'thumbnail_path' = 'null'::jsonb
                        OR v_owner_data -> 'thumbnail_source_generation' = 'null'::jsonb
                        OR (v_owner_data ->> 'thumbnail_source_generation')::bigint
                            <> (v_owner_data ->> 'source_generation')::bigint;
                ELSIF v_child_kind = 'pyramid' THEN
                    IF v_owner_kind = 'dataset_item' THEN
                        SELECT asset.id, asset.active_generation INTO v_asset_id, v_active_generation
                        FROM public.image_pyramid_assets AS asset
                        WHERE asset.dataset_item_id = v_owner_id
                          AND asset.profile_version = v_spec #>> '{policy,profile_version}';
                    ELSE
                        SELECT asset.id, asset.active_generation INTO v_asset_id, v_active_generation
                        FROM public.image_pyramid_assets AS asset
                        WHERE asset.task_id = v_owner_id
                          AND asset.profile_version = v_spec #>> '{policy,profile_version}';
                    END IF;
                    IF v_asset_id IS NOT NULL AND v_active_generation IS NOT NULL THEN
                        SELECT generation.* INTO v_generation
                        FROM public.image_pyramid_generations AS generation
                        WHERE generation.asset_id = v_asset_id
                          AND generation.generation = v_active_generation
                          AND generation.status = 'ready'
                          AND generation.source_generation = (v_owner_data ->> 'source_generation')::bigint
                          AND generation.source_snapshot = v_owner_data ->> 'source_snapshot';
                        v_need_work := NOT FOUND;
                    END IF;
                END IF;
            END IF;
            IF NOT v_need_work THEN
                v_page_skipped := v_page_skipped + 1;
                CONTINUE;
            END IF;
            v_child_spec := pg_catalog.jsonb_build_object(
                'kind', v_child_kind,
                'expected_source_snapshot', v_owner_data -> 'source_snapshot',
                'policy', v_spec -> 'policy'
            );
            IF v_child_kind = 'dimensions' AND v_child_generate_preview THEN
                v_child_spec := v_child_spec || pg_catalog.jsonb_build_object(
                    'generate_preview', true
                );
            END IF;
            v_enqueue := aap_assets_v1._image_enqueue(
                NULL, NULL, v_owner_data, v_child_spec, NOT v_only_missing
            );
            IF v_enqueue IS NULL
               OR v_enqueue ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
               OR pg_catalog.jsonb_typeof(v_enqueue -> 'ok') IS DISTINCT FROM 'boolean' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
            END IF;
            IF v_enqueue -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                v_source_error := v_enqueue #>> '{error,code}';
                IF v_source_error IN ('source_busy', 'operation_busy') THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_source_busy';
                ELSIF v_source_error = 'operation_conflict' THEN
                    v_page_skipped := v_page_skipped + 1;
                    CONTINUE;
                ELSIF v_source_error IN (
                    'source_changed', 'source_not_ready', 'source_missing',
                    'owner_changed', 'not_image'
                ) THEN
                    v_page_skipped := v_page_skipped + 1;
                    CONTINUE;
                END IF;
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
            END IF;
            v_enqueue_data := v_enqueue -> 'data';
            IF pg_catalog.jsonb_typeof(v_enqueue_data) IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_enqueue_data -> 'work') IS DISTINCT FROM 'object'
               OR v_enqueue_data #>> '{work,job_id}' IS NULL
               OR v_enqueue_data #>> '{work,status}' NOT IN ('pending', 'running', 'ready', 'failed', 'canceled') THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
            END IF;
            IF v_enqueue_data #>> '{work,status}' IN ('pending', 'running') THEN
                v_page_scheduled := v_page_scheduled + 1;
            ELSE
                v_page_skipped := v_page_skipped + 1;
            END IF;
        END LOOP;

        BEGIN
            SELECT job.* INTO v_job FROM public.asset_image_jobs AS job
            WHERE job.job_id = p_job_id FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_operation_busy';
        END;
        IF NOT FOUND OR v_job.status <> 'running'
           OR v_job.generation <> p_generation
           OR v_job.lease_token IS DISTINCT FROM p_lease_token
           OR v_job.lease_expires_at <= pg_catalog.clock_timestamp()
           OR v_job.progress #>> '{pending_batch,batch_token}' IS DISTINCT FROM v_batch_token::text THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_lease_lost';
        END IF;
        v_scheduled := v_scheduled + v_page_scheduled;
        v_skipped := v_skipped + v_page_skipped;
        v_data := pg_catalog.jsonb_build_object(
            'job_id', p_job_id,
            'generation', p_generation,
            'lease_token', p_lease_token,
            'scheduled', v_scheduled,
            'skipped', v_skipped,
            'complete', v_complete,
            'next_owner_kind', CASE WHEN v_complete THEN NULL ELSE v_next_kind END,
            'next_owner_id', CASE WHEN v_complete THEN NULL ELSE v_next_id END,
            'batch_token', v_batch_token,
            'candidates', '[]'::jsonb,
            'needs_observation', false
        );
        v_progress := (v_job.progress - 'pending_batch') || pg_catalog.jsonb_build_object(
            'snapshot_digest', v_snapshot_digest,
            'total', v_total,
            'scheduled', v_scheduled,
            'skipped', v_skipped,
            'last_advance_lease_token', p_lease_token,
            'last_advance_batch_token', v_batch_token,
            'last_advance_observations_digest', v_observations_digest,
            'last_advance_result', v_data
        );
        UPDATE public.asset_image_jobs AS job
        SET status = CASE WHEN v_complete THEN 'ready' ELSE 'running' END,
            backfill_cursor_kind = CASE WHEN v_complete THEN NULL ELSE v_next_kind END,
            backfill_cursor_id = CASE WHEN v_complete THEN NULL ELSE v_next_id END,
            progress = v_progress,
            lease_owner = CASE WHEN v_complete THEN NULL ELSE job.lease_owner END,
            lease_token = CASE WHEN v_complete THEN NULL ELSE job.lease_token END,
            lease_expires_at = CASE WHEN v_complete THEN NULL ELSE job.lease_expires_at END,
            last_lease_token = CASE WHEN v_complete THEN p_lease_token ELSE NULL END,
            last_outcome = CASE WHEN v_complete THEN 'published' ELSE NULL END,
            completed_at = CASE WHEN v_complete THEN pg_catalog.clock_timestamp() ELSE NULL END,
            updated_at = pg_catalog.clock_timestamp()
        WHERE job.job_id = p_job_id;
    EXCEPTION WHEN SQLSTATE 'P0001' THEN
        v_failure := SQLERRM;
    END;
    IF v_failure = 'image_source_busy' OR v_failure = 'image_operation_busy' THEN
        RETURN aap_assets_v1._image_reply(NULL,
            CASE WHEN v_failure = 'image_source_busy' THEN 'source_busy' ELSE 'operation_busy' END);
    ELSIF v_failure = 'image_lease_lost' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    ELSIF v_failure IS NOT NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    RETURN aap_assets_v1._image_reply(v_data, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.advance_image_backfill(uuid, bigint, uuid, integer, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.reconcile_image_media(
    p_candidates jsonb, p_batch_size integer
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
    v_source_reply jsonb;
    v_source jsonb;
    v_source_code text;
    v_spec jsonb;
    v_policy jsonb;
    v_generation_present boolean;
    v_asset_present boolean;
    v_stale boolean;
    v_outcome text;
    v_prefix text;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_deadline timestamptz;
    v_work integer := 0;
    v_candidate_index integer := 0;
    v_input_count integer;
    v_expired bigint := 0;
    v_requeued bigint := 0;
    v_failed bigint := 0;
    v_canceled bigint := 0;
    v_retired bigint := 0;
    v_orphans bigint := 0;
    v_ignored bigint := 0;
    v_more boolean := false;
    v_entry jsonb;
    v_asset_id uuid;
    v_generation_number integer;
    v_last_modified timestamptz;
    v_current_job public.asset_image_jobs%ROWTYPE;
    v_owner_kind text;
    v_owner_id uuid;
    v_owner_reply jsonb;
    v_existing boolean;
    v_json_result jsonb;
BEGIN
    IF p_batch_size IS NULL OR p_batch_size NOT BETWEEN 1 AND 500
       OR p_candidates IS NULL OR pg_catalog.jsonb_typeof(p_candidates) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(p_candidates) > 100
       OR pg_catalog.octet_length(pg_catalog.convert_to(p_candidates::text, 'UTF8')) > 8388608 THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_input_count := pg_catalog.jsonb_array_length(p_candidates);
    -- Validate the complete candidate batch before any reconciliation writes.
    FOR v_entry IN SELECT entry.value FROM pg_catalog.jsonb_array_elements(p_candidates) AS entry(value)
    LOOP
        IF NOT aap_assets_v1._media_object_has_only(
            v_entry, ARRAY['asset_id', 'generation', 'last_modified']
        ) OR NOT (v_entry ?& ARRAY['asset_id', 'generation', 'last_modified'])
           OR pg_catalog.jsonb_typeof(v_entry -> 'asset_id') IS DISTINCT FROM 'string'
           OR v_entry ->> 'asset_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR NOT aap_assets_v1._image_exec_integer_valid(v_entry -> 'generation', 1, 2147483647)
           OR pg_catalog.jsonb_typeof(v_entry -> 'last_modified') IS DISTINCT FROM 'string'
           OR v_entry ->> 'last_modified' !~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        BEGIN
            PERFORM (v_entry ->> 'asset_id')::uuid;
            PERFORM (v_entry ->> 'last_modified')::timestamptz;
        EXCEPTION WHEN invalid_text_representation OR datetime_field_overflow THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END;
    END LOOP;
    IF (
        SELECT pg_catalog.count(*) <> pg_catalog.count(DISTINCT
            (entry.value ->> 'asset_id') || ':' || (entry.value ->> 'generation')
        )
        FROM pg_catalog.jsonb_array_elements(p_candidates) AS entry(value)
    ) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;

    -- Expired jobs are handled source first, then Job, then pyramid Asset/Generation.
    FOR v_candidate IN
        SELECT job.job_id, job.owner_kind, job.owner_id
        FROM public.asset_image_jobs AS job
        WHERE job.status = 'running'
          AND job.lease_expires_at <= pg_catalog.clock_timestamp()
        ORDER BY job.lease_expires_at, job.accepted_at, job.job_id
        LIMIT p_batch_size
    LOOP
        EXIT WHEN v_work >= p_batch_size;
        v_work := v_work + 1;
        v_source := NULL;
        v_source_code := NULL;
        IF v_candidate.owner_id IS NOT NULL THEN
            v_source_reply := aap_assets_v1._image_resolve_owner(
                v_candidate.owner_kind, v_candidate.owner_id, true
            );
            IF v_source_reply IS NULL
               OR v_source_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
               OR pg_catalog.jsonb_typeof(v_source_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
            END IF;
            IF v_source_reply -> 'ok' = 'true'::jsonb THEN
                v_source := v_source_reply -> 'data';
                IF pg_catalog.jsonb_typeof(v_source) IS DISTINCT FROM 'object'
                   OR v_source #>> '{owner,kind}' IS DISTINCT FROM v_candidate.owner_kind
                   OR v_source #>> '{owner,id}' IS DISTINCT FROM v_candidate.owner_id::text THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
                END IF;
            ELSE
                v_source_code := v_source_reply #>> '{error,code}';
                IF v_source_code IN ('source_busy', 'task_busy') THEN
                    RAISE lock_not_available USING MESSAGE = 'image source is busy';
                END IF;
                IF v_source_code IS NULL THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
                END IF;
            END IF;
        END IF;
        BEGIN
            SELECT job.* INTO v_job FROM public.asset_image_jobs AS job
            WHERE job.job_id = v_candidate.job_id FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RAISE lock_not_available USING MESSAGE = 'image job is busy';
        END;
        IF NOT FOUND OR v_job.status <> 'running'
           OR v_job.lease_expires_at > pg_catalog.clock_timestamp() THEN
            CONTINUE;
        END IF;
        v_expired := v_expired + 1;
        v_spec := aap_assets_v1._image_spec(v_job.spec);
        IF v_spec IS NULL OR v_spec IS DISTINCT FROM v_job.spec THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
        END IF;
        v_policy := v_spec -> 'policy';
        v_stale := v_source_code IS NOT NULL;
        IF v_job.owner_id IS NOT NULL AND NOT v_stale THEN
            v_stale := v_source -> 'source_drifted' = 'true'::jsonb
                OR v_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb
                OR v_source -> 'file_type' IS DISTINCT FROM '"image"'::jsonb
                OR v_source ->> 'source_snapshot' IS DISTINCT FROM v_job.source_snapshot
                OR (v_source ->> 'source_generation')::bigint IS DISTINCT FROM v_job.source_generation
                OR NOT aap_assets_v1._image_exec_identity_equal(
                    v_source -> 'object_identity', v_job.source_facts -> 'object_identity'
                )
                OR (v_source -> 'file_size' IS NOT NULL
                    AND v_source -> 'file_size' <> 'null'::jsonb
                    AND (v_source ->> 'file_size')::bigint
                        IS DISTINCT FROM (v_source #>> '{object_identity,byte_size}')::bigint);
        END IF;
        v_asset_present := false;
        v_generation_present := false;
        IF v_job.kind = 'pyramid' THEN
            BEGIN
                SELECT asset.* INTO v_asset
                FROM public.image_pyramid_assets AS asset
                WHERE asset.id = v_job.pyramid_asset_id FOR UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RAISE lock_not_available USING MESSAGE = 'image pyramid asset is busy';
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
                    RAISE lock_not_available USING MESSAGE = 'image pyramid generation is busy';
                END;
                v_generation_present := FOUND;
            END IF;
            IF NOT v_asset_present OR NOT v_generation_present
               OR v_asset.building_generation IS DISTINCT FROM v_job.pyramid_generation
               OR v_generation.job_id IS DISTINCT FROM v_job.job_id
               OR v_generation.job_generation IS DISTINCT FROM v_job.generation
               OR v_generation.lease_token IS DISTINCT FROM v_job.lease_token
               OR v_generation.status <> 'building' THEN
                v_stale := true;
            END IF;
        END IF;
        IF v_job.output_prefix IS NOT NULL THEN
            v_prefix := v_job.output_prefix;
            PERFORM aap_assets_v1._queue_image_garbage(
                v_prefix,
                CASE WHEN v_stale THEN 'source_changed' ELSE 'attempt_abandoned' END,
                GREATEST(pg_catalog.clock_timestamp(), v_job.lease_expires_at)
                    + pg_catalog.make_interval(hours => (v_policy ->> 'gc_grace_hours')::integer)
            );
        END IF;
        IF v_stale THEN
            v_outcome := CASE WHEN v_job.kind = 'pyramid' AND (
                    NOT v_asset_present OR NOT v_generation_present
                    OR v_asset.building_generation IS DISTINCT FROM v_job.pyramid_generation
                    OR v_generation.job_id IS DISTINCT FROM v_job.job_id
                     OR v_generation.job_generation IS DISTINCT FROM v_job.generation
                     OR v_generation.status <> 'building')
                THEN 'dependency_changed' ELSE 'source_changed' END;
            IF v_generation_present AND v_generation.status IN ('pending', 'building') THEN
                UPDATE public.image_pyramid_generations AS generation
                SET status = 'failed', error_code = v_outcome,
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
            SET status = 'canceled', error_code = v_outcome,
                error_detail = COALESCE(v_source_code, v_outcome),
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                last_lease_token = v_job.lease_token,
                last_outcome = v_outcome,
                completed_at = pg_catalog.clock_timestamp(), updated_at = pg_catalog.clock_timestamp()
            WHERE job.job_id = v_job.job_id;
            v_canceled := v_canceled + 1;
            CONTINUE;
        END IF;
        IF v_job.attempt_count >= 3 THEN
            IF v_generation_present AND v_generation.status = 'building' THEN
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
                last_lease_token = v_job.lease_token, last_outcome = 'failed',
                completed_at = pg_catalog.clock_timestamp(), updated_at = pg_catalog.clock_timestamp()
            WHERE job.job_id = v_job.job_id;
            v_failed := v_failed + 1;
        ELSE
            IF v_generation_present AND v_generation.status = 'building' THEN
                UPDATE public.image_pyramid_generations AS generation
                SET status = 'pending', error_code = NULL,
                    lease_token = NULL, lease_expires_at = NULL,
                    output_prefix = NULL, updated_at = pg_catalog.clock_timestamp()
                WHERE generation.asset_id = v_job.pyramid_asset_id
                  AND generation.generation = v_job.pyramid_generation;
                UPDATE public.image_pyramid_assets AS asset
                SET building_generation = v_job.pyramid_generation,
                    updated_at = pg_catalog.clock_timestamp()
                WHERE asset.id = v_job.pyramid_asset_id;
            END IF;
            UPDATE public.asset_image_jobs AS job
            SET status = 'pending', error_code = NULL, error_detail = NULL,
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                progress = CASE WHEN job.kind = 'image_backfill'
                    THEN job.progress - 'pending_batch' ELSE '{}'::jsonb END,
                last_lease_token = NULL, last_outcome = NULL,
                completed_at = NULL, updated_at = pg_catalog.clock_timestamp()
            WHERE job.job_id = v_job.job_id;
            v_requeued := v_requeued + 1;
        END IF;
    END LOOP;

    -- Retire inactive durable generation prefixes only after the captured grace.
    IF v_work < p_batch_size THEN
        FOR v_candidate IN
            SELECT asset.id AS asset_id, asset.dataset_item_id, asset.task_id,
                   generation.generation
            FROM public.image_pyramid_generations AS generation
            JOIN public.image_pyramid_assets AS asset ON asset.id = generation.asset_id
            WHERE generation.status IN ('ready', 'failed')
              AND generation.generation IS DISTINCT FROM asset.active_generation
              AND generation.generation IS DISTINCT FROM asset.building_generation
              AND (generation.lease_expires_at IS NULL
                   OR generation.lease_expires_at <= pg_catalog.clock_timestamp())
              AND generation.updated_at <= pg_catalog.clock_timestamp()
                    - pg_catalog.make_interval(hours => COALESCE(
                        (generation.policy ->> 'gc_grace_hours')::integer, 24
                    ))
              AND NOT EXISTS (
                    SELECT 1 FROM public.asset_image_garbage AS garbage
                    WHERE garbage.prefix = 'image-pyramids/' || asset.id::text
                        || '/g' || generation.generation::text || '/'
              )
            ORDER BY generation.updated_at, generation.asset_id, generation.generation
            LIMIT (p_batch_size - v_work)
        LOOP
            v_work := v_work + 1;
            v_owner_kind := CASE WHEN v_candidate.dataset_item_id IS NOT NULL
                THEN 'dataset_item' ELSE 'task' END;
            v_owner_id := COALESCE(v_candidate.dataset_item_id, v_candidate.task_id);
            v_source_reply := aap_assets_v1._image_resolve_owner(
                v_owner_kind, v_owner_id, true
            );
            IF v_source_reply IS NULL
               OR v_source_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
               OR pg_catalog.jsonb_typeof(v_source_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
            END IF;
            IF v_source_reply -> 'ok' = 'true'::jsonb THEN
                v_source := v_source_reply -> 'data';
                IF NOT aap_assets_v1._image_source_data_valid(v_source)
                   OR v_source #>> '{owner,kind}' IS DISTINCT FROM v_owner_kind
                   OR v_source #>> '{owner,id}' IS DISTINCT FROM v_owner_id::text THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
                END IF;
            ELSE
                v_source_code := v_source_reply #>> '{error,code}';
                IF v_source_code IN ('source_busy', 'task_busy') THEN
                    RAISE lock_not_available USING MESSAGE = 'image source is busy';
                ELSIF v_source_code IS NULL OR v_source_code NOT IN (
                    'source_missing', 'source_changed', 'owner_changed',
                    'source_not_ready', 'not_image'
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
                END IF;
            END IF;
            BEGIN
                SELECT asset.* INTO v_asset
                FROM public.image_pyramid_assets AS asset
                WHERE asset.id = v_candidate.asset_id FOR UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RAISE lock_not_available USING MESSAGE = 'image pyramid asset is busy';
            END;
            IF NOT FOUND THEN CONTINUE; END IF;
            BEGIN
                SELECT generation.* INTO v_generation
                FROM public.image_pyramid_generations AS generation
                WHERE generation.asset_id = v_candidate.asset_id
                  AND generation.generation = v_candidate.generation
                FOR UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RAISE lock_not_available USING MESSAGE = 'image pyramid generation is busy';
            END;
            IF NOT FOUND OR v_generation.status NOT IN ('ready', 'failed')
               OR v_generation.generation = v_asset.active_generation
               OR v_generation.generation = v_asset.building_generation
               OR (v_generation.lease_expires_at IS NOT NULL
                   AND v_generation.lease_expires_at > pg_catalog.clock_timestamp()) THEN
                CONTINUE;
            END IF;
            v_policy := aap_assets_v1._image_policy(COALESCE(v_generation.policy, '{}'::jsonb));
            IF v_policy IS NULL THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
            END IF;
            v_prefix := 'image-pyramids/' || v_candidate.asset_id::text
                || '/g' || v_candidate.generation::text || '/';
            PERFORM aap_assets_v1._queue_image_garbage(
                v_prefix, 'generation_retired',
                GREATEST(pg_catalog.clock_timestamp(), v_generation.updated_at
                    + pg_catalog.make_interval(hours => (v_policy ->> 'gc_grace_hours')::integer))
            );
            v_retired := v_retired + 1;
        END LOOP;
    END IF;

    -- S3 reconciliation receives only bounded, age-qualified generation scopes.
    WHILE v_candidate_index < v_input_count AND v_work < p_batch_size LOOP
        v_candidate_index := v_candidate_index + 1;
        v_work := v_work + 1;
        v_entry := p_candidates -> (v_candidate_index - 1);
        v_asset_id := (v_entry ->> 'asset_id')::uuid;
        v_generation_number := (v_entry ->> 'generation')::integer;
        v_last_modified := (v_entry ->> 'last_modified')::timestamptz;
        IF v_last_modified > pg_catalog.clock_timestamp() - interval '24 hours' THEN
            v_ignored := v_ignored + 1;
            CONTINUE;
        END IF;
        SELECT asset.* INTO v_asset
        FROM public.image_pyramid_assets AS asset
        WHERE asset.id = v_asset_id;
        IF NOT FOUND THEN
            v_ignored := v_ignored + 1;
            CONTINUE;
        END IF;
        v_owner_kind := CASE WHEN v_asset.dataset_item_id IS NOT NULL
            THEN 'dataset_item' ELSE 'task' END;
        v_owner_id := COALESCE(v_asset.dataset_item_id, v_asset.task_id);
        IF v_owner_id IS NULL OR (v_asset.dataset_item_id IS NOT NULL AND v_asset.task_id IS NOT NULL) THEN
            v_ignored := v_ignored + 1;
            CONTINUE;
        END IF;
        v_source_reply := aap_assets_v1._image_resolve_owner(v_owner_kind, v_owner_id, true);
        IF v_source_reply IS NULL
           OR v_source_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
           OR pg_catalog.jsonb_typeof(v_source_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
        END IF;
        IF v_source_reply -> 'ok' = 'true'::jsonb THEN
            v_source := v_source_reply -> 'data';
            IF NOT aap_assets_v1._image_source_data_valid(v_source)
               OR v_source #>> '{owner,kind}' IS DISTINCT FROM v_owner_kind
               OR v_source #>> '{owner,id}' IS DISTINCT FROM v_owner_id::text THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
            END IF;
        ELSE
            v_source_code := v_source_reply #>> '{error,code}';
            IF v_source_code IN ('source_busy', 'task_busy') THEN
                RAISE lock_not_available USING MESSAGE = 'image source is busy';
            ELSIF v_source_code IS NULL OR v_source_code NOT IN (
                'source_missing', 'source_changed', 'owner_changed',
                'source_not_ready', 'not_image'
            ) THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
            END IF;
        END IF;
        SELECT job.* INTO v_current_job
        FROM public.asset_image_jobs AS job
        WHERE job.owner_kind = v_owner_kind AND job.owner_id = v_owner_id
          AND job.pyramid_asset_id = v_asset_id AND job.pyramid_generation = v_generation_number
          AND job.status IN ('pending', 'running')
        ORDER BY job.job_id LIMIT 1;
        IF FOUND THEN
            BEGIN
                SELECT job.* INTO v_current_job
                FROM public.asset_image_jobs AS job
                WHERE job.job_id = v_current_job.job_id
                FOR UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RAISE lock_not_available USING MESSAGE = 'image job is busy';
            END;
            IF FOUND AND v_current_job.status IN ('pending', 'running') THEN
                v_ignored := v_ignored + 1;
                CONTINUE;
            END IF;
        END IF;
        BEGIN
            SELECT asset.* INTO v_asset
            FROM public.image_pyramid_assets AS asset
            WHERE asset.id = v_asset_id FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RAISE lock_not_available USING MESSAGE = 'image pyramid asset is busy';
        END;
        IF NOT FOUND OR v_asset.active_generation = v_generation_number
           OR v_asset.building_generation = v_generation_number THEN
            v_ignored := v_ignored + 1;
            CONTINUE;
        END IF;
        BEGIN
            SELECT generation.* INTO v_generation
            FROM public.image_pyramid_generations AS generation
            WHERE generation.asset_id = v_asset_id
              AND generation.generation = v_generation_number
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RAISE lock_not_available USING MESSAGE = 'image pyramid generation is busy';
        END;
        IF FOUND THEN
            v_ignored := v_ignored + 1;
            CONTINUE;
        END IF;
        v_prefix := 'image-pyramids/' || v_asset_id::text
            || '/g' || v_generation_number::text || '/';
        IF EXISTS (
            SELECT 1 FROM public.asset_image_garbage AS garbage
            WHERE garbage.prefix = v_prefix
        ) THEN
            v_ignored := v_ignored + 1;
            CONTINUE;
        END IF;
        PERFORM aap_assets_v1._queue_image_garbage(
            v_prefix, 'orphan_prefix', pg_catalog.clock_timestamp()
        );
        v_orphans := v_orphans + 1;
    END LOOP;

    v_more := v_candidate_index < v_input_count
        OR EXISTS (
            SELECT 1 FROM public.asset_image_jobs AS job
            WHERE job.status = 'running'
              AND job.lease_expires_at <= pg_catalog.clock_timestamp()
        )
        OR EXISTS (
            SELECT 1
            FROM public.image_pyramid_generations AS generation
            JOIN public.image_pyramid_assets AS asset ON asset.id = generation.asset_id
            WHERE generation.status IN ('ready', 'failed')
              AND generation.generation IS DISTINCT FROM asset.active_generation
              AND generation.generation IS DISTINCT FROM asset.building_generation
              AND (generation.lease_expires_at IS NULL
                   OR generation.lease_expires_at <= pg_catalog.clock_timestamp())
              AND generation.updated_at <= pg_catalog.clock_timestamp()
                    - pg_catalog.make_interval(hours => COALESCE(
                        (generation.policy ->> 'gc_grace_hours')::integer, 24
                    ))
              AND NOT EXISTS (
                    SELECT 1 FROM public.asset_image_garbage AS garbage
                    WHERE garbage.prefix = 'image-pyramids/' || generation.asset_id::text
                        || '/g' || generation.generation::text || '/'
              )
        );
    v_json_result := pg_catalog.jsonb_build_object(
        'expired_jobs', v_expired,
        'requeued_jobs', v_requeued,
        'failed_jobs', v_failed,
        'canceled_jobs', v_canceled,
        'retired_generations', v_retired,
        'queued_orphans', v_orphans,
        'ignored_candidates', v_ignored,
        'more', v_more
    );
    RETURN aap_assets_v1._image_reply(v_json_result, NULL);
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    WHEN SQLSTATE 'P0001' THEN
        IF SQLERRM = 'image_invalid_response' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.reconcile_image_media(jsonb, integer) FROM PUBLIC;
