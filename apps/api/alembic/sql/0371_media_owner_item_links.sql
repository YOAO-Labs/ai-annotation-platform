-- Media reads the published derivative pointers of Assets-owned items.
-- Wrapped by the feature migration chain after bo0370; frozen SQL stays intact.
-- Callable by the API role and the Assets media control/worker roles only.

-- aap-statement-break
CREATE FUNCTION aap_assets_v1.read_video_derivative_links(p_item_ids uuid[])
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_links jsonb;
BEGIN
    IF p_item_ids IS NULL
       OR pg_catalog.array_ndims(p_item_ids) IS DISTINCT FROM 1
       OR pg_catalog.cardinality(p_item_ids) < 1
       OR pg_catalog.cardinality(p_item_ids) > 5000
       OR pg_catalog.array_position(p_item_ids, NULL) IS NOT NULL
       OR (SELECT pg_catalog.count(DISTINCT requested.id)
           FROM pg_catalog.unnest(p_item_ids) AS requested(id))
          <> pg_catalog.cardinality(p_item_ids) THEN
        RAISE EXCEPTION 'invalid media item selector' USING ERRCODE = '22023';
    END IF;
    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_object(
                'dataset_item_id', item.id,
                'video_output_id', item.video_output_id,
                'video_building_output_id', item.video_building_output_id
            )
            ORDER BY item.id
        ),
        '[]'::jsonb
    )
    INTO v_links
    FROM public.dataset_items AS item
    WHERE item.id = ANY(p_item_ids);
    RETURN v_links;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_video_derivative_links(uuid[]) FROM PUBLIC;
-- aap-statement-break
-- Labeling owns Task rows. This narrow command lets the canonical pyramid
-- writer publish its overview pointer without touching public.tasks directly.
CREATE FUNCTION aap_labeling_v1.publish_task_pyramid_thumbnail(
    p_task_id uuid,
    p_key text
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_task_id IS NULL
       OR p_key IS NULL
       OR pg_catalog.char_length(p_key) NOT BETWEEN 1 AND 512
       OR p_key !~ '^image-pyramids/[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+$'
       OR p_key ~ '\.\.' THEN
        RAISE EXCEPTION 'invalid Labeling pyramid thumbnail publication'
            USING ERRCODE = '22023';
    END IF;
    UPDATE public.tasks AS task
    SET thumbnail_path = p_key
    WHERE task.id = p_task_id AND task.file_type = 'image';
    RETURN FOUND;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.publish_task_pyramid_thumbnail(uuid,text)
    FROM PUBLIC;
-- aap-statement-break
-- Assets owns DatasetItem rows and coordinates the published pyramid receipt.
-- The command derives the overview key from the locked generation; callers
-- cannot choose a path or bypass the active-generation/output fences.
CREATE FUNCTION aap_assets_v1.publish_image_pyramid_thumbnail(
    p_asset_id uuid,
    p_generation integer,
    p_output_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_asset public.image_pyramid_assets%ROWTYPE;
    v_generation public.image_pyramid_generations%ROWTYPE;
    v_output public.image_pyramid_outputs%ROWTYPE;
    v_applied boolean := false;
BEGIN
    IF p_asset_id IS NULL OR p_output_id IS NULL
       OR p_generation IS NULL OR p_generation < 1 THEN
        RAISE EXCEPTION 'invalid image pyramid thumbnail publication'
            USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_asset
    FROM public.image_pyramid_assets AS asset
    WHERE asset.id = p_asset_id
    FOR UPDATE;
    IF NOT FOUND OR v_asset.active_generation IS DISTINCT FROM p_generation THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-media-thumbnail/v1', 'ok', true,
            'applied', false, 'reason', 'not_active');
    END IF;
    SELECT * INTO v_generation
    FROM public.image_pyramid_generations AS generation
    WHERE generation.asset_id = p_asset_id
      AND generation.generation = p_generation
    FOR UPDATE;
    IF NOT FOUND
       OR v_generation.status IS DISTINCT FROM 'ready'
       OR v_generation.output_id IS DISTINCT FROM p_output_id
       OR v_generation.overview_key IS NULL
       OR v_generation.overview_key !~ '^image-pyramids/'
       OR v_generation.overview_key ~ '\.\.' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-media-thumbnail/v1', 'ok', true,
            'applied', false, 'reason', 'not_published');
    END IF;
    SELECT * INTO v_output
    FROM public.image_pyramid_outputs AS output
    WHERE output.id = p_output_id
    FOR SHARE;
    IF NOT FOUND
       OR v_output.asset_id IS DISTINCT FROM p_asset_id
       OR v_output.generation IS DISTINCT FROM p_generation
       OR v_output.state IS DISTINCT FROM 'ready'
       OR v_output.inventory IS NULL
       OR NOT (v_output.inventory -> 'objects' ? v_generation.overview_key) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-media-thumbnail/v1', 'ok', true,
            'applied', false, 'reason', 'output_mismatch');
    END IF;
    IF v_asset.dataset_item_id IS NOT NULL THEN
        UPDATE public.dataset_items AS item
        SET thumbnail_path = v_generation.overview_key
        WHERE item.id = v_asset.dataset_item_id AND item.file_type = 'image';
        v_applied := FOUND;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-media-thumbnail/v1', 'ok', true,
            'applied', v_applied, 'owner_kind', 'dataset_item',
            'owner_id', v_asset.dataset_item_id,
            'thumbnail_path', v_generation.overview_key);
    END IF;
    IF v_asset.task_id IS NOT NULL THEN
        v_applied := aap_labeling_v1.publish_task_pyramid_thumbnail(
            v_asset.task_id, v_generation.overview_key
        );
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-media-thumbnail/v1', 'ok', true,
            'applied', v_applied, 'owner_kind', 'task',
            'owner_id', v_asset.task_id,
            'thumbnail_path', v_generation.overview_key);
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-media-thumbnail/v1', 'ok', true,
        'applied', false, 'reason', 'owner_missing');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.publish_image_pyramid_thumbnail(uuid,integer,uuid)
    FROM PUBLIC;
