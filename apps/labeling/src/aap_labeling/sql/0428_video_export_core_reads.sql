CREATE VIEW aap_labeling_v1.video_track_quality_run_rows
WITH (security_barrier = true)
AS
SELECT id, task_id, left_segment_id, right_segment_id, async_job_id,
       requested_by_id, status, progress_pct, input_snapshot, input_digest,
       sampling_digest, metrics, pairs, error_message, accepted_by_id,
       accepted_at, stale_at, started_at, completed_at, created_at, updated_at
FROM public.video_track_quality_runs;
-- aap-statement-break
REVOKE ALL ON aap_labeling_v1.video_track_quality_run_rows FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.lock_video_segment_ids_for_export_share(
    p_ids uuid[], p_limit integer
)
RETURNS TABLE(id uuid)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
BEGIN
    IF p_ids IS NULL OR cardinality(p_ids) > 100001 OR p_limit IS NULL
       OR p_limit < 0 OR p_limit > 100001 THEN
        RAISE EXCEPTION 'Invalid Labeling video export lock arguments' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT segment.id
    FROM public.video_segments AS segment
    WHERE segment.id = ANY(p_ids)
    ORDER BY segment.id
    LIMIT p_limit
    FOR SHARE NOWAIT;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_video_segment_ids_for_export_share(uuid[],integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.lock_video_quality_run_ids_for_export_share(
    p_ids uuid[], p_limit integer
)
RETURNS TABLE(id uuid)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
BEGIN
    IF p_ids IS NULL OR cardinality(p_ids) > 100001 OR p_limit IS NULL
       OR p_limit < 0 OR p_limit > 100001 THEN
        RAISE EXCEPTION 'Invalid Labeling video export lock arguments' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT run.id
    FROM public.video_track_quality_runs AS run
    WHERE run.id = ANY(p_ids)
    ORDER BY run.id
    LIMIT p_limit
    FOR SHARE NOWAIT;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_video_quality_run_ids_for_export_share(uuid[],integer) FROM PUBLIC;
