-- A private scanner binds each real database transaction to the native job's
-- execution, lease and Dataset.  Failed or ambiguous transactions stay admitted.
CREATE TABLE aap_assets_v1.fixture_native_scan_admissions (
    token uuid PRIMARY KEY,
    execution_id uuid NOT NULL,
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    family text NOT NULL CHECK (family IN ('image','media')),
    job_id uuid NOT NULL,
    generation bigint NOT NULL CHECK (generation>0),
    lease_token uuid NOT NULL,
    business_pid integer NOT NULL CHECK (business_pid>0),
    state text NOT NULL CHECK (state IN ('admitted','settled'))
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_native_scan_admissions FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_native_scan_admission()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' OR (NEW.token,NEW.execution_id,NEW.run_id,NEW.dataset_id,NEW.family,
       NEW.job_id,NEW.generation,NEW.lease_token,NEW.business_pid) IS DISTINCT FROM
       (OLD.token,OLD.execution_id,OLD.run_id,OLD.dataset_id,OLD.family,
        OLD.job_id,OLD.generation,OLD.lease_token,OLD.business_pid)
       OR OLD.state<>'admitted' OR NEW.state<>'settled' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_scan_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_scan_admission() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_scan_admission_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_native_scan_admissions FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_scan_admission();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_fixture_native_scan(
    p_execution uuid,p_job uuid,p_generation bigint,p_lease uuid,p_pid integer,p_token uuid
) RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_execution aap_assets_v1.fixture_native_media_executions%ROWTYPE;
        v_run_state text; v_root_state text;
BEGIN
    SELECT * INTO v_execution FROM aap_assets_v1.fixture_native_media_executions
    WHERE id=p_execution AND job_id=p_job AND item_id IS NULL AND state='running'
      AND backend_pid=pg_catalog.pg_backend_pid() FOR SHARE;
    IF NOT FOUND OR p_generation IS NULL OR p_generation<1 OR p_lease IS NULL OR p_pid IS NULL OR p_pid<=0
       OR p_token IS NULL OR aap_jobs_v1.fixture_media_principal_matches(v_execution.run_id) IS DISTINCT FROM true
       OR aap_assets_v1.fixture_native_execution_lock_held(v_execution.backend_pid,p_execution) IS DISTINCT FROM true
       OR aap_assets_v1.fixture_native_source_lock_held(p_pid,p_token) IS DISTINCT FROM true
       OR NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_media_claims AS claim
           WHERE claim.execution_id=p_execution AND claim.family=v_execution.family AND claim.job_id=p_job
             AND claim.state='claimed' AND claim.job_generation=p_generation AND claim.lease_token=p_lease)
       THEN RETURN 'denied'; END IF;
    SELECT run.state,root.state INTO v_run_state,v_root_state
    FROM yoao_fixture_v1.fixture_runs AS run
    JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=COALESCE(run.parent_run_id,run.id)
    WHERE run.id=v_execution.run_id FOR SHARE OF run,root;
    IF NOT FOUND OR v_run_state NOT IN ('open','sealing') OR v_root_state NOT IN ('open','sealing')
       THEN RETURN 'denied'; END IF;
    IF v_run_state='sealing' OR v_root_state='sealing' THEN RETURN 'sealed'; END IF;
    INSERT INTO aap_assets_v1.fixture_native_scan_admissions
        (token,execution_id,run_id,dataset_id,family,job_id,generation,lease_token,business_pid,state)
    VALUES (p_token,p_execution,v_execution.run_id,v_execution.dataset_id,v_execution.family,
        p_job,p_generation,p_lease,p_pid,'admitted');
    RETURN 'started';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_fixture_native_scan(uuid,uuid,bigint,uuid,integer,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_fixture_native_scan(p_execution uuid,p_token uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_scan aap_assets_v1.fixture_native_scan_admissions%ROWTYPE;
        v_execution aap_assets_v1.fixture_native_media_executions%ROWTYPE;
BEGIN
    SELECT * INTO v_execution FROM aap_assets_v1.fixture_native_media_executions
    WHERE id=p_execution AND state='running' AND backend_pid=pg_catalog.pg_backend_pid() FOR SHARE;
    IF NOT FOUND OR aap_jobs_v1.fixture_media_principal_matches(v_execution.run_id) IS DISTINCT FROM true
       OR aap_assets_v1.fixture_native_execution_lock_held(v_execution.backend_pid,p_execution) IS DISTINCT FROM true THEN RETURN false; END IF;
    SELECT * INTO v_scan FROM aap_assets_v1.fixture_native_scan_admissions
    WHERE token=p_token AND execution_id=p_execution FOR UPDATE;
    IF NOT FOUND OR v_scan.run_id IS DISTINCT FROM v_execution.run_id
       OR aap_assets_v1.fixture_native_source_lock_held(v_scan.business_pid,p_token) IS DISTINCT FROM true THEN RETURN false; END IF;
    IF v_scan.state='settled' THEN RETURN true; END IF;
    UPDATE aap_assets_v1.fixture_native_scan_admissions SET state='settled' WHERE token=p_token AND state='admitted';
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_fixture_native_scan(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_native_scan_visible(p_dataset uuid,p_pid integer)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_run uuid; v_root uuid; v_run_state text; v_root_state text;
BEGIN
    SELECT resource.run_id INTO v_run FROM yoao_fixture_v1.fixture_run_resources AS resource
    WHERE resource.kind='dataset' AND resource.resource_id=p_dataset;
    IF NOT FOUND THEN RETURN true; END IF;
    IF NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_scan_admissions AS admission
        JOIN aap_assets_v1.fixture_native_media_executions AS execution ON execution.id=admission.execution_id
        WHERE admission.dataset_id=p_dataset AND admission.run_id=v_run
          AND admission.business_pid=p_pid AND admission.state='admitted'
          AND execution.state='running' AND execution.run_id=v_run AND execution.dataset_id=p_dataset
          AND aap_assets_v1.fixture_native_execution_lock_held(execution.backend_pid,execution.id)
          AND aap_assets_v1.fixture_native_source_lock_held(admission.business_pid,admission.token)
    ) THEN RETURN false; END IF;
    SELECT COALESCE(parent_run_id,id) INTO v_root FROM yoao_fixture_v1.fixture_runs WHERE id=v_run;
    SELECT state INTO v_root_state FROM yoao_fixture_v1.fixture_runs
    WHERE id=v_root AND parent_run_id IS NULL FOR SHARE;
    SELECT state INTO v_run_state FROM yoao_fixture_v1.fixture_runs
    WHERE id=v_run AND COALESCE(parent_run_id,id)=v_root FOR SHARE;
    IF v_run_state IS DISTINCT FROM 'open' OR v_root_state IS DISTINCT FROM 'open' THEN
        RAISE EXCEPTION USING ERRCODE='APF01', MESSAGE='fixture_native_scan_sealed';
    END IF;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_native_scan_visible(uuid,integer) FROM PUBLIC;
-- Standalone legacy Tasks have no Dataset scope.  Ordinary project/global
-- scanners cannot adopt a fixture-owned Task into unrelated work.
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_legacy_task_scan_visible(p_task uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT NOT EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
                       WHERE resource.kind='task' AND resource.resource_id=p_task);
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_legacy_task_scan_visible(uuid) FROM PUBLIC;
