CREATE VIEW yoao_learning_v1._training_run_facts AS
SELECT id, project_id, creator_id, study_id, dataset_id, initialization_model_id,
       auto_trigger_id, async_job_id, request_digest, name, status, config,
       config_digest, origin, storage_ref, "authorization", current_attempt_id,
       metrics, error_message, created_at, completed_at
FROM public.training_runs;

-- aap-statement-break

CREATE VIEW yoao_learning_v1._training_attempt_facts AS
SELECT id, run_id, ordinal, status, resume_from_id, executor_id, boot_id, claim_id,
       finish_request_digest, resource_id, runtime, upload_inventory, artifacts,
       observation_state, failure, native_budget, native_accounting, error_message,
       heartbeat_at, started_at, completed_at, created_at
FROM public.training_attempts;

-- aap-statement-break

CREATE VIEW yoao_learning_v1._training_preview_facts AS
SELECT id, project_id, dataset_id, creator_id, async_job_id, request_digest, request,
       input_digest, spec, storage_ref, "authorization", status, resource_id,
       executor_id, boot_id, claim_id, finish_digest, runtime, upload_inventory,
       artifacts, error_message, heartbeat_at, started_at, completed_at,
       put_expires_at, expires_at, created_at
FROM public.training_previews;

-- aap-statement-break

CREATE VIEW yoao_learning_v1.training_run_summary AS
SELECT id, project_id, creator_id, study_id, dataset_id, initialization_model_id,
       auto_trigger_id, async_job_id, name, status, config, config_digest, origin,
       current_attempt_id, metrics, error_message, created_at, completed_at
FROM public.training_runs CROSS JOIN (SELECT 1) AS projection;

-- aap-statement-break

CREATE VIEW yoao_learning_v1.training_run_storage_owner AS
SELECT id, project_id, dataset_id, current_attempt_id, status, storage_ref
FROM public.training_runs CROSS JOIN (SELECT 1) AS projection;

-- aap-statement-break

CREATE VIEW yoao_learning_v1.training_preview_storage_owner AS
SELECT id, project_id, dataset_id, claim_id, status, storage_ref
FROM public.training_previews CROSS JOIN (SELECT 1) AS projection;

-- aap-statement-break

