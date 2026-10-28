CREATE FUNCTION aap_labeling_v1.lock_task_dataset_item_links_for_core_read(
    p_task_id uuid, p_compact boolean, p_link_id uuid, p_max_links integer
)
RETURNS SETOF aap_labeling_v1.task_dataset_item_link_rows
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
BEGIN
    IF p_task_id IS NULL OR p_compact IS NULL OR p_max_links < 0 THEN
        RAISE EXCEPTION 'Invalid Labeling original relation lock arguments'
            USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT link.id, link.task_id, link.dataset_item_id, link.role,
           link.sensor_name, link.created_at
    FROM public.task_dataset_item_links AS link
    WHERE link.task_id = p_task_id
      AND (NOT p_compact OR link.role = 'primary_lidar' OR link.id = p_link_id)
    ORDER BY link.id
    LIMIT CASE WHEN p_max_links IS NULL THEN NULL ELSE p_max_links::bigint + 1 END
    FOR SHARE NOWAIT;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_task_dataset_item_links_for_core_read(uuid,boolean,uuid,integer) FROM PUBLIC;
