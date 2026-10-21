-- Typed foreign-owned facts for Workspace project reads.
CREATE FUNCTION aap_labeling_v1.workspace_project_query_counters(
    p_project_ids uuid[]
)
RETURNS TABLE(
    project_id uuid,
    total_tasks integer,
    completed_tasks integer,
    review_tasks integer,
    in_progress_tasks integer,
    batch_summary jsonb
)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_project_ids IS NULL
       OR (
           pg_catalog.cardinality(p_project_ids) > 0
           AND pg_catalog.array_ndims(p_project_ids) IS DISTINCT FROM 1
       )
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.unnest(p_project_ids) AS requested(project_id)
           WHERE requested.project_id IS NULL
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace project query ids';
    END IF;

    RETURN QUERY
    SELECT project.id,
           project.total_tasks,
           project.completed_tasks,
           project.review_tasks,
           project.in_progress_tasks,
           project.batch_summary
    FROM public.projects AS project
    WHERE project.id = ANY (p_project_ids);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.workspace_project_query_counters(uuid[])
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.workspace_project_query_ai_counts(
    p_project_ids uuid[]
)
RETURNS TABLE(project_id uuid, task_count bigint)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_project_ids IS NULL
       OR (
           pg_catalog.cardinality(p_project_ids) > 0
           AND pg_catalog.array_ndims(p_project_ids) IS DISTINCT FROM 1
       )
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.unnest(p_project_ids) AS requested(project_id)
           WHERE requested.project_id IS NULL
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace project query ids';
    END IF;

    RETURN QUERY
    SELECT annotation.project_id,
           pg_catalog.count(DISTINCT annotation.task_id)::bigint
    FROM public.annotations AS annotation
    WHERE annotation.project_id = ANY (p_project_ids)
      AND annotation.parent_prediction_id IS NOT NULL
      AND annotation.is_active IS TRUE
    GROUP BY annotation.project_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.workspace_project_query_ai_counts(uuid[])
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.workspace_project_query_annotation_count(
    p_project_ids uuid[],
    p_ai_only boolean
)
RETURNS bigint
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_count bigint;
BEGIN
    IF p_project_ids IS NULL
       OR (
           pg_catalog.cardinality(p_project_ids) > 0
           AND pg_catalog.array_ndims(p_project_ids) IS DISTINCT FROM 1
       )
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.unnest(p_project_ids) AS requested(project_id)
           WHERE requested.project_id IS NULL
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace project query ids';
    END IF;
    IF p_ai_only IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace project annotation-count selector';
    END IF;

    SELECT pg_catalog.count(*)::bigint
    INTO v_count
    FROM public.annotations AS annotation
    WHERE annotation.project_id = ANY (p_project_ids)
      AND annotation.is_active IS TRUE
      AND annotation.was_cancelled IS FALSE
      AND (NOT p_ai_only OR annotation.parent_prediction_id IS NOT NULL);

    RETURN v_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.workspace_project_query_annotation_count(uuid[], boolean)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.workspace_project_query_task_series(
    p_project_ids uuid[],
    p_bucket_ends timestamptz[]
)
RETURNS TABLE(
    bucket_index integer,
    total_data bigint,
    completed bigint,
    pending_review bigint
)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_bucket_ends timestamptz[];
    v_total_data bigint[];
    v_completed bigint[];
    v_pending_review bigint[];
