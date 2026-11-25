GRANT USAGE ON SCHEMA
    public,
    yoao_assets_v1,
    yoao_audit_v1,
    yoao_identity_accounts_v1,
    yoao_identity_admin_passwords_v1,
    yoao_identity_briefs_v1,
    yoao_identity_deactivation_v1,
    yoao_identity_deletions_v1,
    yoao_identity_directory_v1,
    yoao_identity_groups_v1,
    yoao_identity_invitations_v1,
    yoao_identity_keys_v1,
    yoao_identity_offboarding_v1,
    yoao_identity_platform_roles_v1,
    yoao_identity_recovery_v1,
    yoao_identity_sessions_v1,
    yoao_identity_suspensions_v1,
    yoao_identity_v1,
    yoao_inference_backend_endpoint_v1,
    yoao_inference_gpu_caller_v1,
    yoao_inference_health_data_v1,
    yoao_inference_health_observation_v1,
    yoao_inference_pipelines_v1,
    yoao_inference_routing_v1,
    yoao_inference_topology_v1,
    yoao_inference_v1,
    yoao_insights_v1,
    yoao_jobs_v1,
    yoao_labeling_class_query_v1,
    yoao_labeling_v1,
    yoao_learning_v1,
    yoao_notifications_v1,
    yoao_workspace_configuration_v1,
    yoao_workspace_directory_query_v1,
    yoao_workspace_membership_v1,
    yoao_workspace_owner_transfer_v1,
    yoao_workspace_project_query_v1,
    yoao_workspace_template_query_v1,
    yoao_workspace_v1
    TO :"application_role";

GRANT SELECT ON
    public.v_annotation_feedback_unified,
    yoao_assets_v1.labeling_dataset_item_facts,
    yoao_assets_v1.spatial_item_references,
    yoao_assets_v1.spatial_scene_references,
    yoao_audit_v1.audit_daily_facts,
    yoao_audit_v1.audit_events,
    yoao_identity_v1.account_profiles,
    yoao_identity_v1.invitation_audit_targets,
    yoao_jobs_v1.notification_scope_facts,
    yoao_labeling_v1.annotation_comment_facts,
    yoao_labeling_v1.annotation_draft_rows,
    yoao_labeling_v1.annotation_feedback_facts,
    yoao_labeling_v1.annotation_rows,
    yoao_labeling_v1.batch_rows,
    yoao_labeling_v1.failed_prediction_rows,
    yoao_labeling_v1.prediction_meta_rows,
    yoao_labeling_v1.prediction_rows,
    yoao_labeling_v1.project_catalog_counters,
    yoao_labeling_v1.scene_track_interval_rows,
    yoao_labeling_v1.scene_track_rows,
    yoao_labeling_v1.task_dataset_item_link_rows,
    yoao_labeling_v1.task_event_facts,
    yoao_labeling_v1.task_lock_rows,
    yoao_labeling_v1.task_rows,
    yoao_labeling_v1.video_segment_rows,
    yoao_labeling_v1.video_track_quality_run_rows,
    yoao_labeling_v1.video_tracker_job_rows,
    yoao_workspace_v1.project_facts,
    yoao_workspace_v1.project_member_facts
    TO :"application_role";

GRANT SELECT, INSERT ON yoao_jobs_v1.async_job_creation TO :"application_role";

