-- A fixture-native lease is claimed only inside an exact private execution.
-- Session loss leaves its execution and claim evidence unsettled.
CREATE TABLE aap_assets_v1.fixture_native_media_executions (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    item_id uuid,
    family text NOT NULL CHECK (family IN ('image','media')),
    job_id uuid NOT NULL,
    backend_pid integer NOT NULL CHECK (backend_pid>0),
    state text NOT NULL CHECK (state IN ('running','settled'))
);
-- aap-statement-break
CREATE UNIQUE INDEX fixture_native_media_execution_live
ON aap_assets_v1.fixture_native_media_executions(family,job_id) WHERE state='running';
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_native_media_executions FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_native_media_execution()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' OR (NEW.id,NEW.run_id,NEW.dataset_id,NEW.item_id,NEW.family,NEW.job_id,NEW.backend_pid)
       IS DISTINCT FROM (OLD.id,OLD.run_id,OLD.dataset_id,OLD.item_id,OLD.family,OLD.job_id,OLD.backend_pid)
       OR OLD.state<>'running' OR NEW.state<>'settled' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_execution_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_media_execution() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_execution_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_native_media_executions FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_media_execution();
-- aap-statement-break
CREATE TABLE aap_assets_v1.fixture_native_media_claims (
    token uuid PRIMARY KEY,
    execution_id uuid NOT NULL,
    run_id uuid NOT NULL,
    family text NOT NULL CHECK (family IN ('image','media')),
    job_id uuid NOT NULL,
    business_pid integer NOT NULL CHECK (business_pid>0),
    state text NOT NULL CHECK (state IN ('admitted','claimed','unused')),
    job_generation bigint,
    lease_token uuid,
    CHECK ((state='claimed')=(job_generation IS NOT NULL AND lease_token IS NOT NULL)),
    CHECK (job_generation IS NULL OR job_generation>0)
);
-- aap-statement-break
CREATE UNIQUE INDEX fixture_native_media_claim_live
ON aap_assets_v1.fixture_native_media_claims(family,job_id) WHERE state='admitted';
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_native_media_claims FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_native_media_claim()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' OR (NEW.token,NEW.execution_id,NEW.run_id,NEW.family,NEW.job_id,NEW.business_pid)
       IS DISTINCT FROM (OLD.token,OLD.execution_id,OLD.run_id,OLD.family,OLD.job_id,OLD.business_pid)
       OR OLD.state<>'admitted' OR NEW.state NOT IN ('claimed','unused') THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_claim_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_media_claim() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_claim_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_native_media_claims FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_media_claim();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_native_execution_lock_held(p_pid integer,p_id uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_locks AS held
        CROSS JOIN LATERAL (
            SELECT pg_catalog.hashtextextended('aap-fixture-native-execution:' || p_id::text,0) AS key
        ) AS lock_key
        WHERE held.locktype='advisory' AND held.pid=p_pid AND held.mode='ExclusiveLock'
          AND held.granted AND held.objsubid=1
          AND held.classid::bigint=((lock_key.key >> 32) & 4294967295::bigint)
          AND held.objid::bigint=(lock_key.key & 4294967295::bigint)
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_native_execution_lock_held(integer,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_fixture_native_execution(p_family text,p_job uuid,p_id uuid)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_birth aap_assets_v1.fixture_native_media_jobs%ROWTYPE;
BEGIN
    IF p_family NOT IN ('image','media') OR p_job IS NULL OR p_id IS NULL
       OR aap_assets_v1.fixture_native_execution_lock_held(pg_catalog.pg_backend_pid(),p_id) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_execution_invalid';
    END IF;
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_native_media_jobs WHERE family=p_family AND job_id=p_job;
    IF NOT FOUND THEN RETURN 'ordinary'; END IF;
    IF aap_jobs_v1.fixture_media_principal_matches(v_birth.run_id) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_native_execution_principal_required';
    END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs AS run
    JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=COALESCE(run.parent_run_id,run.id)
    WHERE run.id=v_birth.run_id AND run.state='open' AND root.state='open' FOR SHARE OF run,root;
    IF NOT FOUND THEN RETURN 'blocked'; END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-fixture-native-job:' || p_family || ':' || p_job::text,0)
    );
    IF EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_media_executions
               WHERE family=p_family AND job_id=p_job AND state='running') THEN RETURN 'blocked'; END IF;
    INSERT INTO aap_assets_v1.fixture_native_media_executions
        (id,run_id,dataset_id,item_id,family,job_id,backend_pid,state)
    VALUES (p_id,v_birth.run_id,v_birth.dataset_id,v_birth.item_id,p_family,p_job,pg_catalog.pg_backend_pid(),'running');
    RETURN 'started';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_fixture_native_execution(text,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_fixture_native_claim(p_execution uuid,p_pid integer,p_token uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_execution aap_assets_v1.fixture_native_media_executions%ROWTYPE;
BEGIN
    SELECT * INTO v_execution FROM aap_assets_v1.fixture_native_media_executions
    WHERE id=p_execution AND state='running' AND backend_pid=pg_catalog.pg_backend_pid() FOR SHARE;
    IF NOT FOUND OR p_pid IS NULL OR p_pid<=0 OR p_token IS NULL
       OR aap_jobs_v1.fixture_media_principal_matches(v_execution.run_id) IS DISTINCT FROM true
       OR aap_assets_v1.fixture_native_execution_lock_held(v_execution.backend_pid,p_execution) IS DISTINCT FROM true
       OR aap_assets_v1.fixture_native_source_lock_held(p_pid,p_token) IS DISTINCT FROM true THEN RETURN false; END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs AS run
    JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=COALESCE(run.parent_run_id,run.id)
    WHERE run.id=v_execution.run_id AND run.state='open' AND root.state='open' FOR SHARE OF run,root;
    IF NOT FOUND THEN RETURN false; END IF;
    INSERT INTO aap_assets_v1.fixture_native_media_claims
        (token,execution_id,run_id,family,job_id,business_pid,state)
    VALUES (p_token,p_execution,v_execution.run_id,v_execution.family,v_execution.job_id,p_pid,'admitted');
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_fixture_native_claim(uuid,integer,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_native_claim_allowed(p_family text,p_job uuid,p_pid integer)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_media_jobs
                       WHERE family=p_family AND job_id=p_job)
      OR EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_media_claims AS claim
        JOIN aap_assets_v1.fixture_native_media_executions AS execution ON execution.id=claim.execution_id
        WHERE claim.family=p_family AND claim.job_id=p_job AND claim.business_pid=p_pid
          AND claim.state='admitted' AND execution.state='running'
          AND execution.run_id=claim.run_id AND execution.family=p_family AND execution.job_id=p_job
          AND aap_assets_v1.fixture_native_execution_lock_held(execution.backend_pid,execution.id)
          AND aap_assets_v1.fixture_native_source_lock_held(claim.business_pid,claim.token)
      );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_native_claim_allowed(text,uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.consume_fixture_native_claim(p_family text,p_job uuid,p_generation bigint,p_lease uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF p_generation IS NULL OR p_generation<1 OR p_lease IS NULL OR
       aap_assets_v1.fixture_native_claim_allowed(p_family,p_job,pg_catalog.pg_backend_pid()) IS DISTINCT FROM true THEN RETURN false; END IF;
    UPDATE aap_assets_v1.fixture_native_media_claims SET state='claimed',job_generation=p_generation,lease_token=p_lease
    WHERE family=p_family AND job_id=p_job AND business_pid=pg_catalog.pg_backend_pid() AND state='admitted';
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.consume_fixture_native_claim(text,uuid,bigint,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_fixture_native_claim(p_execution uuid,p_token uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_claim aap_assets_v1.fixture_native_media_claims%ROWTYPE;
        v_execution aap_assets_v1.fixture_native_media_executions%ROWTYPE;
BEGIN
    SELECT * INTO v_execution FROM aap_assets_v1.fixture_native_media_executions
    WHERE id=p_execution AND state='running' AND backend_pid=pg_catalog.pg_backend_pid() FOR SHARE;
    IF NOT FOUND OR aap_jobs_v1.fixture_media_principal_matches(v_execution.run_id) IS DISTINCT FROM true
       OR aap_assets_v1.fixture_native_execution_lock_held(v_execution.backend_pid,p_execution) IS DISTINCT FROM true THEN RETURN false; END IF;
    SELECT * INTO v_claim FROM aap_assets_v1.fixture_native_media_claims
    WHERE token=p_token AND execution_id=p_execution FOR UPDATE;
    IF NOT FOUND OR aap_assets_v1.fixture_native_source_lock_held(v_claim.business_pid,p_token) IS DISTINCT FROM true THEN RETURN false; END IF;
    IF v_claim.state IN ('claimed','unused') THEN RETURN true; END IF;
    UPDATE aap_assets_v1.fixture_native_media_claims SET state='unused' WHERE token=p_token AND state='admitted';
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_fixture_native_claim(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_fixture_native_execution(p_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_execution aap_assets_v1.fixture_native_media_executions%ROWTYPE;
BEGIN
    SELECT * INTO v_execution FROM aap_assets_v1.fixture_native_media_executions
    WHERE id=p_id AND state='running' AND backend_pid=pg_catalog.pg_backend_pid() FOR UPDATE;
    IF NOT FOUND OR aap_jobs_v1.fixture_media_principal_matches(v_execution.run_id) IS DISTINCT FROM true
       OR aap_assets_v1.fixture_native_execution_lock_held(v_execution.backend_pid,p_id) IS DISTINCT FROM true
       OR EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_scan_admissions
                  WHERE execution_id=p_id AND state='admitted')
       OR EXISTS (
           SELECT 1 FROM aap_assets_v1.fixture_native_media_claims AS claim
           JOIN aap_assets_v1.fixture_native_media_admissions AS admission
             ON admission.family=claim.family AND admission.job_id=claim.job_id
            AND admission.job_generation=claim.job_generation AND admission.lease_token=claim.lease_token
           WHERE claim.execution_id=p_id AND admission.state='admitted'
       )
       OR EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_media_claims WHERE execution_id=p_id AND state='admitted')
       OR EXISTS (
           SELECT 1 FROM aap_assets_v1.fixture_native_media_claims AS claim
           JOIN aap_assets_v1.fixture_native_media_sources AS source
             ON source.family=claim.family AND source.job_id=claim.job_id
            AND source.job_generation=claim.job_generation AND source.lease_token=claim.lease_token
           WHERE claim.execution_id=p_id AND source.state<>'settled'
       ) THEN RETURN false; END IF;
    UPDATE aap_assets_v1.fixture_native_media_executions SET state='settled' WHERE id=p_id AND state='running';
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_fixture_native_execution(uuid) FROM PUBLIC;
-- The run's private worker reads its own durable native queue, never another run's.
-- A running execution is excluded even when the business lease has expired.
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_native_job_candidates(p_family text)
RETURNS uuid[] LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_jobs uuid[];
BEGIN
    IF p_family NOT IN ('image','media') THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_family_invalid';
    END IF;
    SELECT COALESCE(pg_catalog.array_agg(candidate.job_id ORDER BY candidate.accepted_at,candidate.job_id),
                    ARRAY[]::uuid[]) INTO v_jobs
    FROM (
        SELECT birth.job_id,job.accepted_at
        FROM aap_assets_v1.fixture_native_media_jobs AS birth
        JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=birth.run_id
        JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=COALESCE(run.parent_run_id,run.id)
        JOIN LATERAL (
            SELECT image.accepted_at,image.status,image.lease_expires_at FROM public.asset_image_jobs AS image
            WHERE p_family='image' AND image.job_id=birth.job_id
            UNION ALL
            SELECT media.accepted_at,media.status,media.lease_expires_at FROM public.asset_media_jobs AS media
            WHERE p_family='media' AND media.job_id=birth.job_id
        ) AS job ON true
        WHERE birth.family=p_family AND aap_jobs_v1.fixture_media_principal_matches(birth.run_id)
          AND run.state='open' AND root.state='open'
          AND (job.status='pending' OR (job.status='running' AND job.lease_expires_at<=pg_catalog.clock_timestamp()))
          AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_media_executions AS execution
                          WHERE execution.family=p_family AND execution.job_id=birth.job_id AND execution.state='running')
        ORDER BY job.accepted_at,birth.job_id LIMIT 100
    ) AS candidate;
    RETURN v_jobs;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_native_job_candidates(text) FROM PUBLIC;