BEGIN
    IF p_project_ids IS NULL
       OR (
           pg_catalog.cardinality(p_project_ids) > 0
           AND pg_catalog.array_ndims(p_project_ids) IS DISTINCT FROM 1
       )
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.unnest(p_project_ids) AS requested(project_id)
           WHERE requested.project_id IS NULL
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace project query ids';
    END IF;
    IF p_bucket_ends IS NULL
       OR pg_catalog.cardinality(p_bucket_ends) <> 12
       OR pg_catalog.array_ndims(p_bucket_ends) IS DISTINCT FROM 1
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.unnest(p_bucket_ends) AS endpoint(value)
           WHERE endpoint.value IS NULL
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace project task-series buckets';
    END IF;

    SELECT pg_catalog.array_agg(endpoint.value ORDER BY endpoint.ordinality)
    INTO v_bucket_ends
    FROM pg_catalog.unnest(p_bucket_ends) WITH ORDINALITY
        AS endpoint(value, ordinality);

    SELECT ARRAY[
               pg_catalog.count(*) FILTER (WHERE task.created_at <= v_bucket_ends[1]),
               pg_catalog.count(*) FILTER (WHERE task.created_at <= v_bucket_ends[2]),
               pg_catalog.count(*) FILTER (WHERE task.created_at <= v_bucket_ends[3]),
               pg_catalog.count(*) FILTER (WHERE task.created_at <= v_bucket_ends[4]),
               pg_catalog.count(*) FILTER (WHERE task.created_at <= v_bucket_ends[5]),
               pg_catalog.count(*) FILTER (WHERE task.created_at <= v_bucket_ends[6]),
               pg_catalog.count(*) FILTER (WHERE task.created_at <= v_bucket_ends[7]),
               pg_catalog.count(*) FILTER (WHERE task.created_at <= v_bucket_ends[8]),
               pg_catalog.count(*) FILTER (WHERE task.created_at <= v_bucket_ends[9]),
               pg_catalog.count(*) FILTER (WHERE task.created_at <= v_bucket_ends[10]),
               pg_catalog.count(*) FILTER (WHERE task.created_at <= v_bucket_ends[11]),
               pg_catalog.count(*) FILTER (WHERE task.created_at <= v_bucket_ends[12])
           ]::bigint[],
           ARRAY[
               pg_catalog.count(*) FILTER (
                   WHERE task.status = 'completed'
                     AND (task.reviewed_at <= v_bucket_ends[1]
                          OR (task.reviewed_at IS NULL
                              AND task.updated_at <= v_bucket_ends[1]))
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.status = 'completed'
                     AND (task.reviewed_at <= v_bucket_ends[2]
                          OR (task.reviewed_at IS NULL
                              AND task.updated_at <= v_bucket_ends[2]))
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.status = 'completed'
                     AND (task.reviewed_at <= v_bucket_ends[3]
                          OR (task.reviewed_at IS NULL
                              AND task.updated_at <= v_bucket_ends[3]))
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.status = 'completed'
                     AND (task.reviewed_at <= v_bucket_ends[4]
                          OR (task.reviewed_at IS NULL
                              AND task.updated_at <= v_bucket_ends[4]))
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.status = 'completed'
                     AND (task.reviewed_at <= v_bucket_ends[5]
                          OR (task.reviewed_at IS NULL
                              AND task.updated_at <= v_bucket_ends[5]))
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.status = 'completed'
                     AND (task.reviewed_at <= v_bucket_ends[6]
                          OR (task.reviewed_at IS NULL
                              AND task.updated_at <= v_bucket_ends[6]))
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.status = 'completed'
                     AND (task.reviewed_at <= v_bucket_ends[7]
                          OR (task.reviewed_at IS NULL
                              AND task.updated_at <= v_bucket_ends[7]))
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.status = 'completed'
                     AND (task.reviewed_at <= v_bucket_ends[8]
                          OR (task.reviewed_at IS NULL
                              AND task.updated_at <= v_bucket_ends[8]))
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.status = 'completed'
                     AND (task.reviewed_at <= v_bucket_ends[9]
                          OR (task.reviewed_at IS NULL
                              AND task.updated_at <= v_bucket_ends[9]))
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.status = 'completed'
                     AND (task.reviewed_at <= v_bucket_ends[10]
                          OR (task.reviewed_at IS NULL
                              AND task.updated_at <= v_bucket_ends[10]))
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.status = 'completed'
                     AND (task.reviewed_at <= v_bucket_ends[11]
                          OR (task.reviewed_at IS NULL
                              AND task.updated_at <= v_bucket_ends[11]))
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.status = 'completed'
                     AND (task.reviewed_at <= v_bucket_ends[12]
                          OR (task.reviewed_at IS NULL
                              AND task.updated_at <= v_bucket_ends[12]))
               )
           ]::bigint[],
           ARRAY[
               pg_catalog.count(*) FILTER (
                   WHERE task.submitted_at IS NOT NULL
                     AND task.submitted_at <= v_bucket_ends[1]
                     AND (task.reviewed_at IS NULL
                          OR task.reviewed_at > v_bucket_ends[1])
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.submitted_at IS NOT NULL
                     AND task.submitted_at <= v_bucket_ends[2]
                     AND (task.reviewed_at IS NULL
                          OR task.reviewed_at > v_bucket_ends[2])
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.submitted_at IS NOT NULL
                     AND task.submitted_at <= v_bucket_ends[3]
                     AND (task.reviewed_at IS NULL
                          OR task.reviewed_at > v_bucket_ends[3])
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.submitted_at IS NOT NULL
                     AND task.submitted_at <= v_bucket_ends[4]
                     AND (task.reviewed_at IS NULL
                          OR task.reviewed_at > v_bucket_ends[4])
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.submitted_at IS NOT NULL
                     AND task.submitted_at <= v_bucket_ends[5]
                     AND (task.reviewed_at IS NULL
                          OR task.reviewed_at > v_bucket_ends[5])
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.submitted_at IS NOT NULL
                     AND task.submitted_at <= v_bucket_ends[6]
                     AND (task.reviewed_at IS NULL
                          OR task.reviewed_at > v_bucket_ends[6])
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.submitted_at IS NOT NULL
                     AND task.submitted_at <= v_bucket_ends[7]
                     AND (task.reviewed_at IS NULL
                          OR task.reviewed_at > v_bucket_ends[7])
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.submitted_at IS NOT NULL
                     AND task.submitted_at <= v_bucket_ends[8]
                     AND (task.reviewed_at IS NULL
                          OR task.reviewed_at > v_bucket_ends[8])
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.submitted_at IS NOT NULL
                     AND task.submitted_at <= v_bucket_ends[9]
                     AND (task.reviewed_at IS NULL
                          OR task.reviewed_at > v_bucket_ends[9])
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.submitted_at IS NOT NULL
                     AND task.submitted_at <= v_bucket_ends[10]
                     AND (task.reviewed_at IS NULL
                          OR task.reviewed_at > v_bucket_ends[10])
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.submitted_at IS NOT NULL
                     AND task.submitted_at <= v_bucket_ends[11]
                     AND (task.reviewed_at IS NULL
                          OR task.reviewed_at > v_bucket_ends[11])
               ),
               pg_catalog.count(*) FILTER (
                   WHERE task.submitted_at IS NOT NULL
                     AND task.submitted_at <= v_bucket_ends[12]
                     AND (task.reviewed_at IS NULL
                          OR task.reviewed_at > v_bucket_ends[12])
               )
           ]::bigint[]
    INTO v_total_data, v_completed, v_pending_review
    FROM public.tasks AS task
    WHERE task.project_id = ANY (p_project_ids);

    RETURN QUERY
    SELECT total.ordinality::integer,
           total.value,
           completed.value,
           pending.value
    FROM pg_catalog.unnest(v_total_data) WITH ORDINALITY
        AS total(value, ordinality)
    JOIN pg_catalog.unnest(v_completed) WITH ORDINALITY
        AS completed(value, ordinality) USING (ordinality)
    JOIN pg_catalog.unnest(v_pending_review) WITH ORDINALITY
        AS pending(value, ordinality) USING (ordinality)
    ORDER BY total.ordinality;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.workspace_project_query_task_series(uuid[], timestamptz[])
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.workspace_project_query_annotation_series(
    p_project_ids uuid[],
    p_bucket_ends timestamptz[]
)
RETURNS TABLE(
    bucket_index integer,
    total_annotations bigint,
    ai_annotations bigint
)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_bucket_ends timestamptz[];
    v_total_annotations bigint[];
    v_ai_annotations bigint[];
