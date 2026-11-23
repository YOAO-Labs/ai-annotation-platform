CREATE FUNCTION yoao_assets_v1.read_legacy_pyramid_asset(
    p_kind text, p_owner uuid, p_profile text
) RETURNS SETOF public.image_pyramid_assets LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT a.* FROM public.image_pyramid_assets AS a
    WHERE a.profile_version = p_profile
      AND ((p_kind = 'dataset_item' AND a.dataset_item_id = p_owner)
           OR (p_kind = 'task' AND a.task_id = p_owner));
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.read_legacy_pyramid_generation(
    p_asset uuid, p_generation integer
) RETURNS SETOF public.image_pyramid_generations LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT g.* FROM public.image_pyramid_generations AS g
    WHERE g.asset_id = p_asset AND (p_generation IS NULL OR g.generation = p_generation)
    ORDER BY g.generation DESC LIMIT 1;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.read_legacy_pyramid_summaries(
    p_items uuid[], p_tasks uuid[], p_profile text
) RETURNS TABLE(asset public.image_pyramid_assets, selected public.image_pyramid_generations)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT a, COALESCE(active, building, latest.selected)
    FROM public.image_pyramid_assets AS a
    LEFT JOIN public.image_pyramid_generations AS active
        ON active.asset_id = a.id AND active.generation = a.active_generation
        AND active.status = 'ready'
    LEFT JOIN public.image_pyramid_generations AS building
        ON building.asset_id = a.id AND building.generation = a.building_generation
    LEFT JOIN LATERAL (
        SELECT g AS selected FROM public.image_pyramid_generations AS g
        WHERE g.asset_id = a.id AND active.asset_id IS NULL AND building.asset_id IS NULL
        ORDER BY g.generation DESC LIMIT 1
    ) AS latest ON true
    WHERE a.profile_version = p_profile
      AND (a.dataset_item_id = ANY(p_items) OR a.task_id = ANY(p_tasks))
      AND cardinality(p_items) <= 5000 AND cardinality(p_tasks) <= 5000;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.prepare_legacy_pyramid_generation(
    p_asset uuid, p_kind text, p_owner uuid, p_profile text, p_identity text,
    p_original jsonb, p_authority jsonb, p_normalization text, p_force boolean,
    p_keep_inflight boolean
) RETURNS TABLE(reused_inflight boolean, selected public.image_pyramid_generations)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_asset public.image_pyramid_assets%ROWTYPE;
    v_generation public.image_pyramid_generations%ROWTYPE;
    v_number integer;
