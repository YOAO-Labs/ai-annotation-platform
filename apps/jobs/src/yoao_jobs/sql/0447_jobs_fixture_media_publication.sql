-- Fixture media delivery is an independent Jobs publication, not an AsyncJob.
CREATE TABLE aap_jobs_v1.fixture_media_deliveries (
    delivery_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    dataset_id uuid,
    item_id uuid,
    owner_kind text NOT NULL CHECK (owner_kind IN ('task', 'dataset_item')),
    owner_id uuid NOT NULL,
    producer_kind text NOT NULL CHECK (producer_kind IN (
        'pyramid', 'thumbnail', 'task_thumbnail', 'video_metadata', 'video_derivatives'
    )),
    queue text NOT NULL,
    source jsonb NOT NULL,
    envelope jsonb NOT NULL,
    payload_digest text NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
    CHECK ((dataset_id IS NULL) = (item_id IS NULL)),
    CHECK (dataset_id IS NOT NULL OR owner_kind = 'task')
);
-- aap-statement-break
CREATE TABLE aap_jobs_v1.fixture_media_publication_attempts (
    nonce uuid PRIMARY KEY,
    delivery_id uuid NOT NULL REFERENCES aap_jobs_v1.fixture_media_deliveries(delivery_id),
    backend_pid integer NOT NULL,
    state text NOT NULL CHECK (state IN ('entered', 'accepted', 'not_sent'))
);
-- aap-statement-break
CREATE INDEX fixture_media_publication_attempts_delivery
ON aap_jobs_v1.fixture_media_publication_attempts(delivery_id);
-- aap-statement-break
CREATE TABLE aap_jobs_v1.fixture_media_publication_retirements (
    delivery_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    payload_digest text NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('accepted', 'not_sent')),
    retired_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.fixture_media_deliveries,
    aap_jobs_v1.fixture_media_publication_attempts,
    aap_jobs_v1.fixture_media_publication_retirements FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.guard_fixture_media_publication_record()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture media publication record is immutable';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_fixture_media_publication_record() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER guard_fixture_media_delivery BEFORE UPDATE OR DELETE
ON aap_jobs_v1.fixture_media_deliveries FOR EACH ROW
EXECUTE FUNCTION aap_jobs_v1.guard_fixture_media_publication_record();
-- aap-statement-break
CREATE TRIGGER guard_fixture_media_publication_retirement BEFORE UPDATE OR DELETE
ON aap_jobs_v1.fixture_media_publication_retirements FOR EACH ROW
EXECUTE FUNCTION aap_jobs_v1.guard_fixture_media_publication_record();
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.guard_fixture_media_publication_attempt()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.state <> 'entered'
       OR NEW.state NOT IN ('accepted', 'not_sent')
       OR (pg_catalog.to_jsonb(NEW) - 'state') IS DISTINCT FROM
          (pg_catalog.to_jsonb(OLD) - 'state') THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture media publication attempt is immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_fixture_media_publication_attempt() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER guard_fixture_media_publication_attempt BEFORE UPDATE OR DELETE
ON aap_jobs_v1.fixture_media_publication_attempts FOR EACH ROW
EXECUTE FUNCTION aap_jobs_v1.guard_fixture_media_publication_attempt();
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.lock_fixture_media_run(p_run uuid, p_sealing boolean)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_parent uuid;
    v_root yoao_fixture_v1.fixture_runs%ROWTYPE;
    v_run yoao_fixture_v1.fixture_runs%ROWTYPE;
