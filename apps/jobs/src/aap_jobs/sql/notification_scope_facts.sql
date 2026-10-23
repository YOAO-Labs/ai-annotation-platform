CREATE VIEW aap_jobs_v1.notification_scope_facts
WITH (security_barrier = true)
AS
SELECT
    job.id,
    job.kind,
    COALESCE(job.project_id, job.retained_project_id) AS scope_project_id
FROM public.async_jobs AS job;
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.notification_scope_facts FROM PUBLIC;