BEGIN
    IF p_kind NOT IN ('dataset_item', 'task') OR p_owner IS NULL
       OR p_original IS NULL OR p_authority -> 'original' IS DISTINCT FROM p_original THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid legacy pyramid source';
    END IF;
    INSERT INTO public.image_pyramid_assets (id, dataset_item_id, task_id, profile_version)
    VALUES (p_asset, CASE WHEN p_kind = 'dataset_item' THEN p_owner END,
            CASE WHEN p_kind = 'task' THEN p_owner END, p_profile)
    ON CONFLICT DO NOTHING;
    SELECT a.* INTO STRICT v_asset FROM public.image_pyramid_assets AS a
    WHERE a.profile_version = p_profile
      AND ((p_kind = 'dataset_item' AND a.dataset_item_id = p_owner)
           OR (p_kind = 'task' AND a.task_id = p_owner))
    FOR UPDATE;

    SELECT g.* INTO v_generation FROM public.image_pyramid_generations AS g
    WHERE g.asset_id = v_asset.id AND g.status IN ('pending', 'building') FOR UPDATE;
    IF FOUND THEN
        IF p_keep_inflight OR v_generation.job_id IS NOT NULL
           OR v_generation.status <> 'building' OR v_generation.lease_expires_at IS NULL
           OR v_generation.lease_expires_at > pg_catalog.clock_timestamp() THEN
            RETURN QUERY SELECT true, v_generation;
            RETURN;
        END IF;
        UPDATE public.image_pyramid_generations AS g
        SET status = 'failed', error_code = 'lease_expired', lease_token = NULL,
            lease_expires_at = NULL, updated_at = pg_catalog.now()
        WHERE g.asset_id = v_asset.id AND g.generation = v_generation.generation;
        UPDATE public.image_pyramid_assets AS a SET building_generation = NULL, updated_at = pg_catalog.now()
        WHERE a.id = v_asset.id AND a.building_generation = v_generation.generation;
    END IF;

    IF v_asset.active_generation IS NOT NULL THEN
        SELECT g.* INTO v_generation FROM public.image_pyramid_generations AS g
        WHERE g.asset_id = v_asset.id AND g.generation = v_asset.active_generation;
        IF FOUND AND v_generation.status = 'ready' AND v_generation.source_identity = p_identity THEN
            IF NOT p_force THEN
                RETURN QUERY SELECT false, v_generation;
                RETURN;
            END IF;
        ELSIF v_generation.job_id IS NULL THEN
            UPDATE public.image_pyramid_assets AS a
            SET active_generation = NULL, updated_at = pg_catalog.now() WHERE a.id = v_asset.id;
        END IF;
    END IF;

    SELECT COALESCE(max(g.generation), 0) + 1 INTO v_number
    FROM public.image_pyramid_generations AS g WHERE g.asset_id = v_asset.id;
    INSERT INTO public.image_pyramid_generations (
        asset_id, generation, source_identity, original_ref, auth_ref,
        status, normalization_version, tile_size, overlap
    ) VALUES (v_asset.id, v_number, p_identity, p_original, p_authority,
              'pending', p_normalization, 512, 1)
    RETURNING * INTO v_generation;
    UPDATE public.image_pyramid_assets AS a
    SET building_generation = v_number, updated_at = pg_catalog.now() WHERE a.id = v_asset.id;
    RETURN QUERY SELECT false, v_generation;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_legacy_pyramid_claim(p_asset uuid, p_generation integer)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_generation public.image_pyramid_generations%ROWTYPE;
