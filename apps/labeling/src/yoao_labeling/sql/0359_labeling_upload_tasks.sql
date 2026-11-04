CREATE FUNCTION aap_labeling_v1.create_uploading_task(
    p_task_id uuid, p_project_id uuid, p_file_name text,
    p_file_path text, p_file_type text
)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
BEGIN
    IF p_task_id IS NULL OR p_project_id IS NULL
       OR p_file_name IS NULL OR char_length(p_file_name) > 500
       OR p_file_path IS NULL OR char_length(p_file_path) > 1000
       OR p_file_type IS NULL OR char_length(p_file_type) > 20 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid upload Task scope';
    END IF;
    INSERT INTO public.tasks (
        id, project_id, display_id, file_name, file_path, file_type,
        tags, status, assignee_is_override, is_labeled, overlap,
        total_annotations, total_predictions, reviewer_is_override,
        first_review_eligible, annotation_contributor_ids, reopened_count, version
    ) VALUES (
        p_task_id, p_project_id,
        'T-' || pg_catalog.nextval('public.display_seq_tasks'::pg_catalog.regclass)::text,
        p_file_name, p_file_path, p_file_type, '[]'::jsonb, 'uploading',
        false, false, 1, 0, 0, false, true, '[]'::jsonb, 0, 1
    );
    RETURN p_task_id;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.complete_uploading_task(
    p_task_id uuid, p_project_id uuid, p_batch_id uuid,
    p_file_path text, p_file_type text
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE
    v_task record;
BEGIN
    IF p_task_id IS NULL OR p_project_id IS NULL
       OR p_file_path IS NULL OR char_length(p_file_path) > 1000
       OR p_file_type IS NULL OR char_length(p_file_type) > 20 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid upload Task scope';
    END IF;
    SELECT task.project_id, task.batch_id, task.status, task.file_path, task.file_type
    INTO v_task FROM public.tasks AS task
    WHERE task.id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN 'task_not_found';
    END IF;
    IF v_task.status IS DISTINCT FROM 'uploading' THEN
        RETURN 'task_not_uploading';
    END IF;
    IF v_task.project_id IS DISTINCT FROM p_project_id
       OR v_task.batch_id IS DISTINCT FROM p_batch_id
       OR v_task.file_path IS DISTINCT FROM p_file_path
       OR v_task.file_type IS DISTINCT FROM p_file_type THEN
        RETURN 'task_target_changed';
    END IF;
    UPDATE public.tasks SET status = 'pending', updated_at = pg_catalog.now()
    WHERE id = p_task_id;
    RETURN 'pending';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.create_uploading_task(uuid,uuid,text,text,text),
    aap_labeling_v1.complete_uploading_task(uuid,uuid,uuid,text,text) FROM PUBLIC;