GRANT EXECUTE ON FUNCTION
    public.fixture_native_export_cleanup_eligible(uuid),
    public.fixture_native_export_lifetime(uuid,uuid),
    public.image_export_scope_matches(uuid,uuid),
    public.prepare_image_export_scope(uuid,uuid,uuid,uuid,uuid[])
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_assets_v1._lock_accepted_video_input_pin(uuid,uuid,jsonb,uuid),
    yoao_assets_v1.accept_dataset_import(uuid,uuid,uuid,uuid,jsonb,jsonb,jsonb),
    yoao_assets_v1.append_operator_items(uuid,uuid,jsonb),
    yoao_assets_v1.apply_pointcloud_preparation(uuid,jsonb,jsonb,jsonb),
    yoao_assets_v1.apply_spatial_layout(uuid,uuid,uuid,text,boolean,jsonb,jsonb),
    yoao_assets_v1.assign_spatial_items(uuid,uuid,jsonb),
    yoao_assets_v1.associate_project(uuid,uuid,uuid),
    yoao_assets_v1.begin_dataset_ingest(uuid,uuid,uuid,jsonb),
    yoao_assets_v1.begin_image_original_actor(uuid,uuid,uuid,text,text),
    yoao_assets_v1.begin_image_original_system(text,uuid,text,text),
    yoao_assets_v1.begin_media_original_actor(uuid,uuid,uuid,text,text),
    yoao_assets_v1.begin_media_original_system(uuid,text,text),
    yoao_assets_v1.begin_native_pyramid_read(uuid,uuid,uuid,integer,text,uuid,uuid),
    yoao_assets_v1.begin_spatial_calibration_original(uuid,uuid,text),
    yoao_assets_v1.begin_tracker_control_original(uuid,uuid,uuid,uuid,text,text,text,text),
    yoao_assets_v1.begin_tracker_prepare_original(uuid,uuid,uuid,uuid,text,text,text),
    yoao_assets_v1.begin_video_control_original(uuid,uuid,uuid,uuid,text,text,text),
    yoao_assets_v1.begin_video_member_original(uuid,uuid,uuid,text,bigint,uuid,text,text),
    yoao_assets_v1.bind_operator_auxiliary_item(uuid,uuid,uuid),
    yoao_assets_v1.bind_operator_auxiliary_scene(uuid,uuid,uuid),
    yoao_assets_v1.cancel_dataset_import(uuid,uuid),
    yoao_assets_v1.cancel_image_media(uuid,uuid,uuid),
    yoao_assets_v1.cancel_video_media(uuid,uuid,uuid),
    yoao_assets_v1.capture_accepted_image_input_manifest(uuid,uuid,uuid,jsonb),
    yoao_assets_v1.capture_accepted_video_manifest(uuid,uuid,uuid,jsonb),
    yoao_assets_v1.claim_dataset_import_delivery(uuid,integer),
    yoao_assets_v1.claim_dataset_source_ingest_delivery(uuid,integer),
    yoao_assets_v1.classify_fixture_original_reclaim(uuid,text),
    yoao_assets_v1.close_operator_storage_operation(uuid,uuid,boolean),
    yoao_assets_v1.complete_dataset_import_delivery(uuid,uuid),
    yoao_assets_v1.complete_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb),
    yoao_assets_v1.complete_dataset_source_ingest_delivery(uuid,uuid),
    yoao_assets_v1.complete_dataset_upload(uuid,uuid,uuid,uuid,bigint,text,integer,integer),
    yoao_assets_v1.complete_legacy_dataset_upload(uuid,uuid,uuid,uuid,text,bigint,text,integer,integer),
    yoao_assets_v1.complete_native_pyramid_read(uuid,uuid),
    yoao_assets_v1.complete_storage_deletion(uuid,uuid),
    yoao_assets_v1.create_catalog_dataset(uuid,uuid,jsonb),
    yoao_assets_v1.create_operator_image_dataset(uuid,uuid,text,text),
    yoao_assets_v1.create_spatial_scene(uuid,uuid,uuid,text,text,jsonb,uuid),
    yoao_assets_v1.create_storage_connection(uuid,uuid,jsonb,jsonb,text,text,jsonb,jsonb),
    yoao_assets_v1.dataset_import_delivery_target_is_current(text,uuid),
    yoao_assets_v1.delete_dataset(uuid,uuid,uuid,text,jsonb),
    yoao_assets_v1.delete_dataset_item(uuid,uuid,uuid,uuid,text,jsonb),
    yoao_assets_v1.delete_storage_connection(uuid,uuid,uuid,jsonb),
    yoao_assets_v1.discard_dataset_upload(uuid,uuid,uuid,text),
    yoao_assets_v1.ensure_operator_nuscenes_dataset(uuid,uuid,jsonb),
    yoao_assets_v1.ensure_video_media(uuid,uuid,uuid,jsonb),
    yoao_assets_v1.fail_operator_original(uuid,text,text,jsonb),
    yoao_assets_v1.fence_cross_frame_job_dataset_scope(uuid,uuid,uuid,jsonb),
    yoao_assets_v1.find_accepted_image_input(uuid,uuid,uuid),
    yoao_assets_v1.find_dataset_upload_duplicate(uuid,uuid,uuid,text),
    yoao_assets_v1.finish_dataset_ingest(uuid,uuid,uuid,jsonb),
    yoao_assets_v1.finish_image_original_read(uuid,uuid),
    yoao_assets_v1.finish_image_original_system(uuid,uuid),
    yoao_assets_v1.finish_media_original_read(uuid,uuid),
    yoao_assets_v1.finish_media_original_system(uuid,uuid),
    yoao_assets_v1.finish_spatial_calibration_original(uuid,uuid),
    yoao_assets_v1.finish_tracker_control_original(uuid,uuid),
    yoao_assets_v1.finish_video_control_original(uuid,uuid),
    yoao_assets_v1.fixture_media_ordinary_gc(text,uuid,integer),
    yoao_assets_v1.fixture_media_owner_exists(text,uuid),
    yoao_assets_v1.get_catalog_dataset(uuid,uuid),
    yoao_assets_v1.get_spatial_scene(uuid,uuid),
    yoao_assets_v1.guard_spatial_actor(uuid,text),
    yoao_assets_v1.invalidate_image_media(uuid,uuid,uuid,jsonb),
    yoao_assets_v1.invalidate_video_media(uuid,uuid,uuid,jsonb),
    yoao_assets_v1.is_fixture_original_source(text,text),
    yoao_assets_v1.is_fixture_spatial_dataset(uuid),
    yoao_assets_v1.legacy_pyramid_gc_eligible(text,uuid,integer),
    yoao_assets_v1.list_catalog_datasets(uuid,text,text,boolean,integer,bigint),
    yoao_assets_v1.list_catalog_items(uuid,uuid,integer,bigint),
    yoao_assets_v1.list_dataset_projects(uuid,uuid),
    yoao_assets_v1.list_project_datasets(uuid,uuid),
    yoao_assets_v1.list_spatial_scenes(uuid,uuid,timestamptz,uuid,integer),
    yoao_assets_v1.list_storage_connections(uuid,timestamptz,uuid,integer),
    yoao_assets_v1.lock_accepted_image_input(uuid,uuid,uuid),
    yoao_assets_v1.lock_accepted_video_frame(uuid,uuid,uuid,uuid,text,bigint,jsonb,jsonb),
    yoao_assets_v1.lock_accepted_video_input(uuid,uuid,uuid,uuid,text),
    yoao_assets_v1.lock_current_association(uuid,uuid),
    yoao_assets_v1.lock_dataset_reference(uuid),
    yoao_assets_v1.lock_dataset_upload(uuid,uuid,uuid),
    yoao_assets_v1.lock_legacy_dataset_upload(uuid,uuid,uuid,uuid),
    yoao_assets_v1.lock_scene_source_references(uuid[]),
    yoao_assets_v1.lock_spatial_calibration_facts(uuid,uuid,text,uuid,jsonb),
    yoao_assets_v1.mark_operator_original_writing(uuid),
    yoao_assets_v1.mutate_spatial_scene(uuid,uuid,uuid,jsonb,jsonb),
    yoao_assets_v1.observe_image_source(uuid,uuid,uuid,text,jsonb),
    yoao_assets_v1.observe_media_source(uuid,uuid,uuid,text,jsonb),
    yoao_assets_v1.open_operator_storage_operation(uuid),
    yoao_assets_v1.operator_auxiliary_owner(uuid,text,jsonb,uuid),
    yoao_assets_v1.operator_auxiliary_pointer(uuid,uuid,uuid),
    yoao_assets_v1.operator_dataset_state(uuid),
    yoao_assets_v1.operator_item_original_facts(uuid,uuid),
    yoao_assets_v1.operator_scene_state(uuid,uuid),
    yoao_assets_v1.patch_operator_items(uuid,uuid,jsonb),
    yoao_assets_v1.pending_storage_deletions(uuid,integer),
    yoao_assets_v1.prepare_accepted_image_input_manifest_member(uuid,uuid,uuid,text,bigint,uuid,text,jsonb,text),
    yoao_assets_v1.prepare_accepted_video_member(uuid,uuid,uuid,text,bigint,uuid,text,bigint,jsonb,text),
    yoao_assets_v1.prepare_dataset_import(uuid,uuid,uuid),
    yoao_assets_v1.prepare_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb),
    yoao_assets_v1.prepare_image_media(uuid,uuid,uuid,uuid,text,text,jsonb,boolean),
    yoao_assets_v1.prepare_owned_pyramid(uuid,uuid,uuid,uuid,text,text,jsonb,boolean,jsonb),
    yoao_assets_v1.prepare_video_tracker_input(uuid,uuid,uuid,uuid,text,text,bigint,jsonb,text),
    yoao_assets_v1.publish_image_pyramid_thumbnail(uuid,integer,uuid),
    yoao_assets_v1.publish_operator_auxiliary(uuid),
    yoao_assets_v1.publish_operator_item_original(jsonb),
    yoao_assets_v1.read_accepted_image_input(uuid,uuid,uuid),
    yoao_assets_v1.read_accepted_image_input_manifest(uuid,uuid,uuid),
    yoao_assets_v1.read_accepted_image_input_manifest_page(uuid,uuid,uuid,text,text,bigint,integer),
    yoao_assets_v1.read_accepted_image_input_work(uuid,uuid,uuid),
    yoao_assets_v1.read_accepted_video_frame(uuid,uuid,uuid,uuid,text,bigint),
    yoao_assets_v1.read_accepted_video_input(uuid,uuid,uuid,uuid),
    yoao_assets_v1.read_accepted_video_input_work(uuid,uuid,uuid,uuid),
    yoao_assets_v1.read_accepted_video_manifest(uuid,uuid,uuid),
    yoao_assets_v1.read_accepted_video_manifest_page(uuid,uuid,uuid,text,text,bigint,integer),
    yoao_assets_v1.read_accepted_video_times(uuid,uuid,uuid,uuid,text,bigint,integer),
    yoao_assets_v1.read_dataset_ingest_layout(uuid,uuid,uuid,uuid,integer),
    yoao_assets_v1.read_dataset_source_authority(uuid,boolean),
    yoao_assets_v1.read_image_generation(uuid,uuid,uuid,integer,text),
    yoao_assets_v1.read_image_source(uuid,uuid,uuid),
    yoao_assets_v1.read_image_summaries(uuid,jsonb,text),
    yoao_assets_v1.read_image_usage(),
    yoao_assets_v1.read_image_work(uuid,uuid),
    yoao_assets_v1.read_internal_image_work(uuid),
    yoao_assets_v1.read_internal_video_frame(uuid,integer,integer,text),
    yoao_assets_v1.read_legacy_dataset_upload(uuid,uuid,uuid,uuid),
    yoao_assets_v1.read_media_preferences(),
    yoao_assets_v1.read_media_source(uuid,uuid,uuid),
    yoao_assets_v1.read_media_summaries(uuid[]),
    yoao_assets_v1.read_operator_items(uuid,uuid,integer),
    yoao_assets_v1.read_pointcloud_preparation(uuid,uuid,integer),
    yoao_assets_v1.read_spatial_calibrations(uuid[],boolean,uuid,integer,integer),
    yoao_assets_v1.read_spatial_camera_target(uuid,uuid,text),
    yoao_assets_v1.read_spatial_items(uuid,uuid[],uuid,integer),
    yoao_assets_v1.read_spatial_layout(uuid,uuid,uuid,uuid,integer),
    yoao_assets_v1.read_spatial_layout_receipt(uuid,uuid,uuid,text,boolean),
    yoao_assets_v1.read_spatial_pose_pairs(jsonb),
    yoao_assets_v1.read_spatial_poses(uuid,uuid,integer,integer),
    yoao_assets_v1.read_storage_connection(uuid,uuid),
    yoao_assets_v1.read_storage_connection_policy(uuid,jsonb),
    yoao_assets_v1.read_storage_connection_receipt(uuid,uuid,text,uuid,jsonb),
    yoao_assets_v1.read_storage_fault_facts(uuid,uuid,text,boolean,boolean),
    yoao_assets_v1.read_storage_observation_rates(uuid,uuid),
    yoao_assets_v1.read_video_derivative_links(uuid[]),
    yoao_assets_v1.read_video_failures(uuid,integer,bigint),
    yoao_assets_v1.read_video_media_totals(uuid[]),
    yoao_assets_v1.read_video_samples(uuid,uuid,uuid,integer,integer,integer),
    yoao_assets_v1.read_video_timetable(uuid,uuid,uuid,integer,integer,integer,integer),
    yoao_assets_v1.read_video_tracker_input(uuid,uuid,uuid),
    yoao_assets_v1.read_video_work(uuid,uuid),
    yoao_assets_v1.record_operator_original_prepared(uuid,jsonb),
    yoao_assets_v1.record_operator_original_verified(uuid,jsonb),
    yoao_assets_v1.record_operator_original_written(uuid,jsonb),
    yoao_assets_v1.record_spatial_calibration_provenance(uuid,uuid,jsonb),
    yoao_assets_v1.record_storage_connection_test(uuid,uuid,uuid,text,text,jsonb,jsonb,jsonb),
    yoao_assets_v1.record_storage_fault_observation(bigint,bytea,uuid,uuid,text,text,jsonb,jsonb,text),
    yoao_assets_v1.record_storage_observation_rate(uuid,uuid,text,boolean),
    yoao_assets_v1.recount_operator_items(uuid,uuid),
    yoao_assets_v1.reference_video_tracker_source(uuid,uuid,uuid,uuid,text,text),
    yoao_assets_v1.request_video_media(uuid,uuid,uuid,jsonb,boolean),
    yoao_assets_v1.require_accepted_video_frame(uuid,uuid,uuid,uuid,text,bigint),
    yoao_assets_v1.reserve_accepted_image_input(uuid,uuid,uuid,text,jsonb),
    yoao_assets_v1.reserve_dataset_upload(uuid,uuid,uuid,text,text),
    yoao_assets_v1.reserve_storage_fault_observation(uuid,uuid,text,text,jsonb,jsonb),
    yoao_assets_v1.resolve_internal_image_source(text,uuid,text,jsonb),
    yoao_assets_v1.resolve_internal_media_source(uuid,text,jsonb),
    yoao_assets_v1.retry_image_media(uuid,uuid,uuid,uuid,text,text,jsonb,boolean),
    yoao_assets_v1.retry_owned_pyramid(uuid,uuid,uuid,uuid,text,text,jsonb,boolean,jsonb),
    yoao_assets_v1.retry_video_media(uuid,uuid,jsonb),
    yoao_assets_v1.scan_image_backfill(jsonb,text,uuid,integer),
    yoao_assets_v1.scan_video_backfill(jsonb,uuid,integer),
    yoao_assets_v1.schedule_image_media(uuid,text,uuid,jsonb,boolean),
    yoao_assets_v1.schedule_video_media(uuid,uuid,jsonb,boolean),
    yoao_assets_v1.seal_accepted_image_input_manifest(uuid,uuid,uuid,text),
    yoao_assets_v1.seal_accepted_video_manifest(uuid,uuid,uuid,text),
    yoao_assets_v1.set_spatial_dataset_axis(uuid,uuid,text),
    yoao_assets_v1.set_spatial_scene_source(uuid,uuid,text,jsonb),
    yoao_assets_v1.stage_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb),
    yoao_assets_v1.storage_fault_location_visible(uuid,uuid),
    yoao_assets_v1.storage_fault_page(uuid,uuid,text,integer,integer),
    yoao_assets_v1.update_catalog_dataset(uuid,uuid,uuid,jsonb),
    yoao_assets_v1.update_spatial_calibration(uuid,uuid,uuid,text,integer,text,jsonb,text,text,jsonb,jsonb),
    yoao_assets_v1.update_storage_connection_authorized(uuid,uuid,uuid,jsonb,jsonb,text,text,text,jsonb,jsonb,uuid,text[],jsonb),
    yoao_assets_v1.upsert_spatial_poses(uuid,uuid,jsonb)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_audit_v1.append_events(jsonb),
    yoao_audit_v1.archive_output_has_ready(integer,integer),
    yoao_audit_v1.archive_output_history(),
    yoao_audit_v1.archive_output_pending(uuid),
    yoao_audit_v1.archive_output_ready_list(),
    yoao_audit_v1.archive_output_ready_month(integer,integer),
    yoao_audit_v1.archive_output_save_candidate(uuid,uuid,date,bigint,bigint,bigint,bigint,text,bigint,text,jsonb,timestamptz),
    yoao_audit_v1.platform_maintenance(text,date,bigint,bigint,text,uuid,timestamptz),
    yoao_audit_v1.query_events(pg_catalog.jsonb)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_identity_accounts_v1.account_policy(),
    yoao_identity_accounts_v1.apply_account_command(jsonb),
    yoao_identity_accounts_v1.mark_inactive_accounts_offline(timestamptz),
    yoao_identity_accounts_v1.prepare_account_command(jsonb)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_identity_admin_passwords_v1.admin_password_policy(),
    yoao_identity_admin_passwords_v1.execute_admin_password_command(jsonb)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_identity_briefs_v1.briefs_policy(),
    yoao_identity_briefs_v1.execute_briefs_command(jsonb)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_identity_deactivation_v1.deactivation_policy(),
    yoao_identity_deactivation_v1.due_policy(),
    yoao_identity_deactivation_v1.execute_deactivation_command(jsonb),
    yoao_identity_deactivation_v1.process_due_deactivations(jsonb)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_identity_deletions_v1.deletion_policy(),
    yoao_identity_deletions_v1.execute_deletion_command(jsonb)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_identity_directory_v1.directory_policy(),
    yoao_identity_directory_v1.execute_directory_command(jsonb)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_identity_groups_v1.execute_group_command(jsonb),
    yoao_identity_groups_v1.group_policy()
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_identity_invitations_v1.apply_invitation_command(jsonb),
    yoao_identity_invitations_v1.invitation_policy(),
    yoao_identity_invitations_v1.prepare_invitation_command(jsonb)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_identity_keys_v1.execute_key_command(jsonb),
    yoao_identity_keys_v1.key_policy()
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_identity_offboarding_v1.apply_offboarding_command(jsonb),
    yoao_identity_offboarding_v1.offboarding_policy(),
    yoao_identity_offboarding_v1.prepare_offboarding_command(jsonb)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_identity_platform_roles_v1.execute_platform_role_command(jsonb),
    yoao_identity_platform_roles_v1.platform_role_policy()
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_identity_recovery_v1.apply_recovery_command(jsonb),
    yoao_identity_recovery_v1.prepare_recovery_command(jsonb),
    yoao_identity_recovery_v1.read_registration_status(jsonb),
    yoao_identity_recovery_v1.recovery_policy()
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_identity_sessions_v1.execute_session_command(jsonb),
    yoao_identity_sessions_v1.read_login_candidate(text),
    yoao_identity_sessions_v1.session_policy()
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_identity_suspensions_v1.execute_suspension_command(jsonb),
    yoao_identity_suspensions_v1.suspension_policy()
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_identity_v1.advance_credential_generation(uuid),
    yoao_identity_v1.authentication_policy(),
    yoao_identity_v1.check_accepted_api_key(uuid,uuid,text[]),
    yoao_identity_v1.confirm_api_key_principal(uuid,text),
    yoao_identity_v1.create_provisioning_account(jsonb),
    yoao_identity_v1.create_seed_persona_account(jsonb),
    yoao_identity_v1.get_credential_generation(uuid),
    yoao_identity_v1.is_jti_revoked(uuid),
    yoao_identity_v1.lock_accounts_key_share(uuid[],boolean),
    yoao_identity_v1.lock_accounts_share(uuid[],boolean),
    yoao_identity_v1.lock_accounts_update(uuid[],boolean),
    yoao_identity_v1.lock_current_principal(uuid,text,uuid,bigint,timestamptz,text[]),
    yoao_identity_v1.lock_super_admin_invariant(),
    yoao_identity_v1.read_accepted_api_key_facts(uuid,uuid),
    yoao_identity_v1.read_actor_context(uuid,text),
    yoao_identity_v1.read_actor_facts(uuid),
    yoao_identity_v1.read_api_key_candidates(text,uuid,integer),
    yoao_identity_v1.read_jwt_principal(uuid,uuid,bigint,timestamptz,boolean),
    yoao_identity_v1.read_provisioning_account(text),
    yoao_identity_v1.read_scale_seed_annotator(uuid[]),
    yoao_identity_v1.read_scale_seed_super_admin(),
    yoao_identity_v1.retire_account_credentials(uuid),
    yoao_identity_v1.revoke_jti(uuid,timestamptz),
    yoao_identity_v1.super_admin_status(uuid)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_inference_backend_endpoint_v1.backend_endpoint_policy(),
    yoao_inference_backend_endpoint_v1.capture_backend_endpoint_subject(uuid,uuid,text,text),
    yoao_inference_backend_endpoint_v1.catalog_endpoint_rows(),
    yoao_inference_backend_endpoint_v1.controlled_model_credential_policy(uuid),
    yoao_inference_backend_endpoint_v1.health_backend_row(uuid,boolean),
    yoao_inference_backend_endpoint_v1.observation_registration_labels(text[]),
    yoao_inference_backend_endpoint_v1.stats_endpoint_rows()
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_inference_gpu_caller_v1.gpu_caller_policy(),
    yoao_inference_gpu_caller_v1.gpu_caller_step(text,text)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_inference_health_data_v1.apply_reconciliation(uuid,uuid,text,text,text,bigint,text,text),
    yoao_inference_health_data_v1.backend_row(uuid,boolean),
    yoao_inference_health_data_v1.health_data_policy(),
    yoao_inference_health_data_v1.lock_resource(text),
    yoao_inference_health_data_v1.member_pool(uuid),
    yoao_inference_health_data_v1.member_row(uuid),
    yoao_inference_health_data_v1.membership_row(uuid,text,boolean),
    yoao_inference_health_data_v1.observation_clock(),
    yoao_inference_health_data_v1.pool_row(uuid,uuid),
    yoao_inference_health_data_v1.try_promotion(),
    yoao_inference_health_data_v1.write_backend(uuid,text,timestamptz,text,boolean,boolean)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_inference_health_observation_v1.capture_health_observation_subject(uuid,uuid,text,text,double precision),
    yoao_inference_health_observation_v1.verify_health_observation(text,uuid,uuid,text)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_inference_pipelines_v1.pipeline_policy(),
    yoao_inference_pipelines_v1.pipeline_step(jsonb,text,jsonb,text[],text[])
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_inference_routing_v1.model_dispatch_step(text),
    yoao_inference_routing_v1.prediction_scope_reserve(text),
    yoao_inference_routing_v1.prediction_test_scope_reserve(text),
    yoao_inference_routing_v1.routing_policy(),
    yoao_inference_routing_v1.routing_step(text,text),
    yoao_inference_routing_v1.scope_retirement_step(text,text),
    yoao_inference_routing_v1.supersede_unbegun_scope(text),
    yoao_inference_routing_v1.video_execution_scope_step(text,text)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_inference_topology_v1.accept_drift(uuid,uuid,text,boolean,text),
    yoao_inference_topology_v1.authorize_plugin_routing_replacement(uuid,integer,uuid,text,text,text),
    yoao_inference_topology_v1.command_plugin_installation(uuid,integer,text,text),
    yoao_inference_topology_v1.create_manual_registry(uuid,text),
    yoao_inference_topology_v1.create_pool(uuid,text),
    yoao_inference_topology_v1.create_seed_registry(uuid,text,text,text),
    yoao_inference_topology_v1.create_singleton_member(uuid,uuid,uuid),
    yoao_inference_topology_v1.create_singleton_pool(uuid,uuid,text,text,text),
    yoao_inference_topology_v1.delete_empty_pool(uuid),
    yoao_inference_topology_v1.delete_project_associations(uuid),
    yoao_inference_topology_v1.delete_registry(uuid,text),
    yoao_inference_topology_v1.drain_member(uuid,uuid),
    yoao_inference_topology_v1.drift_review_rows(uuid,uuid),
    yoao_inference_topology_v1.enabled_project_ids(uuid[]),
    yoao_inference_topology_v1.lock_drift_acceptance_rows(uuid,uuid),
    yoao_inference_topology_v1.lock_empty_pool(uuid),
    yoao_inference_topology_v1.lock_member_upsert_rows(uuid,uuid),
    yoao_inference_topology_v1.lock_plugin_registry(uuid),
    yoao_inference_topology_v1.lock_repair_model_rows(uuid,uuid,uuid[]),
    yoao_inference_topology_v1.patch_pool(uuid,text),
    yoao_inference_topology_v1.patch_registry_auth_cas(uuid,text,text,bigint),
    yoao_inference_topology_v1.plugin_asset_approval(uuid),
    yoao_inference_topology_v1.plugin_installation(uuid),
    yoao_inference_topology_v1.plugin_installation_evidence(uuid),
    yoao_inference_topology_v1.plugin_installations(),
    yoao_inference_topology_v1.pool_catalog(),
    yoao_inference_topology_v1.pool_for_registry(uuid),
    yoao_inference_topology_v1.pool_members(uuid),
    yoao_inference_topology_v1.pool_reference(uuid),
    yoao_inference_topology_v1.prepare_plugin_installation(text),
    yoao_inference_topology_v1.project_assoc_for_registry(uuid,uuid),
    yoao_inference_topology_v1.project_backend_catalog(uuid),
    yoao_inference_topology_v1.project_backend_facts(uuid[]),
    yoao_inference_topology_v1.project_interactive_backend(uuid),
    yoao_inference_topology_v1.project_pool_catalog(uuid),
    yoao_inference_topology_v1.registry_auth_snapshot(uuid),
    yoao_inference_topology_v1.registry_catalog(),
    yoao_inference_topology_v1.registry_facts(uuid),
    yoao_inference_topology_v1.registry_names(uuid[]),
    yoao_inference_topology_v1.registry_reference(uuid),
    yoao_inference_topology_v1.registry_url_owner(text),
    yoao_inference_topology_v1.remove_member(uuid,uuid,text),
    yoao_inference_topology_v1.resume_member(uuid,uuid),
    yoao_inference_topology_v1.seed_registry_by_url(text),
    yoao_inference_topology_v1.set_plugin_asset_approval(uuid,integer,text),
    yoao_inference_topology_v1.set_project_pool_enablement(uuid,uuid,boolean,text),
    yoao_inference_topology_v1.set_project_registry_enablement(uuid,uuid,boolean,text),
    yoao_inference_topology_v1.singleton_pool(uuid),
    yoao_inference_topology_v1.topology_policy(),
    yoao_inference_topology_v1.upsert_member(uuid,uuid,integer,text),
    yoao_inference_topology_v1.validate_registry_delete(uuid)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_inference_v1.ai_mask_pool_reference_row(uuid)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_insights_v1.create_personal_dataset_view(uuid,uuid,text,jsonb),
    yoao_insights_v1.create_saved_task_view(uuid,uuid,text,text,text,jsonb,jsonb,jsonb),
    yoao_insights_v1.delete_personal_dataset_view(uuid,uuid,uuid),
    yoao_insights_v1.delete_saved_task_view(uuid,uuid,uuid),
    yoao_insights_v1.has_personal_dataset_view(uuid,uuid,uuid),
    yoao_insights_v1.list_personal_dataset_views(uuid,uuid),
    yoao_insights_v1.list_saved_task_views(uuid,uuid,text),
    yoao_insights_v1.prepare_personal_dataset_view(uuid,uuid),
    yoao_insights_v1.read_saved_task_view(uuid,uuid,uuid),
    yoao_insights_v1.update_personal_dataset_view(uuid,uuid,uuid,text,jsonb),
    yoao_insights_v1.update_saved_task_view(uuid,uuid,uuid,text,text,jsonb,jsonb,jsonb)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_jobs_v1.active_cross_frame_job(uuid,text),
    yoao_jobs_v1.apply_lifecycle(uuid,jsonb),
    yoao_jobs_v1.async_job_list_page(uuid,text,text[],text[],uuid,text,integer,integer),
    yoao_jobs_v1.async_job_list_total(uuid,text,text[],text[],uuid,text),
    yoao_jobs_v1.begin_prediction_mask_producer(jsonb,bigint,uuid),
    yoao_jobs_v1.bind_prediction_execution_snapshot(uuid,uuid,uuid),
    yoao_jobs_v1.bind_prediction_mask_object(uuid,uuid),
    yoao_jobs_v1.cancel_batch_predict_jobs(uuid,timestamptz),
    yoao_jobs_v1.cancel_task_creation_job(uuid,uuid),
    yoao_jobs_v1.claim_batch_result_execution(jsonb),
    yoao_jobs_v1.claim_prediction_retry_deliveries(jsonb),
    yoao_jobs_v1.claim_task_creation_job(uuid,text),
    yoao_jobs_v1.count_running_registry_predictions(uuid),
    yoao_jobs_v1.create_task_creation_job(uuid,uuid,uuid,uuid,uuid),
    yoao_jobs_v1.cross_frame_job_count(uuid,text),
    yoao_jobs_v1.cross_frame_job_page(uuid,text,integer),
    yoao_jobs_v1.delete_batch_predict_jobs(uuid),
    yoao_jobs_v1.delete_terminal_jobs_before(timestamptz,text[]),
    yoao_jobs_v1.dispatch_status(uuid),
    yoao_jobs_v1.fail_task_creation_job(uuid,uuid,text),
    yoao_jobs_v1.find_action_idempotent_jobs(text,uuid,uuid,text),
    yoao_jobs_v1.find_task_creation_job(uuid,uuid,uuid,uuid),
    yoao_jobs_v1.find_video_retry_job(uuid,uuid,text),
    yoao_jobs_v1.finish_batch_result_execution(jsonb),
    yoao_jobs_v1.finish_dispatch(jsonb),
    yoao_jobs_v1.finish_prediction_mask_producer(uuid),
    yoao_jobs_v1.finish_prediction_retry_delivery(jsonb),
    yoao_jobs_v1.finish_task_creation_job(uuid,uuid),
    yoao_jobs_v1.fixture_media_backfill_route(uuid),
    yoao_jobs_v1.fixture_media_work_exists(text,text),
    yoao_jobs_v1.frozen_deployment_usage(uuid,uuid,uuid),
    yoao_jobs_v1.heartbeat_batch_result_execution(jsonb),
    yoao_jobs_v1.insert_video_summary(uuid,text,uuid,uuid,jsonb,text),
    yoao_jobs_v1.jobs_by_execution_id(text),
    yoao_jobs_v1.latest_batch_prediction_times(uuid[]),
    yoao_jobs_v1.lock_action_idempotency(text,uuid,uuid,text),
    yoao_jobs_v1.lock_cross_frame_singleflight(uuid,text),
    yoao_jobs_v1.lock_video_retry_jobs(uuid),
    yoao_jobs_v1.mark_batch_result_overwrite(jsonb),
    yoao_jobs_v1.mark_dispatch_failed(uuid,timestamptz,text),
    yoao_jobs_v1.prediction_mask_object_retained(uuid),
    yoao_jobs_v1.prepare_dispatch(jsonb),
    yoao_jobs_v1.read_batch_result_execution(jsonb),
    yoao_jobs_v1.read_job(uuid),
    yoao_jobs_v1.read_job_execution_snapshot(uuid),
    yoao_jobs_v1.read_job_for_share_nowait(uuid),
    yoao_jobs_v1.read_job_for_update(uuid),
    yoao_jobs_v1.read_job_for_update_nowait(uuid),
    yoao_jobs_v1.read_job_progress(uuid),
    yoao_jobs_v1.read_job_progress_for_update(uuid,text,text),
    yoao_jobs_v1.record_job_progress(uuid,text,text,timestamptz,jsonb),
    yoao_jobs_v1.release_batch_result_execution(jsonb),
    yoao_jobs_v1.skip_batch_result_member(jsonb),
    yoao_jobs_v1.stage_batch_result_members(jsonb),
    yoao_jobs_v1.start_batch_result_execution(jsonb),
    yoao_jobs_v1.start_batch_result_member(jsonb),
    yoao_jobs_v1.video_execution_checkpoint_step(text,text),
    yoao_jobs_v1.video_execution_step(text,text)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_labeling_class_query_v1.attribute_rows(uuid),
    yoao_labeling_class_query_v1.class_counts(uuid),
    yoao_labeling_class_query_v1.class_query_policy()
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_labeling_v1.accept_dataset_task_creation(uuid,uuid,uuid,uuid,uuid,uuid,text,integer),
    yoao_labeling_v1.accepted_video_input_referenced(uuid),
    yoao_labeling_v1.admit_prediction_retry(jsonb),
    yoao_labeling_v1.advance_dataset_task_creation(uuid,integer),
    yoao_labeling_v1.ai_mask_accept_command(jsonb),
    yoao_labeling_v1.annotation_bulk_command(jsonb),
    yoao_labeling_v1.annotation_maintenance_command(jsonb),
    yoao_labeling_v1.annotation_prediction_command(jsonb),
    yoao_labeling_v1.annotation_statistics_command(jsonb),
    yoao_labeling_v1.annotation_transfer_command(jsonb),
    yoao_labeling_v1.annotation_write_command(jsonb),
    yoao_labeling_v1.append_dataset_items(uuid,uuid[],uuid),
    yoao_labeling_v1.attach_annotation_input_source(uuid,uuid,text),
    yoao_labeling_v1.attach_annotation_video_input_source(uuid,uuid,jsonb),
    yoao_labeling_v1.attach_annotation_video_tracker_input_source(uuid,uuid,uuid,jsonb),
    yoao_labeling_v1.batch_control_command(jsonb),
    yoao_labeling_v1.begin_batch_result(jsonb),
    yoao_labeling_v1.begin_mask_content_write(jsonb),
    yoao_labeling_v1.capture_video_tracker_input(uuid,uuid,integer),
    yoao_labeling_v1.claim_mask_gc_deletes(jsonb),
    yoao_labeling_v1.claim_mask_gc_sweep(jsonb),
    yoao_labeling_v1.copy_annotation_input_sources(uuid,uuid),
    yoao_labeling_v1.copy_annotation_video_input_sources(uuid,uuid),
    yoao_labeling_v1.copy_annotation_video_tracker_input_sources(uuid,uuid,uuid),
    yoao_labeling_v1.cross_frame_write_command(jsonb),
    yoao_labeling_v1.decrement_project_completed_count(uuid),
    yoao_labeling_v1.decrement_project_review_count(uuid),
    yoao_labeling_v1.discussion_command(jsonb),
    yoao_labeling_v1.finish_batch_result_failure(jsonb),
    yoao_labeling_v1.finish_batch_result_write(jsonb),
    yoao_labeling_v1.finish_mask_gc_delete(jsonb),
    yoao_labeling_v1.finish_prediction_retry_failure(jsonb),
    yoao_labeling_v1.finish_prediction_retry_write(jsonb),
    yoao_labeling_v1.inspect_mask_gc_page(jsonb),
    yoao_labeling_v1.lock_active_learning_feedback_annotation_rows(uuid[]),
    yoao_labeling_v1.lock_active_learning_feedback_task_rows(uuid,uuid[]),
    yoao_labeling_v1.lock_annotation_rows_for_core_read(uuid[],uuid,uuid,text,boolean),
    yoao_labeling_v1.lock_batch_result_write(jsonb),
    yoao_labeling_v1.lock_batch_rows_for_core_read(uuid[],uuid,text,boolean),
    yoao_labeling_v1.lock_mask_write_epochs(jsonb),
    yoao_labeling_v1.lock_prediction_for_core_read_share(uuid,uuid),
    yoao_labeling_v1.lock_prediction_meta_for_core_read_share(uuid),
    yoao_labeling_v1.lock_prediction_rows_for_core_read_share(uuid,uuid[],integer),
    yoao_labeling_v1.lock_scene_track_interval_rows_for_core_read_share(uuid[],integer),
    yoao_labeling_v1.lock_scene_track_rows_for_core_read(uuid[],integer),
    yoao_labeling_v1.lock_task_dataset_item_link_rows_for_core_read_share(uuid[],integer),
    yoao_labeling_v1.lock_task_dataset_item_links_for_core_read(uuid,boolean,uuid,integer),
    yoao_labeling_v1.lock_task_for_core_read(uuid,text,boolean,uuid),
    yoao_labeling_v1.lock_task_rows_for_core_read(uuid[],uuid,uuid),
    yoao_labeling_v1.lock_task_rows_for_core_read_share(uuid[],uuid,uuid),
    yoao_labeling_v1.lock_video_quality_run_ids_for_export_share(uuid[],integer),
    yoao_labeling_v1.lock_video_segment_for_core_read(uuid,uuid),
    yoao_labeling_v1.lock_video_segment_for_core_read_share(uuid,uuid),
    yoao_labeling_v1.lock_video_segment_ids_for_export_share(uuid[],integer),
    yoao_labeling_v1.lock_video_tracker_input_for_accept(uuid,uuid,uuid,uuid,text,text),
    yoao_labeling_v1.lock_video_tracker_input_for_stage(uuid,uuid,uuid,uuid,text,text),
    yoao_labeling_v1.mask_edit_command(jsonb),
    yoao_labeling_v1.mask_qc_command(jsonb),
    yoao_labeling_v1.mask_repair_command(jsonb),
    yoao_labeling_v1.mask_revision_command(jsonb),
    yoao_labeling_v1.next_batch_display_id(),
    yoao_labeling_v1.prediction_partition_command(jsonb),
    yoao_labeling_v1.prediction_record_command(jsonb),
    yoao_labeling_v1.prepare_dataset_task_creation(uuid,jsonb),
    yoao_labeling_v1.publish_task_original(uuid,uuid,uuid,uuid,uuid,text),
    yoao_labeling_v1.put_batch_result_part(jsonb),
    yoao_labeling_v1.raster_mask_retention_query(jsonb),
    yoao_labeling_v1.read_batch_result(jsonb),
    yoao_labeling_v1.read_dataset_task_creation(uuid),
    yoao_labeling_v1.read_mask_gc_progress(jsonb),
    yoao_labeling_v1.read_mask_write_epochs(jsonb),
    yoao_labeling_v1.read_prediction_retry(jsonb),
    yoao_labeling_v1.record_project_review_approval(uuid),
    yoao_labeling_v1.register_tracker_running_mask_root(jsonb),
    yoao_labeling_v1.scene_read_query(jsonb),
    yoao_labeling_v1.scene_track_command(jsonb),
    yoao_labeling_v1.scene_track_domain_command(jsonb),
    yoao_labeling_v1.seal_batch_result(jsonb),
    yoao_labeling_v1.spatial_quality_command(jsonb),
    yoao_labeling_v1.stage_mask_gc_page(jsonb),
    yoao_labeling_v1.task_edit_lock_command(jsonb),
    yoao_labeling_v1.task_event_command(jsonb),
    yoao_labeling_v1.task_maintenance_command(jsonb),
    yoao_labeling_v1.task_workflow_command(jsonb),
    yoao_labeling_v1.video_bbox_editing_command(jsonb),
    yoao_labeling_v1.video_control_command(jsonb),
    yoao_labeling_v1.video_segment_lifecycle_command(jsonb),
    yoao_labeling_v1.video_writer_step(text,text),
    yoao_labeling_v1.write_project_counter_projection(uuid,jsonb)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_learning_v1.registry_evaluation_reference_exists(uuid)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_notifications_v1.append_job_terminal_notification(jsonb),
    yoao_notifications_v1.append_notification(jsonb),
    yoao_notifications_v1.claim_identity_deliveries(jsonb),
    yoao_notifications_v1.delete_command(pg_catalog.jsonb),
    yoao_notifications_v1.delete_command_scoped(jsonb),
    yoao_notifications_v1.delivery_job_scopes(uuid[]),
    yoao_notifications_v1.delivery_member_pairs(jsonb),
    yoao_notifications_v1.delivery_owner_pairs(jsonb),
    yoao_notifications_v1.delivery_reviewer_pairs(jsonb),
    yoao_notifications_v1.delivery_super_pairs(jsonb),
    yoao_notifications_v1.find_terminal_notification(uuid,text,uuid),
    yoao_notifications_v1.finish_identity_delivery(jsonb),
    yoao_notifications_v1.identity_delivery_policy(),
    yoao_notifications_v1.inbox_page(uuid,text,boolean,integer,integer),
    yoao_notifications_v1.inbox_page_scoped(uuid,text,boolean,integer,integer),
    yoao_notifications_v1.inbox_total_count(uuid,text),
    yoao_notifications_v1.inbox_total_count_scoped(uuid,text),
    yoao_notifications_v1.inbox_unread_count(uuid,text),
    yoao_notifications_v1.inbox_unread_count_scoped(uuid,text),
    yoao_notifications_v1.preference_command(jsonb),
    yoao_notifications_v1.read_notification(uuid),
    yoao_notifications_v1.read_state_command(jsonb),
    yoao_notifications_v1.read_state_command_scoped(jsonb)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_workspace_configuration_v1.configuration_policy(),
    yoao_workspace_configuration_v1.configuration_step(jsonb,text,jsonb,text[],text[]),
    yoao_workspace_configuration_v1.guide_assets_step(jsonb,text,jsonb)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_workspace_directory_query_v1.directory_query_policy(),
    yoao_workspace_directory_query_v1.directory_query_step(jsonb,text,jsonb)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_workspace_membership_v1.apply_membership_command(jsonb),
    yoao_workspace_membership_v1.membership_policy(),
    yoao_workspace_membership_v1.prepare_membership_command(jsonb)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_workspace_owner_transfer_v1.owner_transfer_policy(),
    yoao_workspace_owner_transfer_v1.transfer_project_owner(jsonb)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_workspace_project_query_v1.project_query_policy(),
    yoao_workspace_project_query_v1.project_query_step(jsonb,text,jsonb,text,text,text[],text[],uuid,text,timestamptz,timestamptz,bigint,integer),
    yoao_workspace_project_query_v1.project_search_member_ids(uuid,text),
    yoao_workspace_project_query_v1.project_search_scope(uuid,text)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_workspace_template_query_v1.template_query_policy(),
    yoao_workspace_template_query_v1.template_query_step(jsonb,text,jsonb,text,text[],text)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_workspace_v1.actor_project_memberships(uuid),
    yoao_workspace_v1.clear_primary_backend_bindings(uuid),
    yoao_workspace_v1.create_import_project(uuid,text,text,uuid,jsonb),
    yoao_workspace_v1.create_media_seed_project(uuid,text,text,uuid,text,jsonb),
    yoao_workspace_v1.create_point_cloud_seed_project(uuid,text,text,uuid,jsonb),
    yoao_workspace_v1.delete_project(uuid,uuid,uuid,jsonb),
    yoao_workspace_v1.lock_project_manager(uuid,uuid),
    yoao_workspace_v1.lock_project_member_reference(uuid,uuid,boolean),
    yoao_workspace_v1.lock_project_reference(uuid),
    yoao_workspace_v1.lock_project_reference_nowait(uuid),
    yoao_workspace_v1.lock_project_references_for_update(uuid[]),
    yoao_workspace_v1.lock_project_row(uuid,boolean),
    yoao_workspace_v1.pin_seed_member_assignment(uuid,timestamptz),
    yoao_workspace_v1.preview_dataset_unlink(uuid,uuid,uuid),
    yoao_workspace_v1.project_access_step(uuid,uuid,text),
    yoao_workspace_v1.project_member_rows(uuid,uuid[],boolean),
    yoao_workspace_v1.read_import_project(text),
    yoao_workspace_v1.read_project_row(uuid),
    yoao_workspace_v1.unlink_dataset(uuid,uuid,uuid,uuid,text,jsonb),
    yoao_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb),
    yoao_workspace_v1.write_point_cloud_quality_config(uuid,jsonb),
    yoao_workspace_v1.write_seed_members(jsonb),
    yoao_workspace_v1.write_seed_project_projection(uuid,jsonb)
    TO :"application_role";
