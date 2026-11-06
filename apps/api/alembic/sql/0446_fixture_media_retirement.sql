-- Only a registered per-run control LOGIN may retire a sealed dataset's real
-- execution and physical versions.  A lost control session stays unsettled.
CREATE TABLE aap_jobs_v1.fixture_media_retirement_sessions (
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    state text NOT NULL CHECK (state IN ('running','settled')),
    generation bigint NOT NULL CHECK (generation > 0),
    token uuid,
    backend_pid integer,
    PRIMARY KEY (run_id,dataset_id),
    CHECK ((state='running') = (token IS NOT NULL AND backend_pid IS NOT NULL))
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.fixture_media_retirement_sessions FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.guard_fixture_media_retirement_session()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' OR (NEW.run_id,NEW.dataset_id) IS DISTINCT FROM (OLD.run_id,OLD.dataset_id)
       OR NEW.generation < OLD.generation OR NEW.generation > OLD.generation + 1
       OR (NEW.generation=OLD.generation+1 AND NOT (OLD.state='settled' AND NEW.state='running'))
       OR (NEW.generation=OLD.generation AND NOT (OLD.state='running' AND NEW.state='settled')) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_retirement_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_fixture_media_retirement_session() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_media_retirement_session_immutable BEFORE UPDATE OR DELETE
ON aap_jobs_v1.fixture_media_retirement_sessions FOR EACH ROW
EXECUTE FUNCTION aap_jobs_v1.guard_fixture_media_retirement_session();
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.begin_fixture_media_retirement(p_run uuid,p_dataset uuid,p_token uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_binding aap_jobs_v1.fixture_media_backfills%ROWTYPE;
    v_ids text[]; v_not_sent boolean; v_expected text; v_root uuid;
BEGIN
    IF p_run IS NULL OR p_dataset IS NULL OR p_token IS NULL
       OR NOT aap_jobs_v1.fixture_media_session_owns('retirement',p_dataset)
       OR NOT EXISTS (
           SELECT 1 FROM aap_jobs_v1.fixture_media_executor_principals AS principal
           JOIN pg_catalog.pg_roles AS role ON role.oid=principal.principal_oid
           WHERE principal.run_id=p_run AND role.rolname=SESSION_USER
             AND principal.principal_name=role.rolname AND principal.state='active'
       ) THEN RETURN false; END IF;
    SELECT COALESCE(parent_run_id,id) INTO v_root FROM yoao_fixture_v1.fixture_runs WHERE id=p_run;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs
    WHERE id=v_root AND parent_run_id IS NULL AND state IN ('open','sealing') FOR SHARE;
    IF NOT FOUND THEN RETURN false; END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs AS run
    JOIN yoao_fixture_v1.fixture_run_resources AS resource ON resource.run_id=run.id
    WHERE run.id=p_run AND ((run.parent_run_id IS NOT NULL AND run.family='owned')
        OR (run.parent_run_id IS NULL AND run.family='screenshot'))
      AND run.state='sealing' AND resource.kind='dataset' AND resource.state='published'
      AND resource.resource_id=p_dataset FOR SHARE OF run,resource;
    IF NOT FOUND THEN RETURN false; END IF;
    IF EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_media_backfills
               WHERE run_id=p_run AND dataset_id=p_dataset AND state='running')
       OR EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_media_children
                  WHERE run_id=p_run AND dataset_id=p_dataset AND state='running') THEN
        RETURN false;
    END IF;
    IF aap_assets_v1.fixture_dataset_media_producers_quiescent(p_run,p_dataset) IS DISTINCT FROM true THEN
        RETURN false;
    END IF;
    IF EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_backfills AS binding
        LEFT JOIN public.async_jobs AS job ON job.id=binding.job_id
        WHERE binding.run_id=p_run AND binding.dataset_id=p_dataset AND binding.state <> 'retired'
          AND (job.id IS NULL OR job.kind IS DISTINCT FROM 'media_backfill'
            OR job.project_id IS NOT NULL OR job.user_id IS DISTINCT FROM binding.actor_id
            OR job.payload IS DISTINCT FROM binding.payload
            OR (binding.state='pending' AND (job.status IS DISTINCT FROM 'pending'
                                             OR job.started_at IS NOT NULL)))
    ) THEN RETURN false; END IF;
    IF EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_backfills AS binding
        WHERE binding.run_id=p_run AND binding.dataset_id=p_dataset
          AND (SELECT pg_catalog.count(*) FROM aap_jobs_v1.fixture_media_dispatches AS dispatch
               WHERE dispatch.job_id=binding.job_id AND dispatch.run_id=p_run
                 AND dispatch.dataset_id=p_dataset
                 AND dispatch.envelope ->> 'execution_id'=binding.job_id::text) <> 1
    ) OR EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_children AS child
        WHERE child.run_id=p_run AND child.dataset_id=p_dataset
          AND (SELECT pg_catalog.count(*) FROM aap_jobs_v1.fixture_media_dispatches AS dispatch
               WHERE dispatch.job_id=child.job_id AND dispatch.run_id=p_run
                 AND dispatch.dataset_id=p_dataset
                 AND dispatch.envelope ->> 'execution_id'=child.child_id::text) <> 1
    ) OR EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_dispatches AS dispatch
        JOIN aap_jobs_v1.fixture_media_backfills AS binding ON binding.job_id=dispatch.job_id
        WHERE binding.run_id=p_run AND binding.dataset_id=p_dataset
          AND (dispatch.run_id IS DISTINCT FROM p_run OR dispatch.dataset_id IS DISTINCT FROM p_dataset
            OR (dispatch.envelope ->> 'execution_id' <> binding.job_id::text AND NOT EXISTS (
                SELECT 1 FROM aap_jobs_v1.fixture_media_children AS child
                WHERE child.job_id=binding.job_id AND child.run_id=p_run AND child.dataset_id=p_dataset
                  AND child.child_id::text=dispatch.envelope ->> 'execution_id'
            )))
    ) THEN RETURN false; END IF;
    INSERT INTO aap_jobs_v1.fixture_media_retirement_sessions
        (run_id,dataset_id,state,generation,token,backend_pid)
    VALUES (p_run,p_dataset,'running',1,p_token,pg_catalog.pg_backend_pid())
    ON CONFLICT (run_id,dataset_id) DO UPDATE SET state='running',
        generation=aap_jobs_v1.fixture_media_retirement_sessions.generation+1,
        token=EXCLUDED.token,backend_pid=EXCLUDED.backend_pid
    WHERE aap_jobs_v1.fixture_media_retirement_sessions.state='settled';
    IF NOT FOUND THEN RETURN false; END IF;
    FOR v_binding IN SELECT * FROM aap_jobs_v1.fixture_media_backfills
        WHERE run_id=p_run AND dataset_id=p_dataset ORDER BY job_id FOR UPDATE
    LOOP
        v_ids := ARRAY(
            SELECT DISTINCT dispatch.envelope ->> 'execution_id'
            FROM aap_jobs_v1.fixture_media_dispatches AS dispatch
            WHERE dispatch.job_id=v_binding.job_id AND dispatch.run_id=p_run AND dispatch.dataset_id=p_dataset
            ORDER BY dispatch.envelope ->> 'execution_id'
        );
        IF aap_jobs_v1.retire_job_publications(v_binding.job_id,v_ids) IS DISTINCT FROM true THEN RETURN false; END IF;
        v_not_sent := aap_jobs_v1.job_publications_not_sent(v_binding.job_id,v_ids);
        IF aap_jobs_v1.job_publications_retired(v_binding.job_id,v_ids) IS DISTINCT FROM true THEN RETURN false; END IF;
        v_expected := CASE WHEN v_not_sent THEN 'not_sent' ELSE 'executed' END;
        IF (v_binding.state='pending' AND (NOT v_not_sent OR v_binding.generation<>0))
           OR (v_binding.state='settled' AND v_not_sent)
           OR (v_binding.state='retired' AND v_binding.retirement_outcome IS DISTINCT FROM v_expected)
           OR EXISTS (
               SELECT 1 FROM aap_jobs_v1.fixture_media_children AS child
               WHERE child.job_id=v_binding.job_id AND (
                   (child.state='pending' AND (NOT v_not_sent OR child.asset_id IS NOT NULL))
                   OR (child.state='settled' AND v_not_sent)
                   OR (child.state='retired' AND child.retirement_outcome IS DISTINCT FROM v_expected)
               )
           ) THEN RETURN false; END IF;
        UPDATE aap_jobs_v1.fixture_media_children SET state='retired',retirement_outcome=v_expected
        WHERE job_id=v_binding.job_id AND state IN ('pending','settled');
        UPDATE aap_jobs_v1.fixture_media_backfills SET state='retired',retirement_outcome=v_expected
        WHERE job_id=v_binding.job_id AND state IN ('pending','settled');
    END LOOP;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.begin_fixture_media_retirement(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.fixture_media_retirement_authorized(p_run uuid,p_dataset uuid,p_token uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT aap_jobs_v1.fixture_media_session_owns('retirement',p_dataset)
       AND EXISTS (
           SELECT 1 FROM aap_jobs_v1.fixture_media_retirement_sessions AS session
           JOIN aap_jobs_v1.fixture_media_executor_principals AS principal ON principal.run_id=session.run_id
           JOIN pg_catalog.pg_roles AS role ON role.oid=principal.principal_oid
           WHERE session.run_id=p_run AND session.dataset_id=p_dataset AND session.state='running'
             AND session.token=p_token AND session.backend_pid=pg_catalog.pg_backend_pid()
             AND role.rolname=SESSION_USER AND principal.principal_name=role.rolname
             AND principal.state='active'
       );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_media_retirement_authorized(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.fixture_media_retirement_started(p_run uuid,p_dataset uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_media_retirement_sessions
                   WHERE run_id=p_run AND dataset_id=p_dataset);
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_media_retirement_started(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.finish_fixture_media_retirement(p_run uuid,p_dataset uuid,p_token uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NOT aap_jobs_v1.fixture_media_retirement_authorized(p_run,p_dataset,p_token) THEN RETURN false; END IF;
    IF aap_assets_v1.fixture_media_retirement_session_clean(p_run,p_dataset,p_token)
       IS DISTINCT FROM true THEN RETURN false; END IF;
    UPDATE aap_jobs_v1.fixture_media_retirement_sessions
    SET state='settled',token=NULL,backend_pid=NULL
    WHERE run_id=p_run AND dataset_id=p_dataset AND state='running' AND token=p_token;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.finish_fixture_media_retirement(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.fixture_media_retirement_current(p_run uuid,p_dataset uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_retirement_sessions AS session
        WHERE session.run_id=p_run AND session.dataset_id=p_dataset
          AND session.state='running'
          AND aap_jobs_v1.fixture_media_retirement_authorized(p_run,p_dataset,session.token)
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_media_retirement_current(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
