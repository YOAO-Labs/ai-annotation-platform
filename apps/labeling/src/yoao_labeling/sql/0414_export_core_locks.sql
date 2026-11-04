CREATE FUNCTION aap_labeling_v1.lock_task_dataset_item_link_rows_for_core_read_share(
    p_link_ids uuid[], p_limit integer
)
RETURNS SETOF aap_labeling_v1.task_dataset_item_link_rows
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
BEGIN
    IF p_link_ids IS NULL OR p_limit IS NULL OR p_limit < 0 THEN
        RAISE EXCEPTION 'Invalid Labeling export lock arguments' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT link.id, link.task_id, link.dataset_item_id, link.role,
           link.sensor_name, link.created_at
    FROM public.task_dataset_item_links AS link
    WHERE link.id = ANY(p_link_ids)
    ORDER BY link.id
    LIMIT p_limit
    FOR SHARE NOWAIT;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_task_dataset_item_link_rows_for_core_read_share(uuid[],integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.lock_prediction_rows_for_core_read_share(
    p_project_id uuid, p_task_ids uuid[], p_limit integer
)
RETURNS SETOF aap_labeling_v1.prediction_rows
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
BEGIN
    IF p_project_id IS NULL OR p_task_ids IS NULL OR p_limit IS NULL OR p_limit < 0 THEN
        RAISE EXCEPTION 'Invalid Labeling export lock arguments' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT p.id, p.task_id, p.project_id, p.ml_backend_id, p.ml_backend_pool_id,
           p.model_version, p.score, p.tool_unit_id, p.result, p.cluster,
           p.mislabeling, p.source, p.rejected_shape_indexes, p.created_at
    FROM public.predictions AS p
    WHERE p.project_id = p_project_id AND p.task_id = ANY(p_task_ids)
    ORDER BY p.id, p.created_at
    LIMIT p_limit
    FOR SHARE NOWAIT;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_prediction_rows_for_core_read_share(uuid,uuid[],integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.lock_scene_track_rows_for_core_read(
    p_track_ids uuid[], p_limit integer
)
RETURNS SETOF aap_labeling_v1.scene_track_rows
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
BEGIN
    IF p_track_ids IS NULL OR p_limit IS NULL OR p_limit < 0 THEN
        RAISE EXCEPTION 'Invalid Labeling export lock arguments' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT t.id, t.project_id, t.scene_id, t.track_id, t.class_name,
           t.presence_mode, t.attributes, t.attributes_meta, t.revision,
           t.created_by, t.retired_at, t.created_at, t.updated_at
    FROM public.scene_tracks AS t
    WHERE t.id = ANY(p_track_ids)
    ORDER BY t.id
    LIMIT p_limit
    FOR UPDATE NOWAIT;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_scene_track_rows_for_core_read(uuid[],integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.lock_scene_track_interval_rows_for_core_read_share(
    p_track_ids uuid[], p_limit integer
)
RETURNS SETOF aap_labeling_v1.scene_track_interval_rows
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
BEGIN
    IF p_track_ids IS NULL OR p_limit IS NULL OR p_limit < 0 THEN
        RAISE EXCEPTION 'Invalid Labeling export lock arguments' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT i.id, i.scene_track_id, i.start_frame, i.end_frame, i.source,
           i.version, i.created_by, i.operation_id, i.created_at, i.updated_at
    FROM public.scene_track_intervals AS i
    WHERE i.scene_track_id = ANY(p_track_ids)
    ORDER BY i.id
    LIMIT p_limit
    FOR SHARE NOWAIT;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_scene_track_interval_rows_for_core_read_share(uuid[],integer) FROM PUBLIC;