GRANT USAGE ON SCHEMA yoao_settings_v1, yoao_diagnostics_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION
    yoao_settings_v1.runtime_knob(text),
    yoao_settings_v1.connector_override(),
    yoao_settings_v1.import_limits(),
    yoao_settings_v1.management_snapshot(uuid,boolean),
    yoao_settings_v1.apply_settings_edit(uuid,text,jsonb,text[],text),
    yoao_settings_v1.queue_smtp_test(uuid),
    yoao_settings_v1.read_smtp_test(uuid,uuid),
    yoao_diagnostics_v1.create_bug_report(uuid,jsonb),
    yoao_diagnostics_v1.confirm_bug_attachment_binding(uuid,uuid),
    yoao_diagnostics_v1.list_bug_reports(uuid,boolean,text,text,text,integer,integer),
    yoao_diagnostics_v1.read_bug_report(uuid,uuid),
    yoao_diagnostics_v1.patch_bug_report(uuid,uuid,jsonb),
    yoao_diagnostics_v1.prepare_bug_comment(uuid,uuid),
    yoao_diagnostics_v1.add_bug_comment(uuid,uuid,text),
    yoao_diagnostics_v1.bug_cluster_candidates(uuid,uuid),
    yoao_diagnostics_v1.bug_report_markdown_comments(uuid,uuid[]),
    yoao_diagnostics_v1.delete_bug_report(uuid,uuid),
    yoao_diagnostics_v1.detach_orphan_bug_tasks(uuid,uuid[]),
    yoao_diagnostics_v1.reserve_bug_attachment(uuid,uuid,text,integer,text,text,timestamptz,timestamptz,jsonb),
    yoao_diagnostics_v1.lock_pending_bug_attachment(uuid,uuid),
    yoao_diagnostics_v1.record_bug_attachment_version(uuid,uuid,jsonb,text),
    yoao_diagnostics_v1.publish_bug_attachment(uuid,uuid,jsonb,text),
    yoao_diagnostics_v1.lock_report_attachment(uuid,uuid,text),
    yoao_diagnostics_v1.bug_attachment_cleanup_candidates(integer),
    yoao_diagnostics_v1.prepare_bug_attachment_cleanup(uuid),
    yoao_diagnostics_v1.lock_retired_bug_attachment(uuid),
    yoao_diagnostics_v1.complete_bug_attachment_cleanup(uuid,jsonb,text),
    yoao_diagnostics_v1.bug_attachment_history(uuid)
