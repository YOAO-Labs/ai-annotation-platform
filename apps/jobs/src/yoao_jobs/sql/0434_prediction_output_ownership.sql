-- Exact image-prediction producers remain owned even if their worker disappears.
CREATE TABLE aap_jobs_v1.prediction_mask_producers (
    id uuid PRIMARY KEY,
    job_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    project_id uuid NOT NULL,
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    generation bigint NOT NULL CHECK (generation > 0),
    lease_token_digest bytea NOT NULL,
    ordinal bigint NOT NULL CHECK (ordinal > 0),
    state text NOT NULL CHECK (state IN ('pending', 'settled')),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    settled_at timestamptz,
    CHECK ((state = 'pending' AND settled_at IS NULL) OR
           (state = 'settled' AND settled_at IS NOT NULL))
);
-- aap-statement-break
CREATE INDEX ix_prediction_mask_producers_job ON aap_jobs_v1.prediction_mask_producers(job_id, generation);
-- aap-statement-break
CREATE TABLE aap_jobs_v1.prediction_mask_producer_objects (
    producer_id uuid NOT NULL REFERENCES aap_jobs_v1.prediction_mask_producers(id) ON DELETE RESTRICT,
    object_id uuid NOT NULL,
    PRIMARY KEY (producer_id, object_id)
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.prediction_mask_producers, aap_jobs_v1.prediction_mask_producer_objects FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.begin_prediction_mask_producer(p_scope jsonb, p_ordinal bigint, p_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_lock jsonb;
BEGIN
    IF p_id IS NULL OR p_ordinal IS NULL OR p_ordinal < 1
       OR NOT aap_jobs_v1._batch_result_scope_valid(p_scope, true) THEN
        RETURN false;
    END IF;
    v_lock := aap_jobs_v1._lock_batch_result_scope(p_scope, true, true);
    IF v_lock -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR v_lock #> '{data,cancel_requested}' IS DISTINCT FROM 'false'::jsonb
       OR aap_jobs_v1.prediction_fixture_open((p_scope ->> 'job_id')::uuid) IS DISTINCT FROM true
       OR NOT EXISTS (
           SELECT 1 FROM public.batch_result_bundles AS result
           WHERE result.job_id = (p_scope ->> 'job_id')::uuid
             AND result.ordinal = p_ordinal AND result.raw_complete AND NOT result.prepared_complete
       ) THEN
        RETURN false;
    END IF;
    INSERT INTO aap_jobs_v1.prediction_mask_producers(
        id, job_id, actor_id, project_id, request_digest, generation, lease_token_digest, ordinal, state
    ) VALUES (
        p_id, (p_scope ->> 'job_id')::uuid, (p_scope ->> 'actor_id')::uuid,
        (p_scope ->> 'project_id')::uuid, p_scope ->> 'request_digest',
        (p_scope ->> 'generation')::bigint,
        public.digest(pg_catalog.convert_to(p_scope ->> 'lease_token', 'UTF8'), 'sha256'),
        p_ordinal, 'pending'
    );
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.begin_prediction_mask_producer(jsonb,bigint,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.bind_prediction_mask_object(p_producer_id uuid, p_object_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_producer aap_jobs_v1.prediction_mask_producers%ROWTYPE;
    v_execution public.batch_result_executions%ROWTYPE;
    v_job public.async_jobs%ROWTYPE;
BEGIN
    SELECT * INTO v_producer FROM aap_jobs_v1.prediction_mask_producers
    WHERE id = p_producer_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_producer.state <> 'pending' THEN RETURN false; END IF;
    SELECT * INTO v_job FROM public.async_jobs WHERE id = v_producer.job_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_job.status NOT IN ('pending','running')
       OR v_job.user_id IS DISTINCT FROM v_producer.actor_id
       OR v_job.project_id IS DISTINCT FROM v_producer.project_id
       OR aap_jobs_v1.prediction_fixture_open(v_producer.job_id) IS DISTINCT FROM true
       OR COALESCE(v_job.payload -> 'cancel_requested', 'false'::jsonb) IS DISTINCT FROM 'false'::jsonb THEN
        RETURN false;
    END IF;
    SELECT * INTO v_execution FROM public.batch_result_executions
    WHERE job_id = v_producer.job_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_execution.state NOT IN ('preparing','running')
       OR v_execution.actor_id IS DISTINCT FROM v_producer.actor_id
       OR v_execution.project_id IS DISTINCT FROM v_producer.project_id
       OR v_execution.request_digest IS DISTINCT FROM v_producer.request_digest
       OR v_execution.generation IS DISTINCT FROM v_producer.generation
       OR v_execution.lease_token IS NULL OR v_execution.lease_expires_at IS NULL
       OR v_execution.lease_expires_at <= pg_catalog.clock_timestamp()
       OR public.digest(pg_catalog.convert_to(v_execution.lease_token::text, 'UTF8'), 'sha256')
          IS DISTINCT FROM v_producer.lease_token_digest THEN
        RETURN false;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.raster_mask_objects WHERE id=p_object_id) THEN
        RETURN false;
    END IF;
    INSERT INTO aap_jobs_v1.prediction_mask_producer_objects(producer_id, object_id)
    VALUES (p_producer_id, p_object_id) ON CONFLICT DO NOTHING;
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.bind_prediction_mask_object(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.finish_prediction_mask_producer(p_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE aap_jobs_v1.prediction_mask_producers AS producer
    SET state = 'settled', settled_at = pg_catalog.clock_timestamp()
    WHERE producer.id = p_id AND producer.state = 'pending'
      AND EXISTS (SELECT 1 FROM public.batch_result_bundles AS result
                  WHERE result.job_id = producer.job_id AND result.ordinal = producer.ordinal
                    AND result.prepared_complete);
    RETURN FOUND;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.finish_prediction_mask_producer(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.prediction_mask_object_retained(p_object_id uuid)
RETURNS boolean LANGUAGE sql STABLE PARALLEL SAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM aap_jobs_v1.prediction_mask_producer_objects AS object
        JOIN aap_jobs_v1.prediction_mask_producers AS producer ON producer.id = object.producer_id
        WHERE object.object_id = p_object_id AND producer.state = 'pending'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.prediction_mask_object_retained(uuid) FROM PUBLIC;
-- aap-statement-break
-- Record the post-cutover birth of a fixture Job; older unbound Jobs are not adopted.
CREATE TABLE aap_jobs_v1.fixture_prediction_job_births (
    job_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES yoao_fixture_v1.fixture_runs(id) ON DELETE RESTRICT,
    project_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('batch_predict', 'prediction_retry'))
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.fixture_prediction_job_births FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.record_fixture_prediction_job_birth()
RETURNS trigger LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_prediction_job_births WHERE job_id=NEW.id) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_prediction_job_identity_retired';
    END IF;
    IF NEW.kind IN ('batch_predict','prediction_retry') THEN
        INSERT INTO aap_jobs_v1.fixture_prediction_job_births(job_id, run_id, project_id, actor_id, kind)
        SELECT NEW.id, resource.run_id, NEW.project_id, NEW.user_id, NEW.kind
        FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.kind = 'project' AND resource.resource_id = NEW.project_id
          AND resource.state = 'published';
    END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
CREATE TRIGGER aap_fixture_prediction_job_birth
AFTER INSERT ON public.async_jobs
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1.record_fixture_prediction_job_birth();
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.record_fixture_prediction_job_birth() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.prediction_fixture_open(p_job_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_run uuid; v_root uuid; v_run_state text; v_root_state text;
BEGIN
    SELECT birth.run_id INTO v_run FROM aap_jobs_v1.fixture_prediction_job_births AS birth
    WHERE birth.job_id=p_job_id;
    IF NOT FOUND THEN
        RETURN NOT EXISTS (
            SELECT 1 FROM public.async_jobs AS job
            JOIN yoao_fixture_v1.fixture_run_resources AS project
              ON project.kind='project' AND project.resource_id=job.project_id
            WHERE job.id=p_job_id
        );
    END IF;
    SELECT COALESCE(run.parent_run_id,run.id) INTO v_root
    FROM yoao_fixture_v1.fixture_runs AS run WHERE run.id=v_run;
    SELECT state INTO v_root_state FROM yoao_fixture_v1.fixture_runs WHERE id=v_root FOR SHARE NOWAIT;
    SELECT state INTO v_run_state FROM yoao_fixture_v1.fixture_runs WHERE id=v_run FOR SHARE NOWAIT;
    RETURN v_root_state='open' AND v_run_state='open';
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.prediction_fixture_open(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_jobs_v1.fixture_prediction_dispatch_tombstones (
    intent_id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES aap_jobs_v1.fixture_prediction_job_births(job_id) ON DELETE RESTRICT,
    snapshot jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(snapshot)='object'),
    captured_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.fixture_prediction_dispatch_tombstones FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.preserve_fixture_prediction_dispatches()
RETURNS trigger LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_prediction_job_births WHERE job_id=OLD.id) THEN
        PERFORM intent.intent_id FROM aap_jobs_v1.dispatch_intents AS intent
        WHERE intent.job_id=OLD.id ORDER BY intent.intent_id FOR UPDATE NOWAIT;
        INSERT INTO aap_jobs_v1.fixture_prediction_dispatch_tombstones(intent_id,job_id,snapshot)
        SELECT intent.intent_id, intent.job_id, pg_catalog.to_jsonb(intent)
        FROM aap_jobs_v1.dispatch_intents AS intent WHERE intent.job_id=OLD.id;
    END IF;
    RETURN OLD;
END
$function$;
-- aap-statement-break
CREATE TRIGGER aap_fixture_prediction_preserve_dispatch
BEFORE DELETE ON public.async_jobs
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1.preserve_fixture_prediction_dispatches();
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.preserve_fixture_prediction_dispatches() FROM PUBLIC;
-- aap-statement-break
-- Compose the existing fixture original admission fence with exact outstanding
-- Assets read-operation receipts; no sampled process counters are used.
CREATE FUNCTION aap_assets_v1.fixture_prediction_inputs_retired(p_run_id uuid, p_task_ids uuid[])
RETURNS boolean LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_task uuid;
BEGIN
    IF p_run_id IS NULL OR p_task_ids IS NULL
       OR pg_catalog.array_position(p_task_ids, NULL::uuid) IS NOT NULL
       OR NOT EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_runs WHERE id=p_run_id AND state='sealing')
    THEN RETURN false; END IF;
    FOR v_task IN SELECT DISTINCT id FROM pg_catalog.unnest(p_task_ids) AS ids(id)
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM aap_labeling_v1.task_rows AS task
            JOIN yoao_fixture_v1.fixture_run_resources AS project
              ON project.run_id=p_run_id AND project.kind='project'
             AND project.resource_id=task.project_id AND project.state='published'
            JOIN public.dataset_items AS item ON item.id=task.dataset_item_id
            JOIN public.dataset_assets AS asset ON asset.dataset_item_id=item.id
            JOIN public.dataset_asset_locations AS location
              ON location.asset_id=asset.id AND location.role='managed_original'
             AND location.reference_state='present' AND location.object_key=task.file_path
            JOIN public.managed_original_creations AS creation ON creation.id=location.creation_id
            JOIN yoao_fixture_v1.fixture_run_resources AS source
              ON source.run_id=p_run_id AND source.kind='creation' AND source.creation_id=creation.id
             AND source.item_id=item.id AND source.state='published'
            WHERE task.id=v_task AND task.file_type='image'
              AND item.file_path=task.file_path AND creation.object_key=location.object_key
              AND creation.bucket=location.bucket AND creation.location_id=location.location_id
              AND creation.status='verified' AND creation.settled_at IS NOT NULL
              AND creation.version_id IS NOT NULL AND creation.version_id <> 'null'
              AND asset.verification_state='verified' AND asset.checksum_value=creation.sha256
              AND asset.size_bytes=creation.size_bytes
              AND aap_assets_v1.classify_fixture_original_reclaim(creation.id,creation.storage_identity)='qualified'
        ) THEN RETURN false; END IF;
    END LOOP;
    RETURN NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS source
        JOIN public.managed_original_creations AS creation ON creation.id=source.creation_id
        JOIN public.storage_operations AS operation
          ON operation.location_id=creation.location_id
         AND operation.purpose='media_original_' || pg_catalog.replace(creation.id::text,'-','')
         AND operation.node_id='aap-assets-media'
        WHERE source.run_id=p_run_id AND source.kind='creation'
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_prediction_inputs_retired(uuid,uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.fixture_prediction_retired(p_run_id uuid, p_project_id uuid, p_job_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_execution public.batch_result_executions%ROWTYPE;
    v_attempt public.prediction_retry_attempts%ROWTYPE;
    v_delivery public.prediction_retry_deliveries%ROWTYPE;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture retirement requires a test database';
    END IF;
    IF p_run_id IS NULL OR p_project_id IS NULL OR p_job_id IS NULL OR NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_runs AS run
        JOIN yoao_fixture_v1.fixture_run_resources AS project ON project.run_id = run.id
        WHERE run.id = p_run_id AND run.state = 'sealing' AND project.kind = 'project'
          AND project.resource_id = p_project_id AND project.state = 'published'
    ) THEN
        RETURN false;
    END IF;
    PERFORM run.id FROM yoao_fixture_v1.fixture_runs AS run
    WHERE run.id=p_run_id AND run.state='sealing' FOR SHARE NOWAIT;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT * INTO v_job FROM public.async_jobs WHERE id = p_job_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_job.project_id IS DISTINCT FROM p_project_id
       OR v_job.kind NOT IN ('batch_predict','prediction_retry')
       OR v_job.status NOT IN ('completed','failed','cancelled')
       OR NOT EXISTS (
           SELECT 1 FROM aap_jobs_v1.fixture_prediction_job_births AS birth
           WHERE birth.job_id = v_job.id AND birth.run_id = p_run_id
             AND birth.project_id = v_job.project_id AND birth.actor_id = v_job.user_id
             AND birth.kind = v_job.kind
       ) THEN RETURN false; END IF;
    SELECT * INTO v_execution FROM public.batch_result_executions
    WHERE job_id = p_job_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_execution.project_id IS DISTINCT FROM p_project_id
       OR v_execution.actor_id IS DISTINCT FROM v_job.user_id
       OR v_execution.state IS DISTINCT FROM v_job.status THEN RETURN false; END IF;
    IF aap_assets_v1.fixture_prediction_inputs_retired(
        p_run_id,
        COALESCE((SELECT pg_catalog.array_agg(member.task_id ORDER BY member.ordinal)
                  FROM public.batch_result_members AS member WHERE member.job_id=p_job_id),
                 ARRAY[]::uuid[])
    ) IS DISTINCT FROM true THEN RETURN false; END IF;
    IF v_job.kind = 'prediction_retry' THEN
        SELECT * INTO v_attempt FROM public.prediction_retry_attempts WHERE job_id = p_job_id FOR UPDATE NOWAIT;
        IF NOT FOUND OR v_attempt.project_id IS DISTINCT FROM p_project_id
           OR v_attempt.actor_id IS DISTINCT FROM v_job.user_id
           OR v_attempt.request_digest IS DISTINCT FROM v_execution.request_digest
           OR v_attempt.legacy_delivery OR v_attempt.state NOT IN ('succeeded','failed','abandoned')
           OR v_attempt.outcome IS NULL OR v_attempt.terminal_digest IS NULL
           OR NOT aap_labeling_v1._prediction_retry_terminal_matches(p_job_id, v_attempt.state, v_attempt.outcome)
           OR (v_attempt.state = 'succeeded' AND v_job.status <> 'completed')
           OR (v_attempt.state = 'failed' AND v_job.status <> 'failed')
           OR (v_attempt.state = 'abandoned' AND v_job.status NOT IN ('failed','cancelled'))
        THEN RETURN false; END IF;
        SELECT * INTO v_delivery FROM public.prediction_retry_deliveries WHERE job_id = p_job_id FOR UPDATE NOWAIT;
        IF NOT FOUND OR v_delivery.state <> 'terminal'
           OR v_delivery.project_id IS DISTINCT FROM p_project_id
           OR v_delivery.actor_id IS DISTINCT FROM v_attempt.actor_id
           OR v_delivery.failed_prediction_id IS DISTINCT FROM v_attempt.failed_prediction_id
        THEN RETURN false; END IF;
    END IF;
    IF EXISTS (
        SELECT 1 FROM aap_jobs_v1.prediction_mask_producers AS producer
        WHERE producer.job_id = p_job_id AND
            (producer.state <> 'settled' OR producer.project_id IS DISTINCT FROM p_project_id
             OR producer.actor_id IS DISTINCT FROM v_job.user_id
             OR producer.request_digest IS DISTINCT FROM v_execution.request_digest
             OR producer.generation > v_execution.generation)
    ) OR EXISTS (
        SELECT 1 FROM aap_inference_routing_v1.prediction_execution_scopes AS binding
        LEFT JOIN aap_inference_routing_v1.scope_retirements AS retirement ON retirement.scope_id = binding.scope_id
        WHERE binding.job_id = p_job_id AND
            (binding.project_id IS DISTINCT FROM p_project_id
             OR binding.actor_id IS DISTINCT FROM v_job.user_id
             OR binding.request_digest IS DISTINCT FROM v_execution.request_digest
             OR binding.generation > v_execution.generation
             OR binding.uses_gpu
             OR retirement.phase IS DISTINCT FROM 'completed')
    ) OR EXISTS (
        SELECT 1 FROM aap_inference_routing_v1.scopes AS scope
        WHERE scope.project_id = p_project_id
          AND scope.operation IN ('batch_predict','image_batch_predict','prediction_retry')
          AND NOT EXISTS (
              SELECT 1 FROM aap_inference_routing_v1.prediction_execution_scopes AS binding
              WHERE binding.scope_id = scope.id AND binding.project_id = p_project_id
          )
    ) OR EXISTS (
        SELECT 1 FROM aap_jobs_v1.dispatch_intents AS intent
        WHERE intent.job_id = p_job_id AND (
            intent.purpose <> 'execution'
            OR intent.execution_id IS DISTINCT FROM p_job_id::text
            OR intent.envelope #>> '{kwargs,async_job_id}' IS DISTINCT FROM p_job_id::text
            OR intent.envelope ->> 'task_name' IS DISTINCT FROM 'app.workers.tasks.batch_predict'
            OR v_job.kind <> 'batch_predict'
        )
    ) THEN RETURN false; END IF;
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_prediction_retired(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.guard_fixture_prediction_ledgers(p_run_id uuid, p_project_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_id uuid; v_failure text;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture retirement requires a test database';
    END IF;
    IF p_run_id IS NULL OR p_project_id IS NULL OR NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_runs AS run
        JOIN yoao_fixture_v1.fixture_run_resources AS project ON project.run_id = run.id
        WHERE run.id = p_run_id AND run.state = 'sealing' AND project.kind = 'project'
          AND project.resource_id = p_project_id AND project.state = 'published'
    ) THEN RETURN false; END IF;
    PERFORM run.id FROM yoao_fixture_v1.fixture_runs AS run
    WHERE run.id=p_run_id AND run.state='sealing' FOR SHARE NOWAIT;
    IF NOT FOUND THEN RETURN false; END IF;
    PERFORM job.id FROM public.async_jobs AS job
    WHERE job.project_id=p_project_id OR job.retained_project_id=p_project_id
    ORDER BY job.id FOR UPDATE NOWAIT;
    PERFORM failed.id FROM public.failed_predictions AS failed
    WHERE failed.project_id = p_project_id ORDER BY failed.id FOR UPDATE NOWAIT;
    IF EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_presentation_jobs AS proof
        LEFT JOIN public.async_jobs AS job ON job.id=proof.job_id
        WHERE proof.run_id=p_run_id AND proof.project_id=p_project_id AND (
            proof.state <> 'admitted' OR job.id IS NULL
            OR job.project_id IS DISTINCT FROM p_project_id
            OR job.kind IS DISTINCT FROM proof.kind OR job.status='running'
            OR job.celery_task_id IS NOT NULL OR job.started_at IS NOT NULL
            OR EXISTS (SELECT 1 FROM aap_jobs_v1.dispatch_intents AS intent WHERE intent.job_id=proof.job_id)
            OR EXISTS (SELECT 1 FROM public.batch_result_executions AS execution WHERE execution.job_id=proof.job_id)
            OR EXISTS (SELECT 1 FROM public.prediction_retry_attempts AS attempt WHERE attempt.job_id=proof.job_id)
            OR EXISTS (SELECT 1 FROM public.prediction_retry_deliveries AS delivery WHERE delivery.job_id=proof.job_id)
        )
    ) THEN RETURN false; END IF;
    IF EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_presentation_jobs AS proof
        JOIN public.async_jobs AS job ON job.id=proof.job_id
        WHERE proof.run_id=p_run_id AND proof.project_id=p_project_id
          AND job.result ? 'failed_prediction_ids'
          AND pg_catalog.jsonb_typeof(job.result -> 'failed_prediction_ids') IS DISTINCT FROM 'array'
    ) THEN RETURN false; END IF;
    FOR v_failure IN
        SELECT failed.value FROM yoao_fixture_v1.fixture_presentation_jobs AS proof
        JOIN public.async_jobs AS job ON job.id=proof.job_id
        CROSS JOIN LATERAL pg_catalog.jsonb_array_elements_text(
            COALESCE(job.result -> 'failed_prediction_ids','[]'::jsonb)
        ) AS failed(value)
        WHERE proof.run_id=p_run_id AND proof.project_id=p_project_id
    LOOP
        IF v_failure !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR (NOT EXISTS (SELECT 1 FROM public.failed_predictions AS failed
                           WHERE failed.id::text=v_failure AND failed.project_id=p_project_id)
               AND NOT EXISTS (SELECT 1 FROM public.prediction_retry_attempts AS attempt
                               WHERE attempt.failed_prediction_id::text=v_failure
                                 AND attempt.project_id=p_project_id)) THEN
            RETURN false;
        END IF;
    END LOOP;
    FOR v_id IN
        WITH predecessor_ids AS (
            SELECT failed.id::text AS id FROM public.failed_predictions AS failed
            WHERE failed.project_id=p_project_id
            UNION
            SELECT failed.value FROM yoao_fixture_v1.fixture_presentation_jobs AS proof
            JOIN public.async_jobs AS job ON job.id=proof.job_id
            CROSS JOIN LATERAL pg_catalog.jsonb_array_elements_text(
                COALESCE(job.result -> 'failed_prediction_ids','[]'::jsonb)
            ) AS failed(value)
            WHERE proof.run_id=p_run_id AND proof.project_id=p_project_id
        )
        SELECT attempt.job_id FROM public.prediction_retry_attempts AS attempt
        WHERE attempt.project_id=p_project_id
           OR attempt.failed_prediction_id::text IN (SELECT id FROM predecessor_ids)
        UNION
        SELECT delivery.job_id FROM public.prediction_retry_deliveries AS delivery
        WHERE delivery.project_id=p_project_id
           OR delivery.failed_prediction_id::text IN (SELECT id FROM predecessor_ids)
        ORDER BY 1
    LOOP
        IF aap_jobs_v1.fixture_prediction_retired(p_run_id, p_project_id, v_id) IS DISTINCT FROM true THEN
            RETURN false;
        END IF;
    END LOOP;
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_fixture_prediction_ledgers(uuid,uuid) FROM PUBLIC;
