CREATE OR REPLACE FUNCTION yoao_learning_v1.training_dataset_task(p_snapshot jsonb)
RETURNS text LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog, pg_temp
RETURN CASE
    WHEN jsonb_typeof(p_snapshot -> 'schema') = 'number' AND p_snapshot ->> 'schema' = '1'
        AND (NOT p_snapshot ? 'task' OR p_snapshot ->> 'task' = 'image-detection')
        AND NOT p_snapshot ?| ARRAY['geometry', 'bridge']
        AND (NOT p_snapshot ? 'format' OR p_snapshot ->> 'format' = 'ultralytics-detection')
    THEN 'detect'
    WHEN jsonb_typeof(p_snapshot -> 'schema') = 'number' AND p_snapshot ->> 'schema' = '2'
        AND p_snapshot @> '{"task":"image-instance-segmentation","geometry":"normalized-simple-polygon","bridge":"ultralytics-simple-polygon-1"}'
        AND (NOT p_snapshot ? 'format' OR p_snapshot ->> 'format' = 'ultralytics-segmentation')
    THEN 'segment'
    WHEN p_snapshot ->> 'schema' = '3' AND p_snapshot @> '{"schema":3,"task":"detect","media_type":"image","format":"ultralytics-yolo","geometry":"normalized-bbox","bridge":"ultralytics-bbox-1"}'
    THEN 'detect'
    WHEN p_snapshot ->> 'schema' = '3' AND p_snapshot @> '{"schema":3,"task":"segment","media_type":"image","format":"ultralytics-yolo","geometry":"normalized-simple-polygon","bridge":"ultralytics-simple-polygon-1"}'
    THEN 'segment'
    WHEN p_snapshot ->> 'schema' = '4' AND p_snapshot @> '{"schema":4,"task":"segment","media_type":"image","format":"aap-instance-rle","geometry":"instance-mask","bridge":"ultralytics-instance-rle-1"}'
    THEN 'segment'
    WHEN CASE
        WHEN jsonb_typeof(p_snapshot -> 'schema') = 'number'
            AND p_snapshot ->> 'schema' ~ '^[0-9]+$'
        THEN (p_snapshot ->> 'schema')::numeric >= 5
        ELSE false
    END
        AND p_snapshot ->> 'media_type' = 'image'
        AND jsonb_typeof(p_snapshot -> 'format') = 'string'
        AND p_snapshot ->> 'format' <> ''
        AND jsonb_typeof(p_snapshot -> 'geometry') = 'string'
        AND p_snapshot ->> 'geometry' <> ''
        AND jsonb_typeof(p_snapshot -> 'bridge') = 'string'
        AND p_snapshot ->> 'bridge' <> ''
        AND jsonb_typeof(p_snapshot -> 'task') = 'string'
        AND p_snapshot ->> 'task' ~ '^[a-z][a-z0-9_-]{1,31}$'
        AND p_snapshot ->> 'task' NOT IN ('image-detection', 'image-instance-segmentation')
    THEN p_snapshot ->> 'task'
