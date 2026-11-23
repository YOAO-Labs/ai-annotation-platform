GRANT USAGE, CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
GRANT SELECT (version_num) ON public.alembic_version TO :"assets_owner";
GRANT SELECT (asset_id, bucket, etag, location_id, object_key, reference_state, role, storage_ref) ON public.dataset_asset_locations TO :"assets_owner";
GRANT SELECT (bucket, etag, id, location_id, object_key, sha256, size_bytes, storage_identity, storage_ref, version_id) ON public.dataset_auxiliary_originals TO :"assets_owner";
GRANT SELECT (asset_id, bucket, location_id, object_key, path_digest, sha256, size_bytes, source_id, state) ON public.dataset_source_previews TO :"assets_owner";
GRANT SELECT (asset_id, id, inventory, state, storage_ref) ON public.image_pyramid_outputs TO :"assets_owner";
GRANT SELECT (bucket, configuration, etag, id, location_id, object_key, sha256, size_bytes, source, staging_bucket, staging_key, staging_ref, status, storage_identity, version_id) ON public.managed_original_creations TO :"assets_owner";
GRANT SELECT (asset_id, bucket, etag, id, identity_state, location_id, object_key, reference_state, sha256, size_bytes, storage_identity) ON public.managed_original_retirements TO :"assets_owner";
GRANT SELECT (asset_id, bucket, id, location_id, sha256, size_bytes, state, storage_identity, storage_ref) ON public.original_cache_entries TO :"assets_owner";
GRANT SELECT (bucket, id, inventory, size_bytes, source_ref, state, storage_identity, storage_ref) ON public.prediction_input_leases TO :"assets_owner";
GRANT SELECT (bucket, generation, location_id, purpose) ON public.storage_bindings TO :"assets_owner";
GRANT SELECT (config, id, kind, revision, scope) ON public.storage_connections TO :"assets_owner";
GRANT SELECT (config, config_revision, connection_id, enabled, id, kind) ON public.storage_locations TO :"assets_owner";
GRANT SELECT (target_location_id) ON public.storage_migration_copies TO :"assets_owner";
GRANT SELECT (bucket, etag, id, location_id, object_key, sha256, size_bytes, storage_identity, storage_ref, version_id) ON public.task_originals TO :"assets_owner";
GRANT SELECT (etag, id, sha256, size_bytes, state, storage_ref, version_id) ON public.thumbnail_outputs TO :"assets_owner";
GRANT SELECT (id, inventory, original_ref, state, storage_ref) ON public.video_derivative_outputs TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._inventory_saved_locator(jsonb,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_inventory_revisions() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_inventory_locations(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_inventory_bindings(text,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_copy_target_reference_count(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.inventory_prediction_input_leases(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.inventory_prediction_input_leases_objects(uuid,text,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.inventory_image_pyramid_outputs(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.inventory_image_pyramid_outputs_objects(uuid,text,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.inventory_video_derivative_outputs(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.inventory_video_derivative_outputs_objects(uuid,text,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.inventory_managed_original_creations(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.inventory_managed_original_retirements(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.inventory_dataset_asset_locations(uuid,uuid,text,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.inventory_task_originals(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.inventory_dataset_auxiliary_originals(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.inventory_dataset_source_previews(uuid,text,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.inventory_original_cache_entries(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.inventory_thumbnail_outputs(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.usage_prediction_input_leases(uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.usage_image_pyramid_outputs(uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.usage_video_derivative_outputs(uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.usage_original_cache_entries(uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.usage_original_and_preview_claims(uuid,jsonb,text,text,text,text[],uuid[],uuid[]) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_audit_v1 TO :"audit_owner";
GRANT SELECT (id, month, sha256, size_bytes, state, storage_ref, year) ON public.audit_archive_outputs TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.inventory_audit_archive_outputs(uuid,integer) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.usage_audit_archive_outputs(uuid[]) OWNER TO :"audit_owner";
REVOKE CREATE ON SCHEMA yoao_audit_v1 FROM :"audit_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_diagnostics_v1 TO :"diagnostics_owner";
GRANT SELECT (id, mime_type, object_version_id, owner_id, sha256, size_bytes, state, storage_ref) ON public.bug_attachment_uploads TO :"diagnostics_owner";
ALTER FUNCTION yoao_diagnostics_v1.inventory_bug_attachment_uploads(uuid,integer) OWNER TO :"diagnostics_owner";
ALTER FUNCTION yoao_diagnostics_v1.usage_bug_attachment_uploads(uuid[]) OWNER TO :"diagnostics_owner";
REVOKE CREATE ON SCHEMA yoao_diagnostics_v1 FROM :"diagnostics_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_identity_v1 TO :"identity_owner";
GRANT SELECT (object_version_id, sha256, size_bytes, state, storage_ref, token) ON public.avatar_uploads TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.inventory_avatar_uploads(text,integer) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.usage_avatar_uploads(text[]) OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA yoao_identity_v1 FROM :"identity_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_labeling_v1 TO :"labeling_owner";
GRANT SELECT (annotation_id, id, sha256, size_bytes, state, storage_ref) ON public.comment_attachment_uploads TO :"labeling_owner";
GRANT SELECT (id, staged_object_key, staged_sha256, staging_ref, status) ON public.mask_format_imports TO :"labeling_owner";
GRANT SELECT (id, logical_object_key, storage_ref) ON public.mask_format_uploads TO :"labeling_owner";
GRANT SELECT (canonical_sha256, id, state, storage_encoding, storage_ref, stored_sha256, stored_size_bytes) ON public.raster_mask_objects TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.inventory_comment_attachment_uploads(uuid,integer) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.inventory_raster_mask_objects(uuid,integer) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.inventory_mask_format_uploads(uuid,integer) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.inventory_mask_format_imports(uuid,integer) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.usage_comment_attachment_uploads(uuid[]) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.usage_raster_mask_objects(uuid[]) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.usage_mask_format_uploads(text,text,text[]) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.usage_mask_format_imports(text,text,text[]) OWNER TO :"labeling_owner";
REVOKE CREATE ON SCHEMA yoao_labeling_v1 FROM :"labeling_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_learning_v1 TO :"learning_owner";
GRANT SELECT (id, project_id, status, storage_ref) ON public.active_learning_rounds TO :"learning_owner";
GRANT SELECT (id, project_id, status, storage_ref) ON public.sample_evaluation_runs TO :"learning_owner";
GRANT SELECT (id, manifest, package_key, package_sha256, project_id, size_bytes, snapshot, status, storage_ref) ON public.training_dataset_versions TO :"learning_owner";
GRANT SELECT (id, project_id, status, storage_ref, upload_inventory) ON public.training_previews TO :"learning_owner";
GRANT SELECT (id, project_id, status, storage_ref) ON public.training_runs TO :"learning_owner";
GRANT SELECT (bucket, id, prefix, storage_ref) ON public.training_storage_deletions TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.inventory_training_dataset_versions(uuid,integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.inventory_training_runs(uuid,integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.inventory_training_previews(uuid,integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.inventory_sample_evaluation_runs(uuid,integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.inventory_active_learning_rounds(uuid,integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.inventory_training_storage_deletions(uuid,integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.usage_training_dataset_versions(uuid[]) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.usage_training_runs(uuid[]) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.usage_training_previews(uuid[]) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.usage_sample_evaluation_runs(uuid[]) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.usage_active_learning_rounds(uuid[]) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.usage_training_storage_deletions(text,text[]) OWNER TO :"learning_owner";
REVOKE CREATE ON SCHEMA yoao_learning_v1 FROM :"learning_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_workspace_v1 TO :"workspace_owner";
GRANT SELECT (id, project_id, sha256, size_bytes, state, storage_ref) ON public.guide_asset_uploads TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.inventory_guide_asset_uploads(uuid,integer) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.usage_guide_asset_uploads(uuid[]) OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA yoao_workspace_v1 FROM :"workspace_owner";

GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"audit_owner", :"diagnostics_owner", :"identity_owner", :"labeling_owner", :"learning_owner", :"workspace_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1._inventory_saved_locator(jsonb,boolean) TO :"audit_owner", :"diagnostics_owner", :"identity_owner", :"labeling_owner", :"learning_owner", :"workspace_owner";

GRANT CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE ON public.storage_usage_inventories, public.storage_usage_snapshots,
    public.storage_usage_schedules, public.storage_usage_mutations TO :"assets_owner";
GRANT SELECT, INSERT, DELETE ON public.storage_usage_inventory_objects,
    public.storage_usage_reconciliations TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.usage_inventory_status() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.usage_snapshot_facts(text[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_usage_inventory(timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_expired_usage_inventory(timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.start_usage_inventory(uuid,integer,jsonb,jsonb,timestamptz,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.claim_usage_inventory(uuid,integer,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_usage_inventory_page(uuid,integer,integer,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_usage_inventory_page(uuid,integer,integer,jsonb,timestamptz,text,text,bigint,bigint,jsonb,timestamptz,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.release_usage_inventory_chunk(uuid,integer,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_usage_inventory(uuid,integer,text,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.usage_schedule_summary() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_active_usage_schedule() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.configure_usage_schedule(boolean,integer,jsonb,jsonb,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.pause_usage_schedule(text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.advance_usage_schedule(timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_usage_mutation(uuid,text,text,text,text,text,uuid,text,text,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.publish_usage_mutation(uuid,boolean,boolean,bigint,text,bigint,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.stage_usage_inventory_objects(uuid,text,jsonb,timestamptz,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.reconcile_usage_bucket(text,uuid,timestamptz,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.discard_usage_staging(uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.usage_mutation_summary(text[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.usage_reconciliation_facts(text[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_inventory_binding_context() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_location_kind_counts() OWNER TO :"assets_owner";
GRANT SELECT (id, target_location_id, input_fingerprint, baseline_sha256)
    ON public.storage_migration_copies TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_copy_target_reference_footprint(uuid) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";

GRANT CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
GRANT SELECT (
    async_job_id, requested_by, window_id, window_revision, task_id, source_location_id,
    source, limits, handling, authority, input_fingerprint, maintenance_revision,
    operation_id, status, created_at, started_at, completed_at, cancel_requested_at,
    error_code, complete, counts, report, report_sha256, report_size_bytes
), INSERT (
    async_job_id, requested_by, window_id, window_revision, task_id, source_location_id,
    source, limits, handling, authority, input_fingerprint, maintenance_revision, status
), UPDATE (
    operation_id, status, started_at, completed_at, cancel_requested_at,
    error_code, complete, counts, report, report_sha256, report_size_bytes
) ON public.storage_migration_inventories TO :"assets_owner";
GRANT SELECT (
    id, parent_copy_id, target_location_id, state, revision, source_gate_revision,
    source_config_revision, created_by, created_at, updated_at, source_location_id, source, target
), INSERT (
    id, parent_copy_id, target_location_id, state, revision, source_gate_revision,
    source_config_revision, created_by, created_at, updated_at, source_location_id, source, target
), UPDATE (state, revision, source_gate_revision, updated_at)
ON public.storage_migration_windows TO :"assets_owner";
GRANT SELECT (location_id, state, revision), UPDATE (state, revision, changed_at, changed_by)
ON public.storage_maintenance_gates TO :"assets_owner";
GRANT SELECT (id, location_id, revision, purpose), INSERT (id, location_id, revision, purpose, node_id)
ON public.storage_operations TO :"assets_owner";
GRANT SELECT (
    id, target_location_id, baseline_sha256, target, last_attempt_id, source,
    window_id, window_revision, parent_copy_id
), UPDATE (baseline_sha256) ON public.storage_migration_copies TO :"assets_owner";
GRANT SELECT (
    id, async_job_id, copy_id, operation, task_id, status, requested_by, authority,
    cancel_requested_at, source_operation_id, scope
), UPDATE (status) ON public.storage_migration_copy_attempts TO :"assets_owner";
GRANT SELECT (window_id, state) ON public.storage_migration_finalizations TO :"assets_owner";
GRANT USAGE ON SCHEMA yoao_jobs_v1 TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.read_job(uuid), yoao_jobs_v1.read_job_for_share_nowait(uuid) TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._migration_inventory_job_payload(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_migration_inventory(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_migration_inventory_report(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_inventory_history(integer,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_inventory_history_count() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_window_has_active_inventory(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_active_migration_inventory() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.queue_migration_inventory(uuid,uuid,text,uuid,integer,integer,jsonb,jsonb,jsonb,jsonb,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.claim_migration_inventory(uuid,text,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.request_migration_inventory_cancel(uuid,text,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.attach_migration_inventory_operation(uuid,text,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.end_migration_inventory(uuid,text,text,text,timestamptz,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.seal_migration_inventory(uuid,text,timestamptz,bytea,text,boolean,jsonb,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_migration_window(uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_window_history(integer,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_window_history_count() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_window_open_operations(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_window_exists() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.open_migration_window(uuid,uuid,uuid,integer,jsonb,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.transition_migration_window(uuid,integer,text,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.bump_migration_window_finalization_revision(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.register_migration_window_source_read(uuid,integer,integer,text,uuid,text,uuid,jsonb,jsonb,uuid,text) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
GRANT SELECT (id, window_id, state, revision, window_revision, source_location_id, native, copy_id, inventory_job_id, evidence, error_code, created_at, updated_at, sealed_at, invalidated_at),
      UPDATE (state, revision, updated_at, error_code)
    ON public.storage_migration_finalizations TO :"assets_owner";
GRANT SELECT (id, copy_id, source_location_id, target_location_id, state, revision, source, target, evidence),
      UPDATE (state, revision, updated_at, withdrawing_at)
    ON public.storage_migration_read_publications TO :"assets_owner";
GRANT SELECT (publication_id, source_object_id, endpoint_url, bucket, object_key, source_version_id, source_etag, sha256, size_bytes, is_latest, target_key, target_version_id, target_etag, target_content_sha256, nonce, creation_plan_uuid)
    ON public.storage_migration_read_routes TO :"assets_owner";
GRANT SELECT (target_location_id, state) ON public.storage_migration_cutovers TO :"assets_owner";
GRANT SELECT (id, window_id, source, handling, source_report_sha256) ON public.storage_migration_copies TO :"assets_owner";
GRANT SELECT (id, parent_copy_id, source_location_id) ON public.storage_migration_windows TO :"assets_owner";
GRANT SELECT (copy_id, status, source_operation_id, target_operation_id) ON public.storage_migration_copy_attempts TO :"assets_owner";
GRANT SELECT (id, location_id) ON public.storage_operations TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_finalization_for_window(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_finalization_blocks_window(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_finalization_blocks_source_resume(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_read_publication_holds_copy(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_copy_is_frozen(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_cutover_blocks_target_resume(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._migration_checkpoint_changed(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_read_route_recheck(uuid,integer,text,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_read_route_lookup(uuid,text,text,text,text,bigint,boolean,text,text,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_read_prefix_inventory(uuid,integer,text,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_operator_copy_window_unsettled(uuid) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
GRANT USAGE ON SCHEMA yoao_assets_storage_v1 TO :"assets_owner";
GRANT SELECT ON yoao_assets_storage_v1.location_metadata, yoao_assets_storage_v1.connector_origins TO :"assets_owner";
GRANT USAGE ON SCHEMA yoao_jobs_v1 TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.read_job(uuid) TO :"assets_owner";
GRANT SELECT (async_job_id, complete, report, report_sha256, source, window_id, window_revision)
ON public.storage_migration_inventories TO :"assets_owner";
GRANT SELECT (id, location_id, node_id, purpose, revision), UPDATE (id), DELETE
ON public.storage_operations TO :"assets_owner";
GRANT SELECT (id, window_id, window_revision, parent_copy_id, generation, parent_evidence_sha256,
    input_fingerprint, source_inventory_job_id, source_report, source_report_sha256, source_complete,
    source, target_location_id, target, handling, limits, catalog, catalog_sha256, baseline,
    baseline_sha256, summary, last_attempt_id, delta_sequence, delta_bytes, created_at, updated_at),
    INSERT (id, window_id, window_revision, parent_copy_id, generation, parent_evidence_sha256,
    input_fingerprint, source_inventory_job_id, source_report, source_report_sha256, source_complete,
    source, target_location_id, target, handling, limits),
    UPDATE (catalog, catalog_sha256, baseline, baseline_sha256, summary, last_attempt_id,
    delta_sequence, delta_bytes, updated_at)
ON public.storage_migration_copies TO :"assets_owner";
GRANT SELECT (id, copy_id, async_job_id, task_id, requested_by, authority, scope, operation,
    retry_failed, retry_object_ids, status, source_operation_id, target_operation_id, process,
    created_at, started_at, completed_at, cancel_requested_at, error_code, result),
    INSERT (id, copy_id, async_job_id, task_id, requested_by, authority, scope, operation,
    retry_failed, retry_object_ids, status),
    UPDATE (status, started_at, completed_at, cancel_requested_at, error_code, result, process,
    source_operation_id, target_operation_id)
ON public.storage_migration_copy_attempts TO :"assets_owner";
GRANT SELECT (copy_id, object_id, sequence, data, digest, size_bytes),
    INSERT (copy_id, object_id, sequence, data, digest, size_bytes),
    UPDATE (sequence, data, digest, size_bytes)
ON public.storage_migration_copy_deltas TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_migration_copy_planning() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_copy_plan(uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_migration_copy_by_input(text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_copy_private_inputs(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_copy_attempt(uuid,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_copy_successor(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_copy_history(integer,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_copy_target_candidates(integer,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.freeze_migration_copy_plan(uuid,uuid,text,text,jsonb,uuid,jsonb,jsonb,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_copy_parent_settlement(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.reserve_migration_copy_successor(uuid,uuid,uuid,text,text,text,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_copy_failed_selection(uuid,text[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.queue_migration_copy_attempt(uuid,uuid,uuid,uuid,text,uuid,uuid,text,jsonb,text,boolean,text[],boolean,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.claim_migration_copy_attempt(uuid,uuid,uuid,text,jsonb,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.attach_migration_copy_permits(uuid,uuid,uuid,text,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_copy_permits_open(uuid,uuid,text,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.settle_migration_copy_process(uuid,uuid,text,text,jsonb,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.request_migration_copy_cancel(uuid,uuid,uuid,text,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_migration_copy_attempt(uuid,uuid,uuid,text,text,text,text,jsonb,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.publish_migration_copy_plan(uuid,uuid,uuid,text,bytea,bytea,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_migration_copy_delta(uuid,uuid,uuid,text,text,text,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_copy_delta_page(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_copy_failed_objects(uuid,integer,integer) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
