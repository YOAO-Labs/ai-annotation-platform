-- Jobs owns physical publication separately from the durable dispatch lease.
-- An entered attempt survives its session and cannot be inferred settled from a timeout.
CREATE TABLE aap_jobs_v1.dispatch_publication_attempts (
    intent_id uuid NOT NULL,
    job_id uuid NOT NULL,
    execution_id text NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    lease_token uuid NOT NULL,
    backend_pid integer NOT NULL,
    state text NOT NULL CHECK (state IN ('entered', 'settled')),
    PRIMARY KEY (intent_id, generation)
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.dispatch_publication_attempts FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.guard_publication_attempt()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.state <> 'entered' OR NEW.state <> 'settled'
       OR (pg_catalog.to_jsonb(NEW) - 'state') IS DISTINCT FROM (pg_catalog.to_jsonb(OLD) - 'state') THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'publication attempt is immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_publication_attempt() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER guard_publication_attempt BEFORE UPDATE OR DELETE
ON aap_jobs_v1.dispatch_publication_attempts
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1.guard_publication_attempt();
-- aap-statement-break
CREATE TABLE aap_jobs_v1.dispatch_retry_publications (
    intent_id uuid NOT NULL,
    job_id uuid NOT NULL,
    execution_id text NOT NULL,
    retry_number integer NOT NULL CHECK (retry_number BETWEEN 1 AND 120),
    nonce uuid NOT NULL,
    backend_pid integer NOT NULL,
    state text NOT NULL CHECK (state IN ('entered', 'settled')),
    PRIMARY KEY (intent_id, retry_number, nonce)
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.dispatch_retry_publications FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER guard_retry_publication BEFORE UPDATE OR DELETE
ON aap_jobs_v1.dispatch_retry_publications
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1.guard_publication_attempt();
-- aap-statement-break
CREATE TABLE aap_jobs_v1.dispatch_publication_retirements (
    job_id uuid PRIMARY KEY,
    execution_ids text[] NOT NULL,
    retired_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.dispatch_publication_retirements FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.guard_publication_retirement()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'publication retirement is immutable';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_publication_retirement() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER guard_publication_retirement BEFORE UPDATE OR DELETE
ON aap_jobs_v1.dispatch_publication_retirements
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1.guard_publication_retirement();
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.job_publications_retired(p_job uuid, p_execution_ids text[])
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM aap_jobs_v1.dispatch_publication_retirements
        WHERE job_id = p_job AND execution_ids = p_execution_ids
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.job_publications_retired(uuid,text[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.publication_session_owns(p_intent uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_locks AS held
        CROSS JOIN LATERAL (
            SELECT pg_catalog.hashtextextended('aap-jobs:publication:' || p_intent::text, 0) AS key
        ) AS lock_key
        WHERE held.locktype = 'advisory' AND held.pid = pg_catalog.pg_backend_pid()
          AND held.mode = 'ExclusiveLock' AND held.granted AND held.objsubid = 1
          AND held.classid::bigint = ((lock_key.key >> 32) & 4294967295::bigint)
          AND held.objid::bigint = (lock_key.key & 4294967295::bigint)
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.publication_session_owns(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.begin_dispatch_publication(
    p_intent uuid, p_job uuid, p_execution text, p_generation bigint, p_token uuid
) RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_dispatch aap_jobs_v1.dispatch_intents%ROWTYPE;
BEGIN
    IF p_intent IS NULL OR p_job IS NULL OR p_execution IS NULL OR p_generation IS NULL
       OR p_token IS NULL OR NOT aap_jobs_v1.publication_session_owns(p_intent) THEN
        RETURN 'blocked';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-jobs:publication-job:' || p_job::text, 0)
    );
    IF EXISTS (SELECT 1 FROM aap_jobs_v1.dispatch_publication_retirements WHERE job_id = p_job) THEN
        RETURN 'blocked';
    END IF;
    SELECT * INTO v_dispatch FROM aap_jobs_v1.dispatch_intents
    WHERE intent_id = p_intent FOR UPDATE;
    IF NOT FOUND OR v_dispatch.job_id <> p_job OR v_dispatch.execution_id <> p_execution
       OR v_dispatch.state <> 'leased' OR v_dispatch.generation <> p_generation
       OR v_dispatch.lease_token IS DISTINCT FROM p_token THEN
        RETURN 'blocked';
    END IF;
    IF EXISTS (SELECT 1 FROM aap_jobs_v1.dispatch_publication_attempts
               WHERE intent_id = p_intent AND state = 'settled') THEN
        RETURN 'settled';
    END IF;
    INSERT INTO aap_jobs_v1.dispatch_publication_attempts
        (intent_id, job_id, execution_id, generation, lease_token, backend_pid, state)
    VALUES (p_intent, p_job, p_execution, p_generation, p_token, pg_catalog.pg_backend_pid(), 'entered')
    ON CONFLICT DO NOTHING;
    RETURN CASE WHEN FOUND THEN 'entered' ELSE 'blocked' END;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.begin_dispatch_publication(uuid,uuid,text,bigint,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.finish_dispatch_publication(
    p_intent uuid, p_job uuid, p_execution text, p_generation bigint, p_token uuid
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NOT aap_jobs_v1.publication_session_owns(p_intent) THEN RETURN false; END IF;
    UPDATE aap_jobs_v1.dispatch_publication_attempts SET state = 'settled'
    WHERE intent_id = p_intent AND job_id = p_job AND execution_id = p_execution
      AND generation = p_generation AND lease_token = p_token
      AND backend_pid = pg_catalog.pg_backend_pid() AND state = 'entered';
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.finish_dispatch_publication(uuid,uuid,text,bigint,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.retry_publication_intent(
    p_job uuid, p_execution text, p_task text, p_args jsonb, p_kwargs jsonb
) RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT CASE WHEN pg_catalog.count(*) = 1 THEN pg_catalog.min(intent_id::text)::uuid END
    FROM aap_jobs_v1.dispatch_intents
    WHERE job_id = p_job AND execution_id = p_execution
      AND envelope->>'task_name' = p_task
      AND envelope->'args' = p_args AND envelope->'kwargs' = p_kwargs;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.retry_publication_intent(uuid,text,text,jsonb,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.begin_retry_publication(
    p_intent uuid, p_job uuid, p_execution text, p_task text, p_args jsonb,
    p_kwargs jsonb, p_retry integer, p_nonce uuid
) RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF p_nonce IS NULL OR p_retry NOT BETWEEN 1 AND 120
       OR NOT aap_jobs_v1.publication_session_owns(p_intent) THEN RETURN 'blocked'; END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-jobs:publication-job:' || p_job::text, 0)
    );
    IF EXISTS (SELECT 1 FROM aap_jobs_v1.dispatch_publication_retirements WHERE job_id = p_job)
       OR aap_jobs_v1.retry_publication_intent(p_job, p_execution, p_task, p_args, p_kwargs)
          IS DISTINCT FROM p_intent THEN
        RETURN 'blocked';
    END IF;
    IF EXISTS (
        SELECT 1 FROM aap_jobs_v1.dispatch_retry_publications
        WHERE intent_id = p_intent AND retry_number = p_retry AND state = 'settled'
    ) THEN RETURN 'settled'; END IF;
    INSERT INTO aap_jobs_v1.dispatch_retry_publications
        (intent_id, job_id, execution_id, retry_number, nonce, backend_pid, state)
    VALUES (p_intent, p_job, p_execution, p_retry, p_nonce, pg_catalog.pg_backend_pid(), 'entered')
    ON CONFLICT DO NOTHING;
    RETURN CASE WHEN FOUND THEN 'entered' ELSE 'blocked' END;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.begin_retry_publication(uuid,uuid,text,text,jsonb,jsonb,integer,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.finish_retry_publication(
    p_intent uuid, p_job uuid, p_execution text, p_retry integer, p_nonce uuid
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NOT aap_jobs_v1.publication_session_owns(p_intent) THEN RETURN false; END IF;
    UPDATE aap_jobs_v1.dispatch_retry_publications SET state = 'settled'
    WHERE intent_id = p_intent AND job_id = p_job AND execution_id = p_execution
      AND retry_number = p_retry AND nonce = p_nonce
      AND backend_pid = pg_catalog.pg_backend_pid() AND state = 'entered';
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.finish_retry_publication(uuid,uuid,text,integer,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.guard_retired_dispatch()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-jobs:publication-job:' || NEW.job_id::text, 0)
    );
    IF EXISTS (SELECT 1 FROM aap_jobs_v1.dispatch_publication_retirements WHERE job_id = NEW.job_id) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'dispatch publication retired';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_retired_dispatch() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER guard_retired_dispatch BEFORE INSERT OR UPDATE ON aap_jobs_v1.dispatch_intents
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1.guard_retired_dispatch();
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.retire_job_publications(p_job uuid, p_execution_ids text[])
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_intent record;
BEGIN
    IF p_job IS NULL OR p_execution_ids IS NULL OR pg_catalog.cardinality(p_execution_ids) = 0
       OR pg_catalog.array_position(p_execution_ids, NULL::text) IS NOT NULL
       OR p_execution_ids IS DISTINCT FROM ARRAY(
           SELECT DISTINCT id FROM pg_catalog.unnest(p_execution_ids) AS ids(id) ORDER BY id
       ) THEN RETURN false; END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-jobs:publication-job:' || p_job::text, 0)
    );
    IF EXISTS (SELECT 1 FROM aap_jobs_v1.dispatch_publication_retirements WHERE job_id = p_job) THEN
        RETURN EXISTS (SELECT 1 FROM aap_jobs_v1.dispatch_publication_retirements
                       WHERE job_id = p_job AND execution_ids = p_execution_ids);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.async_jobs WHERE id = p_job)
       OR p_execution_ids IS DISTINCT FROM ARRAY(
           SELECT DISTINCT execution_id FROM aap_jobs_v1.dispatch_intents
           WHERE job_id = p_job ORDER BY execution_id
       ) OR EXISTS (
           SELECT 1 FROM aap_jobs_v1.dispatch_publication_attempts AS attempt
           WHERE attempt.job_id = p_job AND NOT EXISTS (
               SELECT 1 FROM aap_jobs_v1.dispatch_intents AS intent
               WHERE intent.intent_id = attempt.intent_id AND intent.job_id = attempt.job_id
                 AND intent.execution_id = attempt.execution_id
           )
       ) OR EXISTS (
           SELECT 1 FROM aap_jobs_v1.dispatch_retry_publications AS attempt
           WHERE attempt.job_id = p_job AND NOT EXISTS (
               SELECT 1 FROM aap_jobs_v1.dispatch_intents AS intent
               WHERE intent.intent_id = attempt.intent_id AND intent.job_id = attempt.job_id
                 AND intent.execution_id = attempt.execution_id
           )
       ) THEN
        RETURN false;
    END IF;
    FOR v_intent IN SELECT intent_id FROM aap_jobs_v1.dispatch_intents
                    WHERE job_id = p_job ORDER BY intent_id FOR UPDATE NOWAIT LOOP
        IF NOT pg_catalog.pg_try_advisory_xact_lock(
            pg_catalog.hashtextextended('aap-jobs:publication:' || v_intent.intent_id::text, 0)
        ) OR EXISTS (
            SELECT 1 FROM aap_jobs_v1.dispatch_publication_attempts
            WHERE intent_id = v_intent.intent_id AND state = 'entered'
        ) OR EXISTS (
            SELECT 1 FROM aap_jobs_v1.dispatch_retry_publications
            WHERE intent_id = v_intent.intent_id AND state = 'entered'
        ) OR NOT EXISTS (
            SELECT 1 FROM aap_jobs_v1.dispatch_publication_attempts
            WHERE intent_id = v_intent.intent_id AND state = 'settled'
        ) THEN RETURN false; END IF;
    END LOOP;
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE aap_jobs_v1.dispatch_intents SET state = 'retired', lease_token = NULL,
        lease_expires_at = NULL, next_attempt_at = NULL, accepted_at = NULL,
        updated_at = pg_catalog.clock_timestamp() WHERE job_id = p_job;
    INSERT INTO aap_jobs_v1.dispatch_publication_retirements(job_id, execution_ids)
    VALUES (p_job, p_execution_ids);
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.retire_job_publications(uuid,text[]) FROM PUBLIC;
