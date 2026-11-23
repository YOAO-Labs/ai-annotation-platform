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
    yoao_inference_backend_probe_v1,
    yoao_inference_gpu_caller_v1,
    yoao_inference_health_data_v1,
    yoao_inference_health_observation_v1,
    yoao_inference_pipelines_v1,
    yoao_inference_registry_ingress_v1,
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
    yoao_inference_backend_probe_v1.backend_probe_policy(),
    yoao_inference_backend_probe_v1.open_backend_probe_subject(text,uuid)
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
    yoao_inference_health_observation_v1.health_observation_policy(),
    yoao_inference_health_observation_v1.open_health_observation_subject(text,uuid,uuid),
    yoao_inference_health_observation_v1.seal_health_observation(text,uuid,uuid,text),
    yoao_inference_health_observation_v1.verify_health_observation(text,uuid,uuid,text)
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_inference_pipelines_v1.pipeline_policy(),
    yoao_inference_pipelines_v1.pipeline_step(jsonb,text,jsonb,text[],text[])
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_inference_registry_ingress_v1.apply_observation(uuid,text,text,text,boolean,text,timestamptz,text,boolean,text),
    yoao_inference_registry_ingress_v1.create_registry(uuid,text,text,text,boolean,text),
    yoao_inference_registry_ingress_v1.create_singleton_pool(uuid,uuid,uuid,text,text,text),
    yoao_inference_registry_ingress_v1.disconnect_env_registry(uuid,text),
    yoao_inference_registry_ingress_v1.record_sync_result(uuid,text,integer),
    yoao_inference_registry_ingress_v1.registry_by_url(text),
    yoao_inference_registry_ingress_v1.registry_candidates(text),
    yoao_inference_registry_ingress_v1.registry_ingress_policy(),
    yoao_inference_registry_ingress_v1.singleton_pool(uuid),
    yoao_inference_registry_ingress_v1.sync_result(uuid,text),
    yoao_inference_registry_ingress_v1.tag_seed_registry(uuid,text)
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
