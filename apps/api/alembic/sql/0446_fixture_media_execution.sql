-- The trusted fixture coordinator registers the authenticated LOGIN itself.
-- No business API or ordinary worker can register an execution principal.
CREATE TABLE aap_jobs_v1.fixture_media_executor_principals (
    run_id uuid NOT NULL,
    principal_oid oid NOT NULL,
    principal_name name NOT NULL,
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','retired')),
    PRIMARY KEY (run_id, principal_oid),
    UNIQUE (run_id)
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.fixture_media_executor_principals FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.guard_fixture_media_executor_principal()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' OR (NEW.run_id,NEW.principal_oid,NEW.principal_name)
       IS DISTINCT FROM (OLD.run_id,OLD.principal_oid,OLD.principal_name)
       OR OLD.state <> 'active' OR NEW.state <> 'retired' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_principal_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_fixture_media_executor_principal() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_media_executor_principal_immutable
BEFORE UPDATE OR DELETE ON aap_jobs_v1.fixture_media_executor_principals
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1.guard_fixture_media_executor_principal();
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.register_fixture_media_executor(p_run_id uuid,p_role name)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_role record;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()),5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()),4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture executor requires a test database';
    END IF;
    IF NOT COALESCE((
        SELECT pg_catalog.pg_has_role(SESSION_USER,pg_catalog.pg_get_userbyid(c.relowner),'MEMBER')
        FROM pg_catalog.pg_class AS c
        WHERE c.oid=pg_catalog.to_regclass('public.fixture_export_zip_retirements')
    ),false) THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture executor requires private owner';
    END IF;
    SELECT role.oid,role.rolname,role.rolcanlogin,role.rolsuper,role.rolinherit,
        role.rolcreatedb,role.rolcreaterole,role.rolreplication,role.rolbypassrls
    INTO v_role FROM pg_catalog.pg_roles AS role WHERE role.rolname=p_role;
    IF NOT FOUND OR NOT v_role.rolcanlogin OR v_role.rolsuper OR v_role.rolinherit
       OR v_role.rolcreatedb OR v_role.rolcreaterole OR v_role.rolreplication OR v_role.rolbypassrls
       OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members WHERE member=v_role.oid)
       OR EXISTS (
           SELECT 1 FROM pg_catalog.pg_class AS relation
           JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=relation.relnamespace
           WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
             AND namespace.nspname NOT LIKE 'pg_toast%'
             AND namespace.nspname NOT LIKE 'pg_temp_%'
             AND relation.relkind IN ('r','p','v','m','f')
             AND (pg_catalog.has_table_privilege(v_role.oid,relation.oid,
                      'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                  OR pg_catalog.has_any_column_privilege(v_role.oid,relation.oid,
                      'SELECT,INSERT,UPDATE,REFERENCES'))
       )
       OR NOT EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_runs AS run
                      WHERE run.id=p_run_id AND run.state='open') THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_principal_invalid';
    END IF;
    INSERT INTO aap_jobs_v1.fixture_media_executor_principals(run_id,principal_oid,principal_name)
    VALUES (p_run_id,v_role.oid,v_role.rolname)
    ON CONFLICT DO NOTHING;
    IF NOT EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_executor_principals
        WHERE run_id=p_run_id AND principal_oid=v_role.oid
          AND principal_name=v_role.rolname AND state='active'
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_principal_changed';
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.register_fixture_media_executor(uuid,name) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.fixture_media_principal_matches(p_run uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_executor_principals AS principal
        JOIN pg_catalog.pg_roles AS role ON role.oid=principal.principal_oid
        WHERE principal.run_id=p_run AND principal.state='active'
          AND role.rolname=SESSION_USER AND principal.principal_name=role.rolname
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_media_principal_matches(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.fixture_media_session_owns(p_kind text,p_id uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_locks AS held
        CROSS JOIN LATERAL (
            SELECT pg_catalog.hashtextextended('aap-fixture-media:' || p_kind || ':' || p_id::text,0) AS key
        ) AS lock_key
        WHERE held.locktype='advisory' AND held.pid=pg_catalog.pg_backend_pid()
          AND held.mode='ExclusiveLock' AND held.granted AND held.objsubid=1
          AND held.classid::bigint=((lock_key.key >> 32) & 4294967295::bigint)
          AND held.objid::bigint=(lock_key.key & 4294967295::bigint)
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_media_session_owns(text,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.begin_fixture_media_backfill(
    p_job_id uuid,p_task_id text,p_queue text,p_token uuid
) RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_binding aap_jobs_v1.fixture_media_backfills%ROWTYPE;
    v_run record;
    v_job record;
BEGIN
    IF p_job_id IS NULL OR p_token IS NULL
       OR NOT aap_jobs_v1.fixture_media_session_owns('backfill',p_job_id) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_lock_required';
    END IF;
    SELECT run.state,root.state AS root_state INTO v_run
    FROM aap_jobs_v1.fixture_media_backfills AS binding
    JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=binding.run_id
    JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=COALESCE(run.parent_run_id,run.id)
    WHERE binding.job_id=p_job_id FOR SHARE OF run,root;
    IF NOT FOUND THEN RETURN 'blocked'; END IF;
    SELECT * INTO v_binding FROM aap_jobs_v1.fixture_media_backfills
    WHERE job_id=p_job_id FOR UPDATE;
    IF NOT EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_executor_principals AS principal
        JOIN pg_catalog.pg_roles AS role ON role.oid=principal.principal_oid
        WHERE principal.run_id=v_binding.run_id AND role.rolname=SESSION_USER
          AND principal.principal_name=role.rolname AND principal.state='active'
    ) THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_media_principal_required';
    END IF;
    IF v_run.state <> 'open' OR v_run.root_state <> 'open'
       OR v_binding.state NOT IN ('pending','settled')
       OR p_task_id IS DISTINCT FROM p_job_id::text
       OR p_queue IS DISTINCT FROM v_binding.queue THEN RETURN 'blocked'; END IF;
    SELECT job.kind,job.status,job.project_id,job.user_id,job.payload INTO v_job
    FROM public.async_jobs AS job WHERE job.id=p_job_id FOR SHARE;
    IF NOT FOUND OR v_job.kind <> 'media_backfill' OR v_job.project_id IS NOT NULL
       OR v_job.user_id IS DISTINCT FROM v_binding.actor_id
       OR v_job.payload IS DISTINCT FROM v_binding.payload THEN RETURN 'blocked'; END IF;
    IF v_job.status NOT IN ('pending','running') THEN RETURN 'settled'; END IF;
    IF (SELECT pg_catalog.count(*) FROM aap_jobs_v1.fixture_media_dispatches AS dispatch
        JOIN aap_jobs_v1.dispatch_intents AS intent ON intent.intent_id=dispatch.intent_id
        WHERE dispatch.job_id=p_job_id AND dispatch.run_id=v_binding.run_id
          AND dispatch.dataset_id=v_binding.dataset_id AND intent.job_id=p_job_id
          AND intent.execution_id=p_job_id::text
          AND dispatch.envelope=intent.envelope) <> 1 THEN RETURN 'blocked'; END IF;
    UPDATE aap_jobs_v1.fixture_media_backfills
    SET state='running',generation=generation+1,token=p_token,
        backend_pid=pg_catalog.pg_backend_pid() WHERE job_id=p_job_id;
    RETURN 'started';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.begin_fixture_media_backfill(uuid,text,text,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.finish_fixture_media_backfill(p_job_id uuid,p_token uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NOT aap_jobs_v1.fixture_media_session_owns('backfill',p_job_id) THEN RETURN false; END IF;
    UPDATE aap_jobs_v1.fixture_media_backfills AS binding
    SET state='settled',token=NULL,backend_pid=NULL
    WHERE binding.job_id=p_job_id AND binding.state='running' AND binding.token=p_token
      AND binding.backend_pid=pg_catalog.pg_backend_pid()
      AND EXISTS (
          SELECT 1 FROM aap_jobs_v1.fixture_media_executor_principals AS principal
          JOIN pg_catalog.pg_roles AS role ON role.oid=principal.principal_oid
          WHERE principal.run_id=binding.run_id AND role.rolname=SESSION_USER
            AND principal.principal_name=role.rolname AND principal.state='active'
      );
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE aap_jobs_v1.fixture_media_video_attempts
    SET state='settled' WHERE job_id=p_job_id AND state='running';
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.finish_fixture_media_backfill(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.fixture_media_session_authorized(
    p_kind text,p_id uuid,p_token uuid,p_run_id uuid,p_dataset_id uuid,p_item_id uuid
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NOT aap_jobs_v1.fixture_media_session_owns(p_kind,p_id) OR NOT EXISTS (
        SELECT 1 FROM aap_jobs_v1.fixture_media_executor_principals AS principal
        JOIN pg_catalog.pg_roles AS role ON role.oid=principal.principal_oid
        WHERE principal.run_id=p_run_id AND role.rolname=SESSION_USER
          AND principal.principal_name=role.rolname AND principal.state='active'
    ) THEN RETURN false; END IF;
    IF p_kind='backfill' THEN
        RETURN EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_media_backfills AS binding
            WHERE binding.job_id=p_id AND binding.run_id=p_run_id
              AND binding.dataset_id=p_dataset_id
              AND binding.state='running' AND binding.token=p_token
              AND binding.backend_pid=pg_catalog.pg_backend_pid()
              AND aap_assets_v1.fixture_media_item_in_cutoff(
                  p_run_id,p_dataset_id,p_item_id,
                  (binding.payload ->> 'accepted_before')::timestamptz
              ) IS TRUE);
    ELSIF p_kind='pyramid' THEN
        RETURN EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_media_children AS child
            WHERE child.child_id=p_id AND child.run_id=p_run_id
              AND child.dataset_id=p_dataset_id AND child.item_id=p_item_id
              AND child.state='running' AND child.token=p_token
              AND child.backend_pid=pg_catalog.pg_backend_pid());
    END IF;
    RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_media_session_authorized(text,uuid,uuid,uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.fixture_media_execution_run(p_kind text,p_id uuid,p_token uuid)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_run uuid;
BEGIN
    IF NOT aap_jobs_v1.fixture_media_session_owns(p_kind,p_id) THEN RETURN NULL; END IF;
    IF p_kind='backfill' THEN
        SELECT run_id INTO v_run FROM aap_jobs_v1.fixture_media_backfills
        WHERE job_id=p_id AND state='running' AND token=p_token
          AND backend_pid=pg_catalog.pg_backend_pid();
    ELSIF p_kind='pyramid' THEN
        SELECT run_id INTO v_run FROM aap_jobs_v1.fixture_media_children
        WHERE child_id=p_id AND state='running' AND token=p_token
          AND backend_pid=pg_catalog.pg_backend_pid();
    ELSE
        RETURN NULL;
    END IF;
    IF NOT aap_jobs_v1.fixture_media_principal_matches(v_run) THEN RETURN NULL; END IF;
    RETURN v_run;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_media_execution_run(text,uuid,uuid) FROM PUBLIC;