TO :"application_role";
GRANT USAGE ON SCHEMA yoao_inference_topology_v1, yoao_inference_pipelines_v1,
    yoao_assets_v1, yoao_labeling_v1
TO :"application_role";
GRANT EXECUTE ON FUNCTION
    yoao_inference_topology_v1.lock_video_prediction_registry(uuid),
    yoao_inference_topology_v1.lock_video_prediction_pool(uuid,uuid,uuid),
    yoao_inference_topology_v1.lock_native_prediction_route(uuid,uuid,uuid),
    yoao_inference_pipelines_v1.create_prediction_execution_snapshot(uuid,text,jsonb),
    yoao_inference_pipelines_v1.read_prediction_execution_snapshot(uuid,uuid),
    yoao_inference_pipelines_v1.prune_prediction_execution_snapshots(timestamptz),
    yoao_labeling_v1.finish_prediction_retry_pipeline_failure(jsonb,jsonb),
    yoao_assets_v1.create_prediction_input_lease(uuid,uuid,text,uuid,integer,jsonb,integer,jsonb,timestamptz),
    yoao_assets_v1.prediction_input_lease_facts(uuid,boolean,timestamptz),
    yoao_assets_v1.prediction_input_objects_pending(uuid),
    yoao_assets_v1.read_prediction_input_inventory(uuid),
    yoao_assets_v1.read_prediction_input_object(uuid,text),
    yoao_assets_v1.reserve_prediction_input_object(uuid,text,jsonb),
    yoao_assets_v1.verify_prediction_input_object(uuid,text,jsonb,text,text),
    yoao_assets_v1.seal_prediction_input_lease(uuid,boolean,timestamptz),
    yoao_assets_v1.prepare_prediction_input_cleanup(uuid,timestamptz),
    yoao_assets_v1.record_prediction_input_cleanup_failure(uuid,text,boolean),
    yoao_assets_v1.finish_prediction_input_cleanup(uuid,uuid,integer,timestamptz),
    yoao_assets_v1.list_due_prediction_input_cleanups(timestamptz,integer),
    yoao_assets_v1.prediction_input_binding_history()
TO :"application_role";
GRANT USAGE ON SCHEMA yoao_learning_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION
    yoao_learning_v1.training_get_run(uuid, uuid, text),
    yoao_learning_v1.training_get_attempt(uuid, text),
    yoao_learning_v1.training_get_preview(uuid, uuid, text),
    yoao_learning_v1.training_executor_run(uuid, boolean),
    yoao_learning_v1.training_executor_preview(uuid, boolean),
    yoao_learning_v1.training_attempt_proof(uuid, text, uuid, text),
    yoao_learning_v1.training_preview_proof(uuid, text, uuid, text),
    yoao_learning_v1.training_run_request(uuid, uuid),
    yoao_learning_v1.training_gpu_queue(),
    yoao_learning_v1.training_study_children(uuid, boolean),
    yoao_learning_v1.training_study_attempts(uuid),
    yoao_learning_v1.training_run_attempts(uuid, boolean),
    yoao_learning_v1.training_adoption_attempts(uuid),
    yoao_learning_v1.training_trigger_run(uuid, boolean),
    yoao_learning_v1.training_active_auto_run(uuid, uuid),
    yoao_learning_v1.training_trigger_run_ids(uuid, uuid[]),
    yoao_learning_v1.training_dataset_run_dependencies(uuid, uuid),
    yoao_learning_v1.training_run_has_lease(uuid),
    yoao_learning_v1.training_create_run(uuid, uuid, uuid, uuid, uuid, uuid, uuid, uuid, text, text, jsonb, text, jsonb, jsonb),
    yoao_learning_v1.training_pin_run_storage(uuid, jsonb, boolean),
    yoao_learning_v1.training_enqueue_attempt(uuid, uuid, uuid, uuid),
    yoao_learning_v1.training_reserve_study_attempt(uuid, uuid, jsonb),
    yoao_learning_v1.training_manual_resume(uuid, uuid, boolean),
    yoao_learning_v1.training_delete_run(uuid, uuid),
    yoao_learning_v1.training_gpu_lease(text),
    yoao_learning_v1.training_lock_gpu_lease(text),
    yoao_learning_v1.training_observe_gpu(text, text, uuid, jsonb, timestamptz),
    yoao_learning_v1.training_cpu_lease(text),
    yoao_learning_v1.training_lock_cpu_lease(text),
    yoao_learning_v1.training_observe_cpu(text, text, uuid, jsonb, timestamptz),
    yoao_learning_v1.training_claim_attempt(uuid, uuid, text, text, uuid, uuid, text, jsonb, timestamptz),
    yoao_learning_v1.training_lose_attempt(uuid, uuid, timestamptz, text, jsonb),
    yoao_learning_v1.training_cancel_attempt(uuid, uuid, text),
    yoao_learning_v1.training_finish_queued_attempt(uuid, uuid, text, timestamptz, text, jsonb, jsonb),
    yoao_learning_v1.training_finish_executor_attempt(uuid, uuid, text, uuid, text, boolean, text, timestamptz, text, jsonb, jsonb, text, jsonb, uuid, boolean),
    yoao_learning_v1.training_heartbeat_attempt(uuid, uuid, text, uuid, text, timestamptz, text, text, jsonb, jsonb),
    yoao_learning_v1.training_attempt_uploads(uuid, uuid, text, uuid, text, jsonb),
    yoao_learning_v1.training_record_event(uuid, text, uuid, text, integer, text, jsonb),
    yoao_learning_v1.training_event_page(uuid, integer, boolean),
    yoao_learning_v1.training_last_epoch(uuid),
    yoao_learning_v1.training_set_observation_receipt(uuid, text, uuid, text, jsonb),
    yoao_learning_v1.training_telemetry_receipt(uuid, integer),
    yoao_learning_v1.training_append_telemetry(uuid, text, uuid, text, uuid, jsonb),
    yoao_learning_v1.training_trim_telemetry(uuid),
    yoao_learning_v1.training_telemetry_earliest(uuid),
    yoao_learning_v1.training_telemetry_page(uuid, integer, integer, integer),
    yoao_learning_v1.training_cleanup_telemetry(timestamptz),
    yoao_learning_v1.training_logs_receipt(uuid, integer),
    yoao_learning_v1.training_append_logs(uuid, text, uuid, text, uuid, jsonb),
    yoao_learning_v1.training_trim_logs(uuid),
    yoao_learning_v1.training_logs_earliest(uuid),
    yoao_learning_v1.training_logs_page(uuid, integer, integer, integer),
    yoao_learning_v1.training_cleanup_logs(timestamptz),
    yoao_learning_v1.training_latest_telemetry(uuid),
    yoao_learning_v1.training_preview_request(uuid, uuid),
    yoao_learning_v1.training_active_preview_count(uuid),
    yoao_learning_v1.training_create_preview(uuid, uuid, uuid, uuid, uuid, text, jsonb, text, jsonb, jsonb, text, timestamptz),
    yoao_learning_v1.training_preview_has_lease(uuid),
    yoao_learning_v1.training_pin_preview_storage(uuid, jsonb, boolean),
    yoao_learning_v1.training_attach_preview_job(uuid, uuid),
    yoao_learning_v1.training_cpu_queue(text),
    yoao_learning_v1.training_claim_preview(uuid, text, text, uuid, uuid, text, jsonb, timestamptz),
    yoao_learning_v1.training_heartbeat_preview(uuid, text, uuid, text, timestamptz, text),
    yoao_learning_v1.training_preview_uploads(uuid, text, uuid, text, jsonb, timestamptz),
    yoao_learning_v1.training_finish_queued_preview(uuid, text, timestamptz, text),
    yoao_learning_v1.training_finish_executor_preview(uuid, text, uuid, text, boolean, text, timestamptz, text, text, jsonb),
    yoao_learning_v1.training_cancel_preview(uuid, text),
    yoao_learning_v1.training_lose_preview(uuid, timestamptz, text),
    yoao_learning_v1.training_expire_preview(uuid, timestamptz),
    yoao_learning_v1.training_preview_cleanup_queue(timestamptz),
    yoao_learning_v1.training_preview_cleanup_expire(uuid, timestamptz),
    yoao_learning_v1.training_delete_expired_preview(uuid, jsonb, timestamptz)
