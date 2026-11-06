-- A native fixture source can be admitted only by a worker whose separate
-- run-scoped control LOGIN bound this exact native lease and DB session.
CREATE TABLE aap_assets_v1.fixture_native_media_admissions (
    token uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    item_id uuid NOT NULL,
    family text NOT NULL CHECK (family IN ('image','media')),
    job_id uuid NOT NULL,
    job_generation bigint NOT NULL CHECK (job_generation > 0),
    lease_token uuid NOT NULL,
    business_pid integer NOT NULL CHECK (business_pid > 0),
    state text NOT NULL CHECK (state IN ('admitted','consumed','unused'))
);
-- aap-statement-break
CREATE UNIQUE INDEX fixture_native_media_admission_active
ON aap_assets_v1.fixture_native_media_admissions(family,job_id,job_generation,lease_token)
WHERE state='admitted';
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_native_media_admissions FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_native_media_admission()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' OR (NEW.token,NEW.run_id,NEW.dataset_id,NEW.item_id,NEW.family,
        NEW.job_id,NEW.job_generation,NEW.lease_token,NEW.business_pid)
       IS DISTINCT FROM (OLD.token,OLD.run_id,OLD.dataset_id,OLD.item_id,OLD.family,
        OLD.job_id,OLD.job_generation,OLD.lease_token,OLD.business_pid)
       OR OLD.state<>'admitted' OR NEW.state NOT IN ('consumed','unused') THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_admission_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_media_admission() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_media_admission_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_native_media_admissions FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_media_admission();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_native_source_lock_held(p_pid integer,p_token uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_locks AS held
        CROSS JOIN LATERAL (
            SELECT pg_catalog.hashtextextended('aap-fixture-native-source:' || p_token::text,0) AS key
        ) AS lock_key
        WHERE held.locktype='advisory' AND held.pid=p_pid AND held.mode='ShareLock'
          AND held.granted AND held.objsubid=1
          AND held.classid::bigint=((lock_key.key >> 32) & 4294967295::bigint)
          AND held.objid::bigint=(lock_key.key & 4294967295::bigint)
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_native_source_lock_held(integer,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_fixture_native_source_admission(
    p_family text,p_job uuid,p_generation bigint,p_lease uuid,p_pid integer,p_token uuid
) RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_birth aap_assets_v1.fixture_native_media_jobs%ROWTYPE; v_job record;
BEGIN
    IF p_family NOT IN ('image','media') OR p_job IS NULL OR p_generation IS NULL
       OR p_generation<1 OR p_lease IS NULL OR p_pid IS NULL OR p_pid<=0 OR p_token IS NULL
       OR aap_assets_v1.fixture_native_source_lock_held(p_pid,p_token) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_admission_invalid';
    END IF;
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_native_media_jobs
    WHERE family=p_family AND job_id=p_job;
    IF NOT FOUND THEN RETURN 'ordinary'; END IF;
    IF aap_jobs_v1.fixture_media_principal_matches(v_birth.run_id) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_native_cache_principal_required';
    END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs AS run
    JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=COALESCE(run.parent_run_id,run.id)
    WHERE run.id=v_birth.run_id AND run.state='open' AND root.state='open'
    FOR SHARE OF run,root;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_run_closed';
    END IF;
    IF p_family='image' THEN
        SELECT status,generation,lease_token,lease_expires_at INTO v_job
        FROM public.asset_image_jobs WHERE job_id=p_job FOR SHARE;
    ELSE
        SELECT status,generation,lease_token,lease_expires_at INTO v_job
        FROM public.asset_media_jobs WHERE job_id=p_job FOR SHARE;
    END IF;
    IF NOT FOUND OR v_job.status<>'running' OR v_job.generation<>p_generation
       OR v_job.lease_token IS DISTINCT FROM p_lease OR v_job.lease_expires_at IS NULL
       OR v_job.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_cache_lease_changed';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_media_claims AS claim
        JOIN aap_assets_v1.fixture_native_media_executions AS execution ON execution.id=claim.execution_id
        WHERE claim.run_id=v_birth.run_id AND claim.family=p_family AND claim.job_id=p_job
          AND claim.job_generation=p_generation AND claim.lease_token=p_lease AND claim.state='claimed'
          AND execution.state='running' AND execution.run_id=v_birth.run_id
          AND aap_assets_v1.fixture_native_execution_lock_held(execution.backend_pid,execution.id)
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_execution_untracked';
    END IF;
    INSERT INTO aap_assets_v1.fixture_native_media_admissions
        (token,run_id,dataset_id,item_id,family,job_id,job_generation,lease_token,business_pid,state)
    VALUES (p_token,v_birth.run_id,v_birth.dataset_id,v_birth.item_id,p_family,p_job,
        p_generation,p_lease,p_pid,'admitted');
    RETURN 'admitted';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_fixture_native_source_admission(text,uuid,bigint,uuid,integer,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_fixture_native_source_admission(p_token uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_admission aap_assets_v1.fixture_native_media_admissions%ROWTYPE;
BEGIN
    SELECT * INTO v_admission FROM aap_assets_v1.fixture_native_media_admissions
    WHERE token=p_token FOR UPDATE;
    IF NOT FOUND OR aap_jobs_v1.fixture_media_principal_matches(v_admission.run_id) IS DISTINCT FROM true
       OR aap_assets_v1.fixture_native_source_lock_held(v_admission.business_pid,p_token) IS DISTINCT FROM true THEN
        RETURN false;
    END IF;
    IF v_admission.state='consumed' OR v_admission.state='unused' THEN RETURN true; END IF;
    UPDATE aap_assets_v1.fixture_native_media_admissions SET state='unused'
    WHERE token=p_token AND state='admitted';
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_fixture_native_source_admission(uuid) FROM PUBLIC;