BEGIN
    IF p_project_ids IS NULL
       OR (
           pg_catalog.cardinality(p_project_ids) > 0
           AND pg_catalog.array_ndims(p_project_ids) IS DISTINCT FROM 1
       )
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.unnest(p_project_ids) AS requested(project_id)
           WHERE requested.project_id IS NULL
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace project query ids';
    END IF;
    IF p_bucket_ends IS NULL
       OR pg_catalog.cardinality(p_bucket_ends) <> 12
       OR pg_catalog.array_ndims(p_bucket_ends) IS DISTINCT FROM 1
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.unnest(p_bucket_ends) AS endpoint(value)
           WHERE endpoint.value IS NULL
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace project annotation-series buckets';
    END IF;

    SELECT pg_catalog.array_agg(endpoint.value ORDER BY endpoint.ordinality)
    INTO v_bucket_ends
    FROM pg_catalog.unnest(p_bucket_ends) WITH ORDINALITY
        AS endpoint(value, ordinality);

    SELECT ARRAY[
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[1]
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[2]
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[3]
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[4]
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[5]
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[6]
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[7]
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[8]
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[9]
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[10]
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[11]
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[12]
               )
           ]::bigint[],
           ARRAY[
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[1]
                     AND annotation.parent_prediction_id IS NOT NULL
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[2]
                     AND annotation.parent_prediction_id IS NOT NULL
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[3]
                     AND annotation.parent_prediction_id IS NOT NULL
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[4]
                     AND annotation.parent_prediction_id IS NOT NULL
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[5]
                     AND annotation.parent_prediction_id IS NOT NULL
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[6]
                     AND annotation.parent_prediction_id IS NOT NULL
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[7]
                     AND annotation.parent_prediction_id IS NOT NULL
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[8]
                     AND annotation.parent_prediction_id IS NOT NULL
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[9]
                     AND annotation.parent_prediction_id IS NOT NULL
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[10]
                     AND annotation.parent_prediction_id IS NOT NULL
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[11]
                     AND annotation.parent_prediction_id IS NOT NULL
               ),
               pg_catalog.count(*) FILTER (
                   WHERE annotation.created_at <= v_bucket_ends[12]
                     AND annotation.parent_prediction_id IS NOT NULL
               )
           ]::bigint[]
    INTO v_total_annotations, v_ai_annotations
    FROM public.annotations AS annotation
    WHERE annotation.project_id = ANY (p_project_ids)
      AND annotation.is_active IS TRUE
      AND annotation.was_cancelled IS FALSE;

    RETURN QUERY
    SELECT total.ordinality::integer,
           total.value,
           ai.value
    FROM pg_catalog.unnest(v_total_annotations) WITH ORDINALITY
        AS total(value, ordinality)
    JOIN pg_catalog.unnest(v_ai_annotations) WITH ORDINALITY
        AS ai(value, ordinality) USING (ordinality)
    ORDER BY total.ordinality;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.workspace_project_query_annotation_series(
        uuid[], timestamptz[]
    )
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.workspace_project_query_dataset_ids(
    p_project_id uuid
)
RETURNS TABLE(dataset_id uuid)
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
            MESSAGE = 'invalid Workspace project query project';
    END IF;

    RETURN QUERY
    SELECT association.dataset_id
    FROM public.project_datasets AS association
    WHERE association.project_id = p_project_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_assets_v1.workspace_project_query_dataset_ids(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.workspace_project_query_dataset_item_count(
    p_dataset_ids uuid[]
)
RETURNS bigint
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_count bigint;
BEGIN
    IF p_dataset_ids IS NULL
       OR (
           pg_catalog.cardinality(p_dataset_ids) > 0
           AND pg_catalog.array_ndims(p_dataset_ids) IS DISTINCT FROM 1
       )
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.unnest(p_dataset_ids) AS requested(dataset_id)
           WHERE requested.dataset_id IS NULL
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace project query dataset ids';
    END IF;

    SELECT pg_catalog.count(*)::bigint
    INTO v_count
    FROM public.dataset_items AS item
    WHERE item.dataset_id = ANY (p_dataset_ids);

    RETURN v_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_assets_v1.workspace_project_query_dataset_item_count(uuid[])
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.workspace_project_query_dataset_item_ids(
    p_dataset_ids uuid[]
)
RETURNS TABLE(item_id uuid)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_dataset_ids IS NULL
       OR (
           pg_catalog.cardinality(p_dataset_ids) > 0
           AND pg_catalog.array_ndims(p_dataset_ids) IS DISTINCT FROM 1
       )
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.unnest(p_dataset_ids) AS requested(dataset_id)
           WHERE requested.dataset_id IS NULL
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace project query dataset ids';
    END IF;

    RETURN QUERY
    SELECT item.id
    FROM public.dataset_items AS item
    WHERE item.dataset_id = ANY (p_dataset_ids);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_assets_v1.workspace_project_query_dataset_item_ids(uuid[])
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.workspace_project_query_linked_task_count(
    p_project_id uuid,
    p_dataset_ids uuid[]
)
RETURNS bigint
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_count bigint;
BEGIN
    IF p_project_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace project query project';
    END IF;
    IF p_dataset_ids IS NULL
       OR (
           pg_catalog.cardinality(p_dataset_ids) > 0
           AND pg_catalog.array_ndims(p_dataset_ids) IS DISTINCT FROM 1
       )
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.unnest(p_dataset_ids) AS requested(dataset_id)
           WHERE requested.dataset_id IS NULL
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace project query dataset ids';
    END IF;

    SELECT pg_catalog.count(*)::bigint
    INTO v_count
    FROM public.tasks AS task
    WHERE task.project_id = p_project_id
      AND task.status <> 'uploading'
      AND task.dataset_item_id IS NOT NULL
      AND task.dataset_item_id IN (
          SELECT item.item_id
          FROM aap_assets_v1.workspace_project_query_dataset_item_ids(
              p_dataset_ids
          ) AS item
      );

    RETURN v_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.workspace_project_query_linked_task_count(uuid, uuid[])
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.workspace_project_query_task_count(
    p_project_id uuid
)
RETURNS bigint
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_count bigint;
BEGIN
    IF p_project_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace project query project';
    END IF;

    SELECT pg_catalog.count(*)::bigint
    INTO v_count
    FROM public.tasks AS task
    WHERE task.project_id = p_project_id
      AND task.status <> 'uploading';

    RETURN v_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.workspace_project_query_task_count(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.workspace_project_query_batches(
    p_project_id uuid
)
RETURNS TABLE(
    id uuid,
    status text,
    annotator_id uuid,
    reviewer_id uuid
)
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
            MESSAGE = 'invalid Workspace project query project';
    END IF;

    RETURN QUERY
    SELECT batch.id,
           batch.status::text,
           batch.annotator_id,
           batch.reviewer_id
    FROM public.task_batches AS batch
    WHERE batch.project_id = p_project_id
      AND batch.status <> 'archived';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.workspace_project_query_batches(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.workspace_project_query_batch_task_counts(
    p_project_id uuid
)
RETURNS TABLE(batch_id uuid, task_count bigint)
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
            MESSAGE = 'invalid Workspace project query project';
    END IF;

    RETURN QUERY
    SELECT task.batch_id,
           pg_catalog.count(*)::bigint
    FROM public.tasks AS task
    WHERE task.project_id = p_project_id
      AND task.status <> 'uploading'
    GROUP BY task.batch_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.workspace_project_query_batch_task_counts(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.workspace_project_query_job_status_counts(
    p_project_id uuid
)
RETURNS TABLE(status text, job_count bigint)
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
            MESSAGE = 'invalid Workspace project query project';
    END IF;

    RETURN QUERY
    SELECT job.status::text,
           pg_catalog.count(*)::bigint
    FROM public.async_jobs AS job
    WHERE job.project_id = p_project_id
      AND job.kind = 'create_tasks'
    GROUP BY job.status;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_jobs_v1.workspace_project_query_job_status_counts(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.workspace_project_query_latest_job_status(
    p_project_id uuid
)
RETURNS TABLE(status text)
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
            MESSAGE = 'invalid Workspace project query project';
    END IF;

    RETURN QUERY
    SELECT job.status::text
    FROM public.async_jobs AS job
    WHERE job.project_id = p_project_id
      AND job.kind = 'create_tasks'
    ORDER BY job.created_at DESC, job.id DESC
    LIMIT 1;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_jobs_v1.workspace_project_query_latest_job_status(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_v1.workspace_project_query_pool(
    p_pool_id uuid
)
RETURNS TABLE(pool_id uuid, legacy_instance_id uuid)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_pool_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Workspace project query pool';
    END IF;

    RETURN QUERY
    SELECT pool.id,
           pool.legacy_instance_id
    FROM public.ml_backend_service_pools AS pool
    WHERE pool.id = p_pool_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_inference_v1.workspace_project_query_pool(uuid)
    FROM PUBLIC;
