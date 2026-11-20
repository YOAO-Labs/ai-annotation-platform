-- A retained Job scope identifies the original project, not a still-live one.
DO $migration$
DECLARE
    v_before constant text := $before$WHEN p_platform_role = 'super_admin' THEN true$before$;
    v_after constant text := $after$WHEN p_platform_role = 'super_admin' THEN
            visible.job_project_id IS NULL
            OR visible.job_project_id IN (
                SELECT project_scope.project_id
                FROM yoao_workspace_project_query_v1.project_search_scope(
                    p_actor_id, p_platform_role
                ) AS project_scope
            )$after$;
    v_definition text := pg_catalog.pg_get_functiondef(
        'yoao_notifications_v1._visible_inbox_rows(uuid,text)'::pg_catalog.regprocedure
    );
BEGIN
    IF pg_catalog.length(v_definition) - pg_catalog.length(
        pg_catalog.replace(v_definition, v_before, '')
    ) <> pg_catalog.length(v_before) THEN
        RAISE EXCEPTION 'Unexpected super-admin inbox visibility rule';
    END IF;
    EXECUTE pg_catalog.replace(v_definition, v_before, v_after);
END
$migration$;
