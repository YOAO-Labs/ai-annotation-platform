-- This read is only for the Assets owner after it has authorized an image
-- actor or validated the exact worker claim. It does not authorize a caller to
-- read the original or reveal a locator to a product role.
CREATE FUNCTION aap_labeling_v1.lock_task_original_pointer(p_task_id uuid, p_object_key text)
RETURNS uuid
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_original_id uuid;
BEGIN
    SELECT task.original_id INTO v_original_id
    FROM public.tasks AS task
    WHERE task.id = p_task_id AND task.file_path = p_object_key
    FOR SHARE NOWAIT;
    RETURN v_original_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_task_original_pointer(uuid,text) FROM PUBLIC;
