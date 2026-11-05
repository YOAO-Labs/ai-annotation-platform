CREATE FUNCTION public.fixture_export_retired(
    p_run_id uuid, p_project_id uuid, p_job_id uuid
)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_run_id IS NULL OR p_project_id IS NULL OR p_job_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid fixture export scope';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_runs AS run
        JOIN yoao_fixture_v1.fixture_run_resources AS project ON project.run_id = run.id
        WHERE run.id = p_run_id AND run.state = 'sealing'
          AND project.kind = 'project' AND project.resource_id = p_project_id
          AND project.state = 'published'
    ) THEN RETURN false; END IF;
    PERFORM bundle.id FROM public.export_media_bundles AS bundle
    WHERE bundle.job_id = p_job_id ORDER BY bundle.id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN false; END IF;
    RETURN NOT EXISTS (
        SELECT 1 FROM public.export_media_bundles AS bundle
        WHERE bundle.job_id = p_job_id AND (
            bundle.fixture_run_id IS DISTINCT FROM p_run_id
            OR bundle.project_id IS DISTINCT FROM p_project_id
            OR bundle.state <> 'deleted'
            OR bundle.expires_at > pg_catalog.clock_timestamp()
            OR (bundle.published_at IS NOT NULL AND NOT bundle.fixture_zip_created)
            OR (bundle.fixture_zip_created AND NOT EXISTS (
                SELECT 1 FROM public.fixture_export_zip_retirements AS receipt
                WHERE receipt.bundle_id = bundle.id AND receipt.output_id = bundle.zip_output_id
                  AND receipt.fixture_run_id = p_run_id AND receipt.project_id = p_project_id
                  AND receipt.job_id = p_job_id
            ))
            OR EXISTS (
                SELECT 1 FROM public.storage_operations AS operation
                WHERE operation.purpose IN (
                    'export_media_' || pg_catalog.replace(bundle.id::text, '-', ''),
                    'export_write_' || pg_catalog.replace(bundle.zip_output_id::text, '-', ''),
                    'export_read_' || pg_catalog.replace(bundle.zip_output_id::text, '-', '')
                )
            )
        )
    ) AND NOT EXISTS (
        SELECT 1 FROM public.export_outputs AS output WHERE output.job_id = p_job_id
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.fixture_export_retired(uuid, uuid, uuid) FROM PUBLIC;
