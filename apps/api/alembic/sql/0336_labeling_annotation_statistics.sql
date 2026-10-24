CREATE FUNCTION aap_labeling_v1.annotation_statistics_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_keys text[];
    v_task_id uuid;
    v_total integer;
    v_is_labeled boolean;
    v_batch_id uuid;
    v_status text;
    v_new_status text;
    v_status_changed boolean := false;
    v_updated_at timestamptz;
    v_task_found boolean := false;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-annotation-statistics/v1'
       OR p_request ->> 'operation' IS DISTINCT FROM 'recompute_task_statistics' THEN
        RAISE EXCEPTION 'Invalid Labeling annotation-statistics request'
            USING ERRCODE = '22023';
    END IF;

    SELECT array_agg(key ORDER BY key) INTO v_keys
    FROM jsonb_object_keys(p_request) AS request_keys(key);
    IF v_keys IS DISTINCT FROM ARRAY['operation', 'schema', 'task_id']::text[]
       OR jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
       OR p_request ->> 'task_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        RAISE EXCEPTION 'Invalid Labeling annotation-statistics fields'
            USING ERRCODE = '22023';
    END IF;

    v_task_id := (p_request ->> 'task_id')::uuid;
    SELECT count(*)::integer INTO v_total
    FROM public.annotations AS annotation
    WHERE annotation.task_id = v_task_id
      AND annotation.is_active IS TRUE
      AND annotation.was_cancelled IS FALSE
      AND annotation.sensor_role IS NULL;
    v_is_labeled := v_total > 0;

    SELECT task.batch_id, task.status, task.updated_at
    INTO v_batch_id, v_status, v_updated_at
    FROM public.tasks AS task
    WHERE task.id = v_task_id;
    v_task_found := FOUND;
    IF v_task_found THEN
        v_new_status := CASE
            WHEN v_total > 0 AND v_status = 'pending' THEN 'in_progress'
            WHEN v_total = 0 AND v_status = 'in_progress' THEN 'pending'
            ELSE v_status
        END;
        v_status_changed := v_status IS DISTINCT FROM v_new_status;

        UPDATE public.tasks AS task
        SET total_annotations = v_total,
            is_labeled = v_is_labeled,
            status = v_new_status,
            updated_at = now()
        WHERE task.id = v_task_id
          AND (task.total_annotations IS DISTINCT FROM v_total
               OR task.is_labeled IS DISTINCT FROM v_is_labeled
               OR task.status IS DISTINCT FROM v_new_status)
        RETURNING task.updated_at INTO v_updated_at;
        IF NOT FOUND THEN
            SELECT task.updated_at INTO v_updated_at
            FROM public.tasks AS task
            WHERE task.id = v_task_id;
        END IF;
        IF v_updated_at IS NULL THEN
            RAISE EXCEPTION 'Labeling annotation-statistics Task disappeared'
                USING ERRCODE = '40001';
        END IF;
    ELSE
        v_batch_id := NULL;
        v_status := NULL;
        v_updated_at := NULL;
    END IF;

    RETURN jsonb_build_object(
        'schema', 'aap-labeling-annotation-statistics-data/v1',
        'ok', true,
        'request', p_request,
        'data', jsonb_build_object(
            'task_found', v_task_found,
            'total_annotations', v_total,
            'is_labeled', v_is_labeled,
            'status', CASE WHEN v_task_found THEN v_new_status ELSE NULL END,
            'batch_id', v_batch_id,
            'status_changed', v_status_changed,
            'updated_at', v_updated_at
        )
    );
END;
$function$;

-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.annotation_statistics_command(jsonb) FROM PUBLIC;
