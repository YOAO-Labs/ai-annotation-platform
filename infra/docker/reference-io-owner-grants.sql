GRANT USAGE ON SCHEMA yoao_assets_storage_v1, yoao_identity_v1, yoao_jobs_v1,
    yoao_labeling_v1 TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_storage_v1.physical_io_receipt(uuid),
    yoao_assets_storage_v1.gate_facts(uuid,text),
    yoao_assets_storage_v1.location_probe_facts(uuid,text),
    yoao_identity_v1.read_actor_facts(uuid),
    yoao_identity_v1.read_accepted_api_key_facts(uuid,uuid),
    yoao_jobs_v1.read_job(uuid),
    yoao_labeling_v1.read_media_task_access(uuid,uuid,uuid) TO :"assets_owner";
GRANT SELECT ON yoao_assets_storage_v1.location_metadata,
    yoao_assets_storage_v1.connector_origins TO :"assets_owner";
GRANT SELECT (id, file_path) ON yoao_labeling_v1.task_rows TO :"assets_owner";
GRANT CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
ALTER TABLE yoao_assets_v1.reference_io_requests OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._reference_io_scope(uuid,text,uuid,uuid,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.prepare_reference_io(uuid,text,uuid,uuid,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.capture_reference_io(uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.recheck_reference_io(uuid,text) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