END;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_learning_v1.training_dataset_profile(p_snapshot jsonb, p_task text)
RETURNS jsonb LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog, pg_temp
RETURN jsonb_build_object(
    'schema', p_snapshot -> 'schema', 'task', p_task,
    'media_type', CASE WHEN NOT p_snapshot ? 'media_type' THEN 'image'
        WHEN jsonb_typeof(p_snapshot -> 'media_type') = 'string' THEN p_snapshot ->> 'media_type' END,
    'source_media_type', CASE
        WHEN NOT p_snapshot ? 'source' OR p_snapshot -> 'source' = 'null' THEN 'image'
        WHEN jsonb_typeof(p_snapshot -> 'source') = 'object' THEN CASE
            WHEN NOT (p_snapshot -> 'source') ? 'media_type' THEN 'image'
            WHEN jsonb_typeof(p_snapshot #> '{source,media_type}') = 'string'
                THEN p_snapshot #>> '{source,media_type}' END END,
    'format', CASE
        WHEN CASE
            WHEN jsonb_typeof(p_snapshot -> 'schema') = 'number'
                AND p_snapshot ->> 'schema' ~ '^[0-9]+$'
            THEN (p_snapshot ->> 'schema')::numeric >= 3
            ELSE false
        END
            AND jsonb_typeof(p_snapshot -> 'format') = 'string'
            AND p_snapshot ->> 'format' <> ''
            THEN p_snapshot ->> 'format'
        WHEN p_snapshot ->> 'mask_storage_kind' = 'instance_rle'
            OR p_snapshot ->> 'geometry' = 'instance-mask'
            OR p_snapshot ->> 'bridge' = 'ultralytics-instance-rle-1'
            THEN 'aap-instance-rle'
        ELSE 'ultralytics-yolo' END,
    'geometry', CASE
        WHEN jsonb_typeof(p_snapshot -> 'geometry') = 'string'
            AND p_snapshot ->> 'geometry' <> ''
            THEN p_snapshot ->> 'geometry'
        WHEN p_snapshot ->> 'mask_storage_kind' = 'instance_rle'
            OR p_snapshot ->> 'bridge' = 'ultralytics-instance-rle-1'
            THEN 'instance-mask'
        WHEN p_task = 'segment' THEN 'normalized-simple-polygon'
        ELSE 'normalized-bbox' END,
    'bridge', CASE
        WHEN jsonb_typeof(p_snapshot -> 'bridge') = 'string'
            AND p_snapshot ->> 'bridge' <> ''
            THEN p_snapshot ->> 'bridge'
        WHEN p_snapshot ->> 'mask_storage_kind' = 'instance_rle'
            THEN 'ultralytics-instance-rle-1'
        WHEN p_task = 'segment' THEN 'ultralytics-simple-polygon-1'
        ELSE 'ultralytics-bbox-1' END
);
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_learning_v1.advance_model_deployment_retirement(
    p_project uuid, p_version uuid, p_deployment uuid, p_check uuid, p_terminal boolean
)
RETURNS TABLE(advanced boolean, active_releases bigint, active_jobs bigint, retryable_failures bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE v_registry uuid;
BEGIN
    SELECT d.registry_id INTO v_registry FROM public.trained_model_deployments d
    JOIN public.trained_model_versions v ON v.id = d.model_version_id
    WHERE v.project_id = p_project AND v.id = p_version
      AND d.id = p_deployment AND d.check_id = p_check
      AND d.status = CASE WHEN p_terminal THEN 'draining' ELSE 'ready' END FOR UPDATE OF d;
    IF NOT FOUND THEN RETURN; END IF;
    active_releases := 0;
    active_jobs := 0;
    retryable_failures := 0;
    IF p_terminal THEN
        active_releases := yoao_learning_v1.count_active_model_releases(ARRAY[p_deployment]);
        SELECT u.active_jobs, u.retryable_failures INTO active_jobs, retryable_failures
        FROM yoao_jobs_v1.frozen_deployment_usage(p_deployment, v_registry, p_version) u;
        IF active_releases > 0 OR active_jobs > 0 OR retryable_failures > 0 THEN
            advanced := false;
            RETURN NEXT;
            RETURN;
        END IF;
    END IF;
    UPDATE public.trained_model_deployments SET
        status = CASE WHEN p_terminal THEN 'retired' ELSE 'draining' END,
        check_id = CASE WHEN p_terminal THEN p_check ELSE gen_random_uuid() END
    WHERE id = p_deployment;
    advanced := true;
    RETURN NEXT;
END $$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_learning_v1.pending_dataset_mask_keys()
RETURNS TABLE(object_key text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT DISTINCT value #>> '{}' FROM public.training_dataset_versions v,
         LATERAL jsonb_path_query(v.snapshot, '$.**.object_key') value
    WHERE v.status <> 'ready'
      AND (
          v.snapshot->>'mask_storage_kind' = 'instance_rle'
          OR v.snapshot->>'geometry' = 'instance-mask'
          OR v.snapshot->>'bridge' = 'ultralytics-instance-rle-1'
      )
      AND value #>> '{}' LIKE 'raster-masks/sha256/%'
$$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_learning_v1.pending_dataset_mask_referenced(p_key text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (SELECT 1 FROM public.training_dataset_versions v,
                         LATERAL jsonb_path_query(v.snapshot, '$.**.object_key') value
                   WHERE v.status <> 'ready'
                     AND (
                         v.snapshot->>'mask_storage_kind' = 'instance_rle'
                         OR v.snapshot->>'geometry' = 'instance-mask'
                         OR v.snapshot->>'bridge' = 'ultralytics-instance-rle-1'
                     )
                     AND value #>> '{}' = p_key)
$$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_learning_v1.selection_rounds(p_project uuid, p_task text, p_limit integer, p_offset integer)
RETURNS SETOF public.active_learning_rounds
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
SELECT r.* FROM public.active_learning_rounds AS r
WHERE r.project_id = p_project AND (
    p_task IS NULL OR (p_task = 'detect' AND COALESCE(r.spec#>>'{model_ref,task}', 'detect')
        IN ('detect','detection','image-detection')) OR
    (p_task = 'segment' AND r.spec#>>'{model_ref,task}'
        IN ('segment','segmentation','image-instance-segmentation','instance-segmentation')) OR
    (p_task NOT IN ('detect', 'segment') AND r.spec#>>'{model_ref,task}' = p_task)
)
ORDER BY r.created_at DESC, r.id DESC LIMIT LEAST(p_limit, 100) OFFSET p_offset
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_learning_v1.selection_round_count(p_project uuid, p_task text)
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
RETURN (SELECT count(*) FROM public.active_learning_rounds AS r
WHERE r.project_id = p_project AND (
    p_task IS NULL OR (p_task = 'detect' AND COALESCE(r.spec#>>'{model_ref,task}', 'detect')
        IN ('detect','detection','image-detection')) OR
    (p_task = 'segment' AND r.spec#>>'{model_ref,task}'
        IN ('segment','segmentation','image-instance-segmentation','instance-segmentation')) OR
    (p_task NOT IN ('detect', 'segment') AND r.spec#>>'{model_ref,task}' = p_task)
));
