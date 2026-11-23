GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.migration_inventory_revisions() TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.migration_inventory_locations(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.migration_inventory_bindings(text,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.migration_copy_target_reference_count(uuid) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.inventory_prediction_input_leases(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.inventory_prediction_input_leases_objects(uuid,text,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.inventory_image_pyramid_outputs(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.inventory_image_pyramid_outputs_objects(uuid,text,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.inventory_video_derivative_outputs(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.inventory_video_derivative_outputs_objects(uuid,text,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.inventory_managed_original_creations(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.inventory_managed_original_retirements(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.inventory_dataset_asset_locations(uuid,uuid,text,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.inventory_task_originals(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.inventory_dataset_auxiliary_originals(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.inventory_dataset_source_previews(uuid,text,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.inventory_original_cache_entries(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.inventory_thumbnail_outputs(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.usage_prediction_input_leases(uuid[]) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.usage_image_pyramid_outputs(uuid[]) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.usage_video_derivative_outputs(uuid[]) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.usage_original_cache_entries(uuid[]) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.usage_original_and_preview_claims(uuid,jsonb,text,text,text,text[],uuid[],uuid[]) TO :"application_role";

GRANT USAGE ON SCHEMA yoao_audit_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.inventory_audit_archive_outputs(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.usage_audit_archive_outputs(uuid[]) TO :"application_role";

GRANT USAGE ON SCHEMA yoao_diagnostics_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_diagnostics_v1.inventory_bug_attachment_uploads(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_diagnostics_v1.usage_bug_attachment_uploads(uuid[]) TO :"application_role";

GRANT USAGE ON SCHEMA yoao_identity_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.inventory_avatar_uploads(text,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.usage_avatar_uploads(text[]) TO :"application_role";

GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.inventory_comment_attachment_uploads(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.inventory_raster_mask_objects(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.inventory_mask_format_uploads(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.inventory_mask_format_imports(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.usage_comment_attachment_uploads(uuid[]) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.usage_raster_mask_objects(uuid[]) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.usage_mask_format_uploads(text,text,text[]) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.usage_mask_format_imports(text,text,text[]) TO :"application_role";

GRANT USAGE ON SCHEMA yoao_learning_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.inventory_training_dataset_versions(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.inventory_training_runs(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.inventory_training_previews(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.inventory_sample_evaluation_runs(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.inventory_active_learning_rounds(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.inventory_training_storage_deletions(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.usage_training_dataset_versions(uuid[]) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.usage_training_runs(uuid[]) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.usage_training_previews(uuid[]) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.usage_sample_evaluation_runs(uuid[]) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.usage_active_learning_rounds(uuid[]) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.usage_training_storage_deletions(text,text[]) TO :"application_role";

GRANT USAGE ON SCHEMA yoao_workspace_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_workspace_v1.inventory_guide_asset_uploads(uuid,integer) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_workspace_v1.usage_guide_asset_uploads(uuid[]) TO :"application_role";

GRANT EXECUTE ON FUNCTION yoao_assets_v1.usage_inventory_status() TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.usage_snapshot_facts(text[]) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.lock_usage_inventory(timestamptz) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.lock_expired_usage_inventory(timestamptz) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.start_usage_inventory(uuid,integer,jsonb,jsonb,timestamptz,timestamptz) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.claim_usage_inventory(uuid,integer,timestamptz) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.begin_usage_inventory_page(uuid,integer,integer,jsonb) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.record_usage_inventory_page(uuid,integer,integer,jsonb,timestamptz,text,text,bigint,bigint,jsonb,timestamptz,timestamptz) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.release_usage_inventory_chunk(uuid,integer,timestamptz) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.finish_usage_inventory(uuid,integer,text,text) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.usage_schedule_summary() TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.lock_active_usage_schedule() TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.configure_usage_schedule(boolean,integer,jsonb,jsonb,timestamptz) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.pause_usage_schedule(text) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.advance_usage_schedule(timestamptz) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.begin_usage_mutation(uuid,text,text,text,text,text,uuid,text,text,text,jsonb) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.publish_usage_mutation(uuid,boolean,boolean,bigint,text,bigint,jsonb) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.stage_usage_inventory_objects(uuid,text,jsonb,timestamptz,timestamptz) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.reconcile_usage_bucket(text,uuid,timestamptz,timestamptz) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.discard_usage_staging(uuid,text) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.usage_mutation_summary(text[]) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.usage_reconciliation_facts(text[]) TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.migration_inventory_binding_context() TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.migration_location_kind_counts() TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.migration_copy_target_reference_footprint(uuid) TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.read_migration_inventory(uuid,boolean),
    yoao_assets_v1.read_migration_inventory_report(uuid,boolean),
    yoao_assets_v1.migration_inventory_history(integer,integer),
    yoao_assets_v1.migration_inventory_history_count(),
    yoao_assets_v1.lock_active_migration_inventory(),
    yoao_assets_v1.queue_migration_inventory(uuid,uuid,text,uuid,integer,integer,jsonb,jsonb,jsonb,jsonb,text),
    yoao_assets_v1.claim_migration_inventory(uuid,text,timestamptz),
    yoao_assets_v1.request_migration_inventory_cancel(uuid,text,timestamptz),
    yoao_assets_v1.attach_migration_inventory_operation(uuid,text,uuid),
    yoao_assets_v1.end_migration_inventory(uuid,text,text,text,timestamptz,text),
    yoao_assets_v1.seal_migration_inventory(uuid,text,timestamptz,bytea,text,boolean,jsonb,text),
    yoao_assets_v1.read_migration_window(uuid,text),
    yoao_assets_v1.migration_window_history(integer,integer),
    yoao_assets_v1.migration_window_history_count(),
    yoao_assets_v1.migration_window_open_operations(uuid),
    yoao_assets_v1.migration_window_exists(),
    yoao_assets_v1.open_migration_window(uuid,uuid,uuid,integer,jsonb,jsonb),
    yoao_assets_v1.transition_migration_window(uuid,integer,text,uuid),
    yoao_assets_v1.register_migration_window_source_read(uuid,integer,integer,text,uuid,text,uuid,jsonb,jsonb,uuid,text)
TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.migration_finalization_for_window(uuid),
    yoao_assets_v1.migration_finalization_blocks_window(uuid,boolean),
    yoao_assets_v1.migration_finalization_blocks_source_resume(uuid),
    yoao_assets_v1.migration_read_publication_holds_copy(uuid),
    yoao_assets_v1.migration_copy_is_frozen(uuid),
    yoao_assets_v1.migration_cutover_blocks_target_resume(uuid),
    yoao_assets_v1.migration_read_route_recheck(uuid,integer,text,boolean),
    yoao_assets_v1.migration_read_route_lookup(uuid,text,text,text,text,bigint,boolean,text,text,boolean),
    yoao_assets_v1.migration_read_prefix_inventory(uuid,integer,text,text)
TO :"application_role";

GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.lock_migration_copy_planning(),
    yoao_assets_v1.migration_copy_plan(uuid,text),
    yoao_assets_v1.lock_migration_copy_by_input(text),
    yoao_assets_v1.migration_copy_private_inputs(uuid),
    yoao_assets_v1.migration_copy_attempt(uuid,uuid,text),
    yoao_assets_v1.migration_copy_successor(uuid),
    yoao_assets_v1.migration_copy_history(integer,integer),
    yoao_assets_v1.migration_copy_target_candidates(integer,integer),
    yoao_assets_v1.freeze_migration_copy_plan(uuid,uuid,text,text,jsonb,uuid,jsonb,jsonb,jsonb),
    yoao_assets_v1.migration_copy_parent_settlement(uuid),
    yoao_assets_v1.reserve_migration_copy_successor(uuid,uuid,uuid,text,text,text,timestamptz),
    yoao_assets_v1.migration_copy_failed_selection(uuid,text[]),
    yoao_assets_v1.queue_migration_copy_attempt(uuid,uuid,uuid,uuid,text,uuid,uuid,text,jsonb,text,boolean,text[],boolean,timestamptz),
    yoao_assets_v1.claim_migration_copy_attempt(uuid,uuid,uuid,text,jsonb,timestamptz),
    yoao_assets_v1.attach_migration_copy_permits(uuid,uuid,uuid,text,uuid,uuid),
    yoao_assets_v1.migration_copy_permits_open(uuid,uuid,text,boolean),
    yoao_assets_v1.settle_migration_copy_process(uuid,uuid,text,text,jsonb,timestamptz),
    yoao_assets_v1.request_migration_copy_cancel(uuid,uuid,uuid,text,timestamptz),
    yoao_assets_v1.finish_migration_copy_attempt(uuid,uuid,uuid,text,text,text,text,jsonb,timestamptz),
    yoao_assets_v1.publish_migration_copy_plan(uuid,uuid,uuid,text,bytea,bytea,jsonb),
    yoao_assets_v1.record_migration_copy_delta(uuid,uuid,uuid,text,text,text,timestamptz),
    yoao_assets_v1.migration_copy_delta_page(uuid,integer),
    yoao_assets_v1.migration_copy_failed_objects(uuid,integer,integer)
TO :"application_role";