TO :"application_role";
GRANT SELECT ON
    yoao_learning_v1.training_run_summary,
    yoao_learning_v1.training_run_storage_owner,
    yoao_learning_v1.training_preview_storage_owner,
    yoao_learning_v1.training_run_lineage,
    yoao_learning_v1.training_attempt_lineage
TO :"application_role";
GRANT USAGE ON SCHEMA yoao_learning_v1 TO :"application_role";
GRANT SELECT ON yoao_learning_v1.training_dataset_metadata TO :"application_role";
GRANT EXECUTE ON FUNCTION
    yoao_learning_v1.training_dataset_task(jsonb),
    yoao_learning_v1.training_dataset_profile(jsonb,text),
    yoao_learning_v1.training_dataset_facts(uuid,uuid,boolean),
    yoao_learning_v1.training_dataset_read(uuid,uuid,boolean),
    yoao_learning_v1.training_dataset_hold(uuid,uuid),
    yoao_learning_v1.training_dataset_request(uuid,uuid,boolean),
    yoao_learning_v1.training_dataset_job_lock(uuid),
    yoao_learning_v1.training_dataset_create(uuid,uuid,uuid,uuid,uuid,text,uuid,text,jsonb,text,jsonb),
    yoao_learning_v1.training_dataset_stream(uuid,uuid),
    yoao_learning_v1.training_dataset_associate(uuid,uuid),
    yoao_learning_v1.training_dataset_set_storage(uuid,uuid,uuid,jsonb),
    yoao_learning_v1.training_dataset_queue(uuid,uuid,uuid,uuid),
    yoao_learning_v1.training_dataset_start(uuid,uuid,uuid),
    yoao_learning_v1.training_dataset_progress(uuid,uuid,uuid,integer),
    yoao_learning_v1.training_dataset_publish(uuid,uuid,uuid,text,jsonb,jsonb,text,text,bigint,timestamptz),
    yoao_learning_v1.training_dataset_fail(uuid,uuid,uuid,text,boolean,text,text,timestamptz),
    yoao_learning_v1.training_dataset_delete(uuid,uuid,uuid),
    yoao_learning_v1.training_dataset_manifest(uuid,uuid),
    yoao_learning_v1.training_dataset_sample_evidence(uuid,uuid,uuid),
    yoao_learning_v1.training_binding_history(),
    yoao_learning_v1.training_dataset_asset_versions(uuid,boolean),
    yoao_learning_v1.training_dataset_location_versions(uuid),
    yoao_learning_v1.training_analysis_read(uuid),
    yoao_learning_v1.training_analysis_start(uuid,uuid,text,text,text,jsonb),
    yoao_learning_v1.training_analysis_add_members(uuid,text,jsonb),
    yoao_learning_v1.training_analysis_sample_page(uuid,text,integer,bigint,text,integer,text,text,text[]),
    yoao_learning_v1.training_analysis_member(uuid,uuid),
    yoao_learning_v1.training_analysis_observations(uuid),
    yoao_learning_v1.training_analysis_media_counts(uuid),
    yoao_learning_v1.training_analysis_observe_media(uuid,uuid,text,bigint,text,timestamptz),
    yoao_learning_v1.training_storage_stage_deletion(uuid,uuid,text,jsonb),
    yoao_learning_v1.training_storage_claim_deletion(uuid,timestamptz),
    yoao_learning_v1.training_storage_complete_deletion(uuid,jsonb),
    yoao_learning_v1.training_storage_deletion_batch(),
    yoao_learning_v1.training_dataset_high_water_violations(uuid)
    TO :"application_role";
GRANT USAGE ON SCHEMA yoao_learning_v1 TO :"application_role";
GRANT SELECT ON yoao_learning_v1.model_version_lineage,
    yoao_learning_v1.model_deployment_lineage,
    yoao_learning_v1.model_release_lineage,
    yoao_learning_v1.sample_evaluation_lineage,
    yoao_learning_v1.sample_evaluation_storage_owners TO :"application_role";
GRANT EXECUTE ON FUNCTION
    yoao_learning_v1.model_version_detail(uuid,uuid,boolean),
    yoao_learning_v1.model_registration_replay(uuid,uuid,uuid),
    yoao_learning_v1.register_model_candidate(uuid,uuid,uuid,uuid,uuid,text,text,text,text,jsonb,jsonb,jsonb,text,jsonb,jsonb),
    yoao_learning_v1.run_model_candidates(uuid,uuid),
    yoao_learning_v1.run_has_model_candidate(uuid,uuid),
    yoao_learning_v1.catalog_model_candidates(uuid[],boolean),
    yoao_learning_v1.model_candidate_visibility(uuid,uuid),
    yoao_learning_v1.model_candidate_metadata(uuid[],uuid[]),
    yoao_learning_v1.list_model_candidates(uuid,text,text,text,text,timestamptz,timestamptz,integer,integer),
    yoao_learning_v1.count_model_candidates(uuid,text,text,text,text,timestamptz,timestamptz),
    yoao_learning_v1.model_deployments_for_versions(uuid[],uuid[]),
    yoao_learning_v1.model_deployment_detail(uuid,uuid,uuid,boolean),
    yoao_learning_v1.hold_model_deployment_for_registry(uuid,uuid,uuid,boolean),
    yoao_learning_v1.hold_registry_model_deployments(uuid),
    yoao_learning_v1.count_active_model_releases(uuid[]),
    yoao_learning_v1.hold_model_candidate(uuid,uuid,uuid),
    yoao_learning_v1.hold_prediction_model_reference(uuid,uuid,uuid,uuid),
    yoao_learning_v1.start_model_deployment_check(uuid,uuid,uuid,uuid),
    yoao_learning_v1.finish_model_deployment_check(uuid,uuid,uuid,jsonb,jsonb,text,timestamptz),
    yoao_learning_v1.advance_model_deployment_retirement(uuid,uuid,uuid,uuid,boolean),
    yoao_learning_v1.model_release_detail(uuid,uuid),
    yoao_learning_v1.model_release_replay(uuid,uuid),
    yoao_learning_v1.catalog_model_releases(uuid[],uuid[]),
    yoao_learning_v1.project_model_selection(uuid,text,boolean),
    yoao_learning_v1.list_project_model_releases(uuid,text,integer,integer),
    yoao_learning_v1.count_project_model_releases(uuid,text),
    yoao_learning_v1.publish_project_model_release(uuid,text,uuid,uuid,text,uuid,integer,uuid,uuid,uuid,uuid,jsonb),
    yoao_learning_v1.append_model_decision(uuid,uuid,uuid,text,uuid,text,text,text),
    yoao_learning_v1.list_model_decisions(uuid,uuid,integer,integer),
    yoao_learning_v1.trial_mask_keys(),
    yoao_learning_v1.trial_mask_referenced(text),
    yoao_learning_v1.pending_dataset_mask_keys(),
    yoao_learning_v1.pending_dataset_mask_referenced(text),
    yoao_learning_v1.evaluation_detail(uuid,uuid,text),
    yoao_learning_v1.evaluation_for_attempt(uuid),
    yoao_learning_v1.hold_evaluation_for_current_job(uuid),
    yoao_learning_v1.evaluation_replay(uuid,uuid),
    yoao_learning_v1.list_project_evaluations(uuid,integer,integer),
    yoao_learning_v1.count_project_evaluations(uuid),
    yoao_learning_v1.dataset_has_sample_evaluation(uuid,uuid),
    yoao_learning_v1.held_out_evaluation_budget(uuid),
    yoao_learning_v1.held_out_evaluation_specs(uuid,boolean),
    yoao_learning_v1.evaluation_attempt_detail(uuid,uuid),
    yoao_learning_v1.evaluation_attempt_history(uuid),
    yoao_learning_v1.evaluation_sample_detail(uuid,integer),
    yoao_learning_v1.evaluation_sample_evidence(uuid),
    yoao_learning_v1.hold_evaluation_adoption_attempts(uuid),
    yoao_learning_v1.hold_evaluation_adoption_samples(uuid),
    yoao_learning_v1.pin_retained_evaluation_storage(uuid,uuid,jsonb),
    yoao_learning_v1.create_sample_evaluation(uuid,uuid,uuid,uuid,uuid,uuid,uuid,text,text,jsonb,text,jsonb,uuid,uuid[]),
    yoao_learning_v1.queue_evaluation_attempt(uuid,uuid,integer,uuid,uuid,uuid,timestamptz),
    yoao_learning_v1.claim_evaluation_attempt(uuid,uuid,integer,uuid,timestamptz),
    yoao_learning_v1.heartbeat_evaluation_attempt(uuid,uuid,integer,uuid,timestamptz),
    yoao_learning_v1.record_evaluation_dispatch(uuid,uuid,integer,timestamptz),
    yoao_learning_v1.evaluation_maintenance_candidates(timestamptz),
    yoao_learning_v1.record_evaluation_maintenance(uuid,uuid,integer,timestamptz),
    yoao_learning_v1.start_evaluation_sample(uuid,uuid,integer,uuid,integer,jsonb),
    yoao_learning_v1.checkpoint_evaluation_sample(uuid,uuid,integer,uuid,integer,jsonb,jsonb,boolean),
    yoao_learning_v1.fail_evaluation_sample_input(uuid,uuid,integer,uuid,integer),
    yoao_learning_v1.request_evaluation_cancellation(uuid,uuid,integer,text,text,boolean),
    yoao_learning_v1.lose_evaluation_attempt(uuid,uuid,integer,uuid,text,text,text),
    yoao_learning_v1.finish_evaluation_attempt(uuid,uuid,integer,uuid,text,text,text,text,jsonb,timestamptz),
    yoao_learning_v1.request_evaluation_deletion(uuid,uuid,timestamptz),
    yoao_learning_v1.purge_deleted_evaluation(uuid,uuid,jsonb) TO :"application_role";
GRANT USAGE ON SCHEMA yoao_learning_v1 TO :"application_role";
GRANT SELECT ON TABLE yoao_learning_v1.selection_storage_owners,
    yoao_learning_v1.recipe_lineage,
    yoao_learning_v1.recipe_revision_lineage TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.selection_create_round(uuid, uuid, uuid, uuid, uuid, uuid, text, text, jsonb, text, jsonb, jsonb, uuid[]),
    yoao_learning_v1.selection_round(uuid, uuid, text),
    yoao_learning_v1.selection_round_request(uuid, uuid),
    yoao_learning_v1.selection_round_for_job(uuid),
    yoao_learning_v1.selection_rounds(uuid, text, integer, integer),
    yoao_learning_v1.selection_round_count(uuid, text),
    yoao_learning_v1.selection_attempt(uuid, uuid),
    yoao_learning_v1.selection_recent_attempts(uuid),
    yoao_learning_v1.selection_attempt_ids(uuid),
    yoao_learning_v1.selection_candidates(uuid, integer),
    yoao_learning_v1.selection_due_rounds(timestamptz),
    yoao_learning_v1.selection_open_attempt(uuid, integer, uuid, uuid, uuid, timestamptz),
    yoao_learning_v1.selection_claim_attempt(uuid, integer, uuid, timestamptz),
    yoao_learning_v1.selection_touch_attempt(uuid, integer, uuid, text, timestamptz),
    yoao_learning_v1.selection_debit_round(uuid, integer, uuid, timestamptz),
    yoao_learning_v1.selection_candidate_request(uuid, integer, uuid, integer, jsonb, jsonb),
    yoao_learning_v1.selection_candidate_evidence(uuid, integer, uuid, integer, jsonb),
    yoao_learning_v1.selection_settle_attempt(uuid, integer, uuid, text, text, text, jsonb, timestamptz),
    yoao_learning_v1.selection_release_cancelled_claim(uuid, integer, uuid),
    yoao_learning_v1.selection_decision_request(uuid, uuid),
    yoao_learning_v1.selection_decisions(uuid, integer, integer),
    yoao_learning_v1.selection_accepted_decisions(uuid),
    yoao_learning_v1.selection_record_decision(uuid, uuid, uuid, uuid, text, text, uuid, integer[], jsonb, text),
    yoao_learning_v1.selection_feedback_request(uuid, uuid),
    yoao_learning_v1.selection_feedback(uuid, integer, integer),
    yoao_learning_v1.selection_receipt_count(uuid, boolean),
    yoao_learning_v1.selection_record_feedback(uuid, uuid, uuid, text, jsonb, text),
    yoao_learning_v1.selection_retain_history(uuid, jsonb),
    yoao_learning_v1.selection_history_reference(uuid, uuid, uuid[], uuid[], uuid, uuid),
    yoao_learning_v1.selection_dataset_history_budget(uuid),
    yoao_learning_v1.selection_dataset_history(uuid, boolean),
    yoao_learning_v1.selection_adoption_attempts(uuid),
    yoao_learning_v1.selection_adoption_candidates(uuid),
    yoao_learning_v1.selection_pin_storage(uuid, uuid, integer, jsonb),
    yoao_learning_v1.selection_policy(uuid, text, uuid, boolean),
    yoao_learning_v1.selection_save_policy(uuid, text, integer, uuid, boolean, uuid, jsonb, jsonb, timestamptz, timestamptz, text),
    yoao_learning_v1.selection_disable_policy(uuid, integer, timestamptz, text),
    yoao_learning_v1.selection_delete_policy(uuid, integer),
    yoao_learning_v1.selection_due_policies(timestamptz),
    yoao_learning_v1.selection_policy_check_state(uuid, integer, boolean, text, text, timestamptz, timestamptz, timestamptz, text, text),
    yoao_learning_v1.selection_advance_baseline(uuid, integer, uuid, uuid, jsonb),
    yoao_learning_v1.selection_trigger(uuid, uuid, text),
    yoao_learning_v1.selection_other_active_trigger(uuid, uuid, uuid),
    yoao_learning_v1.selection_trigger_duplicate(uuid, integer, uuid, text[]),
    yoao_learning_v1.selection_trigger_history(uuid, text, integer, integer),
    yoao_learning_v1.selection_trigger_count(uuid, text),
    yoao_learning_v1.selection_build_trigger(uuid, integer, uuid, text),
    yoao_learning_v1.selection_trigger_progress(uuid, integer, text, text, timestamptz, text, text, text, integer),
    yoao_learning_v1.selection_requeue_trigger(uuid),
    yoao_learning_v1.selection_policy_claim(uuid, uuid, uuid),
    yoao_learning_v1.selection_dataset_references(uuid),
    yoao_learning_v1.selection_study(uuid, uuid, uuid, boolean),
    yoao_learning_v1.selection_studies(uuid, integer, integer),
    yoao_learning_v1.selection_study_count(uuid),
    yoao_learning_v1.selection_due_studies(),
    yoao_learning_v1.selection_study_claim(uuid, uuid, uuid),
    yoao_learning_v1.selection_create_study(uuid, uuid, uuid, uuid, uuid, uuid, text, text, jsonb, text, jsonb, jsonb),
    yoao_learning_v1.selection_trials(uuid, boolean),
    yoao_learning_v1.selection_bind_trial(uuid, uuid, uuid),
    yoao_learning_v1.selection_study_progress(uuid, boolean, text, text, timestamptz, timestamptz),
    yoao_learning_v1.selection_resume_study(uuid, uuid),
    yoao_learning_v1.selection_recipe(uuid, uuid, boolean),
    yoao_learning_v1.selection_recipes(uuid, boolean, integer, integer),
    yoao_learning_v1.selection_recipe_count(uuid, boolean),
    yoao_learning_v1.selection_recipe_revision(uuid, uuid, integer),
    yoao_learning_v1.selection_recipe_request(uuid, uuid),
    yoao_learning_v1.selection_recipe_revisions(uuid, integer, integer),
    yoao_learning_v1.selection_recipe_revision_count(uuid),
    yoao_learning_v1.selection_append_recipe(uuid, uuid, integer, uuid, text, text, jsonb, text, jsonb, uuid, uuid, text),
    yoao_learning_v1.selection_archive_recipe(uuid, uuid, integer)
    TO :"application_role";
