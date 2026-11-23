GRANT USAGE, CREATE ON SCHEMA yoao_inference_topology_v1, yoao_inference_pipelines_v1 TO :"inference_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_jobs_v1 TO :"jobs_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_labeling_v1 TO :"labeling_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";

ALTER FUNCTION yoao_inference_topology_v1.lock_video_prediction_registry(uuid) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_topology_v1.lock_video_prediction_pool(uuid,uuid,uuid) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_topology_v1.lock_native_prediction_route(uuid,uuid,uuid) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_pipelines_v1.create_prediction_execution_snapshot(uuid,text,jsonb) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_pipelines_v1.read_prediction_execution_snapshot(uuid,uuid) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_pipelines_v1.prune_prediction_execution_snapshots(timestamptz) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_jobs_v1.prediction_execution_snapshot_in_use(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_labeling_v1.prediction_execution_snapshot_in_use(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.finish_prediction_retry_pipeline_failure(jsonb,jsonb) OWNER TO :"labeling_owner";

GRANT USAGE ON SCHEMA yoao_jobs_v1, yoao_labeling_v1 TO :"inference_owner";
GRANT EXECUTE ON FUNCTION
    yoao_jobs_v1.prediction_execution_snapshot_in_use(uuid),
    yoao_labeling_v1.prediction_execution_snapshot_in_use(uuid)
TO :"inference_owner";
GRANT SELECT (execution_snapshot_id) ON public.async_jobs TO :"jobs_owner";
GRANT SELECT (execution_snapshot_id) ON public.failed_predictions TO :"labeling_owner";
GRANT SELECT (id, project_id, schema_version, digest, payload, created_at),
      INSERT (id, project_id, schema_version, digest, payload), DELETE
ON public.prediction_execution_snapshots TO :"inference_owner";

ALTER FUNCTION yoao_assets_v1.create_prediction_input_lease(uuid,uuid,text,uuid,integer,jsonb,integer,jsonb,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.prediction_input_lease_facts(uuid,boolean,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.prediction_input_objects_pending(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_prediction_input_inventory(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_prediction_input_object(uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.reserve_prediction_input_object(uuid,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.verify_prediction_input_object(uuid,text,jsonb,text,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.seal_prediction_input_lease(uuid,boolean,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.prepare_prediction_input_cleanup(uuid,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_prediction_input_cleanup_failure(uuid,text,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_prediction_input_cleanup(uuid,uuid,integer,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.list_due_prediction_input_cleanups(timestamptz,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.prediction_input_binding_history() OWNER TO :"assets_owner";

GRANT SELECT (
    id, owner_id, owner_kind, task_id, contract_version, bucket, storage_identity, storage_ref,
    maintenance_revision, created_at, expires_at, retain_until, source_ref, state, uncertain_write,
    object_count, size_bytes, inventory, next_cleanup_at, error_code
), INSERT (
    id, owner_id, owner_kind, task_id, contract_version, bucket, storage_identity, storage_ref,
    maintenance_revision, created_at, expires_at, retain_until, source_ref, state, uncertain_write,
    object_count, size_bytes, inventory, next_cleanup_at
), UPDATE (
    inventory, object_count, size_bytes, state, uncertain_write, next_cleanup_at, last_cleanup_at,
    deleted_at, error_code
) ON public.prediction_input_leases TO :"assets_owner";
GRANT SELECT (id, purpose, location_id, revision) ON public.storage_operations TO :"assets_owner";

REVOKE CREATE ON SCHEMA yoao_inference_topology_v1, yoao_inference_pipelines_v1 FROM :"inference_owner";
REVOKE CREATE ON SCHEMA yoao_jobs_v1 FROM :"jobs_owner";
REVOKE CREATE ON SCHEMA yoao_labeling_v1 FROM :"labeling_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