BEGIN
    PERFORM 1 FROM public.image_pyramid_assets AS a
    WHERE a.id = p_asset AND a.building_generation = p_generation FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT g.* INTO v_generation FROM public.image_pyramid_generations AS g
    WHERE g.asset_id = p_asset AND g.generation = p_generation FOR UPDATE;
    RETURN FOUND AND v_generation.job_id IS NULL AND v_generation.original_ref IS NOT NULL
        AND v_generation.status IN ('pending', 'building')
        AND (v_generation.lease_expires_at IS NULL
             OR v_generation.lease_expires_at <= pg_catalog.clock_timestamp());
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.claim_legacy_pyramid_generation(
    p_asset uuid, p_generation integer, p_output uuid, p_token uuid,
    p_lease_seconds integer, p_ref jsonb
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NOT yoao_assets_v1.lock_legacy_pyramid_claim(p_asset, p_generation)
       OR p_lease_seconds IS NULL OR p_lease_seconds <= 0
       OR yoao_assets_v1._legacy_media_cache_target_current(p_ref) IS DISTINCT FROM true THEN
        RETURN false;
    END IF;
    INSERT INTO public.image_pyramid_outputs (id, asset_id, generation, lease_token, storage_ref, state)
    VALUES (p_output, p_asset, p_generation, p_token, p_ref, 'building');
    UPDATE public.image_pyramid_generations AS g
    SET status = 'building', error_code = NULL, lease_token = p_token, output_id = p_output,
        lease_expires_at = pg_catalog.clock_timestamp() + pg_catalog.make_interval(secs => p_lease_seconds),
        attempts = g.attempts + 1, updated_at = pg_catalog.now()
    WHERE g.asset_id = p_asset AND g.generation = p_generation;
    RETURN true;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_legacy_pyramid_publication(
    p_asset uuid, p_generation integer, p_token uuid, p_output uuid
) RETURNS TABLE(asset public.image_pyramid_assets, selected public.image_pyramid_generations)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_asset public.image_pyramid_assets%ROWTYPE;
    v_generation public.image_pyramid_generations%ROWTYPE;
BEGIN
    SELECT a.* INTO v_asset FROM public.image_pyramid_assets AS a WHERE a.id = p_asset FOR UPDATE;
    IF NOT FOUND OR v_asset.building_generation IS DISTINCT FROM p_generation THEN RETURN; END IF;
    SELECT g.* INTO v_generation FROM public.image_pyramid_generations AS g
    WHERE g.asset_id = p_asset AND g.generation = p_generation FOR UPDATE;
    IF NOT FOUND OR v_generation.job_id IS NOT NULL OR v_generation.status <> 'building'
       OR p_token IS NULL OR v_generation.lease_token IS DISTINCT FROM p_token
       OR v_generation.lease_expires_at IS NULL
       OR v_generation.lease_expires_at <= pg_catalog.clock_timestamp()
       OR v_generation.output_id IS DISTINCT FROM p_output THEN
        RETURN;
    END IF;
    RETURN QUERY SELECT v_asset, v_generation;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.read_legacy_pyramid_context(
    p_asset uuid, p_generation integer, p_output uuid, p_lock boolean
) RETURNS TABLE(asset public.image_pyramid_assets, selected public.image_pyramid_generations,
                output public.image_pyramid_outputs)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_asset public.image_pyramid_assets%ROWTYPE;
    v_generation public.image_pyramid_generations%ROWTYPE;
    v_output public.image_pyramid_outputs%ROWTYPE;
BEGIN
    IF p_lock THEN
        SELECT a.* INTO v_asset FROM public.image_pyramid_assets AS a WHERE a.id = p_asset FOR SHARE NOWAIT;
        SELECT g.* INTO v_generation FROM public.image_pyramid_generations AS g
        WHERE g.asset_id = p_asset AND g.generation = p_generation FOR SHARE NOWAIT;
        SELECT o.* INTO v_output FROM public.image_pyramid_outputs AS o
        WHERE o.id = p_output AND o.asset_id = p_asset AND o.generation = p_generation FOR SHARE NOWAIT;
    ELSE
        SELECT a.* INTO v_asset FROM public.image_pyramid_assets AS a WHERE a.id = p_asset;
        SELECT g.* INTO v_generation FROM public.image_pyramid_generations AS g
        WHERE g.asset_id = p_asset AND g.generation = p_generation;
        SELECT o.* INTO v_output FROM public.image_pyramid_outputs AS o
        WHERE o.id = p_output AND o.asset_id = p_asset AND o.generation = p_generation;
    END IF;
    IF v_asset.id IS NULL OR v_generation.asset_id IS NULL OR v_generation.job_id IS NOT NULL
       OR v_generation.output_id IS DISTINCT FROM p_output OR v_output.id IS NULL THEN
        RETURN;
    END IF;
    RETURN QUERY SELECT v_asset, v_generation, v_output;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.read_legacy_pyramid_output(p_output uuid, p_lock boolean)
RETURNS SETOF public.image_pyramid_outputs LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF p_lock THEN
        RETURN QUERY SELECT o.* FROM public.image_pyramid_outputs AS o
        WHERE o.id = p_output AND o.state IN ('ready', 'retiring') FOR SHARE NOWAIT;
    ELSE
        RETURN QUERY SELECT o.* FROM public.image_pyramid_outputs AS o
        WHERE o.id = p_output AND o.state IN ('ready', 'retiring');
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_legacy_pyramid_output_for_publish(
    p_asset uuid, p_generation integer, p_token uuid, p_output uuid
) RETURNS SETOF public.image_pyramid_outputs LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT o.* FROM public.image_pyramid_outputs AS o
    JOIN yoao_assets_v1.lock_legacy_pyramid_publication(p_asset, p_generation, p_token, p_output) ON true
    WHERE o.id = p_output AND o.asset_id = p_asset AND o.generation = p_generation
      AND o.lease_token = p_token AND o.state = 'building' FOR UPDATE OF o NOWAIT;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.publish_legacy_pyramid_generation(
    p_asset uuid, p_generation integer, p_token uuid, p_output uuid,
    p_result jsonb, p_inventory jsonb, p_gate_revision bigint
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_output public.image_pyramid_outputs%ROWTYPE; v_receipt jsonb;
BEGIN
    SELECT * INTO v_output FROM yoao_assets_v1.lock_legacy_pyramid_output_for_publish(
        p_asset, p_generation, p_token, p_output
    );
    IF NOT FOUND THEN RETURN false; END IF;
    IF NOT EXISTS (SELECT 1 FROM public.storage_maintenance_gates AS gate
                   WHERE gate.location_id = (v_output.storage_ref ->> 'location_id')::uuid
                     AND gate.state = 'active' AND gate.revision = p_gate_revision FOR SHARE NOWAIT)
       OR p_result ->> 'manifest_key' IS DISTINCT FROM (v_output.storage_ref ->> 'prefix') || 'manifest.json'
       OR p_result ->> 'overview_key' IS DISTINCT FROM (v_output.storage_ref ->> 'prefix') || 'overview.webp' THEN
        RETURN false;
    END IF;
    UPDATE public.image_pyramid_outputs AS o
    SET state = 'ready', inventory = p_inventory, updated_at = pg_catalog.now() WHERE o.id = p_output;
    UPDATE public.image_pyramid_generations AS g
    SET status = 'ready', lease_token = NULL, lease_expires_at = NULL, error_code = NULL,
        source_fingerprint = p_result ->> 'source_fingerprint',
        width = (p_result ->> 'width')::integer, height = (p_result ->> 'height')::integer,
        max_level = (p_result ->> 'max_level')::integer, format = 'webp',
        manifest_key = p_result ->> 'manifest_key', manifest_digest = p_result ->> 'manifest_digest',
        overview_key = p_result ->> 'overview_key', overview_digest = p_result ->> 'overview_digest',
        overview_width = (p_result ->> 'overview_width')::integer, overview_height = (p_result ->> 'overview_height')::integer,
        tile_count = (p_result ->> 'tile_count')::integer, retained_bytes = (p_result ->> 'retained_bytes')::bigint,
        updated_at = pg_catalog.now()
    WHERE g.asset_id = p_asset AND g.generation = p_generation;
    UPDATE public.image_pyramid_assets AS a SET active_generation = p_generation,
        building_generation = NULL, updated_at = pg_catalog.now() WHERE a.id = p_asset;
    v_receipt := yoao_assets_v1.publish_image_pyramid_thumbnail(p_asset, p_generation, p_output);
    IF v_receipt ->> 'applied' IS DISTINCT FROM 'true' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0500', MESSAGE = 'thumbnail_publication_rejected';
    END IF;
    RETURN true;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.fail_legacy_pyramid_generation(
    p_asset uuid, p_generation integer, p_token uuid, p_error text
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_generation public.image_pyramid_generations%ROWTYPE;
BEGIN
    PERFORM 1 FROM public.image_pyramid_assets AS a WHERE a.id = p_asset FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT g.* INTO v_generation FROM public.image_pyramid_generations AS g
    WHERE g.asset_id = p_asset AND g.generation = p_generation FOR UPDATE;
    IF NOT FOUND OR v_generation.job_id IS NOT NULL OR v_generation.status <> 'building' THEN RETURN false; END IF;
    IF p_token IS NOT NULL THEN
        IF v_generation.lease_token IS DISTINCT FROM p_token THEN RETURN false; END IF;
    ELSIF p_error IS DISTINCT FROM 'lease_expired' OR v_generation.lease_expires_at IS NULL
       OR v_generation.lease_expires_at > pg_catalog.clock_timestamp()
       OR NOT yoao_assets_v1.legacy_pyramid_gc_eligible('pyramid_generation', p_asset, p_generation) THEN
        RETURN false;
    END IF;
    UPDATE public.image_pyramid_generations AS g
    SET status = 'failed', error_code = pg_catalog.left(p_error, 64), lease_token = NULL,
        lease_expires_at = NULL, updated_at = pg_catalog.now()
    WHERE g.asset_id = p_asset AND g.generation = p_generation;
    UPDATE public.image_pyramid_assets AS a SET building_generation = NULL, updated_at = pg_catalog.now()
    WHERE a.id = p_asset AND a.building_generation = p_generation;
    RETURN true;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.invalidate_legacy_pyramid_generation(
    p_asset uuid, p_generation integer, p_output uuid, p_error text
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    PERFORM 1 FROM public.image_pyramid_assets AS a WHERE a.id = p_asset FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE public.image_pyramid_generations AS g
    SET status = 'failed', error_code = p_error, updated_at = pg_catalog.now()
    WHERE g.asset_id = p_asset AND g.generation = p_generation AND g.job_id IS NULL
      AND g.output_id IS NOT DISTINCT FROM p_output AND g.status = 'ready';
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE public.image_pyramid_assets AS a SET active_generation = NULL, updated_at = pg_catalog.now()
    WHERE a.id = p_asset AND a.active_generation = p_generation;
    RETURN true;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.touch_legacy_pyramid_generation(
    p_asset uuid, p_generation integer, p_output uuid
) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    WITH touched AS (
        UPDATE public.image_pyramid_generations AS g
        SET last_accessed_at = pg_catalog.clock_timestamp(), updated_at = pg_catalog.now()
        WHERE g.asset_id = p_asset AND g.generation = p_generation AND g.output_id = p_output
          AND g.job_id IS NULL AND g.status = 'ready'
          AND (g.last_accessed_at IS NULL OR g.last_accessed_at < pg_catalog.clock_timestamp() - interval '10 minutes')
        RETURNING 1
    ) SELECT EXISTS (SELECT 1 FROM touched);
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.scan_expired_legacy_pyramid_generations()
RETURNS TABLE(asset_id uuid, generation integer) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT g.asset_id, g.generation FROM public.image_pyramid_generations AS g
    WHERE g.status = 'building' AND g.lease_expires_at <= pg_catalog.statement_timestamp()
      AND yoao_assets_v1.legacy_pyramid_gc_eligible('pyramid_generation', g.asset_id, g.generation)
    ORDER BY g.lease_expires_at, g.asset_id, g.generation LIMIT 100;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.scan_stale_legacy_pyramid_generations(p_cutoff timestamptz)
RETURNS TABLE(asset_id uuid, generation integer) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT g.asset_id, g.generation FROM public.image_pyramid_generations AS g
    JOIN public.image_pyramid_assets AS a ON a.id = g.asset_id
    WHERE g.status IN ('ready', 'failed') AND g.output_id IS NOT NULL AND g.updated_at < p_cutoff
      AND g.generation IS DISTINCT FROM a.active_generation
      AND g.generation IS DISTINCT FROM a.building_generation
      AND yoao_assets_v1.legacy_pyramid_gc_eligible('pyramid_generation', g.asset_id, g.generation)
    ORDER BY g.updated_at, g.asset_id, g.generation LIMIT 100;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.remove_stale_legacy_pyramid_generation(
    p_asset uuid, p_generation integer, p_cutoff timestamptz
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    PERFORM 1 FROM public.image_pyramid_assets AS a WHERE a.id = p_asset
      AND a.active_generation IS DISTINCT FROM p_generation
      AND a.building_generation IS DISTINCT FROM p_generation FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    PERFORM 1 FROM public.image_pyramid_generations AS g WHERE g.asset_id = p_asset
      AND g.generation = p_generation AND g.status IN ('ready', 'failed') AND g.updated_at < p_cutoff FOR UPDATE;
    IF NOT FOUND OR NOT yoao_assets_v1.legacy_pyramid_gc_eligible('pyramid_generation', p_asset, p_generation) THEN
        RETURN false;
    END IF;
    DELETE FROM public.image_pyramid_generations AS g WHERE g.asset_id = p_asset AND g.generation = p_generation;
    RETURN true;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._legacy_pyramid_output_unreferenced(p_output uuid, p_excluding uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT yoao_assets_v1.legacy_pyramid_gc_eligible('pyramid_output', p_output, 0)
      AND NOT EXISTS (SELECT 1 FROM public.image_pyramid_generations AS g WHERE g.output_id = p_output)
      AND NOT EXISTS (SELECT 1 FROM public.storage_operations AS op
          WHERE op.purpose = 'image_pyramid_' || pg_catalog.replace(p_output::text, '-', '')
            AND op.id IS DISTINCT FROM p_excluding);
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.scan_retirable_legacy_pyramid_outputs(p_cutoff timestamptz)
RETURNS SETOF uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT o.id FROM public.image_pyramid_outputs AS o
    WHERE o.state <> 'deleted' AND o.updated_at < p_cutoff
      AND yoao_assets_v1._legacy_pyramid_output_unreferenced(o.id, NULL)
    ORDER BY o.updated_at, o.id LIMIT 100;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.begin_legacy_pyramid_retirement(p_output uuid, p_cutoff timestamptz)
RETURNS SETOF public.image_pyramid_outputs LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_output public.image_pyramid_outputs%ROWTYPE;
BEGIN
    SELECT o.* INTO v_output FROM public.image_pyramid_outputs AS o WHERE o.id = p_output FOR UPDATE;
    IF NOT FOUND OR v_output.state = 'deleted' OR v_output.updated_at >= p_cutoff
       OR NOT yoao_assets_v1._legacy_pyramid_output_unreferenced(p_output, NULL) THEN RETURN; END IF;
    UPDATE public.image_pyramid_outputs AS o SET state = 'retiring', updated_at = pg_catalog.now()
    WHERE o.id = p_output AND o.state <> 'retiring';
    RETURN NEXT v_output;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.check_legacy_pyramid_retirement(p_output uuid, p_ref jsonb, p_permit uuid)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    PERFORM 1 FROM public.image_pyramid_outputs AS o WHERE o.id = p_output
      AND o.state = 'retiring' AND o.storage_ref = p_ref FOR UPDATE;
    IF NOT FOUND OR NOT yoao_assets_v1.legacy_pyramid_gc_eligible('pyramid_output', p_output, 0)
       OR EXISTS (SELECT 1 FROM public.image_pyramid_generations AS g WHERE g.output_id = p_output) THEN
        RETURN 'image_pyramid_output_changed';
    END IF;
    IF NOT yoao_assets_v1._legacy_pyramid_output_unreferenced(p_output, p_permit) THEN
        RETURN 'storage_operation_unsettled';
    END IF;
    RETURN NULL;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.finish_legacy_pyramid_retirement(p_output uuid, p_ref jsonb)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    WITH retired AS (
        UPDATE public.image_pyramid_outputs AS o SET state = 'deleted', updated_at = pg_catalog.now()
        WHERE o.id = p_output AND o.state = 'retiring' AND o.storage_ref = p_ref
          AND yoao_assets_v1._legacy_pyramid_output_unreferenced(p_output, NULL)
        RETURNING 1
    ) SELECT EXISTS (SELECT 1 FROM retired);
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.read_legacy_pyramid_retained_bytes()
RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT COALESCE(sum(g.retained_bytes), 0)::bigint
    FROM public.image_pyramid_generations AS g WHERE g.status = 'ready';
$function$;
