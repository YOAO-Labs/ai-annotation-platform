-- Promote the existing execution owner without adopting historical ordinary Jobs.
ALTER TABLE yoao_fixture_v1.fixture_export_executions SET SCHEMA aap_export_v1;
-- aap-statement-break
ALTER TABLE aap_export_v1.fixture_export_executions RENAME TO executions;
-- aap-statement-break
ALTER TABLE aap_export_v1.executions ALTER COLUMN run_id DROP NOT NULL;
-- aap-statement-break
ALTER TABLE aap_export_v1.executions ADD COLUMN checkpoint text
    CHECK (checkpoint IS NULL OR checkpoint IN ('quiescent', 'entered', 'settled'));
-- aap-statement-break
ALTER TABLE aap_export_v1.executions ADD COLUMN business_backend_pid integer;
-- aap-statement-break
REVOKE ALL ON aap_export_v1.executions FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_export_v1.execution_deliveries (
    intent_id uuid PRIMARY KEY,
    job_id uuid NOT NULL,
    execution_id text NOT NULL,
    project_id uuid NOT NULL
);
-- aap-statement-break
REVOKE ALL ON aap_export_v1.execution_deliveries FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_export_v1.publications_retired(p_job uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT aap_jobs_v1.job_publications_retired(p_job, ARRAY(
        SELECT DISTINCT execution_id FROM aap_export_v1.execution_deliveries
        WHERE job_id = p_job ORDER BY execution_id
    ));
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_export_v1.publications_retired(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_export_v1.capture_execution_birth()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NEW.kind = 'export' AND NEW.project_id IS NOT NULL THEN
        INSERT INTO aap_export_v1.executions(job_id, project_id, state, checkpoint)
        VALUES (NEW.id, NEW.project_id, 'pending', 'quiescent');
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_export_v1.capture_execution_birth() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER aaa_export_execution_birth AFTER INSERT ON public.async_jobs
FOR EACH ROW EXECUTE FUNCTION aap_export_v1.capture_execution_birth();
-- aap-statement-break
CREATE FUNCTION aap_export_v1.capture_execution_delivery()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_project uuid;
BEGIN
    IF NEW.envelope ->> 'task_name' IS DISTINCT FROM 'app.workers.export.run_export' THEN
        RETURN NEW;
    END IF;
    SELECT project_id INTO v_project FROM aap_export_v1.executions
    WHERE job_id = NEW.job_id AND state <> 'retired' FOR UPDATE;
    IF NOT FOUND OR NEW.purpose <> 'execution'
       OR COALESCE(NEW.envelope #>> '{kwargs,project_id}', NEW.envelope #>> '{args,0}')
          IS DISTINCT FROM v_project::text
       OR COALESCE(NEW.envelope #>> '{kwargs,async_job_id}', NEW.envelope #>> '{args,4}')
          IS DISTINCT FROM NEW.job_id::text THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'export execution not admitted';
    END IF;
    INSERT INTO aap_export_v1.execution_deliveries(intent_id, job_id, execution_id, project_id)
    VALUES (NEW.intent_id, NEW.job_id, NEW.execution_id, v_project);
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_export_v1.capture_execution_delivery() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER capture_export_execution_delivery AFTER INSERT ON aap_jobs_v1.dispatch_intents
FOR EACH ROW EXECUTE FUNCTION aap_export_v1.capture_execution_delivery();
-- aap-statement-break
CREATE FUNCTION aap_export_v1.session_holds_lock(p_prefix text, p_job uuid, p_pid integer)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_locks AS held
        CROSS JOIN LATERAL (
            SELECT pg_catalog.hashtextextended(p_prefix || p_job::text, 0) AS key
        ) AS lock_key
        WHERE held.locktype = 'advisory' AND held.pid = p_pid
          AND held.mode = 'ExclusiveLock' AND held.granted AND held.objsubid = 1
          AND held.classid::bigint = ((lock_key.key >> 32) & 4294967295::bigint)
          AND held.objid::bigint = (lock_key.key & 4294967295::bigint)
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_export_v1.session_holds_lock(text,uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_export_v1.begin_execution(
    p_job uuid, p_project uuid, p_delivery text, p_token uuid, p_business_pid integer
) RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_execution aap_export_v1.executions%ROWTYPE; v_takeover boolean;
BEGIN
    IF p_job IS NULL OR p_project IS NULL OR p_delivery IS NULL OR p_token IS NULL
       OR p_business_pid IS NULL OR p_business_pid = pg_catalog.pg_backend_pid()
       OR NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_stat_activity AS activity
           WHERE activity.pid = p_business_pid AND activity.usename <> SESSION_USER
       )
       OR NOT aap_export_v1.session_holds_lock('aap-export:control:', p_job, pg_catalog.pg_backend_pid())
       OR NOT aap_export_v1.session_holds_lock('aap-export:job:', p_job, p_business_pid) THEN
        RETURN 'blocked';
    END IF;
    SELECT * INTO v_execution FROM aap_export_v1.executions WHERE job_id = p_job FOR UPDATE;
    IF NOT FOUND OR v_execution.project_id <> p_project OR v_execution.state = 'retired'
       OR v_execution.checkpoint IS NULL OR v_execution.checkpoint = 'entered'
       OR NOT EXISTS (SELECT 1 FROM aap_export_v1.execution_deliveries
                      WHERE job_id = p_job AND project_id = p_project AND execution_id = p_delivery) THEN
        RETURN 'blocked';
    END IF;
    v_takeover := v_execution.state = 'running';
    UPDATE aap_export_v1.executions
    SET state = 'running', generation = generation + 1, token = p_token,
        backend_pid = pg_catalog.pg_backend_pid(), business_backend_pid = p_business_pid,
        checkpoint = 'quiescent'
    WHERE job_id = p_job;
    RETURN (CASE WHEN v_execution.run_id IS NULL THEN '' ELSE 'fixture_' END)
        || (CASE WHEN v_takeover THEN 'takeover' ELSE 'started' END);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_export_v1.begin_execution(uuid,uuid,text,uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_export_v1.enter_execution_io(p_job uuid, p_token uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NOT aap_export_v1.session_holds_lock('aap-export:control:', p_job, pg_catalog.pg_backend_pid()) THEN
        RETURN false;
    END IF;
    UPDATE aap_export_v1.executions
    SET checkpoint = 'entered'
    WHERE job_id = p_job AND state = 'running' AND token = p_token
      AND backend_pid = pg_catalog.pg_backend_pid() AND checkpoint IN ('quiescent', 'entered')
      AND aap_export_v1.session_holds_lock('aap-export:job:', p_job, business_backend_pid);
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_export_v1.enter_execution_io(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_export_v1.finish_execution(p_job uuid, p_token uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NOT aap_export_v1.session_holds_lock('aap-export:control:', p_job, pg_catalog.pg_backend_pid()) THEN
        RETURN false;
    END IF;
    UPDATE aap_export_v1.executions
    SET state = 'settled', token = NULL, backend_pid = NULL,
        business_backend_pid = NULL, checkpoint = 'settled'
    WHERE job_id = p_job AND state = 'running' AND token = p_token
      AND backend_pid = pg_catalog.pg_backend_pid()
      AND aap_export_v1.session_holds_lock('aap-export:job:', p_job, business_backend_pid);
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_export_v1.finish_execution(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_export_v1.retire_execution(p_job uuid, p_project uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF p_job IS NULL OR p_project IS NULL
       OR NOT pg_catalog.pg_try_advisory_xact_lock(pg_catalog.hashtextextended('aap-export:control:' || p_job::text, 0))
       OR NOT pg_catalog.pg_try_advisory_xact_lock(pg_catalog.hashtextextended('aap-export:job:' || p_job::text, 0))
       OR NOT aap_export_v1.publications_retired(p_job) THEN
        RETURN false;
    END IF;
    UPDATE aap_export_v1.executions SET state = 'retired'
    WHERE job_id = p_job AND project_id = p_project AND run_id IS NULL
      AND state = 'settled' AND checkpoint = 'settled';
    IF FOUND THEN RETURN true; END IF;
    RETURN EXISTS (SELECT 1 FROM aap_export_v1.executions
                   WHERE job_id = p_job AND project_id = p_project AND run_id IS NULL
                     AND state = 'retired' AND checkpoint = 'settled');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_export_v1.retire_execution(uuid,uuid) FROM PUBLIC;
