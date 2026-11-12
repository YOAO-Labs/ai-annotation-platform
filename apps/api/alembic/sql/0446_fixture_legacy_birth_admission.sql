-- The private executor binds the real business connection before output birth;
-- losing its commit or release leaves durable unsettled evidence.
CREATE TABLE aap_assets_v1.fixture_legacy_birth_admissions (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    dataset_id uuid,
    item_id uuid,
    task_id uuid,
    execution_kind text NOT NULL CHECK (execution_kind IN ('backfill','pyramid','independent')),
    execution_id uuid NOT NULL,
    execution_token uuid NOT NULL,
    delivery_id uuid,
    business_pid integer NOT NULL CHECK (business_pid>0),
    state text NOT NULL CHECK (state IN ('admitted','settled'))
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_legacy_birth_admissions FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_legacy_birth_admission()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' OR (NEW.id,NEW.run_id,NEW.dataset_id,NEW.item_id,NEW.task_id,
        NEW.execution_kind,NEW.execution_id,NEW.execution_token,NEW.business_pid,NEW.delivery_id)
       IS DISTINCT FROM (OLD.id,OLD.run_id,OLD.dataset_id,OLD.item_id,OLD.task_id,
        OLD.execution_kind,OLD.execution_id,OLD.execution_token,OLD.business_pid,OLD.delivery_id)
       OR OLD.state<>'admitted' OR NEW.state<>'settled' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_legacy_birth_admission_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_legacy_birth_admission() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_legacy_birth_admission_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_legacy_birth_admissions FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_legacy_birth_admission();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_fixture_legacy_birth_admission(
    p_kind text,p_execution uuid,p_token uuid,p_dataset uuid,p_item uuid,p_task uuid,p_delivery uuid,p_pid integer,p_admission uuid
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_run uuid;
BEGIN
    IF p_kind NOT IN ('backfill','pyramid','independent') OR p_execution IS NULL OR p_token IS NULL
       OR p_pid IS NULL OR p_pid<=0 OR p_admission IS NULL
       OR aap_assets_v1.fixture_native_source_lock_held(p_pid,p_admission) IS DISTINCT FROM true THEN RETURN false; END IF;
    IF p_kind='independent' THEN
        SELECT run_id INTO v_run FROM aap_assets_v1.fixture_independent_media_births WHERE delivery_id=p_execution;
        IF NOT FOUND OR aap_assets_v1.fixture_independent_session_authorized(
            p_execution,p_token,v_run,p_dataset,p_item,p_task) IS DISTINCT FROM true THEN RETURN false; END IF;
    ELSE
        v_run := aap_jobs_v1.fixture_media_execution_run(p_kind,p_execution,p_token);
        IF v_run IS NULL OR p_task IS NOT NULL OR aap_jobs_v1.fixture_media_session_authorized(
            p_kind,p_execution,p_token,v_run,p_dataset,p_item) IS DISTINCT FROM true THEN RETURN false; END IF;
    END IF;
    INSERT INTO aap_assets_v1.fixture_legacy_birth_admissions(
        id,run_id,dataset_id,item_id,task_id,execution_kind,execution_id,execution_token,delivery_id,business_pid,state)
    VALUES(p_admission,v_run,p_dataset,p_item,p_task,p_kind,p_execution,p_token,p_delivery,p_pid,'admitted');
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_fixture_legacy_birth_admission(text,uuid,uuid,uuid,uuid,uuid,uuid,integer,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_fixture_legacy_birth_admission(p_admission uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_admission aap_assets_v1.fixture_legacy_birth_admissions%ROWTYPE;
BEGIN
    SELECT * INTO v_admission FROM aap_assets_v1.fixture_legacy_birth_admissions WHERE id=p_admission FOR UPDATE;
    IF NOT FOUND OR aap_assets_v1.fixture_native_source_lock_held(v_admission.business_pid,p_admission) IS DISTINCT FROM true
       OR (CASE WHEN v_admission.execution_kind='independent' THEN
            aap_assets_v1.fixture_independent_session_authorized(v_admission.execution_id,v_admission.execution_token,
                v_admission.run_id,v_admission.dataset_id,v_admission.item_id,v_admission.task_id)
          ELSE aap_jobs_v1.fixture_media_session_authorized(v_admission.execution_kind,v_admission.execution_id,
                v_admission.execution_token,v_admission.run_id,v_admission.dataset_id,v_admission.item_id) END)
          IS DISTINCT FROM true THEN RETURN false; END IF;
    IF v_admission.state='settled' THEN RETURN true; END IF;
    IF v_admission.delivery_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_independent_media_births AS birth
        WHERE birth.delivery_id=v_admission.delivery_id AND birth.run_id=v_admission.run_id
          AND birth.dataset_id IS NOT DISTINCT FROM v_admission.dataset_id
          AND birth.item_id IS NOT DISTINCT FROM v_admission.item_id
    ) THEN RETURN false; END IF;
    UPDATE aap_assets_v1.fixture_legacy_birth_admissions SET state='settled' WHERE id=p_admission AND state='admitted';
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_fixture_legacy_birth_admission(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_legacy_birth_admission_current(p_run uuid,p_dataset uuid,p_item uuid,p_task uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_legacy_birth_admissions AS admission
        WHERE admission.run_id=p_run AND admission.dataset_id IS NOT DISTINCT FROM p_dataset
          AND admission.item_id IS NOT DISTINCT FROM p_item AND admission.task_id IS NOT DISTINCT FROM p_task
          AND admission.state='admitted' AND admission.business_pid=pg_catalog.pg_backend_pid()
          AND aap_assets_v1.fixture_native_source_lock_held(admission.business_pid,admission.id));
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_legacy_birth_admission_current(uuid,uuid,uuid,uuid) FROM PUBLIC;
