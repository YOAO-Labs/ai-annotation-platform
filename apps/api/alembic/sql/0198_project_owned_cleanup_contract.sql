-- Caller-owned project deletion cleanup capabilities.
-- Every function participates in the caller's transaction and never commits.
CREATE SCHEMA aap_inference_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
CREATE SCHEMA aap_insights_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
CREATE SCHEMA aap_diagnostics_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
CREATE SCHEMA aap_export_v1 AUTHORIZATION CURRENT_USER;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_inference_v1 FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_insights_v1 FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_diagnostics_v1 FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON SCHEMA aap_export_v1 FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.delete_project_owned_records(p_project_id uuid)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
STRICT
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_deleted bigint := 0;
    v_rows bigint;
BEGIN
    DELETE FROM public.project_datasets AS association
    WHERE association.project_id = p_project_id;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_deleted := v_deleted + v_rows;

    DELETE FROM public.storage_connections AS connection
    WHERE connection.project_id = p_project_id;
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_deleted := v_deleted + v_rows;

    DELETE FROM public.image_pyramid_assets AS asset
    WHERE asset.task_id IN (
        SELECT reference.task_id
        FROM aap_labeling_v1.project_task_references(p_project_id) AS reference
    );
    GET DIAGNOSTICS v_rows = ROW_COUNT;
    v_deleted := v_deleted + v_rows;

    RETURN v_deleted;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.delete_project_owned_records(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_v1.clear_project_ml_backend_pool(p_project_id uuid)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
STRICT
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_deleted bigint;
BEGIN
    DELETE FROM public.project_ml_backend_pool AS binding
    WHERE binding.project_id = p_project_id;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN v_deleted;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_v1.clear_project_ml_backend_pool(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_inference_v1.clear_project_pipelines(p_project_id uuid)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
STRICT
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_deleted bigint;
BEGIN
    DELETE FROM public.project_pipelines AS pipeline
    WHERE pipeline.project_id = p_project_id;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN v_deleted;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_v1.clear_project_pipelines(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_insights_v1.clear_project_task_views(p_project_id uuid)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
STRICT
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_deleted bigint;
BEGIN
    DELETE FROM public.project_task_views AS task_view
    WHERE task_view.project_id = p_project_id;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN v_deleted;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_insights_v1.clear_project_task_views(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_diagnostics_v1.detach_project_references(p_project_id uuid)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
STRICT
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_detached bigint;
BEGIN
    UPDATE public.bug_reports AS report
    SET project_id = CASE
            WHEN report.project_id = p_project_id THEN NULL
            ELSE report.project_id
        END,
        task_id = CASE
            WHEN report.task_id IN (
                SELECT reference.task_id
                FROM aap_labeling_v1.project_task_references(p_project_id) AS reference
            ) THEN NULL
            ELSE report.task_id
        END
    WHERE report.project_id = p_project_id
       OR report.task_id IN (
            SELECT reference.task_id
            FROM aap_labeling_v1.project_task_references(p_project_id) AS reference
       );
    GET DIAGNOSTICS v_detached = ROW_COUNT;
    RETURN v_detached;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_diagnostics_v1.detach_project_references(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_export_v1.clear_project_export_artifacts(p_project_id uuid)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
STRICT
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_deleted bigint;
BEGIN
    DELETE FROM public.export_artifacts AS artifact
    WHERE artifact.project_id = p_project_id;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN v_deleted;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_export_v1.clear_project_export_artifacts(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.detach_project_references(p_project_id uuid)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
STRICT
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_updated bigint;
BEGIN
    UPDATE public.async_jobs AS job
    SET project_id = NULL,
        status = CASE
            WHEN job.kind = 'create_tasks' AND job.status IN ('pending', 'running')
                THEN 'cancelled'
            ELSE job.status
        END,
        completed_at = CASE
            WHEN job.kind = 'create_tasks' AND job.status IN ('pending', 'running')
                THEN pg_catalog.now()
            ELSE job.completed_at
        END,
        updated_at = CASE
            WHEN job.kind = 'create_tasks' AND job.status IN ('pending', 'running')
                THEN pg_catalog.now()
            ELSE job.updated_at
        END
    WHERE job.project_id = p_project_id;
    GET DIAGNOSTICS v_updated = ROW_COUNT;
    RETURN v_updated;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.detach_project_references(uuid) FROM PUBLIC;
