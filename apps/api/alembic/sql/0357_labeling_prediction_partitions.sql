CREATE FUNCTION aap_labeling_v1.prediction_partition_command(p_request jsonb)
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
    v_month date;
    v_next_month date;
    v_name text;
    v_exists boolean;
    v_created text;
BEGIN
    IF p_request IS NULL
       OR pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-prediction-partitions/v1'
       OR p_request ->> 'operation' IS DISTINCT FROM 'ensure' THEN
        RAISE EXCEPTION 'Invalid Labeling prediction partition request'
            USING ERRCODE = '22023';
    END IF;

    SELECT pg_catalog.array_agg(key ORDER BY key)
    INTO v_keys
    FROM pg_catalog.jsonb_object_keys(p_request) AS request_keys(key);
    IF v_keys IS DISTINCT FROM ARRAY['month','operation','schema']::text[]
       OR pg_catalog.jsonb_typeof(p_request -> 'month') IS DISTINCT FROM 'string'
       OR p_request ->> 'month' !~ '^[0-9]{4}-(0[1-9]|1[0-2])-01$'
       OR pg_catalog.left(p_request ->> 'month', 4) = '0000'
       OR p_request ->> 'month' >= '9999-12-01' THEN
        RAISE EXCEPTION 'Invalid Labeling prediction partition month'
            USING ERRCODE = '22023';
    END IF;

    v_month := (p_request ->> 'month')::date;
    IF v_month < pg_catalog.date_trunc('month', pg_catalog.clock_timestamp())::date THEN
        RAISE EXCEPTION 'Labeling prediction partitions can only be ensured for current or future UTC months'
            USING ERRCODE = '22023';
    END IF;

    v_next_month := (v_month + interval '1 month')::date;
    v_name := 'predictions_y' || pg_catalog.to_char(v_month, 'YYYY"m"MM');
    SELECT EXISTS (
        SELECT FROM pg_catalog.pg_inherits AS inheritance
        JOIN pg_catalog.pg_class AS child
          ON child.oid = inheritance.inhrelid
        WHERE inheritance.inhparent = 'public.predictions'::pg_catalog.regclass
          AND child.relname = v_name
    ) INTO v_exists;

    IF NOT v_exists THEN
        EXECUTE pg_catalog.format(
            'CREATE TABLE IF NOT EXISTS public.%I PARTITION OF public.predictions '
            'FOR VALUES FROM (%L) TO (%L)',
            v_name,
            pg_catalog.to_char(v_month, 'YYYY-MM-DD') || ' 00:00:00+00',
            pg_catalog.to_char(v_next_month, 'YYYY-MM-DD') || ' 00:00:00+00'
        );
        v_created := v_name;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-prediction-partitions/v1',
        'ok', true,
        'request', p_request,
        'data', pg_catalog.jsonb_build_object(
            'operation', 'ensure',
            'month', pg_catalog.to_char(v_month, 'YYYY-MM-DD'),
            'created', v_created
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.prediction_partition_command(jsonb)
    FROM PUBLIC;
