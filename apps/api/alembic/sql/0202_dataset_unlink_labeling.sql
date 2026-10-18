-- The selector treats Task.dataset_item_id as the authoritative primary source;
-- only legacy rows without it may resolve through a primary_lidar link.
CREATE FUNCTION aap_labeling_v1._dataset_unlink_task_snapshot(
    p_dataset_id uuid,
    p_project_id uuid
)
RETURNS TABLE(task_id uuid, batch_id uuid)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_dataset_id IS NULL OR p_project_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'dataset and project ids are required';
    END IF;

    RETURN QUERY
    WITH source_items AS MATERIALIZED (
        SELECT reference.item_id
        FROM aap_assets_v1.dataset_item_references(p_dataset_id, NULL::uuid)
            AS reference
    )
    SELECT task.id, task.batch_id
    FROM public.tasks AS task
    WHERE task.project_id = p_project_id
      AND (
          task.dataset_item_id IN (SELECT source_items.item_id FROM source_items)
          OR (
              task.dataset_item_id IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM public.task_dataset_item_links AS item_link
                  JOIN source_items
                    ON source_items.item_id = item_link.dataset_item_id
                  WHERE item_link.task_id = task.id
                    AND item_link.role = 'primary_lidar'
              )
          )
      )
    ORDER BY task.id;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.dataset_task_references(
    p_dataset_id uuid,
    p_project_id uuid
)
RETURNS TABLE(task_id uuid)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_dataset_id IS NULL OR p_project_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'dataset and project ids are required';
    END IF;

    RETURN QUERY
    SELECT snapshot.task_id
    FROM aap_labeling_v1._dataset_unlink_task_snapshot(
        p_dataset_id, p_project_id
    ) AS snapshot
    ORDER BY snapshot.task_id;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.lock_dataset_unlink_scope(
    p_dataset_id uuid,
    p_project_id uuid
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_before jsonb;
    v_after jsonb;
BEGIN
    IF p_dataset_id IS NULL OR p_project_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'dataset and project ids are required';
    END IF;

    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_array(snapshot.task_id, snapshot.batch_id)
            ORDER BY snapshot.task_id
        ),
        '[]'::jsonb
    )
    INTO v_before
    FROM aap_labeling_v1._dataset_unlink_task_snapshot(
        p_dataset_id, p_project_id
    ) AS snapshot;

    -- Batch assignment locks a batch before updating its task children.
    PERFORM batch.id
    FROM public.task_batches AS batch
    WHERE batch.project_id = p_project_id
    ORDER BY batch.id
    FOR UPDATE NOWAIT;

    PERFORM task.id
    FROM public.tasks AS task
    WHERE task.id IN (
        SELECT snapshot.task_id
        FROM aap_labeling_v1._dataset_unlink_task_snapshot(
            p_dataset_id, p_project_id
        ) AS snapshot
    )
    ORDER BY task.id
    FOR UPDATE NOWAIT;

    SELECT COALESCE(
        pg_catalog.jsonb_agg(
            pg_catalog.jsonb_build_array(snapshot.task_id, snapshot.batch_id)
            ORDER BY snapshot.task_id
        ),
        '[]'::jsonb
    )
    INTO v_after
    FROM aap_labeling_v1._dataset_unlink_task_snapshot(
        p_dataset_id, p_project_id
    ) AS snapshot;

    IF v_before IS DISTINCT FROM v_after THEN
        RAISE EXCEPTION USING
            ERRCODE = '40001',
            MESSAGE = 'dataset unlink source or batch scope changed';
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.guard_dataset_unlink(
    p_dataset_id uuid,
    p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_dataset_id IS NULL OR p_project_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'dataset and project ids are required';
    END IF;

    PERFORM aap_labeling_v1.lock_dataset_unlink_scope(
        p_dataset_id, p_project_id
    );

    -- Task locks serialize task-anchored feedback inserts; annotation locks
    -- also fence lineage/feedback inserts made through another task.
    PERFORM annotation.id
    FROM public.annotations AS annotation
    WHERE annotation.task_id IN (
        SELECT snapshot.task_id
        FROM aap_labeling_v1._dataset_unlink_task_snapshot(
            p_dataset_id, p_project_id
        ) AS snapshot
    )
    ORDER BY annotation.id
    FOR UPDATE NOWAIT;

    IF EXISTS (
        SELECT 1
        FROM public.annotation_feedbacks AS feedback
        WHERE feedback.task_id IN (
            SELECT snapshot.task_id
            FROM aap_labeling_v1._dataset_unlink_task_snapshot(
                p_dataset_id, p_project_id
            ) AS snapshot
        )
           OR feedback.annotation_id IN (
               SELECT annotation.id
               FROM public.annotations AS annotation
               WHERE annotation.task_id IN (
                   SELECT snapshot.task_id
                   FROM aap_labeling_v1._dataset_unlink_task_snapshot(
                       p_dataset_id, p_project_id
                   ) AS snapshot
               )
           )
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', 'task_feedback_retained'
            )
        );
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.annotations AS parent
        JOIN public.annotations AS child
          ON child.parent_annotation_id = parent.id
        WHERE parent.task_id IN (
            SELECT snapshot.task_id
            FROM aap_labeling_v1._dataset_unlink_task_snapshot(
                p_dataset_id, p_project_id
            ) AS snapshot
        )
          AND NOT EXISTS (
              SELECT 1
              FROM aap_labeling_v1._dataset_unlink_task_snapshot(
                  p_dataset_id, p_project_id
              ) AS target
              WHERE target.task_id = child.task_id
          )
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', 'annotation_reference_retained'
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
CREATE FUNCTION aap_labeling_v1.preview_dataset_unlink(
    p_dataset_id uuid,
    p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_tasks bigint;
    v_annotations bigint;
    v_batches bigint;
BEGIN
    IF p_dataset_id IS NULL OR p_project_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'dataset and project ids are required';
    END IF;

    SELECT count(*)
    INTO v_tasks
    FROM aap_labeling_v1._dataset_unlink_task_snapshot(
        p_dataset_id, p_project_id
    );

    SELECT count(*)
    INTO v_annotations
    FROM public.annotations AS annotation
    WHERE annotation.task_id IN (
        SELECT snapshot.task_id
        FROM aap_labeling_v1._dataset_unlink_task_snapshot(
            p_dataset_id, p_project_id
        ) AS snapshot
    );

    SELECT count(*)
    INTO v_batches
    FROM public.task_batches AS batch
    WHERE batch.project_id = p_project_id
      AND batch.display_id <> 'B-DEFAULT'
      AND EXISTS (
          SELECT 1
          FROM aap_labeling_v1._dataset_unlink_task_snapshot(
              p_dataset_id, p_project_id
          ) AS target
          WHERE target.batch_id = batch.id
      )
      AND NOT EXISTS (
          SELECT 1
          FROM public.tasks AS remaining
          WHERE remaining.batch_id = batch.id
            AND NOT EXISTS (
                SELECT 1
                FROM aap_labeling_v1._dataset_unlink_task_snapshot(
                    p_dataset_id, p_project_id
                ) AS target
                WHERE target.task_id = remaining.id
            )
      );

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-dataset-unlink-preview/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'will_delete_tasks', v_tasks,
            'will_delete_annotations', v_annotations,
            'will_delete_batches', v_batches
        )
    );
