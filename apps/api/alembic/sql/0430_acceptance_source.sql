-- Assets owns DatasetItem identity and keeps the source lock in the caller transaction.
CREATE FUNCTION aap_assets_v1._acceptance_history_source(p_item_id uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_item public.dataset_items%ROWTYPE;
BEGIN
    IF p_item_id IS NULL THEN
        RAISE EXCEPTION 'Missing acceptance source item' USING ERRCODE = '22023';
    END IF;
    SELECT item.* INTO v_item FROM public.dataset_items AS item
    WHERE item.id = p_item_id FOR SHARE NOWAIT;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    RETURN jsonb_build_object(
        'item_id', v_item.id,
        'source_generation', v_item.media_source_generation,
        'source_snapshot', aap_assets_v1._media_source_snapshot(v_item),
        'object_identity', v_item.media_object_identity,
        'source_drifted', v_item.media_source_drifted
    );
END;
$function$;
