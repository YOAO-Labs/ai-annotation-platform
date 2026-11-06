CREATE OR REPLACE FUNCTION aap_assets_v1.scan_image_backfill(
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
                  AND aap_assets_v1.fixture_native_scan_visible(item.dataset_id,pg_catalog.pg_backend_pid())
            )
        ) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'source_missing');
        END IF;
        WITH candidates AS MATERIALIZED (
            SELECT item.id,
                   aap_assets_v1._image_item_source_snapshot(item) AS source_snapshot
            FROM public.dataset_items AS item
            WHERE item.file_type = 'image'
              AND aap_assets_v1.fixture_native_scan_visible(item.dataset_id,pg_catalog.pg_backend_pid())
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
CREATE OR REPLACE FUNCTION aap_assets_v1.scan_video_backfill(
    p_selection jsonb,
    p_after_item_id uuid,
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
    v_dataset_id uuid;
    v_item_ids uuid[];
    v_all_items boolean;
    v_cutoff timestamptz;
    v_total bigint;
    v_page jsonb;
    v_count integer;
    v_next uuid;
    v_complete boolean;
    v_digest text;
    v_snapshot jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_selection) IS DISTINCT FROM 'object'
       OR (p_selection - ARRAY['dataset_id', 'dataset_item_ids', 'all_items',
                               'only_missing', 'accepted_before']) IS DISTINCT FROM '{}'::jsonb
       OR (p_selection ? 'only_missing'
           AND pg_catalog.jsonb_typeof(p_selection -> 'only_missing') IS DISTINCT FROM 'boolean')
       OR (p_selection ? 'dataset_id'
           AND pg_catalog.jsonb_typeof(p_selection -> 'dataset_id') NOT IN ('string', 'null'))
       OR (p_selection ? 'dataset_item_ids'
           AND pg_catalog.jsonb_typeof(p_selection -> 'dataset_item_ids') IS DISTINCT FROM 'array')
       OR (p_selection ? 'all_items'
           AND pg_catalog.jsonb_typeof(p_selection -> 'all_items') IS DISTINCT FROM 'boolean')
       OR (p_selection ? 'accepted_before'
           AND pg_catalog.jsonb_typeof(p_selection -> 'accepted_before') NOT IN ('string', 'null'))
       OR p_page_size IS NULL OR p_page_size NOT BETWEEN 1 AND 500 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    IF (CASE WHEN pg_catalog.jsonb_typeof(p_selection -> 'dataset_id') = 'string' THEN 1 ELSE 0 END)
       + (CASE WHEN pg_catalog.jsonb_typeof(p_selection -> 'dataset_item_ids') = 'array'
                    AND pg_catalog.jsonb_array_length(p_selection -> 'dataset_item_ids') > 0 THEN 1 ELSE 0 END)
       + (CASE WHEN p_selection -> 'all_items' = 'true'::jsonb THEN 1 ELSE 0 END) <> 1 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    IF p_selection -> 'dataset_id' IS NOT NULL
       AND pg_catalog.jsonb_typeof(p_selection -> 'dataset_id') = 'string' THEN
        IF (p_selection ->> 'dataset_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END IF;
        BEGIN
            v_dataset_id := (p_selection ->> 'dataset_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END;
    END IF;
    IF pg_catalog.jsonb_typeof(p_selection -> 'dataset_item_ids') = 'array' THEN
        IF pg_catalog.jsonb_array_length(p_selection -> 'dataset_item_ids') > 500
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_array_elements(p_selection -> 'dataset_item_ids') AS entry(value)
               WHERE pg_catalog.jsonb_typeof(entry.value) IS DISTINCT FROM 'string'
                  OR COALESCE(entry.value #>> '{}', '') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           )
           OR (SELECT count(DISTINCT entry.value #>> '{}')
               FROM pg_catalog.jsonb_array_elements(p_selection -> 'dataset_item_ids') AS entry(value))
              <> pg_catalog.jsonb_array_length(p_selection -> 'dataset_item_ids') THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END IF;
        BEGIN
            SELECT COALESCE(pg_catalog.array_agg(DISTINCT (entry.value #>> '{}')::uuid), ARRAY[]::uuid[])
            INTO v_item_ids
            FROM pg_catalog.jsonb_array_elements(p_selection -> 'dataset_item_ids') AS entry(value);
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.unnest(v_item_ids) AS requested(item_id)
            WHERE NOT EXISTS (
                SELECT 1 FROM public.dataset_items AS item
                WHERE item.id = requested.item_id AND item.file_type = 'video'
                  AND aap_assets_v1.fixture_native_scan_visible(item.dataset_id,pg_catalog.pg_backend_pid())
            )
        ) THEN
            RETURN aap_assets_v1._media_reply(NULL, 'source_missing');
        END IF;
    END IF;
    IF pg_catalog.jsonb_typeof(p_selection -> 'all_items') = 'boolean' THEN
        v_all_items := p_selection -> 'all_items' = 'true'::jsonb;
    ELSE
        v_all_items := false;
    END IF;
    v_cutoff := pg_catalog.transaction_timestamp();
    IF p_selection ? 'accepted_before'
       AND pg_catalog.jsonb_typeof(p_selection -> 'accepted_before') = 'string' THEN
        IF (p_selection ->> 'accepted_before') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T.*(Z|[+-][0-9]{2}:[0-9]{2})$' THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END IF;
        BEGIN
            v_cutoff := (p_selection ->> 'accepted_before')::timestamptz;
        EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
            RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
        END;
    END IF;
    v_snapshot := aap_assets_v1._media_backfill_snapshot(p_selection, v_cutoff);
    IF v_snapshot IS NULL
       OR pg_catalog.jsonb_typeof(v_snapshot) IS DISTINCT FROM 'object'
       OR COALESCE(v_snapshot ->> 'snapshot_digest', '') !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(v_snapshot -> 'total') IS DISTINCT FROM 'number'
       OR COALESCE(v_snapshot ->> 'total', '') !~ '^(0|[1-9][0-9]*)$' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    v_total := (v_snapshot ->> 'total')::bigint;
    v_digest := v_snapshot ->> 'snapshot_digest';
    SELECT COALESCE(
        pg_catalog.jsonb_agg(aap_assets_v1._media_source_json(page.item) ORDER BY page.id),
        '[]'::jsonb
    )
    INTO v_page
    FROM (
        SELECT item AS item, item.id
        FROM public.dataset_items AS item
        WHERE item.file_type = 'video'
          AND aap_assets_v1.fixture_native_scan_visible(item.dataset_id,pg_catalog.pg_backend_pid())
          AND item.created_at <= v_cutoff
          AND (p_after_item_id IS NULL OR item.id > p_after_item_id)
          AND (
              (v_dataset_id IS NOT NULL AND item.dataset_id = v_dataset_id)
              OR (v_item_ids IS NOT NULL AND item.id = ANY(v_item_ids))
              OR v_all_items
          )
        ORDER BY item.id
        LIMIT p_page_size
    ) AS page;
    v_count := pg_catalog.jsonb_array_length(v_page);
    IF v_count > 0 THEN
        v_next := (v_page -> (v_count - 1) ->> 'dataset_item_id')::uuid;
    ELSE
        v_next := NULL;
    END IF;
    v_complete := NOT EXISTS (
        SELECT 1 FROM public.dataset_items AS item
        WHERE item.file_type = 'video'
          AND aap_assets_v1.fixture_native_scan_visible(item.dataset_id,pg_catalog.pg_backend_pid())
          AND item.created_at <= v_cutoff
          AND (p_after_item_id IS NULL OR item.id > p_after_item_id)
          AND (v_next IS NULL OR item.id > v_next)
          AND (
              (v_dataset_id IS NOT NULL AND item.dataset_id = v_dataset_id)
              OR (v_item_ids IS NOT NULL AND item.id = ANY(v_item_ids))
              OR v_all_items
          )
    );
    RETURN aap_assets_v1._media_reply(
        pg_catalog.jsonb_build_object(
            'items', v_page,
            'snapshot_digest', v_digest,
            'total', v_total,
            'accepted_before', v_cutoff,
            'after_item_id', p_after_item_id,
            'next_item_id', CASE WHEN v_complete THEN NULL ELSE v_next END,
            'complete', v_complete
        ),
        NULL
    );
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1._media_backfill_snapshot(
    p_selection jsonb,
    p_accepted_before timestamptz
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_item public.dataset_items%ROWTYPE;
    v_selection jsonb;
    v_spec jsonb;
    v_hash bytea;
    v_total bigint := 0;
BEGIN
    IF p_accepted_before IS NULL THEN
        RETURN NULL;
    END IF;
    v_spec := pg_catalog.jsonb_build_object('kind', 'video_backfill', 'selection', p_selection);
    IF NOT aap_assets_v1._media_work_spec_valid(v_spec) THEN
        RETURN NULL;
    END IF;
    IF p_selection ? 'accepted_before'
       AND p_selection ->> 'accepted_before' IS NOT NULL
       AND (p_selection ->> 'accepted_before')::timestamptz IS DISTINCT FROM p_accepted_before THEN
        RETURN NULL;
    END IF;
    v_selection := pg_catalog.jsonb_build_object(
        'dataset_id', COALESCE(p_selection -> 'dataset_id', 'null'::jsonb),
        'dataset_item_ids', COALESCE(p_selection -> 'dataset_item_ids', '[]'::jsonb),
        'all_items', COALESCE(p_selection -> 'all_items', 'false'::jsonb),
        'only_missing', COALESCE(p_selection -> 'only_missing', 'true'::jsonb)
    );
    v_hash := pg_catalog.sha256(pg_catalog.convert_to(
        'aap-assets-media-backfill/v1:' || v_selection::text || ':'
            || p_accepted_before::text,
        'UTF8'
    ));
    FOR v_item IN
        SELECT item.*
        FROM public.dataset_items AS item
        WHERE item.file_type = 'video'
          AND aap_assets_v1.fixture_native_scan_visible(item.dataset_id,pg_catalog.pg_backend_pid())
          AND item.created_at <= p_accepted_before
          AND (
              (v_selection -> 'dataset_id' <> 'null'::jsonb
               AND item.dataset_id = (v_selection ->> 'dataset_id')::uuid)
              OR EXISTS (
                  SELECT 1
                  FROM pg_catalog.jsonb_array_elements_text(
                      v_selection -> 'dataset_item_ids'
                  ) AS selected(item_id)
                  WHERE selected.item_id::uuid = item.id
              )
              OR v_selection -> 'all_items' = 'true'::jsonb
          )
        ORDER BY item.id
    LOOP
        v_hash := pg_catalog.sha256(
            v_hash || pg_catalog.convert_to(
                v_item.id::text || ':' || aap_assets_v1._media_source_snapshot(v_item),
                'UTF8'
            )
        );
        v_total := v_total + 1;
    END LOOP;
    RETURN pg_catalog.jsonb_build_object(
        'snapshot_digest', pg_catalog.encode(v_hash, 'hex'),
        'total', v_total
    );
EXCEPTION
    WHEN invalid_text_representation OR datetime_field_overflow THEN
        RETURN NULL;
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.advance_video_backfill(
    p_job_id uuid,
    p_generation bigint,
    p_lease_token uuid,
    p_batch_size integer
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_job public.asset_media_jobs%ROWTYPE;
    v_item public.dataset_items%ROWTYPE;
    v_snapshot jsonb;
    v_check_snapshot jsonb;
    v_progress jsonb;
    v_child_spec jsonb;
    v_enqueue jsonb;
    v_saved_result jsonb;
    v_error text;
    v_next_cursor uuid;
    v_scheduled bigint;
    v_skipped bigint;
    v_page_scheduled bigint := 0;
    v_page_skipped bigint := 0;
    v_total bigint;
    v_only_missing boolean;
    v_has_more boolean := false;
    v_complete boolean;
    v_data jsonb;
    v_final_result jsonb;
    v_item_snapshot text;
BEGIN
    IF p_job_id IS NULL OR p_generation IS NULL OR p_generation < 1
       OR p_lease_token IS NULL OR p_batch_size NOT BETWEEN 1 AND 500 THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT job.* INTO v_job
        FROM public.asset_media_jobs AS job
        WHERE job.job_id = p_job_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._media_reply(NULL, 'operation_busy');
    END;
    IF NOT FOUND OR v_job.kind <> 'video_backfill' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'lease_lost');
    END IF;
    v_progress := COALESCE(v_job.progress, '{}'::jsonb);
    IF v_progress ->> 'last_advance_lease_token' = p_lease_token::text
       AND v_progress ->> 'last_advance_batch_size' = p_batch_size::text
       AND pg_catalog.jsonb_typeof(v_progress -> 'last_advance_result') = 'object' THEN
        RETURN aap_assets_v1._media_reply(v_progress -> 'last_advance_result', NULL);
    END IF;
    IF v_job.status <> 'running'
       OR v_job.generation <> p_generation
       OR v_job.lease_token IS DISTINCT FROM p_lease_token
       OR v_job.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._media_reply(NULL, 'lease_lost');
    END IF;
    v_snapshot := aap_assets_v1._media_backfill_snapshot(
        v_job.backfill_selection,
        v_job.backfill_cutoff
    );
    IF pg_catalog.jsonb_typeof(v_snapshot) IS DISTINCT FROM 'object'
       OR v_snapshot ->> 'snapshot_digest' !~ '^[0-9a-f]{64}$'
       OR v_snapshot ->> 'total' IS NULL THEN
        RETURN aap_assets_v1._media_reply(NULL, 'invalid_response');
    END IF;
    v_total := (v_snapshot ->> 'total')::bigint;
    v_only_missing := COALESCE(
        v_job.backfill_selection -> 'only_missing' = 'true'::jsonb,
        true
    );
    IF v_progress ? 'snapshot_digest'
       AND (v_progress ->> 'snapshot_digest' IS DISTINCT FROM v_snapshot ->> 'snapshot_digest'
            OR (v_progress ->> 'total')::bigint IS DISTINCT FROM v_total) THEN
        UPDATE public.asset_media_jobs AS job
        SET status = 'canceled', lease_token = NULL, lease_expires_at = NULL,
            last_lease_token = p_lease_token, last_outcome = 'source_changed',
            completed_at = pg_catalog.clock_timestamp(),
            updated_at = pg_catalog.clock_timestamp()
        WHERE job.job_id = p_job_id;
        RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
    END IF;
    IF NOT (v_progress ? 'snapshot_digest') THEN
        v_progress := v_progress || pg_catalog.jsonb_build_object(
            'snapshot_digest', v_snapshot ->> 'snapshot_digest',
            'total', v_total,
            'scheduled', 0,
            'skipped', 0
        );
    END IF;
    v_scheduled := COALESCE((v_progress ->> 'scheduled')::bigint, 0);
    v_skipped := COALESCE((v_progress ->> 'skipped')::bigint, 0);
    v_next_cursor := v_job.backfill_cursor;
    BEGIN
        FOR v_item IN
            SELECT item.*
            FROM public.dataset_items AS item
            WHERE item.file_type = 'video'
              AND aap_assets_v1.fixture_native_scan_visible(item.dataset_id,pg_catalog.pg_backend_pid())
              AND item.created_at <= v_job.backfill_cutoff
              AND (v_job.backfill_cursor IS NULL OR item.id > v_job.backfill_cursor)
              AND (
                  (v_job.backfill_selection -> 'dataset_id' <> 'null'::jsonb
                   AND item.dataset_id = (v_job.backfill_selection ->> 'dataset_id')::uuid)
                  OR EXISTS (
                      SELECT 1
                      FROM pg_catalog.jsonb_array_elements_text(
                          v_job.backfill_selection -> 'dataset_item_ids'
                      ) AS selected(item_id)
                      WHERE selected.item_id::uuid = item.id
                  )
                  OR v_job.backfill_selection -> 'all_items' = 'true'::jsonb
              )
            ORDER BY item.id
            LIMIT p_batch_size
        LOOP
            v_next_cursor := v_item.id;
            IF v_only_missing
               AND v_item.media_materialized_generation >= v_item.media_source_generation THEN
                v_page_skipped := v_page_skipped + 1;
                CONTINUE;
            END IF;
            v_item_snapshot := aap_assets_v1._media_source_snapshot(v_item);
            v_child_spec := pg_catalog.jsonb_build_object(
                'kind', 'video_metadata',
                'generate_playback', true,
                'generate_poster', true,
                'probe_timetable', true,
                'expected_source_snapshot', v_item_snapshot
            );
            v_enqueue := aap_assets_v1._media_enqueue(
                NULL,
                NULL,
                v_item.id,
                v_child_spec,
                NOT v_only_missing
            );
            IF v_enqueue -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                v_error := v_enqueue #>> '{error,code}';
                IF v_error IN ('source_busy', 'operation_busy') THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'media_source_busy';
                END IF;
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'media_source_changed';
            END IF;
            IF v_enqueue #>> '{data,work,status}' IN ('pending', 'running') THEN
                v_page_scheduled := v_page_scheduled + 1;
            ELSE
                v_page_skipped := v_page_skipped + 1;
            END IF;
        END LOOP;
        v_check_snapshot := aap_assets_v1._media_backfill_snapshot(
            v_job.backfill_selection,
            v_job.backfill_cutoff
        );
        IF v_check_snapshot IS NULL
           OR v_check_snapshot ->> 'snapshot_digest'
                IS DISTINCT FROM v_snapshot ->> 'snapshot_digest'
           OR (v_check_snapshot ->> 'total')::bigint IS DISTINCT FROM v_total THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'media_source_changed';
        END IF;
        SELECT EXISTS (
            SELECT 1
            FROM public.dataset_items AS item
            WHERE item.file_type = 'video'
              AND aap_assets_v1.fixture_native_scan_visible(item.dataset_id,pg_catalog.pg_backend_pid())
              AND item.created_at <= v_job.backfill_cutoff
              AND (v_next_cursor IS NULL OR item.id > v_next_cursor)
              AND (
                  (v_job.backfill_selection -> 'dataset_id' <> 'null'::jsonb
                   AND item.dataset_id = (v_job.backfill_selection ->> 'dataset_id')::uuid)
                  OR EXISTS (
                      SELECT 1
                      FROM pg_catalog.jsonb_array_elements_text(
                          v_job.backfill_selection -> 'dataset_item_ids'
                      ) AS selected(item_id)
                      WHERE selected.item_id::uuid = item.id
                  )
                  OR v_job.backfill_selection -> 'all_items' = 'true'::jsonb
              )
        ) INTO v_has_more;
        v_complete := NOT v_has_more;
        v_scheduled := v_scheduled + v_page_scheduled;
        v_skipped := v_skipped + v_page_skipped;
        v_data := pg_catalog.jsonb_build_object(
            'job_id', p_job_id,
            'generation', p_generation,
            'lease_token', p_lease_token,
            'scheduled', v_scheduled,
            'skipped', v_skipped,
            'complete', v_complete,
            'next_item_id', v_next_cursor
        );
        v_progress := v_progress || pg_catalog.jsonb_build_object(
            'scheduled', v_scheduled,
            'skipped', v_skipped,
            'last_advance_lease_token', p_lease_token,
            'last_advance_batch_size', p_batch_size,
            'last_advance_result', v_data
        );
        UPDATE public.asset_media_jobs AS job
        SET status = CASE WHEN v_complete THEN 'ready' ELSE 'pending' END,
            backfill_cursor = v_next_cursor,
            progress = v_progress,
            lease_token = NULL,
            lease_expires_at = NULL,
            last_lease_token = p_lease_token,
            last_outcome = 'published',
            completed_at = CASE WHEN v_complete THEN pg_catalog.clock_timestamp() END,
            updated_at = pg_catalog.clock_timestamp()
        WHERE job.job_id = p_job_id;
        v_final_result := aap_assets_v1._media_reply(v_data, NULL);
    EXCEPTION
        WHEN SQLSTATE 'P0001' THEN
            v_error := SQLERRM;
    END;
    IF v_error = 'media_source_busy' THEN
        RETURN aap_assets_v1._media_reply(NULL, 'source_busy');
    ELSIF v_error = 'media_source_changed' THEN
        UPDATE public.asset_media_jobs AS job
        SET status = 'canceled', lease_token = NULL, lease_expires_at = NULL,
            last_lease_token = p_lease_token, last_outcome = 'source_changed',
            completed_at = pg_catalog.clock_timestamp(),
            updated_at = pg_catalog.clock_timestamp()
        WHERE job.job_id = p_job_id;
        RETURN aap_assets_v1._media_reply(NULL, 'source_changed');
    END IF;
    RETURN v_final_result;
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_labeling_v1.scan_image_task_sources(
    p_selection jsonb,
    p_after_id uuid,
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
    v_owner_ids uuid[];
    v_project_id uuid;
    v_all_owners boolean;
    v_cutoff timestamptz;
    v_total bigint;
    v_digest text;
    v_items jsonb;
    v_more boolean;
    v_next_id uuid;
BEGIN
    IF pg_catalog.jsonb_typeof(p_selection) IS DISTINCT FROM 'object'
       OR (p_selection - ARRAY[
            'owner_kind', 'dataset_id', 'project_id', 'owner_ids',
            'all_owners', 'purpose', 'only_missing', 'accepted_before'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_selection ?& ARRAY[
            'owner_kind', 'dataset_id', 'project_id', 'owner_ids',
            'all_owners', 'purpose', 'only_missing', 'accepted_before'
       ])
       OR p_selection ->> 'owner_kind' IS DISTINCT FROM 'task'
       OR p_selection -> 'dataset_id' <> 'null'::jsonb
       OR pg_catalog.jsonb_typeof(p_selection -> 'project_id') NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(p_selection -> 'owner_ids') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(p_selection -> 'owner_ids') > 500
       OR pg_catalog.jsonb_typeof(p_selection -> 'all_owners') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(p_selection -> 'only_missing') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(p_selection -> 'purpose') IS DISTINCT FROM 'string'
       OR p_selection ->> 'purpose' NOT IN ('dimensions', 'thumbnail', 'pyramid')
       OR pg_catalog.jsonb_typeof(p_selection -> 'accepted_before') IS DISTINCT FROM 'string'
       OR p_selection ->> 'accepted_before' !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T.*(Z|[+-][0-9]{2}:[0-9]{2})$'
       OR p_page_size IS NULL OR p_page_size NOT BETWEEN 1 AND 500 THEN
        RETURN aap_labeling_v1._image_source_reply(NULL, 'invalid_request');
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_array_elements(p_selection -> 'owner_ids') AS entry(value)
        WHERE pg_catalog.jsonb_typeof(entry.value) IS DISTINCT FROM 'string'
           OR entry.value #>> '{}' !~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
    ) OR (
        SELECT pg_catalog.count(DISTINCT (entry.value #>> '{}')::uuid)
        FROM pg_catalog.jsonb_array_elements(p_selection -> 'owner_ids') AS entry(value)
    ) <> pg_catalog.jsonb_array_length(p_selection -> 'owner_ids') THEN
        RETURN aap_labeling_v1._image_source_reply(NULL, 'invalid_request');
    END IF;
    IF ((p_selection -> 'project_id' <> 'null'::jsonb)::integer
        + (pg_catalog.jsonb_array_length(p_selection -> 'owner_ids') > 0)::integer
        + (p_selection ->> 'all_owners')::boolean::integer) <> 1 THEN
        RETURN aap_labeling_v1._image_source_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        v_cutoff := (p_selection ->> 'accepted_before')::timestamptz;
        IF p_selection -> 'project_id' <> 'null'::jsonb THEN
            v_project_id := (p_selection ->> 'project_id')::uuid;
        END IF;
        SELECT COALESCE(pg_catalog.array_agg((entry.value #>> '{}')::uuid), ARRAY[]::uuid[])
        INTO v_owner_ids
        FROM pg_catalog.jsonb_array_elements(p_selection -> 'owner_ids') AS entry(value);
    EXCEPTION WHEN invalid_text_representation OR invalid_datetime_format OR datetime_field_overflow THEN
        RETURN aap_labeling_v1._image_source_reply(NULL, 'invalid_request');
    END;
    v_all_owners := (p_selection ->> 'all_owners')::boolean;
    IF pg_catalog.cardinality(v_owner_ids) > 0 AND EXISTS (
        SELECT 1 FROM pg_catalog.unnest(v_owner_ids) AS requested(task_id)
        WHERE NOT EXISTS (
            SELECT 1 FROM public.tasks AS task
            WHERE task.id = requested.task_id
              AND aap_assets_v1.fixture_legacy_task_scan_visible(task.id)
              AND task.file_type = 'image'
              AND task.dataset_item_id IS NULL
              AND task.status IS NOT NULL
              AND task.status <> 'uploading'
              AND task.created_at <= v_cutoff
        )
    ) THEN
        RETURN aap_labeling_v1._image_source_reply(NULL, 'source_missing');
    END IF;
    WITH candidates AS MATERIALIZED (
        SELECT task.id,
               aap_labeling_v1._image_source_snapshot_fields(
                   task.id,
                   task.created_at,
                   task.project_id,
                   task.dataset_item_id,
                   task.file_type,
                   task.file_path,
                   task.media_source_generation
               ) AS source_snapshot
        FROM public.tasks AS task
        WHERE task.file_type = 'image'
          AND aap_assets_v1.fixture_legacy_task_scan_visible(task.id)
          AND task.dataset_item_id IS NULL
          AND task.status IS NOT NULL
          AND task.status <> 'uploading'
          AND task.created_at <= v_cutoff
          AND (
              (v_project_id IS NOT NULL AND task.project_id = v_project_id)
              OR (pg_catalog.cardinality(v_owner_ids) > 0 AND task.id = ANY(v_owner_ids))
              OR v_all_owners
          )
    ), snapshot AS (
        SELECT pg_catalog.count(*)::bigint AS total,
               pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                   pg_catalog.jsonb_build_object(
                       'accepted_before', v_cutoff AT TIME ZONE 'UTC',
                       'items', COALESCE(pg_catalog.jsonb_agg(
                           pg_catalog.jsonb_build_object(
                               'task_id', candidates.id,
                               'source_snapshot', candidates.source_snapshot
                           ) ORDER BY candidates.id
                       ), '[]'::jsonb)
                   )::text,
                   'UTF8'
               )), 'hex') AS snapshot_digest
        FROM candidates
    ), ranked AS (
        SELECT task.id,
               task.project_id,
               task.dataset_item_id,
               task.file_type,
               task.file_path,
               task.file_name,
               task.created_at,
               task.media_source_generation,
               task.media_object_identity,
               task.media_source_drifted,
               task.thumbnail_path,
               task.blurhash,
               candidates.source_snapshot AS image_source_snapshot,
               pg_catalog.row_number() OVER (ORDER BY task.id) AS row_number
        FROM public.tasks AS task
        JOIN candidates ON candidates.id = task.id
        WHERE p_after_id IS NULL OR task.id > p_after_id
    ), page AS (
        SELECT ranked.id,
               ranked.project_id,
               ranked.dataset_item_id,
               ranked.file_type,
               ranked.file_path,
               ranked.file_name,
               ranked.created_at,
               ranked.media_source_generation,
               ranked.media_object_identity,
               ranked.media_source_drifted,
               ranked.thumbnail_path,
               ranked.blurhash,
               ranked.image_source_snapshot,
               ranked.row_number
        FROM ranked
        WHERE ranked.row_number <= p_page_size + 1
    )
    SELECT snapshot.total,
           snapshot.snapshot_digest,
           COALESCE(pg_catalog.jsonb_agg(
               pg_catalog.jsonb_build_object(
                   'task_id', page.id,
                   'project_id', page.project_id,
                   'dataset_item_id', page.dataset_item_id,
                   'file_type', page.file_type,
                   'file_path', page.file_path,
                   'file_name', page.file_name,
                   'created_at', page.created_at,
                   'source_generation', page.media_source_generation,
                   'source_snapshot', page.image_source_snapshot,
                   'object_identity', page.media_object_identity,
                   'source_drifted', page.media_source_drifted,
                   'upload_ready', true,
                   'thumbnail_path', page.thumbnail_path,
                   'blurhash', page.blurhash,
                   'thumbnail_source_generation', CASE
                       WHEN page.thumbnail_path IS NOT NULL
                        AND aap_labeling_v1._image_thumbnail_key_valid(page.thumbnail_path)
                        AND page.media_object_identity IS NOT NULL
                        AND NOT page.media_source_drifted
                           THEN page.media_source_generation
                       ELSE NULL END
               ) ORDER BY page.id
           ) FILTER (WHERE page.row_number <= p_page_size), '[]'::jsonb),
           COALESCE(pg_catalog.bool_or(page.row_number > p_page_size), false),
           (pg_catalog.array_agg(page.id) FILTER (WHERE page.row_number = p_page_size))[1]
    INTO v_total, v_digest, v_items, v_more, v_next_id
    FROM snapshot
    LEFT JOIN page ON true
    GROUP BY snapshot.total, snapshot.snapshot_digest;
    RETURN aap_labeling_v1._image_source_reply(
        pg_catalog.jsonb_build_object(
            'items', v_items,
            'snapshot_digest', v_digest,
            'total', v_total,
            'accepted_before', v_cutoff,
            'after_id', p_after_id,
            'next_id', CASE WHEN v_more THEN v_next_id END,
            'complete', NOT v_more
        ),
        NULL
    );
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range
    OR invalid_datetime_format OR datetime_field_overflow THEN
    RETURN aap_labeling_v1._image_source_reply(NULL, 'invalid_request');
WHEN OTHERS THEN
    RETURN aap_labeling_v1._image_source_reply(NULL, 'invalid_response');
END;
$function$;
