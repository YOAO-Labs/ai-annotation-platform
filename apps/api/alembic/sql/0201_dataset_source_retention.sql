-- Learning-owned source retention checks for Assets/Workspace lifecycle commands.
CREATE FUNCTION aap_learning_v1.guard_dataset_item_deletion(
    p_dataset_id uuid,
    p_item_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_dataset_id IS NULL OR p_item_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    IF EXISTS (
        WITH source_items AS MATERIALIZED (
            SELECT item_id
            FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
        )
        SELECT 1
        FROM source_items AS source
        JOIN public.active_learning_pool_members AS member
          ON member.dataset_item_id = source.item_id
        UNION ALL
        SELECT 1
        FROM source_items AS source
        JOIN public.active_learning_candidates AS candidate
          ON candidate.dataset_item_id = source.item_id
        LIMIT 1
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', 'active_learning_reference'
            )
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-source-retention/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object()
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_learning_v1.guard_dataset_item_deletion(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_learning_v1.guard_dataset_unlink(
    p_dataset_id uuid,
    p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_dataset_id IS NULL OR p_project_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    IF EXISTS (
        WITH target_tasks AS MATERIALIZED (
            SELECT task_id
            FROM aap_labeling_v1.dataset_task_references(
                p_dataset_id, p_project_id
            )
        )
        SELECT 1
        FROM target_tasks AS task
        JOIN public.active_learning_pool_members AS member USING (task_id)
        UNION ALL
        SELECT 1
        FROM target_tasks AS task
        JOIN public.active_learning_candidates AS candidate USING (task_id)
        LIMIT 1
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', 'active_learning_reference'
            )
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-source-retention/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object()
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_learning_v1.guard_dataset_unlink(uuid, uuid) FROM PUBLIC;
