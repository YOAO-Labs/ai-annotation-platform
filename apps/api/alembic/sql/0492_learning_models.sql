CREATE VIEW yoao_learning_v1._model_version_rows AS
SELECT id, project_id, run_id, attempt_id, request_id, request_digest, name,
       framework, framework_version, artifact, classes, bindings, binding_digest,
       provenance, evaluation, created_at
FROM public.trained_model_versions;
-- aap-statement-break
CREATE VIEW yoao_learning_v1._model_deployment_rows AS
SELECT id, model_version_id, registry_id, check_id, status, receipt, trial,
       error_message, checked_at, created_at
FROM public.trained_model_deployments;
-- aap-statement-break
CREATE VIEW yoao_learning_v1._model_release_rows AS
SELECT id, project_id, model_version_id, deployment_id, creator_id, previous_release_id,
       rollback_from_id, request_id, request_digest, generation, task, snapshot, created_at
FROM public.project_model_releases;
-- aap-statement-break
CREATE VIEW yoao_learning_v1._model_decision_rows AS
SELECT id, project_id, model_version_id, creator_id, creator_label, sequence,
       request_id, request_digest, disposition, note, created_at
FROM public.model_decisions;
-- aap-statement-break
CREATE VIEW yoao_learning_v1.model_version_lineage AS
SELECT id, project_id, run_id, attempt_id, name, created_at, binding_digest, framework,
       CASE WHEN jsonb_typeof(artifact->'sha256') = 'string' THEN left(artifact->>'sha256', 256) END AS weights_sha256,
       CASE WHEN jsonb_typeof(provenance->'run_id') = 'string' THEN left(provenance->>'run_id', 256) END AS frozen_run_id,
       CASE WHEN jsonb_typeof(provenance->'attempt_id') = 'string' THEN left(provenance->>'attempt_id', 256) END AS frozen_attempt_id,
       CASE WHEN jsonb_typeof(provenance->'dataset_id') = 'string' THEN left(provenance->>'dataset_id', 256) END AS frozen_dataset_id,
       CASE WHEN jsonb_typeof(provenance->'dataset_digest') = 'string' THEN left(provenance->>'dataset_digest', 256) END AS frozen_dataset_digest,
       CASE WHEN jsonb_typeof(provenance->'validation_digest') = 'string' THEN left(provenance->>'validation_digest', 256) END AS frozen_validation_digest,
       CASE WHEN jsonb_typeof(provenance->'config_digest') = 'string' THEN left(provenance->>'config_digest', 256) END AS frozen_config_digest
FROM public.trained_model_versions;
-- aap-statement-break
CREATE VIEW yoao_learning_v1.model_deployment_lineage AS
SELECT d.id, v.project_id, d.model_version_id, d.registry_id, d.status,
       d.checked_at, d.created_at
FROM public.trained_model_deployments d
JOIN public.trained_model_versions v ON v.id = d.model_version_id;
-- aap-statement-break
CREATE VIEW yoao_learning_v1.model_release_lineage AS
SELECT id, project_id, model_version_id, deployment_id, generation,
       previous_release_id, rollback_from_id, created_at,
       CASE WHEN jsonb_typeof(snapshot->'model_version_id') = 'string' THEN left(snapshot->>'model_version_id', 256) END AS frozen_model_version_id,
       CASE WHEN jsonb_typeof(snapshot->'deployment_id') = 'string' THEN left(snapshot->>'deployment_id', 256) END AS frozen_deployment_id,
       CASE WHEN jsonb_typeof(snapshot->'sha256') = 'string' THEN left(snapshot->>'sha256', 256) END AS frozen_sha256,
       CASE WHEN jsonb_typeof(snapshot->'reference_digest') = 'string' THEN left(snapshot->>'reference_digest', 256) END AS frozen_reference_digest
FROM public.project_model_releases;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.model_version_detail(p_project uuid, p_version uuid, p_key_share boolean)
RETURNS SETOF yoao_learning_v1._model_version_rows
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF p_key_share THEN
        RETURN QUERY SELECT v.* FROM yoao_learning_v1._model_version_rows v
        WHERE v.project_id = p_project AND v.id = p_version FOR KEY SHARE OF v;
    ELSE
        RETURN QUERY SELECT v.* FROM yoao_learning_v1._model_version_rows v
        WHERE v.project_id = p_project AND v.id = p_version;
    END IF;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.model_registration_replay(p_project uuid, p_request uuid, p_attempt uuid)
RETURNS SETOF yoao_learning_v1._model_version_rows
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT v.* FROM yoao_learning_v1._model_version_rows v
    WHERE v.project_id = p_project
      AND ((p_request IS NOT NULL AND p_attempt IS NULL AND v.request_id = p_request)
        OR (p_request IS NULL AND p_attempt IS NOT NULL AND v.attempt_id = p_attempt))
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.register_model_candidate(
    p_project uuid, p_run uuid, p_attempt uuid, p_creator uuid,
    p_request uuid, p_digest text, p_name text, p_framework text, p_framework_version text,
    p_artifact jsonb, p_classes jsonb, p_bindings jsonb, p_binding_digest text,
    p_provenance jsonb, p_evaluation jsonb
)
RETURNS SETOF yoao_learning_v1._model_version_rows
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE v_id uuid := gen_random_uuid();
BEGIN
    PERFORM r.id FROM public.training_runs r
    WHERE r.project_id = p_project AND r.id = p_run FOR UPDATE;
    IF NOT FOUND OR p_artifact->>'role' IS DISTINCT FROM 'weights'
       OR NOT EXISTS (
           SELECT 1 FROM public.training_attempts a WHERE a.run_id = p_run AND a.id = p_attempt
             AND a.status = 'succeeded'
             AND EXISTS (SELECT 1 FROM jsonb_each(a.artifacts) artifact WHERE artifact.value = p_artifact)
       )
       OR EXISTS (
           SELECT 1 FROM public.trained_model_versions v
           WHERE v.project_id = p_project AND v.attempt_id = p_attempt
       )
    THEN RETURN; END IF;
    INSERT INTO public.trained_model_versions
        (id, project_id, run_id, attempt_id, creator_id, request_id, request_digest,
         name, framework, framework_version, artifact, classes, bindings, binding_digest,
         provenance, evaluation)
    VALUES (v_id, p_project, p_run, p_attempt, p_creator, p_request, p_digest,
            p_name, p_framework, p_framework_version, p_artifact, p_classes, p_bindings,
            p_binding_digest, p_provenance, p_evaluation);
    RETURN QUERY SELECT v.* FROM yoao_learning_v1._model_version_rows v WHERE v.id = v_id;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.run_model_candidates(p_project uuid, p_run uuid)
