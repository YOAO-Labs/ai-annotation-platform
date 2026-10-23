-- Fixed, read-only Native facts for project class and attribute usage.
CREATE SCHEMA aap_labeling_class_query_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_labeling_class_query_v1 FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_class_query_v1.class_query_policy()
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-class-query/v1'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_class_query_v1.class_query_policy()
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_class_query_v1.class_counts(
    p_project_id uuid
)
RETURNS TABLE(class_name text, annotation_count bigint)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_project_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'project_id is required';
    END IF;

    RETURN QUERY
    SELECT annotation.class_name::text,
           pg_catalog.count(annotation.id)
    FROM public.annotations AS annotation
    WHERE annotation.project_id = p_project_id
      AND annotation.is_active IS TRUE
    GROUP BY annotation.class_name;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_class_query_v1.class_counts(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_class_query_v1.attribute_rows(
    p_project_id uuid
)
RETURNS TABLE(attributes_json text)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_project_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'project_id is required';
    END IF;

    RETURN QUERY
    SELECT annotation.attributes::text
    FROM public.annotations AS annotation
    WHERE annotation.project_id = p_project_id
      AND annotation.is_active IS TRUE;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_class_query_v1.attribute_rows(uuid)
    FROM PUBLIC;
