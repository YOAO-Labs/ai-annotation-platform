CREATE FUNCTION yoao_labeling_v1.lock_point_quality_annotation_geometry(p_run_id uuid)
RETURNS void
LANGUAGE plpgsql VOLATILE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_scope jsonb;
    v_scenes uuid[];
BEGIN
    SELECT r.scope_json INTO v_scope FROM public.point_cloud_quality_runs AS r
    WHERE r.id = p_run_id AND r.status = 'running';
    IF NOT FOUND THEN RETURN; END IF;
    PERFORM a.id FROM public.annotations AS a
    WHERE a.task_id IN (SELECT id::uuid FROM pg_catalog.jsonb_array_elements_text(v_scope -> 'task_ids') AS ids(id))
    ORDER BY a.id FOR SHARE NOWAIT;
    SELECT ARRAY(SELECT id::uuid FROM pg_catalog.jsonb_array_elements_text(v_scope -> 'scene_ids') AS ids(id))
    INTO v_scenes;
    PERFORM t.id FROM public.scene_tracks AS t WHERE t.scene_id = ANY (v_scenes)
    ORDER BY t.id FOR SHARE NOWAIT;
    PERFORM i.id FROM public.scene_track_intervals AS i
    WHERE i.scene_track_id IN (SELECT t.id FROM public.scene_tracks AS t WHERE t.scene_id = ANY (v_scenes))
    ORDER BY i.id FOR SHARE NOWAIT;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1.lock_point_quality_annotation_geometry(uuid) FROM PUBLIC;