END;
$function$;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.delete_dataset_tasks(
    p_dataset_id uuid,
    p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_task_ids uuid[];
    v_affected_batch_ids uuid[];
    v_deleted_batch_ids uuid[];
    v_deleted_tasks bigint := 0;
    v_deleted_annotations bigint := 0;
    v_deleted_batches bigint := 0;
    v_project_total bigint := 0;
    v_project_completed bigint := 0;
    v_project_review bigint := 0;
    v_project_in_progress bigint := 0;
    v_batch_summary jsonb;
    v_retention_guard jsonb;
BEGIN
    IF p_dataset_id IS NULL OR p_project_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'dataset and project ids are required';
    END IF;

    v_retention_guard := aap_labeling_v1.guard_dataset_unlink(
        p_dataset_id, p_project_id
    );
    IF v_retention_guard ->> 'ok' IS DISTINCT FROM 'true' THEN
        RAISE EXCEPTION USING
            ERRCODE = '23503',
            MESSAGE = 'dataset task unlink is blocked by retained references',
            DETAIL = v_retention_guard -> 'error' ->> 'code';
    END IF;

    v_task_ids := ARRAY(
        SELECT snapshot.task_id
        FROM aap_labeling_v1._dataset_unlink_task_snapshot(
            p_dataset_id, p_project_id
        ) AS snapshot
        ORDER BY snapshot.task_id
    );
    v_affected_batch_ids := ARRAY(
        SELECT DISTINCT snapshot.batch_id
        FROM aap_labeling_v1._dataset_unlink_task_snapshot(
            p_dataset_id, p_project_id
        ) AS snapshot
        WHERE snapshot.batch_id IS NOT NULL
        ORDER BY snapshot.batch_id
    );

    DELETE FROM public.annotation_comments AS comment
    WHERE comment.annotation_id IN (
        SELECT annotation.id
        FROM public.annotations AS annotation
        WHERE annotation.task_id = ANY(v_task_ids)
    );

    DELETE FROM public.annotation_drafts AS draft
    WHERE draft.task_id = ANY(v_task_ids)
       OR draft.annotation_id IN (
           SELECT annotation.id
           FROM public.annotations AS annotation
           WHERE annotation.task_id = ANY(v_task_ids)
       );

    DELETE FROM public.prediction_metas AS meta
    WHERE meta.prediction_id IN (
        SELECT prediction.id
        FROM public.predictions AS prediction
        WHERE prediction.task_id = ANY(v_task_ids)
    )
       OR meta.failed_prediction_id IN (
           SELECT failed.id
           FROM public.failed_predictions AS failed
           WHERE failed.task_id = ANY(v_task_ids)
       );

    DELETE FROM public.task_locks AS task_lock
    WHERE task_lock.task_id = ANY(v_task_ids);

    UPDATE public.annotations AS annotation
    SET parent_annotation_id = NULL,
        parent_prediction_id = NULL
    WHERE annotation.task_id = ANY(v_task_ids);

    DELETE FROM public.annotations AS annotation
    WHERE annotation.task_id = ANY(v_task_ids);
    GET DIAGNOSTICS v_deleted_annotations = ROW_COUNT;

    DELETE FROM public.predictions AS prediction
    WHERE prediction.task_id = ANY(v_task_ids);

    DELETE FROM public.failed_predictions AS failed
    WHERE failed.task_id = ANY(v_task_ids);

    DELETE FROM public.tasks AS task
    WHERE task.id = ANY(v_task_ids);
    GET DIAGNOSTICS v_deleted_tasks = ROW_COUNT;

    WITH counts AS (
        SELECT
            batch.id,
            count(task.id)::integer AS total_tasks,
            count(*) FILTER (WHERE task.status = 'completed')::integer
                AS completed_tasks,
            count(*) FILTER (WHERE task.status = 'review')::integer
                AS review_tasks
        FROM public.task_batches AS batch
        LEFT JOIN public.tasks AS task ON task.batch_id = batch.id
        WHERE batch.project_id = p_project_id
        GROUP BY batch.id
    )
    UPDATE public.task_batches AS batch
    SET total_tasks = counts.total_tasks,
        completed_tasks = counts.completed_tasks,
        review_tasks = counts.review_tasks,
        updated_at = pg_catalog.now()
    FROM counts
    WHERE batch.id = counts.id;

    SELECT COALESCE(
        pg_catalog.array_agg(batch.id ORDER BY batch.id),
        ARRAY[]::uuid[]
    )
    INTO v_deleted_batch_ids
    FROM public.task_batches AS batch
    WHERE batch.id = ANY(v_affected_batch_ids)
      AND batch.display_id <> 'B-DEFAULT'
      AND NOT EXISTS (
          SELECT 1
          FROM public.tasks AS remaining
          WHERE remaining.batch_id = batch.id
      );

    DELETE FROM public.task_batches AS batch
    WHERE batch.id = ANY(v_deleted_batch_ids);
    GET DIAGNOSTICS v_deleted_batches = ROW_COUNT;

    SELECT
        count(*),
        count(*) FILTER (WHERE task.status = 'completed'),
        count(*) FILTER (WHERE task.status = 'review'),
        count(*) FILTER (WHERE task.status = 'in_progress')
    INTO
        v_project_total,
        v_project_completed,
        v_project_review,
        v_project_in_progress
    FROM public.tasks AS task
    WHERE task.project_id = p_project_id;

    SELECT pg_catalog.jsonb_build_object(
        'total', count(*),
        'assigned', count(*) FILTER (WHERE batch.annotator_id IS NOT NULL),
        'in_review', count(*) FILTER (WHERE batch.status = 'reviewing')
    )
    INTO v_batch_summary
    FROM public.task_batches AS batch
    WHERE batch.project_id = p_project_id;

    UPDATE public.projects AS project
    SET total_tasks = v_project_total::integer,
        completed_tasks = v_project_completed::integer,
        review_tasks = v_project_review::integer,
        in_progress_tasks = v_project_in_progress::integer,
        batch_summary = v_batch_summary,
        updated_at = pg_catalog.now()
    WHERE project.id = p_project_id;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-dataset-task-unlink/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'deleted_tasks', v_deleted_tasks,
            'deleted_annotations', v_deleted_annotations,
            'deleted_batches', v_deleted_batches,
            'deleted_batch_ids', v_deleted_batch_ids,
            'soft', false
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._dataset_unlink_task_snapshot(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.dataset_task_references(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_dataset_unlink_scope(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.guard_dataset_unlink(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.preview_dataset_unlink(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.delete_dataset_tasks(uuid, uuid)
    FROM PUBLIC;
