CREATE FUNCTION aap_labeling_v1.decrement_project_review_count(
    p_project_id uuid
)
RETURNS integer
LANGUAGE sql
VOLATILE
CALLED ON NULL INPUT
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.projects AS project
    SET review_tasks = GREATEST(COALESCE(project.review_tasks, 0) - 1, 0),
        updated_at = pg_catalog.now()
    WHERE project.id = p_project_id
    RETURNING project.review_tasks
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.decrement_project_review_count(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.decrement_project_completed_count(
    p_project_id uuid
)
RETURNS integer
LANGUAGE sql
VOLATILE
CALLED ON NULL INPUT
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.projects AS project
    SET completed_tasks = GREATEST(COALESCE(project.completed_tasks, 0) - 1, 0),
        updated_at = pg_catalog.now()
    WHERE project.id = p_project_id
    RETURNING project.completed_tasks
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.decrement_project_completed_count(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.record_project_review_approval(
    p_project_id uuid
)
RETURNS TABLE(completed_tasks integer, review_tasks integer)
LANGUAGE sql
VOLATILE
CALLED ON NULL INPUT
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.projects AS project
    SET completed_tasks = COALESCE(project.completed_tasks, 0) + 1,
        review_tasks = GREATEST(COALESCE(project.review_tasks, 0) - 1, 0),
        updated_at = pg_catalog.now()
    WHERE project.id = p_project_id
    RETURNING project.completed_tasks, project.review_tasks
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.record_project_review_approval(uuid)
    FROM PUBLIC;
