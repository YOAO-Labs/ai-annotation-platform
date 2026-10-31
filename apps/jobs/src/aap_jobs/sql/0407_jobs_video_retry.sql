CREATE FUNCTION aap_jobs_v1.find_video_retry_job(p_output_id uuid, p_user_id uuid, p_asset_type text)
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT job.id FROM public.async_jobs AS job
    WHERE job.project_id IS NULL AND job.kind = 'video_asset_retry'
      AND job.status IN ('pending', 'running')
      AND job.payload ->> 'output_id' = p_output_id::text
      AND job.user_id = p_user_id AND job.payload ->> 'asset_type' = p_asset_type
    LIMIT 1
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.find_video_retry_job(uuid,uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.lock_video_retry_jobs(p_output_id uuid)
RETURNS SETOF uuid
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT job.id FROM public.async_jobs AS job
    WHERE job.project_id IS NULL AND job.kind = 'video_asset_retry'
      AND job.status IN ('pending', 'running')
      AND job.payload ->> 'output_id' = p_output_id::text
    ORDER BY job.id FOR UPDATE
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.lock_video_retry_jobs(uuid) FROM PUBLIC;
