CREATE FUNCTION yoao_assets_v1.lock_point_quality_scenes(p_scene_ids uuid[])
RETURNS void
LANGUAGE sql VOLATILE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT scene.id FROM public.scenes AS scene
    WHERE scene.id = ANY (p_scene_ids) ORDER BY scene.id FOR SHARE NOWAIT
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.lock_point_quality_scenes(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_point_quality_poses(p_scene_ids uuid[])
RETURNS void
LANGUAGE sql VOLATILE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pose.id FROM public.scene_frame_poses AS pose
    WHERE pose.scene_id = ANY (p_scene_ids) ORDER BY pose.id FOR SHARE NOWAIT
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.lock_point_quality_poses(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_point_quality_item_datasets(p_item_ids uuid[])
RETURNS void
LANGUAGE sql VOLATILE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT d.id FROM public.datasets AS d
    WHERE d.id IN (SELECT i.dataset_id FROM public.dataset_items AS i WHERE i.id = ANY (p_item_ids))
    ORDER BY d.id FOR SHARE NOWAIT
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.lock_point_quality_item_datasets(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.point_quality_original_run(p_run_id uuid, p_lock boolean)
RETURNS TABLE (
    project_id uuid, async_job_id uuid, requested_by_id uuid, status text,
    source_snapshot_digest text, config_digest text, scope_json jsonb,
    source_snapshot jsonb, config_snapshot jsonb
)
LANGUAGE plpgsql VOLATILE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock THEN
        PERFORM r.id FROM public.point_cloud_quality_runs AS r WHERE r.id = p_run_id FOR UPDATE;
    END IF;
    RETURN QUERY SELECT r.project_id, r.async_job_id, r.requested_by_id, r.status::text,
        r.source_snapshot_digest::text, r.config_digest::text, r.scope_json,
        r.source_snapshot, r.config_snapshot
    FROM public.point_cloud_quality_runs AS r WHERE r.id = p_run_id;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1.point_quality_original_run(uuid, boolean) FROM PUBLIC;
-- aap-statement-break
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
