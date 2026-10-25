-- Fixed local scale-seed Labeling operations. The caller owns all transaction
-- boundaries; this file adds no arbitrary SQL, table, or predicate selector.

CREATE VIEW aap_assets_v1.scale_seed_dataset_item_facts
WITH (security_barrier = true)
AS
SELECT id, dataset_id, file_name
FROM public.dataset_items;
-- aap-statement-break
REVOKE ALL ON TABLE aap_assets_v1.scale_seed_dataset_item_facts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.purge_scale_seed_labeling_rows(
    p_marker text,
    p_project_ids uuid[]
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
BEGIN
    IF p_marker IS NULL OR pg_catalog.btrim(p_marker) = ''
       OR p_project_ids IS NULL
       OR (pg_catalog.cardinality(p_project_ids) > 0
           AND pg_catalog.array_ndims(p_project_ids) IS DISTINCT FROM 1)
       OR pg_catalog.array_position(p_project_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid scale seed Labeling cleanup scope';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.unnest(p_project_ids) AS requested(project_id)
        JOIN aap_workspace_v1.project_facts AS project
          ON project.id = requested.project_id
        WHERE project.name IS DISTINCT FROM p_marker
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'scale seed Project marker changed',
            DETAIL = '{"code":"scale_seed_marker_mismatch"}';
    END IF;

    DELETE FROM public.annotations AS annotation
    WHERE annotation.task_id IN (
        SELECT task.id
        FROM public.tasks AS task
        WHERE task.project_id = ANY (p_project_ids)
    );
    DELETE FROM public.tasks AS task
    WHERE task.project_id = ANY (p_project_ids);
    DELETE FROM public.task_batches AS batch
    WHERE batch.project_id = ANY (p_project_ids);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.purge_scale_seed_labeling_rows(text,uuid[])
FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.create_scale_seed_batches(
    p_marker text,
    p_project_id uuid,
    p_dataset_id uuid,
    p_batch_count integer,
    p_annotator_id uuid
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_inserted bigint;
BEGIN
    IF p_marker IS NULL OR pg_catalog.btrim(p_marker) = ''
       OR p_project_id IS NULL OR p_dataset_id IS NULL
       OR p_batch_count IS NULL OR p_annotator_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid scale seed batch request';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM aap_workspace_v1.project_facts AS project
        WHERE project.id = p_project_id
          AND project.name = p_marker
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'scale seed Project marker changed',
            DETAIL = '{"code":"scale_seed_marker_mismatch"}';
    END IF;

    INSERT INTO public.task_batches (
        project_id, dataset_id, display_id, name, status, annotator_id
    )
    SELECT
        p_project_id,
        p_dataset_id,
        'B-' || pg_catalog.nextval(
            'public.display_seq_batches'::pg_catalog.regclass
        )::pg_catalog.text,
        'batch ' || batch.number,
        'active',
        p_annotator_id
    FROM pg_catalog.generate_series(1, p_batch_count) AS batch(number);
    GET DIAGNOSTICS v_inserted = ROW_COUNT;
    RETURN v_inserted;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.create_scale_seed_batches(text,uuid,uuid,integer,uuid)
FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.create_scale_seed_tasks(
    p_marker text,
    p_project_id uuid,
    p_dataset_id uuid
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_inserted bigint;
BEGIN
    IF p_marker IS NULL OR pg_catalog.btrim(p_marker) = ''
       OR p_project_id IS NULL OR p_dataset_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid scale seed task request';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM aap_workspace_v1.project_facts AS project
        WHERE project.id = p_project_id
          AND project.name = p_marker
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'scale seed Project marker changed',
            DETAIL = '{"code":"scale_seed_marker_mismatch"}';
    END IF;

    WITH batches AS (
        SELECT
            batch.id,
            pg_catalog.row_number() OVER (ORDER BY batch.display_id) - 1 AS rn,
            pg_catalog.count(*) OVER () AS cnt
        FROM public.task_batches AS batch
        WHERE batch.project_id = p_project_id
    ), items AS (
        SELECT
            item.id,
            pg_catalog.row_number() OVER (ORDER BY item.file_name) - 1 AS rn
        FROM aap_assets_v1.scale_seed_dataset_item_facts AS item
        WHERE item.dataset_id = p_dataset_id
    )
    INSERT INTO public.tasks (
        id, project_id, dataset_item_id, batch_id, display_id,
        file_name, file_path, file_type, status, sequence_order,
        created_at, is_labeled
    )
    SELECT
        pg_catalog.gen_random_uuid(),
        p_project_id,
        items.id,
        (
            SELECT batch.id
            FROM batches AS batch
            WHERE batch.rn = items.rn % (SELECT batches.cnt FROM batches LIMIT 1)
        ),
        'T-' || pg_catalog.nextval(
            'public.display_seq_tasks'::pg_catalog.regclass
        )::pg_catalog.text,
        'img_' || items.rn::pg_catalog.text || '.jpg',
        '/seed/' || items.rn::pg_catalog.text || '.jpg',
        'image',
        'pending',
        items.rn,
        pg_catalog.now() - pg_catalog.make_interval(
            secs => items.rn
        ),
        false
    FROM items;
    GET DIAGNOSTICS v_inserted = ROW_COUNT;
    RETURN v_inserted;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.create_scale_seed_tasks(text,uuid,uuid)
FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.create_scale_seed_annotations(
    p_marker text,
    p_project_id uuid,
    p_annotator_id uuid,
    p_annotation_count integer
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_inserted bigint;
BEGIN
    IF p_marker IS NULL OR pg_catalog.btrim(p_marker) = ''
       OR p_project_id IS NULL OR p_annotator_id IS NULL
       OR p_annotation_count IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid scale seed annotation request';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM aap_workspace_v1.project_facts AS project
        WHERE project.id = p_project_id
          AND project.name = p_marker
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'scale seed Project marker changed',
            DETAIL = '{"code":"scale_seed_marker_mismatch"}';
    END IF;

    INSERT INTO public.annotations (
        id, task_id, user_id, project_id, class_name, geometry, is_active
    )
    SELECT
        pg_catalog.gen_random_uuid(),
        task.id,
        p_annotator_id,
        p_project_id,
        'seed',
        pg_catalog.jsonb_build_object(
            'type', 'bbox', 'x', 0, 'y', 0, 'w', 1, 'h', 1
        ),
        true
    FROM (
        SELECT task.id
        FROM public.tasks AS task
        WHERE task.project_id = p_project_id
        ORDER BY task.sequence_order
        LIMIT p_annotation_count
    ) AS task;
    GET DIAGNOSTICS v_inserted = ROW_COUNT;
    RETURN v_inserted;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.create_scale_seed_annotations(text,uuid,uuid,integer)
FROM PUBLIC;
