-- Bind image prediction route scopes to the exact renewable Jobs execution lease.
CREATE TABLE aap_inference_routing_v1.prediction_execution_scopes (
    scope_id uuid PRIMARY KEY REFERENCES aap_inference_routing_v1.scopes(id)
        DEFERRABLE INITIALLY DEFERRED,
    job_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    project_id uuid NOT NULL,
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    generation bigint NOT NULL CHECK (generation > 0),
    lease_token_digest bytea NOT NULL,
    uses_gpu boolean NOT NULL DEFAULT false,
    creating_xid xid8 NOT NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
CREATE INDEX ix_prediction_execution_scopes_job
ON aap_inference_routing_v1.prediction_execution_scopes(job_id, generation);
-- aap-statement-break
REVOKE ALL ON aap_inference_routing_v1.prediction_execution_scopes FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_inference_routing_v1.fixture_prediction_test_scopes (
    scope_id uuid PRIMARY KEY REFERENCES aap_inference_routing_v1.scopes(id)
        DEFERRABLE INITIALLY DEFERRED,
    run_id uuid NOT NULL REFERENCES yoao_fixture_v1.fixture_runs(id) ON DELETE RESTRICT,
    project_id uuid NOT NULL,
    task_id uuid NOT NULL,
    uses_gpu boolean NOT NULL DEFAULT false,
    creating_xid xid8 NOT NULL
);
-- aap-statement-break
CREATE INDEX ix_fixture_prediction_test_scopes_run
ON aap_inference_routing_v1.fixture_prediction_test_scopes(run_id, task_id);
-- aap-statement-break
REVOKE ALL ON aap_inference_routing_v1.fixture_prediction_test_scopes FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1.prediction_scope_reserve(p_arguments text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_args jsonb;
    v_proof jsonb;
    v_reservation jsonb;
    v_scope_id uuid;
    v_job_id uuid;
    v_lock jsonb;
    v_result jsonb;
    v_operation text;
    v_owner text;
BEGIN
    IF p_arguments IS NULL OR pg_catalog.octet_length(p_arguments) > 2097152 THEN
        RETURN aap_inference_routing_v1._routing_rejected('invalid_command');
    END IF;
    v_args := p_arguments::jsonb;
    IF NOT aap_inference_routing_v1._routing_exact_keys(v_args, ARRAY['proof','reservation']) THEN
        RETURN aap_inference_routing_v1._routing_rejected('invalid_command');
    END IF;
    v_proof := v_args -> 'proof';
    v_reservation := v_args -> 'reservation';
    IF NOT aap_jobs_v1._batch_result_scope_valid(v_proof, true)
       OR NOT aap_inference_routing_v1._routing_exact_keys(v_reservation,
            ARRAY['snapshot','candidate_ids','scope_id','command_sha256','owner','operation','ttl_ms']) THEN
        RETURN aap_inference_routing_v1._routing_rejected('invalid_command');
    END IF;
    v_job_id := (v_proof ->> 'job_id')::uuid;
    v_scope_id := aap_inference_routing_v1._routing_uuid(v_reservation ->> 'scope_id');
    v_operation := v_reservation ->> 'operation';
    v_owner := v_reservation ->> 'owner';
    IF NOT ((v_operation = 'image_batch_predict'
             AND v_owner ~ ('^batch:' || v_job_id::text || ':stage:[0-9]+$'))
         OR (v_operation = 'prediction_retry'
             AND v_owner ~ ('^retry:' || v_job_id::text || ':stage:[0-9]+$'))) THEN
        RETURN aap_inference_routing_v1._routing_rejected('invalid_command');
    END IF;
    v_lock := aap_jobs_v1._lock_batch_result_scope(v_proof, true, true);
    IF v_lock -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR v_lock #> '{data,cancel_requested}' IS DISTINCT FROM 'false'::jsonb
       OR aap_jobs_v1.prediction_fixture_open(v_job_id) IS DISTINCT FROM true THEN
        RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.async_jobs AS job
        WHERE job.id = v_job_id
          AND ((job.kind = 'batch_predict' AND v_operation = 'image_batch_predict')
            OR (job.kind = 'prediction_retry' AND v_operation = 'prediction_retry'))
    ) THEN
        RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
    END IF;
    INSERT INTO aap_inference_routing_v1.prediction_execution_scopes (
        scope_id, job_id, actor_id, project_id, request_digest, generation,
        lease_token_digest, creating_xid
    ) VALUES (
        v_scope_id, v_job_id, (v_proof ->> 'actor_id')::uuid,
        (v_proof ->> 'project_id')::uuid, v_proof ->> 'request_digest',
        (v_proof ->> 'generation')::bigint,
        public.digest(pg_catalog.convert_to(v_proof ->> 'lease_token', 'UTF8'), 'sha256'),
        pg_catalog.pg_current_xact_id()
    );
    v_result := aap_inference_routing_v1.routing_step('reserve_scope', v_reservation::text);
    IF v_result ->> 'status' IS DISTINCT FROM 'ok' THEN
        DELETE FROM aap_inference_routing_v1.prediction_execution_scopes WHERE scope_id = v_scope_id;
        RETURN v_result;
    END IF;
    IF v_result #>> '{scope,project_id}' IS DISTINCT FROM v_proof ->> 'project_id' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'prediction scope project mismatch';
    END IF;
    UPDATE aap_inference_routing_v1.prediction_execution_scopes AS binding
    SET uses_gpu = registry.gpu_resource_id IS NOT NULL OR registry.vram_budget_mb IS NOT NULL
        OR COALESCE(registry.extra_params #>> '{plugin,manifest,resource,device}','cpu') <> 'cpu'
    FROM aap_inference_routing_v1.scopes AS scope
    JOIN public.ml_backend_registry AS registry ON registry.id=scope.registry_id
    WHERE binding.scope_id=v_scope_id AND scope.id=binding.scope_id;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='prediction backend identity missing'; END IF;
    RETURN v_result;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1.prediction_scope_reserve(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1.prediction_test_scope_reserve(p_arguments text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_args jsonb;
    v_proof jsonb;
    v_reservation jsonb;
    v_run_id uuid;
    v_root_id uuid;
    v_run_state text;
    v_root_state text;
    v_project_id uuid;
    v_task_id uuid;
    v_scope_id uuid;
    v_result jsonb;
BEGIN
    IF p_arguments IS NULL OR pg_catalog.octet_length(p_arguments) > 2097152 THEN
        RETURN aap_inference_routing_v1._routing_rejected('invalid_command');
    END IF;
    v_args := p_arguments::jsonb;
    IF NOT aap_inference_routing_v1._routing_exact_keys(v_args, ARRAY['proof','reservation']) THEN
        RETURN aap_inference_routing_v1._routing_rejected('invalid_command');
    END IF;
    v_proof := v_args -> 'proof';
    v_reservation := v_args -> 'reservation';
    IF NOT aap_inference_routing_v1._routing_exact_keys(v_proof, ARRAY['project_id','task_id'])
       OR NOT aap_inference_routing_v1._routing_exact_keys(v_reservation,
            ARRAY['snapshot','candidate_ids','scope_id','command_sha256','owner','operation','ttl_ms']) THEN
        RETURN aap_inference_routing_v1._routing_rejected('invalid_command');
    END IF;
    v_project_id := aap_inference_routing_v1._routing_uuid(v_proof ->> 'project_id');
    v_task_id := aap_inference_routing_v1._routing_uuid(v_proof ->> 'task_id');
    v_scope_id := aap_inference_routing_v1._routing_uuid(v_reservation ->> 'scope_id');
    IF v_reservation ->> 'operation' IS DISTINCT FROM 'predict_test'
       OR v_reservation ->> 'owner' IS DISTINCT FROM 'api:predict-test:' || v_task_id::text
       OR NOT EXISTS (SELECT 1 FROM aap_labeling_v1.task_rows AS task
                      WHERE task.id = v_task_id AND task.project_id = v_project_id) THEN
        RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
    END IF;
    SELECT resource.run_id INTO v_run_id
    FROM yoao_fixture_v1.fixture_run_resources AS resource
    WHERE resource.kind = 'project' AND resource.resource_id = v_project_id
      AND resource.state = 'published';
    IF v_run_id IS NOT NULL THEN
        SELECT COALESCE(run.parent_run_id, run.id) INTO v_root_id
        FROM yoao_fixture_v1.fixture_runs AS run WHERE run.id = v_run_id;
        SELECT run.state INTO v_root_state FROM yoao_fixture_v1.fixture_runs AS run
        WHERE run.id = v_root_id FOR SHARE NOWAIT;
        SELECT run.state INTO v_run_state FROM yoao_fixture_v1.fixture_runs AS run
        WHERE run.id = v_run_id FOR SHARE NOWAIT;
        IF v_root_state IS DISTINCT FROM 'open' OR v_run_state IS DISTINCT FROM 'open' THEN
            RETURN aap_inference_routing_v1._routing_rejected('scope_unavailable');
        END IF;
        INSERT INTO aap_inference_routing_v1.fixture_prediction_test_scopes(
            scope_id, run_id, project_id, task_id, creating_xid
        ) VALUES (v_scope_id, v_run_id, v_project_id, v_task_id, pg_catalog.pg_current_xact_id());
    END IF;
    v_result := aap_inference_routing_v1.routing_step('reserve_scope', v_reservation::text);
    IF v_result ->> 'status' IS DISTINCT FROM 'ok' THEN
        DELETE FROM aap_inference_routing_v1.fixture_prediction_test_scopes WHERE scope_id = v_scope_id;
        RETURN v_result;
    END IF;
    IF v_result #>> '{scope,project_id}' IS DISTINCT FROM v_project_id::text THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'prediction test project mismatch';
    END IF;
    IF v_run_id IS NOT NULL THEN
        UPDATE aap_inference_routing_v1.fixture_prediction_test_scopes AS binding
        SET uses_gpu = registry.gpu_resource_id IS NOT NULL OR registry.vram_budget_mb IS NOT NULL
            OR COALESCE(registry.extra_params #>> '{plugin,manifest,resource,device}','cpu') <> 'cpu'
        FROM aap_inference_routing_v1.scopes AS scope
        JOIN public.ml_backend_registry AS registry ON registry.id=scope.registry_id
        WHERE binding.scope_id=v_scope_id AND scope.id=binding.scope_id;
        IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='prediction backend identity missing'; END IF;
    END IF;
    RETURN v_result;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1.prediction_test_scope_reserve(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1.guard_prediction_scope_insert()
RETURNS trigger LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    IF NEW.operation IN ('image_batch_predict', 'prediction_retry') AND NOT EXISTS (
        SELECT 1 FROM aap_inference_routing_v1.prediction_execution_scopes AS binding
        WHERE binding.scope_id = NEW.id AND binding.project_id = NEW.project_id
          AND binding.creating_xid = pg_catalog.pg_current_xact_id()
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'prediction execution scope required';
    END IF;
    IF NEW.operation = 'predict_test'
       AND EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
                   WHERE resource.kind = 'project' AND resource.resource_id = NEW.project_id
                     AND resource.state = 'published')
       AND NOT EXISTS (
           SELECT 1 FROM aap_inference_routing_v1.fixture_prediction_test_scopes AS binding
           WHERE binding.scope_id = NEW.id AND binding.project_id = NEW.project_id
             AND binding.creating_xid = pg_catalog.pg_current_xact_id()
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture prediction test scope required';
    END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
CREATE TRIGGER aap_route_guard_prediction_scope_insert
BEFORE INSERT ON aap_inference_routing_v1.scopes
FOR EACH ROW EXECUTE FUNCTION aap_inference_routing_v1.guard_prediction_scope_insert();
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1.guard_prediction_scope_insert() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1.guard_prediction_dispatch_start()
RETURNS trigger LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_binding aap_inference_routing_v1.prediction_execution_scopes%ROWTYPE;
    v_execution public.batch_result_executions%ROWTYPE;
    v_job public.async_jobs%ROWTYPE;
    v_test_run uuid;
    v_test_root uuid;
    v_root_state text;
    v_test_state text;
BEGIN
    IF NEW.dispatch_state IS DISTINCT FROM 'started'
       OR OLD.dispatch_state IS NOT DISTINCT FROM NEW.dispatch_state THEN
        RETURN NEW;
    END IF;
    SELECT * INTO v_binding FROM aap_inference_routing_v1.prediction_execution_scopes
    WHERE scope_id = NEW.scope_id;
    IF NOT FOUND THEN
        SELECT binding.run_id INTO v_test_run
        FROM aap_inference_routing_v1.fixture_prediction_test_scopes AS binding
        WHERE binding.scope_id = NEW.scope_id;
        IF FOUND THEN
            SELECT COALESCE(run.parent_run_id, run.id) INTO v_test_root
            FROM yoao_fixture_v1.fixture_runs AS run WHERE run.id = v_test_run;
            SELECT run.state INTO v_root_state FROM yoao_fixture_v1.fixture_runs AS run
            WHERE run.id = v_test_root FOR SHARE NOWAIT;
            SELECT run.state INTO v_test_state FROM yoao_fixture_v1.fixture_runs AS run
            WHERE run.id = v_test_run FOR SHARE NOWAIT;
            IF v_root_state IS DISTINCT FROM 'open' OR v_test_state IS DISTINCT FROM 'open' THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture prediction test no longer current';
            END IF;
            RETURN NEW;
        END IF;
        IF EXISTS (SELECT 1 FROM aap_inference_routing_v1.scopes
                   WHERE id = NEW.scope_id AND operation IN ('image_batch_predict','prediction_retry')) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'prediction execution scope required';
        END IF;
        IF EXISTS (
            SELECT 1 FROM aap_inference_routing_v1.scopes AS scope
            JOIN yoao_fixture_v1.fixture_run_resources AS resource
              ON resource.kind = 'project' AND resource.resource_id = scope.project_id
            WHERE scope.id = NEW.scope_id AND scope.operation = 'predict_test'
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture prediction test scope required';
        END IF;
        RETURN NEW;
    END IF;
    SELECT * INTO v_job FROM public.async_jobs WHERE id = v_binding.job_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_job.status NOT IN ('pending','running')
       OR v_job.project_id IS DISTINCT FROM v_binding.project_id
       OR v_job.user_id IS DISTINCT FROM v_binding.actor_id
       OR aap_jobs_v1.prediction_fixture_open(v_binding.job_id) IS DISTINCT FROM true
       OR COALESCE(v_job.payload -> 'cancel_requested', 'false'::jsonb) IS DISTINCT FROM 'false'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'prediction execution no longer current';
    END IF;
    SELECT * INTO v_execution FROM public.batch_result_executions
    WHERE job_id = v_binding.job_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_execution.state NOT IN ('preparing','running')
       OR v_execution.actor_id IS DISTINCT FROM v_binding.actor_id
       OR v_execution.project_id IS DISTINCT FROM v_binding.project_id
       OR v_execution.request_digest IS DISTINCT FROM v_binding.request_digest
       OR v_execution.generation IS DISTINCT FROM v_binding.generation
       OR v_execution.lease_token IS NULL
       OR v_execution.lease_expires_at IS NULL
       OR v_execution.lease_expires_at <= pg_catalog.clock_timestamp()
       OR public.digest(pg_catalog.convert_to(v_execution.lease_token::text, 'UTF8'), 'sha256')
          IS DISTINCT FROM v_binding.lease_token_digest THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'prediction execution no longer current';
    END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
CREATE TRIGGER aap_route_guard_prediction_dispatch_start
BEFORE UPDATE OF dispatch_state ON aap_inference_routing_v1.attempts
FOR EACH ROW EXECUTE FUNCTION aap_inference_routing_v1.guard_prediction_dispatch_start();
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1.guard_prediction_dispatch_start() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1.guard_prediction_managed_attempt()
RETURNS trigger LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    IF EXISTS (
        SELECT 1 FROM aap_inference_routing_v1.attempts AS attempt
        JOIN aap_inference_routing_v1.scopes AS scope ON scope.id=attempt.scope_id
        WHERE attempt.id=NEW.id AND attempt.dispatch_state='unmanaged'
          AND (scope.operation IN ('image_batch_predict','prediction_retry')
               OR (scope.operation='predict_test' AND EXISTS (
                   SELECT 1 FROM aap_inference_routing_v1.fixture_prediction_test_scopes AS binding
                   WHERE binding.scope_id=scope.id
               )))
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='prediction managed dispatch required';
    END IF;
    RETURN NULL;
END
$function$;
-- aap-statement-break
CREATE CONSTRAINT TRIGGER aap_route_guard_prediction_managed_attempt
AFTER INSERT OR UPDATE ON aap_inference_routing_v1.attempts
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION aap_inference_routing_v1.guard_prediction_managed_attempt();
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1.guard_prediction_managed_attempt() FROM PUBLIC;
-- aap-statement-break
-- This test-only inventory is deliberately exact: unknown old scopes block.
CREATE FUNCTION aap_inference_routing_v1.fixture_prediction_scope_inventory(
    p_run_id uuid, p_project_ids uuid[], p_task_ids uuid[]
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE v_scopes jsonb;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture retirement requires a test database';
    END IF;
    IF p_run_id IS NULL OR p_project_ids IS NULL OR p_task_ids IS NULL
       OR pg_catalog.array_position(p_project_ids, NULL::uuid) IS NOT NULL
       OR pg_catalog.array_position(p_task_ids, NULL::uuid) IS NOT NULL
       OR (SELECT pg_catalog.count(DISTINCT id) FROM pg_catalog.unnest(p_project_ids) AS value(id))
          <> pg_catalog.cardinality(p_project_ids)
       OR (SELECT pg_catalog.count(DISTINCT id) FROM pg_catalog.unnest(p_task_ids) AS value(id))
          <> pg_catalog.cardinality(p_task_ids)
       OR NOT EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_runs WHERE id=p_run_id AND state='sealing')
       OR EXISTS (
           SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
           WHERE resource.run_id=p_run_id AND resource.kind='project' AND resource.state='published'
             AND NOT (resource.resource_id=ANY(p_project_ids))
       ) OR EXISTS (
           SELECT 1 FROM pg_catalog.unnest(p_project_ids) AS ids(id)
           WHERE NOT EXISTS (
               SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
               WHERE resource.run_id=p_run_id AND resource.kind='project' AND resource.state='published'
                 AND resource.resource_id=ids.id
           )
       ) OR EXISTS (
           SELECT 1 FROM aap_labeling_v1.task_rows AS task
           WHERE task.project_id=ANY(p_project_ids) AND NOT (task.id=ANY(p_task_ids))
       ) OR EXISTS (
           SELECT 1 FROM pg_catalog.unnest(p_task_ids) AS ids(id)
           WHERE NOT EXISTS (
               SELECT 1 FROM aap_labeling_v1.task_rows AS task
               WHERE task.id=ids.id AND task.project_id=ANY(p_project_ids)
           )
       ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_prediction_scope_changed';
    END IF;
    IF EXISTS (
        SELECT 1 FROM aap_inference_routing_v1.scopes AS scope
        WHERE scope.project_id=ANY(p_project_ids)
          AND ((scope.operation IN ('batch_predict','image_batch_predict','prediction_retry')
                AND NOT EXISTS (
                    SELECT 1 FROM aap_inference_routing_v1.prediction_execution_scopes AS binding
                    WHERE binding.scope_id=scope.id AND binding.project_id=scope.project_id
                ))
            OR (scope.operation='predict_test' AND NOT EXISTS (
                    SELECT 1 FROM aap_inference_routing_v1.fixture_prediction_test_scopes AS binding
                    WHERE binding.scope_id=scope.id AND binding.run_id=p_run_id
                      AND binding.project_id=scope.project_id AND binding.task_id=ANY(p_task_ids)
                )))
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_prediction_scope_untracked';
    END IF;
    IF EXISTS (
        SELECT 1 FROM aap_inference_routing_v1.prediction_execution_scopes AS binding
        LEFT JOIN public.async_jobs AS job ON job.id=binding.job_id
        WHERE binding.project_id=ANY(p_project_ids)
          AND (job.id IS NULL OR job.project_id IS DISTINCT FROM binding.project_id
               OR job.status NOT IN ('completed','failed','cancelled'))
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_prediction_job_active';
    END IF;
    IF EXISTS (
        SELECT 1 FROM aap_inference_routing_v1.prediction_execution_scopes AS binding
        WHERE binding.project_id=ANY(p_project_ids) AND binding.uses_gpu
    ) OR EXISTS (
        SELECT 1 FROM aap_inference_routing_v1.fixture_prediction_test_scopes AS binding
        WHERE binding.run_id=p_run_id AND binding.project_id=ANY(p_project_ids) AND binding.uses_gpu
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_prediction_gpu_unqualified';
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(
        pg_catalog.jsonb_build_object(
            'scope_id', scopes.id,
            'capability', aap_inference_routing_v1._routing_scope_capability(scopes.id),
            'retirement_id', (SELECT retirement.retirement_id
                              FROM aap_inference_routing_v1.scope_retirements AS retirement
                              WHERE retirement.scope_id=scopes.id)
        ) ORDER BY scopes.id
    ), '[]'::jsonb) INTO v_scopes
    FROM aap_inference_routing_v1.scopes AS scopes
    WHERE EXISTS (
        SELECT 1 FROM aap_inference_routing_v1.prediction_execution_scopes AS binding
        WHERE binding.scope_id=scopes.id AND binding.project_id=ANY(p_project_ids)
    ) OR EXISTS (
        SELECT 1 FROM aap_inference_routing_v1.fixture_prediction_test_scopes AS binding
        WHERE binding.scope_id=scopes.id AND binding.run_id=p_run_id
          AND binding.project_id=ANY(p_project_ids) AND binding.task_id=ANY(p_task_ids)
    );
    RETURN pg_catalog.jsonb_build_object('schema','aap-fixture-prediction-scopes/v1','scopes',v_scopes);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1.fixture_prediction_scope_inventory(uuid,uuid[],uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_routing_v1.fixture_prediction_tests_retired(
    p_run_id uuid, p_project_ids uuid[], p_task_ids uuid[]
)
RETURNS boolean LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
BEGIN
    PERFORM aap_inference_routing_v1.fixture_prediction_scope_inventory(p_run_id,p_project_ids,p_task_ids);
    IF aap_assets_v1.fixture_prediction_inputs_retired(
        p_run_id,
        COALESCE((SELECT pg_catalog.array_agg(DISTINCT binding.task_id)
                  FROM aap_inference_routing_v1.fixture_prediction_test_scopes AS binding
                  WHERE binding.run_id=p_run_id AND binding.project_id=ANY(p_project_ids)),
                 ARRAY[]::uuid[])
    ) IS DISTINCT FROM true THEN RETURN false; END IF;
    RETURN NOT EXISTS (
        SELECT 1 FROM aap_inference_routing_v1.fixture_prediction_test_scopes AS binding
        LEFT JOIN aap_inference_routing_v1.scope_retirements AS retirement ON retirement.scope_id=binding.scope_id
        WHERE binding.run_id=p_run_id AND binding.project_id=ANY(p_project_ids)
          AND binding.task_id=ANY(p_task_ids) AND retirement.phase IS DISTINCT FROM 'completed'
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1.fixture_prediction_tests_retired(uuid,uuid[],uuid[]) FROM PUBLIC;