GRANT USAGE ON SCHEMA yoao_assets_storage_v1 TO :"platform_runtime";
GRANT SELECT ON yoao_assets_storage_v1.location_metadata,
    yoao_assets_storage_v1.connector_origins, yoao_assets_storage_v1.purpose_bindings,
    yoao_assets_storage_v1.agent_facts, yoao_assets_storage_v1.site_facts,
    yoao_assets_storage_v1.agent_location_status, yoao_assets_storage_v1.probe_health,
    yoao_assets_storage_v1.gate_status TO :"platform_runtime";
GRANT EXECUTE ON FUNCTION
    yoao_assets_storage_v1._location_config(jsonb),
    yoao_assets_storage_v1._connector_origin(text,jsonb),
    yoao_assets_storage_v1.initialize_gate(uuid),
    yoao_assets_storage_v1.gate_facts(uuid,text),
    yoao_assets_storage_v1.begin_physical_io(uuid,uuid,text,text,integer),
    yoao_assets_storage_v1.settle_physical_io(uuid,uuid,integer),
    yoao_assets_storage_v1.physical_io_is_open(text,uuid,uuid),
    yoao_assets_storage_v1.physical_io_receipt(uuid),
    yoao_assets_storage_v1.maintenance_status(uuid),
    yoao_assets_storage_v1.maintenance_dependency_snapshot(uuid,boolean),
    yoao_assets_storage_v1.location_storage_referenced(uuid),
    yoao_assets_storage_v1.transition_gate(uuid,integer,text,uuid),
    yoao_assets_storage_v1.checkpoint_media_native_stage(uuid,uuid,integer,jsonb),
    yoao_assets_storage_v1.location_facts(uuid,text,uuid,text,boolean),
    yoao_assets_storage_v1.connector_origin(uuid,text),
    yoao_assets_storage_v1.purpose_binding(text,text),
    yoao_assets_storage_v1.location_visible(uuid,text,uuid),
    yoao_assets_storage_v1.create_read_location(uuid,text,text,jsonb),
    yoao_assets_storage_v1.edit_location_policy(uuid,integer,text,boolean,jsonb),
    yoao_assets_storage_v1.lock_binding_locations(uuid,uuid),
    yoao_assets_storage_v1.ensure_embedded_targets(jsonb,jsonb),
    yoao_assets_storage_v1.switch_purpose_binding(text,integer,uuid,text),
    yoao_assets_storage_v1.location_probe_facts(uuid,text),
    yoao_assets_storage_v1.record_location_probe(uuid,text,integer,timestamptz,jsonb),
    yoao_assets_storage_v1.create_site(uuid,uuid,text,text),
    yoao_assets_storage_v1.register_agent(uuid,uuid,uuid,text,jsonb,text),
    yoao_assets_storage_v1.agent_identity(uuid,integer,text,text),
    yoao_assets_storage_v1.pair_agent(uuid,text,text),
    yoao_assets_storage_v1.rotate_agent(uuid,integer,text),
    yoao_assets_storage_v1.control_agent(uuid,integer,boolean,boolean),
    yoao_assets_storage_v1.observe_agent(uuid,integer,text,uuid,jsonb,jsonb),
    yoao_assets_storage_v1.agent_command_receipt(uuid),
    yoao_assets_storage_v1.lock_agent_command(uuid,uuid,integer,text),
    yoao_assets_storage_v1.enqueue_agent_command(uuid,uuid,uuid,integer,uuid,jsonb,text,timestamptz),
    yoao_assets_storage_v1.agent_lease_candidates(uuid,integer,text,uuid),
    yoao_assets_storage_v1.claim_agent_command(uuid,uuid,integer,text,uuid,text),
    yoao_assets_storage_v1.record_agent_chunk(uuid,uuid,integer,text,text,integer,integer,text),
    yoao_assets_storage_v1.record_agent_result(uuid,uuid,integer,text,text,jsonb,text,text),
    yoao_assets_storage_v1.lock_agent_ack(uuid),
    yoao_assets_storage_v1.acknowledge_agent_result(uuid,text,boolean,text),
    yoao_assets_storage_v1.cleanup_agent_relays(),
    yoao_assets_storage_v1.release_agent_relay(uuid,text),
    yoao_assets_storage_v1.expire_agent_command(uuid),
    yoao_assets_storage_v1.abandon_agent_command(uuid,uuid,text)
TO :"platform_runtime";
GRANT USAGE ON SCHEMA yoao_assets_v1, yoao_labeling_v1, yoao_workspace_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.lock_original_item(uuid,boolean),
    yoao_assets_v1.original_asset_for_item(uuid,boolean),
    yoao_assets_v1.managed_original_locations(uuid,boolean,boolean),
    yoao_assets_v1.task_original_source(uuid,boolean),
    yoao_assets_v1.auxiliary_original_source(uuid,boolean),
    yoao_assets_v1.auxiliary_original_item(uuid,boolean),
    yoao_assets_v1.auxiliary_original_scene(uuid,boolean),
    yoao_assets_v1.publish_task_original_source(uuid),
    yoao_assets_v1.publish_auxiliary_original_source(uuid),
    yoao_assets_v1.managed_original_location_ref(uuid,uuid,text,text),
    yoao_assets_v1.original_binding_history(),
    yoao_assets_v1.local_original_receipt(uuid),
    yoao_assets_v1.local_original_for_owner_key(text,uuid,text),
    yoao_assets_v1.abandon_unstarted_fixture_original(uuid,uuid,uuid,boolean),
    yoao_assets_v1.fixture_auxiliary_original_material(uuid),
    yoao_assets_v1.fixture_original_target_matches(uuid,boolean),
    yoao_assets_v1.allocate_local_original(jsonb),
    yoao_assets_v1.record_local_original_step(uuid,text,text,jsonb),
    yoao_assets_v1.protect_original_group(text,text),
    yoao_assets_v1.attach_verified_original(uuid,uuid),
    yoao_assets_v1.legacy_original_registry_snapshot(),
    yoao_assets_v1.observe_original_reclamation(text,uuid,text,text,text,jsonb,uuid,uuid,integer,integer),
    yoao_assets_v1.original_cache_scope(uuid,uuid,boolean),
    yoao_assets_v1.original_cache_usage(uuid),
    yoao_assets_v1.original_cache_charge(uuid),
    yoao_assets_v1.original_cache_current_state(uuid),
    yoao_assets_v1.original_cache_entry_facts(uuid,text),
    yoao_assets_v1.reserve_original_cache_generation(jsonb),
    yoao_assets_v1.original_cache_has_readers(uuid,timestamptz),
    yoao_assets_v1.retire_original_cache_generation(uuid,timestamptz,timestamptz,boolean),
    yoao_assets_v1.retire_original_cache_page(uuid,timestamptz,timestamptz,integer),
    yoao_assets_v1.publish_original_cache_generation(uuid,timestamptz),
    yoao_assets_v1.lock_original_cache_read(uuid,uuid),
    yoao_assets_v1.original_cache_locator_current(uuid),
    yoao_assets_v1.begin_original_cache_read(uuid,uuid,timestamptz),
    yoao_assets_v1.original_cache_lease_current(uuid,uuid,timestamptz),
    yoao_assets_v1.end_original_cache_read(uuid,uuid),
    yoao_assets_v1.expire_original_cache_reads(uuid,timestamptz),
    yoao_assets_v1.evict_original_cache_generation(uuid,timestamptz),
    yoao_assets_v1.reschedule_original_cache_check(uuid,timestamptz),
    yoao_assets_v1.record_original_cache_cleanup_error(uuid,text),
    yoao_assets_v1.finish_original_cache_cleanup(uuid,timestamptz),
    yoao_assets_v1.due_original_cache_checks(timestamptz,integer),
    yoao_assets_v1.reclaim_attempt_facts(uuid,boolean),
    yoao_assets_v1.reclaim_attempt_for_job(uuid),
    yoao_assets_v1.reclaim_history_page(uuid,integer,integer),
    yoao_assets_v1.reclaim_history_count(uuid),
    yoao_assets_v1.reclaim_claim_facts(text,text,boolean),
    yoao_assets_v1.reclaim_creation_active(uuid),
    yoao_assets_v1.admit_original_reclaim(jsonb,uuid,integer),
    yoao_assets_v1.claim_original_reclaim_attempt(uuid,text,timestamptz),
    yoao_assets_v1.request_original_reclaim_cancel(uuid,text,timestamptz),
    yoao_assets_v1.finish_original_reclaim_attempt(uuid,text,text,text,text,text,timestamptz,timestamptz,boolean,jsonb,jsonb),
    yoao_assets_v1.acquire_original_reclaim_operation(uuid,text,uuid,text),
    yoao_assets_v1.reclaim_version_may_be_deleted(uuid,text),
    yoao_assets_v1.checkpoint_original_reclaim(uuid,text,text,jsonb,timestamptz),
    yoao_assets_v1.original_reclaim_gate_blocked(uuid),
    yoao_assets_v1.original_reclaim_source_blocked(text[]),
    yoao_assets_v1.original_reconciliation_facts(boolean,boolean),
    yoao_assets_v1.queue_original_reconciliation(uuid,text,uuid,text),
    yoao_assets_v1.advance_original_reconciliation(uuid,text,text,text,text,jsonb,timestamptz,jsonb),
    yoao_assets_v1.observe_original_retention(uuid,uuid,text,timestamptz,uuid,text,text,integer,integer),
    yoao_assets_v1.original_retirement_anchor(uuid,boolean),
    yoao_assets_v1.lock_original_retention_hold(uuid,uuid),
    yoao_assets_v1.apply_original_retention(uuid,integer,text,timestamptz,uuid,integer,text,text,text,uuid,timestamptz),
    yoao_assets_v1.observe_original_verification(uuid),
    yoao_assets_v1.original_verification_facts(uuid,boolean),
    yoao_assets_v1.original_verification_for_job(uuid),
    yoao_assets_v1.active_original_verification(uuid,boolean),
    yoao_assets_v1.original_verification_unsettled(uuid),
    yoao_assets_v1.original_verification_history(uuid,integer,integer),
    yoao_assets_v1.original_verification_count(uuid),
    yoao_assets_v1.admit_original_verification(jsonb),
    yoao_assets_v1.claim_original_verification(uuid,text,timestamptz),
    yoao_assets_v1.request_original_verification_cancel(uuid,text,timestamptz),
    yoao_assets_v1.bind_original_verification_permit(uuid,text,uuid),
    yoao_assets_v1.finish_original_verification(uuid,text,text,text,text,text,timestamptz,timestamptz,jsonb),
    yoao_assets_v1.original_retirement_candidates(uuid,uuid),
    yoao_assets_v1.append_original_retirement_assertions(uuid,jsonb),
    yoao_assets_v1.dataset_original_binding_history(),
    yoao_assets_v1.lock_point_cloud_original_scenes(uuid[]),
    yoao_assets_v1.lock_point_cloud_original_poses(uuid[]),
    yoao_assets_v1.lock_point_cloud_original_items(uuid[]),
    yoao_labeling_v1.lock_point_cloud_original_run(uuid),
    yoao_workspace_v1.lock_point_cloud_original_configuration(uuid)
