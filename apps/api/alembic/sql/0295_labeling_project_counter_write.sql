CREATE FUNCTION aap_labeling_v1.write_project_counter_projection(
    p_project_id uuid,
    p_patch jsonb
)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_column text;
    v_assignments text[] := ARRAY[]::text[];
    v_updated_at_expression text;
    v_project_id uuid;
BEGIN
    IF p_project_id IS NULL
       OR p_patch IS NULL
       OR pg_catalog.jsonb_typeof(p_patch) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Labeling Project counter patch';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_object_keys(p_patch) AS supplied(name)
        WHERE supplied.name NOT IN (
            'total_tasks',
            'completed_tasks',
            'review_tasks',
            'in_progress_tasks',
            'batch_summary',
            'updated_at'
        )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid Labeling Project counter patch fields';
    END IF;

    FOREACH v_column IN ARRAY ARRAY[
        'total_tasks',
        'completed_tasks',
        'review_tasks',
        'in_progress_tasks',
        'batch_summary'
    ] LOOP
        IF p_patch ? v_column THEN
            v_assignments := pg_catalog.array_append(
                v_assignments,
                pg_catalog.format(
                    '%I = (pg_catalog.jsonb_populate_record('
                    'NULL::public.projects, $2)).%I',
                    v_column,
                    v_column
                )
            );
        END IF;
    END LOOP;

    IF pg_catalog.cardinality(v_assignments) = 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'empty Labeling Project counter patch';
    END IF;

    IF p_patch ? 'updated_at' THEN
        v_updated_at_expression :=
            '(pg_catalog.jsonb_populate_record('
            'NULL::public.projects, $2)).updated_at';
    ELSE
        v_updated_at_expression := 'pg_catalog.now()';
    END IF;

    EXECUTE pg_catalog.format(
        'UPDATE public.projects SET %s, updated_at = %s '
        'WHERE id = $1 RETURNING id',
        pg_catalog.array_to_string(v_assignments, ', '),
        v_updated_at_expression
    )
    INTO v_project_id
    USING p_project_id, p_patch;

    RETURN v_project_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.write_project_counter_projection(uuid, jsonb)
    FROM PUBLIC;
