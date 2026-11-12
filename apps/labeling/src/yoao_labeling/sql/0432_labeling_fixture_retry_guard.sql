-- Lock the exact predecessor failures before retiring an undelivered
-- presentation Job. Real retry attempts remain authoritative even if their
-- generic Job row is no longer present.
CREATE FUNCTION aap_labeling_v1.guard_fixture_presentation_failures(
    p_run_id uuid, p_project_id uuid, p_failed_ids uuid[]
)
RETURNS void LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_count bigint;
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='Labeling fixture cleanup requires a test database';
    END IF;
    IF p_run_id IS NULL OR p_project_id IS NULL OR p_failed_ids IS NULL
       OR pg_catalog.array_position(p_failed_ids, NULL::uuid) IS NOT NULL
       OR (SELECT pg_catalog.count(DISTINCT value) FROM pg_catalog.unnest(p_failed_ids) AS ids(value))
          <> pg_catalog.cardinality(p_failed_ids)
       OR NOT EXISTS (
           SELECT 1 FROM yoao_fixture_v1.fixture_runs AS run
           JOIN yoao_fixture_v1.fixture_run_resources AS project ON project.run_id=run.id
           WHERE run.id=p_run_id AND run.state IN ('open','sealing')
             AND project.kind='project' AND project.resource_id=p_project_id
             AND project.state='published'
       ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_presentation_scope_changed';
    END IF;
    BEGIN
        PERFORM failed.id FROM public.failed_predictions AS failed
        WHERE failed.id=ANY(p_failed_ids) AND failed.project_id=p_project_id
        ORDER BY failed.id FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
    END;
    SELECT pg_catalog.count(*) INTO v_count FROM public.failed_predictions AS failed
    WHERE failed.id=ANY(p_failed_ids) AND failed.project_id=p_project_id;
    IF v_count <> pg_catalog.cardinality(p_failed_ids) OR EXISTS (
        SELECT 1 FROM public.prediction_retry_attempts AS attempt
        WHERE attempt.failed_prediction_id=ANY(p_failed_ids)
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.guard_fixture_presentation_failures(uuid,uuid,uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.guard_fixture_project_retry_anchors(p_project_ids uuid[])
RETURNS void LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='Labeling fixture cleanup requires a test database';
    END IF;
    IF p_project_ids IS NULL OR pg_catalog.array_position(p_project_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid fixture project scope';
    END IF;
    BEGIN
        PERFORM failed.id FROM public.failed_predictions AS failed
        WHERE failed.project_id=ANY(p_project_ids)
        ORDER BY failed.id FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
    END;
    IF EXISTS (
        SELECT 1 FROM public.prediction_retry_attempts AS attempt
        WHERE attempt.project_id=ANY(p_project_ids)
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_job_execution_unsettled';
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.guard_fixture_project_retry_anchors(uuid[]) FROM PUBLIC;
