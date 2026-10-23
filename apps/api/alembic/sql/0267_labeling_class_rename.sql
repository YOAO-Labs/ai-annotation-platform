-- Narrow Labeling-owned annotation class-name effect for Workspace project rename.
CREATE OR REPLACE FUNCTION aap_labeling_v1.rename_project_annotation_class(
    p_project uuid,
    p_old text,
    p_new text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_project IS NULL OR p_old IS NULL OR p_new IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'project_id and class names are required';
    END IF;

    UPDATE public.annotations
    SET class_name = p_new
    WHERE project_id = p_project
      AND class_name = p_old;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.rename_project_annotation_class(uuid, text, text)
    FROM PUBLIC;
