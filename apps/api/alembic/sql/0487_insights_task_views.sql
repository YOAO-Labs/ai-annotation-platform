CREATE FUNCTION yoao_insights_v1._task_view_project_scope(
    p_actor uuid, p_project uuid, p_write boolean
)
RETURNS boolean
LANGUAGE plpgsql VOLATILE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_access jsonb;
BEGIN
    IF p_write THEN
        v_access := yoao_workspace_v1.lock_media_project_access(p_actor, p_project);
    ELSE
        v_access := yoao_workspace_v1.read_project_visibility(p_actor, p_project);
    END IF;
    IF v_access -> 'ok' = 'true'::jsonb THEN
        RETURN COALESCE(v_access #> '{data,is_manager}' = 'true'::jsonb, false);
    END IF;
    CASE v_access #>> '{error,code}'
        WHEN 'account_not_active' THEN
            RAISE EXCEPTION USING ERRCODE = 'IT401', MESSAGE = 'Task view actor is inactive';
        WHEN 'project_missing' THEN
            RAISE EXCEPTION USING ERRCODE = 'IT404', MESSAGE = 'Task view project not found';
        WHEN 'invalid_account_role', 'invalid_project_role', 'incompatible_project_role' THEN
            RAISE EXCEPTION USING ERRCODE = 'IT403', MESSAGE = 'Task view project access denied';
        WHEN 'task_busy', 'account_busy' THEN
            RAISE EXCEPTION USING ERRCODE = 'IT409', MESSAGE = 'Task view project access is busy';
        ELSE
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid Workspace access reply';
    END CASE;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_insights_v1._task_view_snapshot(p_view public.project_task_views)
RETURNS jsonb
LANGUAGE sql STABLE STRICT
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', p_view.id, 'project_id', p_view.project_id, 'owner_id', p_view.owner_id,
        'name', p_view.name, 'visibility', p_view.visibility, 'entity_scope', p_view.entity_scope,
        'filter_json', p_view.filter_json, 'sort_json', p_view.sort_json,
        'columns_json', p_view.columns_json, 'created_at', p_view.created_at, 'updated_at', p_view.updated_at
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_insights_v1.list_saved_task_views(p_actor uuid, p_project uuid, p_scope text)
RETURNS SETOF jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM yoao_insights_v1._task_view_project_scope(p_actor, p_project, false);
    RETURN QUERY
    SELECT yoao_insights_v1._task_view_snapshot(v)
    FROM public.project_task_views AS v
    WHERE v.project_id = p_project AND v.entity_scope = p_scope
      AND (v.visibility = 'project' OR v.owner_id = p_actor)
    ORDER BY v.visibility DESC, v.name ASC;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_insights_v1.read_saved_task_view(p_actor uuid, p_project uuid, p_view uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM yoao_insights_v1._task_view_project_scope(p_actor, p_project, false);
    RETURN (
        SELECT yoao_insights_v1._task_view_snapshot(v)
        FROM public.project_task_views AS v
        WHERE v.id = p_view AND v.project_id = p_project
          AND (v.visibility = 'project' OR v.owner_id = p_actor)
    );
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_insights_v1.create_saved_task_view(
    p_actor uuid, p_project uuid, p_name text, p_visibility text, p_scope text,
    p_filter jsonb, p_sort jsonb, p_columns jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_row public.project_task_views;
    v_manager boolean := yoao_insights_v1._task_view_project_scope(p_actor, p_project, true);
BEGIN
    IF p_visibility = 'project' AND NOT v_manager THEN
        RAISE EXCEPTION USING ERRCODE = 'IT413', MESSAGE = 'Only project admins can create shared views';
    END IF;
    IF pg_catalog.jsonb_typeof(p_filter) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_sort) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(p_columns) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid task view definition';
    END IF;
    INSERT INTO public.project_task_views AS v (
        id, project_id, owner_id, name, visibility, entity_scope, filter_json, sort_json, columns_json
    ) VALUES (
        pg_catalog.gen_random_uuid(), p_project, p_actor, p_name, p_visibility,
        p_scope, p_filter, p_sort, p_columns
    ) RETURNING v.* INTO v_row;
    RETURN yoao_insights_v1._task_view_snapshot(v_row);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_insights_v1._lock_writable_task_view(
    p_actor uuid, p_project uuid, p_view uuid, p_share boolean
)
RETURNS public.project_task_views
LANGUAGE plpgsql VOLATILE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_row public.project_task_views;
    v_manager boolean := yoao_insights_v1._task_view_project_scope(p_actor, p_project, true);
BEGIN
    SELECT v.* INTO v_row
    FROM public.project_task_views AS v
    WHERE v.id = p_view AND v.project_id = p_project
      AND (v.visibility = 'project' OR v.owner_id = p_actor)
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'IT414', MESSAGE = 'Task view not found';
    END IF;
    IF v_row.visibility = 'project' AND NOT v_manager THEN
        RAISE EXCEPTION USING ERRCODE = 'IT423', MESSAGE = 'Only project admins can edit shared views';
    END IF;
    IF p_share AND NOT v_manager THEN
        RAISE EXCEPTION USING ERRCODE = 'IT433', MESSAGE = 'Only project admins can share views';
    END IF;
    RETURN v_row;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_insights_v1.update_saved_task_view(
    p_actor uuid, p_project uuid, p_view uuid, p_name text, p_visibility text,
    p_filter jsonb, p_sort jsonb, p_columns jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_row public.project_task_views := yoao_insights_v1._lock_writable_task_view(
        p_actor, p_project, p_view, p_visibility = 'project'
    );
BEGIN
    IF (p_filter IS NOT NULL AND pg_catalog.jsonb_typeof(p_filter) IS DISTINCT FROM 'object')
       OR (p_sort IS NOT NULL AND pg_catalog.jsonb_typeof(p_sort) IS DISTINCT FROM 'array')
       OR (p_columns IS NOT NULL AND pg_catalog.jsonb_typeof(p_columns) IS DISTINCT FROM 'array') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid task view definition';
    END IF;
    IF ROW(COALESCE(p_name, v_row.name), COALESCE(p_visibility, v_row.visibility),
           COALESCE(p_filter, v_row.filter_json), COALESCE(p_sort, v_row.sort_json),
           COALESCE(p_columns, v_row.columns_json)) IS DISTINCT FROM
       ROW(v_row.name::text, v_row.visibility::text, v_row.filter_json,
           v_row.sort_json, v_row.columns_json) THEN
        UPDATE public.project_task_views AS v
        SET name = COALESCE(p_name, v.name), visibility = COALESCE(p_visibility, v.visibility),
            filter_json = COALESCE(p_filter, v.filter_json), sort_json = COALESCE(p_sort, v.sort_json),
            columns_json = COALESCE(p_columns, v.columns_json), updated_at = pg_catalog.now()
        WHERE v.id = v_row.id
        RETURNING v.* INTO v_row;
    END IF;
    RETURN yoao_insights_v1._task_view_snapshot(v_row);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_insights_v1.delete_saved_task_view(p_actor uuid, p_project uuid, p_view uuid)
RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM yoao_insights_v1._lock_writable_task_view(p_actor, p_project, p_view, false);
    DELETE FROM public.project_task_views AS v WHERE v.id = p_view;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_insights_v1._task_view_project_scope(uuid,uuid,boolean),
    yoao_insights_v1._task_view_snapshot(public.project_task_views),
    yoao_insights_v1._lock_writable_task_view(uuid,uuid,uuid,boolean),
    yoao_insights_v1.list_saved_task_views(uuid,uuid,text),
    yoao_insights_v1.read_saved_task_view(uuid,uuid,uuid),
    yoao_insights_v1.create_saved_task_view(uuid,uuid,text,text,text,jsonb,jsonb,jsonb),
    yoao_insights_v1.update_saved_task_view(uuid,uuid,uuid,text,text,jsonb,jsonb,jsonb),
    yoao_insights_v1.delete_saved_task_view(uuid,uuid,uuid) FROM PUBLIC;
