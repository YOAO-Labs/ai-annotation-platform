-- Install with psql --single-transaction and ON_ERROR_STOP enabled.
CREATE SCHEMA aap_learning_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_learning_v1 FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_learning_v1.lock_project_deletion(p_project_id uuid)
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_project_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid project deletion lock request',
            DETAIL = '{"code":"invalid_request"}';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(p_project_id::text, 0)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_learning_v1.lock_project_deletion(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_learning_v1.guard_project_deletion(p_project_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_project_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-delete-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.active_learning_rounds AS learning_round
        WHERE learning_round.project_id = p_project_id
        LIMIT 1
    ) OR EXISTS (
        WITH project_tasks AS MATERIALIZED (
            SELECT task_id
            FROM aap_labeling_v1.project_task_references(p_project_id)
        )
        SELECT 1
        FROM project_tasks AS task
        JOIN public.active_learning_pool_members AS member USING (task_id)
        UNION ALL
        SELECT 1
        FROM project_tasks AS task
        JOIN public.active_learning_candidates AS candidate USING (task_id)
        LIMIT 1
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-delete-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', 'active_learning_reference'
            )
        );
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.training_studies AS study
        WHERE study.project_id = p_project_id
        LIMIT 1
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-delete-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', 'retained_training_studies'
            )
        );
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.trained_model_versions AS model_version
        WHERE model_version.project_id = p_project_id
        LIMIT 1
    ) OR EXISTS (
        SELECT 1
        FROM public.project_model_selections AS model_selection
        WHERE model_selection.project_id = p_project_id
        LIMIT 1
    ) OR EXISTS (
        SELECT 1
        FROM public.project_model_releases AS model_release
        WHERE model_release.project_id = p_project_id
        LIMIT 1
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-delete-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', 'retained_trained_models'
            )
        );
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.model_decisions AS decision
        WHERE decision.project_id = p_project_id
        LIMIT 1
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-delete-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', 'retained_model_decisions'
            )
        );
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.sample_evaluation_runs AS evaluation
        WHERE evaluation.project_id = p_project_id
        LIMIT 1
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-delete-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', 'retained_sample_evaluations'
            )
        );
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.learning_policies AS policy
        WHERE policy.project_id = p_project_id
        LIMIT 1
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-delete-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', 'retained_learning_policies'
            )
        );
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.training_dataset_versions AS dataset_version
        WHERE dataset_version.project_id = p_project_id
        LIMIT 1
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-delete-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', 'retained_training_dataset_versions'
            )
        );
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.training_runs AS run
        WHERE run.project_id = p_project_id
        LIMIT 1
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-delete-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'retained_training_runs')
        );
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.training_previews AS preview
        WHERE preview.project_id = p_project_id
        LIMIT 1
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-delete-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', 'retained_training_previews'
            )
        );
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.learning_triggers AS learning_trigger
        WHERE learning_trigger.project_id = p_project_id
        LIMIT 1
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-delete-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', 'retained_learning_triggers'
            )
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-project-delete-retention/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object()
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_learning_v1.guard_project_deletion(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_learning_v1.delete_project_owned_records(p_project_id uuid)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_deleted bigint := 0;
    v_rows bigint;
BEGIN
    IF p_project_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid project deletion request',
            DETAIL = '{"code":"invalid_request"}';
    END IF;

    DELETE FROM public.project_task_datasets AS stream
    WHERE stream.project_id = p_project_id;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_deleted := v_deleted + v_rows;

    DELETE FROM public.training_recipe_revisions AS revision
    WHERE revision.project_id = p_project_id;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_deleted := v_deleted + v_rows;

    DELETE FROM public.training_recipes AS recipe
    WHERE recipe.project_id = p_project_id;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_deleted := v_deleted + v_rows;

    RETURN v_deleted;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_learning_v1.delete_project_owned_records(uuid) FROM PUBLIC;
