-- Forward extension: the only writer of async_jobs.execution_snapshot_id.
--
-- The frozen 0327 jobs lifecycle command surface deliberately rejects
-- execution_snapshot_id, so neither batch_predict nor prediction_retry may
-- submit it through apply_lifecycle. This narrow capability binds the immutable
-- snapshot once for a pending Job whose current owner and project still match,
-- and stays idempotent for the same snapshot on redelivery.
CREATE FUNCTION aap_jobs_v1.bind_prediction_execution_snapshot(
    p_job_id pg_catalog.uuid,
    p_actor_id pg_catalog.uuid,
    p_snapshot_id pg_catalog.uuid
)
RETURNS boolean
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_project uuid;
    v_bound boolean := false;
BEGIN
    IF p_job_id IS NULL OR p_actor_id IS NULL OR p_snapshot_id IS NULL THEN
        RETURN false;
    END IF;
    BEGIN
        SELECT * INTO v_job FROM public.async_jobs AS job
        WHERE job.id = p_job_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN false;
    END;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    IF v_job.kind NOT IN ('batch_predict', 'prediction_retry')
       OR v_job.user_id IS DISTINCT FROM p_actor_id
       OR v_job.status IS DISTINCT FROM 'pending' THEN
        RETURN false;
    END IF;
    SELECT snapshot.project_id INTO v_project
    FROM public.prediction_execution_snapshots AS snapshot
    WHERE snapshot.id = p_snapshot_id;
    IF NOT FOUND OR v_project IS NULL THEN
        RETURN false;
    END IF;
    IF COALESCE(v_job.project_id, v_job.retained_project_id) IS DISTINCT FROM v_project THEN
        RETURN false;
    END IF;
    IF v_job.execution_snapshot_id IS NOT NULL THEN
        RETURN v_job.execution_snapshot_id = p_snapshot_id;
    END IF;
    UPDATE public.async_jobs AS job
    SET execution_snapshot_id = p_snapshot_id,
        updated_at = pg_catalog.clock_timestamp()
    WHERE job.id = p_job_id
      AND job.execution_snapshot_id IS NULL;
    v_bound := FOUND;
    IF NOT v_bound THEN
        -- A concurrent binder won the row; only the same snapshot is accepted.
        SELECT job.execution_snapshot_id = p_snapshot_id INTO v_bound
        FROM public.async_jobs AS job
        WHERE job.id = p_job_id;
        v_bound := COALESCE(v_bound, false);
    END IF;
    RETURN v_bound;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.bind_prediction_execution_snapshot(
    pg_catalog.uuid, pg_catalog.uuid, pg_catalog.uuid
) FROM PUBLIC;