CREATE VIEW yoao_learning_v1.training_run_lineage AS
SELECT id, project_id, dataset_id, initialization_model_id, current_attempt_id,
       name, status, config_digest, created_at,
       CASE WHEN jsonb_typeof(origin -> 'kind') = 'string'
            THEN left((origin -> 'kind') #>> '{}', 256) END AS origin_kind,
       CASE WHEN jsonb_typeof(origin -> 'run_id') = 'string'
            THEN left((origin -> 'run_id') #>> '{}', 256) END AS origin_run_id,
       CASE WHEN jsonb_typeof(origin -> 'attempt_id') = 'string'
            THEN left((origin -> 'attempt_id') #>> '{}', 256) END AS origin_attempt_id,
       CASE WHEN jsonb_typeof(origin -> 'recipe_id') = 'string'
            THEN left((origin -> 'recipe_id') #>> '{}', 256) END AS origin_recipe_id,
       CASE WHEN jsonb_typeof(origin -> 'revision_id') = 'string'
            THEN left((origin -> 'revision_id') #>> '{}', 256) END AS origin_revision_id,
       CASE WHEN jsonb_typeof(origin -> 'digest') = 'string'
            THEN left((origin -> 'digest') #>> '{}', 256) END AS origin_digest,
       CASE WHEN jsonb_typeof(config -> 'framework') = 'string'
            THEN left((config -> 'framework') #>> '{}', 256) END AS framework,
       CASE WHEN jsonb_typeof(config -> 'dataset_digest') = 'string'
            THEN left((config -> 'dataset_digest') #>> '{}', 256) END AS dataset_digest,
       CASE WHEN jsonb_typeof(config -> 'validation_digest') = 'string'
            THEN left((config -> 'validation_digest') #>> '{}', 256) END AS validation_digest,
       CASE WHEN jsonb_typeof(config -> 'initialization' -> 'kind') = 'string'
            THEN left((config -> 'initialization' -> 'kind') #>> '{}', 256) END AS initialization_kind,
       CASE WHEN jsonb_typeof(config -> 'initialization' -> 'project_id') = 'string'
            THEN left((config -> 'initialization' -> 'project_id') #>> '{}', 256) END AS initialization_project_id,
       CASE WHEN jsonb_typeof(config -> 'initialization' -> 'model_version_id') = 'string'
            THEN left((config -> 'initialization' -> 'model_version_id') #>> '{}', 256) END AS initialization_model_version_id,
       CASE WHEN jsonb_typeof(config -> 'initialization' -> 'run_id') = 'string'
            THEN left((config -> 'initialization' -> 'run_id') #>> '{}', 256) END AS initialization_run_id,
       CASE WHEN jsonb_typeof(config -> 'initialization' -> 'attempt_id') = 'string'
            THEN left((config -> 'initialization' -> 'attempt_id') #>> '{}', 256) END AS initialization_attempt_id,
       CASE WHEN jsonb_typeof(config -> 'initialization' -> 'source_digest') = 'string'
            THEN left((config -> 'initialization' -> 'source_digest') #>> '{}', 256) END AS initialization_source_digest,
       CASE WHEN jsonb_typeof(config -> 'initialization' -> 'policy_digest') = 'string'
            THEN left((config -> 'initialization' -> 'policy_digest') #>> '{}', 256) END AS initialization_policy_digest
FROM public.training_runs CROSS JOIN (SELECT 1) AS projection;

-- aap-statement-break

CREATE VIEW yoao_learning_v1.training_attempt_lineage AS
SELECT a.id, a.run_id, r.project_id, a.ordinal, a.resume_from_id, a.status, a.created_at
FROM public.training_attempts AS a JOIN public.training_runs AS r ON r.id = a.run_id;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_get_run(p_project uuid, p_id uuid, p_lock text)
RETURNS SETOF yoao_learning_v1._training_run_facts
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock = 'update' THEN
        RETURN QUERY SELECT r.* FROM yoao_learning_v1._training_run_facts AS r WHERE r.id = p_id AND r.project_id = p_project FOR UPDATE;
    ELSIF p_lock = 'share' THEN
        RETURN QUERY SELECT r.* FROM yoao_learning_v1._training_run_facts AS r WHERE r.id = p_id AND r.project_id = p_project FOR SHARE;
    ELSIF p_lock = 'none' THEN
        RETURN QUERY SELECT r.* FROM yoao_learning_v1._training_run_facts AS r WHERE r.id = p_id AND r.project_id = p_project;
    ELSE
        RAISE EXCEPTION 'Invalid training lock' USING ERRCODE = '22023';
    END IF;
END;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_get_attempt(p_id uuid, p_lock text) RETURNS SETOF yoao_learning_v1._training_attempt_facts
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock = 'update' THEN
        RETURN QUERY SELECT r.* FROM yoao_learning_v1._training_attempt_facts AS r WHERE r.id = p_id FOR UPDATE;
    ELSIF p_lock = 'share' THEN
        RETURN QUERY SELECT r.* FROM yoao_learning_v1._training_attempt_facts AS r WHERE r.id = p_id FOR SHARE;
    ELSIF p_lock = 'none' THEN
        RETURN QUERY SELECT r.* FROM yoao_learning_v1._training_attempt_facts AS r WHERE r.id = p_id;
    ELSE
        RAISE EXCEPTION 'Invalid training lock' USING ERRCODE = '22023';
    END IF;
END;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_get_preview(p_project uuid, p_id uuid, p_lock text)
RETURNS SETOF yoao_learning_v1._training_preview_facts
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock = 'update' THEN
        RETURN QUERY SELECT r.* FROM yoao_learning_v1._training_preview_facts AS r WHERE r.id = p_id AND r.project_id = p_project FOR UPDATE;
    ELSIF p_lock = 'share' THEN
        RETURN QUERY SELECT r.* FROM yoao_learning_v1._training_preview_facts AS r WHERE r.id = p_id AND r.project_id = p_project FOR SHARE;
    ELSIF p_lock = 'none' THEN
        RETURN QUERY SELECT r.* FROM yoao_learning_v1._training_preview_facts AS r WHERE r.id = p_id AND r.project_id = p_project;
    ELSE
        RAISE EXCEPTION 'Invalid training lock' USING ERRCODE = '22023';
    END IF;
END;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_executor_run(p_id uuid, p_lock boolean)
RETURNS SETOF yoao_learning_v1._training_run_facts
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock THEN
        RETURN QUERY SELECT r.* FROM yoao_learning_v1._training_run_facts AS r WHERE r.id = p_id FOR UPDATE;
    ELSE
        RETURN QUERY SELECT r.* FROM yoao_learning_v1._training_run_facts AS r WHERE r.id = p_id;
    END IF;
END;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_executor_preview(p_id uuid, p_lock boolean)
RETURNS SETOF yoao_learning_v1._training_preview_facts
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock THEN
        RETURN QUERY SELECT r.* FROM yoao_learning_v1._training_preview_facts AS r WHERE r.id = p_id FOR UPDATE;
    ELSE
        RETURN QUERY SELECT r.* FROM yoao_learning_v1._training_preview_facts AS r WHERE r.id = p_id;
    END IF;
END;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_attempt_proof(p_id uuid, p_executor text, p_boot uuid, p_digest text)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT EXISTS (
    SELECT FROM public.training_attempts
    WHERE id = p_id AND executor_id = p_executor AND boot_id = p_boot AND lease_digest = p_digest
);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_preview_proof(p_id uuid, p_executor text, p_boot uuid, p_digest text)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT EXISTS (
    SELECT FROM public.training_previews
    WHERE id = p_id AND executor_id = p_executor AND boot_id = p_boot AND lease_digest = p_digest
);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_run_request(p_project uuid, p_request uuid)
RETURNS SETOF yoao_learning_v1._training_run_facts
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT r.* FROM public.training_runs AS t
JOIN yoao_learning_v1._training_run_facts AS r ON r.id = t.id
WHERE t.project_id = p_project AND t.request_id = p_request;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_gpu_queue() RETURNS SETOF yoao_learning_v1._training_run_facts
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT r.* FROM yoao_learning_v1._training_run_facts AS r
WHERE r.status = 'queued' ORDER BY r.created_at LIMIT 100 FOR UPDATE SKIP LOCKED;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_study_children(p_study uuid, p_lock boolean)
RETURNS SETOF yoao_learning_v1._training_run_facts
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock THEN
        RETURN QUERY SELECT r.* FROM yoao_learning_v1._training_run_facts AS r
        WHERE r.study_id = p_study ORDER BY r.id FOR UPDATE;
    ELSE
        RETURN QUERY SELECT r.* FROM yoao_learning_v1._training_run_facts AS r
        WHERE r.study_id = p_study ORDER BY r.id;
    END IF;
END;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_study_attempts(p_study uuid) RETURNS SETOF yoao_learning_v1._training_attempt_facts
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT a.* FROM yoao_learning_v1._training_attempt_facts AS a
JOIN public.training_runs AS r ON r.id = a.run_id WHERE r.study_id = p_study;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_run_attempts(p_run uuid, p_desc boolean)
RETURNS SETOF yoao_learning_v1._training_attempt_facts
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT a.* FROM yoao_learning_v1._training_attempt_facts AS a WHERE a.run_id = p_run
ORDER BY CASE WHEN p_desc THEN -a.ordinal ELSE a.ordinal END;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_adoption_attempts(p_run uuid) RETURNS SETOF yoao_learning_v1._training_attempt_facts
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT a.* FROM yoao_learning_v1._training_attempt_facts AS a WHERE a.run_id = p_run
ORDER BY a.id LIMIT 10001 FOR SHARE;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_trigger_run(p_trigger uuid, p_lock boolean)
RETURNS SETOF yoao_learning_v1._training_run_facts
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock THEN
        RETURN QUERY SELECT r.* FROM yoao_learning_v1._training_run_facts AS r
        WHERE r.auto_trigger_id = p_trigger FOR UPDATE;
    ELSE
        RETURN QUERY SELECT r.* FROM yoao_learning_v1._training_run_facts AS r
        WHERE r.auto_trigger_id = p_trigger;
    END IF;
END;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_active_auto_run(p_project uuid, p_excluding uuid)
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT id FROM public.training_runs WHERE project_id = p_project AND auto_trigger_id IS NOT NULL
  AND status IN ('queued','preparing','training','evaluating','uploading','cancelling','lost')
  AND id IS DISTINCT FROM p_excluding LIMIT 1;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_trigger_run_ids(p_project uuid, p_triggers uuid[])
RETURNS TABLE (auto_trigger_id uuid, id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT auto_trigger_id, id FROM public.training_runs
WHERE project_id = p_project AND auto_trigger_id = ANY(p_triggers);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_dataset_run_dependencies(p_project uuid, p_dataset uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT EXISTS (SELECT FROM public.training_runs WHERE project_id = p_project AND dataset_id = p_dataset)
    OR EXISTS (SELECT FROM public.training_previews WHERE project_id = p_project AND dataset_id = p_dataset);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_run_has_lease(p_run uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT EXISTS (
    SELECT FROM public.training_resource_leases AS l
    JOIN public.training_attempts AS a ON a.id = l.attempt_id WHERE a.run_id = p_run
);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_create_run(
    p_id uuid, p_project uuid, p_creator uuid, p_dataset uuid, p_model uuid, p_study uuid,
    p_trigger uuid, p_request uuid, p_request_digest text, p_name text, p_config jsonb,
    p_config_digest text, p_origin jsonb, p_authorization jsonb
)
RETURNS SETOF yoao_learning_v1._training_run_facts
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_study IS NOT NULL AND p_trigger IS NOT NULL THEN
        RAISE EXCEPTION 'A training run cannot be both a study child and an automatic run'
            USING ERRCODE = '22023';
    END IF;
    INSERT INTO public.training_runs (
        id, project_id, creator_id, dataset_id, initialization_model_id, study_id,
        auto_trigger_id, request_id, request_digest, name, config, config_digest,
        origin, "authorization", status, metrics
    ) VALUES (
        p_id, p_project, p_creator, p_dataset, p_model, p_study,
        p_trigger, p_request, p_request_digest, p_name, p_config, p_config_digest,
        p_origin, p_authorization, 'queued', '{}'::jsonb
    );
    RETURN QUERY SELECT r.* FROM yoao_learning_v1._training_run_facts AS r WHERE r.id = p_id;
END;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_pin_run_storage(p_run uuid, p_ref jsonb, p_retained boolean)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH pinned AS (
    UPDATE public.training_runs AS r SET storage_ref = p_ref
    WHERE r.id = p_run AND r.storage_ref IS NULL AND (
        (NOT p_retained AND r.status = 'queued' AND r.current_attempt_id IS NULL
         AND p_ref->>'origin' = 'new') OR
        (p_retained AND r.status IN ('succeeded','failed','cancelled')
         AND p_ref->>'origin' = 'verified-retained'
         AND NOT EXISTS (
             SELECT FROM public.training_attempts AS a WHERE a.run_id = r.id
             AND a.status NOT IN ('succeeded','failed','cancelled')
         ) AND NOT yoao_learning_v1.training_run_has_lease(r.id))
    ) RETURNING 1
) SELECT EXISTS (SELECT FROM pinned);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_enqueue_attempt(p_run uuid, p_previous uuid, p_attempt uuid, p_job uuid)
RETURNS SETOF yoao_learning_v1._training_attempt_facts
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.training_runs AS r
    SET current_attempt_id = p_attempt, async_job_id = p_job, status = 'queued',
        metrics = '{}'::jsonb, error_message = NULL, completed_at = NULL
    WHERE r.id = p_run AND r.current_attempt_id IS NOT DISTINCT FROM p_previous
      AND r.storage_ref IS NOT NULL AND (
          (p_previous IS NULL AND r.status = 'queued') OR
          (p_previous IS NOT NULL AND r.status IN ('failed','cancelled') AND EXISTS (
              SELECT FROM public.training_attempts AS a WHERE a.id = p_previous
              AND a.run_id = r.id AND a.status IN ('failed','cancelled')
          ) AND NOT yoao_learning_v1.training_run_has_lease(r.id))
      );
    IF NOT FOUND THEN RETURN; END IF;
    INSERT INTO public.training_attempts (
        id, run_id, ordinal, resume_from_id, status, runtime, upload_inventory, artifacts, observation_state
    ) VALUES (
        p_attempt, p_run,
        (SELECT COALESCE(max(a.ordinal), 0) + 1 FROM public.training_attempts AS a WHERE a.run_id = p_run),
        p_previous, 'queued', '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb
    );
    RETURN QUERY SELECT a.* FROM yoao_learning_v1._training_attempt_facts AS a WHERE a.id = p_attempt;
END;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_reserve_study_attempt(p_run uuid, p_attempt uuid, p_budget jsonb)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH reserved AS (
    UPDATE public.training_attempts AS a SET native_budget = p_budget
    FROM public.training_runs AS r
    WHERE r.id = p_run AND r.study_id IS NOT NULL AND r.current_attempt_id = a.id
      AND a.id = p_attempt AND a.run_id = r.id AND a.status = 'queued' AND a.native_budget IS NULL
      AND p_budget->>'attempt_id' = a.id::text AND p_budget->>'config_digest' = r.config_digest
    RETURNING 1
) SELECT EXISTS (SELECT FROM reserved);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_manual_resume(p_run uuid, p_attempt uuid, p_study boolean)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH resumed AS (
    UPDATE public.training_runs AS r
    SET "authorization" = r."authorization" || jsonb_build_object(
        CASE WHEN p_study THEN 'study_manual_resume' ELSE 'manual_resume' END, true
    )
    WHERE r.id = p_run AND r.current_attempt_id = p_attempt AND r.status = 'queued'
      AND ((p_study AND r.study_id IS NOT NULL) OR (NOT p_study AND r.auto_trigger_id IS NOT NULL))
    RETURNING 1
) SELECT EXISTS (SELECT FROM resumed);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_delete_run(p_project uuid, p_run uuid)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH removed AS (
    DELETE FROM public.training_runs AS r
    WHERE r.id = p_run AND r.project_id = p_project AND r.study_id IS NULL
      AND r.status IN ('succeeded','failed','cancelled') RETURNING 1
) SELECT EXISTS (SELECT FROM removed);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_gpu_lease(p_resource text) RETURNS TABLE (resource_id varchar, attempt_id uuid, executor_id varchar, boot_id uuid, runtime jsonb, last_seen_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT l.resource_id, l.attempt_id, l.executor_id, l.boot_id, l.runtime, l.last_seen_at
FROM public.training_resource_leases AS l WHERE l.resource_id = p_resource;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_lock_gpu_lease(p_resource text) RETURNS TABLE (resource_id varchar, attempt_id uuid, executor_id varchar, boot_id uuid, runtime jsonb, last_seen_at timestamptz)
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
INSERT INTO public.training_resource_leases (resource_id, runtime) VALUES (p_resource, '{}'::jsonb)
ON CONFLICT DO NOTHING;
SELECT l.resource_id, l.attempt_id, l.executor_id, l.boot_id, l.runtime, l.last_seen_at
FROM public.training_resource_leases AS l WHERE l.resource_id = p_resource FOR UPDATE;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_observe_gpu(p_resource text, p_executor text, p_boot uuid, p_runtime jsonb, p_at timestamptz)
RETURNS void
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
UPDATE public.training_resource_leases
SET executor_id = p_executor, boot_id = p_boot, runtime = p_runtime, last_seen_at = p_at, updated_at = now()
WHERE resource_id = p_resource;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_cpu_lease(p_resource text) RETURNS TABLE (resource_id varchar, preview_id uuid, executor_id varchar, boot_id uuid, runtime jsonb, last_seen_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT l.resource_id, l.preview_id, l.executor_id, l.boot_id, l.runtime, l.last_seen_at
FROM public.training_preview_resource_leases AS l WHERE l.resource_id = p_resource;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_lock_cpu_lease(p_resource text) RETURNS TABLE (resource_id varchar, preview_id uuid, executor_id varchar, boot_id uuid, runtime jsonb, last_seen_at timestamptz)
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
INSERT INTO public.training_preview_resource_leases (resource_id, runtime) VALUES (p_resource, '{}'::jsonb)
ON CONFLICT DO NOTHING;
SELECT l.resource_id, l.preview_id, l.executor_id, l.boot_id, l.runtime, l.last_seen_at
FROM public.training_preview_resource_leases AS l WHERE l.resource_id = p_resource FOR UPDATE;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_observe_cpu(p_resource text, p_executor text, p_boot uuid, p_runtime jsonb, p_at timestamptz)
RETURNS void
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
UPDATE public.training_preview_resource_leases
SET executor_id = p_executor, boot_id = p_boot, runtime = p_runtime, last_seen_at = p_at
WHERE resource_id = p_resource;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_claim_attempt(
    p_run uuid, p_attempt uuid, p_resource text, p_executor text, p_boot uuid,
    p_claim uuid, p_digest text, p_runtime jsonb, p_at timestamptz
)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.training_resource_leases SET attempt_id = p_attempt, updated_at = now()
    WHERE resource_id = p_resource AND attempt_id IS NULL
      AND executor_id = p_executor AND boot_id = p_boot;
    IF NOT FOUND THEN RAISE EXCEPTION 'GPU lease changed' USING ERRCODE = '40001'; END IF;
    UPDATE public.training_runs SET status = 'preparing'
    WHERE id = p_run AND current_attempt_id = p_attempt AND status = 'queued';
    IF NOT FOUND THEN RAISE EXCEPTION 'Training run changed' USING ERRCODE = '40001'; END IF;
    UPDATE public.training_attempts
    SET status = 'preparing', resource_id = p_resource, executor_id = p_executor, boot_id = p_boot,
        claim_id = p_claim, lease_digest = p_digest, runtime = p_runtime,
        started_at = p_at, heartbeat_at = p_at
    WHERE id = p_attempt AND run_id = p_run AND status = 'queued' AND resource_id IS NULL;
    IF NOT FOUND THEN RAISE EXCEPTION 'Training attempt changed' USING ERRCODE = '40001'; END IF;
END;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_lose_attempt(
    p_run uuid, p_attempt uuid, p_cutoff timestamptz, p_error text, p_failure jsonb
)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH lost AS (
    UPDATE public.training_attempts AS a SET status = 'lost', failure = p_failure
    FROM public.training_runs AS r, public.training_resource_leases AS l
    WHERE a.id = p_attempt AND r.id = p_run AND a.run_id = r.id AND r.current_attempt_id = a.id
      AND l.attempt_id = a.id AND l.resource_id = a.resource_id
      AND a.status IN ('preparing','training','evaluating','uploading','queued')
      AND a.heartbeat_at < p_cutoff
    RETURNING a.run_id
), marked AS (
    UPDATE public.training_runs AS r SET status = 'lost', error_message = p_error
    FROM lost WHERE r.id = lost.run_id RETURNING 1
) SELECT EXISTS (SELECT FROM marked);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_cancel_attempt(p_run uuid, p_attempt uuid, p_status text)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH cancelling AS (
    UPDATE public.training_attempts AS a SET status = 'cancelling' FROM public.training_runs AS r
    WHERE r.id = p_run AND a.id = p_attempt AND a.run_id = r.id AND r.current_attempt_id = a.id
      AND r.status = p_status AND r.status IN ('preparing','training','evaluating','uploading','lost','cancelling')
      AND a.status NOT IN ('succeeded','failed','cancelled')
    RETURNING a.run_id
), marked AS (
    UPDATE public.training_runs AS r SET status = 'cancelling' FROM cancelling
    WHERE r.id = cancelling.run_id RETURNING 1
) SELECT EXISTS (SELECT FROM marked);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_finish_queued_attempt(
    p_run uuid, p_attempt uuid, p_status text, p_at timestamptz, p_error text,
    p_accounting jsonb, p_failure jsonb
)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH finished AS (
    UPDATE public.training_attempts AS a
    SET status = p_status, completed_at = p_at, error_message = p_error,
        native_accounting = COALESCE(p_accounting, a.native_accounting), failure = COALESCE(p_failure, a.failure)
    FROM public.training_runs AS r WHERE r.id = p_run AND r.current_attempt_id = a.id
      AND a.id = p_attempt AND a.run_id = r.id AND r.status = 'queued' AND a.status = 'queued'
      AND a.resource_id IS NULL AND p_status IN ('failed','cancelled')
      AND NOT EXISTS (SELECT FROM public.training_resource_leases WHERE attempt_id = a.id)
    RETURNING a.run_id
), marked AS (
    UPDATE public.training_runs AS r SET status = p_status, completed_at = p_at, error_message = p_error
    FROM finished WHERE r.id = finished.run_id RETURNING 1
) SELECT EXISTS (SELECT FROM marked);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_finish_executor_attempt(
    p_run uuid, p_attempt uuid, p_executor text, p_boot uuid, p_digest text, p_exited boolean,
    p_status text, p_at timestamptz, p_error text, p_accounting jsonb, p_failure jsonb,
    p_finish_digest text, p_artifacts jsonb, p_recovery_boot uuid, p_exclusive boolean
)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH finished AS (
    UPDATE public.training_attempts AS a
    SET status = p_status, completed_at = p_at, error_message = p_error,
        native_accounting = CASE WHEN p_recovery_boot IS NULL THEN p_accounting ELSE a.native_accounting END,
        failure = COALESCE(p_failure, a.failure), artifacts = COALESCE(p_artifacts, a.artifacts),
        finish_request_digest = COALESCE(p_finish_digest, a.finish_request_digest),
        runtime = CASE WHEN p_recovery_boot IS NULL THEN a.runtime
                  ELSE a.runtime || jsonb_build_object('recovery_boot_id', p_recovery_boot) END
    FROM public.training_runs AS r, public.training_resource_leases AS l
    WHERE r.id = p_run AND a.id = p_attempt AND a.run_id = r.id AND r.current_attempt_id = a.id
      AND l.resource_id = a.resource_id AND l.attempt_id = a.id
      AND a.status NOT IN ('succeeded','failed','cancelled')
      AND a.executor_id = p_executor AND a.boot_id = p_boot AND a.lease_digest = p_digest AND p_exited
      AND p_status IN ('succeeded','failed','cancelled') AND (r.status <> 'cancelling' OR p_status = 'cancelled')
      AND ((p_recovery_boot IS NULL AND p_finish_digest IS NOT NULL AND p_artifacts IS NOT NULL) OR
           (p_recovery_boot <> p_boot AND p_exclusive AND p_status <> 'succeeded' AND p_finish_digest IS NULL))
    RETURNING a.id, a.run_id, a.resource_id
), released AS (
    UPDATE public.training_resource_leases AS l SET attempt_id = NULL, updated_at = now()
    FROM finished WHERE l.attempt_id = finished.id AND l.resource_id = finished.resource_id RETURNING 1
), marked AS (
    UPDATE public.training_runs AS r SET status = p_status, completed_at = p_at, error_message = p_error
    FROM finished WHERE r.id = finished.run_id RETURNING 1
) SELECT EXISTS (SELECT FROM marked) AND EXISTS (SELECT FROM released);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_heartbeat_attempt(
    p_run uuid, p_attempt uuid, p_executor text, p_boot uuid, p_digest text,
    p_at timestamptz, p_status text, p_error text, p_failure jsonb, p_metrics jsonb
)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH seen AS (
    UPDATE public.training_attempts AS a
    SET heartbeat_at = p_at, status = p_status, failure = COALESCE(p_failure, a.failure)
    FROM public.training_runs AS r, public.training_resource_leases AS l
    WHERE r.id = p_run AND a.id = p_attempt AND a.run_id = r.id AND r.current_attempt_id = a.id
      AND l.resource_id = a.resource_id AND l.attempt_id = a.id
      AND a.status NOT IN ('succeeded','failed','cancelled')
      AND a.executor_id = p_executor AND a.boot_id = p_boot AND a.lease_digest = p_digest
      AND p_status IN ('preparing','training','evaluating','uploading','cancelling','lost')
      AND (r.status NOT IN ('lost','cancelling') OR p_status IN ('lost','cancelling'))
    RETURNING a.id, a.run_id, a.resource_id
), marked AS (
    UPDATE public.training_runs AS r
    SET status = p_status, error_message = COALESCE(p_error, r.error_message), metrics = COALESCE(p_metrics, r.metrics)
    FROM seen WHERE r.id = seen.run_id RETURNING 1
), touched AS (
    UPDATE public.training_resource_leases AS l SET last_seen_at = p_at, updated_at = now()
    FROM seen WHERE l.resource_id = seen.resource_id AND l.attempt_id = seen.id RETURNING 1
) SELECT EXISTS (SELECT FROM marked) AND EXISTS (SELECT FROM touched);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_attempt_uploads(
    p_run uuid, p_attempt uuid, p_executor text, p_boot uuid, p_digest text, p_inventory jsonb
)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH authorized AS (
    UPDATE public.training_attempts AS a
    SET upload_inventory = p_inventory, status = CASE WHEN r.status IN ('lost','cancelling')
        THEN a.status ELSE 'uploading' END
    FROM public.training_runs AS r, public.training_resource_leases AS l
    WHERE r.id = p_run AND a.id = p_attempt AND a.run_id = r.id AND r.current_attempt_id = a.id
      AND l.resource_id = a.resource_id AND l.attempt_id = a.id
      AND a.executor_id = p_executor AND a.boot_id = p_boot AND a.lease_digest = p_digest
      AND a.status NOT IN ('succeeded','failed','cancelled')
      AND (a.upload_inventory = '{}'::jsonb OR a.upload_inventory = p_inventory)
    RETURNING a.run_id
), marked AS (
    UPDATE public.training_runs AS r SET status = 'uploading' FROM authorized
    WHERE r.id = authorized.run_id AND r.status NOT IN ('lost','cancelling') RETURNING 1
) SELECT EXISTS (SELECT FROM authorized);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_record_event(
    p_attempt uuid, p_executor text, p_boot uuid, p_digest text,
    p_sequence integer, p_kind text, p_payload jsonb
)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_kind text;
    v_payload jsonb;
    v_ceiling integer;
BEGIN
    SELECT (r.config->'parameters'->>'epochs')::integer INTO v_ceiling
    FROM public.training_runs AS r JOIN public.training_attempts AS a ON a.run_id = r.id
    WHERE a.id = p_attempt AND r.current_attempt_id = a.id
      AND a.executor_id = p_executor AND a.boot_id = p_boot AND a.lease_digest = p_digest
      AND EXISTS (
          SELECT FROM public.training_resource_leases AS l
          WHERE l.resource_id = a.resource_id AND l.attempt_id = a.id
      ) AND a.status NOT IN ('succeeded','failed','cancelled') FOR UPDATE OF a;
    IF NOT FOUND THEN RETURN 'stale'; END IF;
    SELECT e.kind, e.payload INTO v_kind, v_payload
    FROM public.training_events AS e WHERE e.attempt_id = p_attempt AND e.sequence = p_sequence;
    IF FOUND THEN
        RETURN CASE WHEN v_kind = p_kind AND v_payload = p_payload THEN 'replay' ELSE 'conflict' END;
    END IF;
    IF p_sequence <> 1 + COALESCE((SELECT max(e.sequence) FROM public.training_events AS e
                                  WHERE e.attempt_id = p_attempt), 0) THEN
        RETURN 'gap';
    END IF;
    IF (p_payload->>'epoch')::integer > v_ceiling THEN RETURN 'epoch_bounds'; END IF;
    IF p_kind NOT IN ('preparing','training','epoch','evaluating','uploading') THEN
        RAISE EXCEPTION 'Invalid training event' USING ERRCODE = '22023';
    END IF;
    INSERT INTO public.training_events (attempt_id, sequence, kind, payload)
    VALUES (p_attempt, p_sequence, p_kind, p_payload);
    RETURN 'accepted';
END;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_event_page(p_attempt uuid, p_after integer, p_latest boolean)
RETURNS TABLE (sequence integer, kind varchar, payload jsonb)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT e.sequence, e.kind, e.payload FROM public.training_events AS e
WHERE e.attempt_id = p_attempt AND e.sequence > p_after
ORDER BY CASE WHEN p_latest THEN -e.sequence ELSE e.sequence END LIMIT 100;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_last_epoch(p_attempt uuid) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT max((e.payload->>'epoch')::integer) FROM public.training_events AS e
WHERE e.attempt_id = p_attempt AND e.kind = 'epoch';
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_set_observation_receipt(
    p_attempt uuid, p_executor text, p_boot uuid, p_digest text, p_state jsonb
)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH observed AS (
    UPDATE public.training_attempts AS a SET observation_state = p_state
    FROM public.training_runs AS r
    WHERE a.id = p_attempt AND r.id = a.run_id AND r.current_attempt_id = a.id
      AND a.executor_id = p_executor AND a.boot_id = p_boot AND a.lease_digest = p_digest
      AND (a.status IN ('succeeded','failed','cancelled') OR EXISTS (
          SELECT FROM public.training_resource_leases AS l WHERE l.attempt_id = a.id AND l.resource_id = a.resource_id
      )) AND (a.observation_state = '{}'::jsonb OR a.observation_state->>'stream_id' = p_state->>'stream_id')
    RETURNING 1
) SELECT EXISTS (SELECT FROM observed);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_telemetry_receipt(p_attempt uuid, p_sequence integer)
RETURNS TABLE (content_digest varchar, stream_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT r.content_digest, r.stream_id FROM public.training_telemetry_samples AS r
WHERE r.attempt_id = p_attempt AND r.sequence = p_sequence;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_append_telemetry(
    p_attempt uuid, p_executor text, p_boot uuid, p_lease_digest text,
    p_stream uuid, p_rows jsonb
)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH inserted AS (
    INSERT INTO public.training_telemetry_samples (
        attempt_id, sequence, stream_id, producer_boot_id, captured_at, content_digest, payload
    )
    SELECT a.id, (i.payload->>'sequence')::integer, p_stream,
           (i.payload->>'producer_boot_id')::uuid, (i.payload->>'captured_at')::timestamptz,
           i.digest, i.payload
    FROM public.training_attempts AS a JOIN public.training_runs AS r ON r.current_attempt_id = a.id
    CROSS JOIN jsonb_to_recordset(p_rows) AS i(digest text, payload jsonb)
    WHERE a.id = p_attempt AND r.id = a.run_id AND a.executor_id = p_executor
      AND a.boot_id = p_boot AND a.lease_digest = p_lease_digest
      AND jsonb_array_length(p_rows) BETWEEN 1 AND 12
      AND (a.status IN ('succeeded','failed','cancelled') OR EXISTS (
          SELECT FROM public.training_resource_leases AS l WHERE l.attempt_id = a.id AND l.resource_id = a.resource_id
      )) AND (a.observation_state = '{}'::jsonb OR a.observation_state->>'stream_id' = p_stream::text)
    RETURNING 1
) SELECT EXISTS (SELECT FROM inserted);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_trim_telemetry(p_attempt uuid)
RETURNS void
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DELETE FROM public.training_telemetry_samples AS s USING public.training_attempts AS a
WHERE s.attempt_id = p_attempt AND a.id = s.attempt_id
  AND s.sequence <= (a.observation_state->>'telemetry_cursor')::integer - 720;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_telemetry_earliest(p_attempt uuid) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT min(sequence) FROM public.training_telemetry_samples WHERE attempt_id = p_attempt;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_telemetry_page(
    p_attempt uuid, p_after integer, p_before integer, p_limit integer
)
RETURNS TABLE (sequence integer, payload jsonb, received_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT r.sequence, r.payload, r.received_at FROM public.training_telemetry_samples AS r
WHERE r.attempt_id = p_attempt AND (p_after IS NULL OR r.sequence > p_after)
  AND (p_after IS NOT NULL OR p_before IS NULL OR r.sequence < p_before)
ORDER BY CASE WHEN p_after IS NULL THEN -r.sequence ELSE r.sequence END
LIMIT LEAST(200, GREATEST(0, p_limit));
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_cleanup_telemetry(p_cutoff timestamptz)
RETURNS bigint
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH removed AS (
    DELETE FROM public.training_telemetry_samples WHERE (attempt_id, sequence) IN (
        SELECT r.attempt_id, r.sequence FROM public.training_telemetry_samples AS r
        WHERE r.received_at < LEAST(p_cutoff, now() - interval '7 days') ORDER BY r.received_at LIMIT 2000
    ) RETURNING 1
) SELECT count(*) FROM removed;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_logs_receipt(p_attempt uuid, p_sequence integer)
RETURNS TABLE (content_digest varchar, stream_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT r.content_digest, r.stream_id FROM public.training_log_chunks AS r
WHERE r.attempt_id = p_attempt AND r.sequence = p_sequence;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_append_logs(
    p_attempt uuid, p_executor text, p_boot uuid, p_lease_digest text,
    p_stream uuid, p_rows jsonb
)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH inserted AS (
    INSERT INTO public.training_log_chunks (
        attempt_id, sequence, stream_id, producer_boot_id, captured_at, content_digest, payload
    )
    SELECT a.id, (i.payload->>'sequence')::integer, p_stream,
           (i.payload->>'producer_boot_id')::uuid, (i.payload->>'captured_at')::timestamptz,
           i.digest, i.payload
    FROM public.training_attempts AS a JOIN public.training_runs AS r ON r.current_attempt_id = a.id
    CROSS JOIN jsonb_to_recordset(p_rows) AS i(digest text, payload jsonb)
    WHERE a.id = p_attempt AND r.id = a.run_id AND a.executor_id = p_executor
      AND a.boot_id = p_boot AND a.lease_digest = p_lease_digest
      AND jsonb_array_length(p_rows) BETWEEN 1 AND 32
      AND (a.status IN ('succeeded','failed','cancelled') OR EXISTS (
          SELECT FROM public.training_resource_leases AS l WHERE l.attempt_id = a.id AND l.resource_id = a.resource_id
      )) AND (a.observation_state = '{}'::jsonb OR a.observation_state->>'stream_id' = p_stream::text)
    RETURNING 1
) SELECT EXISTS (SELECT FROM inserted);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_trim_logs(p_attempt uuid)
RETURNS void
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DELETE FROM public.training_log_chunks AS s USING public.training_attempts AS a
WHERE s.attempt_id = p_attempt AND a.id = s.attempt_id
  AND s.sequence <= (a.observation_state->>'logs_cursor')::integer - 512;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_logs_earliest(p_attempt uuid) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT min(sequence) FROM public.training_log_chunks WHERE attempt_id = p_attempt;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_logs_page(
    p_attempt uuid, p_after integer, p_before integer, p_limit integer
)
RETURNS TABLE (sequence integer, payload jsonb, received_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT r.sequence, r.payload, r.received_at FROM public.training_log_chunks AS r
WHERE r.attempt_id = p_attempt AND (p_after IS NULL OR r.sequence > p_after)
  AND (p_after IS NOT NULL OR p_before IS NULL OR r.sequence < p_before)
ORDER BY CASE WHEN p_after IS NULL THEN -r.sequence ELSE r.sequence END
LIMIT LEAST(200, GREATEST(0, p_limit));
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_cleanup_logs(p_cutoff timestamptz) RETURNS bigint
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH removed AS (
    DELETE FROM public.training_log_chunks WHERE (attempt_id, sequence) IN (
        SELECT r.attempt_id, r.sequence FROM public.training_log_chunks AS r
        WHERE r.received_at < LEAST(p_cutoff, now() - interval '7 days') ORDER BY r.received_at LIMIT 2000
    ) RETURNING 1
) SELECT count(*) FROM removed;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_latest_telemetry(p_attempt uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT payload FROM public.training_telemetry_samples WHERE attempt_id = p_attempt ORDER BY sequence DESC LIMIT 1;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_preview_request(p_project uuid, p_request uuid)
RETURNS SETOF yoao_learning_v1._training_preview_facts
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT r.* FROM public.training_previews AS t JOIN yoao_learning_v1._training_preview_facts AS r ON r.id = t.id
WHERE t.project_id = p_project AND t.request_id = p_request FOR SHARE OF t;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_active_preview_count(p_project uuid) RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT count(*) FROM public.training_previews
WHERE project_id = p_project AND status IN ('queued','running','cancelling','lost');
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_create_preview(
    p_id uuid, p_project uuid, p_dataset uuid, p_creator uuid, p_request_id uuid,
    p_request_digest text, p_request jsonb, p_input_digest text, p_spec jsonb,
    p_authorization jsonb, p_resource text, p_expires_at timestamptz
)
RETURNS SETOF yoao_learning_v1._training_preview_facts
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    INSERT INTO public.training_previews (
        id, project_id, dataset_id, creator_id, request_id, request_digest, request,
        input_digest, spec, "authorization", resource_id, expires_at, status,
        runtime, upload_inventory, artifacts
    ) VALUES (
        p_id, p_project, p_dataset, p_creator, p_request_id, p_request_digest, p_request,
        p_input_digest, p_spec, p_authorization, p_resource, p_expires_at, 'queued',
        '{}'::jsonb, '{}'::jsonb, '{}'::jsonb
    );
    RETURN QUERY SELECT p.* FROM yoao_learning_v1._training_preview_facts AS p WHERE p.id = p_id;
END;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_preview_has_lease(p_preview uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT EXISTS (SELECT FROM public.training_preview_resource_leases WHERE preview_id = p_preview);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_pin_preview_storage(p_preview uuid, p_ref jsonb, p_retained boolean)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH pinned AS (
    UPDATE public.training_previews AS p SET storage_ref = p_ref
    WHERE p.id = p_preview AND p.storage_ref IS NULL AND (
        (NOT p_retained AND p.status = 'queued' AND p.async_job_id IS NULL
         AND p_ref->>'origin' = 'new') OR
        (p_retained AND p.status IN ('succeeded','failed','cancelled','expired')
         AND p_ref->>'origin' = 'verified-retained'
         AND (p.put_expires_at IS NULL OR p.put_expires_at <= now())
         AND NOT yoao_learning_v1.training_preview_has_lease(p.id))
    ) RETURNING 1
) SELECT EXISTS (SELECT FROM pinned);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_attach_preview_job(p_preview uuid, p_job uuid)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH attached AS (
    UPDATE public.training_previews SET async_job_id = p_job
    WHERE id = p_preview AND async_job_id IS NULL AND status = 'queued' AND storage_ref IS NOT NULL
    RETURNING 1
) SELECT EXISTS (SELECT FROM attached);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_cpu_queue(p_resource text) RETURNS SETOF yoao_learning_v1._training_preview_facts
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT p.* FROM yoao_learning_v1._training_preview_facts AS p
WHERE p.status = 'queued' AND p.resource_id = p_resource
ORDER BY p.created_at LIMIT 100 FOR UPDATE SKIP LOCKED;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_claim_preview(
    p_preview uuid, p_resource text, p_executor text, p_boot uuid,
    p_claim uuid, p_digest text, p_runtime jsonb, p_at timestamptz
)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.training_preview_resource_leases SET preview_id = p_preview
    WHERE resource_id = p_resource AND preview_id IS NULL
      AND executor_id = p_executor AND boot_id = p_boot;
    IF NOT FOUND THEN RAISE EXCEPTION 'CPU lease changed' USING ERRCODE = '40001'; END IF;
    UPDATE public.training_previews
    SET status = 'running', executor_id = p_executor, boot_id = p_boot,
        claim_id = p_claim, lease_digest = p_digest, runtime = p_runtime,
        started_at = p_at, heartbeat_at = p_at
    WHERE id = p_preview AND status = 'queued' AND resource_id = p_resource;
    IF NOT FOUND THEN RAISE EXCEPTION 'Training preview changed' USING ERRCODE = '40001'; END IF;
END;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_heartbeat_preview(
    p_preview uuid, p_executor text, p_boot uuid, p_digest text, p_at timestamptz, p_stop_reason text
)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH seen AS (
    UPDATE public.training_previews AS p
    SET heartbeat_at = p_at,
        status = CASE WHEN p_stop_reason IS NULL THEN p.status ELSE 'cancelling' END,
        error_message = COALESCE(p_stop_reason, p.error_message)
    FROM public.training_preview_resource_leases AS l
    WHERE p.id = p_preview AND l.resource_id = p.resource_id AND l.preview_id = p.id
      AND p.status IN ('running','lost','cancelling')
      AND p.executor_id = p_executor AND p.boot_id = p_boot AND p.lease_digest = p_digest
    RETURNING p.id, p.resource_id
), touched AS (
    UPDATE public.training_preview_resource_leases AS l SET last_seen_at = p_at FROM seen
    WHERE l.resource_id = seen.resource_id AND l.preview_id = seen.id RETURNING 1
) SELECT EXISTS (SELECT FROM touched);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_preview_uploads(
    p_preview uuid, p_executor text, p_boot uuid, p_digest text, p_inventory jsonb, p_until timestamptz
)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH authorized AS (
    UPDATE public.training_previews AS p SET upload_inventory = p_inventory, put_expires_at = p_until
    FROM public.training_preview_resource_leases AS l
    WHERE p.id = p_preview AND l.resource_id = p.resource_id AND l.preview_id = p.id
      AND p.status IN ('running','lost')
      AND p.executor_id = p_executor AND p.boot_id = p_boot AND p.lease_digest = p_digest
      AND (p.upload_inventory = '{}'::jsonb OR p.upload_inventory = p_inventory)
    RETURNING 1
) SELECT EXISTS (SELECT FROM authorized);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_finish_queued_preview(p_preview uuid, p_status text, p_at timestamptz, p_error text)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH finished AS (
    UPDATE public.training_previews SET status = p_status, completed_at = p_at, error_message = p_error
    WHERE id = p_preview AND status = 'queued' AND p_status IN ('failed','cancelled')
      AND NOT yoao_learning_v1.training_preview_has_lease(p_preview)
    RETURNING 1
) SELECT EXISTS (SELECT FROM finished);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_finish_executor_preview(
    p_preview uuid, p_executor text, p_boot uuid, p_digest text, p_exited boolean, p_status text,
    p_at timestamptz, p_error text, p_finish_digest text, p_artifacts jsonb
)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH finished AS (
    UPDATE public.training_previews AS p
    SET status = p_status, completed_at = p_at, error_message = p_error,
        finish_digest = p_finish_digest, artifacts = COALESCE(p_artifacts, p.artifacts)
    FROM public.training_preview_resource_leases AS l
    WHERE p.id = p_preview AND l.resource_id = p.resource_id AND l.preview_id = p.id
      AND p.status IN ('running','cancelling','lost')
      AND p.executor_id = p_executor AND p.boot_id = p_boot AND p.lease_digest = p_digest
      AND p_exited AND p_finish_digest IS NOT NULL AND p_status IN ('succeeded','failed','cancelled')
      AND (p.status <> 'cancelling' OR p_status = 'cancelled')
    RETURNING p.id, p.resource_id
), released AS (
    UPDATE public.training_preview_resource_leases AS l SET preview_id = NULL FROM finished
    WHERE l.resource_id = finished.resource_id AND l.preview_id = finished.id RETURNING 1
) SELECT EXISTS (SELECT FROM released);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_cancel_preview(p_preview uuid, p_error text)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH stopping AS (
    UPDATE public.training_previews SET status = 'cancelling', error_message = p_error
    WHERE id = p_preview AND status IN ('running','cancelling','lost') RETURNING 1
) SELECT EXISTS (SELECT FROM stopping);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_lose_preview(p_preview uuid, p_cutoff timestamptz, p_error text)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH lost AS (
    UPDATE public.training_previews SET status = 'lost', error_message = p_error
    WHERE id = p_preview AND status = 'running' AND heartbeat_at < p_cutoff
      AND yoao_learning_v1.training_preview_has_lease(p_preview) RETURNING 1
) SELECT EXISTS (SELECT FROM lost);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_expire_preview(p_preview uuid, p_at timestamptz)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH expired AS (
    UPDATE public.training_previews SET expires_at = LEAST(expires_at, p_at)
    WHERE id = p_preview RETURNING 1
) SELECT EXISTS (SELECT FROM expired);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_preview_cleanup_queue(p_at timestamptz)
RETURNS SETOF yoao_learning_v1._training_preview_facts
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
SELECT p.* FROM yoao_learning_v1._training_preview_facts AS p
WHERE p.expires_at <= p_at AND p.status IN ('queued','succeeded','failed','cancelled','expired')
ORDER BY p.expires_at LIMIT 100 FOR UPDATE SKIP LOCKED;
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_preview_cleanup_expire(p_preview uuid, p_at timestamptz)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH expired AS (
    UPDATE public.training_previews SET status = 'expired'
    WHERE id = p_preview AND expires_at <= p_at AND status IN ('succeeded','failed','cancelled','expired')
      AND NOT yoao_learning_v1.training_preview_has_lease(p_preview) RETURNING 1
) SELECT EXISTS (SELECT FROM expired);
$function$;

-- aap-statement-break

CREATE FUNCTION yoao_learning_v1.training_delete_expired_preview(p_preview uuid, p_ref jsonb, p_at timestamptz)
RETURNS boolean
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH removed AS (
    DELETE FROM public.training_previews
    WHERE id = p_preview AND status = 'expired' AND expires_at <= p_at
      AND (put_expires_at IS NULL OR put_expires_at < p_at) AND storage_ref = p_ref
      AND NOT yoao_learning_v1.training_preview_has_lease(p_preview) RETURNING 1
) SELECT EXISTS (SELECT FROM removed);
$function$;

-- aap-statement-break

REVOKE ALL ON FUNCTION
    yoao_learning_v1.training_get_run(uuid, uuid, text),
    yoao_learning_v1.training_get_attempt(uuid, text),
    yoao_learning_v1.training_get_preview(uuid, uuid, text),
    yoao_learning_v1.training_executor_run(uuid, boolean),
    yoao_learning_v1.training_executor_preview(uuid, boolean),
    yoao_learning_v1.training_attempt_proof(uuid, text, uuid, text),
    yoao_learning_v1.training_preview_proof(uuid, text, uuid, text),
    yoao_learning_v1.training_run_request(uuid, uuid),
    yoao_learning_v1.training_gpu_queue(),
    yoao_learning_v1.training_study_children(uuid, boolean),
    yoao_learning_v1.training_study_attempts(uuid),
    yoao_learning_v1.training_run_attempts(uuid, boolean),
    yoao_learning_v1.training_adoption_attempts(uuid),
    yoao_learning_v1.training_trigger_run(uuid, boolean),
    yoao_learning_v1.training_active_auto_run(uuid, uuid),
    yoao_learning_v1.training_trigger_run_ids(uuid, uuid[]),
    yoao_learning_v1.training_dataset_run_dependencies(uuid, uuid),
    yoao_learning_v1.training_run_has_lease(uuid),
    yoao_learning_v1.training_create_run(uuid, uuid, uuid, uuid, uuid, uuid, uuid, uuid, text, text, jsonb, text, jsonb, jsonb),
    yoao_learning_v1.training_pin_run_storage(uuid, jsonb, boolean),
    yoao_learning_v1.training_enqueue_attempt(uuid, uuid, uuid, uuid),
    yoao_learning_v1.training_reserve_study_attempt(uuid, uuid, jsonb),
    yoao_learning_v1.training_manual_resume(uuid, uuid, boolean),
    yoao_learning_v1.training_delete_run(uuid, uuid),
    yoao_learning_v1.training_gpu_lease(text),
    yoao_learning_v1.training_lock_gpu_lease(text),
    yoao_learning_v1.training_observe_gpu(text, text, uuid, jsonb, timestamptz),
    yoao_learning_v1.training_cpu_lease(text),
    yoao_learning_v1.training_lock_cpu_lease(text),
    yoao_learning_v1.training_observe_cpu(text, text, uuid, jsonb, timestamptz),
    yoao_learning_v1.training_claim_attempt(uuid, uuid, text, text, uuid, uuid, text, jsonb, timestamptz),
    yoao_learning_v1.training_lose_attempt(uuid, uuid, timestamptz, text, jsonb),
    yoao_learning_v1.training_cancel_attempt(uuid, uuid, text),
    yoao_learning_v1.training_finish_queued_attempt(uuid, uuid, text, timestamptz, text, jsonb, jsonb),
    yoao_learning_v1.training_finish_executor_attempt(uuid, uuid, text, uuid, text, boolean, text, timestamptz, text, jsonb, jsonb, text, jsonb, uuid, boolean),
    yoao_learning_v1.training_heartbeat_attempt(uuid, uuid, text, uuid, text, timestamptz, text, text, jsonb, jsonb),
    yoao_learning_v1.training_attempt_uploads(uuid, uuid, text, uuid, text, jsonb),
    yoao_learning_v1.training_record_event(uuid, text, uuid, text, integer, text, jsonb),
    yoao_learning_v1.training_event_page(uuid, integer, boolean),
    yoao_learning_v1.training_last_epoch(uuid),
    yoao_learning_v1.training_set_observation_receipt(uuid, text, uuid, text, jsonb),
    yoao_learning_v1.training_telemetry_receipt(uuid, integer),
    yoao_learning_v1.training_append_telemetry(uuid, text, uuid, text, uuid, jsonb),
    yoao_learning_v1.training_trim_telemetry(uuid),
    yoao_learning_v1.training_telemetry_earliest(uuid),
    yoao_learning_v1.training_telemetry_page(uuid, integer, integer, integer),
    yoao_learning_v1.training_cleanup_telemetry(timestamptz),
    yoao_learning_v1.training_logs_receipt(uuid, integer),
    yoao_learning_v1.training_append_logs(uuid, text, uuid, text, uuid, jsonb),
    yoao_learning_v1.training_trim_logs(uuid),
    yoao_learning_v1.training_logs_earliest(uuid),
    yoao_learning_v1.training_logs_page(uuid, integer, integer, integer),
    yoao_learning_v1.training_cleanup_logs(timestamptz),
    yoao_learning_v1.training_latest_telemetry(uuid),
    yoao_learning_v1.training_preview_request(uuid, uuid),
    yoao_learning_v1.training_active_preview_count(uuid),
    yoao_learning_v1.training_create_preview(uuid, uuid, uuid, uuid, uuid, text, jsonb, text, jsonb, jsonb, text, timestamptz),
    yoao_learning_v1.training_preview_has_lease(uuid),
    yoao_learning_v1.training_pin_preview_storage(uuid, jsonb, boolean),
    yoao_learning_v1.training_attach_preview_job(uuid, uuid),
    yoao_learning_v1.training_cpu_queue(text),
    yoao_learning_v1.training_claim_preview(uuid, text, text, uuid, uuid, text, jsonb, timestamptz),
    yoao_learning_v1.training_heartbeat_preview(uuid, text, uuid, text, timestamptz, text),
    yoao_learning_v1.training_preview_uploads(uuid, text, uuid, text, jsonb, timestamptz),
    yoao_learning_v1.training_finish_queued_preview(uuid, text, timestamptz, text),
    yoao_learning_v1.training_finish_executor_preview(uuid, text, uuid, text, boolean, text, timestamptz, text, text, jsonb),
    yoao_learning_v1.training_cancel_preview(uuid, text),
    yoao_learning_v1.training_lose_preview(uuid, timestamptz, text),
    yoao_learning_v1.training_expire_preview(uuid, timestamptz),
    yoao_learning_v1.training_preview_cleanup_queue(timestamptz),
    yoao_learning_v1.training_preview_cleanup_expire(uuid, timestamptz),
    yoao_learning_v1.training_delete_expired_preview(uuid, jsonb, timestamptz)
FROM PUBLIC;

-- aap-statement-break

REVOKE ALL ON yoao_learning_v1._training_run_facts,
    yoao_learning_v1._training_attempt_facts,
    yoao_learning_v1._training_preview_facts,
    yoao_learning_v1.training_run_summary,
    yoao_learning_v1.training_run_storage_owner,
    yoao_learning_v1.training_preview_storage_owner,
    yoao_learning_v1.training_run_lineage,
    yoao_learning_v1.training_attempt_lineage FROM PUBLIC;
