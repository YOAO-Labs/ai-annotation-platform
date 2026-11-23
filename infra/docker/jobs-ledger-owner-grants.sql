GRANT CREATE ON SCHEMA yoao_jobs_v1 TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.dataset_job_visible(uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._directory_job_ids(uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.directory_job_page(uuid,uuid,integer,integer) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.directory_job_total(uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.dataset_active_job_facts(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.active_storage_writer_job_counts() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.settle_dataset_scan_job(uuid,text,text,timestamptz,text,jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.transition_directory_registration_job(uuid,uuid,text,text,text,timestamptz,text,jsonb) OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA yoao_jobs_v1 FROM :"jobs_owner";

GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.directory_job_visible(uuid,text,uuid,text,uuid),
    yoao_assets_v1.directory_location_job_runs(uuid,uuid),
    yoao_assets_v1.dataset_quality_job_visible(uuid,text,text)
    TO :"jobs_owner";
GRANT USAGE ON SCHEMA yoao_jobs_v1 TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    yoao_jobs_v1.dataset_active_job_facts(uuid),
    yoao_jobs_v1.settle_dataset_scan_job(uuid,text,text,timestamptz,text,jsonb),
    yoao_jobs_v1.transition_directory_registration_job(uuid,uuid,text,text,text,timestamptz,text,jsonb)
    TO :"assets_owner";
