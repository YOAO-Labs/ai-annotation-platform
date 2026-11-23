CREATE FUNCTION yoao_insights_v1._dataset_view_snapshot(p_view public.dataset_saved_views)
RETURNS jsonb
LANGUAGE sql STABLE STRICT
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', p_view.id, 'dataset_id', p_view.dataset_id, 'name', p_view.name,
        'query', p_view.query, 'query_schema_version', p_view.query_schema_version,
        'created_at', p_view.created_at, 'updated_at', p_view.updated_at
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_insights_v1.list_personal_dataset_views(p_actor uuid, p_dataset uuid)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT yoao_insights_v1._dataset_view_snapshot(v)
    FROM public.dataset_saved_views AS v
    WHERE v.dataset_id = p_dataset AND v.owner_id = p_actor
    ORDER BY v.name, v.id
    LIMIT 100;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_insights_v1.has_personal_dataset_view(p_actor uuid, p_dataset uuid, p_view uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT FROM public.dataset_saved_views AS v
        WHERE v.id = p_view AND v.dataset_id = p_dataset AND v.owner_id = p_actor
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_insights_v1.prepare_personal_dataset_view(p_actor uuid, p_dataset uuid)
RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_actor IS NULL OR p_dataset IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Missing personal dataset view scope';
    END IF;
    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE = '0A000', MESSAGE = 'Personal dataset view creation requires READ COMMITTED';
    END IF;
    IF yoao_assets_v1.lock_dataset_reference(p_dataset) IS DISTINCT FROM p_dataset THEN
        RAISE EXCEPTION USING ERRCODE = 'ID404', MESSAGE = 'Dataset not found';
    END IF;
    IF (SELECT pg_catalog.count(*) FROM public.dataset_saved_views AS v
        WHERE v.dataset_id = p_dataset AND v.owner_id = p_actor) >= 100 THEN
        RAISE EXCEPTION USING ERRCODE = 'ID409', MESSAGE = 'At most 100 personal views per dataset';
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_insights_v1.create_personal_dataset_view(
    p_actor uuid, p_dataset uuid, p_name text, p_query jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_row public.dataset_saved_views;
BEGIN
    PERFORM yoao_insights_v1.prepare_personal_dataset_view(p_actor, p_dataset);
    IF pg_catalog.jsonb_typeof(p_query) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid personal dataset view query';
    END IF;
    INSERT INTO public.dataset_saved_views AS v (id, dataset_id, owner_id, name, query)
    VALUES (pg_catalog.gen_random_uuid(), p_dataset, p_actor, p_name, p_query)
    RETURNING v.* INTO v_row;
    RETURN yoao_insights_v1._dataset_view_snapshot(v_row);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_insights_v1.update_personal_dataset_view(
    p_actor uuid, p_dataset uuid, p_view uuid, p_name text, p_query jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_row public.dataset_saved_views;
BEGIN
    IF pg_catalog.jsonb_typeof(p_query) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Invalid personal dataset view query';
    END IF;
    SELECT v.* INTO v_row FROM public.dataset_saved_views AS v
    WHERE v.id = p_view AND v.dataset_id = p_dataset AND v.owner_id = p_actor
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'ID414', MESSAGE = 'Personal dataset view not found';
    END IF;
    IF ROW(v_row.name::text, v_row.query, v_row.query_schema_version) IS DISTINCT FROM ROW(p_name, p_query, 1) THEN
        UPDATE public.dataset_saved_views AS v
        SET name = p_name, query = p_query, query_schema_version = 1, updated_at = pg_catalog.now()
        WHERE v.id = p_view
        RETURNING v.* INTO v_row;
    END IF;
    RETURN yoao_insights_v1._dataset_view_snapshot(v_row);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_insights_v1.delete_personal_dataset_view(p_actor uuid, p_dataset uuid, p_view uuid)
RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    DELETE FROM public.dataset_saved_views AS v
    WHERE v.id = p_view AND v.dataset_id = p_dataset AND v.owner_id = p_actor;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'ID414', MESSAGE = 'Personal dataset view not found';
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_insights_v1._dataset_view_snapshot(public.dataset_saved_views),
    yoao_insights_v1.list_personal_dataset_views(uuid,uuid),
    yoao_insights_v1.has_personal_dataset_view(uuid,uuid,uuid),
    yoao_insights_v1.prepare_personal_dataset_view(uuid,uuid),
    yoao_insights_v1.create_personal_dataset_view(uuid,uuid,text,jsonb),
    yoao_insights_v1.update_personal_dataset_view(uuid,uuid,uuid,text,jsonb),
    yoao_insights_v1.delete_personal_dataset_view(uuid,uuid,uuid) FROM PUBLIC;
