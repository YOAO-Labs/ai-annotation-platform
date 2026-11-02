-- Slice receipts hash compact, sorted JSON, unlike the revision table's jsonb::text digest.
CREATE FUNCTION aap_labeling_v1._mask_revision_canonical_json(p_value jsonb)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_type text := jsonb_typeof(p_value);
    v_result text;
BEGIN
    IF v_type = 'object' THEN
        SELECT '{' || COALESCE(string_agg(
            to_json(pair.key)::text || ':' ||
            aap_labeling_v1._mask_revision_canonical_json(pair.value),
            ',' ORDER BY pair.key COLLATE "C"
        ), '') || '}' INTO v_result
        FROM jsonb_each(p_value) AS pair(key, value);
        RETURN v_result;
    ELSIF v_type = 'array' THEN
        SELECT '[' || COALESCE(string_agg(
            aap_labeling_v1._mask_revision_canonical_json(item.value),
            ',' ORDER BY item.ordinality
        ), '') || ']' INTO v_result
        FROM jsonb_array_elements(p_value) WITH ORDINALITY AS item(value, ordinality);
        RETURN v_result;
    END IF;
    RETURN p_value::text;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._mask_revision_canonical_json(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_labeling_v1.mask_revision_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_operation text;
    v_keys text[];
    v_expected_keys text[];
    v_task_id uuid;
    v_annotation_id uuid;
    v_version integer;
    v_reference jsonb;
    v_entry record;
    v_geometry jsonb;
    v_task_exists boolean;
    v_count integer := 0;
    v_deleted integer := 0;
    v_rows jsonb := '[]'::jsonb;
    v_reply jsonb;
    v_now timestamptz;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-mask-revisions/v1'
       OR jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Mask revision request' USING ERRCODE = '22023';
    END IF;
    v_operation := p_request ->> 'operation';
    CASE v_operation
        WHEN 'resolve' THEN
            v_expected_keys := ARRAY['schema','operation','task_id','references','lock'];
        WHEN 'protect' THEN
            v_expected_keys := ARRAY['schema','operation','task_id','references','expires_at'];
        WHEN 'expire' THEN
            v_expected_keys := ARRAY['schema','operation','now'];
        ELSE
            RAISE EXCEPTION 'Invalid Mask revision operation' USING ERRCODE = '22023';
    END CASE;
    SELECT array_agg(key ORDER BY key) INTO v_keys
    FROM jsonb_object_keys(p_request) AS fields(key);
    SELECT array_agg(key ORDER BY key) INTO v_expected_keys
    FROM unnest(v_expected_keys) AS fields(key);
    IF v_keys IS DISTINCT FROM v_expected_keys THEN
        RAISE EXCEPTION 'Invalid Mask revision request fields' USING ERRCODE = '22023';
    END IF;
    IF v_operation = 'expire' THEN
        IF jsonb_typeof(p_request -> 'now') NOT IN ('string','null') THEN
            RAISE EXCEPTION 'Invalid Mask revision expiry time' USING ERRCODE = '22023';
        END IF;
        v_now := COALESCE((p_request ->> 'now')::timestamptz,clock_timestamp());
        WITH expired AS (
            DELETE FROM public.mask_annotation_revisions AS revision
            WHERE revision.expires_at <= v_now
            RETURNING revision.id
        ) SELECT count(*)::integer INTO v_count FROM expired;
    ELSE
        IF jsonb_typeof(p_request -> 'references') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid Mask revision references' USING ERRCODE = '22023';
        END IF;
        IF v_operation = 'resolve' THEN
            IF jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'lock') IS DISTINCT FROM 'boolean' THEN
                RAISE EXCEPTION 'Invalid Mask revision resolve request' USING ERRCODE = '22023';
            END IF;
            v_task_id := (p_request ->> 'task_id')::uuid;
            SELECT EXISTS(SELECT 1 FROM public.tasks AS task WHERE task.id = v_task_id)
            INTO v_task_exists;
            IF NOT v_task_exists THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_revision_error',
                    DETAIL = jsonb_build_object('status_code',404,'detail',jsonb_build_object('reason','task_not_found'))::text;
            END IF;
        ELSE
            IF jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'expires_at') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION 'Invalid Mask revision expiry time' USING ERRCODE = '22023';
            END IF;
            v_task_id := (p_request ->> 'task_id')::uuid;
            IF NOT EXISTS (SELECT 1 FROM public.tasks AS task WHERE task.id = v_task_id) THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_revision_error',
                    DETAIL = jsonb_build_object('status_code',404,'detail',jsonb_build_object('reason','task_not_found'))::text;
            END IF;
        END IF;
        FOR v_reference IN SELECT value FROM jsonb_array_elements(p_request -> 'references') AS refs(value)
        LOOP
            IF jsonb_typeof(v_reference) IS DISTINCT FROM 'object'
               OR (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(v_reference) AS object_keys(key)) <> 3
               OR jsonb_typeof(v_reference -> 'annotation_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(v_reference -> 'annotation_version') IS DISTINCT FROM 'number'
               OR v_reference ->> 'annotation_version' !~ '^[1-9][0-9]*$'
               OR jsonb_typeof(v_reference -> 'geometry_digest') IS DISTINCT FROM 'string'
               OR v_reference ->> 'geometry_digest' !~ '^[0-9a-f]{64}$' THEN
                RAISE EXCEPTION 'Invalid Mask revision reference' USING ERRCODE = '22023';
            END IF;
            v_annotation_id := (v_reference ->> 'annotation_id')::uuid;
            v_version := (v_reference ->> 'annotation_version')::integer;
            IF v_operation = 'resolve' THEN
                SELECT annotation.geometry INTO v_geometry
                FROM public.annotations AS annotation
                WHERE annotation.id = v_annotation_id AND annotation.task_id = v_task_id
                  AND annotation.version = v_version;
                IF NOT FOUND THEN
                    IF (p_request ->> 'lock')::boolean THEN
                        SELECT revision.geometry INTO v_geometry
                        FROM public.mask_annotation_revisions AS revision
                        WHERE revision.task_id = v_task_id
                          AND revision.annotation_id = v_annotation_id
                          AND revision.annotation_version = v_version
                          AND encode(public.digest(convert_to(aap_labeling_v1._mask_revision_canonical_json(revision.geometry), 'UTF8'), 'sha256'), 'hex') = v_reference ->> 'geometry_digest'
                          AND revision.expires_at > clock_timestamp()
                        FOR UPDATE;
                    ELSE
                        SELECT revision.geometry INTO v_geometry
                        FROM public.mask_annotation_revisions AS revision
                        WHERE revision.task_id = v_task_id
                          AND revision.annotation_id = v_annotation_id
                          AND revision.annotation_version = v_version
                          AND encode(public.digest(convert_to(aap_labeling_v1._mask_revision_canonical_json(revision.geometry), 'UTF8'), 'sha256'), 'hex') = v_reference ->> 'geometry_digest'
                          AND revision.expires_at > clock_timestamp();
                    END IF;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_revision_error',
                            DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','snapshot_unavailable'))::text;
                    END IF;
                END IF;
                IF jsonb_typeof(v_geometry) IS DISTINCT FROM 'object'
                   OR v_geometry ->> 'type' IS DISTINCT FROM 'raster_mask' THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_mask_revision_error',
                        DETAIL = jsonb_build_object('status_code',409,'detail',jsonb_build_object('reason','snapshot_unavailable'))::text;
                END IF;
                v_rows := v_rows || jsonb_build_array(jsonb_build_object(
                    'annotation_id',v_annotation_id,'annotation_version',v_version,
                    'geometry',v_geometry
                ));
            ELSE
                UPDATE public.mask_annotation_revisions AS revision
                SET expires_at = GREATEST(revision.expires_at,(p_request ->> 'expires_at')::timestamptz)
                WHERE revision.task_id = v_task_id
                  AND revision.annotation_id = v_annotation_id
                  AND revision.annotation_version = v_version
                  AND encode(public.digest(convert_to(aap_labeling_v1._mask_revision_canonical_json(revision.geometry), 'UTF8'), 'sha256'), 'hex') = v_reference ->> 'geometry_digest';
                GET DIAGNOSTICS v_deleted = ROW_COUNT;
                v_count := v_count + v_deleted;
            END IF;
        END LOOP;
    END IF;
    v_reply := jsonb_build_object(
        'schema','aap-labeling-mask-revisions/v1','ok',true,
        'request',p_request,
        'data',jsonb_build_object(
            'geometries',v_rows,'affected',v_count
        )
    );
    RETURN v_reply;
END;
$function$;