TO :"application_role";
GRANT USAGE ON SCHEMA yoao_assets_v1, yoao_labeling_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.active_dataset_scan(uuid, uuid),
    yoao_assets_v1.active_directory_registration(uuid, uuid),
    yoao_assets_v1.append_directory_source_revision(uuid, uuid, uuid, text, uuid, uuid, text, boolean, uuid, uuid, uuid, integer, text),
    yoao_assets_v1.apply_directory_source_selection(uuid, uuid, integer, integer, jsonb),
    yoao_assets_v1.arm_directory_registration_dispatch(uuid, uuid, text),
    yoao_assets_v1.bind_dataset_scan_attempt(uuid, uuid, integer, uuid, uuid, text),
    yoao_assets_v1.capture_dataset_scan_directory(uuid, uuid, text, integer, uuid, text, text, jsonb, jsonb),
    yoao_assets_v1.capture_dataset_scan_s3_page(uuid, uuid, text, integer, uuid, text, integer, text, integer, jsonb),
    yoao_assets_v1.change_dataset_archive_state(uuid, uuid, integer, boolean, timestamptz),
    yoao_assets_v1.change_directory_reference_replica(uuid, uuid, uuid, integer),
    yoao_assets_v1.change_directory_source_lifecycle(uuid, uuid, integer, integer, text, timestamptz),
    yoao_assets_v1.check_directory_revision_selection(uuid, text, integer, uuid, text, text),
    yoao_assets_v1.claim_dataset_scan(uuid, uuid, text, text, text),
    yoao_assets_v1.claim_directory_registration(uuid, uuid, text, text, uuid),
    yoao_assets_v1.claim_directory_sync_wakeup(uuid, uuid, timestamptz),
    yoao_assets_v1.configure_directory_sync(uuid, integer, uuid, uuid, text, boolean, integer, text, text, integer, integer, jsonb, timestamptz),
    yoao_assets_v1.confirm_directory_registration(uuid, uuid, integer, text),
    yoao_assets_v1.consume_directory_relocation(uuid, text, uuid, uuid),
    yoao_assets_v1.continue_directory_sync(uuid, integer, uuid, timestamptz),
    yoao_assets_v1.create_dataset_directory_source(uuid, uuid, uuid, text, text, text, jsonb),
    yoao_assets_v1.create_directory_preflight(uuid, uuid, uuid, text),
    yoao_assets_v1.create_directory_reference_replica(uuid, uuid, uuid, integer),
    yoao_assets_v1.dataset_archive_impact(uuid),
    yoao_assets_v1.dataset_cover_facts(uuid),
    yoao_assets_v1.dataset_display_item_page(uuid, uuid[], integer, bigint),
    yoao_assets_v1.dataset_item_at_object_key(uuid, text),
    yoao_assets_v1.dataset_item_count(uuid),
    yoao_assets_v1.dataset_item_path_facts(uuid),
    yoao_assets_v1.dataset_managed_duplicate_candidates(uuid, uuid, text, text),
    yoao_assets_v1.dataset_metadata(uuid, boolean, boolean),
    yoao_assets_v1.dataset_owns_directory_media(uuid),
    yoao_assets_v1.dataset_project_ids(uuid),
    yoao_assets_v1.dataset_quality_apply_review(uuid, uuid, uuid, integer, text, text, timestamptz),
    yoao_assets_v1.dataset_quality_attach_job(uuid, integer, uuid, uuid, uuid, uuid, text, integer, jsonb, timestamptz),
    yoao_assets_v1.dataset_quality_begin(uuid, uuid, jsonb, integer),
    yoao_assets_v1.dataset_quality_expired_runs(timestamptz),
    yoao_assets_v1.dataset_quality_key_matches(uuid, integer, text),
    yoao_assets_v1.dataset_quality_lock_locations(uuid, uuid[]),
    yoao_assets_v1.dataset_quality_lock_result(uuid, uuid),
    yoao_assets_v1.dataset_quality_maintenance_members(uuid),
    yoao_assets_v1.dataset_quality_next_result(uuid),
    yoao_assets_v1.dataset_quality_other_active(uuid, uuid),
    yoao_assets_v1.dataset_quality_reconcile(uuid, boolean, timestamptz),
    yoao_assets_v1.dataset_quality_record_result(uuid, uuid, integer, text, uuid, timestamptz, text, text, boolean, jsonb, integer, integer, timestamptz),
    yoao_assets_v1.dataset_quality_result_page(uuid, uuid, integer, integer, text, text, text),
    yoao_assets_v1.dataset_quality_review_attach_job(uuid, integer, uuid, uuid, uuid, uuid, text, integer, timestamptz, text),
    yoao_assets_v1.dataset_quality_review_attempt(uuid, uuid, text, integer, text, timestamptz),
    yoao_assets_v1.dataset_quality_review_batch(uuid, uuid, uuid, boolean),
    yoao_assets_v1.dataset_quality_review_batch_page(uuid, uuid, integer, integer),
    yoao_assets_v1.dataset_quality_review_checkpoint(uuid, uuid, integer, text, uuid, integer, timestamptz, timestamptz),
    yoao_assets_v1.dataset_quality_review_create_preview(uuid, uuid, integer, jsonb, text, text, text, timestamptz, timestamptz, jsonb),
    yoao_assets_v1.dataset_quality_review_expired_batches(timestamptz),
    yoao_assets_v1.dataset_quality_review_key_matches(uuid, integer, text),
    yoao_assets_v1.dataset_quality_review_member_page(uuid, uuid, uuid, integer, integer),
    yoao_assets_v1.dataset_quality_review_next_member(uuid),
    yoao_assets_v1.dataset_quality_review_prepare_preview(uuid, uuid, timestamptz),
    yoao_assets_v1.dataset_quality_review_preview_rows(uuid, uuid, text, text, text, uuid[]),
    yoao_assets_v1.dataset_quality_review_reconcile(uuid, boolean, timestamptz),
    yoao_assets_v1.dataset_quality_review_transition(uuid, uuid, integer, text, text, timestamptz, timestamptz, text),
    yoao_assets_v1.dataset_quality_run(uuid, uuid, uuid, boolean),
    yoao_assets_v1.dataset_quality_run_attempt(uuid, uuid, text, integer, text, timestamptz),
    yoao_assets_v1.dataset_quality_run_page(uuid, uuid, integer, integer),
    yoao_assets_v1.dataset_quality_run_transition(uuid, uuid, integer, text, text, timestamptz, timestamptz, text),
    yoao_assets_v1.dataset_scan_dispatch_route(uuid),
    yoao_assets_v1.dataset_scan_for_job(uuid),
    yoao_assets_v1.dataset_scan_snapshot(uuid),
    yoao_assets_v1.dataset_scene_choices(uuid, uuid),
    yoao_assets_v1.dataset_workbench_cache_candidates(uuid, uuid, uuid[], uuid[]),
    yoao_assets_v1.dataset_workbench_item_matches(uuid, uuid, uuid, text, text, uuid, text, text, text, integer, integer, integer, integer, uuid),
    yoao_assets_v1.dataset_workbench_item_states(uuid, uuid),
    yoao_assets_v1.dataset_workbench_page_observations(uuid, uuid, uuid, uuid[], uuid[]),
    yoao_assets_v1.dataset_workbench_preview_mapping(uuid, uuid, uuid),
    yoao_assets_v1.dataset_workbench_source_reference_evidence(uuid, uuid[], uuid[], uuid[], text[]),
    yoao_assets_v1.dataset_workbench_source_runs(uuid, uuid[]),
    yoao_assets_v1.declare_directory_source_protection(uuid, uuid, integer, jsonb),
    yoao_assets_v1.directory_confirmation_sample(uuid, text),
    yoao_assets_v1.directory_dataset_names(uuid, uuid, uuid[]),
    yoao_assets_v1.directory_preflight_exclude_unseen(uuid, text[]),
    yoao_assets_v1.directory_preflight_unseen(uuid),
    yoao_assets_v1.directory_preview_is_current(uuid, text, uuid, text),
    yoao_assets_v1.directory_preview_slot(uuid, text),
    yoao_assets_v1.directory_reference_binding(uuid, text, boolean),
    yoao_assets_v1.directory_reference_content(uuid),
    yoao_assets_v1.directory_reference_lineage(uuid, uuid, uuid, text, uuid),
    yoao_assets_v1.directory_reference_reclaim_facts(uuid, uuid, text),
    yoao_assets_v1.directory_reference_replica_available(uuid, uuid),
    yoao_assets_v1.directory_reference_replica_edge(uuid, uuid),
    yoao_assets_v1.directory_reference_replica_page(uuid, uuid, integer, integer, boolean, uuid),
    yoao_assets_v1.directory_reference_route_order(uuid, uuid),
    yoao_assets_v1.directory_registration_binding(uuid),
    yoao_assets_v1.directory_registration_change_kinds(uuid),
    yoao_assets_v1.directory_registration_changes(uuid, text, integer, integer),
    yoao_assets_v1.directory_registration_entries(uuid, text, integer, integer),
    yoao_assets_v1.directory_registration_for_job(uuid),
    yoao_assets_v1.directory_registration_for_scan(uuid),
    yoao_assets_v1.directory_registration_probe_paths(uuid),
    yoao_assets_v1.directory_relocation_candidates(uuid, text, integer, integer),
    yoao_assets_v1.directory_revision_for_observation(uuid, uuid, text),
    yoao_assets_v1.directory_scan_entry_page(uuid, uuid, integer, bigint),
    yoao_assets_v1.directory_source_active_attempts(uuid, uuid),
    yoao_assets_v1.directory_source_display_locations(uuid, uuid[]),
    yoao_assets_v1.directory_source_impact(uuid, uuid),
    yoao_assets_v1.directory_source_revision_history(uuid, text, integer, integer),
    yoao_assets_v1.directory_source_selection_heads(uuid, uuid),
    yoao_assets_v1.directory_source_usage(uuid, uuid[]),
    yoao_assets_v1.due_directory_registrations(timestamptz),
    yoao_assets_v1.due_directory_sync_sources(timestamptz),
    yoao_assets_v1.edit_dataset_directory_source(uuid, uuid, integer, text, boolean, text),
    yoao_assets_v1.fail_directory_confirmation_sample(uuid, uuid, integer, text, text),
    yoao_assets_v1.fail_directory_sync_wakeup(uuid, integer, uuid, text, boolean, timestamptz),
    yoao_assets_v1.filtered_dataset_catalog(uuid, text, text[], boolean, text, uuid, integer, bigint),
    yoao_assets_v1.finish_directory_registration(uuid, uuid, text, text, text),
    yoao_assets_v1.list_dataset_scans(uuid, uuid, boolean, boolean, integer, integer),
    yoao_assets_v1.lock_dataset_directory_source(uuid),
    yoao_assets_v1.lock_directory_reference_bindings(uuid[], uuid[], uuid[], uuid[]),
    yoao_assets_v1.lock_directory_reference_location(uuid, uuid),
    yoao_assets_v1.lock_directory_reference_replica_primary(uuid, uuid),
    yoao_assets_v1.lock_managed_item_asset_slot(uuid, text),
    yoao_assets_v1.next_dataset_scan_directory(uuid, uuid, text, integer, uuid),
    yoao_assets_v1.next_directory_registration_entry(uuid, uuid, text, integer, uuid),
    yoao_assets_v1.note_directory_registration_dispatch_failure(uuid, uuid, text),
    yoao_assets_v1.open_dataset_scan_s3_page(uuid, uuid, text, integer, uuid),
    yoao_assets_v1.open_directory_registration_attempt(uuid, uuid, uuid, uuid, uuid, text),
    yoao_assets_v1.pin_legacy_dataset_prefix(uuid, integer),
    yoao_assets_v1.project_has_dataset(uuid, uuid),
    yoao_assets_v1.publish_directory_preview(uuid, text, uuid, text, integer, integer, integer, integer, timestamptz, uuid),
    yoao_assets_v1.publish_directory_reference(uuid, uuid, uuid, text, text, bigint),
    yoao_assets_v1.publish_directory_reference_head(uuid, uuid, uuid, uuid, text),
    yoao_assets_v1.publish_managed_dataset_item(uuid, integer, text, text, text, bigint, text, integer, integer, text, boolean),
    yoao_assets_v1.read_dataset_scan(uuid),
    yoao_assets_v1.read_dataset_scan_directory(uuid, uuid, text, integer, uuid, text),
    yoao_assets_v1.read_dataset_scan_execution(uuid, boolean),
    yoao_assets_v1.read_directory_registration(uuid, boolean),
    yoao_assets_v1.read_directory_source_head(uuid, text),
    yoao_assets_v1.read_directory_source_revision(uuid, uuid),
    yoao_assets_v1.read_directory_sync_schedule(uuid, boolean),
    yoao_assets_v1.read_observed_dataset_scan_file(uuid, uuid, text, boolean),
    yoao_assets_v1.record_directory_registration_entry(uuid, uuid, text, integer, uuid, text, text, text, uuid, bigint, text, bigint, bigint, bigint, boolean),
    yoao_assets_v1.register_managed_item_asset(uuid, uuid, text, uuid, text, jsonb, text, bigint, text, timestamptz),
    yoao_assets_v1.requeue_directory_registration(uuid, uuid, text, integer, uuid, text, integer, text, boolean),
    yoao_assets_v1.retry_directory_registration_entry(uuid, integer, text),
    yoao_assets_v1.search_dataset_names(uuid, text, integer),
    yoao_assets_v1.settle_dataset_scan(uuid, uuid, text, integer, uuid, text, text, text),
    yoao_assets_v1.settle_directory_sync_wakeup(uuid, integer, uuid, text, uuid, text, timestamptz),
    yoao_assets_v1.start_dataset_scan(uuid, uuid, jsonb),
    yoao_assets_v1.validate_dataset_scan_directory(uuid, uuid, text, integer, uuid, text, text, text),
    yoao_assets_v1.verify_observed_dataset_scan_file(uuid, uuid, text, jsonb),
    yoao_assets_v1.visible_dataset_sources(uuid, uuid, uuid, uuid),
    yoao_labeling_v1.lock_point_quality_annotation_geometry(uuid)
    TO :"application_role";
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
GRANT USAGE ON SCHEMA yoao_export_v1, yoao_assets_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION
    yoao_export_v1.zip_receipt(uuid,text),
    yoao_export_v1.reserve_zip(uuid,uuid,uuid,uuid,text,text,integer,text,bigint,jsonb,timestamptz,timestamptz),
    yoao_export_v1.save_zip_proof(uuid,jsonb), yoao_export_v1.publish_zip(uuid),
    yoao_export_v1.allocate_media(uuid,uuid,uuid,uuid,jsonb,text,jsonb,jsonb,integer,bigint,jsonb,timestamptz,timestamptz),
    yoao_export_v1.media_delivery(uuid,boolean), yoao_export_v1.media_fence(uuid,text),
    yoao_export_v1.claim_media(uuid,uuid,timestamptz),
    yoao_export_v1.publish_media(uuid,uuid,timestamptz,timestamptz),
    yoao_export_v1.media_version(uuid,uuid), yoao_export_v1.media_versions(uuid),
    yoao_export_v1.find_cache(text,timestamptz), yoao_export_v1.lock_cache(uuid),
    yoao_export_v1.invalidate_cache(uuid,uuid), yoao_export_v1.record_cache(text,uuid,uuid),
    yoao_export_v1.media_attempt_ended(uuid,timestamptz),
    yoao_export_v1.media_has_operations(uuid,uuid),
    yoao_export_v1.lock_media_retirement(uuid,boolean),
    yoao_export_v1.start_media_retirement(uuid,timestamptz),
    yoao_export_v1.finish_media_retirement(uuid,timestamptz),
    yoao_export_v1.media_retirement_queue(timestamptz,integer),
    yoao_export_v1.zip_retirement_bundle(uuid),
    yoao_export_v1.start_zip_retirement(uuid,timestamptz),
    yoao_export_v1.zip_retirement_unreferenced(uuid),
    yoao_export_v1.remove_retired_zip(uuid,timestamptz),
    yoao_export_v1.purge_legacy_cache(timestamptz,integer),
    yoao_export_v1.zip_retirement_queue(timestamptz,integer),
    yoao_export_v1.binding_history(), yoao_export_v1.saved_location_is_referenced(uuid),
    yoao_export_v1.export_zip_inventory(uuid,integer), yoao_export_v1.export_media_inventory(uuid,integer),
    yoao_export_v1.export_media_inventory_objects(uuid,text,integer),
    yoao_export_v1.export_zip_usage(uuid[]), yoao_export_v1.export_media_usage(uuid[])
    TO :"application_role";
