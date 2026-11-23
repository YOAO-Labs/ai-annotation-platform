GRANT USAGE, CREATE ON SCHEMA yoao_export_v1 TO :"export_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
GRANT CREATE ON SCHEMA public TO :"export_owner";
GRANT USAGE ON SCHEMA yoao_export_v1 TO :"assets_owner";

GRANT SELECT, INSERT, DELETE ON public.export_artifacts TO :"export_owner";
GRANT UPDATE (output_id, batch_id, format, object_key, file_count, size_bytes, expires_at)
    ON public.export_artifacts TO :"export_owner";
GRANT SELECT, DELETE ON public.export_outputs TO :"export_owner";
GRANT INSERT (id, project_id, job_id, media_bundle_id, media, format, file_count,
    sha256, size_bytes, storage_ref, state, created_at, expires_at),
    UPDATE (upload_proof, state, retired_at, last_attempted_at)
    ON public.export_outputs TO :"export_owner";
GRANT SELECT ON public.export_media_bundles TO :"export_owner";
GRANT INSERT (id, project_id, job_id, zip_output_id, input_snapshot, input_digest,
    logical_paths, inventory, object_count, size_bytes, storage_ref, state, created_at, expires_at),
    UPDATE (state, lease_token, lease_expires_at, published_at, retired_at, last_attempted_at, fixture_zip_created)
    ON public.export_media_bundles TO :"export_owner";
GRANT SELECT, INSERT ON public.export_media_member_versions TO :"export_owner";
GRANT SELECT (id, purpose, location_id, revision) ON public.storage_operations TO :"export_owner";
GRANT SELECT (location_id, state, revision) ON public.storage_maintenance_gates TO :"export_owner";
GRANT SELECT (purpose, location_id, bucket, generation) ON public.storage_bindings TO :"export_owner";
GRANT SELECT (id, kind, enabled, config_revision) ON public.storage_locations TO :"export_owner";

ALTER FUNCTION public.check_export_media_pair() OWNER TO :"export_owner";
ALTER VIEW yoao_export_v1._zip_receipt OWNER TO :"export_owner";
ALTER VIEW yoao_export_v1._media_fence OWNER TO :"export_owner";
ALTER VIEW yoao_export_v1._media_delivery OWNER TO :"export_owner";
ALTER VIEW yoao_export_v1._cache_entry OWNER TO :"export_owner";
ALTER VIEW yoao_export_v1.export_zip_maintenance OWNER TO :"export_owner";
ALTER VIEW yoao_export_v1.export_media_maintenance OWNER TO :"export_owner";
GRANT SELECT ON yoao_export_v1.export_zip_maintenance, yoao_export_v1.export_media_maintenance
    TO :"assets_owner";
ALTER FUNCTION yoao_export_v1.zip_receipt(uuid,text) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1._matches_export_allocation(jsonb) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.reserve_zip(uuid,uuid,uuid,uuid,text,text,integer,text,bigint,jsonb,timestamptz,timestamptz) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.save_zip_proof(uuid,jsonb) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.publish_zip(uuid) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.allocate_media(uuid,uuid,uuid,uuid,jsonb,text,jsonb,jsonb,integer,bigint,jsonb,timestamptz,timestamptz) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.media_delivery(uuid,boolean) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.media_fence(uuid,text) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.claim_media(uuid,uuid,timestamptz) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.publish_media(uuid,uuid,timestamptz,timestamptz) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.save_media_version(uuid,uuid,uuid,timestamptz,text) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.media_version(uuid,uuid) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.media_versions(uuid) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.find_cache(text,timestamptz) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.lock_cache(uuid) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.invalidate_cache(uuid,uuid) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.record_cache(text,uuid,uuid) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1._media_has_operations(uuid,uuid,uuid) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1._media_attempt_ended(uuid,uuid,uuid,timestamptz,timestamptz,timestamptz,timestamptz) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.media_attempt_ended(uuid,timestamptz) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.media_has_operations(uuid,uuid) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.lock_media_retirement(uuid,boolean) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.start_media_retirement(uuid,timestamptz) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.finish_media_retirement(uuid,timestamptz) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.media_retirement_queue(timestamptz,integer) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.zip_retirement_bundle(uuid) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1._zip_has_read(uuid) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1._zip_retirement_allowed(uuid,uuid,timestamptz,boolean) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.start_zip_retirement(uuid,timestamptz) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.zip_retirement_unreferenced(uuid) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.remove_retired_zip(uuid,timestamptz) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.purge_legacy_cache(timestamptz,integer) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.zip_retirement_queue(timestamptz,integer) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.binding_history() OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.saved_location_is_referenced(uuid) OWNER TO :"export_owner";
GRANT EXECUTE ON FUNCTION yoao_export_v1.saved_location_is_referenced(uuid) TO :"assets_owner";
ALTER FUNCTION yoao_export_v1._inventory_ref(jsonb) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.export_zip_inventory(uuid,integer) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.export_media_inventory(uuid,integer) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.export_media_inventory_objects(uuid,text,integer) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.export_zip_usage(uuid[]) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.export_media_usage(uuid[]) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.retained_zip_download(uuid) OWNER TO :"export_owner";
GRANT EXECUTE ON FUNCTION yoao_export_v1.retained_zip_download(uuid) TO :"assets_owner";
ALTER FUNCTION yoao_export_v1._export_io_gate_revision(uuid,uuid,text[],boolean) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.zip_io_subject(uuid,uuid,text) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.media_member_io_subject(uuid,uuid,uuid,text) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.media_prefix_io_subject(uuid,uuid,text) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.save_source_zip_proof(uuid,uuid,jsonb) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.save_source_media_version(uuid,uuid,uuid,text) OWNER TO :"export_owner";

GRANT SELECT ON public.datasets, public.scenes TO :"assets_owner";
GRANT UPDATE (id) ON public.sensor_calibration_revisions TO :"assets_owner";
ALTER VIEW yoao_assets_v1.export_dataset_facts OWNER TO :"assets_owner";
ALTER VIEW yoao_assets_v1.export_item_facts OWNER TO :"assets_owner";
ALTER VIEW yoao_assets_v1.export_scene_facts OWNER TO :"assets_owner";
ALTER VIEW yoao_assets_v1.export_calibration_facts OWNER TO :"assets_owner";
ALTER VIEW yoao_assets_v1.export_pose_facts OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_export_datasets_share(uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_export_items_update(uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_export_scenes_update(uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_export_calibrations_share(uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_export_poses_share(uuid[]) OWNER TO :"assets_owner";

REVOKE CREATE ON SCHEMA public, yoao_export_v1 FROM :"export_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
