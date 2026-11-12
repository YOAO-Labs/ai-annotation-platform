-- A mixed job is published, never an all-not-sent retirement.
CREATE OR REPLACE FUNCTION aap_jobs_v1.retire_job_publications(p_job uuid, p_execution_ids text[])
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_intent record;
    v_published boolean := false;
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
       OR NOT EXISTS (SELECT 1 FROM aap_jobs_v1.publication_job_births WHERE job_id = p_job)
       OR p_execution_ids IS DISTINCT FROM ARRAY(
           SELECT DISTINCT execution_id FROM aap_jobs_v1.dispatch_intents
           WHERE job_id = p_job ORDER BY execution_id
       ) OR EXISTS (
           SELECT 1 FROM aap_jobs_v1.dispatch_intents AS intent
           WHERE intent.job_id = p_job AND NOT EXISTS (
               SELECT 1 FROM aap_jobs_v1.publication_intent_births AS birth
               WHERE birth.intent_id = intent.intent_id AND birth.job_id = intent.job_id
                 AND birth.execution_id = intent.execution_id AND birth.envelope = intent.envelope
           )
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
        ) THEN RETURN false; END IF;
        IF EXISTS (
            SELECT 1 FROM aap_jobs_v1.dispatch_publication_attempts
            WHERE intent_id = v_intent.intent_id AND state = 'settled'
        ) THEN
            v_published := true;
        ELSIF EXISTS (
            SELECT 1 FROM aap_jobs_v1.dispatch_publication_attempts
            WHERE intent_id = v_intent.intent_id AND state <> 'not_sent'
        ) OR EXISTS (
            SELECT 1 FROM aap_jobs_v1.dispatch_retry_publications
            WHERE intent_id = v_intent.intent_id
        ) THEN
            RETURN false;
        END IF;
    END LOOP;
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE aap_jobs_v1.dispatch_intents SET state = 'retired', lease_token = NULL,
        lease_expires_at = NULL, next_attempt_at = NULL, accepted_at = NULL,
        updated_at = pg_catalog.clock_timestamp() WHERE job_id = p_job;
    INSERT INTO aap_jobs_v1.dispatch_publication_retirements(job_id, execution_ids, outcome)
    VALUES (p_job, p_execution_ids, CASE WHEN v_published THEN 'published' ELSE 'not_sent' END);
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.retire_job_publications(uuid,text[]) FROM PUBLIC;