RETURNS TABLE(id uuid, attempt_id uuid, name varchar)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT v.id, v.attempt_id, v.name FROM public.trained_model_versions v
    WHERE v.project_id = p_project AND v.run_id = p_run ORDER BY v.created_at, v.id
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.run_has_model_candidate(p_run uuid, p_attempt uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.trained_model_versions v
        WHERE (v.run_id = p_run OR EXISTS (
            SELECT 1 FROM public.training_attempts a WHERE a.run_id = p_run AND a.id = v.attempt_id
        )) AND (p_attempt IS NULL OR v.attempt_id = p_attempt)
    )
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.catalog_model_candidates(p_projects uuid[], p_include_candidates boolean)
RETURNS SETOF yoao_learning_v1._model_version_rows
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT v.* FROM yoao_learning_v1._model_version_rows v
    WHERE v.project_id = ANY(p_projects) AND (p_include_candidates OR EXISTS (
        SELECT 1 FROM public.project_model_releases r
        WHERE r.project_id = v.project_id AND r.model_version_id = v.id
    ))
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.model_candidate_visibility(p_project uuid, p_version uuid)
RETURNS TABLE(published boolean, disposition varchar, sequence integer, is_current boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (SELECT 1 FROM public.project_model_releases r
                   WHERE r.project_id = v.project_id AND r.model_version_id = v.id),
           d.disposition, d.sequence,
           EXISTS (
               SELECT 1 FROM public.project_model_selections s
               JOIN public.project_model_releases r ON r.id = s.release_id
               WHERE s.project_id = v.project_id AND r.project_id = v.project_id
                 AND r.model_version_id = v.id AND r.task = s.task
           )
    FROM public.trained_model_versions v
    LEFT JOIN LATERAL (
        SELECT decision.disposition, decision.sequence FROM public.model_decisions decision
        WHERE decision.model_version_id = v.id AND decision.project_id = v.project_id
        ORDER BY decision.sequence DESC LIMIT 1
    ) d ON true
    WHERE v.project_id = p_project AND v.id = p_version
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.model_candidate_metadata(p_projects uuid[], p_versions uuid[])
RETURNS TABLE(id uuid, disposition varchar, sequence integer, is_current boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT v.id, f.disposition, f.sequence, f.is_current
    FROM public.trained_model_versions v
    CROSS JOIN LATERAL yoao_learning_v1.model_candidate_visibility(v.project_id, v.id) f
    WHERE v.project_id = ANY(p_projects) AND v.id = ANY(p_versions)
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1._filtered_model_candidates(
    p_project uuid, p_archive text, p_pattern text, p_framework text, p_task text,
    p_start timestamptz, p_end timestamptz
)
RETURNS TABLE(id uuid, created_at timestamptz)
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
    SELECT v.id, v.created_at FROM public.trained_model_versions v
    LEFT JOIN LATERAL yoao_learning_v1.model_candidate_visibility(v.project_id, v.id) f
      ON p_archive IN ('only', 'exclude')
    WHERE v.project_id = p_project
      AND (p_archive <> 'only' OR f.disposition = 'archived')
      AND (p_archive <> 'exclude' OR f.disposition IS DISTINCT FROM 'archived' OR f.is_current)
      AND (p_pattern IS NULL OR v.name ILIKE p_pattern ESCAPE E'\\')
      AND (p_framework IS NULL OR v.framework = p_framework)
      AND (p_start IS NULL OR v.created_at >= p_start)
      AND (p_end IS NULL OR v.created_at <= p_end)
      AND (p_task IS NULL OR p_task = CASE coalesce(v.provenance->>'task', 'detect')
               WHEN 'detection' THEN 'detect' WHEN 'image-detection' THEN 'detect'
               WHEN 'segmentation' THEN 'segment' WHEN 'image-instance-segmentation' THEN 'segment'
               WHEN 'instance-segmentation' THEN 'segment' ELSE coalesce(v.provenance->>'task', 'detect') END)
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.list_model_candidates(
    p_project uuid, p_archive text, p_pattern text, p_framework text, p_task text,
    p_start timestamptz, p_end timestamptz, p_limit integer, p_offset integer
)
RETURNS SETOF yoao_learning_v1._model_version_rows
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT v.* FROM (
        SELECT f.id, f.created_at FROM yoao_learning_v1._filtered_model_candidates(
            p_project, p_archive, p_pattern, p_framework, p_task, p_start, p_end) f
        ORDER BY f.created_at DESC, f.id DESC LIMIT least(greatest(p_limit, 1), 100) OFFSET greatest(p_offset, 0)
    ) page JOIN yoao_learning_v1._model_version_rows v ON v.id = page.id
    ORDER BY page.created_at DESC, page.id DESC
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.count_model_candidates(
    p_project uuid, p_archive text, p_pattern text, p_framework text, p_task text,
    p_start timestamptz, p_end timestamptz
)
RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT count(*) FROM yoao_learning_v1._filtered_model_candidates(
        p_project, p_archive, p_pattern, p_framework, p_task, p_start, p_end)
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.model_deployments_for_versions(p_projects uuid[], p_versions uuid[])
RETURNS SETOF yoao_learning_v1._model_deployment_rows
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT d.* FROM yoao_learning_v1._model_deployment_rows d
    JOIN public.trained_model_versions v ON v.id = d.model_version_id
    WHERE v.project_id = ANY(p_projects) AND v.id = ANY(p_versions)
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.model_deployment_detail(
    p_project uuid, p_version uuid, p_deployment uuid, p_lock boolean
)
RETURNS SETOF yoao_learning_v1._model_deployment_rows
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF p_lock THEN
        RETURN QUERY SELECT d.* FROM yoao_learning_v1._model_deployment_rows d
        JOIN public.trained_model_versions v ON v.id = d.model_version_id
        WHERE v.project_id = p_project AND v.id = p_version AND d.id = p_deployment FOR UPDATE OF d;
    ELSE
        RETURN QUERY SELECT d.* FROM yoao_learning_v1._model_deployment_rows d
        JOIN public.trained_model_versions v ON v.id = d.model_version_id
        WHERE v.project_id = p_project AND v.id = p_version AND d.id = p_deployment;
    END IF;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.hold_model_deployment_for_registry(
    p_project uuid, p_version uuid, p_registry uuid, p_ready boolean
)
RETURNS SETOF yoao_learning_v1._model_deployment_rows
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT d.* FROM yoao_learning_v1._model_deployment_rows d
    JOIN public.trained_model_versions v ON v.id = d.model_version_id
    WHERE v.project_id = p_project AND v.id = p_version AND d.registry_id = p_registry
      AND (NOT p_ready OR d.status = 'ready') FOR UPDATE OF d
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.hold_registry_model_deployments(p_registry uuid)
RETURNS TABLE(id uuid, model_version_id uuid, registry_id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT d.id, d.model_version_id, d.registry_id FROM public.trained_model_deployments d
    WHERE d.registry_id = p_registry ORDER BY d.id FOR UPDATE OF d
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.count_active_model_releases(p_deployments uuid[])
RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT count(*) FROM public.project_model_selections s
    JOIN public.project_model_releases r ON r.id = s.release_id
    WHERE r.deployment_id = ANY(p_deployments)
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.hold_model_candidate(p_project uuid, p_version uuid, p_deployment uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM v.id FROM public.trained_model_versions v
    WHERE v.project_id = p_project AND v.id = p_version FOR SHARE;
    IF NOT FOUND THEN RETURN false; END IF;
    PERFORM d.id FROM public.trained_model_deployments d
    WHERE d.model_version_id = p_version AND d.id = p_deployment FOR SHARE;
    RETURN FOUND;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.hold_prediction_model_reference(
    p_project uuid, p_version uuid, p_deployment uuid, p_release uuid
)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM v.id FROM public.trained_model_versions v
    WHERE v.project_id = p_project AND v.id = p_version FOR SHARE NOWAIT;
    IF NOT FOUND THEN RETURN false; END IF;
    PERFORM d.id FROM public.trained_model_deployments d
    WHERE d.model_version_id = p_version AND d.id = p_deployment FOR SHARE NOWAIT;
    IF NOT FOUND THEN RETURN false; END IF;
    IF p_release IS NULL THEN RETURN true; END IF;
    PERFORM r.id FROM public.project_model_releases r
    WHERE r.project_id = p_project AND r.id = p_release
      AND r.model_version_id = p_version AND r.deployment_id = p_deployment FOR SHARE NOWAIT;
    RETURN FOUND;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.start_model_deployment_check(
    p_project uuid, p_version uuid, p_registry uuid, p_check uuid
)
RETURNS SETOF yoao_learning_v1._model_deployment_rows
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE v_id uuid;
BEGIN
    INSERT INTO public.trained_model_deployments AS d
        (id, model_version_id, registry_id, check_id, status, receipt, trial)
    SELECT gen_random_uuid(), v.id, p_registry, p_check, 'checking', '{}'::jsonb, '{}'::jsonb
    FROM public.trained_model_versions v WHERE v.project_id = p_project AND v.id = p_version
    ON CONFLICT (model_version_id, registry_id) DO UPDATE
      SET check_id = excluded.check_id, status = 'checking', error_message = NULL
      WHERE d.status NOT IN ('draining', 'retired')
    RETURNING d.id INTO v_id;
    RETURN QUERY SELECT d.* FROM yoao_learning_v1._model_deployment_rows d WHERE d.id = v_id;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.finish_model_deployment_check(
    p_project uuid, p_deployment uuid, p_check uuid, p_receipt jsonb, p_trial jsonb,
    p_error text, p_checked_at timestamptz
)
RETURNS SETOF yoao_learning_v1._model_deployment_rows
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    UPDATE public.trained_model_deployments d
    SET status = CASE WHEN p_error IS NULL THEN 'ready' ELSE 'failed' END,
        error_message = p_error, checked_at = p_checked_at,
        receipt = CASE WHEN p_error IS NULL THEN p_receipt ELSE d.receipt END,
        trial = CASE WHEN p_error IS NULL THEN p_trial ELSE d.trial END
    FROM public.trained_model_versions v
    WHERE d.id = p_deployment AND d.check_id = p_check AND d.status = 'checking'
      AND v.id = d.model_version_id AND v.project_id = p_project;
    IF FOUND THEN
        RETURN QUERY SELECT d.* FROM yoao_learning_v1._model_deployment_rows d WHERE d.id = p_deployment;
    END IF;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.advance_model_deployment_retirement(
    p_project uuid, p_version uuid, p_deployment uuid, p_check uuid, p_terminal boolean
)
RETURNS TABLE(advanced boolean, active_releases bigint, active_jobs bigint, retryable_failures bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE v_registry uuid;
BEGIN
    SELECT d.registry_id INTO v_registry FROM public.trained_model_deployments d
    JOIN public.trained_model_versions v ON v.id = d.model_version_id
    WHERE v.project_id = p_project AND v.id = p_version AND v.framework = 'libreyolo'
      AND d.id = p_deployment AND d.check_id = p_check
      AND d.status = CASE WHEN p_terminal THEN 'draining' ELSE 'ready' END FOR UPDATE OF d;
    IF NOT FOUND THEN RETURN; END IF;
    active_releases := 0;
    active_jobs := 0;
    retryable_failures := 0;
    IF p_terminal THEN
        active_releases := yoao_learning_v1.count_active_model_releases(ARRAY[p_deployment]);
        SELECT u.active_jobs, u.retryable_failures INTO active_jobs, retryable_failures
        FROM yoao_jobs_v1.frozen_deployment_usage(p_deployment, v_registry, p_version) u;
        IF active_releases > 0 OR active_jobs > 0 OR retryable_failures > 0 THEN
            advanced := false;
            RETURN NEXT;
            RETURN;
        END IF;
    END IF;
    UPDATE public.trained_model_deployments SET
        status = CASE WHEN p_terminal THEN 'retired' ELSE 'draining' END,
        check_id = CASE WHEN p_terminal THEN p_check ELSE gen_random_uuid() END
    WHERE id = p_deployment;
    advanced := true;
    RETURN NEXT;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.model_release_detail(p_project uuid, p_release uuid)
RETURNS SETOF yoao_learning_v1._model_release_rows
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT r.* FROM yoao_learning_v1._model_release_rows r
    WHERE r.project_id = p_project AND r.id = p_release
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.model_release_replay(p_project uuid, p_request uuid)
RETURNS SETOF yoao_learning_v1._model_release_rows
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT r.* FROM yoao_learning_v1._model_release_rows r
    WHERE r.project_id = p_project AND r.request_id = p_request
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.catalog_model_releases(p_projects uuid[], p_versions uuid[])
RETURNS TABLE(id uuid, model_version_id uuid, deployment_id uuid, generation integer, task varchar, is_current boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT r.id, r.model_version_id, r.deployment_id, r.generation, r.task,
           coalesce(s.release_id = r.id, false)
    FROM public.project_model_releases r
    LEFT JOIN public.project_model_selections s ON s.project_id = r.project_id AND s.task = r.task
    WHERE r.project_id = ANY(p_projects) AND r.model_version_id = ANY(p_versions)
    ORDER BY r.generation DESC
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.project_model_selection(p_project uuid, p_task text, p_hold boolean)
RETURNS TABLE(release_id uuid, generation integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF p_hold THEN
        RETURN QUERY SELECT s.release_id, s.generation FROM public.project_model_selections s
        WHERE s.project_id = p_project AND s.task = p_task FOR UPDATE;
    ELSE
        RETURN QUERY SELECT s.release_id, s.generation FROM public.project_model_selections s
        WHERE s.project_id = p_project AND s.task = p_task;
    END IF;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.list_project_model_releases(
    p_project uuid, p_task text, p_limit integer, p_offset integer
)
RETURNS SETOF yoao_learning_v1._model_release_rows
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT r.* FROM yoao_learning_v1._model_release_rows r
    WHERE r.project_id = p_project AND r.task = p_task
    ORDER BY r.generation DESC LIMIT least(greatest(p_limit, 1), 100) OFFSET greatest(p_offset, 0)
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.count_project_model_releases(p_project uuid, p_task text)
RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT count(*) FROM public.project_model_releases r WHERE r.project_id = p_project AND r.task = p_task
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.publish_project_model_release(
    p_project uuid, p_task text, p_creator uuid, p_request uuid, p_digest text,
    p_release uuid, p_generation integer, p_previous uuid, p_rollback uuid,
    p_version uuid, p_deployment uuid, p_snapshot jsonb
)
RETURNS SETOF yoao_learning_v1._model_release_rows
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_generation integer;
    v_previous uuid;
BEGIN
    SELECT s.generation, s.release_id INTO v_generation, v_previous
    FROM public.project_model_selections s
    WHERE s.project_id = p_project AND s.task = p_task FOR UPDATE;
    IF coalesce(v_generation, 0) IS DISTINCT FROM p_generation
       OR v_previous IS DISTINCT FROM p_previous
       OR (p_version IS NULL AND p_previous IS NULL)
       OR EXISTS (
           SELECT 1 FROM public.project_model_releases r WHERE r.id = p_previous
           AND r.model_version_id IS NOT DISTINCT FROM p_version
           AND r.deployment_id IS NOT DISTINCT FROM p_deployment
       )
    THEN RETURN; END IF;
    IF p_version IS NOT NULL THEN
        PERFORM d.id FROM public.trained_model_deployments d
        JOIN public.trained_model_versions v ON v.id = d.model_version_id
        WHERE d.id = p_deployment AND d.status = 'ready' AND v.id = p_version AND v.project_id = p_project
          AND p_snapshot->>'project_id' = p_project::text
          AND p_snapshot->>'release_id' = p_release::text
          AND p_snapshot->>'release_generation' = (p_generation + 1)::text
          AND p_snapshot->>'model_version_id' = p_version::text
          AND p_snapshot->>'deployment_id' = p_deployment::text
          AND p_snapshot->>'registry_id' = d.registry_id::text
          AND p_snapshot->>'sha256' = v.artifact->>'sha256'
          AND p_snapshot->>'framework' = v.framework AND p_snapshot->>'framework_version' = v.framework_version
          AND p_snapshot->>'task' = CASE WHEN p_task = 'segment' THEN 'segmentation' ELSE 'detection' END
          AND p_snapshot->'classes' = v.classes AND p_snapshot->'bindings' = v.bindings
          AND p_snapshot->>'binding_digest' = v.binding_digest FOR UPDATE OF d;
        IF NOT FOUND THEN RETURN; END IF;
    ELSIF p_deployment IS NOT NULL OR p_snapshot IS DISTINCT FROM '{}'::jsonb THEN
        RETURN;
    END IF;
    IF p_rollback IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.project_model_releases r WHERE r.id = p_rollback
          AND r.project_id = p_project AND r.task = p_task
          AND r.model_version_id IS NOT DISTINCT FROM p_version
          AND r.deployment_id IS NOT DISTINCT FROM p_deployment
    ) THEN RETURN; END IF;
    IF v_generation IS NULL THEN
        INSERT INTO public.project_model_selections (project_id, task, generation)
        VALUES (p_project, p_task, 0) ON CONFLICT (project_id, task) DO NOTHING;
        IF NOT FOUND THEN RETURN; END IF;
    END IF;
    INSERT INTO public.project_model_releases
        (id, project_id, task, creator_id, request_id, request_digest, model_version_id,
         deployment_id, previous_release_id, rollback_from_id, generation, snapshot)
    VALUES (p_release, p_project, p_task, p_creator, p_request, p_digest, p_version,
            p_deployment, p_previous, p_rollback, p_generation + 1, p_snapshot);
    UPDATE public.project_model_selections SET release_id = p_release,
        generation = p_generation + 1, updated_at = now()
    WHERE project_id = p_project AND task = p_task;
    RETURN QUERY SELECT r.* FROM yoao_learning_v1._model_release_rows r WHERE r.id = p_release;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.append_model_decision(
    p_project uuid, p_model uuid, p_creator uuid, p_label text,
    p_request uuid, p_digest text, p_disposition text, p_note text
)
RETURNS SETOF yoao_learning_v1._model_decision_rows
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE v_id uuid;
BEGIN
    PERFORM 1 FROM public.trained_model_versions v WHERE v.id = p_model AND v.project_id = p_project FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT d.id INTO v_id FROM public.model_decisions d
    WHERE d.model_version_id = p_model AND d.request_id = p_request;
    IF v_id IS NULL THEN
        INSERT INTO public.model_decisions
            (id, project_id, model_version_id, creator_id, creator_label,
             sequence, request_id, request_digest, disposition, note)
        SELECT gen_random_uuid(), p_project, p_model, p_creator, p_label,
               coalesce(max(d.sequence), 0) + 1, p_request, p_digest, p_disposition, p_note
        FROM public.model_decisions d WHERE d.model_version_id = p_model
        RETURNING id INTO v_id;
    END IF;
    RETURN QUERY SELECT d.* FROM yoao_learning_v1._model_decision_rows d WHERE d.id = v_id;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.list_model_decisions(p_project uuid, p_model uuid, p_before integer, p_limit integer)
RETURNS SETOF yoao_learning_v1._model_decision_rows
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT d.* FROM yoao_learning_v1._model_decision_rows d
    WHERE d.project_id = p_project AND d.model_version_id = p_model
      AND (p_before IS NULL OR d.sequence < p_before)
    ORDER BY d.sequence DESC LIMIT least(greatest(p_limit, 1), 101)
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.trial_mask_keys()
RETURNS TABLE(object_key text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT DISTINCT value #>> '{}' FROM public.trained_model_deployments d,
         LATERAL jsonb_path_query(d.trial, '$.**.object_key') value
    WHERE value #>> '{}' LIKE 'raster-masks/sha256/%'
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.trial_mask_referenced(p_key text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (SELECT 1 FROM public.trained_model_deployments d,
                         LATERAL jsonb_path_query(d.trial, '$.**.object_key') value
                   WHERE value #>> '{}' = p_key)
$$;
-- aap-statement-break
CREATE VIEW yoao_learning_v1._evaluation_run_rows AS
SELECT id, project_id, creator_id, dataset_id, model_version_id, deployment_id, request_id,
       request_digest, name, status, spec, spec_digest, storage_ref, "authorization",
       generation, current_attempt_id, async_job_id, error_code, error_message,
       created_at, completed_at, deletion_requested_at
FROM public.sample_evaluation_runs;
-- aap-statement-break
CREATE VIEW yoao_learning_v1._evaluation_attempt_rows AS
SELECT id, run_id, ordinal, generation, status, claim_id, async_job_id, report, error_code,
       error_message, heartbeat_at, dispatched_at, maintenance_at, started_at, completed_at, created_at
FROM public.sample_evaluation_attempts;
-- aap-statement-break
CREATE VIEW yoao_learning_v1._evaluation_sample_rows AS
SELECT run_id, ordinal, request_id, status, request, evidence, error_code, error_message, produced_attempt_id
FROM public.sample_evaluation_samples;
-- aap-statement-break
CREATE VIEW yoao_learning_v1.sample_evaluation_lineage AS
SELECT id, project_id, dataset_id, model_version_id, deployment_id, name, status,
       current_attempt_id, generation, created_at, spec_digest, deletion_requested_at,
       CASE WHEN jsonb_typeof(ref->'model_version_id') = 'string' THEN left(ref->>'model_version_id', 256) END AS frozen_model_version_id,
       CASE WHEN jsonb_typeof(ref->'deployment_id') = 'string' THEN left(ref->>'deployment_id', 256) END AS frozen_deployment_id,
       CASE WHEN jsonb_typeof(ref->'project_id') = 'string' THEN left(ref->>'project_id', 256) END AS frozen_project_id,
       CASE WHEN jsonb_typeof(ref->'sha256') = 'string' THEN left(ref->>'sha256', 256) END AS frozen_sha256,
       CASE WHEN jsonb_typeof(ref->'reference_digest') = 'string' THEN left(ref->>'reference_digest', 256) END AS frozen_reference_digest
FROM public.sample_evaluation_runs r CROSS JOIN LATERAL (SELECT r.spec->'model_ref' AS ref) frozen;
-- aap-statement-break
CREATE VIEW yoao_learning_v1.sample_evaluation_storage_owners AS
SELECT id, project_id, dataset_id, generation, current_attempt_id, status, storage_ref
FROM public.sample_evaluation_runs;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.evaluation_detail(p_project uuid, p_run uuid, p_lock text)
RETURNS SETOF yoao_learning_v1._evaluation_run_rows
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    CASE p_lock
    WHEN 'read' THEN
        RETURN QUERY SELECT r.* FROM yoao_learning_v1._evaluation_run_rows r
        WHERE r.id = p_run AND (p_project IS NULL OR r.project_id = p_project);
    WHEN 'share' THEN
        RETURN QUERY SELECT r.* FROM yoao_learning_v1._evaluation_run_rows r
        WHERE r.id = p_run AND r.project_id = p_project FOR SHARE;
    WHEN 'update' THEN
        RETURN QUERY SELECT r.* FROM yoao_learning_v1._evaluation_run_rows r
        WHERE r.id = p_run AND (p_project IS NULL OR r.project_id = p_project) FOR UPDATE;
    WHEN 'maintenance' THEN
        RETURN QUERY SELECT r.* FROM yoao_learning_v1._evaluation_run_rows r
        WHERE r.id = p_run FOR UPDATE SKIP LOCKED;
    END CASE;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.evaluation_for_attempt(p_attempt uuid)
RETURNS SETOF yoao_learning_v1._evaluation_run_rows
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT r.* FROM yoao_learning_v1._evaluation_run_rows r
    JOIN public.sample_evaluation_attempts a ON a.run_id = r.id
    WHERE a.id = p_attempt
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.hold_evaluation_for_current_job(p_job uuid)
RETURNS SETOF yoao_learning_v1._evaluation_run_rows
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT r.* FROM yoao_learning_v1._evaluation_run_rows r
    WHERE r.async_job_id = p_job FOR UPDATE OF r
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.evaluation_replay(p_project uuid, p_request uuid)
RETURNS SETOF yoao_learning_v1._evaluation_run_rows
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT r.* FROM yoao_learning_v1._evaluation_run_rows r
    WHERE r.project_id = p_project AND r.request_id = p_request
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.list_project_evaluations(p_project uuid, p_limit integer, p_offset integer)
RETURNS SETOF yoao_learning_v1._evaluation_run_rows
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT r.* FROM yoao_learning_v1._evaluation_run_rows r WHERE r.project_id = p_project
    ORDER BY r.created_at DESC, r.id DESC LIMIT least(greatest(p_limit, 1), 100) OFFSET greatest(p_offset, 0)
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.count_project_evaluations(p_project uuid)
RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT count(*) FROM public.sample_evaluation_runs r WHERE r.project_id = p_project
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.dataset_has_sample_evaluation(p_project uuid, p_dataset uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (SELECT 1 FROM public.sample_evaluation_runs r
                   WHERE r.project_id = p_project AND r.dataset_id = p_dataset)
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.held_out_evaluation_budget(p_project uuid)
RETURNS TABLE(run_count bigint, byte_count bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT count(*), coalesce(sum(octet_length(r.spec::text)), 0)
    FROM public.sample_evaluation_runs r WHERE r.project_id = p_project AND r.spec->>'role' = 'held_out_test'
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.held_out_evaluation_specs(p_project uuid, p_hold boolean)
RETURNS TABLE(id uuid, spec jsonb, spec_digest varchar, deletion_requested_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF p_hold THEN
        RETURN QUERY SELECT r.id, r.spec, r.spec_digest, r.deletion_requested_at
        FROM public.sample_evaluation_runs r WHERE r.project_id = p_project AND r.spec->>'role' = 'held_out_test'
        ORDER BY r.id FOR SHARE;
    ELSE
        RETURN QUERY SELECT r.id, r.spec, r.spec_digest, r.deletion_requested_at
        FROM public.sample_evaluation_runs r WHERE r.project_id = p_project AND r.spec->>'role' = 'held_out_test'
        ORDER BY r.id;
    END IF;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.evaluation_attempt_detail(p_run uuid, p_attempt uuid)
RETURNS SETOF yoao_learning_v1._evaluation_attempt_rows
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT a.* FROM yoao_learning_v1._evaluation_attempt_rows a
    WHERE a.id = p_attempt AND (p_run IS NULL OR a.run_id = p_run)
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.evaluation_attempt_history(p_run uuid)
RETURNS SETOF yoao_learning_v1._evaluation_attempt_rows
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT a.* FROM yoao_learning_v1._evaluation_attempt_rows a
    WHERE a.run_id = p_run ORDER BY a.ordinal DESC
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.evaluation_sample_detail(p_run uuid, p_ordinal integer)
RETURNS SETOF yoao_learning_v1._evaluation_sample_rows
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT s.* FROM yoao_learning_v1._evaluation_sample_rows s
    WHERE s.run_id = p_run AND s.ordinal = p_ordinal
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.evaluation_sample_evidence(p_run uuid)
RETURNS SETOF yoao_learning_v1._evaluation_sample_rows
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT s.* FROM yoao_learning_v1._evaluation_sample_rows s WHERE s.run_id = p_run ORDER BY s.ordinal
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.hold_evaluation_adoption_attempts(p_run uuid)
RETURNS SETOF yoao_learning_v1._evaluation_attempt_rows
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT a.* FROM yoao_learning_v1._evaluation_attempt_rows a
    WHERE a.run_id = p_run ORDER BY a.id LIMIT 10001 FOR SHARE OF a
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.hold_evaluation_adoption_samples(p_run uuid)
RETURNS SETOF yoao_learning_v1._evaluation_sample_rows
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT s.* FROM yoao_learning_v1._evaluation_sample_rows s
    WHERE s.run_id = p_run ORDER BY s.ordinal LIMIT 33 FOR SHARE OF s
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.pin_retained_evaluation_storage(p_project uuid, p_run uuid, p_ref jsonb)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    UPDATE public.sample_evaluation_runs
    SET storage_ref = p_ref
    WHERE project_id = p_project AND id = p_run AND storage_ref IS NULL
      AND status IN ('succeeded', 'failed', 'cancelled')
      AND p_ref->>'origin' = 'verified-retained' AND jsonb_typeof(p_ref->'evidence') = 'object';
    RETURN FOUND;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.create_sample_evaluation(
    p_project uuid, p_run uuid, p_creator uuid, p_dataset uuid, p_model uuid, p_deployment uuid,
    p_request uuid, p_digest text, p_name text, p_spec jsonb, p_spec_digest text,
    p_ref jsonb, p_key uuid, p_sample_requests uuid[]
)
RETURNS SETOF yoao_learning_v1._evaluation_run_rows
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF p_ref IS NULL OR cardinality(p_sample_requests) IS NULL OR cardinality(p_sample_requests) NOT BETWEEN 1 AND 32
       OR cardinality(p_sample_requests) IS DISTINCT FROM jsonb_array_length(p_spec->'samples')
       OR p_ref->>'origin' IS DISTINCT FROM 'new'
       OR p_spec->>'evaluation_id' IS DISTINCT FROM p_run::text
       OR p_spec->>'dataset_id' IS DISTINCT FROM p_dataset::text
       OR p_spec#>>'{model_ref,project_id}' IS DISTINCT FROM p_project::text
       OR p_spec#>>'{model_ref,model_version_id}' IS DISTINCT FROM p_model::text
       OR p_spec#>>'{model_ref,deployment_id}' IS DISTINCT FROM p_deployment::text
       OR NOT EXISTS (SELECT 1 FROM public.training_dataset_versions v
                      WHERE v.id = p_dataset AND v.project_id = p_project AND v.status = 'ready')
       OR NOT EXISTS (
           SELECT 1 FROM public.trained_model_versions v
           JOIN public.trained_model_deployments d ON d.model_version_id = v.id
           WHERE v.project_id = p_project AND v.id = p_model AND d.id = p_deployment
             AND d.status IN ('ready', 'draining')
             AND d.registry_id::text = p_spec#>>'{model_ref,registry_id}'
       )
    THEN RETURN; END IF;
    INSERT INTO public.sample_evaluation_runs
        (id, project_id, creator_id, dataset_id, model_version_id, deployment_id, request_id,
         request_digest, name, status, spec, spec_digest, storage_ref, "authorization", generation)
    VALUES (p_run, p_project, p_creator, p_dataset, p_model, p_deployment, p_request,
            p_digest, p_name, 'queued', p_spec, p_spec_digest, p_ref,
            jsonb_build_object('api_key_id', p_key), 0);
    INSERT INTO public.sample_evaluation_samples (run_id, ordinal, request_id, status)
    SELECT p_run, sample.ordinal - 1, sample.request_id, 'pending'
    FROM unnest(p_sample_requests) WITH ORDINALITY AS sample(request_id, ordinal);
    RETURN QUERY SELECT r.* FROM yoao_learning_v1._evaluation_run_rows r WHERE r.id = p_run;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.queue_evaluation_attempt(
    p_project uuid, p_run uuid, p_generation integer, p_previous uuid, p_attempt uuid, p_job uuid,
    p_at timestamptz
)
RETURNS SETOF yoao_learning_v1._evaluation_attempt_rows
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE v_ordinal integer;
BEGIN
    PERFORM r.id FROM public.sample_evaluation_runs r
    WHERE r.project_id = p_project AND r.id = p_run AND r.generation = p_generation
      AND r.current_attempt_id IS NOT DISTINCT FROM p_previous
      AND r.storage_ref IS NOT NULL AND r.deletion_requested_at IS NULL
      AND EXISTS (
          SELECT 1 FROM yoao_jobs_v1.read_job(p_job) j
          WHERE j.kind = 'sample_evaluation' AND j.status = 'pending' AND j.project_id = r.project_id
            AND j.user_id IS NOT DISTINCT FROM r.creator_id
            AND j.payload->>'evaluation_id' = p_run::text AND j.payload->>'attempt_id' = p_attempt::text
      )
      AND ((p_generation = 0 AND p_previous IS NULL AND r.status = 'queued')
        OR (p_generation > 0 AND p_previous IS NOT NULL AND r.status IN ('lost', 'failed')
            AND (r.spec->>'expires_at')::bigint > floor(extract(epoch FROM p_at))::bigint)) FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;
    IF p_previous IS NOT NULL THEN
        UPDATE public.sample_evaluation_attempts SET claim_id = NULL,
            completed_at = CASE WHEN status = 'lost' THEN p_at ELSE completed_at END
        WHERE id = p_previous AND run_id = p_run AND generation = p_generation;
        IF NOT FOUND THEN RETURN; END IF;
        UPDATE public.sample_evaluation_samples SET status = 'pending', error_code = NULL, error_message = NULL
        WHERE run_id = p_run AND status = 'failed';
    END IF;
    SELECT coalesce(max(a.ordinal), 0) + 1 INTO v_ordinal
    FROM public.sample_evaluation_attempts a WHERE a.run_id = p_run;
    INSERT INTO public.sample_evaluation_attempts (id, run_id, ordinal, generation, status, async_job_id)
    VALUES (p_attempt, p_run, v_ordinal, p_generation + 1, 'queued', p_job);
    UPDATE public.sample_evaluation_runs SET generation = p_generation + 1,
        current_attempt_id = p_attempt, async_job_id = p_job, status = 'queued',
        error_code = NULL, error_message = NULL, completed_at = NULL
    WHERE id = p_run;
    RETURN QUERY SELECT a.* FROM yoao_learning_v1._evaluation_attempt_rows a WHERE a.id = p_attempt;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1._hold_evaluation_attempt(
    p_run uuid, p_attempt uuid, p_generation integer, p_claim uuid, p_check_claim boolean, p_statuses text[]
)
RETURNS boolean LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM r.id FROM public.sample_evaluation_runs r
    WHERE r.id = p_run AND r.current_attempt_id = p_attempt AND r.generation = p_generation
      AND r.deletion_requested_at IS NULL AND r.status = ANY(p_statuses)
    FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    RETURN EXISTS (
        SELECT 1 FROM public.sample_evaluation_attempts a
        WHERE a.id = p_attempt AND a.run_id = p_run AND a.generation = p_generation
          AND (NOT p_check_claim OR a.claim_id IS NOT DISTINCT FROM p_claim)
    );
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.claim_evaluation_attempt(
    p_run uuid, p_attempt uuid, p_generation integer, p_claim uuid, p_at timestamptz
)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF p_claim IS NULL OR NOT yoao_learning_v1._hold_evaluation_attempt(
        p_run, p_attempt, p_generation, NULL, true, ARRAY['queued']) THEN RETURN false; END IF;
    UPDATE public.sample_evaluation_runs SET status = 'running' WHERE id = p_run;
    UPDATE public.sample_evaluation_attempts SET status = 'running', claim_id = p_claim,
        heartbeat_at = p_at, started_at = p_at WHERE id = p_attempt;
    RETURN true;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.heartbeat_evaluation_attempt(
    p_run uuid, p_attempt uuid, p_generation integer, p_claim uuid, p_at timestamptz
)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF p_claim IS NULL OR NOT yoao_learning_v1._hold_evaluation_attempt(
        p_run, p_attempt, p_generation, p_claim, true, ARRAY['running','cancelling']) THEN RETURN false; END IF;
    UPDATE public.sample_evaluation_attempts SET heartbeat_at = p_at WHERE id = p_attempt;
    RETURN true;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.record_evaluation_dispatch(
    p_run uuid, p_attempt uuid, p_generation integer, p_at timestamptz
)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT yoao_learning_v1._hold_evaluation_attempt(
        p_run, p_attempt, p_generation, NULL, true, ARRAY['queued']) THEN RETURN false; END IF;
    UPDATE public.sample_evaluation_attempts SET dispatched_at = p_at WHERE id = p_attempt;
    RETURN true;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.evaluation_maintenance_candidates(p_at timestamptz)
RETURNS TABLE(id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT r.id FROM public.sample_evaluation_runs r
    JOIN public.sample_evaluation_attempts a ON a.id = r.current_attempt_id AND a.run_id = r.id
    WHERE r.deletion_requested_at IS NOT NULL OR r.status = 'cancelling'
       OR (r.status = 'queued' AND (a.dispatched_at IS NULL OR a.dispatched_at < p_at - interval '30 seconds'))
       OR (r.status = 'running' AND (a.heartbeat_at IS NULL OR a.heartbeat_at < p_at - interval '45 seconds'))
    ORDER BY a.maintenance_at ASC NULLS FIRST, r.id LIMIT 100
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.record_evaluation_maintenance(
    p_run uuid, p_attempt uuid, p_generation integer, p_at timestamptz
)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    PERFORM r.id FROM public.sample_evaluation_runs r WHERE r.id = p_run
      AND r.current_attempt_id = p_attempt AND r.generation = p_generation FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE public.sample_evaluation_attempts SET maintenance_at = p_at
    WHERE id = p_attempt AND run_id = p_run AND generation = p_generation;
    RETURN FOUND;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.start_evaluation_sample(
    p_run uuid, p_attempt uuid, p_generation integer, p_claim uuid, p_ordinal integer, p_request jsonb
)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF p_claim IS NULL OR NOT yoao_learning_v1._hold_evaluation_attempt(
        p_run, p_attempt, p_generation, p_claim, true, ARRAY['running']) THEN RETURN false; END IF;
    UPDATE public.sample_evaluation_samples SET request = p_request, status = 'inflight'
    WHERE run_id = p_run AND ordinal = p_ordinal AND evidence IS NULL
      AND p_request#>>'{evaluation,request_id}' = request_id::text
      AND (request IS NULL OR request = p_request);
    RETURN FOUND;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.checkpoint_evaluation_sample(
    p_run uuid, p_attempt uuid, p_generation integer, p_claim uuid, p_ordinal integer,
    p_request jsonb, p_evidence jsonb, p_recovered boolean
)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF (NOT p_recovered AND p_claim IS NULL) OR NOT yoao_learning_v1._hold_evaluation_attempt(
        p_run, p_attempt, p_generation, p_claim, NOT p_recovered,
        CASE WHEN p_recovered THEN ARRAY['cancelling'] ELSE ARRAY['running','cancelling'] END
    ) THEN RETURN false; END IF;
    UPDATE public.sample_evaluation_samples SET evidence = p_evidence, status = 'succeeded',
        produced_attempt_id = p_attempt, error_code = CASE WHEN p_recovered THEN error_code END,
        error_message = CASE WHEN p_recovered THEN error_message END
    WHERE run_id = p_run AND ordinal = p_ordinal AND evidence IS NULL AND request = p_request
      AND p_evidence->>'produced_attempt_id' = p_attempt::text
      AND p_evidence->>'request_id' = request_id::text;
    RETURN FOUND;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.fail_evaluation_sample_input(
    p_run uuid, p_attempt uuid, p_generation integer, p_claim uuid, p_ordinal integer
)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF p_claim IS NULL OR NOT yoao_learning_v1._hold_evaluation_attempt(
        p_run, p_attempt, p_generation, p_claim, true, ARRAY['running','cancelling']) THEN RETURN false; END IF;
    UPDATE public.sample_evaluation_samples SET status = 'failed',
        error_code = 'frozen_input_unavailable', error_message = '冻结图片未通过完整性、尺寸或解码检查；未运行推理'
    WHERE run_id = p_run AND ordinal = p_ordinal AND request IS NULL AND evidence IS NULL;
    RETURN FOUND;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.request_evaluation_cancellation(
    p_run uuid, p_attempt uuid, p_generation integer, p_code text, p_message text, p_release boolean
)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE v_cancelling boolean;
BEGIN
    IF NOT yoao_learning_v1._hold_evaluation_attempt(
        p_run, p_attempt, p_generation, NULL, false, ARRAY['queued','running','lost','cancelling']
    ) THEN RETURN false; END IF;
    SELECT r.status = 'cancelling' INTO v_cancelling FROM public.sample_evaluation_runs r WHERE r.id = p_run;
    IF NOT v_cancelling OR p_code IS NOT NULL THEN
        p_code := coalesce(p_code, 'cancel_requested');
        p_message := coalesce(p_message, '已请求取消，正在确认旧推理已停止');
        UPDATE public.sample_evaluation_runs SET status = 'cancelling', error_code = p_code,
            error_message = p_message WHERE id = p_run;
        UPDATE public.sample_evaluation_attempts SET status = 'cancelling', error_code = p_code,
            error_message = p_message WHERE id = p_attempt;
    END IF;
    IF p_release THEN
        UPDATE public.sample_evaluation_attempts SET claim_id = NULL WHERE id = p_attempt AND claim_id IS NOT NULL;
    END IF;
    RETURN true;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.lose_evaluation_attempt(
    p_run uuid, p_attempt uuid, p_generation integer, p_claim uuid, p_status text, p_code text, p_message text
)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF p_status NOT IN ('queued', 'running', 'lost') OR NOT yoao_learning_v1._hold_evaluation_attempt(
        p_run, p_attempt, p_generation, p_claim, true, ARRAY[p_status]
    ) THEN RETURN false; END IF;
    UPDATE public.sample_evaluation_runs SET status = 'lost', error_code = p_code,
        error_message = p_message WHERE id = p_run;
    UPDATE public.sample_evaluation_attempts SET status = 'lost', error_code = p_code,
        error_message = p_message, claim_id = NULL WHERE id = p_attempt;
    RETURN true;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.finish_evaluation_attempt(
    p_run uuid, p_attempt uuid, p_generation integer, p_claim uuid, p_status text,
    p_terminal text, p_code text, p_message text, p_report jsonb, p_at timestamptz
)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT ((p_status = 'running' AND p_claim IS NOT NULL AND p_terminal IN ('succeeded','failed'))
            OR (p_status = 'cancelling' AND p_terminal IN ('cancelled','failed')))
       OR NOT yoao_learning_v1._hold_evaluation_attempt(
           p_run, p_attempt, p_generation, p_claim, p_status = 'running', ARRAY[p_status]
       ) THEN RETURN false; END IF;
    IF p_terminal = 'succeeded' AND (
        jsonb_typeof(p_report) IS DISTINCT FROM 'object' OR EXISTS (
            SELECT 1 FROM public.sample_evaluation_samples s
            WHERE s.run_id = p_run AND (s.status <> 'succeeded' OR s.evidence IS NULL)
        )
    ) THEN RETURN false; END IF;
    UPDATE public.sample_evaluation_runs SET status = p_terminal, completed_at = p_at,
        error_code = p_code, error_message = p_message WHERE id = p_run;
    UPDATE public.sample_evaluation_attempts SET status = p_terminal, completed_at = p_at,
        error_code = p_code, error_message = p_message, report = nullif(p_report, 'null'::jsonb),
        claim_id = CASE WHEN p_status = 'running' THEN claim_id END
    WHERE id = p_attempt;
    RETURN true;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.request_evaluation_deletion(p_project uuid, p_run uuid, p_at timestamptz)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    UPDATE public.sample_evaluation_runs SET deletion_requested_at = coalesce(deletion_requested_at, p_at)
    WHERE project_id = p_project AND id = p_run AND status IN ('succeeded', 'failed', 'cancelled')
      AND NOT EXISTS (SELECT 1 FROM public.active_learning_references r WHERE r.evaluation_id = p_run);
    RETURN FOUND;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.purge_deleted_evaluation(p_project uuid, p_run uuid, p_ref jsonb)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    DELETE FROM public.sample_evaluation_runs
    WHERE project_id = p_project AND id = p_run AND status IN ('succeeded', 'failed', 'cancelled')
      AND deletion_requested_at IS NOT NULL AND storage_ref = p_ref;
    RETURN FOUND;
END $$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.pending_dataset_mask_keys()
RETURNS TABLE(object_key text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT DISTINCT value #>> '{}' FROM public.training_dataset_versions v,
         LATERAL jsonb_path_query(v.snapshot, '$.**.object_key') value
    WHERE v.status <> 'ready' AND v.snapshot->>'bridge' = 'ultralytics-instance-rle-1'
      AND value #>> '{}' LIKE 'raster-masks/sha256/%'
$$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.pending_dataset_mask_referenced(p_key text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS (SELECT 1 FROM public.training_dataset_versions v,
                         LATERAL jsonb_path_query(v.snapshot, '$.**.object_key') value
                   WHERE v.status <> 'ready' AND v.snapshot->>'bridge' = 'ultralytics-instance-rle-1'
                     AND value #>> '{}' = p_key)
$$;
