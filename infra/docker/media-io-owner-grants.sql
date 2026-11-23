ALTER TABLE yoao_assets_v1.media_io_claims OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.media_io_derivative_manifests OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.embedded_media_claim_key OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._media_io_permit(uuid,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._media_io_target(jsonb,text,text,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._media_io_import_target(jsonb,text,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._media_io_creation_target(public.managed_original_creations) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_workspace_v1._media_io_guide(uuid,text) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_labeling_v1._media_io_comment(uuid,text) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._media_io_raster(uuid,text) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_assets_v1.media_io_artifact(text,text,uuid,uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.media_io_cache_read(text,uuid,text,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.media_io_original_read(text,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.media_io_creation(uuid,uuid,uuid,text,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.media_io_creation_evidence(uuid,uuid,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_saved_operator_original(jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.saved_operator_item_original(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.saved_operator_auxiliary_original(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.media_io_thumbnail(text,uuid,uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_labeling_v1._media_io_mask_upload(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_assets_v1.media_io_mask_upload(uuid,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.media_io_derivative(text,text,uuid,uuid,uuid,jsonb,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.media_io_original_cache(uuid,uuid,uuid,text,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._embedded_original_builtin(text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.prove_embedded_admitted_original(jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_embedded_admitted_original(jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.media_io_prediction_input(uuid,uuid,text,text,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.media_io_retired_original(uuid,uuid,text,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.open_saved_operator_original_operation(uuid) OWNER TO :"assets_owner";

GRANT USAGE ON SCHEMA yoao_workspace_v1, yoao_labeling_v1 TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_workspace_v1._media_io_guide(uuid,text),
    yoao_labeling_v1._media_io_comment(uuid,text),
    yoao_labeling_v1._media_io_raster(uuid,text), yoao_labeling_v1._media_io_mask_upload(uuid)
    TO :"assets_owner";
GRANT SELECT (id, checksum_value, verification_state, checksum_algorithm, size_bytes)
    ON public.dataset_assets TO :"assets_owner";
GRANT SELECT (id, object_key, storage_ref, bucket, location_id, sha256, size_bytes)
    ON public.dataset_auxiliary_originals, public.task_originals TO :"assets_owner";
GRANT SELECT (id, input_ref, writer_until, storage_ref, state) ON public.thumbnail_outputs TO :"assets_owner";
GRANT SELECT (id, location_id, asset_id, state, writer_until, expires_at, storage_ref, sha256, size_bytes)
    ON public.original_cache_entries TO :"assets_owner";
GRANT SELECT (id, entry_id, expires_at) ON public.original_cache_read_leases TO :"assets_owner";
GRANT SELECT (id, state, expires_at, storage_ref, inventory, contract_version, maintenance_revision)
    ON public.prediction_input_leases TO :"assets_owner";