GRANT SELECT ON yoao_assets_v1.export_dataset_facts, yoao_assets_v1.export_item_facts,
    yoao_assets_v1.export_scene_facts, yoao_assets_v1.export_calibration_facts,
    yoao_assets_v1.export_pose_facts TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.lock_export_datasets_share(uuid[]),
    yoao_assets_v1.lock_export_items_update(uuid[]), yoao_assets_v1.lock_export_scenes_update(uuid[]),
    yoao_assets_v1.lock_export_calibrations_share(uuid[]), yoao_assets_v1.lock_export_poses_share(uuid[])
    TO :"application_role";
GRANT USAGE ON SCHEMA yoao_labeling_v1, yoao_workspace_v1, yoao_assets_v1
    TO :"application_role";
GRANT EXECUTE ON FUNCTION
    yoao_labeling_v1.create_uploading_task(uuid,uuid,text,text,text,uuid),
    yoao_labeling_v1.mask_ai_inventory(),
    yoao_workspace_v1.guide_clone_snapshot(uuid),
    yoao_workspace_v1.stage_guide_upload(uuid,uuid,uuid,jsonb,text,integer,text,text,timestamptz),
    yoao_workspace_v1.lock_guide_upload(uuid,boolean),
    yoao_workspace_v1.mount_guide_upload(uuid,uuid),
    yoao_workspace_v1.guide_upload_has_reference(uuid),
    yoao_workspace_v1.guide_retirement_candidates(integer),
    yoao_workspace_v1.attempt_guide_retirement(uuid,timestamptz),
    yoao_workspace_v1.settle_guide_retirement(uuid,jsonb),
    yoao_workspace_v1.guide_binding_history(),
    yoao_labeling_v1.stage_mask_format_upload(uuid,uuid,uuid,text,text,jsonb,timestamptz),
    yoao_labeling_v1.mask_format_upload_source(text,boolean),
    yoao_labeling_v1.mask_format_binding_history(),
    yoao_labeling_v1.stage_comment_attachment(uuid,uuid,uuid,uuid,uuid,jsonb,text,integer,text,text,timestamptz),
    yoao_labeling_v1.lock_comment_attachment(uuid,boolean),
    yoao_labeling_v1.comment_attachment_binding(uuid),
    yoao_labeling_v1.publish_comment_attachment(uuid,jsonb),
    yoao_labeling_v1.bind_comment_attachment(uuid,uuid),
    yoao_labeling_v1.retire_comment_attachments(uuid[],timestamptz),
    yoao_labeling_v1.comment_attachment_retirement_candidates(timestamptz,integer),
    yoao_labeling_v1.attempt_comment_attachment_retirement(uuid,timestamptz),
    yoao_labeling_v1.settle_comment_attachment_retirement(uuid,jsonb,timestamptz),
    yoao_labeling_v1.annotation_binding_history(),
    yoao_labeling_v1.stage_raster_object(uuid,text,text,integer,integer,integer,integer,text,text,integer,jsonb,timestamptz),
    yoao_labeling_v1.ready_raster_owner(text,boolean),
    yoao_labeling_v1.lock_raster_owners(uuid[],boolean),
    yoao_labeling_v1.lock_raster_ready_for_publish(uuid),
    yoao_labeling_v1.publish_raster_object(uuid,jsonb,timestamptz),
    yoao_labeling_v1.touch_ready_raster_object(uuid,jsonb,timestamptz),
    yoao_labeling_v1.retire_raster_object(uuid,text,jsonb,timestamptz,timestamptz),
    yoao_labeling_v1.raster_physical_gc_candidates(timestamptz,integer,boolean),
    yoao_labeling_v1.note_raster_gc_attempts(uuid[],timestamptz[]),
    yoao_labeling_v1.settle_raster_gc(uuid,jsonb),
    yoao_labeling_v1.open_raster_publication(uuid,uuid,integer,timestamptz),
    yoao_labeling_v1.raster_publication_matches(uuid,uuid,integer),
    yoao_labeling_v1.release_raster_publication(uuid,uuid,integer),
    yoao_labeling_v1.lock_raster_publications(uuid[]),
    yoao_labeling_v1.consume_raster_publications(uuid[],uuid[],integer[]),
    yoao_labeling_v1.raster_object_has_publication(uuid),
    yoao_labeling_v1.expire_unlinked_raster_uploads(timestamptz),
    yoao_labeling_v1.raster_physical_reference_keys(text),
    yoao_assets_v1.lock_video_segment_source(uuid,uuid)
    TO :"application_role";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.read_legacy_pyramid_asset(text,uuid,text),
    yoao_assets_v1.read_legacy_pyramid_generation(uuid,integer),
    yoao_assets_v1.read_legacy_pyramid_summaries(uuid[],uuid[],text),
    yoao_assets_v1.read_legacy_pyramid_context(uuid,integer,uuid,boolean),
    yoao_assets_v1.read_legacy_pyramid_output(uuid,boolean),
    yoao_assets_v1.invalidate_legacy_pyramid_generation(uuid,integer,uuid,text),
    yoao_assets_v1.touch_legacy_pyramid_generation(uuid,integer,uuid),
    yoao_assets_v1.read_legacy_video_chapters(uuid,uuid,uuid),
    yoao_assets_v1.lock_legacy_video_chapter_source(uuid,uuid,uuid),
    yoao_assets_v1.lock_legacy_video_chapter(uuid,uuid,uuid,uuid),
    yoao_assets_v1.create_legacy_video_chapter(uuid,uuid,uuid,uuid,integer,integer,text,text,jsonb),
    yoao_assets_v1.update_legacy_video_chapter(uuid,uuid,uuid,uuid,integer,integer,text,text,jsonb),
    yoao_assets_v1.delete_legacy_video_chapter(uuid,uuid,uuid,uuid),
    yoao_assets_v1.read_media_cache_binding_history(),
    yoao_assets_v1.legacy_thumbnail_output(uuid,boolean),
    yoao_assets_v1.legacy_item_thumbnail_pointer(uuid,text),
    yoao_assets_v1.directory_thumbnail_evidence(uuid[]),
    yoao_assets_v1.allocate_legacy_thumbnail(uuid,text,uuid,text,jsonb,jsonb,timestamptz),
    yoao_assets_v1.record_legacy_thumbnail_result(uuid,jsonb,text,bigint,text,text,boolean,boolean,timestamptz),
    yoao_assets_v1.legacy_directory_thumbnail_candidate(uuid,uuid,text,jsonb),
    yoao_assets_v1.retire_legacy_directory_thumbnail(uuid,timestamptz,timestamptz),
    yoao_assets_v1.legacy_video_item(uuid,text),
    yoao_assets_v1.legacy_video_slot(uuid,text,jsonb,text),
    yoao_assets_v1.legacy_video_create_slot(uuid,text,jsonb),
    yoao_assets_v1.legacy_video_attempt(uuid,text,uuid,text),
    yoao_assets_v1.legacy_video_allocate_output(uuid,uuid,text,jsonb,jsonb,jsonb,uuid,jsonb),
    yoao_assets_v1.legacy_video_attach_output(uuid,uuid,uuid),
    yoao_assets_v1.legacy_video_cache_output(uuid,boolean),
    yoao_assets_v1.legacy_video_dispatch_sources(uuid[]),
    yoao_assets_v1.legacy_video_pts(uuid,integer),
    yoao_assets_v1.legacy_video_failed_frames(uuid,integer,text,integer[]),
    yoao_assets_v1.legacy_video_touch_slot(text,uuid,uuid,uuid,timestamptz)
TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.prepare_legacy_pyramid_generation(uuid,text,uuid,text,text,jsonb,jsonb,text,boolean,boolean),
    yoao_assets_v1.lock_legacy_pyramid_claim(uuid,integer),
    yoao_assets_v1.claim_legacy_pyramid_generation(uuid,integer,uuid,uuid,integer,jsonb),
    yoao_assets_v1.lock_legacy_pyramid_publication(uuid,integer,uuid,uuid),
    yoao_assets_v1.lock_legacy_pyramid_output_for_publish(uuid,integer,uuid,uuid),
    yoao_assets_v1.publish_legacy_pyramid_generation(uuid,integer,uuid,uuid,jsonb,jsonb,bigint),
    yoao_assets_v1.fail_legacy_pyramid_generation(uuid,integer,uuid,text),
    yoao_assets_v1.scan_expired_legacy_pyramid_generations(),
    yoao_assets_v1.scan_stale_legacy_pyramid_generations(timestamptz),
    yoao_assets_v1.remove_stale_legacy_pyramid_generation(uuid,integer,timestamptz),
    yoao_assets_v1.scan_retirable_legacy_pyramid_outputs(timestamptz),
    yoao_assets_v1.begin_legacy_pyramid_retirement(uuid,timestamptz),
    yoao_assets_v1.check_legacy_pyramid_retirement(uuid,jsonb,uuid),
    yoao_assets_v1.finish_legacy_pyramid_retirement(uuid,jsonb),
    yoao_assets_v1.read_legacy_pyramid_retained_bytes(),
    yoao_assets_v1.legacy_thumbnail_business(text,uuid,boolean),
    yoao_assets_v1.set_legacy_item_dimensions(uuid,integer,integer),
    yoao_assets_v1.record_legacy_item_thumbnail_error(uuid,text),
    yoao_assets_v1.publish_legacy_thumbnail(text,uuid,uuid,jsonb,jsonb,text,timestamptz),
    yoao_assets_v1.legacy_thumbnail_gc_candidates(integer,timestamptz,integer),
    yoao_assets_v1.legacy_thumbnail_gc_start(uuid,timestamptz,integer),
    yoao_assets_v1.legacy_thumbnail_gc_admit(uuid,jsonb,uuid,timestamptz,integer),
    yoao_assets_v1.legacy_thumbnail_gc_finish(uuid,jsonb,timestamptz,integer),
    yoao_assets_v1.legacy_video_rebuild_items(uuid[],uuid,integer),
    yoao_assets_v1.legacy_video_locate_pending(uuid),
    yoao_assets_v1.legacy_video_claim_attempt(uuid,uuid,integer),
    yoao_assets_v1.legacy_video_fail_attempt(uuid,uuid,text),
    yoao_assets_v1.legacy_video_publish_attempt(uuid,uuid,jsonb,jsonb,timestamptz,integer,integer,text),
    yoao_assets_v1.legacy_video_ready_bytes(),
    yoao_assets_v1.legacy_video_expired_slots(text,timestamptz,integer),
    yoao_assets_v1.legacy_video_expire_slot(text,uuid,uuid,uuid,timestamptz),
    yoao_assets_v1.legacy_video_abandoned_attempts(timestamptz,integer),
    yoao_assets_v1.legacy_video_expire_attempt(uuid,timestamptz),
    yoao_assets_v1.legacy_video_retirement_candidates(timestamptz,timestamptz,integer),
    yoao_assets_v1.legacy_video_begin_sweep(uuid,timestamptz,timestamptz),
    yoao_assets_v1.legacy_video_sweep_attempt(uuid),
    yoao_assets_v1.legacy_video_sweep_permit(uuid,integer),
    yoao_assets_v1.legacy_video_finish_sweep(uuid,timestamptz)
TO :"application_role";
GRANT USAGE ON SCHEMA yoao_identity_v1 TO :"platform_runtime";
GRANT EXECUTE ON FUNCTION
    yoao_identity_v1.reserve_avatar_upload(uuid,text,jsonb,text,integer),
    yoao_identity_v1.confirm_avatar_upload_version(uuid,text,jsonb,text,integer,text),
    yoao_identity_v1.avatar_upload_pending_matches(uuid,text,jsonb,text,integer,text),
    yoao_identity_v1.read_ready_avatar_upload(text),
    yoao_identity_v1.list_avatar_retirements(integer),
    yoao_identity_v1.begin_avatar_retirement(text),
    yoao_identity_v1.finish_avatar_retirement(text,jsonb,text,integer,text),
    yoao_identity_v1.read_avatar_binding_history(),
    yoao_identity_v1.avatar_saved_target_referenced(uuid)
    TO :"platform_runtime";
GRANT USAGE ON SCHEMA yoao_jobs_v1 TO :"platform_runtime";
GRANT EXECUTE ON FUNCTION
    yoao_jobs_v1.dataset_job_visible(uuid,uuid),
    yoao_jobs_v1.directory_job_page(uuid,uuid,integer,integer),
    yoao_jobs_v1.directory_job_total(uuid,uuid),
    yoao_jobs_v1.active_storage_writer_job_counts()
    TO :"platform_runtime";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"platform_runtime";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.begin_saved_binding_probe(uuid,uuid,uuid,text,text,uuid,integer,integer,integer)
    TO :"platform_runtime";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.prepare_reference_io(uuid,text,uuid,uuid,text,jsonb)
    TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.admit_saved_output_read(uuid,text,uuid,uuid,uuid,text)
TO :"application_role";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.migration_io_bind_read(uuid,uuid,integer,jsonb)
TO :"application_role";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.prove_embedded_admitted_original(jsonb),
    yoao_assets_v1.begin_embedded_admitted_original(jsonb)
TO :"platform_runtime";
