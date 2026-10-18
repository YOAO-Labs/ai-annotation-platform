-- Source-retention checks for Dataset and DatasetItem deletion.
-- The caller owns the transaction and the source locks; guards never commit.
CREATE FUNCTION aap_learning_v1.guard_dataset_source_deletion(
    p_dataset_id uuid,
    p_item_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_dataset_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    IF EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
        JOIN public.active_learning_pool_members AS member
          ON member.dataset_item_id = source_item.item_id
        LIMIT 1
    ) OR EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
        JOIN public.active_learning_candidates AS candidate
          ON candidate.dataset_item_id = source_item.item_id
        LIMIT 1
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', 'active_learning_reference'
            )
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-source-retention/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object()
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_learning_v1.guard_dataset_source_deletion(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.guard_dataset_source_deletion(
    p_dataset_id uuid,
    p_item_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_dataset_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    -- A direct primary source and every multimodal task link retain the item,
    -- including links whose role is not primary_lidar.
    IF EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
        JOIN public.tasks AS task
          ON task.dataset_item_id = source_item.item_id
        LIMIT 1
    ) OR EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
        JOIN public.task_dataset_item_links AS item_link
          ON item_link.dataset_item_id = source_item.item_id
        LIMIT 1
    ) OR EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
        JOIN public.annotations AS annotation
          ON annotation.sensor_dataset_item_id = source_item.item_id
        LIMIT 1
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'source_in_use')
        );
    END IF;

    -- Deleting an Item may proceed when it has no item-owned history. Dataset
    -- deletion also removes Scenes, so retain all Scene/track/quality history.
    IF p_item_id IS NULL THEN
        IF EXISTS (
            SELECT 1
            FROM aap_assets_v1.dataset_scene_references(p_dataset_id)
                AS source_scene
            JOIN public.scene_tracks AS scene_track
              ON scene_track.scene_id = source_scene.scene_id
            LIMIT 1
        ) OR EXISTS (
            SELECT 1
            FROM aap_assets_v1.dataset_scene_references(p_dataset_id)
                AS source_scene
            JOIN public.scene_track_operations AS operation
              ON operation.scene_id = source_scene.scene_id
            LIMIT 1
        ) OR EXISTS (
            SELECT 1
            FROM aap_assets_v1.dataset_scene_references(p_dataset_id)
                AS source_scene
            JOIN public.point_cloud_quality_issues AS issue
              ON issue.scene_id = source_scene.scene_id
            LIMIT 1
        ) OR EXISTS (
            SELECT 1
            FROM aap_assets_v1.dataset_scene_references(p_dataset_id)
                AS source_scene
            JOIN public.point_cloud_quality_runs AS quality_run
              ON (quality_run.scope_json -> 'scene_ids') ? source_scene.scene_id::text
            LIMIT 1
        ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'source_in_use')
            );
        END IF;
    END IF;

    -- Video segment/tracker records are retained evidence. DatasetItem FKs
    -- cascade, so reject deletion before those rows could disappear silently.
    IF EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
        JOIN public.video_segments AS segment
          ON segment.dataset_item_id = source_item.item_id
        LIMIT 1
    ) OR EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
        JOIN public.video_tracker_jobs AS tracker_job
          ON tracker_job.dataset_item_id = source_item.item_id
        LIMIT 1
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'source_in_use')
        );
    END IF;

    IF p_item_id IS NOT NULL THEN
        IF EXISTS (
            SELECT 1
            FROM public.task_creation_operations AS operation
            WHERE operation.dataset_id = p_dataset_id
              AND operation.state IN ('accepted', 'running')
              AND EXISTS (
                  SELECT 1
                  FROM aap_assets_v1.dataset_association_references(
                      p_dataset_id
                  ) AS association
                  WHERE association.association_id = operation.association_id
                    AND association.project_id = operation.project_id
              )
              AND aap_jobs_v1.task_creation_execution_active(
                  operation.id, operation.job_id
              ) IS DISTINCT FROM false
            LIMIT 1
        ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object(
                    'code', 'creation_in_progress'
                )
            );
        END IF;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-source-retention/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object()
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.guard_dataset_source_deletion(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.detach_deleted_dataset_attribution(
    p_dataset_id uuid
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_detached bigint;
BEGIN
    IF p_dataset_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'dataset id is required',
            DETAIL = '{"code":"invalid_request"}';
    END IF;

    -- Match the former Dataset FK's SET NULL behavior, while preserving batch
    -- identity, counters, status, and all task/annotation history.
    UPDATE public.task_batches AS batch
    SET dataset_id = NULL
    WHERE batch.dataset_id = p_dataset_id;

    GET DIAGNOSTICS v_detached = ROW_COUNT;
    RETURN v_detached;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.detach_deleted_dataset_attribution(uuid) FROM PUBLIC;
