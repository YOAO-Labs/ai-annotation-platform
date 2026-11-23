GRANT USAGE ON SCHEMA yoao_jobs_v1 TO :"platform_runtime";
GRANT EXECUTE ON FUNCTION
    yoao_jobs_v1.dataset_job_visible(uuid,uuid),
    yoao_jobs_v1.directory_job_page(uuid,uuid,integer,integer),
    yoao_jobs_v1.directory_job_total(uuid,uuid),
    yoao_jobs_v1.active_storage_writer_job_counts()
    TO :"platform_runtime";
