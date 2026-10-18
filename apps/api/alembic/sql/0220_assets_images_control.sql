-- Public image control, source and composition capabilities. Execution and
-- cleanup lease operations are installed from their separate 0220 artifact.

CREATE FUNCTION aap_assets_v1._image_item_source_snapshot(p_item public.dataset_items)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
        pg_catalog.jsonb_build_object(
            'dataset_item_id', (p_item).id,
            'created_at', (p_item).created_at AT TIME ZONE 'UTC',
            'dataset_id', (p_item).dataset_id,
            'file_path', (p_item).file_path,
            'file_type', (p_item).file_type,
            'file_size', (p_item).file_size,
            'content_hash', (p_item).content_hash,
            'source_generation', (p_item).media_source_generation
        )::text,
        'UTF8'
    )), 'hex')
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_item_source_snapshot(public.dataset_items)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._image_thumbnail_key_valid(p_key text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT COALESCE(
        p_key ~ '^image-assets/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g[1-9][0-9]*/a[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/thumbnail\.webp$'
        OR p_key ~ '^image-pyramids/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g[1-9][0-9]*/a[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/overview\.webp$',
        false
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_thumbnail_key_valid(text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._image_normalize_identity(p_identity jsonb)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_identity IS NULL OR p_identity = 'null'::jsonb
        THEN NULL ELSE pg_catalog.jsonb_build_object(
        'etag', p_identity -> 'etag',
        'byte_size', p_identity -> 'byte_size',
        'version_id', COALESCE(p_identity -> 'version_id', 'null'::jsonb)
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_normalize_identity(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._image_source_data_valid(p_source jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_owner_kind text;
    v_source_generation bigint;
    v_uuid text;
BEGIN
    IF pg_catalog.jsonb_typeof(p_source) IS DISTINCT FROM 'object'
       OR (p_source - ARRAY[
            'owner', 'task_id', 'project_id', 'dataset_id', 'item_id',
            'source_generation', 'source_snapshot', 'object_identity',
            'source_drifted', 'upload_ready', 'file_path', 'file_name',
            'file_type', 'bucket_kind', 'file_size', 'content_hash', 'width',
            'height', 'thumbnail_path', 'blurhash', 'thumbnail_source_generation',
            'thumbnail_error'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_source ?& ARRAY[
            'owner', 'task_id', 'project_id', 'dataset_id', 'item_id',
            'source_generation', 'source_snapshot', 'object_identity',
            'source_drifted', 'upload_ready', 'file_path', 'file_name',
            'file_type', 'bucket_kind', 'file_size', 'content_hash', 'width',
            'height', 'thumbnail_path', 'blurhash', 'thumbnail_source_generation',
            'thumbnail_error'
       ])
       OR pg_catalog.jsonb_typeof(p_source -> 'owner') IS DISTINCT FROM 'object'
       OR ((p_source -> 'owner') - ARRAY['kind', 'id']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_source -> 'owner' ?& ARRAY['kind', 'id'])
       OR pg_catalog.jsonb_typeof(p_source #> '{owner,kind}') IS DISTINCT FROM 'string'
       OR p_source #>> '{owner,kind}' NOT IN ('task', 'dataset_item')
       OR pg_catalog.jsonb_typeof(p_source #> '{owner,id}') IS DISTINCT FROM 'string'
       OR p_source #>> '{owner,id}' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_source -> 'source_generation') IS DISTINCT FROM 'number'
       OR p_source ->> 'source_generation' !~ '^[1-9][0-9]*$'
       OR pg_catalog.char_length(p_source ->> 'source_generation') > 19
       OR pg_catalog.jsonb_typeof(p_source -> 'source_snapshot') IS DISTINCT FROM 'string'
       OR p_source ->> 'source_snapshot' !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_source -> 'source_drifted') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(p_source -> 'upload_ready') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(p_source -> 'file_type') IS DISTINCT FROM 'string'
       OR p_source ->> 'file_type' IS DISTINCT FROM 'image'
       OR pg_catalog.jsonb_typeof(p_source -> 'bucket_kind') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_source -> 'file_path') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_source ->> 'file_path') NOT BETWEEN 1 AND 1024
       OR pg_catalog.jsonb_typeof(p_source -> 'file_name') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_source ->> 'file_name') NOT BETWEEN 1 AND 512
       OR (p_source ->> 'file_path') LIKE '/%'
       OR pg_catalog.strpos(p_source ->> 'file_path', pg_catalog.chr(92)) > 0
       OR pg_catalog.array_position(pg_catalog.string_to_array(p_source ->> 'file_path', '/'), '') IS NOT NULL
       OR EXISTS (
            SELECT 1 FROM pg_catalog.unnest(pg_catalog.string_to_array(p_source ->> 'file_path', '/')) AS part(value)
            WHERE part.value IN ('.', '..')
       ) THEN
        RETURN false;
    END IF;
    v_owner_kind := p_source #>> '{owner,kind}';
    v_source_generation := (p_source ->> 'source_generation')::bigint;
    v_uuid := p_source #>> '{owner,id}';
    IF (p_source -> 'task_id' <> 'null'::jsonb
        AND pg_catalog.jsonb_typeof(p_source -> 'task_id') IS DISTINCT FROM 'string')
       OR (p_source -> 'project_id' <> 'null'::jsonb
           AND pg_catalog.jsonb_typeof(p_source -> 'project_id') IS DISTINCT FROM 'string')
       OR (p_source -> 'dataset_id' <> 'null'::jsonb
           AND pg_catalog.jsonb_typeof(p_source -> 'dataset_id') IS DISTINCT FROM 'string')
       OR (p_source -> 'item_id' <> 'null'::jsonb
           AND pg_catalog.jsonb_typeof(p_source -> 'item_id') IS DISTINCT FROM 'string')
       OR (p_source -> 'file_size' <> 'null'::jsonb
           AND (pg_catalog.jsonb_typeof(p_source -> 'file_size') IS DISTINCT FROM 'number'
                OR p_source ->> 'file_size' !~ '^(0|[1-9][0-9]*)$'
                OR pg_catalog.char_length(p_source ->> 'file_size') > 19
                OR (p_source ->> 'file_size')::numeric > 9223372036854775807))
       OR (p_source -> 'content_hash' <> 'null'::jsonb
           AND (pg_catalog.jsonb_typeof(p_source -> 'content_hash') IS DISTINCT FROM 'string'
                OR pg_catalog.char_length(p_source ->> 'content_hash') > 255))
       OR (p_source -> 'width' <> 'null'::jsonb
           AND (pg_catalog.jsonb_typeof(p_source -> 'width') IS DISTINCT FROM 'number'
                OR p_source ->> 'width' !~ '^[1-9][0-9]*$'
                OR (p_source ->> 'width')::numeric > 2147483647))
       OR (p_source -> 'height' <> 'null'::jsonb
           AND (pg_catalog.jsonb_typeof(p_source -> 'height') IS DISTINCT FROM 'number'
                OR p_source ->> 'height' !~ '^[1-9][0-9]*$'
                OR (p_source ->> 'height')::numeric > 2147483647))
       OR (p_source -> 'object_identity' <> 'null'::jsonb
           AND NOT aap_assets_v1._media_object_identity_valid(p_source -> 'object_identity'))
       OR (p_source -> 'thumbnail_path' <> 'null'::jsonb
           AND (pg_catalog.jsonb_typeof(p_source -> 'thumbnail_path') IS DISTINCT FROM 'string'
                OR pg_catalog.char_length(p_source ->> 'thumbnail_path') NOT BETWEEN 1 AND 1024
                OR NOT aap_assets_v1._image_thumbnail_key_valid(p_source ->> 'thumbnail_path')))
       OR (p_source -> 'blurhash' <> 'null'::jsonb
           AND (pg_catalog.jsonb_typeof(p_source -> 'blurhash') IS DISTINCT FROM 'string'
                OR pg_catalog.char_length(p_source ->> 'blurhash') > 128))
       OR (p_source -> 'thumbnail_error' <> 'null'::jsonb
           AND (pg_catalog.jsonb_typeof(p_source -> 'thumbnail_error') IS DISTINCT FROM 'string'
                OR pg_catalog.char_length(p_source ->> 'thumbnail_error') > 2000))
       OR (p_source -> 'thumbnail_source_generation' <> 'null'::jsonb
           AND (pg_catalog.jsonb_typeof(p_source -> 'thumbnail_source_generation') IS DISTINCT FROM 'number'
                OR p_source ->> 'thumbnail_source_generation' <> p_source ->> 'source_generation'))
       OR ((p_source -> 'thumbnail_path' = 'null'::jsonb)
            <> (p_source -> 'thumbnail_source_generation' = 'null'::jsonb)) THEN
        RETURN false;
    END IF;
    IF v_owner_kind = 'task' THEN
        RETURN p_source ->> 'bucket_kind' = 'legacy'
            AND p_source ->> 'task_id' = v_uuid
            AND p_source -> 'dataset_id' = 'null'::jsonb
            AND p_source -> 'item_id' = 'null'::jsonb
            AND p_source -> 'file_size' = 'null'::jsonb
            AND p_source -> 'content_hash' = 'null'::jsonb
            AND ((p_source -> 'width' = 'null'::jsonb
                  AND p_source -> 'height' = 'null'::jsonb)
                 OR (p_source -> 'width' <> 'null'::jsonb
                     AND p_source -> 'height' <> 'null'::jsonb));
    END IF;
    RETURN p_source ->> 'bucket_kind' = 'datasets'
        AND p_source ->> 'item_id' = v_uuid
        AND p_source -> 'dataset_id' <> 'null'::jsonb;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_source_data_valid(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._image_current_error(
    p_owner_kind text, p_owner_id uuid, p_source_generation bigint, p_source_snapshot text
)
RETURNS text
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT COALESCE(job.error_detail, job.error_code)
    FROM public.asset_image_jobs AS job
    WHERE job.owner_kind = p_owner_kind
      AND job.owner_id = p_owner_id
      AND job.kind IN ('thumbnail', 'pyramid')
      AND job.status = 'failed'
      AND job.source_generation = p_source_generation
      AND job.source_snapshot = p_source_snapshot
    ORDER BY job.updated_at DESC, job.job_id DESC
    LIMIT 1
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_current_error(text, uuid, bigint, text)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._image_item_source_json(
    p_item public.dataset_items,
    p_task_id uuid DEFAULT NULL,
    p_project_id uuid DEFAULT NULL
)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH source AS (
        SELECT
            (p_item).id AS item_id,
            (p_item).dataset_id AS dataset_id,
            (p_item).created_at AS created_at,
            (p_item).file_path AS file_path,
            (p_item).file_name AS file_name,
            (p_item).file_type AS file_type,
            (p_item).file_size AS file_size,
            (p_item).content_hash AS content_hash,
            (p_item).width AS width,
            (p_item).height AS height,
            (p_item).thumbnail_path AS thumbnail_path,
            (p_item).blurhash AS blurhash,
            (p_item).media_source_generation AS source_generation,
            (p_item).media_materialized_generation AS materialized_generation,
            (p_item).media_object_identity AS object_identity,
            (p_item).media_source_drifted AS source_drifted,
            aap_assets_v1._image_item_source_snapshot(p_item) AS source_snapshot
    ), dimension_job AS MATERIALIZED (
        SELECT job.result
        FROM public.asset_image_jobs AS job
        CROSS JOIN source
        WHERE job.owner_kind = 'dataset_item'
          AND job.owner_id = source.item_id
          AND job.kind = 'dimensions'
          AND job.status = 'ready'
          AND job.source_generation = source.source_generation
          AND job.source_snapshot = source.source_snapshot
          AND job.spec ->> 'kind' = 'dimensions'
          AND NOT source.source_drifted
          AND source.object_identity IS NOT NULL
          AND aap_assets_v1._image_normalize_identity(job.source_facts -> 'object_identity')
                = aap_assets_v1._image_normalize_identity(source.object_identity)
          AND aap_assets_v1._image_normalize_identity(job.result -> 'source_identity')
                = aap_assets_v1._image_normalize_identity(source.object_identity)
          AND job.result ->> 'kind' = 'dimensions'
          AND CASE
                WHEN pg_catalog.jsonb_typeof(job.result -> 'width') = 'number'
                 AND COALESCE(job.result ->> 'width', '') ~ '^[1-9][0-9]*$'
                 AND pg_catalog.char_length(job.result ->> 'width') <= 10
                    THEN (job.result ->> 'width')::numeric BETWEEN 1 AND 2147483647
                ELSE false END
          AND CASE
                WHEN pg_catalog.jsonb_typeof(job.result -> 'height') = 'number'
                 AND COALESCE(job.result ->> 'height', '') ~ '^[1-9][0-9]*$'
                 AND pg_catalog.char_length(job.result ->> 'height') <= 10
                    THEN (job.result ->> 'height')::numeric BETWEEN 1 AND 2147483647
                ELSE false END
        ORDER BY job.updated_at DESC, job.job_id DESC
        LIMIT 1
    ), dimensions AS (
        SELECT (dimension_job.result ->> 'width')::integer AS width,
               (dimension_job.result ->> 'height')::integer AS height
        FROM dimension_job
    )
    SELECT pg_catalog.jsonb_build_object(
        'owner', pg_catalog.jsonb_build_object('kind', 'dataset_item', 'id', source.item_id),
        'task_id', p_task_id,
        'project_id', p_project_id,
        'dataset_id', source.dataset_id,
        'item_id', source.item_id,
        'source_generation', source.source_generation,
        'source_snapshot', source.source_snapshot,
        'object_identity', aap_assets_v1._image_normalize_identity(source.object_identity),
        'source_drifted', source.source_drifted,
        'upload_ready', true,
        'file_path', source.file_path,
        'file_name', source.file_name,
        'file_type', source.file_type,
        'bucket_kind', 'datasets',
        'file_size', source.file_size,
        'content_hash', source.content_hash,
        'width', COALESCE(CASE WHEN source.width BETWEEN 1 AND 2147483647
            THEN source.width END, dimensions.width),
        'height', COALESCE(CASE WHEN source.height BETWEEN 1 AND 2147483647
            THEN source.height END, dimensions.height),
        'thumbnail_path', CASE WHEN
            source.materialized_generation = source.source_generation
            AND source.object_identity IS NOT NULL
            AND NOT source.source_drifted
            AND aap_assets_v1._image_thumbnail_key_valid(source.thumbnail_path)
            THEN source.thumbnail_path END,
        'blurhash', CASE WHEN
            source.materialized_generation = source.source_generation
            AND source.object_identity IS NOT NULL
            AND NOT source.source_drifted
            AND aap_assets_v1._image_thumbnail_key_valid(source.thumbnail_path)
            THEN source.blurhash END,
        'thumbnail_source_generation', CASE WHEN
            source.materialized_generation = source.source_generation
            AND source.object_identity IS NOT NULL
            AND NOT source.source_drifted
            AND aap_assets_v1._image_thumbnail_key_valid(source.thumbnail_path)
            THEN source.source_generation END,
        'thumbnail_error', aap_assets_v1._image_current_error(
            'dataset_item', source.item_id, source.source_generation,
            source.source_snapshot
        )
    )
    FROM source LEFT JOIN dimensions ON true
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_item_source_json(
    public.dataset_items, uuid, uuid
) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._image_task_source_json(p_facts jsonb)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_task_id uuid;
    v_project_id uuid;
    v_dataset_item_id uuid;
    v_source_generation bigint;
    v_snapshot text;
    v_identity jsonb;
    v_drifted boolean;
    v_thumbnail text;
    v_thumbnail_generation bigint;
    v_width integer;
    v_height integer;
    v_created_at timestamptz;
BEGIN
    IF pg_catalog.jsonb_typeof(p_facts) IS DISTINCT FROM 'object'
       OR (p_facts - ARRAY[
            'task_id', 'project_id', 'dataset_item_id', 'file_type', 'file_path',
            'file_name', 'created_at', 'source_generation', 'source_snapshot',
            'object_identity', 'source_drifted', 'upload_ready', 'thumbnail_path',
            'blurhash', 'thumbnail_source_generation'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_facts ?& ARRAY[
            'task_id', 'project_id', 'dataset_item_id', 'file_type', 'file_path',
            'file_name', 'created_at', 'source_generation', 'source_snapshot',
            'object_identity', 'source_drifted', 'upload_ready', 'thumbnail_path',
            'blurhash', 'thumbnail_source_generation'
       ]) THEN
        RETURN NULL;
    END IF;
    IF pg_catalog.jsonb_typeof(p_facts -> 'task_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_facts -> 'file_type') IS DISTINCT FROM 'string'
       OR p_facts ->> 'file_type' IS DISTINCT FROM 'image'
       OR pg_catalog.jsonb_typeof(p_facts -> 'file_path') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_facts ->> 'file_path') NOT BETWEEN 1 AND 1024
       OR pg_catalog.jsonb_typeof(p_facts -> 'file_name') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_facts ->> 'file_name') NOT BETWEEN 1 AND 512
       OR pg_catalog.jsonb_typeof(p_facts -> 'created_at') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_facts -> 'source_generation') IS DISTINCT FROM 'number'
       OR p_facts ->> 'source_generation' !~ '^[1-9][0-9]*$'
       OR pg_catalog.char_length(p_facts ->> 'source_generation') > 19
       OR (p_facts ->> 'source_generation')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(p_facts -> 'source_snapshot') IS DISTINCT FROM 'string'
       OR p_facts ->> 'source_snapshot' !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_facts -> 'source_drifted') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(p_facts -> 'upload_ready') IS DISTINCT FROM 'boolean'
       OR (p_facts -> 'project_id' <> 'null'::jsonb
           AND pg_catalog.jsonb_typeof(p_facts -> 'project_id') IS DISTINCT FROM 'string')
       OR (p_facts -> 'dataset_item_id' <> 'null'::jsonb
           AND pg_catalog.jsonb_typeof(p_facts -> 'dataset_item_id') IS DISTINCT FROM 'string')
       OR (p_facts -> 'object_identity' <> 'null'::jsonb
           AND NOT aap_assets_v1._media_object_identity_valid(p_facts -> 'object_identity'))
       OR (p_facts -> 'thumbnail_path' <> 'null'::jsonb
           AND (pg_catalog.jsonb_typeof(p_facts -> 'thumbnail_path') IS DISTINCT FROM 'string'
                OR pg_catalog.char_length(p_facts ->> 'thumbnail_path') NOT BETWEEN 1 AND 1024))
       OR (p_facts -> 'blurhash' <> 'null'::jsonb
           AND (pg_catalog.jsonb_typeof(p_facts -> 'blurhash') IS DISTINCT FROM 'string'
                OR pg_catalog.char_length(p_facts ->> 'blurhash') > 128))
       OR (p_facts -> 'thumbnail_source_generation' <> 'null'::jsonb
           AND (pg_catalog.jsonb_typeof(p_facts -> 'thumbnail_source_generation') IS DISTINCT FROM 'number'
                OR p_facts ->> 'thumbnail_source_generation' !~ '^[1-9][0-9]*$'
                OR p_facts ->> 'thumbnail_source_generation' <> p_facts ->> 'source_generation')) THEN
        RETURN NULL;
    END IF;
    v_task_id := (p_facts ->> 'task_id')::uuid;
    v_project_id := CASE WHEN p_facts -> 'project_id' = 'null'::jsonb
        THEN NULL ELSE (p_facts ->> 'project_id')::uuid END;
    v_dataset_item_id := CASE WHEN p_facts -> 'dataset_item_id' = 'null'::jsonb
        THEN NULL ELSE (p_facts ->> 'dataset_item_id')::uuid END;
    v_created_at := (p_facts ->> 'created_at')::timestamptz;
    v_source_generation := (p_facts ->> 'source_generation')::bigint;
    v_snapshot := p_facts ->> 'source_snapshot';
    v_identity := aap_assets_v1._image_normalize_identity(p_facts -> 'object_identity');
    v_drifted := (p_facts ->> 'source_drifted')::boolean;
    v_thumbnail_generation := CASE
        WHEN p_facts -> 'thumbnail_source_generation' = 'null'::jsonb THEN NULL
        ELSE (p_facts ->> 'thumbnail_source_generation')::bigint END;
    v_thumbnail := CASE WHEN v_thumbnail_generation = v_source_generation
        AND v_identity IS NOT NULL AND NOT v_drifted
        AND (p_facts ->> 'upload_ready')::boolean
        AND aap_assets_v1._image_thumbnail_key_valid(p_facts ->> 'thumbnail_path')
        THEN p_facts ->> 'thumbnail_path' END;
    IF v_identity IS NOT NULL AND NOT v_drifted
       AND (p_facts ->> 'upload_ready')::boolean THEN
        SELECT candidate.width, candidate.height
        INTO v_width, v_height
        FROM (
            SELECT
                CASE
                    WHEN job.kind IN ('dimensions', 'thumbnail')
                     AND job.result ->> 'kind' = job.kind
                     AND pg_catalog.jsonb_typeof(job.result -> 'width') = 'number'
                     AND job.result ->> 'width' ~ '^[1-9][0-9]*$'
                     AND pg_catalog.char_length(job.result ->> 'width') <= 10
                     AND CASE WHEN job.result ->> 'width' ~ '^[1-9][0-9]*$'
                                   AND pg_catalog.char_length(job.result ->> 'width') <= 10
                         THEN (job.result ->> 'width')::numeric BETWEEN 1 AND 2147483647
                         ELSE false END
                        THEN (job.result ->> 'width')::integer
                    WHEN job.kind = 'pyramid'
                     AND job.result ->> 'kind' = 'pyramid'
                     AND pg_catalog.jsonb_typeof(job.result -> 'manifest') = 'object'
                     AND pg_catalog.jsonb_typeof(job.result #> '{manifest,width}') = 'number'
                     AND job.result #>> '{manifest,width}' ~ '^[1-9][0-9]*$'
                     AND pg_catalog.char_length(job.result #>> '{manifest,width}') <= 10
                     AND CASE WHEN job.result #>> '{manifest,width}' ~ '^[1-9][0-9]*$'
                                   AND pg_catalog.char_length(job.result #>> '{manifest,width}') <= 10
                         THEN (job.result #>> '{manifest,width}')::numeric BETWEEN 1 AND 2147483647
                         ELSE false END
                        THEN (job.result #>> '{manifest,width}')::integer
                END AS width,
                CASE
                    WHEN job.kind IN ('dimensions', 'thumbnail')
                     AND job.result ->> 'kind' = job.kind
                     AND pg_catalog.jsonb_typeof(job.result -> 'height') = 'number'
                     AND job.result ->> 'height' ~ '^[1-9][0-9]*$'
                     AND pg_catalog.char_length(job.result ->> 'height') <= 10
                     AND CASE WHEN job.result ->> 'height' ~ '^[1-9][0-9]*$'
                                   AND pg_catalog.char_length(job.result ->> 'height') <= 10
                         THEN (job.result ->> 'height')::numeric BETWEEN 1 AND 2147483647
                         ELSE false END
                        THEN (job.result ->> 'height')::integer
                    WHEN job.kind = 'pyramid'
                     AND job.result ->> 'kind' = 'pyramid'
                     AND pg_catalog.jsonb_typeof(job.result -> 'manifest') = 'object'
                     AND pg_catalog.jsonb_typeof(job.result #> '{manifest,height}') = 'number'
                     AND job.result #>> '{manifest,height}' ~ '^[1-9][0-9]*$'
                     AND pg_catalog.char_length(job.result #>> '{manifest,height}') <= 10
                     AND CASE WHEN job.result #>> '{manifest,height}' ~ '^[1-9][0-9]*$'
                                   AND pg_catalog.char_length(job.result #>> '{manifest,height}') <= 10
                         THEN (job.result #>> '{manifest,height}')::numeric BETWEEN 1 AND 2147483647
                         ELSE false END
                        THEN (job.result #>> '{manifest,height}')::integer
                END AS height,
                job.updated_at,
                job.job_id
            FROM public.asset_image_jobs AS job
            WHERE job.owner_kind = 'task'
              AND job.owner_id = v_task_id
              AND job.kind IN ('dimensions', 'thumbnail', 'pyramid')
              AND job.status = 'ready'
              AND job.source_generation = v_source_generation
              AND job.source_snapshot = v_snapshot
              AND aap_assets_v1._media_object_identity_valid(
                    job.source_facts -> 'object_identity'
                  )
              AND aap_assets_v1._image_normalize_identity(
                    job.source_facts -> 'object_identity'
                  ) = v_identity
              AND aap_assets_v1._media_object_identity_valid(
                    job.result -> 'source_identity'
                  )
              AND aap_assets_v1._image_normalize_identity(
                    job.result -> 'source_identity'
                  ) = v_identity
        ) AS candidate
        WHERE candidate.width IS NOT NULL AND candidate.height IS NOT NULL
        ORDER BY candidate.updated_at DESC, candidate.job_id DESC
        LIMIT 1;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'owner', pg_catalog.jsonb_build_object('kind', 'task', 'id', v_task_id),
        'task_id', v_task_id,
        'project_id', v_project_id,
        'dataset_id', NULL,
        'item_id', NULL,
        'source_generation', v_source_generation,
        'source_snapshot', v_snapshot,
        'object_identity', v_identity,
        'source_drifted', v_drifted,
        'upload_ready', (p_facts ->> 'upload_ready')::boolean,
        'file_path', p_facts ->> 'file_path',
        'file_name', p_facts ->> 'file_name',
        'file_type', 'image',
        'bucket_kind', 'legacy',
        'file_size', NULL,
        'content_hash', NULL,
        'width', v_width,
        'height', v_height,
        'thumbnail_path', v_thumbnail,
        'blurhash', CASE WHEN v_thumbnail IS NOT NULL THEN p_facts -> 'blurhash' END,
        'thumbnail_source_generation', CASE WHEN v_thumbnail IS NOT NULL
            THEN v_source_generation END,
        'thumbnail_error', aap_assets_v1._image_current_error(
            'task', v_task_id, v_source_generation, v_snapshot
        )
    );
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range
    OR datetime_field_overflow OR invalid_datetime_format THEN
    RETURN NULL;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_task_source_json(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._image_actor(p_actor_id uuid, p_lock boolean)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_data jsonb;
BEGIN
    IF p_actor_id IS NULL OR p_lock IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    IF p_lock THEN
        v_reply := aap_identity_v1.lock_active_actor(p_actor_id);
    ELSE
        SELECT pg_catalog.jsonb_build_object(
            'schema', 'aap-active-actor/v1', 'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'actor_id', facts.actor_id,
                'platform_role', facts.platform_role
            )
        ) INTO v_reply
        FROM aap_identity_v1.read_actor_facts(p_actor_id) AS facts
        WHERE facts.actor_id = p_actor_id AND facts.is_active IS TRUE;
        IF v_reply IS NULL THEN
            RETURN aap_assets_v1._image_reply(NULL, 'account_not_active');
        END IF;
    END IF;
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-active-actor/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_reply #>> '{error,code}'
            WHEN 'account_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'account_busy');
            WHEN 'account_not_active' THEN RETURN aap_assets_v1._image_reply(NULL, 'account_not_active');
            ELSE RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END CASE;
    END IF;
    v_data := v_reply -> 'data';
    IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
       OR (v_data - ARRAY['actor_id', 'platform_role']) IS DISTINCT FROM '{}'::jsonb
       OR v_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR pg_catalog.jsonb_typeof(v_data -> 'platform_role') IS DISTINCT FROM 'string'
       OR v_data ->> 'platform_role' NOT IN (
            'super_admin', 'project_admin', 'employee', 'viewer'
       ) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_account_role');
    END IF;
    RETURN aap_assets_v1._image_reply(v_data, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_actor(uuid, boolean) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._image_manager_item(
    p_actor_id uuid, p_dataset_item_id uuid, p_lock boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_data jsonb;
BEGIN
    v_reply := aap_assets_v1._media_manager_item(
        p_actor_id, p_dataset_item_id, p_lock
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-media/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_reply #>> '{error,code}'
            WHEN 'account_not_active' THEN RETURN aap_assets_v1._image_reply(NULL, 'account_not_active');
            WHEN 'account_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'account_busy');
            WHEN 'permission_denied' THEN RETURN aap_assets_v1._image_reply(NULL, 'permission_denied');
            WHEN 'source_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
            WHEN 'task_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
            WHEN 'invalid_account_role' THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_account_role');
            ELSE RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END CASE;
    END IF;
    v_data := v_reply -> 'data';
    IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
       OR (v_data - ARRAY['actor_id', 'dataset_item_id', 'task_id', 'project_id']) IS DISTINCT FROM '{}'::jsonb
       OR v_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR v_data ->> 'dataset_item_id' IS DISTINCT FROM p_dataset_item_id::text THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    RETURN aap_assets_v1._image_reply(v_data, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_manager_item(uuid, uuid, boolean)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._image_labeling_task(
    p_task_id uuid, p_actor_id uuid, p_lock_source boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_data jsonb;
    v_project_id uuid;
    v_workspace jsonb;
    v_workspace_data jsonb;
BEGIN
    v_reply := aap_labeling_v1.read_image_task_source(
        p_task_id, p_actor_id,
        CASE WHEN p_actor_id IS NOT NULL AND p_lock_source THEN false ELSE p_lock_source END
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-image-source/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_reply #>> '{error,code}'
            WHEN 'account_not_active' THEN RETURN aap_assets_v1._image_reply(NULL, 'account_not_active');
            WHEN 'account_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'account_busy');
            WHEN 'task_not_visible' THEN RETURN aap_assets_v1._image_reply(NULL, 'task_not_visible');
            WHEN 'source_missing' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_missing');
            WHEN 'source_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
            WHEN 'source_changed' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
            WHEN 'source_not_ready' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_not_ready');
            WHEN 'not_image' THEN RETURN aap_assets_v1._image_reply(NULL, 'not_image');
            WHEN 'invalid_account_role' THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_account_role');
            ELSE RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END CASE;
    END IF;
    v_data := v_reply -> 'data';
    IF aap_assets_v1._image_task_source_json(v_data) IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    IF p_actor_id IS NOT NULL AND p_lock_source THEN
        IF pg_catalog.jsonb_typeof(v_data -> 'project_id') IS DISTINCT FROM 'string' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        BEGIN
            v_project_id := (v_data ->> 'project_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END;
        v_workspace := aap_workspace_v1.lock_media_project_access(
            p_actor_id, v_project_id
        );
        IF v_workspace IS NULL
           OR v_workspace ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
           OR pg_catalog.jsonb_typeof(v_workspace -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_workspace -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            CASE v_workspace #>> '{error,code}'
                WHEN 'account_not_active' THEN RETURN aap_assets_v1._image_reply(NULL, 'account_not_active');
                WHEN 'account_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'account_busy');
                WHEN 'task_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
                WHEN 'project_missing' THEN RETURN aap_assets_v1._image_reply(NULL, 'task_not_visible');
                WHEN 'permission_denied' THEN RETURN aap_assets_v1._image_reply(NULL, 'task_not_visible');
                WHEN 'invalid_account_role' THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_account_role');
                ELSE RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END CASE;
        END IF;
        v_workspace_data := v_workspace -> 'data';
        IF pg_catalog.jsonb_typeof(v_workspace_data) IS DISTINCT FROM 'object'
           OR v_workspace_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
           OR v_workspace_data ->> 'project_id' IS DISTINCT FROM v_project_id::text
           OR pg_catalog.jsonb_typeof(v_workspace_data -> 'platform_role') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_workspace_data -> 'is_manager') IS DISTINCT FROM 'boolean'
           OR (v_workspace_data -> 'project_role' <> 'null'::jsonb
               AND pg_catalog.jsonb_typeof(v_workspace_data -> 'project_role') IS DISTINCT FROM 'string') THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        v_reply := aap_labeling_v1.read_image_task_source(p_task_id, p_actor_id, true);
        IF v_reply IS NULL
           OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-image-source/v1'
           OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            CASE v_reply #>> '{error,code}'
                WHEN 'account_not_active' THEN RETURN aap_assets_v1._image_reply(NULL, 'account_not_active');
                WHEN 'account_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'account_busy');
                WHEN 'task_not_visible' THEN RETURN aap_assets_v1._image_reply(NULL, 'task_not_visible');
                WHEN 'source_missing' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_missing');
                WHEN 'source_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
                WHEN 'source_changed' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
                WHEN 'source_not_ready' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_not_ready');
                WHEN 'not_image' THEN RETURN aap_assets_v1._image_reply(NULL, 'not_image');
                WHEN 'invalid_account_role' THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_account_role');
                ELSE RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END CASE;
        END IF;
        v_data := v_reply -> 'data';
        IF v_data ->> 'project_id' IS DISTINCT FROM v_project_id::text
           OR aap_assets_v1._image_task_source_json(v_data) IS NULL THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
        END IF;
    END IF;
    RETURN aap_assets_v1._image_reply(v_data, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_labeling_task(uuid, uuid, boolean)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._image_owner_source(
    p_actor_id uuid,
    p_task_id uuid,
    p_dataset_item_id uuid,
    p_lock_source boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_task_reply jsonb;
    v_task_data jsonb;
    v_item public.dataset_items%ROWTYPE;
    v_source jsonb;
BEGIN
    IF ((p_task_id IS NULL) = (p_dataset_item_id IS NULL))
       OR p_lock_source IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    IF p_actor_id IS NOT NULL THEN
        v_actor := aap_assets_v1._image_actor(p_actor_id, p_lock_source);
        IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_actor; END IF;
    END IF;
    IF p_task_id IS NOT NULL THEN
        v_task_reply := aap_assets_v1._image_labeling_task(
            p_task_id, p_actor_id, p_lock_source
        );
        IF v_task_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN v_task_reply;
        END IF;
        v_task_data := v_task_reply -> 'data';
        IF v_task_data -> 'dataset_item_id' = 'null'::jsonb THEN
            v_source := aap_assets_v1._image_task_source_json(v_task_data);
            IF v_source IS NULL THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_response'); END IF;
            RETURN aap_assets_v1._image_reply(v_source, NULL);
        END IF;
        BEGIN
            IF p_lock_source THEN
                SELECT item.* INTO v_item
                FROM public.dataset_items AS item
                WHERE item.id = (v_task_data ->> 'dataset_item_id')::uuid
                FOR UPDATE NOWAIT;
            ELSE
                SELECT item.* INTO v_item
                FROM public.dataset_items AS item
                WHERE item.id = (v_task_data ->> 'dataset_item_id')::uuid;
            END IF;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
        WHEN invalid_text_representation THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END;
        IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'source_missing'); END IF;
        IF v_item.file_type IS DISTINCT FROM 'image' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'not_image');
        END IF;
        IF v_item.media_source_generation IS NULL OR v_item.media_source_generation <= 0
           OR v_item.media_source_drifted IS NULL
           OR (v_item.media_object_identity IS NOT NULL
               AND NOT aap_assets_v1._media_object_identity_valid(v_item.media_object_identity)) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        v_source := aap_assets_v1._image_item_source_json(
            v_item, (v_task_data ->> 'task_id')::uuid,
            CASE WHEN v_task_data -> 'project_id' = 'null'::jsonb
                THEN NULL ELSE (v_task_data ->> 'project_id')::uuid END
        );
        IF pg_catalog.jsonb_typeof(v_source) IS DISTINCT FROM 'object'
           OR v_source ->> 'owner' IS NULL
           OR v_source #>> '{owner,kind}' IS DISTINCT FROM 'dataset_item'
           OR v_source #>> '{owner,id}' IS DISTINCT FROM v_item.id::text
           OR v_source ->> 'task_id' IS DISTINCT FROM p_task_id::text
           OR v_source ->> 'file_type' IS DISTINCT FROM 'image' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        RETURN aap_assets_v1._image_reply(v_source, NULL);
    END IF;
    BEGIN
        IF p_lock_source THEN
            SELECT item.* INTO v_item FROM public.dataset_items AS item
            WHERE item.id = p_dataset_item_id FOR UPDATE NOWAIT;
        ELSE
            SELECT item.* INTO v_item FROM public.dataset_items AS item
            WHERE item.id = p_dataset_item_id;
        END IF;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
    END;
    IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'source_missing'); END IF;
    IF v_item.file_type IS DISTINCT FROM 'image' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'not_image');
    END IF;
    IF v_item.media_source_generation IS NULL OR v_item.media_source_generation <= 0
       OR v_item.media_source_drifted IS NULL
       OR (v_item.media_object_identity IS NOT NULL
           AND NOT aap_assets_v1._media_object_identity_valid(v_item.media_object_identity)) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    v_source := aap_assets_v1._image_item_source_json(v_item);
    RETURN aap_assets_v1._image_reply(v_source, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_owner_source(uuid, uuid, uuid, boolean)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._image_resolve_owner(
    p_owner_kind text, p_owner_id uuid, p_lock_source boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_source jsonb;
BEGIN
    IF p_owner_kind IS NULL OR p_owner_kind NOT IN ('dataset_item', 'task')
       OR p_owner_id IS NULL OR p_lock_source IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_reply := aap_assets_v1._image_owner_source(
        NULL,
        CASE WHEN p_owner_kind = 'task' THEN p_owner_id END,
        CASE WHEN p_owner_kind = 'dataset_item' THEN p_owner_id END,
        p_lock_source
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_source := v_reply -> 'data';
    IF v_source #>> '{owner,kind}' IS DISTINCT FROM p_owner_kind
       OR v_source #>> '{owner,id}' IS DISTINCT FROM p_owner_id::text THEN
        RETURN aap_assets_v1._image_reply(NULL, 'owner_changed');
    END IF;
    RETURN v_reply;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_resolve_owner(text, uuid, boolean)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._image_observe_owner(
    p_owner_kind text, p_owner_id uuid, p_expected_snapshot text, p_identity jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_source jsonb;
    v_identity jsonb;
    v_current jsonb;
    v_item public.dataset_items%ROWTYPE;
    v_snapshot text;
    v_drift boolean;
BEGIN
    IF p_owner_kind IS NULL OR p_owner_kind NOT IN ('dataset_item', 'task')
       OR p_owner_id IS NULL OR p_expected_snapshot IS NULL
       OR p_expected_snapshot !~ '^[0-9a-f]{64}$'
       OR NOT aap_assets_v1._media_object_identity_valid(p_identity) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_identity := aap_assets_v1._image_normalize_identity(p_identity);
    v_reply := aap_assets_v1._image_resolve_owner(p_owner_kind, p_owner_id, false);
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_source := v_reply -> 'data';
    IF v_source ->> 'source_snapshot' IS DISTINCT FROM p_expected_snapshot THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    IF v_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_not_ready');
    END IF;
    IF v_source -> 'source_drifted' = 'true'::jsonb THEN RETURN v_reply; END IF;
    v_current := v_source -> 'object_identity';
    v_drift := (v_source -> 'file_size' <> 'null'::jsonb
            AND v_source ->> 'file_size' <> v_identity ->> 'byte_size')
        OR (v_current <> 'null'::jsonb
            AND aap_assets_v1._image_normalize_identity(v_current) IS DISTINCT FROM v_identity);
    IF NOT v_drift AND v_current <> 'null'::jsonb THEN
        RETURN v_reply;
    END IF;
    IF p_owner_kind = 'task' THEN
        v_reply := aap_labeling_v1.observe_image_task_source(
            p_owner_id, p_expected_snapshot, v_identity
        );
        IF v_reply IS NULL
           OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-image-source/v1'
           OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            CASE v_reply #>> '{error,code}'
                WHEN 'source_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
                WHEN 'source_changed' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
                WHEN 'source_missing' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_missing');
                WHEN 'source_not_ready' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_not_ready');
                WHEN 'invalid_request' THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
                ELSE RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END CASE;
        END IF;
        v_reply := aap_assets_v1._image_resolve_owner('task', p_owner_id, false);
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR NOT aap_assets_v1._image_source_data_valid(v_reply -> 'data') THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Task source after observation';
        END IF;
        RETURN v_reply;
    END IF;
    BEGIN
        SELECT item.* INTO v_item
        FROM public.dataset_items AS item
        WHERE item.id = p_owner_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
    END;
    IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'source_missing'); END IF;
    v_snapshot := aap_assets_v1._image_item_source_snapshot(v_item);
    IF v_snapshot IS DISTINCT FROM p_expected_snapshot THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    IF v_item.media_source_drifted THEN
        RETURN aap_assets_v1._image_resolve_owner('dataset_item', p_owner_id, false);
    END IF;
    v_current := aap_assets_v1._image_normalize_identity(v_item.media_object_identity);
    v_drift := (v_item.file_size IS NOT NULL
            AND v_item.file_size::text <> v_identity ->> 'byte_size')
        OR (v_current IS NOT NULL AND v_current IS DISTINCT FROM v_identity);
    IF v_drift THEN
        IF v_item.media_source_generation = 9223372036854775807 THEN
            RETURN aap_assets_v1._image_reply(NULL, 'resource_budget_exceeded');
        END IF;
        UPDATE public.dataset_items AS item
        SET media_source_generation = v_item.media_source_generation + 1,
            media_object_identity = v_identity,
            media_source_drifted = true,
            updated_at = pg_catalog.clock_timestamp()
        WHERE item.id = p_owner_id;
    ELSIF v_current IS NULL THEN
        UPDATE public.dataset_items AS item
        SET media_object_identity = v_identity,
            updated_at = pg_catalog.clock_timestamp()
        WHERE item.id = p_owner_id;
    END IF;
    v_reply := aap_assets_v1._image_resolve_owner('dataset_item', p_owner_id, false);
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR NOT aap_assets_v1._image_source_data_valid(v_reply -> 'data') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid DatasetItem source after observation';
    END IF;
    RETURN v_reply;
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_observe_owner(text, uuid, text, jsonb)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._image_pyramid_projection(
    p_source jsonb, p_generation integer, p_profile_version text
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_owner_kind text;
    v_owner_id uuid;
    v_source_generation bigint;
    v_source_snapshot text;
    v_source_drifted boolean;
    v_upload_ready boolean;
    v_profile_version text;
    v_registered_width integer;
    v_registered_height integer;
    v_width integer;
    v_height integer;
    v_asset public.image_pyramid_assets%ROWTYPE;
    v_generation public.image_pyramid_generations%ROWTYPE;
    v_building public.image_pyramid_generations%ROWTYPE;
    v_dimension_job public.asset_image_jobs%ROWTYPE;
    v_policy jsonb;
    v_status text;
    v_building_status text;
    v_required boolean := false;
    v_retryable boolean := false;
    v_retry_after bigint;
    v_error_code text;
    v_current boolean := false;
    v_manifest jsonb;
    v_manifest_key text;
    v_manifest_digest text;
    v_overview_key text;
    v_overview_digest text;
    v_has_generation boolean := false;
BEGIN
    v_profile_version := COALESCE(p_profile_version, 'pyramid-v1');
    IF pg_catalog.jsonb_typeof(p_source) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_source -> 'owner') IS DISTINCT FROM 'object'
       OR p_source #>> '{owner,kind}' NOT IN ('dataset_item', 'task')
       OR p_source #>> '{owner,id}' IS NULL
       OR p_source ->> 'file_type' IS DISTINCT FROM 'image'
       OR COALESCE(p_source ->> 'source_snapshot', '') !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_source -> 'source_generation') IS DISTINCT FROM 'number'
       OR p_source ->> 'source_generation' !~ '^[1-9][0-9]*$'
       OR pg_catalog.jsonb_typeof(p_source -> 'source_drifted') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(p_source -> 'upload_ready') IS DISTINCT FROM 'boolean'
       OR pg_catalog.char_length(v_profile_version) NOT BETWEEN 1 AND 64 THEN
        RETURN NULL;
    END IF;
    BEGIN
        v_owner_kind := p_source #>> '{owner,kind}';
        v_owner_id := (p_source #>> '{owner,id}')::uuid;
        v_source_generation := (p_source ->> 'source_generation')::bigint;
        v_source_snapshot := p_source ->> 'source_snapshot';
        v_source_drifted := (p_source ->> 'source_drifted')::boolean;
        v_upload_ready := (p_source ->> 'upload_ready')::boolean;
        v_registered_width := CASE WHEN p_source -> 'width' = 'null'::jsonb THEN NULL
            ELSE (p_source ->> 'width')::integer END;
        v_registered_height := CASE WHEN p_source -> 'height' = 'null'::jsonb THEN NULL
            ELSE (p_source ->> 'height')::integer END;
        v_width := v_registered_width;
        v_height := v_registered_height;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN NULL;
    END;
    v_policy := aap_assets_v1._image_policy('{}'::jsonb);
    IF v_owner_kind = 'dataset_item' THEN
        SELECT asset.* INTO v_asset
        FROM public.image_pyramid_assets AS asset
        WHERE asset.dataset_item_id = v_owner_id
          AND asset.profile_version = v_profile_version
        ORDER BY asset.updated_at DESC, asset.id DESC
        LIMIT 1;
    ELSE
        SELECT asset.* INTO v_asset
        FROM public.image_pyramid_assets AS asset
        WHERE asset.task_id = v_owner_id
          AND asset.profile_version = v_profile_version
        ORDER BY asset.updated_at DESC, asset.id DESC
        LIMIT 1;
    END IF;
    IF NOT FOUND THEN
        v_status := CASE
            WHEN v_width IS NOT NULL AND v_height IS NOT NULL
             AND v_width::numeric * v_height::numeric >= (v_policy ->> 'optional_pixels')::numeric
                THEN 'missing'
            ELSE 'not_available'
        END;
        v_retryable := v_status = 'missing';
        v_required := v_width IS NOT NULL AND v_height IS NOT NULL
            AND v_width::numeric * v_height::numeric >= (v_policy ->> 'required_pixels')::numeric;
        IF (v_width IS NULL OR v_height IS NULL)
           AND NOT v_source_drifted AND v_upload_ready
           AND p_source -> 'object_identity' <> 'null'::jsonb THEN
            SELECT job.* INTO v_dimension_job
            FROM public.asset_image_jobs AS job
            WHERE job.owner_kind = v_owner_kind
              AND job.owner_id = v_owner_id
              AND job.kind = 'dimensions'
              AND job.slot_key IN ('dimensions', 'dimensions:preview')
              AND job.status IN ('pending', 'running', 'failed')
              AND job.source_generation = v_source_generation
              AND job.source_snapshot = v_source_snapshot
              AND job.source_facts #>> '{owner,kind}' = v_owner_kind
              AND job.source_facts #>> '{owner,id}' = v_owner_id::text
              AND job.spec ->> 'kind' = 'dimensions'
              AND job.spec ->> 'expected_source_snapshot' = v_source_snapshot
              AND job.source_facts ->> 'source_generation' = v_source_generation::text
              AND job.source_facts ->> 'source_snapshot' = v_source_snapshot
              AND job.source_facts -> 'source_drifted' = 'false'::jsonb
              AND job.source_facts -> 'upload_ready' = 'true'::jsonb
              AND aap_assets_v1._media_object_identity_valid(
                    job.source_facts -> 'object_identity'
              )
              AND aap_assets_v1._image_normalize_identity(
                    job.source_facts -> 'object_identity'
              ) = aap_assets_v1._image_normalize_identity(
                    p_source -> 'object_identity'
              )
            ORDER BY job.updated_at DESC, job.job_id DESC
            LIMIT 1;
            IF FOUND THEN
                IF v_dimension_job.status = 'pending' THEN
                    v_status := 'pending';
                    v_building_status := 'pending';
                    v_retry_after := 2000;
                ELSIF v_dimension_job.status = 'running' THEN
                    v_status := 'building';
                    v_building_status := 'building';
                    v_retry_after := 2000;
                ELSE
                    v_status := 'failed';
                    v_error_code := v_dimension_job.error_code;
                END IF;
            END IF;
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'asset_id', NULL, 'status', v_status, 'required', v_required,
            'profile_version', v_profile_version,
            'generation', NULL, 'building_generation', NULL,
            'building_status', v_building_status,
            'source_generation', NULL, 'source_snapshot', NULL,
            'width', v_width, 'height', v_height, 'tile_size', 512,
            'format', NULL, 'retryable', v_retryable, 'retry_after_ms', v_retry_after,
            'error_code', v_error_code, 'manifest', NULL, 'manifest_key', NULL,
            'manifest_digest', NULL, 'overview_key', NULL, 'overview_digest', NULL,
            'tile_count', 0, 'retained_bytes', 0
        );
    END IF;
    IF p_generation IS NOT NULL THEN
        IF p_generation < 1 THEN RETURN NULL; END IF;
        SELECT generation.* INTO v_generation
        FROM public.image_pyramid_generations AS generation
        WHERE generation.asset_id = v_asset.id
          AND generation.generation = p_generation;
        IF NOT FOUND THEN RETURN NULL; END IF;
    ELSIF v_asset.active_generation IS NOT NULL THEN
        SELECT generation.* INTO v_generation
        FROM public.image_pyramid_generations AS generation
        WHERE generation.asset_id = v_asset.id
          AND generation.generation = v_asset.active_generation;
    ELSIF v_asset.building_generation IS NOT NULL THEN
        SELECT generation.* INTO v_generation
        FROM public.image_pyramid_generations AS generation
        WHERE generation.asset_id = v_asset.id
          AND generation.generation = v_asset.building_generation;
    ELSE
        SELECT generation.* INTO v_generation
        FROM public.image_pyramid_generations AS generation
        WHERE generation.asset_id = v_asset.id
        ORDER BY generation.generation DESC
        LIMIT 1;
    END IF;
    v_has_generation := FOUND;
    IF v_asset.building_generation IS NOT NULL THEN
        SELECT generation.* INTO v_building
        FROM public.image_pyramid_generations AS generation
        WHERE generation.asset_id = v_asset.id
          AND generation.generation = v_asset.building_generation;
    END IF;
    IF NOT v_has_generation OR v_generation.generation IS NULL THEN RETURN NULL; END IF;
    IF v_generation.policy IS NOT NULL THEN
        v_policy := aap_assets_v1._image_policy(v_generation.policy);
        IF v_policy IS NULL THEN RETURN NULL; END IF;
    END IF;
    v_width := COALESCE(v_generation.width, v_width);
    v_height := COALESCE(v_generation.height, v_height);
    v_required := v_width IS NOT NULL AND v_height IS NOT NULL
        AND v_width::numeric * v_height::numeric >= (v_policy ->> 'required_pixels')::numeric;
    v_status := v_generation.status;
    v_current := COALESCE(v_generation.source_generation = v_source_generation
        AND v_generation.source_snapshot = v_source_snapshot
        AND v_generation.source_identity = aap_assets_v1._image_exec_legacy_identity(
            p_source -> 'object_identity'
        )
        AND (v_registered_width IS NULL OR v_generation.width = v_registered_width)
        AND (v_registered_height IS NULL OR v_generation.height = v_registered_height)
        AND NOT v_source_drifted AND v_upload_ready
        AND p_source -> 'object_identity' <> 'null'::jsonb, false);
    IF v_status = 'ready' AND NOT v_current THEN
        v_status := 'stale';
    ELSIF v_status = 'ready' AND (
        pg_catalog.jsonb_typeof(v_generation.manifest) IS DISTINCT FROM 'object'
        OR v_generation.manifest_key IS NULL
        OR v_generation.manifest_digest IS NULL
        OR v_generation.manifest_digest !~ '^sha256:[0-9a-f]{64}$'
        OR v_generation.overview_key IS NULL
        OR v_generation.overview_digest IS NULL
        OR v_generation.overview_digest !~ '^sha256:[0-9a-f]{64}$'
        OR (v_generation.manifest ->> 'generation') IS DISTINCT FROM v_generation.generation::text
    ) THEN
        v_status := 'inconsistent';
    END IF;
    v_retryable := v_status IN ('missing', 'failed', 'stale', 'inconsistent');
    v_error_code := CASE WHEN v_status = 'stale' THEN 'source_changed'
        WHEN v_status = 'inconsistent' AND v_generation.error_code IS NULL THEN 'inconsistent_ready'
        ELSE v_generation.error_code END;
    IF v_status IN ('pending', 'building') THEN
        v_retry_after := 2000;
    ELSIF v_status = 'failed' THEN
        v_retry_after := GREATEST(0::numeric,
            (v_policy ->> 'retry_cooldown_seconds')::numeric * 1000
            - GREATEST(0, EXTRACT(EPOCH FROM (pg_catalog.clock_timestamp() - v_generation.updated_at)) * 1000)
        )::bigint;
    END IF;
    IF v_status = 'ready' THEN
        v_manifest := v_generation.manifest;
        v_manifest_key := v_generation.manifest_key;
        v_manifest_digest := v_generation.manifest_digest;
        v_overview_key := v_generation.overview_key;
        v_overview_digest := v_generation.overview_digest;
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'asset_id', v_asset.id,
        'status', v_status,
        'required', v_required,
        'profile_version', v_asset.profile_version,
        'generation', v_generation.generation,
        'building_generation', v_asset.building_generation,
        'building_status', CASE WHEN v_building.status IN ('pending', 'building')
            THEN v_building.status END,
        'source_generation', v_generation.source_generation,
        'source_snapshot', v_generation.source_snapshot,
        'width', v_generation.width,
        'height', v_generation.height,
        'tile_size', 512,
        'format', CASE WHEN v_status = 'ready' THEN v_generation.format END,
        'retryable', v_retryable,
        'retry_after_ms', v_retry_after,
        'error_code', v_error_code,
        'manifest', v_manifest,
        'manifest_key', v_manifest_key,
        'manifest_digest', v_manifest_digest,
        'overview_key', v_overview_key,
        'overview_digest', v_overview_digest,
        'tile_count', COALESCE(v_generation.tile_count, 0),
        'retained_bytes', COALESCE(v_generation.retained_bytes, 0)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_pyramid_projection(jsonb, integer, text)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._image_pyramid_summary(p_projection jsonb)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'status', p_projection -> 'status',
        'required', p_projection -> 'required',
        'profile_version', p_projection -> 'profile_version',
        'generation', p_projection -> 'generation',
        'building_generation', p_projection -> 'building_generation',
        'building_status', p_projection -> 'building_status',
        'width', p_projection -> 'width',
        'height', p_projection -> 'height',
        'tile_size', 512,
        'format', p_projection -> 'format',
        'retryable', p_projection -> 'retryable',
        'retry_after_ms', p_projection -> 'retry_after_ms',
        'error_code', p_projection -> 'error_code'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_pyramid_summary(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._image_progress_count(p_value jsonb)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF pg_catalog.jsonb_typeof(p_value) IS DISTINCT FROM 'number'
       OR p_value #>> '{}' !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.char_length(p_value #>> '{}') > 19 THEN
        RETURN NULL;
    END IF;
    IF (p_value #>> '{}')::numeric > 9223372036854775807 THEN RETURN NULL; END IF;
    RETURN p_value;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN NULL;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_progress_count(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._image_public_progress(p_progress jsonb)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_strip_nulls(pg_catalog.jsonb_build_object(
        'scheduled', aap_assets_v1._image_progress_count(p_progress -> 'scheduled'),
        'skipped', aap_assets_v1._image_progress_count(p_progress -> 'skipped'),
        'processed', aap_assets_v1._image_progress_count(p_progress -> 'processed'),
        'total', aap_assets_v1._image_progress_count(p_progress -> 'total'),
        'complete', CASE WHEN pg_catalog.jsonb_typeof(p_progress -> 'complete') = 'boolean'
            THEN p_progress -> 'complete' END,
        'next_owner_kind', CASE WHEN p_progress ->> 'next_owner_kind' IN ('dataset_item', 'task')
            THEN p_progress -> 'next_owner_kind' END,
        'next_owner_id', CASE WHEN p_progress ->> 'next_owner_id'
            ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN p_progress -> 'next_owner_id' END,
        'cursor_owner_kind', CASE WHEN p_progress ->> 'cursor_owner_kind' IN ('dataset_item', 'task')
            THEN p_progress -> 'cursor_owner_kind' END,
        'cursor_owner_id', CASE WHEN p_progress ->> 'cursor_owner_id'
            ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN p_progress -> 'cursor_owner_id' END
    ))
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_public_progress(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._image_work_status(
    p_job public.asset_image_jobs, p_public boolean
)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'work', aap_assets_v1._image_work_reference(p_job),
        'spec', (p_job).spec,
        'progress', CASE WHEN p_public
            THEN aap_assets_v1._image_public_progress((p_job).progress)
                || CASE WHEN (p_job).kind = 'image_backfill' THEN
                    pg_catalog.jsonb_strip_nulls(pg_catalog.jsonb_build_object(
                        'cursor_owner_kind', (p_job).backfill_cursor_kind,
                        'cursor_owner_id', (p_job).backfill_cursor_id
                    ))
                   ELSE '{}'::jsonb END
            ELSE (p_job).progress END,
        'error_code', (p_job).error_code,
        'error_detail', (p_job).error_detail,
        'lease_expires_at', (p_job).lease_expires_at
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_work_status(public.asset_image_jobs, boolean)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._image_enqueue(
    p_operation_id uuid,
    p_actor_id uuid,
    p_source jsonb,
    p_spec jsonb,
    p_force boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_spec jsonb;
    v_policy jsonb;
    v_kind text;
    v_owner_kind text;
    v_owner_id uuid;
    v_source_generation bigint;
    v_source_snapshot text;
    v_slot text;
    v_job public.asset_image_jobs%ROWTYPE;
    v_asset public.image_pyramid_assets%ROWTYPE;
    v_old_generation public.image_pyramid_generations%ROWTYPE;
    v_pyramid_generation integer;
    v_job_generation bigint;
    v_job_id uuid;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_same boolean := false;
    v_reuse boolean := false;
    v_attempt_prefix text;
    v_data jsonb;
    v_retained_publication jsonb;
    v_existing boolean := false;
BEGIN
    IF p_force IS NULL THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    v_spec := aap_assets_v1._image_spec(p_spec);
    IF v_spec IS NULL THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    v_kind := v_spec ->> 'kind';
    v_policy := v_spec -> 'policy';
    IF v_kind = 'image_backfill' THEN
        IF p_source IS NOT NULL OR p_actor_id IS NOT NULL THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        IF v_spec #> '{selection,accepted_before}' = 'null'::jsonb THEN
            v_spec := pg_catalog.jsonb_set(
                v_spec, '{selection,accepted_before}', pg_catalog.to_jsonb(v_now), true
            );
        END IF;
        BEGIN
            v_job_id := pg_catalog.gen_random_uuid();
            INSERT INTO public.asset_image_jobs(
                job_id, kind, owner_kind, owner_id, slot_key, generation, spec,
                status, source_generation, source_snapshot, source_facts, actor_id,
                progress, attempt_count, accepted_at, updated_at, backfill_cutoff
            ) VALUES (
                v_job_id, 'image_backfill', NULL, NULL,
                'backfill:' || COALESCE(p_operation_id::text, v_job_id::text), 1,
                v_spec, 'pending', 0, NULL, NULL, NULL, '{}'::jsonb, 0,
                v_now, v_now, COALESCE((v_spec #>> '{selection,accepted_before}')::timestamptz, v_now)
            ) RETURNING * INTO v_job;
            v_data := pg_catalog.jsonb_build_object(
                'operation_id', p_operation_id,
                'source', NULL,
                'work', aap_assets_v1._image_work_reference(v_job),
                'spec', v_spec
            );
            RETURN aap_assets_v1._image_reply(v_data, NULL);
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        WHEN unique_violation THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        WHEN OTHERS THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END;
    END IF;
    IF p_source IS NULL OR NOT aap_assets_v1._image_source_data_valid(p_source) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    IF p_source -> 'source_drifted' = 'true'::jsonb THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    IF p_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_not_ready');
    END IF;
    IF p_source -> 'object_identity' = 'null'::jsonb THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_not_ready');
    END IF;
    v_owner_kind := p_source #>> '{owner,kind}';
    BEGIN
        v_owner_id := (p_source #>> '{owner,id}')::uuid;
        v_source_generation := (p_source ->> 'source_generation')::bigint;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END;
    v_source_snapshot := p_source ->> 'source_snapshot';
    IF v_spec ->> 'expected_source_snapshot' IS DISTINCT FROM v_source_snapshot THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    v_slot := CASE
        WHEN v_kind = 'pyramid' THEN 'pyramid:' || (v_policy ->> 'profile_version')
        WHEN v_kind = 'dimensions' AND v_spec -> 'generate_preview' = 'true'::jsonb
            THEN 'dimensions:preview'
        ELSE v_kind END;
    IF v_kind = 'pyramid' THEN
        BEGIN
            IF v_owner_kind = 'dataset_item' THEN
                INSERT INTO public.image_pyramid_assets(id, dataset_item_id, profile_version)
                VALUES (pg_catalog.gen_random_uuid(), v_owner_id, v_policy ->> 'profile_version')
                ON CONFLICT (dataset_item_id, profile_version)
                    WHERE dataset_item_id IS NOT NULL DO NOTHING;
                SELECT asset.* INTO v_asset
                FROM public.image_pyramid_assets AS asset
                WHERE asset.dataset_item_id = v_owner_id
                  AND asset.profile_version = v_policy ->> 'profile_version'
                FOR UPDATE NOWAIT;
            ELSE
                INSERT INTO public.image_pyramid_assets(id, task_id, profile_version)
                VALUES (pg_catalog.gen_random_uuid(), v_owner_id, v_policy ->> 'profile_version')
                ON CONFLICT (task_id, profile_version)
                    WHERE task_id IS NOT NULL DO NOTHING;
                SELECT asset.* INTO v_asset
                FROM public.image_pyramid_assets AS asset
                WHERE asset.task_id = v_owner_id
                  AND asset.profile_version = v_policy ->> 'profile_version'
                FOR UPDATE NOWAIT;
            END IF;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        WHEN unique_violation THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        END;
        IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_response'); END IF;
    END IF;
    BEGIN
        SELECT job.* INTO v_job
        FROM public.asset_image_jobs AS job
        WHERE job.owner_kind = v_owner_kind
          AND job.owner_id = v_owner_id
          AND job.slot_key = v_slot
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    END;
    v_existing := FOUND;
    IF v_existing THEN
        v_same := v_job.source_generation = v_source_generation
            AND v_job.source_snapshot = v_source_snapshot
            AND v_job.spec = v_spec;
        v_reuse := v_same AND v_job.status IN ('pending', 'running');
        IF v_same AND v_job.status = 'ready' AND NOT p_force THEN v_reuse := true; END IF;
        IF v_same AND v_job.status = 'failed' AND (
            NOT p_force OR v_now < COALESCE(v_job.completed_at, v_job.updated_at)
                + pg_catalog.make_interval(secs => (v_policy ->> 'retry_cooldown_seconds')::integer)
        ) THEN v_reuse := true; END IF;
        IF v_reuse THEN
            v_data := pg_catalog.jsonb_build_object(
                'operation_id', p_operation_id,
                'source', p_source,
                'work', aap_assets_v1._image_work_reference(v_job),
                'spec', v_job.spec
            );
            RETURN aap_assets_v1._image_reply(v_data, NULL);
        END IF;
        IF v_job.status IN ('pending', 'running') THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_conflict');
        END IF;
        IF v_kind = 'thumbnail' THEN
            IF v_job.status = 'ready' THEN
                IF v_job.kind = 'thumbnail'
                   AND v_job.owner_kind = v_owner_kind
                   AND v_job.owner_id = v_owner_id
                   AND v_job.source_generation = v_source_generation
                   AND v_job.source_snapshot = v_source_snapshot
                   AND v_job.spec ->> 'kind' = 'thumbnail'
                   AND v_job.spec ->> 'expected_source_snapshot' = v_source_snapshot
                   AND v_job.result ->> 'kind' = 'thumbnail'
                   AND v_job.output_prefix IS NOT NULL
                   AND v_job.result #>> '{artifact,key}' = v_job.output_prefix || 'thumbnail.webp'
                   AND p_source -> 'thumbnail_path' = v_job.result #> '{artifact,key}'
                   AND p_source -> 'thumbnail_source_generation' = p_source -> 'source_generation'
                   AND aap_assets_v1._media_object_identity_valid(
                        v_job.source_facts -> 'object_identity'
                   )
                   AND aap_assets_v1._image_exec_identity_equal(
                        v_job.source_facts -> 'object_identity', p_source -> 'object_identity'
                   )
                   AND aap_assets_v1._image_exec_identity_equal(
                        v_job.result -> 'source_identity', p_source -> 'object_identity'
                   ) THEN
                    v_retained_publication := pg_catalog.jsonb_build_object(
                        'schema', 'aap-image-thumbnail-publication/v1',
                        'job_id', v_job.job_id,
                        'generation', v_job.generation,
                        'owner', pg_catalog.jsonb_build_object(
                            'kind', v_job.owner_kind, 'id', v_job.owner_id
                        ),
                        'source_generation', v_job.source_generation,
                        'source_snapshot', v_job.source_snapshot,
                        'source_identity', aap_assets_v1._image_normalize_identity(
                            v_job.source_facts -> 'object_identity'
                        ),
                        'spec', v_job.spec,
                        'result', v_job.result
                    );
                END IF;
            ELSIF v_job.status IN ('failed', 'canceled') THEN
                v_retained_publication := v_job.progress -> 'retained_publication';
                IF pg_catalog.jsonb_typeof(v_retained_publication) IS DISTINCT FROM 'object'
                   OR (v_retained_publication - ARRAY[
                        'schema', 'job_id', 'generation', 'owner', 'source_generation',
                        'source_snapshot', 'source_identity', 'spec', 'result'
                   ]) IS DISTINCT FROM '{}'::jsonb
                   OR NOT (v_retained_publication ?& ARRAY[
                        'schema', 'job_id', 'generation', 'owner', 'source_generation',
                        'source_snapshot', 'source_identity', 'spec', 'result'
                   ])
                   OR v_retained_publication ->> 'schema'
                        IS DISTINCT FROM 'aap-image-thumbnail-publication/v1'
                   OR v_retained_publication ->> 'job_id' IS DISTINCT FROM v_job.job_id::text
                   OR COALESCE(v_retained_publication ->> 'generation', '') !~ '^[1-9][0-9]*$'
                   OR (v_retained_publication #>> '{owner,kind}') IS DISTINCT FROM v_owner_kind
                   OR (v_retained_publication #>> '{owner,id}') IS DISTINCT FROM v_owner_id::text
                   OR v_job.source_generation IS DISTINCT FROM v_source_generation
                   OR v_job.source_snapshot IS DISTINCT FROM v_source_snapshot
                   OR v_retained_publication ->> 'source_generation'
                        IS DISTINCT FROM v_source_generation::text
                   OR v_retained_publication ->> 'source_snapshot'
                        IS DISTINCT FROM v_source_snapshot
                   OR NOT aap_assets_v1._media_object_identity_valid(
                        v_retained_publication -> 'source_identity'
                   )
                   OR NOT aap_assets_v1._image_exec_identity_equal(
                        v_retained_publication -> 'source_identity', p_source -> 'object_identity'
                   )
                   OR aap_assets_v1._image_spec(v_retained_publication -> 'spec')
                        IS DISTINCT FROM v_retained_publication -> 'spec'
                   OR v_retained_publication #>> '{spec,kind}' IS DISTINCT FROM 'thumbnail'
                   OR v_retained_publication #>> '{spec,expected_source_snapshot}'
                        IS DISTINCT FROM v_source_snapshot
                   OR pg_catalog.jsonb_typeof(v_retained_publication -> 'result')
                        IS DISTINCT FROM 'object'
                   OR v_retained_publication #>> '{result,kind}' IS DISTINCT FROM 'thumbnail'
                   OR v_retained_publication #>> '{result,artifact,key}'
                        IS DISTINCT FROM p_source ->> 'thumbnail_path'
                   OR p_source -> 'thumbnail_source_generation'
                        IS DISTINCT FROM p_source -> 'source_generation'
                   OR NOT aap_assets_v1._media_object_identity_valid(
                        v_retained_publication #> '{result,source_identity}'
                   )
                   OR NOT aap_assets_v1._image_exec_identity_equal(
                        v_retained_publication #> '{result,source_identity}',
                        p_source -> 'object_identity'
                   ) THEN
                    v_retained_publication := NULL;
                END IF;
            END IF;
        END IF;
        IF v_job.generation = 9223372036854775807 THEN
            RETURN aap_assets_v1._image_reply(NULL, 'resource_budget_exceeded');
        END IF;
        IF v_kind = 'pyramid' THEN
            v_pyramid_generation := aap_assets_v1._image_next_generation(v_asset.id);
            IF v_pyramid_generation IS NULL THEN
                RETURN aap_assets_v1._image_reply(NULL, 'resource_budget_exceeded');
            END IF;
        END IF;
        v_job_generation := v_job.generation + 1;
        IF v_job.output_prefix IS NOT NULL THEN
            PERFORM aap_assets_v1._queue_image_garbage(
                v_job.output_prefix, 'attempt_abandoned',
                GREATEST(v_now, COALESCE(v_job.lease_expires_at, v_now))
                    + pg_catalog.make_interval(hours => (v_policy ->> 'gc_grace_hours')::integer)
            );
        END IF;
        IF v_kind = 'pyramid' AND v_job.pyramid_asset_id IS NOT NULL
           AND v_job.pyramid_generation IS NOT NULL THEN
            SELECT generation.* INTO v_old_generation
            FROM public.image_pyramid_generations AS generation
            WHERE generation.asset_id = v_job.pyramid_asset_id
              AND generation.generation = v_job.pyramid_generation
            FOR UPDATE NOWAIT;
            IF FOUND AND v_old_generation.status IN ('pending', 'building') THEN
                IF v_old_generation.output_prefix IS NOT NULL THEN
                    PERFORM aap_assets_v1._queue_image_garbage(
                        v_old_generation.output_prefix, 'attempt_abandoned',
                        GREATEST(v_now, COALESCE(v_old_generation.lease_expires_at, v_now))
                            + pg_catalog.make_interval(hours => (v_policy ->> 'gc_grace_hours')::integer)
                    );
                END IF;
                UPDATE public.image_pyramid_generations AS generation
                SET status = 'failed', error_code = 'source_changed',
                    updated_at = v_now
                WHERE generation.asset_id = v_old_generation.asset_id
                  AND generation.generation = v_old_generation.generation;
            END IF;
        END IF;
    ELSE
        v_job_generation := 1;
        v_job_id := pg_catalog.gen_random_uuid();
        IF v_kind = 'pyramid' THEN
            v_pyramid_generation := aap_assets_v1._image_next_generation(v_asset.id);
            IF v_pyramid_generation IS NULL THEN
                RETURN aap_assets_v1._image_reply(NULL, 'resource_budget_exceeded');
            END IF;
        END IF;
    END IF;
    IF v_existing THEN
        UPDATE public.asset_image_jobs AS job
        SET generation = v_job_generation,
            spec = v_spec,
            status = 'pending',
            source_generation = v_source_generation,
            source_snapshot = v_source_snapshot,
            source_facts = p_source,
            actor_id = p_actor_id,
            progress = CASE WHEN v_retained_publication IS NULL THEN '{}'::jsonb
                ELSE pg_catalog.jsonb_build_object(
                    'retained_publication', v_retained_publication
                ) END,
            result = NULL,
            error_code = NULL,
            error_detail = NULL,
            attempt_count = 0,
            lease_owner = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            output_prefix = NULL,
            pyramid_asset_id = CASE WHEN v_kind = 'pyramid' THEN v_asset.id END,
            pyramid_generation = v_pyramid_generation,
            last_lease_token = NULL,
            last_outcome = NULL,
            accepted_at = v_now,
            updated_at = v_now,
            completed_at = NULL
        WHERE job.job_id = v_job.job_id
        RETURNING job.* INTO v_job;
    ELSE
        INSERT INTO public.asset_image_jobs(
            job_id, kind, owner_kind, owner_id, slot_key, generation, spec, status,
            source_generation, source_snapshot, source_facts, actor_id, progress,
            attempt_count, pyramid_asset_id, pyramid_generation, accepted_at, updated_at
        ) VALUES (
            v_job_id, v_kind, v_owner_kind, v_owner_id, v_slot, v_job_generation,
            v_spec, 'pending', v_source_generation, v_source_snapshot, p_source,
            p_actor_id, '{}'::jsonb, 0,
            CASE WHEN v_kind = 'pyramid' THEN v_asset.id END,
            v_pyramid_generation, v_now, v_now
        ) RETURNING * INTO v_job;
    END IF;
    IF v_kind = 'pyramid' THEN
        IF EXISTS (
            SELECT 1 FROM public.image_pyramid_generations AS generation
            WHERE generation.asset_id = v_asset.id
              AND generation.generation = v_pyramid_generation
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23505', MESSAGE = 'image generation was already reserved';
        END IF;
        INSERT INTO public.image_pyramid_generations(
            asset_id, generation, job_id, job_generation, source_generation,
            source_snapshot, output_prefix, manifest, policy, source_identity,
            source_fingerprint, status, lease_token, lease_expires_at,
            width, height, tile_size, overlap, normalization_version,
            tile_count, retained_bytes, created_at, updated_at
        ) VALUES (
            v_asset.id, v_pyramid_generation, v_job.job_id, v_job.generation,
            v_source_generation, v_source_snapshot, NULL, NULL, v_policy,
            v_source_snapshot, NULL, 'pending', NULL, NULL, NULL, NULL, 512, 1,
            v_policy ->> 'normalization_version', 0, 0, v_now, v_now
        );
        UPDATE public.image_pyramid_assets AS asset
        SET building_generation = v_pyramid_generation,
            updated_at = v_now
        WHERE asset.id = v_asset.id;
    END IF;
    v_data := pg_catalog.jsonb_build_object(
        'operation_id', p_operation_id,
        'source', p_source,
        'work', aap_assets_v1._image_work_reference(v_job),
        'spec', v_job.spec
    );
    RETURN aap_assets_v1._image_reply(v_data, NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
WHEN unique_violation THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_enqueue(uuid, uuid, jsonb, jsonb, boolean)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._image_accept_source(
    p_operation_id uuid,
    p_actor_id uuid,
    p_task_id uuid,
    p_dataset_item_id uuid,
    p_purpose text,
    p_expected_source_snapshot text,
    p_policy jsonb,
    p_force boolean,
    p_action text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_policy jsonb;
    v_source_reply jsonb;
    v_source jsonb;
    v_access jsonb;
    v_spec jsonb;
    v_scope jsonb;
    v_intent jsonb;
    v_saved jsonb;
    v_result jsonb;
    v_data jsonb;
    v_job public.asset_image_jobs%ROWTYPE;
    v_projection jsonb;
    v_owner_kind text;
    v_owner_id uuid;
    v_slot text;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_force IS NULL
       OR p_action NOT IN ('request', 'retry')
       OR p_purpose NOT IN ('dimensions', 'thumbnail', 'pyramid')
       OR ((p_task_id IS NULL) = (p_dataset_item_id IS NULL))
       OR (p_expected_source_snapshot IS NOT NULL
           AND p_expected_source_snapshot !~ '^[0-9a-f]{64}$') THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_policy := aap_assets_v1._image_policy(COALESCE(p_policy, '{}'::jsonb));
    IF v_policy IS NULL THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    IF p_task_id IS NULL THEN
        v_access := aap_assets_v1._image_manager_item(p_actor_id, p_dataset_item_id, true);
        IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_access; END IF;
    ELSE
        v_access := aap_assets_v1._image_actor(p_actor_id, true);
        IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_access; END IF;
    END IF;
    v_source_reply := aap_assets_v1._image_owner_source(
        p_actor_id, p_task_id, p_dataset_item_id, true
    );
    IF v_source_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_source_reply; END IF;
    v_source := v_source_reply -> 'data';
    IF NOT aap_assets_v1._image_source_data_valid(v_source) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    v_spec := pg_catalog.jsonb_build_object(
        'kind', p_purpose,
        'expected_source_snapshot', v_source -> 'source_snapshot',
        'policy', v_policy
    );
    v_spec := aap_assets_v1._image_spec(v_spec);
    IF v_spec IS NULL THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_response'); END IF;
    v_scope := pg_catalog.jsonb_build_object(
        'task_id', p_task_id,
        'dataset_item_id', p_dataset_item_id,
        'purpose', p_purpose
    );
    v_intent := pg_catalog.jsonb_build_object(
        'expected_source_snapshot', p_expected_source_snapshot,
        'policy', v_policy,
        'force', p_force
    );
    v_saved := aap_assets_v1._image_read_operation(
        p_operation_id, p_actor_id, p_action, v_scope, v_intent
    );
    IF v_saved IS NOT NULL THEN
        IF v_saved ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
           OR pg_catalog.jsonb_typeof(v_saved -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_saved -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_saved; END IF;
        v_data := v_saved -> 'data';
        IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
           OR (v_data - ARRAY['operation_id', 'source', 'work', 'spec']) IS DISTINCT FROM '{}'::jsonb
           OR v_data ->> 'operation_id' IS DISTINCT FROM p_operation_id::text
           OR NOT aap_assets_v1._image_source_data_valid(v_data -> 'source')
           OR aap_assets_v1._image_spec(v_data -> 'spec') IS DISTINCT FROM v_data -> 'spec'
           OR pg_catalog.jsonb_typeof(v_data -> 'work') IS DISTINCT FROM 'object'
           OR v_data #>> '{spec,kind}' IS DISTINCT FROM p_purpose
           OR v_data #>> '{work,kind}' IS DISTINCT FROM p_purpose
           OR v_data #>> '{spec,expected_source_snapshot}' IS DISTINCT FROM v_data #>> '{source,source_snapshot}'
           OR v_data #> '{spec,policy}' IS DISTINCT FROM v_policy
           OR v_data #>> '{work,owner,kind}' IS DISTINCT FROM v_data #>> '{source,owner,kind}'
           OR v_data #>> '{work,owner,id}' IS DISTINCT FROM v_data #>> '{source,owner,id}'
           OR v_data #>> '{work,source_generation}' IS DISTINCT FROM v_data #>> '{source,source_generation}' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_data #>> '{source,owner,kind}' IS DISTINCT FROM v_source #>> '{owner,kind}'
           OR v_data #>> '{source,owner,id}' IS DISTINCT FROM v_source #>> '{owner,id}'
           OR v_data #>> '{source,source_snapshot}' IS DISTINCT FROM v_source ->> 'source_snapshot'
           OR v_data #>> '{source,source_generation}' IS DISTINCT FROM v_source ->> 'source_generation' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
        END IF;
        RETURN v_saved;
    END IF;
    IF v_source -> 'source_drifted' = 'true'::jsonb THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    IF v_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb
       OR v_source -> 'object_identity' = 'null'::jsonb THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_not_ready');
    END IF;
    IF p_expected_source_snapshot IS NOT NULL
       AND v_source ->> 'source_snapshot' IS DISTINCT FROM p_expected_source_snapshot THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    IF p_purpose = 'pyramid'
       AND v_source -> 'width' <> 'null'::jsonb
       AND v_source -> 'height' <> 'null'::jsonb
       AND (v_source ->> 'width')::numeric * (v_source ->> 'height')::numeric
            < (v_policy ->> 'optional_pixels')::numeric THEN
        RETURN aap_assets_v1._image_reply(NULL, 'resource_limit');
    END IF;
    IF p_action = 'retry' THEN
        v_owner_kind := v_source #>> '{owner,kind}';
        v_owner_id := (v_source #>> '{owner,id}')::uuid;
        v_slot := CASE WHEN p_purpose = 'pyramid'
            THEN 'pyramid:' || (v_policy ->> 'profile_version') ELSE p_purpose END;
        IF p_purpose = 'dimensions'
           AND v_source -> 'width' <> 'null'::jsonb
           AND v_source -> 'height' <> 'null'::jsonb THEN
            RETURN aap_assets_v1._image_reply(NULL, 'already_ready');
        ELSIF p_purpose = 'thumbnail'
           AND v_source -> 'thumbnail_path' <> 'null'::jsonb
           AND v_source -> 'thumbnail_source_generation' = v_source -> 'source_generation' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'already_ready');
        ELSIF p_purpose = 'pyramid' THEN
            v_projection := aap_assets_v1._image_pyramid_projection(
                v_source, NULL, v_policy ->> 'profile_version'
            );
            IF v_projection IS NULL THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            IF v_projection ->> 'status' = 'ready' THEN
                RETURN aap_assets_v1._image_reply(NULL, 'already_ready');
            END IF;
            IF v_source -> 'width' <> 'null'::jsonb
               AND v_source -> 'height' <> 'null'::jsonb
               AND (v_source ->> 'width')::numeric * (v_source ->> 'height')::numeric
                    < (v_policy ->> 'optional_pixels')::numeric THEN
                RETURN aap_assets_v1._image_reply(NULL, 'not_eligible');
            END IF;
        END IF;
        SELECT job.* INTO v_job
        FROM public.asset_image_jobs AS job
        WHERE job.owner_kind = v_owner_kind
          AND job.owner_id = v_owner_id
          AND job.slot_key = v_slot;
        IF FOUND AND v_job.status = 'failed'
           AND v_job.source_generation = (v_source ->> 'source_generation')::bigint
           AND v_job.source_snapshot = v_source ->> 'source_snapshot'
           AND v_job.spec = v_spec
           AND pg_catalog.clock_timestamp() < COALESCE(v_job.completed_at, v_job.updated_at)
               + pg_catalog.make_interval(secs => (v_policy ->> 'retry_cooldown_seconds')::integer) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'retry_cooldown');
        END IF;
    END IF;
    v_result := aap_assets_v1._image_enqueue(
        p_operation_id, p_actor_id, v_source, v_spec, p_force
    );
    IF v_result ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
       OR pg_catalog.jsonb_typeof(v_result -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'image enqueue envelope mismatch';
    END IF;
    IF v_result -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_result; END IF;
    v_data := v_result -> 'data';
    IF v_data ->> 'operation_id' IS DISTINCT FROM p_operation_id::text
       OR NOT aap_assets_v1._image_source_data_valid(v_data -> 'source')
       OR v_data #>> '{source,owner,kind}' IS DISTINCT FROM v_source #>> '{owner,kind}'
       OR v_data #>> '{source,owner,id}' IS DISTINCT FROM v_source #>> '{owner,id}'
       OR v_data #>> '{source,source_snapshot}' IS DISTINCT FROM v_source ->> 'source_snapshot'
       OR v_data #>> '{spec,kind}' IS DISTINCT FROM p_purpose
       OR v_data #>> '{work,kind}' IS DISTINCT FROM p_purpose
       OR v_data #>> '{spec,expected_source_snapshot}' IS DISTINCT FROM v_source ->> 'source_snapshot'
       OR v_data #>> '{work,owner,kind}' IS DISTINCT FROM v_source #>> '{owner,kind}'
       OR v_data #>> '{work,owner,id}' IS DISTINCT FROM v_source #>> '{owner,id}'
       OR v_data #>> '{work,source_generation}' IS DISTINCT FROM v_source ->> 'source_generation' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'image enqueue result escaped request';
    END IF;
    v_saved := aap_assets_v1._image_write_operation(
        p_operation_id, p_actor_id, p_action, v_scope, v_intent, v_result
    );
    IF v_saved IS NULL OR v_saved IS DISTINCT FROM v_result THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'image operation receipt mismatch';
    END IF;
    RETURN v_saved;
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
WHEN unique_violation THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_accept_source(
    uuid, uuid, uuid, uuid, text, text, jsonb, boolean, text
) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.prepare_image_media(
    p_operation_id uuid,
    p_actor_id uuid,
    p_task_id uuid,
    p_dataset_item_id uuid,
    p_purpose text,
    p_expected_source_snapshot text,
    p_policy jsonb,
    p_force boolean
)
RETURNS jsonb
LANGUAGE sql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT aap_assets_v1._image_accept_source(
        p_operation_id, p_actor_id, p_task_id, p_dataset_item_id,
        p_purpose, p_expected_source_snapshot, p_policy, p_force, 'request'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.prepare_image_media(
    uuid, uuid, uuid, uuid, text, text, jsonb, boolean
) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.retry_image_media(
    p_operation_id uuid,
    p_actor_id uuid,
    p_task_id uuid,
    p_dataset_item_id uuid,
    p_purpose text,
    p_expected_source_snapshot text,
    p_policy jsonb,
    p_force boolean
)
RETURNS jsonb
LANGUAGE sql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT aap_assets_v1._image_accept_source(
        p_operation_id, p_actor_id, p_task_id, p_dataset_item_id,
        p_purpose, p_expected_source_snapshot, p_policy, p_force, 'retry'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.retry_image_media(
    uuid, uuid, uuid, uuid, text, text, jsonb, boolean
) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.schedule_image_media(
    p_operation_id uuid,
    p_owner_kind text,
    p_owner_id uuid,
    p_spec jsonb,
    p_force boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_spec jsonb;
    v_source_reply jsonb;
    v_source jsonb;
    v_scope jsonb;
    v_intent jsonb;
    v_saved jsonb;
    v_result jsonb;
    v_data jsonb;
BEGIN
    IF p_force IS NULL THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    v_spec := aap_assets_v1._image_spec(p_spec);
    IF v_spec IS NULL THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    IF v_spec ->> 'kind' = 'image_backfill' THEN
        IF p_owner_kind IS NOT NULL OR p_owner_id IS NOT NULL THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        v_scope := pg_catalog.jsonb_build_object('owner_kind', NULL, 'owner_id', NULL);
    ELSE
        IF p_owner_kind IS NULL OR p_owner_kind NOT IN ('dataset_item', 'task')
           OR p_owner_id IS NULL THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        v_scope := pg_catalog.jsonb_build_object(
            'owner_kind', p_owner_kind, 'owner_id', p_owner_id
        );
    END IF;
    v_intent := pg_catalog.jsonb_build_object('spec', v_spec, 'force', p_force);
    IF p_operation_id IS NOT NULL THEN
        v_saved := aap_assets_v1._image_read_operation(
            p_operation_id, NULL, 'request', v_scope, v_intent
        );
        IF v_saved IS NOT NULL THEN
            IF v_saved ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
               OR pg_catalog.jsonb_typeof(v_saved -> 'ok') IS DISTINCT FROM 'boolean' THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            IF v_saved -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_saved; END IF;
            v_data := v_saved -> 'data';
            IF v_data ->> 'operation_id' IS DISTINCT FROM p_operation_id::text
               OR v_data -> 'spec' IS NULL
               OR v_data #>> '{spec,kind}' IS DISTINCT FROM v_spec ->> 'kind'
               OR (p_owner_kind IS NULL AND v_data -> 'source' <> 'null'::jsonb)
               OR (p_owner_kind IS NOT NULL AND (
                    NOT aap_assets_v1._image_source_data_valid(v_data -> 'source')
                    OR v_data #>> '{source,owner,kind}' IS DISTINCT FROM p_owner_kind
                    OR v_data #>> '{source,owner,id}' IS DISTINCT FROM p_owner_id::text
                    OR v_data #>> '{work,owner,kind}' IS DISTINCT FROM p_owner_kind
                    OR v_data #>> '{work,owner,id}' IS DISTINCT FROM p_owner_id::text
               )) THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            RETURN v_saved;
        END IF;
    END IF;
    IF p_owner_kind IS NULL THEN
        v_result := aap_assets_v1._image_enqueue(
            p_operation_id, NULL, NULL, v_spec, p_force
        );
    ELSE
        v_source_reply := aap_assets_v1._image_resolve_owner(
            p_owner_kind, p_owner_id, true
        );
        IF v_source_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN v_source_reply;
        END IF;
        v_source := v_source_reply -> 'data';
        IF NOT aap_assets_v1._image_source_data_valid(v_source) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_source -> 'source_drifted' = 'true'::jsonb THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
        END IF;
        IF v_source ->> 'source_snapshot' IS DISTINCT FROM v_spec ->> 'expected_source_snapshot' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
        END IF;
        v_result := aap_assets_v1._image_enqueue(
            p_operation_id, NULL, v_source, v_spec, p_force
        );
    END IF;
    IF v_result ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
       OR pg_catalog.jsonb_typeof(v_result -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'image schedule envelope mismatch';
    END IF;
    IF v_result -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_result; END IF;
    IF p_operation_id IS NOT NULL THEN
        v_saved := aap_assets_v1._image_write_operation(
            p_operation_id, NULL, 'request', v_scope, v_intent, v_result
        );
        IF v_saved IS NULL OR v_saved IS DISTINCT FROM v_result THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'image schedule receipt mismatch';
        END IF;
        RETURN v_saved;
    END IF;
    RETURN v_result;
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
WHEN unique_violation THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.schedule_image_media(uuid, text, uuid, jsonb, boolean)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.scan_image_backfill(
    p_selection jsonb,
    p_after_owner_kind text,
    p_after_owner_id uuid,
    p_page_size integer
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_selection jsonb;
    v_owner_kind text;
    v_cutoff timestamptz;
    v_dataset_id uuid;
    v_project_id uuid;
    v_owner_ids uuid[];
    v_all_owners boolean;
    v_task_reply jsonb;
    v_task_data jsonb;
    v_items jsonb;
    v_item_count integer;
    v_total bigint;
    v_digest text;
    v_next_id uuid;
    v_complete boolean;
    v_item_ids jsonb;
    v_row jsonb;
    v_previous_id uuid;
    v_current_id uuid;
    v_source jsonb;
BEGIN
    IF p_page_size IS NULL OR p_page_size NOT BETWEEN 1 AND 500
       OR ((p_after_owner_kind IS NULL) <> (p_after_owner_id IS NULL)) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_previous_id := p_after_owner_id;
    v_selection := aap_assets_v1._image_backfill_selection(p_selection);
    IF v_selection IS NULL THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    v_owner_kind := v_selection ->> 'owner_kind';
    IF p_after_owner_kind IS NOT NULL AND p_after_owner_kind IS DISTINCT FROM v_owner_kind THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        v_cutoff := CASE WHEN v_selection -> 'accepted_before' = 'null'::jsonb
            THEN pg_catalog.transaction_timestamp()
            ELSE (v_selection ->> 'accepted_before')::timestamptz END;
        IF v_selection -> 'dataset_id' <> 'null'::jsonb THEN
            v_dataset_id := (v_selection ->> 'dataset_id')::uuid;
        END IF;
        IF v_selection -> 'project_id' <> 'null'::jsonb THEN
            v_project_id := (v_selection ->> 'project_id')::uuid;
        END IF;
        SELECT COALESCE(pg_catalog.array_agg((entry.value #>> '{}')::uuid), ARRAY[]::uuid[])
        INTO v_owner_ids
        FROM pg_catalog.jsonb_array_elements(v_selection -> 'owner_ids') AS entry(value);
    EXCEPTION WHEN invalid_text_representation OR invalid_datetime_format OR datetime_field_overflow THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END;
    v_all_owners := (v_selection ->> 'all_owners')::boolean;
    v_selection := pg_catalog.jsonb_set(
        v_selection, '{accepted_before}', pg_catalog.to_jsonb(v_cutoff), true
    );
    IF v_owner_kind = 'task' THEN
        v_task_reply := aap_labeling_v1.scan_image_task_sources(
            v_selection, p_after_owner_id, p_page_size
        );
        IF v_task_reply IS NULL
           OR v_task_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-image-backfill/v1'
           OR pg_catalog.jsonb_typeof(v_task_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_task_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            CASE v_task_reply #>> '{error,code}'
                WHEN 'invalid_request' THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
                WHEN 'source_missing' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_missing');
                WHEN 'source_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
                ELSE RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END CASE;
        END IF;
        v_task_data := v_task_reply -> 'data';
        IF pg_catalog.jsonb_typeof(v_task_data) IS DISTINCT FROM 'object'
           OR (v_task_data - ARRAY[
                'items', 'snapshot_digest', 'total', 'accepted_before',
                'after_id', 'next_id', 'complete'
           ]) IS DISTINCT FROM '{}'::jsonb
           OR NOT (v_task_data ?& ARRAY[
                'items', 'snapshot_digest', 'total', 'accepted_before',
                'after_id', 'next_id', 'complete'
           ])
           OR pg_catalog.jsonb_typeof(v_task_data -> 'items') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(v_task_data -> 'items') > p_page_size
           OR COALESCE(v_task_data ->> 'snapshot_digest', '') !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(v_task_data -> 'total') IS DISTINCT FROM 'number'
           OR v_task_data ->> 'total' !~ '^(0|[1-9][0-9]*)$'
           OR pg_catalog.char_length(v_task_data ->> 'total') > 19
           OR pg_catalog.jsonb_typeof(v_task_data -> 'accepted_before') IS DISTINCT FROM 'string'
           OR (v_task_data ->> 'accepted_before')::timestamptz IS DISTINCT FROM v_cutoff
           OR (v_task_data -> 'after_id' = 'null'::jsonb AND p_after_owner_id IS NOT NULL)
           OR (v_task_data -> 'after_id' <> 'null'::jsonb
               AND v_task_data ->> 'after_id' IS DISTINCT FROM p_after_owner_id::text)
           OR pg_catalog.jsonb_typeof(v_task_data -> 'complete') IS DISTINCT FROM 'boolean'
           OR (v_task_data -> 'next_id' = 'null'::jsonb)
                IS DISTINCT FROM (v_task_data -> 'complete' = 'true'::jsonb) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        v_total := (v_task_data ->> 'total')::bigint;
        v_items := '[]'::jsonb;
        FOR v_item_ids IN
            SELECT entry.value
            FROM pg_catalog.jsonb_array_elements(v_task_data -> 'items') WITH ORDINALITY AS entry(value, ordinality)
            ORDER BY entry.ordinality
        LOOP
            IF pg_catalog.jsonb_typeof(v_item_ids) IS DISTINCT FROM 'object'
               OR v_item_ids ->> 'file_type' IS DISTINCT FROM 'image'
               OR (v_project_id IS NOT NULL
                   AND v_item_ids ->> 'project_id' IS DISTINCT FROM v_project_id::text)
               OR (pg_catalog.cardinality(v_owner_ids) > 0
                   AND NOT ((v_item_ids ->> 'task_id')::uuid = ANY(v_owner_ids))) THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            v_source := aap_assets_v1._image_task_source_json(v_item_ids);
            IF v_source IS NULL OR NOT aap_assets_v1._image_source_data_valid(v_source) THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            IF p_after_owner_id IS NOT NULL
               AND (v_source #>> '{owner,id}')::uuid <= p_after_owner_id THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            v_items := v_items || pg_catalog.jsonb_build_array(v_source);
        END LOOP;
        v_item_count := pg_catalog.jsonb_array_length(v_items);
        IF v_item_count > v_total
           OR (v_task_data -> 'complete' = 'false'::jsonb AND v_item_count = 0)
           OR (v_task_data -> 'next_id' <> 'null'::jsonb
               AND (v_item_count = 0 OR v_task_data ->> 'next_id'
                    IS DISTINCT FROM v_items -> (v_item_count - 1) #>> '{owner,id}')) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        v_next_id := CASE WHEN v_task_data -> 'next_id' = 'null'::jsonb
            THEN NULL ELSE (v_task_data ->> 'next_id')::uuid END;
        v_complete := (v_task_data ->> 'complete')::boolean;
        v_digest := v_task_data ->> 'snapshot_digest';
        v_cutoff := (v_task_data ->> 'accepted_before')::timestamptz;
    ELSE
        IF pg_catalog.cardinality(v_owner_ids) > 0 AND EXISTS (
            SELECT 1 FROM pg_catalog.unnest(v_owner_ids) AS requested(item_id)
            WHERE NOT EXISTS (
                SELECT 1 FROM public.dataset_items AS item
                WHERE item.id = requested.item_id AND item.file_type = 'image'
            )
        ) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_missing');
        END IF;
        WITH candidates AS MATERIALIZED (
            SELECT item.id,
                   aap_assets_v1._image_item_source_snapshot(item) AS source_snapshot
            FROM public.dataset_items AS item
            WHERE item.file_type = 'image'
              AND item.created_at <= v_cutoff
              AND (
                    (v_dataset_id IS NOT NULL AND item.dataset_id = v_dataset_id)
                    OR (v_project_id IS NOT NULL AND EXISTS (
                        SELECT 1 FROM public.project_datasets AS association
                        WHERE association.project_id = v_project_id
                          AND association.dataset_id = item.dataset_id
                    ))
                    OR (pg_catalog.cardinality(v_owner_ids) > 0 AND item.id = ANY(v_owner_ids))
                    OR v_all_owners
              )
        ), snapshot AS (
            SELECT pg_catalog.count(*)::bigint AS total,
                   pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                       pg_catalog.jsonb_build_object(
                           'accepted_before', v_cutoff AT TIME ZONE 'UTC',
                           'items', COALESCE(pg_catalog.jsonb_agg(
                               pg_catalog.jsonb_build_object(
                                   'item_id', candidates.id,
                                   'source_snapshot', candidates.source_snapshot
                               ) ORDER BY candidates.id
                           ), '[]'::jsonb)
                       )::text,
                       'UTF8'
                   )), 'hex') AS snapshot_digest
            FROM candidates
        ), ranked AS (
            SELECT candidates.id,
                   pg_catalog.row_number() OVER (ORDER BY candidates.id) AS row_number
            FROM candidates
            WHERE p_after_owner_id IS NULL OR candidates.id > p_after_owner_id
        ), page AS (
            SELECT ranked.id, ranked.row_number
            FROM ranked
            WHERE ranked.row_number <= p_page_size + 1
        )
        SELECT snapshot.total, snapshot.snapshot_digest,
               COALESCE(pg_catalog.jsonb_agg(
                   aap_assets_v1._image_item_source_json(item, NULL, v_project_id)
                   ORDER BY page.id
               ) FILTER (WHERE page.row_number <= p_page_size), '[]'::jsonb),
               COALESCE(pg_catalog.bool_or(page.row_number > p_page_size), false),
               (pg_catalog.array_agg(page.id) FILTER (WHERE page.row_number = p_page_size))[1]
        INTO v_total, v_digest, v_items, v_complete, v_next_id
        FROM snapshot
        LEFT JOIN page ON true
        LEFT JOIN public.dataset_items AS item ON item.id = page.id
        GROUP BY snapshot.total, snapshot.snapshot_digest;
        v_complete := NOT v_complete;
        v_item_count := pg_catalog.jsonb_array_length(v_items);
        IF v_item_count = 0 OR v_complete THEN
            v_next_id := NULL;
        END IF;
    END IF;
    FOR v_row IN
        SELECT row.value
        FROM pg_catalog.jsonb_array_elements(v_items) AS row(value)
    LOOP
        IF NOT aap_assets_v1._image_source_data_valid(v_row)
           OR v_row #>> '{owner,kind}' IS DISTINCT FROM v_owner_kind THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        BEGIN
            v_current_id := (v_row #>> '{owner,id}')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END;
        IF v_previous_id IS NOT NULL AND v_current_id <= v_previous_id THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_dataset_id IS NOT NULL
           AND v_row ->> 'dataset_id' IS DISTINCT FROM v_dataset_id::text THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_project_id IS NOT NULL
           AND v_row ->> 'project_id' IS DISTINCT FROM v_project_id::text THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        v_previous_id := v_current_id;
    END LOOP;
    RETURN aap_assets_v1._image_reply(
        pg_catalog.jsonb_build_object(
            'items', v_items,
            'snapshot_digest', v_digest,
            'total', v_total,
            'accepted_before', v_cutoff,
            'after_owner_kind', p_after_owner_kind,
            'after_owner_id', p_after_owner_id,
            'next_owner_kind', CASE WHEN v_complete THEN NULL ELSE v_owner_kind END,
            'next_owner_id', CASE WHEN v_complete THEN NULL ELSE v_next_id END,
            'complete', v_complete
        ), NULL
    );
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range
    OR invalid_datetime_format OR datetime_field_overflow THEN
    RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
WHEN OTHERS THEN
    RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.scan_image_backfill(jsonb, text, uuid, integer)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.cancel_image_media(
    p_operation_id uuid, p_actor_id uuid, p_job_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_access jsonb;
    v_job public.asset_image_jobs%ROWTYPE;
    v_generation public.image_pyramid_generations%ROWTYPE;
    v_policy jsonb;
    v_scope jsonb;
    v_intent jsonb;
    v_saved jsonb;
    v_result jsonb;
    v_data jsonb;
    v_canceled boolean := false;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_prefix text;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_job_id IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_access := aap_assets_v1._image_job_access(p_job_id, p_actor_id, true);
    IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_access; END IF;
    v_scope := pg_catalog.jsonb_build_object('job_id', p_job_id);
    v_intent := pg_catalog.jsonb_build_object('job_id', p_job_id);
    v_saved := aap_assets_v1._image_read_operation(
        p_operation_id, p_actor_id, 'cancel', v_scope, v_intent
    );
    IF v_saved IS NOT NULL THEN
        IF v_saved ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
           OR pg_catalog.jsonb_typeof(v_saved -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_saved -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_saved; END IF;
        v_data := v_saved -> 'data';
        IF v_data ->> 'operation_id' IS DISTINCT FROM p_operation_id::text
           OR v_data ->> 'job_id' IS DISTINCT FROM p_job_id::text
           OR pg_catalog.jsonb_typeof(v_data -> 'canceled') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        RETURN v_saved;
    END IF;
    BEGIN
        SELECT job.* INTO v_job
        FROM public.asset_image_jobs AS job
        WHERE job.job_id = p_job_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'source_missing'); END IF;
        IF v_job.status IN ('pending', 'running') THEN
            v_canceled := true;
            v_policy := aap_assets_v1._image_policy(COALESCE(v_job.spec -> 'policy', '{}'::jsonb));
            IF v_policy IS NULL THEN RAISE EXCEPTION 'invalid captured image policy'; END IF;
            IF v_job.output_prefix IS NOT NULL THEN
                PERFORM aap_assets_v1._queue_image_garbage(
                    v_job.output_prefix,
                    'attempt_abandoned',
                    GREATEST(v_now, COALESCE(v_job.lease_expires_at, v_now))
                        + pg_catalog.make_interval(hours => (v_policy ->> 'gc_grace_hours')::integer)
                );
            END IF;
            IF v_job.kind = 'pyramid'
               AND v_job.pyramid_asset_id IS NOT NULL
               AND v_job.pyramid_generation IS NOT NULL THEN
                SELECT generation.* INTO v_generation
                FROM public.image_pyramid_generations AS generation
                WHERE generation.asset_id = v_job.pyramid_asset_id
                  AND generation.generation = v_job.pyramid_generation
                FOR UPDATE NOWAIT;
                IF FOUND AND v_generation.status IN ('pending', 'building') THEN
                    v_prefix := COALESCE(v_generation.output_prefix, v_job.output_prefix);
                    IF v_prefix IS NOT NULL AND v_prefix IS DISTINCT FROM v_job.output_prefix THEN
                        PERFORM aap_assets_v1._queue_image_garbage(
                            v_prefix,
                            'attempt_abandoned',
                            GREATEST(v_now, COALESCE(v_generation.lease_expires_at, v_now))
                                + pg_catalog.make_interval(hours => (v_policy ->> 'gc_grace_hours')::integer)
                        );
                    END IF;
                    UPDATE public.image_pyramid_generations AS generation
                    SET status = 'failed', error_code = 'owner_changed',
                        lease_token = NULL, lease_expires_at = NULL,
                        updated_at = v_now
                    WHERE generation.asset_id = v_generation.asset_id
                      AND generation.generation = v_generation.generation;
                    UPDATE public.image_pyramid_assets AS asset
                    SET building_generation = CASE
                            WHEN asset.building_generation = v_job.pyramid_generation THEN NULL
                            ELSE asset.building_generation END,
                        updated_at = v_now
                    WHERE asset.id = v_job.pyramid_asset_id;
                END IF;
            END IF;
            UPDATE public.asset_image_jobs AS job
            SET status = 'canceled',
                error_code = 'canceled',
                error_detail = NULL,
                last_lease_token = CASE WHEN v_job.lease_token IS NOT NULL
                    THEN v_job.lease_token ELSE job.last_lease_token END,
                last_outcome = CASE WHEN v_job.lease_token IS NOT NULL
                    THEN 'lease_lost' ELSE job.last_outcome END,
                lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                completed_at = v_now,
                updated_at = v_now
            WHERE job.job_id = p_job_id
            RETURNING job.* INTO v_job;
        END IF;
        v_result := aap_assets_v1._image_reply(
            pg_catalog.jsonb_build_object(
                'operation_id', p_operation_id,
                'job_id', p_job_id,
                'canceled', v_canceled
            ), NULL
        );
        v_saved := aap_assets_v1._image_write_operation(
            p_operation_id, p_actor_id, 'cancel', v_scope, v_intent, v_result
        );
        IF v_saved IS NULL OR v_saved IS DISTINCT FROM v_result THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'image cancellation receipt mismatch';
        END IF;
        RETURN v_saved;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    WHEN OTHERS THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.cancel_image_media(uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.invalidate_image_media(
    p_actor_id uuid,
    p_task_id uuid,
    p_dataset_item_id uuid,
    p_invalidation jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_access jsonb;
    v_source_reply jsonb;
    v_source jsonb;
    v_updated_source jsonb;
    v_owner_kind text;
    v_owner_id uuid;
    v_snapshot text;
    v_kind text;
    v_generation integer;
    v_level integer;
    v_x integer;
    v_y integer;
    v_key text;
    v_prefix text;
    v_match text[];
    v_asset public.image_pyramid_assets%ROWTYPE;
    v_gen public.image_pyramid_generations%ROWTYPE;
    v_task_reply jsonb;
    v_task_data jsonb;
    v_changed boolean := false;
    v_projection jsonb;
    v_policy jsonb;
    v_profile_version text;
BEGIN
    IF p_actor_id IS NULL OR ((p_task_id IS NULL) = (p_dataset_item_id IS NULL))
       OR pg_catalog.jsonb_typeof(p_invalidation) IS DISTINCT FROM 'object'
       OR (p_invalidation - ARRAY[
            'source_snapshot', 'kind', 'generation', 'profile_version', 'level', 'x', 'y'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_invalidation ?& ARRAY['source_snapshot', 'kind'])
       OR pg_catalog.jsonb_typeof(p_invalidation -> 'source_snapshot') IS DISTINCT FROM 'string'
       OR p_invalidation ->> 'source_snapshot' !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_invalidation -> 'kind') IS DISTINCT FROM 'string'
       OR p_invalidation ->> 'kind' NOT IN ('thumbnail', 'overview', 'manifest', 'tile') THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_kind := p_invalidation ->> 'kind';
    v_snapshot := p_invalidation ->> 'source_snapshot';
    IF p_invalidation -> 'profile_version' <> 'null'::jsonb
       AND (pg_catalog.jsonb_typeof(p_invalidation -> 'profile_version') IS DISTINCT FROM 'string'
            OR pg_catalog.char_length(p_invalidation ->> 'profile_version') NOT BETWEEN 1 AND 64) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_profile_version := COALESCE(p_invalidation ->> 'profile_version', 'pyramid-v1');
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_each(p_invalidation) AS field(name, value)
        WHERE field.name IN ('generation', 'level', 'x', 'y')
          AND field.value <> 'null'::jsonb
          AND (pg_catalog.jsonb_typeof(field.value) IS DISTINCT FROM 'number'
               OR field.value #>> '{}' !~ '^(0|[1-9][0-9]*)$'
               OR pg_catalog.char_length(field.value #>> '{}') > 10
               OR (field.value #>> '{}')::numeric > 2147483647)
    ) THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    BEGIN
        v_generation := CASE WHEN p_invalidation -> 'generation' = 'null'::jsonb
            THEN NULL ELSE (p_invalidation ->> 'generation')::integer END;
        v_level := CASE WHEN p_invalidation -> 'level' = 'null'::jsonb
            THEN NULL ELSE (p_invalidation ->> 'level')::integer END;
        v_x := CASE WHEN p_invalidation -> 'x' = 'null'::jsonb
            THEN NULL ELSE (p_invalidation ->> 'x')::integer END;
        v_y := CASE WHEN p_invalidation -> 'y' = 'null'::jsonb
            THEN NULL ELSE (p_invalidation ->> 'y')::integer END;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END;
    IF (v_kind = 'thumbnail' AND (v_generation IS NOT NULL OR p_invalidation -> 'profile_version' <> 'null'::jsonb OR v_level IS NOT NULL OR v_x IS NOT NULL OR v_y IS NOT NULL))
       OR (v_kind IN ('overview', 'manifest') AND (
            v_generation IS NULL OR v_generation < 1 OR v_level IS NOT NULL OR v_x IS NOT NULL OR v_y IS NOT NULL
       ))
       OR (v_kind = 'tile' AND (
            v_generation IS NULL OR v_generation < 1 OR v_level IS NULL OR v_x IS NULL OR v_y IS NULL
       )) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_access := aap_assets_v1._image_actor(p_actor_id, true);
    IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_access; END IF;
    v_source_reply := aap_assets_v1._image_owner_source(
        p_actor_id, p_task_id, p_dataset_item_id, true
    );
    IF v_source_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_source_reply; END IF;
    v_source := v_source_reply -> 'data';
    IF NOT aap_assets_v1._image_source_data_valid(v_source) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    IF v_source ->> 'source_snapshot' IS DISTINCT FROM v_snapshot THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    v_owner_kind := v_source #>> '{owner,kind}';
    v_owner_id := (v_source #>> '{owner,id}')::uuid;

    IF v_kind = 'thumbnail' THEN
        v_key := v_source ->> 'thumbnail_path';
        IF v_key IS NOT NULL THEN
            IF v_owner_kind = 'task' THEN
                v_task_reply := aap_labeling_v1.invalidate_task_image_thumbnail(
                    v_owner_id, v_snapshot, v_key
                );
                IF v_task_reply IS NULL
                   OR v_task_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-image-thumbnail/v1'
                   OR pg_catalog.jsonb_typeof(v_task_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
                    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Task thumbnail invalidation envelope mismatch';
                END IF;
                IF v_task_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                    CASE v_task_reply #>> '{error,code}'
                        WHEN 'source_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
                        WHEN 'source_changed' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
                        WHEN 'invalid_request' THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
                        ELSE RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
                    END CASE;
                END IF;
                v_task_data := v_task_reply -> 'data';
                IF (v_task_data - ARRAY['task_id', 'applied']) IS DISTINCT FROM '{}'::jsonb
                   OR v_task_data ->> 'task_id' IS DISTINCT FROM v_owner_id::text
                   OR pg_catalog.jsonb_typeof(v_task_data -> 'applied') IS DISTINCT FROM 'boolean' THEN
                    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Task thumbnail invalidation data mismatch';
                END IF;
                v_changed := (v_task_data ->> 'applied')::boolean;
            ELSE
                UPDATE public.dataset_items AS item
                SET thumbnail_path = NULL,
                    blurhash = NULL,
                    media_materialized_generation = 0,
                    updated_at = pg_catalog.clock_timestamp()
                WHERE item.id = v_owner_id
                  AND item.media_source_generation = (v_source ->> 'source_generation')::bigint
                  AND item.thumbnail_path = v_key
                  AND item.media_materialized_generation = item.media_source_generation
                  AND NOT item.media_source_drifted;
                v_changed := FOUND;
            END IF;
            IF v_changed THEN
                IF v_key ~ '^image-assets/' THEN
                    v_prefix := pg_catalog.regexp_replace(v_key, 'thumbnail\.webp$', '');
                    PERFORM aap_assets_v1._queue_image_garbage(
                        v_prefix, 'object_missing', pg_catalog.clock_timestamp()
                    );
                ELSE
                    v_match := pg_catalog.regexp_match(
                        v_key,
                        '^image-pyramids/([0-9a-f-]{36})/g([1-9][0-9]*)/a[0-9a-f-]{36}/overview\.webp$'
                    );
                    IF v_match IS NULL THEN
                        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid current pyramid thumbnail key';
                    END IF;
                    v_prefix := pg_catalog.regexp_replace(v_key, 'overview\.webp$', '');
                    SELECT asset.* INTO v_asset FROM public.image_pyramid_assets AS asset
                    WHERE asset.id = v_match[1]::uuid
                      AND ((v_owner_kind = 'task' AND asset.task_id = v_owner_id)
                           OR (v_owner_kind = 'dataset_item' AND asset.dataset_item_id = v_owner_id))
                    FOR UPDATE NOWAIT;
                    IF FOUND THEN
                        SELECT generation.* INTO v_gen
                        FROM public.image_pyramid_generations AS generation
                        WHERE generation.asset_id = v_asset.id
                          AND generation.generation = v_match[2]::integer
                        FOR UPDATE NOWAIT;
                        IF FOUND AND v_gen.overview_key = v_key THEN
                            v_prefix := COALESCE(v_gen.output_prefix,
                                pg_catalog.regexp_replace(v_key, 'overview\.webp$', ''));
                            v_policy := aap_assets_v1._image_policy(COALESCE(v_gen.policy, '{}'::jsonb));
                            IF v_policy IS NULL THEN
                                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid captured image policy';
                            END IF;
                            UPDATE public.image_pyramid_generations AS generation
                            SET status = 'failed', error_code = 'object_missing',
                                updated_at = pg_catalog.clock_timestamp()
                            WHERE generation.asset_id = v_gen.asset_id
                              AND generation.generation = v_gen.generation;
                            UPDATE public.image_pyramid_assets AS asset
                            SET active_generation = CASE
                                    WHEN asset.active_generation = v_gen.generation THEN NULL
                                    ELSE asset.active_generation END,
                                updated_at = pg_catalog.clock_timestamp()
                            WHERE asset.id = v_asset.id;
                            PERFORM aap_assets_v1._queue_image_garbage(
                                v_prefix, 'object_missing',
                                pg_catalog.clock_timestamp()
                                    + pg_catalog.make_interval(hours => (v_policy ->> 'gc_grace_hours')::integer)
                            );
                            v_prefix := NULL;
                        ELSIF FOUND AND v_asset.active_generation = v_gen.generation THEN
                            v_prefix := NULL;
                        END IF;
                    END IF;
                    IF v_prefix IS NOT NULL THEN
                        PERFORM aap_assets_v1._queue_image_garbage(
                            v_prefix, 'object_missing', pg_catalog.clock_timestamp()
                        );
                    END IF;
                END IF;
            END IF;
        END IF;
    ELSE
        IF v_owner_kind = 'dataset_item' THEN
            SELECT asset.* INTO v_asset FROM public.image_pyramid_assets AS asset
            WHERE asset.dataset_item_id = v_owner_id
              AND asset.profile_version = v_profile_version
            ORDER BY asset.updated_at DESC, asset.id DESC
            LIMIT 1 FOR UPDATE NOWAIT;
        ELSE
            SELECT asset.* INTO v_asset FROM public.image_pyramid_assets AS asset
            WHERE asset.task_id = v_owner_id
              AND asset.profile_version = v_profile_version
            ORDER BY asset.updated_at DESC, asset.id DESC
            LIMIT 1 FOR UPDATE NOWAIT;
        END IF;
        IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'asset_missing'); END IF;
        IF v_asset.active_generation IS DISTINCT FROM v_generation THEN
            RETURN aap_assets_v1._image_reply(NULL, 'asset_missing');
        END IF;
        SELECT generation.* INTO v_gen
        FROM public.image_pyramid_generations AS generation
        WHERE generation.asset_id = v_asset.id
          AND generation.generation = v_generation
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'asset_missing'); END IF;
        IF v_gen.status IS DISTINCT FROM 'ready'
           OR v_gen.source_generation IS DISTINCT FROM (v_source ->> 'source_generation')::bigint
           OR v_gen.source_snapshot IS DISTINCT FROM v_snapshot
           OR v_gen.source_identity IS DISTINCT FROM v_snapshot
           OR v_source -> 'source_drifted' = 'true'::jsonb THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
        END IF;
        IF v_kind = 'overview' THEN
            v_key := v_gen.overview_key;
        ELSIF v_kind = 'manifest' THEN
            v_key := v_gen.manifest_key;
        ELSE
            IF pg_catalog.jsonb_typeof(v_gen.manifest -> 'levels') IS DISTINCT FROM 'array' THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            SELECT level.value INTO v_projection
            FROM pg_catalog.jsonb_array_elements(v_gen.manifest -> 'levels') AS level(value)
            WHERE level.value ->> 'level' = v_level::text;
            IF v_projection IS NULL THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_coordinate'); END IF;
            IF v_x >= (v_projection ->> 'columns')::integer
               OR v_y >= (v_projection ->> 'rows')::integer THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_coordinate');
            END IF;
            v_key := 'image-pyramids/' || v_asset.id::text || '/g' || v_generation::text
                || '/tiles/' || v_level::text || '/' || v_x::text || '/' || v_y::text || '.webp';
        END IF;
        IF v_key IS NULL THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_response'); END IF;
        v_policy := aap_assets_v1._image_policy(COALESCE(v_gen.policy, '{}'::jsonb));
        IF v_policy IS NULL THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        v_prefix := COALESCE(v_gen.output_prefix,
            CASE WHEN v_gen.overview_key IS NOT NULL
                THEN pg_catalog.regexp_replace(v_gen.overview_key, 'overview\.webp$', '')
                WHEN v_gen.manifest_key IS NOT NULL
                THEN pg_catalog.regexp_replace(v_gen.manifest_key, 'manifest\.json$', '') END);
        IF v_source ->> 'thumbnail_path' = v_gen.overview_key THEN
            IF v_owner_kind = 'task' THEN
                v_task_reply := aap_labeling_v1.invalidate_task_image_thumbnail(
                    v_owner_id, v_snapshot, v_gen.overview_key
                );
                IF v_task_reply IS NULL
                   OR v_task_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-image-thumbnail/v1'
                   OR pg_catalog.jsonb_typeof(v_task_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
                    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Task thumbnail invalidation rejected';
                END IF;
                IF v_task_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                    CASE v_task_reply #>> '{error,code}'
                        WHEN 'source_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
                        WHEN 'source_changed' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
                        WHEN 'invalid_request' THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
                        ELSE RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'Task thumbnail invalidation failed';
                    END CASE;
                END IF;
                IF v_task_reply #> '{data,applied}' IS DISTINCT FROM 'true'::jsonb THEN
                    RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
                END IF;
            ELSE
                UPDATE public.dataset_items AS item
                SET thumbnail_path = NULL, blurhash = NULL,
                    media_materialized_generation = 0,
                    updated_at = pg_catalog.clock_timestamp()
                WHERE item.id = v_owner_id
                  AND item.thumbnail_path = v_gen.overview_key
                  AND item.media_source_generation = (v_source ->> 'source_generation')::bigint;
                IF NOT FOUND THEN
                    RAISE EXCEPTION USING ERRCODE = '40001', MESSAGE = 'image thumbnail changed during invalidation';
                END IF;
            END IF;
        END IF;
        UPDATE public.image_pyramid_generations AS generation
        SET status = 'failed', error_code = 'object_missing',
            updated_at = pg_catalog.clock_timestamp()
        WHERE generation.asset_id = v_gen.asset_id
          AND generation.generation = v_gen.generation;
        UPDATE public.image_pyramid_assets AS asset
        SET active_generation = NULL,
            updated_at = pg_catalog.clock_timestamp()
        WHERE asset.id = v_asset.id;
        IF v_prefix IS NOT NULL THEN
            PERFORM aap_assets_v1._queue_image_garbage(
                v_prefix, 'object_missing',
                pg_catalog.clock_timestamp()
                    + pg_catalog.make_interval(hours => (v_policy ->> 'gc_grace_hours')::integer)
            );
        END IF;
        v_changed := true;
    END IF;
    v_source_reply := aap_assets_v1._image_owner_source(
        p_actor_id, p_task_id, p_dataset_item_id, false
    );
    IF v_source_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'image source disappeared during invalidation';
    END IF;
    v_updated_source := v_source_reply -> 'data';
    IF NOT aap_assets_v1._image_source_data_valid(v_updated_source) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid image source after invalidation';
    END IF;
    RETURN aap_assets_v1._image_reply(
        pg_catalog.jsonb_build_object('source', v_updated_source, 'changed', v_changed), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
WHEN OTHERS THEN
    RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.invalidate_image_media(uuid, uuid, uuid, jsonb)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.read_image_source(
    p_actor_id uuid, p_task_id uuid, p_dataset_item_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
BEGIN
    IF p_actor_id IS NULL OR ((p_task_id IS NULL) = (p_dataset_item_id IS NULL)) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_reply := aap_assets_v1._image_owner_source(
        p_actor_id, p_task_id, p_dataset_item_id, false
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    IF NOT aap_assets_v1._image_source_data_valid(v_reply -> 'data') THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    RETURN v_reply;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_image_source(uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.observe_image_source(
    p_actor_id uuid,
    p_task_id uuid,
    p_dataset_item_id uuid,
    p_source_snapshot text,
    p_object_identity jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_source jsonb;
    v_owner_kind text;
    v_owner_id uuid;
    v_identity jsonb;
BEGIN
    IF p_actor_id IS NULL OR ((p_task_id IS NULL) = (p_dataset_item_id IS NULL))
       OR p_source_snapshot IS NULL OR p_source_snapshot !~ '^[0-9a-f]{64}$'
       OR NOT aap_assets_v1._media_object_identity_valid(p_object_identity) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_reply := aap_assets_v1._image_actor(p_actor_id, true);
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_reply := aap_assets_v1._image_owner_source(
        p_actor_id, p_task_id, p_dataset_item_id, true
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_source := v_reply -> 'data';
    IF v_source ->> 'source_snapshot' IS DISTINCT FROM p_source_snapshot THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    v_owner_kind := v_source #>> '{owner,kind}';
    BEGIN
        v_owner_id := (v_source #>> '{owner,id}')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END;
    v_identity := aap_assets_v1._image_normalize_identity(p_object_identity);
    v_reply := aap_assets_v1._image_observe_owner(
        v_owner_kind, v_owner_id, p_source_snapshot, v_identity
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_reply := aap_assets_v1._image_owner_source(
        p_actor_id, p_task_id, p_dataset_item_id, false
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    IF NOT aap_assets_v1._image_source_data_valid(v_reply -> 'data') THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    RETURN v_reply;
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.observe_image_source(uuid, uuid, uuid, text, jsonb)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.resolve_internal_image_source(
    p_owner_kind text,
    p_owner_id uuid,
    p_expected_source_snapshot text,
    p_object_identity jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
BEGIN
    IF p_owner_kind IS NULL OR p_owner_kind NOT IN ('dataset_item', 'task')
       OR p_owner_id IS NULL
       OR ((p_expected_source_snapshot IS NULL) <> (p_object_identity IS NULL))
       OR (p_expected_source_snapshot IS NOT NULL
           AND p_expected_source_snapshot !~ '^[0-9a-f]{64}$')
       OR (p_object_identity IS NOT NULL
           AND NOT aap_assets_v1._media_object_identity_valid(p_object_identity)) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    IF p_expected_source_snapshot IS NULL THEN
        v_reply := aap_assets_v1._image_resolve_owner(p_owner_kind, p_owner_id, false);
    ELSE
        v_reply := aap_assets_v1._image_observe_owner(
            p_owner_kind, p_owner_id, p_expected_source_snapshot, p_object_identity
        );
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    IF NOT aap_assets_v1._image_source_data_valid(v_reply -> 'data')
       OR v_reply #>> '{data,owner,kind}' IS DISTINCT FROM p_owner_kind
       OR v_reply #>> '{data,owner,id}' IS DISTINCT FROM p_owner_id::text THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    RETURN v_reply;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.resolve_internal_image_source(text, uuid, text, jsonb)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._image_project_manager(
    p_actor_id uuid, p_project_id uuid, p_lock boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_data jsonb;
BEGIN
    IF p_actor_id IS NULL OR p_project_id IS NULL OR p_lock IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    IF p_lock THEN
        v_reply := aap_workspace_v1.lock_media_project_access(p_actor_id, p_project_id);
    ELSE
        v_reply := aap_workspace_v1.read_media_project_access(p_actor_id, p_project_id);
    END IF;
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        CASE v_reply #>> '{error,code}'
            WHEN 'account_not_active' THEN RETURN aap_assets_v1._image_reply(NULL, 'account_not_active');
            WHEN 'account_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'account_busy');
            WHEN 'invalid_account_role' THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_account_role');
            WHEN 'task_busy' THEN RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
            WHEN 'project_missing' THEN RETURN aap_assets_v1._image_reply(NULL, 'permission_denied');
            ELSE RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END CASE;
    END IF;
    v_data := v_reply -> 'data';
    IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
       OR v_data ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR v_data ->> 'project_id' IS DISTINCT FROM p_project_id::text
       OR pg_catalog.jsonb_typeof(v_data -> 'is_manager') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_data -> 'platform_role') IS DISTINCT FROM 'string' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    IF v_data -> 'is_manager' IS DISTINCT FROM 'true'::jsonb
       OR v_data ->> 'platform_role' NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_assets_v1._image_reply(NULL, 'permission_denied');
    END IF;
    RETURN aap_assets_v1._image_reply(v_data, NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_project_manager(uuid, uuid, boolean)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._image_job_access(
    p_job_id uuid, p_actor_id uuid, p_lock_source boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.asset_image_jobs%ROWTYPE;
    v_actor jsonb;
    v_source_reply jsonb;
    v_source jsonb;
    v_task_id uuid;
    v_access jsonb;
    v_selection jsonb;
    v_item_id uuid;
    v_project_id uuid;
    v_owner_kind text;
    v_owner_ids jsonb;
    v_id jsonb;
BEGIN
    IF p_job_id IS NULL OR p_actor_id IS NULL OR p_lock_source IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_actor := aap_assets_v1._image_actor(p_actor_id, p_lock_source);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_actor; END IF;
    SELECT job.* INTO v_job
    FROM public.asset_image_jobs AS job
    WHERE job.job_id = p_job_id;
    IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'source_missing'); END IF;
    IF v_job.owner_id IS NULL THEN
        v_selection := v_job.spec -> 'selection';
        IF pg_catalog.jsonb_typeof(v_selection) IS DISTINCT FROM 'object'
           OR (v_selection ->> 'owner_kind') NOT IN ('task', 'dataset_item') THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        v_owner_kind := v_selection ->> 'owner_kind';
        IF v_selection -> 'project_id' <> 'null'::jsonb THEN
            BEGIN
                v_project_id := (v_selection ->> 'project_id')::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END;
            v_access := aap_assets_v1._image_project_manager(
                p_actor_id, v_project_id, p_lock_source
            );
            IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_access; END IF;
        ELSIF v_selection -> 'owner_ids' <> '[]'::jsonb THEN
            v_owner_ids := v_selection -> 'owner_ids';
            IF pg_catalog.jsonb_typeof(v_owner_ids) IS DISTINCT FROM 'array'
               OR pg_catalog.jsonb_array_length(v_owner_ids) > 500 THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            FOR v_id IN
                SELECT entry.value
                FROM pg_catalog.jsonb_array_elements(v_owner_ids) AS entry(value)
                ORDER BY (entry.value #>> '{}')::uuid
            LOOP
                BEGIN
                    v_item_id := (v_id #>> '{}')::uuid;
                EXCEPTION WHEN invalid_text_representation THEN
                    RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
                END;
                IF v_owner_kind = 'task' THEN
                    v_source_reply := aap_assets_v1._image_owner_source(
                        p_actor_id, v_item_id, NULL, p_lock_source
                    );
                    IF v_source_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                        RETURN v_source_reply;
                    END IF;
                    v_source := v_source_reply -> 'data';
                    IF v_source -> 'dataset_id' <> 'null'::jsonb THEN
                        RETURN aap_assets_v1._image_reply(NULL, 'owner_changed');
                    END IF;
                ELSE
                    v_access := aap_assets_v1._image_manager_item(
                        p_actor_id, v_item_id, p_lock_source
                    );
                    IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_access; END IF;
                END IF;
            END LOOP;
        ELSIF v_selection -> 'all_owners' = 'true'::jsonb THEN
            IF v_actor #>> '{data,platform_role}' IS DISTINCT FROM 'super_admin' THEN
                RETURN aap_assets_v1._image_reply(NULL, 'permission_denied');
            END IF;
        ELSE
            IF v_actor #>> '{data,platform_role}' NOT IN ('super_admin', 'project_admin') THEN
                RETURN aap_assets_v1._image_reply(NULL, 'permission_denied');
            END IF;
        END IF;
    ELSIF NOT aap_assets_v1._image_source_data_valid(v_job.source_facts) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    ELSIF v_job.source_facts -> 'task_id' <> 'null'::jsonb THEN
        BEGIN
            v_task_id := (v_job.source_facts ->> 'task_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END;
        v_source_reply := aap_assets_v1._image_owner_source(
            p_actor_id, v_task_id, NULL, p_lock_source
        );
        IF v_source_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN v_source_reply;
        END IF;
        v_source := v_source_reply -> 'data';
        IF v_source #>> '{owner,kind}' IS DISTINCT FROM v_job.owner_kind
           OR v_source #>> '{owner,id}' IS DISTINCT FROM v_job.owner_id::text THEN
            RETURN aap_assets_v1._image_reply(NULL, 'owner_changed');
        END IF;
    ELSE
        v_access := aap_assets_v1._image_manager_item(
            p_actor_id, v_job.owner_id, p_lock_source
        );
        IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_access; END IF;
        v_source_reply := aap_assets_v1._image_owner_source(
            p_actor_id, NULL, v_job.owner_id, p_lock_source
        );
        IF v_source_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN v_source_reply;
        END IF;
    END IF;
    RETURN aap_assets_v1._image_reply(
        pg_catalog.jsonb_build_object('actor_id', p_actor_id, 'job_id', p_job_id), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_job_access(uuid, uuid, boolean)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.read_image_summaries(
    p_actor_id uuid, p_selectors jsonb, p_profile_version text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_selector jsonb;
    v_task_id uuid;
    v_item_id uuid;
    v_source_reply jsonb;
    v_source jsonb;
    v_projection jsonb;
    v_selector_json jsonb;
    v_selected_profile text;
    v_items jsonb := '[]'::jsonb;
BEGIN
    IF p_actor_id IS NULL OR pg_catalog.jsonb_typeof(p_selectors) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(p_selectors) > 500
       OR (p_profile_version IS NOT NULL
           AND pg_catalog.char_length(p_profile_version) NOT BETWEEN 1 AND 64) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_selected_profile := COALESCE(p_profile_version, 'pyramid-v1');
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_array_elements(p_selectors) AS row(value)
        WHERE pg_catalog.jsonb_typeof(row.value) IS DISTINCT FROM 'object'
           OR (row.value - ARRAY['task_id', 'dataset_item_id']) IS DISTINCT FROM '{}'::jsonb
           OR ((row.value -> 'task_id' IS NULL) AND (row.value -> 'dataset_item_id' IS NULL))
           OR (row.value -> 'task_id' <> 'null'::jsonb
               AND (pg_catalog.jsonb_typeof(row.value -> 'task_id') IS DISTINCT FROM 'string'
                    OR row.value ->> 'task_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'))
           OR (row.value -> 'dataset_item_id' <> 'null'::jsonb
               AND (pg_catalog.jsonb_typeof(row.value -> 'dataset_item_id') IS DISTINCT FROM 'string'
                    OR row.value ->> 'dataset_item_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'))
           OR ((row.value -> 'task_id' <> 'null'::jsonb)
                = (row.value -> 'dataset_item_id' <> 'null'::jsonb))
    ) THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_array_elements(p_selectors) AS requested(value)
        GROUP BY COALESCE(requested.value ->> 'task_id', ''),
                 COALESCE(requested.value ->> 'dataset_item_id', '')
        HAVING pg_catalog.count(*) > 1
    ) THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    v_actor := aap_assets_v1._image_actor(p_actor_id, false);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_actor; END IF;
    FOR v_selector IN
        SELECT requested.value
        FROM pg_catalog.jsonb_array_elements(p_selectors) WITH ORDINALITY AS requested(value, ordinality)
        ORDER BY requested.ordinality
    LOOP
        v_task_id := CASE WHEN v_selector -> 'task_id' = 'null'::jsonb
            THEN NULL ELSE (v_selector ->> 'task_id')::uuid END;
        v_item_id := CASE WHEN v_selector -> 'dataset_item_id' = 'null'::jsonb
            THEN NULL ELSE (v_selector ->> 'dataset_item_id')::uuid END;
        v_source_reply := aap_assets_v1._image_owner_source(
            p_actor_id, v_task_id, v_item_id, false
        );
        IF v_source_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN v_source_reply;
        END IF;
        v_source := v_source_reply -> 'data';
        IF NOT aap_assets_v1._image_source_data_valid(v_source) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        v_projection := aap_assets_v1._image_pyramid_projection(
            v_source, NULL, v_selected_profile
        );
        IF v_projection IS NULL THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_response'); END IF;
        v_selector_json := pg_catalog.jsonb_build_object(
            'task_id', v_task_id, 'dataset_item_id', v_item_id
        );
        v_items := v_items || pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object(
            'selector', v_selector_json,
            'source', v_source,
            'thumbnail_current', v_source -> 'thumbnail_path' <> 'null'::jsonb
                AND v_source -> 'thumbnail_source_generation' = v_source -> 'source_generation',
            'pyramid', aap_assets_v1._image_pyramid_summary(v_projection)
        ));
    END LOOP;
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object('items', v_items), NULL);
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_image_summaries(uuid, jsonb, text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.read_image_generation(
    p_actor_id uuid, p_task_id uuid, p_dataset_item_id uuid,
    p_generation integer, p_profile_version text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_source_reply jsonb;
    v_source jsonb;
    v_projection jsonb;
    v_selected_profile text;
    v_asset_id uuid;
    v_active_generation integer;
    v_last_accessed_at timestamptz;
    v_generation_policy jsonb;
    v_policy jsonb;
    v_accessed_at timestamptz;
    v_retention_until timestamptz;
    v_asset_prefix text;
    v_generation_prefix text;
    v_attempt_prefix text;
    v_prefix text;
    v_prefixes text[];
    v_garbage public.asset_image_garbage%ROWTYPE;
    v_garbage_error text;
    v_touch_access boolean;
BEGIN
    IF p_actor_id IS NULL OR ((p_task_id IS NULL) = (p_dataset_item_id IS NULL))
       OR (p_generation IS NOT NULL AND p_generation < 1)
       OR (p_profile_version IS NOT NULL
           AND pg_catalog.char_length(p_profile_version) NOT BETWEEN 1 AND 64) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_selected_profile := COALESCE(p_profile_version, 'pyramid-v1');
    v_source_reply := aap_assets_v1._image_owner_source(
        p_actor_id, p_task_id, p_dataset_item_id, false
    );
    IF v_source_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_source_reply; END IF;
    v_source := v_source_reply -> 'data';
    IF NOT aap_assets_v1._image_source_data_valid(v_source) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    v_projection := aap_assets_v1._image_pyramid_projection(
        v_source, p_generation, v_selected_profile
    );
    IF v_projection IS NULL THEN
        RETURN aap_assets_v1._image_reply(
            NULL, CASE WHEN p_generation IS NOT NULL THEN 'asset_missing' ELSE 'invalid_response' END
        );
    END IF;
    IF p_generation IS NOT NULL
       AND v_projection ->> 'status' = 'ready'
       AND v_projection ->> 'generation' = p_generation::text THEN
        BEGIN
            v_asset_id := (v_projection ->> 'asset_id')::uuid;
            SELECT asset.active_generation, generation.last_accessed_at,
                   generation.policy
            INTO v_active_generation, v_last_accessed_at, v_generation_policy
            FROM public.image_pyramid_assets AS asset
            JOIN public.image_pyramid_generations AS generation
              ON generation.asset_id = asset.id
            WHERE asset.id = v_asset_id
              AND asset.profile_version = v_selected_profile
              AND generation.generation = p_generation
              AND generation.status = 'ready'
              AND generation.source_generation = (v_source ->> 'source_generation')::bigint
              AND generation.source_snapshot = v_source ->> 'source_snapshot'
              AND generation.source_identity = aap_assets_v1._image_exec_legacy_identity(
                    v_source -> 'object_identity'
              )
              AND (v_source -> 'width' = 'null'::jsonb
                   OR generation.width = (v_source ->> 'width')::integer)
              AND (v_source -> 'height' = 'null'::jsonb
                   OR generation.height = (v_source ->> 'height')::integer)
            FOR UPDATE OF asset, generation NOWAIT;
            IF NOT FOUND THEN
                RETURN aap_assets_v1._image_reply(NULL, 'asset_missing');
            END IF;

            v_policy := aap_assets_v1._image_policy(
                COALESCE(v_generation_policy, '{}'::jsonb)
            );
            IF v_policy IS NULL THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            v_asset_prefix := 'image-pyramids/' || v_asset_id::text || '/';
            v_generation_prefix := v_asset_prefix || 'g' || p_generation::text || '/';
            v_attempt_prefix := pg_catalog.regexp_replace(
                v_projection ->> 'manifest_key', 'manifest\.json$', ''
            );
            IF v_attempt_prefix IS NULL
               OR (v_attempt_prefix <> v_generation_prefix
                   AND (pg_catalog.left(v_attempt_prefix, pg_catalog.char_length(v_generation_prefix))
                            IS DISTINCT FROM v_generation_prefix
                        OR NOT aap_assets_v1._image_garbage_prefix_valid(v_attempt_prefix))) THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            v_prefixes := ARRAY[v_asset_prefix, v_generation_prefix, v_attempt_prefix];
            v_touch_access := v_last_accessed_at IS NULL
                OR v_last_accessed_at < pg_catalog.clock_timestamp() - interval '10 minutes';
            IF v_touch_access THEN
                v_accessed_at := pg_catalog.clock_timestamp();
            ELSE
                v_accessed_at := v_last_accessed_at;
            END IF;
            v_retention_until := v_accessed_at + pg_catalog.make_interval(
                hours => (v_policy ->> 'gc_grace_hours')::integer
            );
            BEGIN
                FOR v_prefix IN
                    SELECT DISTINCT candidate.prefix
                    FROM pg_catalog.unnest(v_prefixes) AS candidate(prefix)
                    ORDER BY candidate.prefix
                LOOP
                    IF NOT pg_catalog.pg_try_advisory_xact_lock(
                        pg_catalog.hashtextextended('aap-image-garbage:' || v_prefix, 0)
                    ) THEN
                        v_garbage_error := 'operation_busy';
                        RAISE lock_not_available USING MESSAGE = 'image garbage scope is busy';
                    END IF;
                END LOOP;
                FOR v_garbage IN
                    SELECT garbage.*
                    FROM public.asset_image_garbage AS garbage
                    WHERE garbage.prefix = ANY(v_prefixes)
                    ORDER BY garbage.prefix
                    FOR UPDATE NOWAIT
                LOOP
                    IF v_garbage.completed_at IS NOT NULL
                       OR v_garbage.last_ack_token IS NOT NULL THEN
                        v_garbage_error := 'asset_missing';
                        RAISE lock_not_available USING MESSAGE = 'image generation was collected';
                    ELSIF v_garbage.lease_token IS NOT NULL THEN
                        v_garbage_error := 'source_busy';
                        RAISE lock_not_available USING MESSAGE = 'image generation cleanup is active';
                    END IF;
                END LOOP;
                IF v_touch_access THEN
                    UPDATE public.image_pyramid_generations AS generation
                    SET last_accessed_at = v_accessed_at
                    WHERE generation.asset_id = v_asset_id
                      AND generation.generation = p_generation;
                END IF;
                IF v_active_generation IS DISTINCT FROM p_generation THEN
                    PERFORM aap_assets_v1._queue_image_garbage(
                        v_generation_prefix, 'generation_retired', v_retention_until
                    );
                END IF;
                UPDATE public.asset_image_garbage AS garbage
                SET not_before = GREATEST(garbage.not_before, v_retention_until)
                WHERE garbage.prefix = ANY(v_prefixes)
                  AND garbage.completed_at IS NULL
                  AND garbage.lease_token IS NULL
                  AND garbage.not_before < v_retention_until;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN aap_assets_v1._image_reply(
                    NULL, COALESCE(v_garbage_error, 'operation_busy')
                );
            END;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END;
    END IF;
    RETURN aap_assets_v1._image_reply(
        pg_catalog.jsonb_build_object('source', v_source, 'pyramid', v_projection), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_image_generation(uuid, uuid, uuid, integer, text)
    FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.read_image_work(p_actor_id uuid, p_job_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_access jsonb;
    v_job public.asset_image_jobs%ROWTYPE;
BEGIN
    IF p_actor_id IS NULL OR p_job_id IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_access := aap_assets_v1._image_job_access(p_job_id, p_actor_id, false);
    IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_access; END IF;
    SELECT job.* INTO v_job FROM public.asset_image_jobs AS job WHERE job.job_id = p_job_id;
    IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'source_missing'); END IF;
    RETURN aap_assets_v1._image_reply(aap_assets_v1._image_work_status(v_job, true), NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_image_work(uuid, uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.read_internal_image_work(p_job_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.asset_image_jobs%ROWTYPE;
BEGIN
    IF p_job_id IS NULL THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    SELECT job.* INTO v_job FROM public.asset_image_jobs AS job WHERE job.job_id = p_job_id;
    IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'source_missing'); END IF;
    RETURN aap_assets_v1._image_reply(aap_assets_v1._image_work_status(v_job, false), NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_internal_image_work(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.read_image_usage()
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_source_bytes numeric;
    v_thumbnail_bytes numeric;
    v_pyramid_bytes numeric;
    v_pending bigint;
    v_running bigint;
    v_failed bigint;
    v_garbage bigint;
BEGIN
    SELECT COALESCE(pg_catalog.sum(item.file_size), 0)
    INTO v_source_bytes
    FROM public.dataset_items AS item
    WHERE item.file_type = 'image';
    SELECT COALESCE(pg_catalog.sum((job.result #>> '{artifact,byte_size}')::numeric), 0)
    INTO v_thumbnail_bytes
    FROM public.asset_image_jobs AS job
    WHERE job.kind = 'thumbnail' AND job.status = 'ready'
      AND pg_catalog.jsonb_typeof(job.result -> 'artifact') = 'object'
      AND COALESCE(job.result #>> '{artifact,byte_size}', '') ~ '^(0|[1-9][0-9]*)$';
    SELECT COALESCE(pg_catalog.sum(generation.retained_bytes), 0)
    INTO v_pyramid_bytes
    FROM public.image_pyramid_assets AS asset
    JOIN public.image_pyramid_generations AS generation
      ON generation.asset_id = asset.id
     AND generation.generation = asset.active_generation
    WHERE generation.status = 'ready';
    SELECT
        pg_catalog.count(*) FILTER (WHERE job.status = 'pending'),
        pg_catalog.count(*) FILTER (WHERE job.status = 'running'),
        pg_catalog.count(*) FILTER (WHERE job.status = 'failed')
    INTO v_pending, v_running, v_failed
    FROM public.asset_image_jobs AS job;
    SELECT pg_catalog.count(*) INTO v_garbage
    FROM public.asset_image_garbage AS garbage
    WHERE garbage.completed_at IS NULL;
    IF GREATEST(v_source_bytes, v_thumbnail_bytes, v_pyramid_bytes) > 9223372036854775807 THEN
        RETURN aap_assets_v1._image_reply(NULL, 'resource_budget_exceeded');
    END IF;
    RETURN aap_assets_v1._image_reply(
        pg_catalog.jsonb_build_object(
            'source_bytes', v_source_bytes::bigint,
            'thumbnail_bytes', v_thumbnail_bytes::bigint,
            'pyramid_bytes', v_pyramid_bytes::bigint,
            'pending', v_pending,
            'running', v_running,
            'failed', v_failed,
            'garbage_pending', v_garbage
        ), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_image_usage() FROM PUBLIC;
-- aap-statement-break
