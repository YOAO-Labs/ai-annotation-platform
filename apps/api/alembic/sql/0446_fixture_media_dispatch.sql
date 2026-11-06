-- Preserve each exact sender receipt after the business Job disappears.
CREATE TABLE aap_jobs_v1.fixture_media_dispatches (
    intent_id uuid PRIMARY KEY,
    job_id uuid NOT NULL,
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    envelope jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(envelope)='object')
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.fixture_media_dispatches FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.guard_fixture_media_dispatch_evidence()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_dispatch_immutable';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_fixture_media_dispatch_evidence() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_media_dispatch_evidence_immutable
BEFORE UPDATE OR DELETE ON aap_jobs_v1.fixture_media_dispatches
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1.guard_fixture_media_dispatch_evidence();
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.guard_fixture_media_dispatch()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_binding aap_jobs_v1.fixture_media_backfills%ROWTYPE;
    v_child aap_jobs_v1.fixture_media_children%ROWTYPE;
    v_task text;
    v_args jsonb;
    v_queue text;
    v_state text;
    v_root_state text;
BEGIN
    SELECT * INTO v_binding FROM aap_jobs_v1.fixture_media_backfills
    WHERE job_id=NEW.job_id;
    IF NOT FOUND THEN RETURN NEW; END IF;
    IF NEW.execution_id=NEW.job_id::text THEN
        v_task := 'app.workers.media.run_media_backfill';
        v_args := pg_catalog.jsonb_build_array(NEW.job_id::text);
        v_queue := v_binding.queue;
    ELSE
        SELECT * INTO v_child FROM aap_jobs_v1.fixture_media_children AS child
        WHERE child.job_id=NEW.job_id AND child.child_id::text=NEW.execution_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_child_missing';
        END IF;
        v_task := 'app.workers.image_pyramid.generate_image_pyramid';
        v_args := pg_catalog.jsonb_build_array(
            'dataset_item',v_child.item_id::text,false,v_child.delegation
        );
        v_queue := v_child.queue;
    END IF;
    IF NEW.purpose IS DISTINCT FROM 'execution'
       OR NEW.natural_identity IS DISTINCT FROM true
       OR NEW.envelope ->> 'schema' IS DISTINCT FROM 'aap-jobs-queue/v2'
       OR NEW.envelope ->> 'operation' IS DISTINCT FROM 'dispatch'
       OR NEW.envelope ->> 'task_name' IS DISTINCT FROM v_task
       OR NEW.envelope ->> 'execution_id' IS DISTINCT FROM NEW.execution_id
       OR NEW.envelope -> 'args' IS DISTINCT FROM v_args
       OR NEW.envelope -> 'kwargs' IS DISTINCT FROM '{}'::jsonb
       OR NEW.envelope -> 'options' IS DISTINCT FROM pg_catalog.jsonb_build_object('queue',v_queue)
       OR (NEW.envelope - ARRAY['schema','operation','task_name','execution_id','args','kwargs','options']) <> '{}'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_dispatch_mismatch';
    END IF;
    IF TG_OP='INSERT' THEN
        SELECT run.state,root.state INTO v_state,v_root_state
        FROM yoao_fixture_v1.fixture_runs AS run
        JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=COALESCE(run.parent_run_id,run.id)
        WHERE run.id=v_binding.run_id FOR SHARE OF run,root;
        IF v_state IS DISTINCT FROM 'open' OR v_root_state IS DISTINCT FROM 'open' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_run_closed';
        END IF;
    ELSIF (NEW.intent_id,NEW.job_id,NEW.purpose,NEW.execution_id,NEW.envelope,NEW.natural_identity)
       IS DISTINCT FROM (OLD.intent_id,OLD.job_id,OLD.purpose,OLD.execution_id,OLD.envelope,OLD.natural_identity) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_dispatch_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_fixture_media_dispatch() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_media_dispatch_guard BEFORE INSERT OR UPDATE ON aap_jobs_v1.dispatch_intents
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1.guard_fixture_media_dispatch();
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.capture_fixture_media_dispatch()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    INSERT INTO aap_jobs_v1.fixture_media_dispatches(intent_id,job_id,run_id,dataset_id,envelope)
    SELECT NEW.intent_id,NEW.job_id,binding.run_id,binding.dataset_id,NEW.envelope
    FROM aap_jobs_v1.fixture_media_backfills AS binding WHERE binding.job_id=NEW.job_id;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.capture_fixture_media_dispatch() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_media_dispatch_capture AFTER INSERT ON aap_jobs_v1.dispatch_intents
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1.capture_fixture_media_dispatch();
