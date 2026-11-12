-- An undelivered presentation row receives its proof before Job insertion in
-- the same transaction; no committed or previously dispatched Job is adopted.
CREATE FUNCTION yoao_fixture_v1.guard_presentation_job_update()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_proof yoao_fixture_v1.fixture_presentation_jobs%ROWTYPE;
BEGIN
    SELECT proof.* INTO v_proof FROM yoao_fixture_v1.fixture_presentation_jobs AS proof
    WHERE proof.job_id = OLD.id;
    IF NOT FOUND THEN RETURN NEW; END IF;
    IF v_proof.state = 'admitted' AND OLD.status = 'pending'
       AND OLD.celery_task_id IS NULL AND OLD.started_at IS NULL
       AND NEW.celery_task_id IS NULL AND NEW.started_at IS NULL
       AND (pg_catalog.to_jsonb(NEW) - ARRAY['status','progress_pct','result','error_message','updated_at'])
           IS NOT DISTINCT FROM (pg_catalog.to_jsonb(OLD) - ARRAY['status','progress_pct','result','error_message','updated_at'])
       AND ((v_proof.kind = 'batch_predict' AND NEW.status = 'completed'
             AND NEW.progress_pct = 100 AND NEW.result = '{"success_count":2}'::jsonb AND NEW.error_message IS NULL)
         OR (v_proof.kind = 'export' AND NEW.status = 'failed'
             AND NEW.progress_pct = 40 AND NEW.error_message = 'fixture failure' AND NEW.result = '{}'::jsonb)) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_presentation_job_immutable';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_fixture_v1.guard_presentation_job_update() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_presentation_job_update_guard
BEFORE UPDATE ON public.async_jobs FOR EACH ROW
EXECUTE FUNCTION yoao_fixture_v1.guard_presentation_job_update();
-- aap-statement-break
CREATE FUNCTION yoao_fixture_v1.guard_presentation_job_dispatch()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_presentation_jobs AS proof WHERE proof.job_id = NEW.job_id) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_presentation_job_cannot_dispatch';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_fixture_v1.guard_presentation_job_dispatch() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_presentation_job_dispatch_guard
BEFORE INSERT OR UPDATE OF job_id ON aap_jobs_v1.dispatch_intents FOR EACH ROW
EXECUTE FUNCTION yoao_fixture_v1.guard_presentation_job_dispatch();
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1.set_filter_fixture_job_outcomes(
    p_completed_job_id uuid, p_failed_job_id uuid
)
RETURNS bigint LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
AS $function$
DECLARE v_id uuid; v_count bigint;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Jobs fixture initialization requires a test database';
    END IF;
    IF p_completed_job_id IS NULL OR p_failed_job_id IS NULL OR p_completed_job_id = p_failed_job_id
       OR NOT EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_presentation_jobs
           WHERE job_id = p_completed_job_id AND kind = 'batch_predict' AND state = 'admitted')
       OR NOT EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_presentation_jobs
           WHERE job_id = p_failed_job_id AND kind = 'export' AND state = 'admitted') THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Jobs filter fixture proof';
    END IF;
    FOR v_id IN SELECT item.id FROM pg_catalog.unnest(ARRAY[p_completed_job_id, p_failed_job_id]) AS item(id) ORDER BY item.id LOOP
        IF v_id = p_completed_job_id THEN
            UPDATE public.async_jobs SET status = 'completed', progress_pct = 100,
                result = '{"success_count":2}'::jsonb, updated_at = pg_catalog.now() WHERE id = v_id;
        ELSE
            UPDATE public.async_jobs SET status = 'failed', progress_pct = 40,
                error_message = 'fixture failure', updated_at = pg_catalog.now() WHERE id = v_id;
        END IF;
        GET DIAGNOSTICS v_count = ROW_COUNT;
        IF v_count <> 1 THEN RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'Jobs filter fixture row not found'; END IF;
    END LOOP;
    RETURN 2::bigint;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.set_filter_fixture_job_outcomes(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.create_filter_fixture_jobs(
    p_run_id uuid, p_completed_id uuid, p_failed_id uuid,
    p_completed_project uuid, p_failed_project uuid, p_actor_id uuid,
    p_completed_prompt text, p_failed_prompt text
)
RETURNS bigint LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_state text;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Jobs fixture initialization requires a test database';
    END IF;
    IF p_run_id IS NULL OR p_completed_id IS NULL OR p_failed_id IS NULL
       OR p_completed_id = p_failed_id OR p_completed_project IS NULL OR p_failed_project IS NULL
       OR p_completed_project = p_failed_project OR p_actor_id IS NULL
       OR p_completed_prompt IS NULL OR p_failed_prompt IS NULL
       OR pg_catalog.length(p_completed_prompt) NOT BETWEEN 1 AND 255
       OR pg_catalog.length(p_failed_prompt) NOT BETWEEN 1 AND 255 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Jobs filter fixture scope';
    END IF;
    SELECT run.state INTO v_state FROM yoao_fixture_v1.fixture_runs AS run WHERE run.id = p_run_id FOR SHARE NOWAIT;
    IF NOT FOUND OR v_state <> 'open' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_run_closed';
    END IF;
    IF (SELECT pg_catalog.count(*) FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.run_id = p_run_id AND resource.kind = 'project' AND resource.state = 'published'
          AND resource.resource_id IN (p_completed_project, p_failed_project)) <> 2 THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_project_scope_mismatch';
    END IF;
    INSERT INTO yoao_fixture_v1.fixture_presentation_jobs (job_id, run_id, project_id, kind)
    VALUES (p_completed_id, p_run_id, p_completed_project, 'batch_predict'),
           (p_failed_id, p_run_id, p_failed_project, 'export');
    INSERT INTO public.async_jobs (id, kind, project_id, user_id, status, progress_pct, payload, result)
    VALUES (p_completed_id, 'batch_predict', p_completed_project, p_actor_id, 'pending', 0,
                pg_catalog.jsonb_build_object('model_key','filter-detector','prompt',p_completed_prompt), '{}'::jsonb),
           (p_failed_id, 'export', p_failed_project, p_actor_id, 'pending', 0,
                pg_catalog.jsonb_build_object('model_key','filter-export','prompt',p_failed_prompt), '{}'::jsonb);
    RETURN aap_jobs_v1.set_filter_fixture_job_outcomes(p_completed_id, p_failed_id);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.create_filter_fixture_jobs(uuid,uuid,uuid,uuid,uuid,uuid,text,text) FROM PUBLIC;
