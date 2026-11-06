-- A fixture backfill retains the real dataset, operation and queue identity;
-- no project is manufactured for this dataset-scoped producer.
CREATE TABLE aap_jobs_v1.fixture_media_backfills (
    job_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    operation_id uuid NOT NULL,
    payload jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(payload) = 'object'),
    queue text NOT NULL,
    state text NOT NULL CHECK (state IN ('pending', 'running', 'settled', 'retired')),
    retirement_outcome text CHECK (retirement_outcome IN ('executed','not_sent')),
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    token uuid,
    backend_pid integer,
    CHECK ((state = 'running') = (token IS NOT NULL AND backend_pid IS NOT NULL)),
    CHECK ((state = 'retired') = (retirement_outcome IS NOT NULL))
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.fixture_media_backfills FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.guard_fixture_media_backfill_evidence()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP = 'DELETE' OR (NEW.job_id,NEW.run_id,NEW.dataset_id,NEW.actor_id,
            NEW.operation_id,NEW.payload,NEW.queue)
       IS DISTINCT FROM (OLD.job_id,OLD.run_id,OLD.dataset_id,OLD.actor_id,
            OLD.operation_id,OLD.payload,OLD.queue)
       OR (NEW.retirement_outcome IS DISTINCT FROM OLD.retirement_outcome
           AND NOT (OLD.state IN ('pending','settled') AND NEW.state='retired'
                    AND NEW.retirement_outcome IN ('executed','not_sent')))
       OR (OLD.state = 'retired' AND NEW IS DISTINCT FROM OLD) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_media_backfill_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_fixture_media_backfill_evidence() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_media_backfill_evidence_immutable
BEFORE UPDATE OR DELETE ON aap_jobs_v1.fixture_media_backfills
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1.guard_fixture_media_backfill_evidence();
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.capture_fixture_media_backfill()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_scope jsonb;
    v_dataset uuid;
    v_operation uuid;
    v_run uuid;
    v_root uuid;
    v_state text;
    v_root_state text;
BEGIN
    IF NEW.kind <> 'media_backfill' THEN RETURN NEW; END IF;
    v_scope := aap_jobs_v1.fixture_dataset_job_scope(
        NEW.id,NEW.kind,NEW.payload,NEW.project_id,NEW.user_id
    );
    IF v_scope ->> 'kind' <> 'dataset' THEN RETURN NEW; END IF;
    v_dataset := (v_scope ->> 'dataset_id')::uuid;
    SELECT resource.run_id INTO v_run FROM yoao_fixture_v1.fixture_run_resources AS resource
    WHERE resource.kind='dataset' AND resource.resource_id=v_dataset;
    IF NOT FOUND THEN RETURN NEW; END IF;
    IF NEW.project_id IS NOT NULL OR NEW.user_id IS NULL
       OR NEW.payload ->> 'media_backfill_kind' IS DISTINCT FROM 'aap-media-backfill/v1'
       OR COALESCE(NEW.payload ->> 'purpose' IN ('dimensions','thumbnail'), false) IS NOT TRUE
       OR pg_catalog.jsonb_typeof(NEW.payload -> 'video_delegations') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(NEW.payload -> 'accepted_before') IS DISTINCT FROM 'string'
       OR (NEW.payload ->> 'accepted_before') !~ '([zZ]|[+-][0-9]{2}:[0-9]{2})$'
       OR pg_catalog.jsonb_typeof(NEW.payload -> 'operation_id') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_backfill_invalid';
    END IF;
    BEGIN
        v_operation := (NEW.payload ->> 'operation_id')::uuid;
        PERFORM (NEW.payload ->> 'accepted_before')::timestamptz;
    EXCEPTION WHEN invalid_text_representation OR datetime_field_overflow THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_backfill_invalid';
    END;
    SELECT COALESCE(run.parent_run_id,run.id) INTO v_root
    FROM yoao_fixture_v1.fixture_runs AS run WHERE run.id=v_run;
    SELECT root.state INTO v_root_state FROM yoao_fixture_v1.fixture_runs AS root
    WHERE root.id=v_root AND root.parent_run_id IS NULL FOR SHARE;
    SELECT run.state INTO v_state FROM yoao_fixture_v1.fixture_runs AS run
    WHERE run.id=v_run AND COALESCE(run.parent_run_id,run.id)=v_root FOR SHARE;
    IF v_state IS DISTINCT FROM 'open' OR v_root_state IS DISTINCT FROM 'open'
       OR NOT EXISTS (
           SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS dataset
           WHERE dataset.run_id=v_run AND dataset.kind='dataset'
             AND dataset.resource_id=v_dataset AND dataset.state='published'
       ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_run_closed';
    END IF;
    INSERT INTO aap_jobs_v1.fixture_media_backfills
        (job_id,run_id,dataset_id,actor_id,operation_id,payload,queue,state)
    VALUES (NEW.id,v_run,v_dataset,NEW.user_id,v_operation,NEW.payload,
        'aap_workspace_worker_' || pg_catalog.replace(v_run::text,'-','') || '_media',
        'pending');
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.capture_fixture_media_backfill() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_media_backfill_birth AFTER INSERT ON public.async_jobs
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1.capture_fixture_media_backfill();
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.fixture_media_backfill_route(p_job_id uuid)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_queue text; v_state text; v_root_state text;
BEGIN
    SELECT backfill.queue,run.state,root.state INTO v_queue,v_state,v_root_state
    FROM aap_jobs_v1.fixture_media_backfills AS backfill
    JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=backfill.run_id
    JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=COALESCE(run.parent_run_id,run.id)
    WHERE backfill.job_id=p_job_id FOR SHARE OF run,root;
    IF NOT FOUND THEN
        IF EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_media_backfills WHERE job_id=p_job_id) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_backfill_unsettled';
        END IF;
        RETURN 'media';
    END IF;
    IF v_state <> 'open' OR v_root_state <> 'open' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_run_closed';
    END IF;
    RETURN v_queue;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_media_backfill_route(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.guard_fixture_media_backfill_job()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_backfill aap_jobs_v1.fixture_media_backfills%ROWTYPE;
BEGIN
    SELECT * INTO v_backfill FROM aap_jobs_v1.fixture_media_backfills AS backfill
    WHERE backfill.job_id=OLD.id;
    IF NOT FOUND THEN RETURN NEW; END IF;
    IF (NEW.id,NEW.kind,NEW.project_id,NEW.user_id,NEW.payload)
       IS DISTINCT FROM (OLD.id,OLD.kind,OLD.project_id,OLD.user_id,OLD.payload)
       OR NEW.kind <> 'media_backfill' OR NEW.project_id IS NOT NULL
       OR NEW.user_id <> v_backfill.actor_id OR NEW.payload <> v_backfill.payload THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_backfill_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_fixture_media_backfill_job() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_media_backfill_job_immutable
BEFORE UPDATE OF id,kind,project_id,user_id,payload ON public.async_jobs
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1.guard_fixture_media_backfill_job();
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.guard_fixture_media_job_reuse()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='UPDATE' AND NEW.id IS NOT DISTINCT FROM OLD.id THEN RETURN NEW; END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-fixture-media:job-birth:' || NEW.id::text,0)
    );
    IF EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_media_backfills AS binding
               WHERE binding.job_id=NEW.id) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_job_id_reused';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_fixture_media_job_reuse() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_media_job_identity
BEFORE INSERT OR UPDATE OF id ON public.async_jobs
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1.guard_fixture_media_job_reuse();
