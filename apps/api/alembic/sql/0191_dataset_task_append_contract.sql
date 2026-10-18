-- Persistent, transaction-preserving dataset item append contract.
-- The caller owns BEGIN/COMMIT. The function never commits.
CREATE SCHEMA aap_assets_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
CREATE SCHEMA aap_labeling_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_assets_v1 FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_labeling_v1 FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.task_append_operations (
    operation_id uuid PRIMARY KEY,
    dataset_id uuid NOT NULL,
    item_ids uuid[] NOT NULL,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
-- aap-statement-break
COMMENT ON TABLE public.task_append_operations IS
    'Labeling-owned frozen receipts for dataset item append operations without asset foreign keys.';
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.lock_dataset_reference(p_dataset_id uuid)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_dataset_id uuid;
BEGIN
    SELECT dataset.id
    INTO v_dataset_id
    FROM public.datasets AS dataset
    WHERE dataset.id = p_dataset_id
    FOR NO KEY UPDATE;

    RETURN v_dataset_id;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.lock_current_association(
    p_dataset_id uuid,
    p_project_id uuid
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_dataset_id uuid;
    v_association_id uuid;
BEGIN
    v_dataset_id := aap_assets_v1.lock_dataset_reference(p_dataset_id);
    IF v_dataset_id IS NULL THEN
        RETURN NULL;
    END IF;

    IF aap_workspace_v1.lock_project_reference(p_project_id) IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT association.id
    INTO v_association_id
    FROM public.project_datasets AS association
    WHERE association.dataset_id = v_dataset_id
      AND association.project_id = p_project_id
    FOR UPDATE;

    RETURN v_association_id;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.lock_append_sources(
    p_dataset_id uuid,
    p_item_ids uuid[]
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_dataset_id uuid;
    v_project_id uuid;
    v_association_id uuid;
    v_associations jsonb := '[]'::jsonb;
    v_items jsonb := '[]'::jsonb;
    v_item record;
BEGIN
    IF p_dataset_id IS NULL OR p_item_ids IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid dataset append source request',
            DETAIL = '{"code":"invalid_request"}';
    END IF;

    v_dataset_id := aap_assets_v1.lock_dataset_reference(p_dataset_id);
    IF v_dataset_id IS NULL OR pg_catalog.cardinality(p_item_ids) = 0 THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-assets-append-sources/v1',
            'dataset_id', p_dataset_id,
            'associations', v_associations,
            'items', v_items
        );
    END IF;

    -- Dataset mutations take the same parent lock. Sorting keeps multi-project
    -- append operations on one dataset from taking association locks in a cycle.
    FOR v_project_id IN
        SELECT association.project_id
        FROM public.project_datasets AS association
        WHERE association.dataset_id = v_dataset_id
        ORDER BY association.project_id
    LOOP
        v_association_id := aap_assets_v1.lock_current_association(
            v_dataset_id,
            v_project_id
        );
        IF v_association_id IS NOT NULL THEN
            v_associations := v_associations || pg_catalog.jsonb_build_array(
                pg_catalog.jsonb_build_object(
                    'id', v_association_id,
                    'project_id', v_project_id
                )
            );
        END IF;
    END LOOP;

    IF v_associations = '[]'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-assets-append-sources/v1',
            'dataset_id', v_dataset_id,
            'associations', v_associations,
            'items', v_items
        );
    END IF;

    FOR v_item IN
        WITH requested AS (
            SELECT input.item_id, pg_catalog.min(input.ordinality) AS ordinality
            FROM pg_catalog.unnest(p_item_ids) WITH ORDINALITY
                AS input(item_id, ordinality)
            WHERE input.item_id IS NOT NULL
            GROUP BY input.item_id
        )
        SELECT
            item.id,
            item.file_name,
            item.file_path,
            item.file_type,
            requested.ordinality
        FROM requested
        JOIN public.dataset_items AS item
          ON item.id = requested.item_id
         AND item.dataset_id = v_dataset_id
        ORDER BY requested.ordinality, item.id
        FOR KEY SHARE OF item
    LOOP
        v_items := v_items || pg_catalog.jsonb_build_array(
            pg_catalog.jsonb_build_object(
                'id', v_item.id,
                'file_name', v_item.file_name,
                'file_path', v_item.file_path,
                'file_type', v_item.file_type
            )
        );
    END LOOP;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-assets-append-sources/v1',
        'dataset_id', v_dataset_id,
        'associations', v_associations,
        'items', v_items
    );