BEGIN
    IF p_run IS NULL OR p_sealing IS NULL THEN RETURN false; END IF;
    SELECT parent_run_id INTO v_parent FROM yoao_fixture_v1.fixture_runs WHERE id = p_run;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT * INTO v_root FROM yoao_fixture_v1.fixture_runs
    WHERE id = COALESCE(v_parent, p_run) FOR SHARE;
    IF NOT FOUND OR v_root.parent_run_id IS NOT NULL THEN RETURN false; END IF;
    SELECT * INTO v_run FROM yoao_fixture_v1.fixture_runs WHERE id = p_run FOR SHARE;
    IF NOT FOUND OR COALESCE(v_run.parent_run_id, v_run.id) <> v_root.id
       OR v_run.family <> v_root.family THEN RETURN false; END IF;
    IF p_sealing THEN
        RETURN v_run.state = 'sealing' AND v_root.state IN ('open', 'sealing');
    END IF;
    RETURN v_run.state = 'open' AND v_root.state = 'open';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.lock_fixture_media_run(uuid,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.fixture_media_publisher_owns(p_delivery uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_locks AS held
        CROSS JOIN LATERAL (
            SELECT pg_catalog.hashtextextended('aap-jobs:fixture-media:' || p_delivery::text, 0) AS key
        ) AS lock_key
        WHERE held.locktype = 'advisory' AND held.pid = pg_catalog.pg_backend_pid()
          AND held.mode = 'ExclusiveLock' AND held.granted AND held.objsubid = 1
          AND held.classid::bigint = ((lock_key.key >> 32) & 4294967295::bigint)
          AND held.objid::bigint = (lock_key.key & 4294967295::bigint)
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_media_publisher_owns(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.prepare_fixture_media_delivery(
    p_run uuid, p_dataset uuid, p_item uuid, p_owner_kind text, p_owner_id uuid,
    p_producer_kind text, p_delivery uuid, p_queue text, p_source jsonb, p_envelope jsonb
) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_task text;
    v_queue text;
    v_digest text;
    v_delivery aap_jobs_v1.fixture_media_deliveries%ROWTYPE;
BEGIN
    IF p_delivery IS NULL OR p_owner_id IS NULL OR p_owner_kind IS NULL
       OR p_owner_kind NOT IN ('task', 'dataset_item')
       OR (p_dataset IS NULL) IS DISTINCT FROM (p_item IS NULL)
       OR (p_dataset IS NULL AND p_owner_kind <> 'task')
       OR pg_catalog.jsonb_typeof(p_source) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_envelope) IS DISTINCT FROM 'object'
       OR NOT aap_jobs_v1.lock_fixture_media_run(p_run, false) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_media_publication_invalid';
    END IF;
    v_task := CASE p_producer_kind
        WHEN 'pyramid' THEN 'app.workers.image_pyramid.generate_image_pyramid'
        WHEN 'thumbnail' THEN 'app.workers.media.generate_thumbnail'
        WHEN 'task_thumbnail' THEN 'app.workers.media.generate_task_thumbnail'
        WHEN 'video_metadata' THEN 'app.workers.media.generate_video_metadata'
        WHEN 'video_derivatives' THEN 'app.workers.media.generate_video_derivatives'
    END;
    v_queue := 'aap_workspace_worker_' || pg_catalog.replace(p_run::text, '-', '') ||
        CASE WHEN p_producer_kind = 'pyramid' THEN '_pyramid' ELSE '_media' END;
    IF v_task IS NULL OR p_queue IS DISTINCT FROM v_queue
       OR NOT (p_envelope ?& ARRAY['schema','operation','task_name','execution_id','args','kwargs','options'])
       OR (p_envelope - ARRAY['schema','operation','task_name','execution_id','args','kwargs','options']) <> '{}'::jsonb
       OR p_envelope->>'schema' IS DISTINCT FROM 'aap-jobs-queue/v2'
       OR p_envelope->>'operation' IS DISTINCT FROM 'dispatch'
       OR p_envelope->>'task_name' IS DISTINCT FROM v_task
       OR p_envelope->>'execution_id' IS DISTINCT FROM p_delivery::text
       OR pg_catalog.jsonb_typeof(p_envelope->'args') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(p_envelope->'kwargs') IS DISTINCT FROM 'object'
       OR p_envelope->'options' IS DISTINCT FROM pg_catalog.jsonb_build_object('queue', p_queue)
       OR aap_assets_v1.fixture_independent_source_matches(
           p_run, p_dataset, p_item, p_owner_kind, p_owner_id, p_producer_kind, p_source, p_envelope
       ) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_media_publication_invalid';
    END IF;
    IF aap_assets_v1.prepare_fixture_independent_execution(
        p_run, p_dataset, p_item, p_owner_kind, p_owner_id, p_producer_kind,
        p_delivery, p_queue, p_source, p_envelope
    ) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_media_execution_birth_invalid';
    END IF;
    v_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p_envelope::text, 'UTF8')), 'hex');
    INSERT INTO aap_jobs_v1.fixture_media_deliveries(
        delivery_id, run_id, dataset_id, item_id, owner_kind, owner_id,
        producer_kind, queue, source, envelope, payload_digest
    ) VALUES (
        p_delivery, p_run, p_dataset, p_item, p_owner_kind, p_owner_id,
        p_producer_kind, p_queue, p_source, p_envelope, v_digest
    ) ON CONFLICT (delivery_id) DO NOTHING;
    SELECT * INTO v_delivery FROM aap_jobs_v1.fixture_media_deliveries
    WHERE delivery_id = p_delivery FOR UPDATE;
    IF v_delivery.run_id IS DISTINCT FROM p_run
       OR v_delivery.dataset_id IS DISTINCT FROM p_dataset
       OR v_delivery.item_id IS DISTINCT FROM p_item
       OR v_delivery.owner_kind IS DISTINCT FROM p_owner_kind
       OR v_delivery.owner_id IS DISTINCT FROM p_owner_id
       OR v_delivery.producer_kind IS DISTINCT FROM p_producer_kind
       OR v_delivery.queue IS DISTINCT FROM p_queue
       OR v_delivery.source IS DISTINCT FROM p_source
       OR v_delivery.envelope IS DISTINCT FROM p_envelope
       OR v_delivery.payload_digest IS DISTINCT FROM v_digest THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_media_publication_identity_conflict';
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-jobs-fixture-media-publication/v1',
        'delivery_id', p_delivery, 'payload_digest', v_digest, 'envelope', v_delivery.envelope
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.prepare_fixture_media_delivery(uuid,uuid,uuid,text,uuid,text,uuid,text,jsonb,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.fixture_media_delivery(p_delivery uuid)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', 'aap-jobs-fixture-media-delivery/v1',
        'run_id', delivery.run_id, 'dataset_id', delivery.dataset_id,
        'item_id', delivery.item_id, 'owner_kind', delivery.owner_kind,
        'owner_id', delivery.owner_id, 'producer_kind', delivery.producer_kind,
        'delivery_id', delivery.delivery_id, 'queue', delivery.queue,
        'source', delivery.source, 'envelope', delivery.envelope,
        'payload_digest', delivery.payload_digest
    )
    FROM aap_jobs_v1.fixture_media_deliveries AS delivery
    WHERE delivery.delivery_id = p_delivery;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_media_delivery(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.fixture_media_publication_accepted(p_run uuid, p_delivery uuid, p_digest text)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF p_run IS NULL OR p_delivery IS NULL OR p_digest IS NULL THEN RETURN false; END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock_shared(
        pg_catalog.hashtextextended('aap-jobs:fixture-media:' || p_delivery::text, 0)
    );
    RETURN EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_deliveries AS delivery
        WHERE delivery.run_id = p_run AND delivery.delivery_id = p_delivery
          AND delivery.payload_digest = p_digest
          AND EXISTS (
              SELECT 1 FROM aap_jobs_v1.fixture_media_publication_attempts AS attempt
              WHERE attempt.delivery_id = p_delivery AND attempt.state = 'accepted'
          )
          AND NOT EXISTS (
              SELECT 1 FROM aap_jobs_v1.fixture_media_publication_attempts AS attempt
              WHERE attempt.delivery_id = p_delivery AND attempt.state = 'entered'
          )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_media_publication_accepted(uuid,uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.begin_fixture_media_publication(p_delivery uuid, p_nonce uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_delivery aap_jobs_v1.fixture_media_deliveries%ROWTYPE;
BEGIN
    IF p_delivery IS NULL OR p_nonce IS NULL OR NOT aap_jobs_v1.fixture_media_publisher_owns(p_delivery) THEN
        RETURN NULL;
    END IF;
    SELECT * INTO v_delivery FROM aap_jobs_v1.fixture_media_deliveries WHERE delivery_id = p_delivery;
    IF NOT FOUND OR NOT aap_jobs_v1.lock_fixture_media_run(v_delivery.run_id, false)
       OR EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_media_publication_retirements WHERE delivery_id = p_delivery) THEN
        RETURN NULL;
    END IF;
    IF EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_media_publication_attempts
               WHERE delivery_id = p_delivery AND state = 'accepted') THEN
        RETURN pg_catalog.jsonb_build_object('state', 'accepted', 'delivery_id', p_delivery,
            'payload_digest', v_delivery.payload_digest, 'envelope', v_delivery.envelope);
    END IF;
    INSERT INTO aap_jobs_v1.fixture_media_publication_attempts(nonce, delivery_id, backend_pid, state)
    VALUES (p_nonce, p_delivery, pg_catalog.pg_backend_pid(), 'entered') ON CONFLICT DO NOTHING;
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN pg_catalog.jsonb_build_object('state', 'entered', 'delivery_id', p_delivery,
        'payload_digest', v_delivery.payload_digest, 'envelope', v_delivery.envelope);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.begin_fixture_media_publication(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.finish_fixture_media_publication(p_delivery uuid, p_nonce uuid, p_outcome text)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF p_outcome IS NULL OR p_outcome NOT IN ('accepted', 'not_sent')
       OR NOT aap_jobs_v1.fixture_media_publisher_owns(p_delivery) THEN RETURN false; END IF;
    UPDATE aap_jobs_v1.fixture_media_publication_attempts SET state = p_outcome
    WHERE nonce = p_nonce AND delivery_id = p_delivery AND state = 'entered'
      AND backend_pid = pg_catalog.pg_backend_pid();
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.finish_fixture_media_publication(uuid,uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.retire_fixture_media_publication(p_run uuid, p_delivery uuid, p_digest text)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_delivery aap_jobs_v1.fixture_media_deliveries%ROWTYPE; v_outcome text;
BEGIN
    IF p_delivery IS NULL OR p_digest IS NULL OR NOT aap_jobs_v1.lock_fixture_media_run(p_run, true)
       OR NOT pg_catalog.pg_try_advisory_xact_lock(
           pg_catalog.hashtextextended('aap-jobs:fixture-media:' || p_delivery::text, 0)
       ) THEN RETURN 'blocked'; END IF;
    SELECT * INTO v_delivery FROM aap_jobs_v1.fixture_media_deliveries
    WHERE delivery_id = p_delivery FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_delivery.run_id <> p_run OR v_delivery.payload_digest <> p_digest THEN
        RETURN 'blocked';
    END IF;
    SELECT outcome INTO v_outcome FROM aap_jobs_v1.fixture_media_publication_retirements
    WHERE delivery_id = p_delivery AND run_id = p_run AND payload_digest = p_digest;
    IF FOUND THEN RETURN v_outcome; END IF;
    IF NOT EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_media_publication_attempts WHERE delivery_id = p_delivery)
       OR EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_media_publication_attempts
                  WHERE delivery_id = p_delivery AND state = 'entered') THEN
        RETURN 'blocked';
    END IF;
    v_outcome := CASE WHEN EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_publication_attempts
        WHERE delivery_id = p_delivery AND state = 'accepted'
    ) THEN 'accepted' ELSE 'not_sent' END;
    INSERT INTO aap_jobs_v1.fixture_media_publication_retirements(delivery_id, run_id, payload_digest, outcome)
    VALUES (p_delivery, p_run, p_digest, v_outcome);
    RETURN v_outcome;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.retire_fixture_media_publication(uuid,uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.fixture_media_publication_retired(p_run uuid, p_delivery uuid, p_digest text)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_publication_retirements
        WHERE run_id = p_run AND delivery_id = p_delivery AND payload_digest = p_digest
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_media_publication_retired(uuid,uuid,text) FROM PUBLIC;
