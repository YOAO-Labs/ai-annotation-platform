-- Labeling-owned project deletion primitives; the caller owns the transaction.
CREATE FUNCTION aap_labeling_v1.project_task_references(p_project_id uuid)
RETURNS TABLE(task_id uuid)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_project_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'project id is required';
    END IF;

    RETURN QUERY
    SELECT task.id
    FROM public.tasks AS task
    WHERE task.project_id = p_project_id
    ORDER BY task.id;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.delete_project_owned_records(p_project_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_deleted_tasks bigint := 0;
    v_deleted_annotations bigint := 0;
BEGIN
    IF p_project_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'project id is required';
    END IF;

    -- Lock the parent batch rows before their Task children. NOWAIT makes
    -- concurrent labeling writes fail the whole caller transaction promptly.
    PERFORM batch.id
    FROM public.task_batches AS batch
    WHERE batch.project_id = p_project_id
    ORDER BY batch.id
    FOR UPDATE NOWAIT;

    PERFORM task.id
    FROM public.tasks AS task
    WHERE task.project_id = p_project_id
    ORDER BY task.id
    FOR UPDATE NOWAIT;

    -- Remove records with non-cascading/self references before their targets.
    DELETE FROM public.prediction_metas AS meta
    WHERE meta.prediction_id IN (
        SELECT prediction.id
        FROM public.predictions AS prediction
        WHERE prediction.project_id = p_project_id
           OR prediction.task_id IN (
               SELECT task.id
               FROM public.tasks AS task
               WHERE task.project_id = p_project_id
           )
    )
       OR meta.failed_prediction_id IN (
           SELECT failed.id
           FROM public.failed_predictions AS failed
           WHERE failed.project_id = p_project_id
              OR failed.task_id IN (
                  SELECT task.id
                  FROM public.tasks AS task
                  WHERE task.project_id = p_project_id
              )
       );

    UPDATE public.annotations AS child
    SET parent_annotation_id = NULL
    WHERE child.parent_annotation_id IN (
        SELECT annotation.id
        FROM public.annotations AS annotation
        WHERE annotation.project_id = p_project_id
           OR annotation.task_id IN (
               SELECT task.id
               FROM public.tasks AS task
               WHERE task.project_id = p_project_id
           )
    );

    UPDATE public.annotations AS child
    SET parent_prediction_id = NULL
    WHERE child.parent_prediction_id IN (
        SELECT prediction.id
        FROM public.predictions AS prediction
        WHERE prediction.project_id = p_project_id
           OR prediction.task_id IN (
               SELECT task.id
               FROM public.tasks AS task
               WHERE task.project_id = p_project_id
           )
    );

    UPDATE public.annotation_feedbacks AS child
    SET thread_parent_id = NULL
    WHERE child.thread_parent_id IN (
        SELECT feedback.id
        FROM public.annotation_feedbacks AS feedback
        WHERE feedback.project_id = p_project_id
           OR feedback.task_id IN (
               SELECT task.id
               FROM public.tasks AS task
               WHERE task.project_id = p_project_id
           )
           OR feedback.annotation_id IN (
               SELECT annotation.id
               FROM public.annotations AS annotation
               WHERE annotation.project_id = p_project_id
                  OR annotation.task_id IN (
                      SELECT task.id
                      FROM public.tasks AS task
                      WHERE task.project_id = p_project_id
                  )
           )
    );

    DELETE FROM public.annotation_drafts AS draft
    WHERE draft.task_id IN (
        SELECT task.id
        FROM public.tasks AS task
        WHERE task.project_id = p_project_id
    )
       OR draft.annotation_id IN (
           SELECT annotation.id
           FROM public.annotations AS annotation
           WHERE annotation.project_id = p_project_id
              OR annotation.task_id IN (
                  SELECT task.id
                  FROM public.tasks AS task
                  WHERE task.project_id = p_project_id
              )
       );

    DELETE FROM public.task_locks AS task_lock
    WHERE task_lock.task_id IN (
        SELECT task.id
        FROM public.tasks AS task
        WHERE task.project_id = p_project_id
    );

    DELETE FROM public.annotation_comments AS comment
    WHERE comment.project_id = p_project_id
       OR comment.annotation_id IN (
           SELECT annotation.id
           FROM public.annotations AS annotation
           WHERE annotation.project_id = p_project_id
              OR annotation.task_id IN (
                  SELECT task.id
                  FROM public.tasks AS task
                  WHERE task.project_id = p_project_id
              )
       );

    DELETE FROM public.annotation_feedbacks AS feedback
    WHERE feedback.project_id = p_project_id
       OR feedback.task_id IN (
           SELECT task.id
           FROM public.tasks AS task
           WHERE task.project_id = p_project_id
       )
       OR feedback.annotation_id IN (
           SELECT annotation.id
           FROM public.annotations AS annotation
           WHERE annotation.project_id = p_project_id
              OR annotation.task_id IN (
                  SELECT task.id
                  FROM public.tasks AS task
                  WHERE task.project_id = p_project_id
              )
       );

    -- Task events have their own direct Project FK and include retained
    -- project/task evidence, so do not rely on Project or Task cascades.
    DELETE FROM public.task_events AS task_event_row
    WHERE task_event_row.project_id = p_project_id;

    DELETE FROM public.mask_review_scopes AS scope
    WHERE scope.project_id = p_project_id;

    DELETE FROM public.mask_qc_issues AS issue
    WHERE issue.project_id = p_project_id;

    DELETE FROM public.mask_qc_runs AS run
    WHERE run.project_id = p_project_id;

    DELETE FROM public.point_cloud_quality_issues AS issue
    WHERE issue.project_id = p_project_id;

    DELETE FROM public.point_cloud_quality_evaluations AS evaluation
    WHERE evaluation.project_id = p_project_id;

    DELETE FROM public.point_cloud_quality_runs AS run
    WHERE run.project_id = p_project_id;

    DELETE FROM public.mask_annotation_revisions AS revision
    WHERE revision.project_id = p_project_id;

    DELETE FROM public.mask_repair_batches AS batch
    WHERE batch.project_id = p_project_id;

    DELETE FROM public.mask_format_imports AS format_row
    WHERE format_row.project_id = p_project_id;

    DELETE FROM public.annotations AS annotation
    WHERE annotation.project_id = p_project_id
       OR annotation.task_id IN (
           SELECT task.id
           FROM public.tasks AS task
           WHERE task.project_id = p_project_id
       );
    GET DIAGNOSTICS v_deleted_annotations = ROW_COUNT;

    DELETE FROM public.predictions AS prediction
    WHERE prediction.project_id = p_project_id
       OR prediction.task_id IN (
           SELECT task.id
           FROM public.tasks AS task
           WHERE task.project_id = p_project_id
       );

    DELETE FROM public.failed_predictions AS failed
    WHERE failed.project_id = p_project_id
       OR failed.task_id IN (
           SELECT task.id
           FROM public.tasks AS task
           WHERE task.project_id = p_project_id
       );

    DELETE FROM public.scene_tracks AS scene_track
    WHERE scene_track.project_id = p_project_id;

    DELETE FROM public.tasks AS task
    WHERE task.project_id = p_project_id;
    GET DIAGNOSTICS v_deleted_tasks = ROW_COUNT;

    DELETE FROM public.task_batches AS batch
    WHERE batch.project_id = p_project_id;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-project-labeling-delete/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'deleted_tasks', v_deleted_tasks,
            'deleted_annotations', v_deleted_annotations
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.project_task_references(uuid) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.delete_project_owned_records(uuid) FROM PUBLIC;