END;
$function$;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.append_dataset_items(
    p_dataset_id uuid,
    p_item_ids uuid[],
    p_operation_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_canonical_item_ids uuid[];
    v_inserted_receipt integer;
    v_existing_dataset_id uuid;
    v_existing_item_ids uuid[];
    v_existing_result jsonb;
    v_source_snapshot jsonb;
    v_project_id uuid;
    v_created bigint := 0;
    v_project_totals jsonb := '[]'::jsonb;
    v_result jsonb;
BEGIN
    IF p_dataset_id IS NULL OR p_item_ids IS NULL OR p_operation_id IS NULL
       OR pg_catalog.array_position(p_item_ids, NULL) IS NOT NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid dataset append operation',
            DETAIL = '{"code":"invalid_request"}';
    END IF;

    SELECT COALESCE(
        pg_catalog.array_agg(requested.item_id ORDER BY requested.item_id),
        ARRAY[]::uuid[]
    )
    INTO v_canonical_item_ids
    FROM (
        SELECT DISTINCT input.item_id
        FROM pg_catalog.unnest(p_item_ids) AS input(item_id)
    ) AS requested;

    INSERT INTO public.task_append_operations (
        operation_id,
        dataset_id,
        item_ids,
        result
    )
    VALUES (
        p_operation_id,
        p_dataset_id,
        v_canonical_item_ids,
        NULL
    )
    ON CONFLICT (operation_id) DO NOTHING;
    GET DIAGNOSTICS v_inserted_receipt = ROW_COUNT;

    IF v_inserted_receipt = 0 THEN
        SELECT
            operation.dataset_id,
            operation.item_ids,
            operation.result
        INTO
            v_existing_dataset_id,
            v_existing_item_ids,
            v_existing_result
        FROM public.task_append_operations AS operation
        WHERE operation.operation_id = p_operation_id
        FOR UPDATE;

        IF v_existing_dataset_id IS DISTINCT FROM p_dataset_id
           OR v_existing_item_ids IS DISTINCT FROM v_canonical_item_ids THEN
            RAISE EXCEPTION USING
                ERRCODE = '22023',
                MESSAGE = 'task append operation id was reused for a different request',
                DETAIL = '{"code":"operation_conflict"}';
        END IF;

        IF v_existing_result IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '55000',
                MESSAGE = 'task append operation receipt is incomplete',
                DETAIL = '{"code":"operation_incomplete"}';
        END IF;

        RETURN v_existing_result;
    END IF;

    IF pg_catalog.cardinality(v_canonical_item_ids) > 0 THEN
        v_source_snapshot := aap_assets_v1.lock_append_sources(
            p_dataset_id,
            p_item_ids
        );

        -- Project.total_tasks is a labeling-owned projection. Lock its rows
        -- after the sorted asset-link locks so concurrent datasets update
        -- multiple project counters in one global order.
        FOR v_project_id IN
            SELECT DISTINCT (association.value ->> 'project_id')::uuid
            FROM pg_catalog.jsonb_array_elements(
                v_source_snapshot -> 'associations'
            ) AS association(value)
            ORDER BY (association.value ->> 'project_id')::uuid
        LOOP
            PERFORM project.id
            FROM public.projects AS project
            WHERE project.id = v_project_id
            FOR NO KEY UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23503',
                    MESSAGE = 'append association project disappeared',
                    DETAIL = '{"code":"project_reference_missing"}';
            END IF;
        END LOOP;

        WITH associations AS MATERIALIZED (
            SELECT
                (association.value ->> 'id')::uuid AS association_id,
                (association.value ->> 'project_id')::uuid AS project_id
            FROM pg_catalog.jsonb_array_elements(
                v_source_snapshot -> 'associations'
            ) AS association(value)
        ),
        items AS MATERIALIZED (
            SELECT
                (item.value ->> 'id')::uuid AS item_id,
                item.value ->> 'file_name' AS file_name,
                item.value ->> 'file_path' AS file_path,
                item.value ->> 'file_type' AS file_type,
                item.ordinality AS item_ordinality
            FROM pg_catalog.jsonb_array_elements(
                v_source_snapshot -> 'items'
            ) WITH ORDINALITY AS item(value, ordinality)
        ),
        pending AS MATERIALIZED (
            SELECT
                association.project_id,
                item.item_id,
                item.file_name,
                item.file_path,
                item.file_type,
                item.item_ordinality
            FROM associations AS association
            CROSS JOIN items AS item
            WHERE NOT EXISTS (
                SELECT 1
                FROM public.tasks AS task
                WHERE task.project_id = association.project_id
                  AND task.dataset_item_id = item.item_id
            )
            ORDER BY association.project_id, item.item_ordinality
        ),
        inserted AS (
            INSERT INTO public.tasks (
                id,
                project_id,
                dataset_item_id,
                display_id,
                file_name,
                file_path,
                file_type,
                tags,
                status,
                is_labeled,
                overlap,
                total_annotations,
                total_predictions,
                assignee_is_override,
                reviewer_is_override,
                first_review_eligible,
                annotation_contributor_ids,
                reopened_count,
                version
            )
            SELECT
                pg_catalog.gen_random_uuid(),
                pending.project_id,
                pending.item_id,
                'T-' || pg_catalog.nextval(
                    'public.display_seq_tasks'::pg_catalog.regclass
                )::text,
                pending.file_name,
                pending.file_path,
                pending.file_type,
                '[]'::jsonb,
                'pending',
                false,
                1,
                0,
                0,
                false,
                false,
                true,
                '[]'::jsonb,
                0,
                1
            FROM pending
            ORDER BY pending.project_id, pending.item_ordinality
            RETURNING project_id
        ),
        created_by_project AS MATERIALIZED (
            SELECT inserted.project_id, pg_catalog.count(*) AS created
            FROM inserted
            GROUP BY inserted.project_id
        ),
        updated_projects AS (
            UPDATE public.projects AS project
            SET total_tasks = COALESCE(project.total_tasks, 0)
                + created.created
            FROM created_by_project AS created
            WHERE project.id = created.project_id
            RETURNING project.id AS project_id, project.total_tasks
        )
        SELECT
            COALESCE(
                (SELECT pg_catalog.sum(created.created) FROM created_by_project AS created),
                0
            ),
            COALESCE(
                (
                    SELECT pg_catalog.jsonb_agg(
                        pg_catalog.jsonb_build_object(
                            'project_id', updated.project_id,
                            'total_tasks', updated.total_tasks
                        )
                        ORDER BY updated.project_id
                    )
                    FROM updated_projects AS updated
                ),
                '[]'::jsonb
            )
        INTO v_created, v_project_totals;
    END IF;

    v_result := pg_catalog.jsonb_build_object(
        'schema', 'aap-task-append/v1',
        'operation_id', p_operation_id,
        'dataset_id', p_dataset_id,
        'created', v_created,
        'project_totals', v_project_totals,
        'associations', COALESCE(
            v_source_snapshot -> 'associations',
            '[]'::jsonb
        )
    );

    UPDATE public.task_append_operations AS operation
    SET result = v_result
    WHERE operation.operation_id = p_operation_id;

    RETURN v_result;
END;
$function$;

-- aap-statement-break
REVOKE ALL ON TABLE public.task_append_operations FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.lock_dataset_reference(uuid) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.lock_current_association(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.lock_append_sources(uuid, uuid[]) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.append_dataset_items(uuid, uuid[], uuid) FROM PUBLIC;
