-- Run with psql as the migration owner after the application migration head is
-- installed. All changes are confined to the selected database and runtime role.
-- psql -v expected_database=annotation_production -v application_role=anno_prod_app
--       -v migration_role=anno_prod_owner -v asset_cleanup_role=yoao_asset_cleanup_client
--       -f lan-production-grants.sql
-- application_role composes embedded owners through their published capabilities.
-- Identity, Audit, and Notifications writes use owner capabilities; other legacy access remains.
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.application_role', :'application_role', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.asset_cleanup_role', :'asset_cleanup_role', true);

DO $$
BEGIN
    IF current_database() <> current_setting('yoao.expected_database') THEN
        RAISE EXCEPTION 'Unexpected database; refusing to change application grants';
    END IF;
    IF current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Run this script as the migration owner';
    END IF;
    IF current_setting('yoao.application_role') = current_user
       OR pg_has_role(current_setting('yoao.application_role'), current_user, 'MEMBER') THEN
        RAISE EXCEPTION 'The runtime role must not inherit the migration owner';
    END IF;
    IF current_setting('yoao.asset_cleanup_role') IN (
           current_setting('yoao.application_role'), current_user
       )
       OR pg_has_role(current_setting('yoao.asset_cleanup_role'), current_user, 'MEMBER')
       OR EXISTS (
           SELECT 1 FROM pg_catalog.pg_auth_members AS member
           WHERE member.member = (
               SELECT candidate.oid FROM pg_catalog.pg_roles AS candidate
               WHERE candidate.rolname = current_setting('yoao.asset_cleanup_role')
           ) OR member.roleid = (
               SELECT candidate.oid FROM pg_catalog.pg_roles AS candidate
               WHERE candidate.rolname = current_setting('yoao.asset_cleanup_role')
           )
       ) THEN
        RAISE EXCEPTION 'The Assets cleanup role must be distinct and have no role memberships';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_roles
        WHERE rolname = current_setting('yoao.application_role')
          AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'The runtime role must not hold cluster administration privileges';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = current_setting('yoao.asset_cleanup_role')
          AND rolcanlogin
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'The Assets cleanup role must be a non-admin LOGIN role';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM PUBLIC;
-- Retained Predictions and user-performance maintenance still needs these privileges.
GRANT CONNECT, TEMPORARY ON DATABASE :"expected_database" TO :"application_role";
REVOKE ALL ON DATABASE :"expected_database" FROM :"asset_cleanup_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"asset_cleanup_role";
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO :"application_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"application_role";
REVOKE ALL ON TABLE public.fixture_export_zip_retirements FROM :"application_role", PUBLIC;
REVOKE ALL ON TABLE public.image_export_scopes FROM :"application_role", PUBLIC;
REVOKE ALL (job_id, bundle_id, project_id, actor_id, job_born, dataset_ids)
    ON TABLE public.image_export_scopes FROM :"application_role", PUBLIC;
REVOKE ALL ON FUNCTION public.fixture_export_owner_session(),
    public.retire_fixture_native_export_output(uuid),
    public.confirm_fixture_export_zip_absent(uuid) FROM :"application_role", PUBLIC;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"application_role";
GRANT USAGE ON SCHEMA yoao_workspace_v1, yoao_workspace_membership_v1,
    yoao_workspace_owner_transfer_v1, yoao_inference_pipelines_v1,
    yoao_assets_v1, yoao_labeling_v1, yoao_jobs_v1, yoao_learning_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_inference_pipelines_v1.pipeline_policy(),
    yoao_inference_pipelines_v1.pipeline_step(jsonb, text, jsonb, text[], text[])
    TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_workspace_v1.clear_primary_backend_bindings(uuid)
    TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_workspace_v1.read_import_project(text),
    yoao_workspace_v1.create_import_project(uuid,text,text,uuid,jsonb),
    yoao_workspace_v1.create_point_cloud_seed_project(uuid,text,text,uuid,jsonb),
    yoao_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb),
    yoao_workspace_v1.write_seed_members(jsonb),
    yoao_workspace_v1.create_media_seed_project(uuid,text,text,uuid,text,jsonb),
    yoao_workspace_v1.write_seed_project_projection(uuid,jsonb),
    yoao_workspace_v1.pin_seed_member_assignment(uuid,timestamptz),
    yoao_workspace_v1.create_scale_seed_project(uuid,text),
    yoao_workspace_v1.ensure_scale_seed_member(uuid,uuid,uuid),
    yoao_workspace_v1.guard_scale_seed_cleanup(text),
    yoao_workspace_v1.delete_scale_seed_projects(text,uuid[]),
    yoao_assets_v1.guard_scale_seed_dataset_cleanup(uuid,boolean),
    yoao_workspace_v1.write_point_cloud_quality_config(uuid,jsonb)
    TO :"application_role";
GRANT USAGE ON SCHEMA yoao_inference_backend_probe_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_inference_backend_probe_v1.backend_probe_policy(),
    yoao_inference_backend_probe_v1.open_backend_probe_subject(text,uuid)
    TO :"application_role";
GRANT USAGE ON SCHEMA yoao_inference_health_observation_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_inference_health_observation_v1.health_observation_policy(),
    yoao_inference_health_observation_v1.capture_health_observation_subject(uuid,uuid,text,text,double precision),
    yoao_inference_health_observation_v1.open_health_observation_subject(text,uuid,uuid),
    yoao_inference_health_observation_v1.seal_health_observation(text,uuid,uuid,text),
    yoao_inference_health_observation_v1.verify_health_observation(text,uuid,uuid,text)
    TO :"application_role";
GRANT USAGE ON SCHEMA yoao_inference_health_data_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_inference_health_data_v1.health_data_policy(),
    yoao_inference_health_data_v1.backend_row(uuid,boolean),
    yoao_inference_health_data_v1.membership_row(uuid,text,boolean),
    yoao_inference_health_data_v1.observation_clock(),
    yoao_inference_health_data_v1.lock_resource(text),
    yoao_inference_health_data_v1.try_promotion(),
    yoao_inference_health_data_v1.write_backend(uuid,text,timestamp with time zone,text,boolean,boolean),
    yoao_inference_health_data_v1.member_pool(uuid),
    yoao_inference_health_data_v1.pool_row(uuid,uuid),
    yoao_inference_health_data_v1.member_row(uuid),
    yoao_inference_health_data_v1.apply_reconciliation(uuid,uuid,text,text,text,bigint,text,text)
    TO :"application_role";
GRANT USAGE ON SCHEMA yoao_inference_registry_ingress_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_inference_registry_ingress_v1.registry_ingress_policy(),
    yoao_inference_registry_ingress_v1.registry_by_url(text),
    yoao_inference_registry_ingress_v1.registry_candidates(text),
    yoao_inference_registry_ingress_v1.create_registry(uuid,text,text,text,boolean,text),
    yoao_inference_registry_ingress_v1.singleton_pool(uuid),
    yoao_inference_registry_ingress_v1.create_singleton_pool(uuid,uuid,uuid,text,text,text),
    yoao_inference_registry_ingress_v1.apply_observation(uuid,text,text,text,boolean,text,timestamp with time zone,text,boolean,text),
    yoao_inference_registry_ingress_v1.disconnect_env_registry(uuid,text),
    yoao_inference_registry_ingress_v1.tag_seed_registry(uuid,text),
    yoao_inference_registry_ingress_v1.sync_result(uuid,text),
    yoao_inference_registry_ingress_v1.record_sync_result(uuid,text,integer)
    TO :"application_role";
GRANT USAGE ON SCHEMA yoao_inference_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_inference_v1.ai_mask_pool_reference_row(uuid) TO :"application_role";
GRANT USAGE ON SCHEMA yoao_inference_routing_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_inference_routing_v1.routing_policy(),
    yoao_inference_routing_v1.routing_step(text,text),
    yoao_inference_routing_v1.prediction_scope_reserve(text),
    yoao_inference_routing_v1.prediction_test_scope_reserve(text),
    yoao_inference_routing_v1.scope_retirement_step(text,text),
    yoao_inference_routing_v1.video_execution_scope_step(text,text)
    TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.registry_evaluation_reference_exists(uuid),
    yoao_jobs_v1.count_running_registry_predictions(uuid),
    yoao_jobs_v1.video_execution_step(text,text),
    yoao_jobs_v1.video_execution_checkpoint_step(text,text) TO :"application_role";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.fence_cross_frame_job_dataset_scope(uuid,uuid,uuid,jsonb)
    TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.is_fixture_original_source(text,text)
    TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.fixture_media_owner_exists(text,uuid)
    TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.fixture_media_ordinary_gc(text,uuid,integer)
    TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.legacy_pyramid_gc_eligible(text,uuid,integer)
    TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.begin_operator_auxiliary(jsonb),
    yoao_assets_v1.open_operator_storage_operation(uuid),
    yoao_assets_v1.record_operator_original_prepared(uuid,jsonb),
    yoao_assets_v1.mark_operator_original_writing(uuid),
    yoao_assets_v1.record_operator_original_written(uuid,jsonb),
    yoao_assets_v1.record_operator_original_verified(uuid,jsonb),
    yoao_assets_v1.fail_operator_original(uuid,text,text,jsonb),
    yoao_assets_v1.close_operator_storage_operation(uuid,uuid,boolean)
    TO :"application_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.video_writer_step(text,text),
    yoao_labeling_v1.task_edit_lock_command(jsonb),
    yoao_labeling_v1.task_workflow_command(jsonb),
    yoao_labeling_v1.annotation_statistics_command(jsonb),
    yoao_labeling_v1.scene_track_domain_command(jsonb),
    yoao_labeling_v1.annotation_write_command(jsonb),
    yoao_labeling_v1.annotation_bulk_command(jsonb),
    yoao_labeling_v1.video_bbox_editing_command(jsonb),
    yoao_labeling_v1.batch_control_command(jsonb),
    yoao_labeling_v1.prediction_record_command(jsonb),
    yoao_labeling_v1.prediction_partition_command(jsonb),
    yoao_labeling_v1.scene_track_command(jsonb),
    yoao_labeling_v1.cross_frame_write_command(jsonb),
    yoao_labeling_v1.annotation_prediction_command(jsonb),
    yoao_labeling_v1.annotation_maintenance_command(jsonb),
    yoao_labeling_v1.discussion_command(jsonb),
    yoao_labeling_v1.task_event_command(jsonb),
    yoao_labeling_v1.mask_edit_command(jsonb),
    yoao_labeling_v1.mask_revision_command(jsonb),
    yoao_labeling_v1.ai_mask_accept_command(jsonb),
    yoao_labeling_v1.scene_read_query(jsonb),
    yoao_labeling_v1.lock_video_segment_for_core_read(uuid,uuid),
    yoao_labeling_v1.lock_task_rows_for_core_read(uuid[],uuid,uuid),
    yoao_labeling_v1.create_uploading_task(uuid,uuid,text,text,text,text,uuid),
    yoao_labeling_v1.lock_task_rows_for_core_read_share(uuid[],uuid,uuid),
    yoao_labeling_v1.lock_prediction_for_core_read_share(uuid,uuid),
    yoao_labeling_v1.lock_prediction_rows_for_core_read_share(uuid,uuid[],integer),
    yoao_labeling_v1.lock_scene_track_rows_for_core_read(uuid[],integer),
    yoao_labeling_v1.lock_scene_track_interval_rows_for_core_read_share(uuid[],integer),
    yoao_labeling_v1.lock_prediction_meta_for_core_read_share(uuid),
    yoao_labeling_v1.lock_video_segment_for_core_read_share(uuid,uuid),
    yoao_labeling_v1.lock_video_segment_ids_for_export_share(uuid[],integer),
    yoao_labeling_v1.lock_video_quality_run_ids_for_export_share(uuid[],integer),
    yoao_labeling_v1.publish_task_original(uuid,uuid,uuid,uuid,uuid,text),
    yoao_labeling_v1.lock_active_learning_feedback_task_rows(uuid,uuid[]),
    yoao_labeling_v1.lock_active_learning_feedback_annotation_rows(uuid[]),
    yoao_labeling_v1.next_batch_display_id(),
    yoao_labeling_v1.video_control_command(jsonb),
    yoao_labeling_v1.video_segment_lifecycle_command(jsonb),
    yoao_labeling_v1.screenshot_operations_command(jsonb),
    yoao_labeling_v1.annotation_transfer_command(jsonb),
    yoao_labeling_v1.lock_task_for_core_read(uuid,text,boolean,uuid),
    yoao_labeling_v1.lock_task_dataset_item_links_for_core_read(uuid,boolean,uuid,integer),
    yoao_labeling_v1.lock_task_dataset_item_link_rows_for_core_read_share(uuid[],integer),
    yoao_labeling_v1.lock_batch_rows_for_core_read(uuid[],uuid,text,boolean),
    yoao_labeling_v1.lock_annotation_rows_for_core_read(uuid[],uuid,uuid,text,boolean),
    yoao_labeling_v1.mask_qc_command(jsonb),
    yoao_labeling_v1.mask_repair_command(jsonb),
    yoao_labeling_v1.raster_mask_retention_query(jsonb),
    yoao_labeling_v1.spatial_quality_command(jsonb),
    yoao_labeling_v1.task_maintenance_command(jsonb),
    yoao_labeling_v1.purge_scale_seed_labeling_rows(text,uuid[]),
    yoao_labeling_v1.create_scale_seed_batches(text,uuid,uuid,integer,uuid),
    yoao_labeling_v1.create_scale_seed_tasks(text,uuid,uuid),
    yoao_labeling_v1.create_scale_seed_annotations(text,uuid,uuid,integer)
    TO :"application_role";
GRANT SELECT ON yoao_labeling_v1.task_rows,
    yoao_labeling_v1.batch_rows,
    yoao_labeling_v1.annotation_rows,
    yoao_labeling_v1.annotation_draft_rows,
    yoao_labeling_v1.prediction_rows,
    yoao_labeling_v1.prediction_meta_rows,
    yoao_labeling_v1.failed_prediction_rows,
    yoao_labeling_v1.task_lock_rows,
    yoao_labeling_v1.task_dataset_item_link_rows,
    yoao_labeling_v1.scene_track_rows,
    yoao_labeling_v1.scene_track_interval_rows,
    yoao_labeling_v1.video_segment_rows,
    yoao_labeling_v1.video_tracker_job_rows,
    yoao_labeling_v1.video_track_quality_run_rows,
    yoao_labeling_v1.annotation_comment_facts,
    yoao_labeling_v1.annotation_feedback_facts, yoao_labeling_v1.task_event_facts
    TO :"application_role";
-- Embedded Identity adapters use the same owner capabilities as isolated clients.
GRANT USAGE ON SCHEMA yoao_identity_v1, yoao_identity_keys_v1,
    yoao_identity_sessions_v1, yoao_identity_accounts_v1, yoao_identity_recovery_v1,
    yoao_identity_groups_v1, yoao_identity_deactivation_v1,
    yoao_identity_invitations_v1, yoao_identity_mail_v2,
    yoao_identity_directory_v1, yoao_identity_admin_passwords_v1,
    yoao_identity_platform_roles_v1, yoao_identity_suspensions_v1,
    yoao_identity_deletions_v1, yoao_identity_offboarding_v1,
    yoao_identity_briefs_v1,
    yoao_settings_v1, yoao_notifications_v1 TO :"application_role";
-- Existing API bindings call these owner functions inside the business transaction.
GRANT USAGE ON SCHEMA yoao_audit_v1 TO :"application_role";
REVOKE ALL ON FUNCTION yoao_audit_v1.maintenance_command(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION yoao_audit_v1.append_events(jsonb),
    yoao_audit_v1.query_events(pg_catalog.jsonb),
    yoao_audit_v1.maintenance_command(jsonb),
    yoao_notifications_v1.preference_command(jsonb),
    yoao_notifications_v1.append_notification(jsonb),
    yoao_notifications_v1.append_job_terminal_notification(jsonb),
    yoao_notifications_v1.inbox_page_scoped(uuid,text,boolean,integer,integer),
    yoao_notifications_v1.inbox_total_count_scoped(uuid,text),
    yoao_notifications_v1.inbox_unread_count_scoped(uuid,text),
    yoao_notifications_v1.read_state_command_scoped(jsonb),
    yoao_notifications_v1.delete_command_scoped(jsonb),
    yoao_notifications_v1.inbox_page(uuid,text,boolean,integer,integer),
    yoao_notifications_v1.inbox_total_count(uuid,text),
    yoao_notifications_v1.inbox_unread_count(uuid,text),
    yoao_notifications_v1.read_notification(uuid),
    yoao_notifications_v1.find_terminal_notification(uuid,text,uuid),
    yoao_notifications_v1.delivery_job_scopes(uuid[]),
    yoao_notifications_v1.delivery_member_pairs(jsonb),
    yoao_notifications_v1.delivery_reviewer_pairs(jsonb),
    yoao_notifications_v1.delivery_owner_pairs(jsonb),
    yoao_notifications_v1.delivery_super_pairs(jsonb),
    yoao_notifications_v1.read_state_command(jsonb),
    yoao_notifications_v1.delete_command(pg_catalog.jsonb),
    yoao_jobs_v1.async_job_list_page(uuid,text,text[],text[],uuid,text,integer,integer),
    yoao_jobs_v1.async_job_list_total(uuid,text,text[],text[],uuid,text),
    yoao_jobs_v1.read_job(uuid),
    yoao_jobs_v1.read_job_for_update(uuid),
    yoao_jobs_v1.read_job_for_share_nowait(uuid),
    yoao_jobs_v1.read_job_for_update_nowait(uuid),
    yoao_jobs_v1.lock_action_idempotency(text,uuid,uuid,text),
    yoao_jobs_v1.find_action_idempotent_jobs(text,uuid,uuid,text),
    yoao_jobs_v1.find_video_retry_job(uuid,uuid,text),
    yoao_jobs_v1.lock_video_retry_jobs(uuid),
    yoao_jobs_v1.lock_cross_frame_singleflight(uuid,text),
    yoao_jobs_v1.active_cross_frame_job(uuid,text),
    yoao_jobs_v1.cross_frame_job_page(uuid,text,integer),
    yoao_jobs_v1.cross_frame_job_count(uuid,text),
    yoao_jobs_v1.latest_batch_prediction_times(uuid[]),
    yoao_jobs_v1.frozen_deployment_usage(uuid,uuid,uuid),
    yoao_jobs_v1.jobs_by_execution_id(text),
    yoao_jobs_v1.insert_video_summary(uuid,text,uuid,uuid,jsonb,text),
    yoao_jobs_v1.apply_lifecycle(uuid,jsonb),
    yoao_jobs_v1.bind_prediction_execution_snapshot(uuid,uuid,uuid),
    yoao_jobs_v1.read_job_execution_snapshot(uuid),
    yoao_jobs_v1.read_job_progress(uuid),
    yoao_jobs_v1.read_job_progress_for_update(uuid,text,text),
    yoao_jobs_v1.record_job_progress(uuid,text,text,timestamptz,jsonb),
    yoao_jobs_v1.mark_dispatch_failed(uuid,timestamptz,text),
    yoao_jobs_v1.cancel_batch_predict_jobs(uuid,timestamptz),
    yoao_jobs_v1.delete_batch_predict_jobs(uuid),
    yoao_jobs_v1.delete_terminal_jobs_before(timestamptz,text[]),
    yoao_jobs_v1.prepare_dispatch(jsonb),
    yoao_jobs_v1.finish_dispatch(jsonb),
    yoao_jobs_v1.dispatch_status(uuid)
    TO :"application_role";
GRANT SELECT, INSERT ON yoao_jobs_v1.async_job_creation TO :"application_role";
REVOKE EXECUTE ON FUNCTION yoao_jobs_v1.claim_dispatches(jsonb)
    FROM :"application_role", PUBLIC;
REVOKE ALL ON TABLE yoao_jobs_v1.dispatch_intents FROM :"application_role", PUBLIC;
REVOKE ALL (
    intent_id, job_id, purpose, execution_id, envelope, natural_identity,
    state, lease_token, generation, attempts, lease_expires_at,
    next_attempt_at, accepted_at, created_at, updated_at
) ON TABLE yoao_jobs_v1.dispatch_intents FROM :"application_role", PUBLIC;
DO $jobs_dispatch_acl$
DECLARE
    v_runtime name := current_setting('yoao.application_role');
BEGIN
    IF pg_catalog.has_table_privilege(
        v_runtime, 'yoao_jobs_v1.dispatch_intents',
        'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
    ) OR pg_catalog.has_any_column_privilege(
        v_runtime, 'yoao_jobs_v1.dispatch_intents', 'SELECT,INSERT,UPDATE,REFERENCES'
    ) OR pg_catalog.has_function_privilege(
        v_runtime, 'yoao_jobs_v1.claim_dispatches(jsonb)', 'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Runtime retains private Jobs dispatch or relay authority';
    END IF;
END
$jobs_dispatch_acl$;
DO $jobs_creation_acl$
DECLARE
    v_runtime name := current_setting('yoao.application_role');
    v_columns text;
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_class
        WHERE oid IN ('public.async_jobs'::regclass,
                      'yoao_jobs_v1.async_job_creation'::regclass)
          AND pg_catalog.pg_has_role(v_runtime, relowner, 'MEMBER')
    ) THEN
        RAISE EXCEPTION 'The runtime role must not own or inherit Jobs creation authority';
    END IF;
    EXECUTE format('REVOKE INSERT ON TABLE public.async_jobs FROM %I, PUBLIC',
                   v_runtime);
    SELECT string_agg(format('%I', attname), ', ' ORDER BY attnum)
    INTO v_columns FROM pg_catalog.pg_attribute
    WHERE attrelid = 'public.async_jobs'::regclass
      AND attnum > 0 AND NOT attisdropped;
    IF v_columns IS NOT NULL THEN
        EXECUTE format('REVOKE INSERT (%s) ON TABLE public.async_jobs FROM %I, PUBLIC',
                       v_columns, v_runtime);
    END IF;
    IF pg_catalog.has_table_privilege(v_runtime, 'public.async_jobs', 'INSERT')
       OR pg_catalog.has_any_column_privilege(v_runtime, 'public.async_jobs', 'INSERT') THEN
        RAISE EXCEPTION 'The runtime role still has effective private Jobs INSERT privileges';
    END IF;
    IF NOT pg_catalog.has_table_privilege(v_runtime, 'yoao_jobs_v1.async_job_creation', 'SELECT')
       OR NOT pg_catalog.has_table_privilege(v_runtime, 'yoao_jobs_v1.async_job_creation', 'INSERT') THEN
        RAISE EXCEPTION 'The runtime role is missing the Jobs creation capability';
    END IF;
END
$jobs_creation_acl$;
DO $jobs_delete_acl$
DECLARE
    v_runtime name := current_setting('yoao.application_role');
BEGIN
    EXECUTE format('REVOKE DELETE ON TABLE public.async_jobs FROM %I, PUBLIC',
                   v_runtime);
    IF pg_catalog.has_table_privilege(v_runtime, 'public.async_jobs', 'DELETE') THEN
        RAISE EXCEPTION 'The runtime role still has effective private Jobs DELETE privileges';
    END IF;
END
$jobs_delete_acl$;
DO $jobs_update_acl$
DECLARE
    v_runtime name := current_setting('yoao.application_role');
    v_columns text;
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_class
        WHERE oid = 'public.async_jobs'::regclass
          AND pg_catalog.pg_has_role(v_runtime, relowner, 'MEMBER')
    ) THEN
        RAISE EXCEPTION 'The runtime role must not own or inherit Jobs update authority';
    END IF;
    EXECUTE format('REVOKE UPDATE ON TABLE public.async_jobs FROM %I, PUBLIC', v_runtime);
    SELECT string_agg(format('%I', attname), ', ' ORDER BY attnum)
    INTO v_columns FROM pg_catalog.pg_attribute
    WHERE attrelid = 'public.async_jobs'::regclass
      AND attnum > 0 AND NOT attisdropped;
    IF v_columns IS NOT NULL THEN
        EXECUTE format('REVOKE UPDATE (%s) ON TABLE public.async_jobs FROM %I, PUBLIC',
                       v_columns, v_runtime);
    END IF;
    IF pg_catalog.has_table_privilege(v_runtime, 'public.async_jobs', 'UPDATE')
       OR pg_catalog.has_any_column_privilege(v_runtime, 'public.async_jobs', 'UPDATE') THEN
        RAISE EXCEPTION 'The runtime role still has effective private Jobs UPDATE privileges';
    END IF;
END
$jobs_update_acl$;
REVOKE ALL ON FUNCTION yoao_jobs_v1._jobs_lifecycle_has_keys(jsonb,text[]),
    yoao_jobs_v1._jobs_lifecycle_is_truthy(jsonb),
    yoao_jobs_v1._jobs_lifecycle_integer(jsonb) FROM :"application_role", PUBLIC;
REVOKE ALL ON FUNCTION
    yoao_jobs_v1._async_job_list_rows(uuid,text,text[],text[],uuid,text)
    FROM :"application_role", PUBLIC;

-- Fixture authority never follows a test database suffix into the runtime role.
REVOKE ALL ON SCHEMA yoao_fixture_v1, yoao_identity_fixture_v1, yoao_workspace_fixture_v1
    FROM :"application_role";
REVOKE ALL ON ALL TABLES IN SCHEMA yoao_fixture_v1, yoao_identity_fixture_v1,
    yoao_workspace_fixture_v1 FROM :"application_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA yoao_fixture_v1, yoao_identity_fixture_v1,
    yoao_workspace_fixture_v1 FROM :"application_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_fixture_v1, yoao_identity_fixture_v1,
    yoao_workspace_fixture_v1 FROM :"application_role";
REVOKE ALL ON FUNCTION yoao_identity_v1.replace_fixture_preferences(uuid,jsonb),
    yoao_labeling_v1.screenshot_operations_command(jsonb),
    yoao_labeling_v1.purge_scale_seed_labeling_rows(text,uuid[]),
    yoao_labeling_v1.create_scale_seed_batches(text,uuid,uuid,integer,uuid),
    yoao_labeling_v1.create_scale_seed_tasks(text,uuid,uuid),
    yoao_labeling_v1.create_scale_seed_annotations(text,uuid,uuid,integer),
    yoao_workspace_v1.create_scale_seed_project(uuid,text),
    yoao_workspace_v1.ensure_scale_seed_member(uuid,uuid,uuid),
    yoao_workspace_v1.guard_scale_seed_cleanup(text),
    yoao_workspace_v1.delete_scale_seed_projects(text,uuid[]),
    yoao_assets_v1.guard_scale_seed_dataset_cleanup(uuid,boolean),
    yoao_assets_v1.guard_scale_fixture_originals(uuid,uuid[])
    FROM :"application_role", PUBLIC;
REVOKE ALL ON FUNCTION yoao_jobs_v1.delete_fixture_project_jobs(uuid[])
    FROM :"application_role", PUBLIC;
REVOKE ALL ON FUNCTION yoao_jobs_v1.delete_fixture_media_backfills(uuid,uuid)
    FROM :"application_role", PUBLIC;
REVOKE ALL ON FUNCTION yoao_labeling_v1.delete_fixture_project_rows(uuid[])
    FROM :"application_role", PUBLIC;
REVOKE ALL ON FUNCTION yoao_labeling_v1.e2e_fixture_command(jsonb)
    FROM :"application_role", PUBLIC;
REVOKE ALL ON FUNCTION yoao_labeling_v1.prepare_filtering_rendering(jsonb)
    FROM :"application_role", PUBLIC;
REVOKE ALL ON FUNCTION yoao_jobs_v1.set_filter_fixture_job_outcomes(uuid,uuid)
    FROM :"application_role", PUBLIC;

REVOKE ALL ON TABLE public.identity_account_presence FROM :"application_role", PUBLIC;
REVOKE ALL (user_id, status, last_seen_at)
    ON TABLE public.identity_account_presence FROM :"application_role", PUBLIC;
REVOKE ALL ON FUNCTION yoao_identity_v1._initialize_account_presence()
    FROM :"application_role", PUBLIC;
REVOKE ALL ON TABLE public.identity_api_key_usage FROM :"application_role", PUBLIC;
REVOKE ALL (api_key_id, last_used_at)
    ON TABLE public.identity_api_key_usage FROM :"application_role", PUBLIC;
REVOKE ALL ON FUNCTION yoao_identity_v1._initialize_api_key_usage()
    FROM :"application_role", PUBLIC;
GRANT SELECT ON yoao_identity_v1.account_profiles,
    yoao_identity_v1.invitation_audit_targets TO :"application_role";
REVOKE ALL ON TABLE public.notifications, public.notification_preferences,
    public.notification_delivery_outbox FROM :"application_role", PUBLIC;
REVOKE ALL (id, user_id, type, target_type, target_id, payload, read_at, created_at)
    ON TABLE public.notifications FROM :"application_role", PUBLIC;
REVOKE ALL (user_id, type, channels, updated_at)
    ON TABLE public.notification_preferences FROM :"application_role", PUBLIC;
REVOKE ALL (notification_id, operation_id, recipient_id, notification_type,
    state, attempt, available_at, worker_id, lease_id, lease_expires_at,
    last_reason, created_at, delivered_at)
    ON TABLE public.notification_delivery_outbox FROM :"application_role", PUBLIC;
REVOKE ALL ON TABLE yoao_notifications_v1.notification_facts
    FROM :"application_role", PUBLIC;
REVOKE ALL (id, user_id, type, target_type, target_id, payload, read_at, created_at)
    ON TABLE yoao_notifications_v1.notification_facts FROM :"application_role", PUBLIC;
REVOKE EXECUTE ON FUNCTION yoao_notifications_v1._visible_inbox_rows(uuid,text),
    yoao_notifications_v1._visible_inbox_rows_scoped(uuid,text),
    yoao_notifications_v1._notification_is_migration_inventory(uuid),
    yoao_notifications_v1._notification_unknown_job_scope(uuid)
    FROM :"application_role", PUBLIC;
REVOKE ALL ON TABLE yoao_audit_v1.audit_events FROM :"application_role", PUBLIC;
REVOKE ALL (id, actor_id, actor_email, actor_role, action, target_type, target_id,
    method, path, status_code, ip, detail_json, request_id, created_at)
    ON TABLE yoao_audit_v1.audit_events FROM :"application_role", PUBLIC;
GRANT SELECT ON yoao_audit_v1.audit_events TO :"application_role";
REVOKE ALL ON TABLE public.mv_audit_bi_daily FROM :"application_role", PUBLIC;
REVOKE ALL (day, action, target_type, actor_role, status_family, event_count)
    ON TABLE public.mv_audit_bi_daily FROM :"application_role", PUBLIC;
REVOKE ALL ON TABLE yoao_audit_v1.audit_daily_facts FROM :"application_role", PUBLIC;
REVOKE ALL (day, action, target_type, actor_role, status_family, event_count)
    ON TABLE yoao_audit_v1.audit_daily_facts FROM :"application_role", PUBLIC;
GRANT SELECT ON yoao_audit_v1.audit_daily_facts TO :"application_role";
REVOKE ALL ON TABLE yoao_jobs_v1.notification_scope_facts FROM :"application_role", PUBLIC;
REVOKE ALL (id, kind, scope_project_id)
    ON TABLE yoao_jobs_v1.notification_scope_facts FROM :"application_role", PUBLIC;
GRANT SELECT ON yoao_jobs_v1.notification_scope_facts TO :"application_role";
REVOKE EXECUTE ON FUNCTION yoao_audit_v1.refresh_audit_bi_mv()
    FROM :"application_role", PUBLIC;
DO $audit_storage_acl$
DECLARE
    v_runtime pg_catalog.name := current_setting('yoao.application_role');
    v_partition record;
    v_columns text;
    v_sequence text := pg_catalog.pg_get_serial_sequence('public.audit_logs', 'id');
BEGIN
    EXECUTE pg_catalog.format('REVOKE ALL ON TABLE public.audit_logs FROM %I, PUBLIC',
        v_runtime);
    SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attribute.attname), ',' ORDER BY attribute.attnum)
    INTO v_columns
    FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = 'public.audit_logs'::pg_catalog.regclass
      AND attribute.attnum > 0 AND NOT attribute.attisdropped;
    EXECUTE pg_catalog.format(
        'REVOKE ALL (%s) ON TABLE public.audit_logs FROM %I, PUBLIC',
        v_columns, v_runtime);
    EXECUTE pg_catalog.format('GRANT SELECT ON TABLE public.audit_logs TO %I',
        v_runtime);
    FOR v_partition IN
        SELECT relid FROM pg_catalog.pg_partition_tree('public.audit_logs')
        WHERE relid <> 'public.audit_logs'::pg_catalog.regclass
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON TABLE %s FROM %I, PUBLIC',
            v_partition.relid::pg_catalog.regclass, v_runtime);
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attribute.attname), ',' ORDER BY attribute.attnum)
        INTO v_columns
        FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = v_partition.relid
          AND attribute.attnum > 0 AND NOT attribute.attisdropped;
        EXECUTE pg_catalog.format('REVOKE ALL (%s) ON TABLE %s FROM %I, PUBLIC',
            v_columns, v_partition.relid::pg_catalog.regclass, v_runtime);
    END LOOP;
    IF v_sequence IS NULL THEN
        RAISE EXCEPTION 'Audit log sequence is missing';
    END IF;
    EXECUTE pg_catalog.format('REVOKE ALL ON SEQUENCE %s FROM %I, PUBLIC',
        v_sequence, v_runtime);
END;
$audit_storage_acl$;
DO $notification_private_acl$
DECLARE
    v_runtime pg_catalog.name := current_setting('yoao.application_role');
    v_relation pg_catalog.regclass;
BEGIN
    FOREACH v_relation IN ARRAY ARRAY[
        'public.notifications'::pg_catalog.regclass,
        'public.notification_preferences'::pg_catalog.regclass,
        'public.notification_delivery_outbox'::pg_catalog.regclass
    ] LOOP
        IF pg_catalog.has_table_privilege(v_runtime, v_relation,
                'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR pg_catalog.has_any_column_privilege(v_runtime, v_relation,
                'SELECT,INSERT,UPDATE,REFERENCES') THEN
            RAISE EXCEPTION 'The runtime role still has direct Notifications storage access';
        END IF;
    END LOOP;
    IF pg_catalog.has_table_privilege(v_runtime,
            'yoao_notifications_v1.notification_facts', 'SELECT')
       OR pg_catalog.has_any_column_privilege(v_runtime,
            'yoao_notifications_v1.notification_facts', 'SELECT')
       OR pg_catalog.has_table_privilege(v_runtime,
            'yoao_notifications_v1.notification_facts',
            'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(v_runtime,
            'yoao_notifications_v1.notification_facts', 'INSERT,UPDATE,REFERENCES') THEN
        RAISE EXCEPTION 'The runtime role can read Notifications facts directly';
    END IF;
    IF NOT pg_catalog.has_function_privilege(v_runtime,
            'yoao_notifications_v1.read_notification(uuid)', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_runtime,
            'yoao_notifications_v1.find_terminal_notification(uuid,text,uuid)', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_runtime,
            'yoao_notifications_v1.delivery_job_scopes(uuid[])', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_runtime,
            'yoao_notifications_v1.delivery_member_pairs(jsonb)', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_runtime,
            'yoao_notifications_v1.delivery_reviewer_pairs(jsonb)', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_runtime,
            'yoao_notifications_v1.delivery_owner_pairs(jsonb)', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_runtime,
            'yoao_notifications_v1.delivery_super_pairs(jsonb)', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_runtime,
            'yoao_notifications_v1.inbox_page(uuid,text,boolean,integer,integer)', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_runtime,
            'yoao_notifications_v1.inbox_total_count(uuid,text)', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_runtime,
            'yoao_notifications_v1.inbox_unread_count(uuid,text)', 'EXECUTE')
       OR pg_catalog.has_function_privilege(v_runtime,
            'yoao_notifications_v1._visible_inbox_rows(uuid,text)', 'EXECUTE') THEN
        RAISE EXCEPTION 'The runtime role is missing fixed Notifications reads';
    END IF;
END;
$notification_private_acl$;
DO $audit_read_acl$
DECLARE
    v_runtime pg_catalog.name := current_setting('yoao.application_role');
    v_sequence text := pg_catalog.pg_get_serial_sequence('public.audit_logs', 'id');
BEGIN
    IF NOT pg_catalog.has_table_privilege(v_runtime, 'public.audit_logs', 'SELECT')
       OR pg_catalog.has_table_privilege(v_runtime, 'public.audit_logs',
            'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(v_runtime, 'public.audit_logs',
            'INSERT,UPDATE,REFERENCES')
       OR pg_catalog.has_sequence_privilege(v_runtime, v_sequence, 'SELECT,UPDATE,USAGE')
       OR pg_catalog.pg_has_role(v_runtime,
            (SELECT relation.relowner FROM pg_catalog.pg_class AS relation
             WHERE relation.oid='public.audit_logs'::pg_catalog.regclass), 'MEMBER')
       OR NOT pg_catalog.has_function_privilege(v_runtime,
            'yoao_audit_v1.maintenance_command(jsonb)', 'EXECUTE')
       OR pg_catalog.has_function_privilege(v_runtime,
            'yoao_audit_v1.refresh_audit_bi_mv()', 'EXECUTE')
       OR EXISTS (
            SELECT FROM pg_catalog.pg_partition_tree('public.audit_logs') AS partition
            WHERE partition.relid <> 'public.audit_logs'::pg_catalog.regclass
              AND (
                  pg_catalog.has_table_privilege(v_runtime, partition.relid,
                      'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                  OR pg_catalog.has_any_column_privilege(v_runtime, partition.relid,
                      'SELECT,INSERT,UPDATE,REFERENCES')
              )
       )
       OR NOT pg_catalog.has_table_privilege(v_runtime, 'yoao_audit_v1.audit_events', 'SELECT')
       OR pg_catalog.has_table_privilege(v_runtime, 'yoao_audit_v1.audit_events',
            'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(v_runtime, 'yoao_audit_v1.audit_events',
            'INSERT,UPDATE,REFERENCES')
       OR NOT pg_catalog.has_table_privilege(v_runtime, 'yoao_audit_v1.audit_daily_facts', 'SELECT')
       OR pg_catalog.has_table_privilege(v_runtime, 'yoao_audit_v1.audit_daily_facts',
            'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(v_runtime, 'yoao_audit_v1.audit_daily_facts',
            'INSERT,UPDATE,REFERENCES')
       OR pg_catalog.has_table_privilege(v_runtime, 'public.mv_audit_bi_daily',
            'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(v_runtime, 'public.mv_audit_bi_daily',
            'SELECT,INSERT,UPDATE,REFERENCES')
       OR pg_catalog.has_function_privilege(v_runtime, 'yoao_audit_v1.refresh_audit_bi_mv()', 'EXECUTE') THEN
        RAISE EXCEPTION 'The runtime role is missing read-only Audit facts';
    END IF;
END;
$audit_read_acl$;
DO $jobs_scope_acl$
DECLARE
    v_runtime pg_catalog.name := current_setting('yoao.application_role');
BEGIN
    IF NOT pg_catalog.has_table_privilege(v_runtime, 'yoao_jobs_v1.notification_scope_facts', 'SELECT')
       OR pg_catalog.has_table_privilege(v_runtime, 'yoao_jobs_v1.notification_scope_facts',
            'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(v_runtime, 'yoao_jobs_v1.notification_scope_facts',
            'INSERT,UPDATE,REFERENCES') THEN
        RAISE EXCEPTION 'The runtime role is missing read-only Jobs notification facts';
    END IF;
END;
$jobs_scope_acl$;
GRANT SELECT ON yoao_workspace_v1.project_member_facts TO :"application_role";
GRANT SELECT ON yoao_workspace_v1.project_facts,
    yoao_labeling_v1.project_catalog_counters TO :"application_role";
GRANT EXECUTE ON FUNCTION
    yoao_identity_v1.authentication_policy(),
    yoao_identity_v1.read_provisioning_account(text),
    yoao_identity_v1.create_provisioning_account(jsonb),
    yoao_identity_v1.create_seed_persona_account(jsonb),
    yoao_identity_v1.read_scale_seed_super_admin(),
    yoao_identity_v1.read_scale_seed_annotator(uuid[]),
    yoao_identity_v1.read_actor_context(uuid,text),
    yoao_identity_v1.read_actor_facts(uuid),
    yoao_identity_v1.lock_accounts_share(uuid[],boolean),
    yoao_identity_v1.check_accepted_api_key(uuid,uuid,text[]),
    yoao_identity_v1.read_accepted_api_key_facts(uuid,uuid),
    yoao_identity_v1.lock_accounts_update(uuid[],boolean),
    yoao_identity_v1.lock_accounts_key_share(uuid[],boolean),
    yoao_identity_v1.lock_super_admin_invariant(),
    yoao_identity_v1.super_admin_status(uuid),
    yoao_identity_v1.read_jwt_principal(uuid,uuid,bigint,timestamptz,boolean),
    yoao_identity_v1.read_api_key_candidates(text,uuid,integer),
    yoao_identity_v1.confirm_api_key_principal(uuid,text),
    yoao_identity_v1.get_credential_generation(uuid),
    yoao_identity_v1.advance_credential_generation(uuid),
    yoao_identity_v1.revoke_jti(uuid,timestamptz),
    yoao_identity_v1.is_jti_revoked(uuid),
    yoao_identity_v1.retire_account_credentials(uuid),
    yoao_identity_v1.lock_current_principal(uuid,text,uuid,bigint,timestamptz,text[]),
    yoao_identity_keys_v1.key_policy(),
    yoao_identity_keys_v1.execute_key_command(jsonb),
    yoao_identity_sessions_v1.session_policy(),
    yoao_identity_sessions_v1.read_login_candidate(text),
    yoao_identity_sessions_v1.execute_session_command(jsonb),
    yoao_identity_accounts_v1.account_policy(),
    yoao_identity_accounts_v1.prepare_account_command(jsonb),
    yoao_identity_accounts_v1.apply_account_command(jsonb),
    yoao_identity_accounts_v1.mark_inactive_accounts_offline(timestamptz),
    yoao_identity_recovery_v1.recovery_policy(),
    yoao_identity_recovery_v1.prepare_recovery_command(jsonb),
    yoao_identity_recovery_v1.apply_recovery_command(jsonb),
    yoao_identity_recovery_v1.read_registration_status(jsonb),
    yoao_identity_recovery_v1.mail_policy(),
    yoao_identity_recovery_v1.claim_mail(jsonb),
    yoao_identity_recovery_v1.renew_mail(jsonb),
    yoao_identity_recovery_v1.finish_mail(jsonb),
    yoao_identity_groups_v1.group_policy(),
    yoao_identity_groups_v1.execute_group_command(jsonb),
    yoao_identity_deactivation_v1.deactivation_policy(),
    yoao_identity_deactivation_v1.execute_deactivation_command(jsonb),
    yoao_identity_deactivation_v1.due_policy(),
    yoao_identity_deactivation_v1.process_due_deactivations(jsonb),
    yoao_identity_invitations_v1.invitation_policy(),
    yoao_identity_invitations_v1.prepare_invitation_command(jsonb),
    yoao_identity_invitations_v1.apply_invitation_command(jsonb),
    yoao_identity_directory_v1.directory_policy(),
    yoao_identity_directory_v1.execute_directory_command(jsonb),
    yoao_identity_admin_passwords_v1.admin_password_policy(),
    yoao_identity_admin_passwords_v1.execute_admin_password_command(jsonb),
    yoao_identity_platform_roles_v1.platform_role_policy(),
    yoao_identity_platform_roles_v1.execute_platform_role_command(jsonb),
    yoao_identity_suspensions_v1.suspension_policy(),
    yoao_identity_suspensions_v1.execute_suspension_command(jsonb),
    yoao_identity_deletions_v1.deletion_policy(),
    yoao_identity_deletions_v1.execute_deletion_command(jsonb),
    yoao_identity_offboarding_v1.offboarding_policy(),
    yoao_identity_offboarding_v1.prepare_offboarding_command(jsonb),
    yoao_identity_offboarding_v1.apply_offboarding_command(jsonb),
    yoao_identity_briefs_v1.briefs_policy(),
    yoao_identity_briefs_v1.execute_briefs_command(jsonb),
    yoao_identity_mail_v2.mail_policy(),
    yoao_identity_mail_v2.claim_mail(jsonb),
    yoao_identity_mail_v2.renew_mail(jsonb),
    yoao_identity_mail_v2.finish_mail(jsonb),
    yoao_notifications_v1.identity_delivery_policy(),
    yoao_notifications_v1.claim_identity_deliveries(jsonb),
    yoao_notifications_v1.finish_identity_delivery(jsonb),
    yoao_settings_v1.mail_delivery_policy(jsonb)
    TO :"application_role";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_membership_v1.membership_policy(),
    yoao_workspace_membership_v1.prepare_membership_command(jsonb),
    yoao_workspace_membership_v1.apply_membership_command(jsonb)
    TO :"application_role";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_owner_transfer_v1.owner_transfer_policy(),
    yoao_workspace_owner_transfer_v1.transfer_project_owner(jsonb)
    TO :"application_role";
GRANT USAGE ON SCHEMA yoao_workspace_configuration_v1,
    yoao_workspace_template_query_v1, yoao_workspace_directory_query_v1
    TO :"application_role";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_configuration_v1.configuration_policy(),
    yoao_workspace_configuration_v1.configuration_step(jsonb,text,jsonb,text[],text[]),
    yoao_workspace_configuration_v1.guide_assets_step(jsonb,text,jsonb),
    yoao_workspace_template_query_v1.template_query_policy(),
    yoao_workspace_template_query_v1.template_query_step(jsonb,text,jsonb,text,text[],text),
    yoao_workspace_directory_query_v1.directory_query_policy(),
    yoao_workspace_directory_query_v1.directory_query_step(jsonb,text,jsonb)
    TO :"application_role";
GRANT USAGE ON SCHEMA yoao_workspace_project_query_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_project_query_v1.project_query_policy(),
    yoao_workspace_project_query_v1.project_query_step(
        jsonb, text, jsonb, text, text, text[], text[], uuid, text,
        timestamptz, timestamptz, bigint, integer
    )
    TO :"application_role";
-- These cross-owner dependencies are called only by Workspace SECURITY DEFINER code.
REVOKE ALL ON FUNCTION
    yoao_identity_v1.workspace_configuration_org_scope(uuid, uuid),
    yoao_assets_v1.workspace_configuration_datasets(uuid),
    yoao_labeling_v1.workspace_configuration_video_facts(uuid),
    yoao_inference_v1.enable_workspace_project_backend(uuid, uuid)
    FROM :"application_role";
GRANT EXECUTE ON FUNCTION yoao_workspace_v1.lock_project_reference(uuid),
    yoao_workspace_v1.lock_project_reference_nowait(uuid),
    yoao_workspace_v1.read_project_row(uuid),
    yoao_workspace_v1.actor_project_memberships(uuid),
    yoao_workspace_project_query_v1.project_search_scope(uuid, text),
    yoao_workspace_project_query_v1.project_search_member_ids(uuid, text),
    yoao_workspace_v1.lock_project_references_for_update(uuid[]),
    yoao_workspace_v1.lock_project_member_reference(uuid, uuid, boolean),
    yoao_workspace_v1.project_member_rows(uuid, uuid[], boolean),
    yoao_workspace_v1.lock_project_manager(uuid, uuid),
    yoao_workspace_v1.project_access_step(uuid, uuid, text),
    yoao_workspace_v1.lock_project_row(uuid, boolean),
    yoao_workspace_v1.delete_project(uuid, uuid, uuid, jsonb),
    yoao_workspace_v1.preview_dataset_unlink(uuid, uuid, uuid),
    yoao_workspace_v1.unlink_dataset(uuid, uuid, uuid, uuid, text, jsonb),
    yoao_assets_v1.read_dataset_source_authority(uuid, boolean),
    yoao_assets_v1.lock_dataset_reference(uuid),
    yoao_assets_v1.lock_current_association(uuid, uuid),
    yoao_assets_v1.associate_project(uuid, uuid, uuid),
    yoao_assets_v1.lock_scene_source_references(uuid[]),
    yoao_assets_v1.delete_dataset(uuid, uuid, uuid, text, jsonb),
    yoao_assets_v1.delete_dataset_item(uuid, uuid, uuid, uuid, text, jsonb),
    yoao_assets_v1.reserve_dataset_upload(uuid, uuid, uuid, text, text),
    yoao_assets_v1.lock_dataset_upload(uuid, uuid, uuid),
    yoao_assets_v1.find_dataset_upload_duplicate(uuid, uuid, uuid, text),
    yoao_assets_v1.complete_dataset_upload(uuid, uuid, uuid, uuid, bigint, text, integer, integer),
    yoao_assets_v1.discard_dataset_upload(uuid, uuid, uuid, text),
    yoao_labeling_v1.append_dataset_items(uuid, uuid[], uuid),
    yoao_labeling_v1.write_project_counter_projection(uuid, jsonb),
    yoao_labeling_v1.decrement_project_review_count(uuid),
    yoao_labeling_v1.decrement_project_completed_count(uuid),
    yoao_labeling_v1.record_project_review_approval(uuid),
    yoao_labeling_v1.read_mask_write_epochs(jsonb),
    yoao_labeling_v1.begin_mask_content_write(jsonb),
    yoao_labeling_v1.lock_mask_write_epochs(jsonb),
    yoao_labeling_v1.register_tracker_running_mask_root(jsonb),
    yoao_labeling_v1.capture_video_tracker_input(uuid,uuid,integer),
    yoao_labeling_v1.lock_video_tracker_input_for_stage(uuid,uuid,uuid,uuid,text,text),
    yoao_labeling_v1.lock_video_tracker_input_for_accept(uuid,uuid,uuid,uuid,text,text),
    yoao_labeling_v1.attach_annotation_video_tracker_input_source(uuid,uuid,uuid,jsonb),
    yoao_labeling_v1.copy_annotation_video_tracker_input_sources(uuid,uuid,uuid),
    yoao_labeling_v1.claim_mask_gc_sweep(jsonb),
    yoao_labeling_v1.stage_mask_gc_page(jsonb),
    yoao_labeling_v1.claim_mask_gc_deletes(jsonb),
    yoao_labeling_v1.finish_mask_gc_delete(jsonb),
    yoao_labeling_v1.read_mask_gc_progress(jsonb),
    yoao_labeling_v1.inspect_mask_gc_page(jsonb),
    yoao_labeling_v1.begin_batch_result(jsonb),
    yoao_labeling_v1.put_batch_result_part(jsonb),
    yoao_labeling_v1.seal_batch_result(jsonb),
    yoao_labeling_v1.read_batch_result(jsonb),
    yoao_labeling_v1.lock_batch_result_write(jsonb),
    yoao_labeling_v1.finish_batch_result_write(jsonb),
    yoao_labeling_v1.finish_batch_result_failure(jsonb),
    yoao_jobs_v1.claim_batch_result_execution(jsonb),
    yoao_jobs_v1.heartbeat_batch_result_execution(jsonb),
    yoao_jobs_v1.release_batch_result_execution(jsonb),
    yoao_jobs_v1.stage_batch_result_members(jsonb),
    yoao_jobs_v1.start_batch_result_execution(jsonb),
    yoao_jobs_v1.read_batch_result_execution(jsonb),
    yoao_jobs_v1.mark_batch_result_overwrite(jsonb),
    yoao_jobs_v1.skip_batch_result_member(jsonb),
    yoao_jobs_v1.finish_batch_result_execution(jsonb),
    yoao_jobs_v1.start_batch_result_member(jsonb),
    yoao_jobs_v1.begin_prediction_mask_producer(jsonb,bigint,uuid),
    yoao_jobs_v1.bind_prediction_mask_object(uuid,uuid),
    yoao_jobs_v1.finish_prediction_mask_producer(uuid),
    yoao_jobs_v1.prediction_mask_object_retained(uuid),
    yoao_jobs_v1.fixture_media_backfill_route(uuid),
    yoao_jobs_v1.fixture_media_work_exists(text,text),
    public.prepare_image_export_scope(uuid,uuid,uuid,uuid,uuid[]),
    public.image_export_scope_matches(uuid,uuid),
    public.fixture_native_export_lifetime(uuid,uuid),
    public.fixture_native_export_cleanup_eligible(uuid),
    yoao_labeling_v1.admit_prediction_retry(jsonb),
    yoao_labeling_v1.read_prediction_retry(jsonb),
    yoao_labeling_v1.finish_prediction_retry_write(jsonb),
    yoao_labeling_v1.finish_prediction_retry_failure(jsonb),
    yoao_jobs_v1.claim_prediction_retry_deliveries(jsonb),
    yoao_jobs_v1.finish_prediction_retry_delivery(jsonb),
    yoao_labeling_v1.accept_dataset_task_creation(uuid, uuid, uuid, uuid, uuid, uuid, text, integer),
    yoao_labeling_v1.prepare_dataset_task_creation(uuid, jsonb),
    yoao_labeling_v1.read_dataset_task_creation(uuid),
    yoao_labeling_v1.advance_dataset_task_creation(uuid, integer),
    yoao_jobs_v1.create_task_creation_job(uuid, uuid, uuid, uuid, uuid),
    yoao_jobs_v1.find_task_creation_job(uuid, uuid, uuid, uuid),
    yoao_jobs_v1.claim_task_creation_job(uuid, text),
    yoao_jobs_v1.finish_task_creation_job(uuid, uuid),
    yoao_jobs_v1.fail_task_creation_job(uuid, uuid, text),
    yoao_jobs_v1.cancel_task_creation_job(uuid, uuid),
    yoao_assets_v1.pending_storage_deletions(uuid, integer),
    yoao_assets_v1.complete_storage_deletion(uuid, uuid)
    TO :"application_role";
DO $image_export_scope_acl$
DECLARE
    v_runtime name := current_setting('yoao.application_role');
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_class
        WHERE oid = 'public.image_export_scopes'::regclass
          AND pg_catalog.pg_has_role(v_runtime, relowner, 'MEMBER')
    ) OR pg_catalog.has_table_privilege(v_runtime, 'public.image_export_scopes',
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(v_runtime, 'public.image_export_scopes',
           'SELECT,INSERT,UPDATE,REFERENCES') THEN
        RAISE EXCEPTION 'The runtime role retains private image export scope access';
    END IF;
    IF NOT pg_catalog.has_function_privilege(v_runtime,
           'public.prepare_image_export_scope(uuid,uuid,uuid,uuid,uuid[])', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_runtime,
           'public.image_export_scope_matches(uuid,uuid)', 'EXECUTE') THEN
        RAISE EXCEPTION 'The runtime role is missing image export scope capabilities';
    END IF;
END;
$image_export_scope_acl$;
-- Compose the existing Spatial native contract for default API/worker calls.
GRANT SELECT ON yoao_assets_v1.spatial_item_references,
    yoao_assets_v1.spatial_scene_references,
    yoao_assets_v1.labeling_dataset_item_facts TO :"application_role";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.guard_spatial_actor(uuid,text),
    yoao_assets_v1.list_spatial_scenes(uuid,uuid,timestamp with time zone,uuid,integer),
    yoao_assets_v1.get_spatial_scene(uuid,uuid),
    yoao_assets_v1.read_spatial_layout(uuid,uuid,uuid,uuid,integer),
    yoao_assets_v1.read_spatial_items(uuid,uuid[],uuid,integer),
    yoao_assets_v1.read_spatial_poses(uuid,uuid,integer,integer),
    yoao_assets_v1.read_spatial_pose_pairs(jsonb),
    yoao_assets_v1.read_spatial_calibrations(uuid[],boolean,uuid,integer,integer),
    yoao_assets_v1.lock_spatial_calibration_facts(uuid,uuid,text,uuid,jsonb),
    yoao_assets_v1.mutate_spatial_scene(uuid,uuid,uuid,jsonb,jsonb),
    yoao_assets_v1.apply_spatial_layout(uuid,uuid,uuid,text,boolean,jsonb,jsonb),
    yoao_assets_v1.read_spatial_layout_receipt(uuid,uuid,uuid,text,boolean),
    yoao_assets_v1.upsert_spatial_poses(uuid,uuid,jsonb),
    yoao_assets_v1.create_spatial_scene(uuid,uuid,uuid,text,text,jsonb,uuid),
    yoao_assets_v1.assign_spatial_items(uuid,uuid,jsonb),
    yoao_assets_v1.set_spatial_scene_source(uuid,uuid,text,jsonb),
    yoao_assets_v1.update_spatial_calibration(uuid,uuid,uuid,text,integer,text,jsonb,text,text,jsonb,jsonb),
    yoao_assets_v1.read_pointcloud_preparation(uuid,uuid,integer),
    yoao_assets_v1.apply_pointcloud_preparation(uuid,jsonb,jsonb,jsonb),
    yoao_assets_v1.read_spatial_camera_target(uuid,uuid,text),
    yoao_assets_v1.record_spatial_calibration_provenance(uuid,uuid,jsonb),
    yoao_assets_v1.set_spatial_dataset_axis(uuid,uuid,text)
    TO :"application_role";

-- The monolithic API/Celery runtime composes retained video input capabilities
-- locally. Keep this bounded to workflow, control, worker and labeling calls;
-- system sweep and video-input garbage operations stay with their owners.
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.capture_accepted_video_manifest(uuid, uuid, uuid, jsonb),
    yoao_assets_v1.read_accepted_video_manifest(uuid, uuid, uuid),
    yoao_assets_v1.read_accepted_video_manifest_page(uuid, uuid, uuid, text, text, bigint, integer),
    yoao_assets_v1.prepare_accepted_video_member(uuid, uuid, uuid, text, bigint, uuid, text, bigint, jsonb, text),
    yoao_assets_v1.seal_accepted_video_manifest(uuid, uuid, uuid, text),
    yoao_assets_v1.read_accepted_video_input(uuid, uuid, uuid, uuid),
    yoao_assets_v1.read_accepted_video_input_work(uuid, uuid, uuid, uuid),
    yoao_assets_v1.read_accepted_video_times(uuid, uuid, uuid, uuid, text, bigint, integer),
    yoao_assets_v1.require_accepted_video_frame(uuid, uuid, uuid, uuid, text, bigint),
    yoao_assets_v1.read_accepted_video_frame(uuid, uuid, uuid, uuid, text, bigint),
    yoao_assets_v1.claim_accepted_video_input(uuid, uuid, integer),
    yoao_assets_v1.heartbeat_accepted_video_input(uuid, bigint, uuid, integer),
    yoao_assets_v1.stage_accepted_video_times(uuid, bigint, uuid, bigint, jsonb),
    yoao_assets_v1.finish_accepted_video_input(uuid, bigint, uuid, jsonb),
    yoao_assets_v1.fail_accepted_video_input(uuid, bigint, uuid, text),
    yoao_assets_v1.claim_accepted_video_frame(uuid, uuid, integer),
    yoao_assets_v1.heartbeat_accepted_video_frame(uuid, bigint, uuid, integer),
    yoao_assets_v1.finish_accepted_video_frame(uuid, bigint, uuid, jsonb),
    yoao_assets_v1.fail_accepted_video_frame(uuid, bigint, uuid, text),
    yoao_assets_v1.lock_accepted_video_frame(uuid, uuid, uuid, uuid, text, bigint, jsonb, jsonb),
    yoao_assets_v1.lock_accepted_video_input(uuid, uuid, uuid, uuid, text),
    yoao_assets_v1._lock_accepted_video_input_pin(uuid, uuid, jsonb, uuid),
    yoao_labeling_v1.attach_annotation_video_input_source(uuid, uuid, jsonb),
    yoao_labeling_v1.copy_annotation_video_input_sources(uuid, uuid),
    yoao_labeling_v1.accepted_video_input_referenced(uuid)
    TO :"application_role";

-- The monolithic API/Celery runtime still composes the image media owner
-- locally. Preserve its published image runtime surface alongside video.
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.read_video_derivative_links(uuid[]),
    yoao_assets_v1.publish_image_pyramid_thumbnail(uuid,integer,uuid),
    yoao_assets_v1.read_image_source(uuid,uuid,uuid),
    yoao_assets_v1.begin_image_original_actor(uuid,uuid,uuid,text,text),
    yoao_assets_v1.finish_image_original_read(uuid,uuid),
    yoao_assets_v1.begin_image_original_system(text,uuid,text,text),
    yoao_assets_v1.finish_image_original_system(uuid,uuid),
    yoao_assets_v1.begin_image_original_worker(uuid,bigint,uuid,text,text),
    yoao_assets_v1.finish_image_original_worker(uuid,uuid),
    yoao_assets_v1.begin_image_original_backfill(uuid,bigint,uuid,uuid,text,uuid,text,text),
    yoao_assets_v1.observe_image_source(uuid,uuid,uuid,text,jsonb),
    yoao_assets_v1.read_image_summaries(uuid,jsonb,text),
    yoao_assets_v1.prepare_image_media(uuid,uuid,uuid,uuid,text,text,jsonb,boolean),
    yoao_assets_v1.read_image_generation(uuid,uuid,uuid,integer,text),
    yoao_assets_v1.begin_native_pyramid_read(uuid,uuid,uuid,integer,text,uuid,uuid),
    yoao_assets_v1.complete_native_pyramid_read(uuid,uuid),
    yoao_assets_v1.read_image_work(uuid,uuid),
    yoao_assets_v1.retry_image_media(uuid,uuid,uuid,uuid,text,text,jsonb,boolean),
    yoao_assets_v1.invalidate_image_media(uuid,uuid,uuid,jsonb),
    yoao_assets_v1.cancel_image_media(uuid,uuid,uuid),
    yoao_assets_v1.resolve_internal_image_source(text,uuid,text,jsonb),
    yoao_assets_v1.schedule_image_media(uuid,text,uuid,jsonb,boolean),
    yoao_assets_v1.read_internal_image_work(uuid),
    yoao_assets_v1.scan_image_backfill(jsonb,text,uuid,integer),
    yoao_assets_v1.read_image_usage(),
    yoao_assets_v1.claim_image_media(uuid,uuid,integer,text[]),
    yoao_assets_v1.heartbeat_image_media(uuid,bigint,uuid,integer),
    yoao_assets_v1.finish_image_media(uuid,bigint,uuid,jsonb),
    yoao_assets_v1.fail_image_media(uuid,bigint,uuid,text,text),
    yoao_assets_v1.advance_image_backfill(uuid,bigint,uuid,integer,jsonb),
    yoao_assets_v1.reconcile_image_media(jsonb,integer),
    yoao_assets_v1.claim_image_garbage(uuid,integer,integer),
    yoao_assets_v1.complete_image_garbage(uuid,uuid)
    TO :"application_role";

-- Retained accepted-image inputs and manifests are used by the legacy
-- API/Celery adapter. Keep their public workflow/control/worker/Labeling calls
-- here; input sweep, input garbage and private receipt/pin helpers stay owner-only.
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.find_accepted_image_input(uuid,uuid,uuid),
    yoao_assets_v1.reserve_accepted_image_input(uuid,uuid,uuid,text,jsonb),
    yoao_assets_v1.read_accepted_image_input(uuid,uuid,uuid),
    yoao_assets_v1.read_accepted_image_input_work(uuid,uuid,uuid),
    yoao_assets_v1.claim_accepted_image_input(uuid,uuid,integer),
    yoao_assets_v1.heartbeat_accepted_image_input(uuid,bigint,uuid,integer),
    yoao_assets_v1.begin_accepted_image_original(uuid,bigint,uuid),
    yoao_assets_v1.finish_accepted_image_original(uuid,uuid),
    yoao_assets_v1.finish_accepted_image_input(uuid,bigint,uuid,jsonb),
    yoao_assets_v1.fail_accepted_image_input(uuid,bigint,uuid,text),
    yoao_assets_v1.lock_accepted_image_input(uuid,uuid,uuid),
    yoao_assets_v1.capture_accepted_image_input_manifest(uuid,uuid,uuid,jsonb),
    yoao_assets_v1.read_accepted_image_input_manifest(uuid,uuid,uuid),
    yoao_assets_v1.read_accepted_image_input_manifest_page(uuid,uuid,uuid,text,text,bigint,integer),
    yoao_assets_v1.prepare_accepted_image_input_manifest_member(uuid,uuid,uuid,text,bigint,uuid,text,jsonb,text),
    yoao_assets_v1.seal_accepted_image_input_manifest(uuid,uuid,uuid,text),
    yoao_labeling_v1.attach_annotation_input_source(uuid,uuid,text),
    yoao_labeling_v1.copy_annotation_input_sources(uuid,uuid)
    TO :"application_role";

-- Mask edit capabilities are restricted to explicitly granted consumers.
REVOKE ALL ON FUNCTION yoao_labeling_v1.mask_edit_command(jsonb),
    yoao_labeling_v1.mask_revision_command(jsonb),
    yoao_labeling_v1.ai_mask_accept_command(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION yoao_labeling_v1._mask_revision_canonical_json(jsonb)
    FROM :"application_role", PUBLIC;
REVOKE ALL ON SCHEMA yoao_assets_v1 FROM :"asset_cleanup_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_assets_v1 FROM :"asset_cleanup_role";
REVOKE ALL ON TABLE public.asset_storage_deletions FROM :"asset_cleanup_role";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"asset_cleanup_role";
REVOKE ALL ON FUNCTION yoao_assets_v1.pending_storage_deletions(uuid, integer)
    FROM :"asset_cleanup_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.pending_storage_deletions_v2(
    text, timestamptz, uuid, uuid, integer
),
    yoao_assets_v1.complete_storage_deletion(uuid, uuid)
    TO :"asset_cleanup_role";
REVOKE ALL ON SCHEMA public, yoao_identity_v1, yoao_identity_directory_v1,
    yoao_workspace_v1,
    yoao_labeling_v1, yoao_jobs_v1, yoao_learning_v1, yoao_inference_v1,
    yoao_insights_v1, yoao_diagnostics_v1, yoao_export_v1, yoao_audit_v1
    FROM :"asset_cleanup_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_identity_v1, yoao_identity_directory_v1,
    yoao_workspace_v1,
    yoao_labeling_v1, yoao_jobs_v1, yoao_learning_v1, yoao_inference_v1,
    yoao_insights_v1, yoao_diagnostics_v1, yoao_export_v1, yoao_audit_v1
    FROM :"asset_cleanup_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"asset_cleanup_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"asset_cleanup_role";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"asset_cleanup_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.pending_storage_deletions_v2(
    text, timestamptz, uuid, uuid, integer
),
    yoao_assets_v1.complete_storage_deletion(uuid, uuid),
    yoao_assets_v1.claim_image_garbage(uuid,integer,integer),
    yoao_assets_v1.complete_image_garbage(uuid,uuid),
    yoao_assets_v1.claim_accepted_input_garbage(uuid,integer,integer),
    yoao_assets_v1.complete_accepted_input_garbage(uuid,uuid),
    yoao_assets_v1.claim_accepted_video_input_garbage(uuid,integer,integer),
    yoao_assets_v1.complete_accepted_video_input_garbage(uuid,uuid),
    yoao_assets_v1.claim_native_pyramid_retirement(uuid,integer),
    yoao_assets_v1.prepare_native_pyramid_retirement_page(uuid,uuid,integer),
    yoao_assets_v1.complete_native_pyramid_retirement_page(uuid,uuid,uuid,text,jsonb)
    TO :"asset_cleanup_role";
-- Refresh explicit runtime grants after migrations; blanket defaults expose owner storage.

-- User-performance maintenance retains this MV; prediction partitions use
-- their Labeling owner. The migration role inherits the runtime, never vice versa.
DO $$
BEGIN
    EXECUTE format('ALTER MATERIALIZED VIEW public.mv_user_perf_daily OWNER TO %I',
                   current_setting('yoao.application_role'));
END $$;

REVOKE INSERT, UPDATE, DELETE ON TABLE public.alembic_version FROM :"application_role";
REVOKE DELETE ON TABLE public.gpu_backend_memberships, public.gpu_backend_fences
    FROM :"application_role";

DO $$
BEGIN
    IF has_table_privilege(current_setting('yoao.application_role'),
                           'public.gpu_backend_memberships', 'DELETE')
       OR has_table_privilege(current_setting('yoao.application_role'),
                              'public.gpu_backend_fences', 'DELETE') THEN
        RAISE EXCEPTION 'The runtime role still has effective GPU tombstone DELETE privileges';
    END IF;
END $$;

-- Keep fixed operational capabilities; fence Workspace storage in embedded runtimes.
DO $workspace_private_acl$
DECLARE
    v_runtime name := current_setting('yoao.application_role');
    v_runtime_oid oid;
    v_relations oid[];
    v_owners oid[];
    v_readable oid[] := ARRAY[
        'yoao_workspace_v1.project_facts'::regclass,
        'yoao_workspace_v1.project_member_facts'::regclass
    ];
    v_name text;
    v_relation record;
    v_schema record;
    v_default record;
    v_columns text;
BEGIN
    SELECT oid INTO v_runtime_oid FROM pg_catalog.pg_roles
    WHERE rolname=v_runtime AND rolcanlogin
      AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls);
    IF current_database() <> current_setting('yoao.expected_database')
       OR v_runtime_oid IS NULL OR v_runtime=current_user THEN
        RAISE EXCEPTION 'Invalid embedded Workspace runtime ownership';
    END IF;
    FOREACH v_name IN ARRAY ARRAY[
        'projects','project_members','project_templates',
        'project_deletion_operations','dataset_unlink_operations'
    ] LOOP
        IF pg_catalog.to_regclass(pg_catalog.format('public.%I',v_name)) IS NULL THEN
            RAISE EXCEPTION 'Missing private Workspace relation: %',v_name;
        END IF;
    END LOOP;
    v_relations := ARRAY(
        WITH RECURSIVE storage(oid) AS (
            SELECT c.oid FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE c.relkind IN ('r','p','v','m','f') AND (
                n.nspname ~ '^(aap|yoao)_workspace(_[a-z0-9]+)*_v[0-9]+$'
                OR (n.nspname='public' AND (
                    c.relname IN ('projects','project_members','project_templates',
                        'project_deletion_operations','dataset_unlink_operations')
                    OR pg_catalog.left(c.relname,10)='workspace_'
                ))
            )
            UNION
            SELECT i.inhrelid FROM pg_catalog.pg_inherits i
            JOIN storage s ON s.oid=i.inhparent
        ) SELECT oid FROM storage
    );
    v_owners := ARRAY(
        SELECT relowner FROM pg_catalog.pg_class WHERE oid=ANY(v_relations)
        UNION SELECT p.proowner FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname ~ '^(aap|yoao)_workspace(_[a-z0-9]+)*_v[0-9]+$'
        UNION SELECT nspowner FROM pg_catalog.pg_namespace
        WHERE nspname ~ '^(aap|yoao)_workspace(_[a-z0-9]+)*_v[0-9]+$'
        UNION SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_user
    );
    IF EXISTS (SELECT FROM pg_catalog.unnest(v_owners) owner_oid
               WHERE owner_oid=v_runtime_oid
                  OR pg_catalog.pg_has_role(v_runtime_oid,owner_oid,'MEMBER')) THEN
        RAISE EXCEPTION 'Runtime inherits private Workspace ownership';
    END IF;
    FOR v_relation IN
        SELECT c.oid,n.nspname,c.relname FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE c.oid=ANY(v_relations)
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON TABLE %I.%I FROM %I, PUBLIC',
            v_relation.nspname,v_relation.relname,v_runtime);
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),',' ORDER BY attnum)
        INTO v_columns FROM pg_catalog.pg_attribute
        WHERE attrelid=v_relation.oid AND attnum>0 AND NOT attisdropped;
        IF v_columns IS NOT NULL THEN
            EXECUTE pg_catalog.format('REVOKE ALL (%s) ON TABLE %I.%I FROM %I, PUBLIC',
                v_columns,v_relation.nspname,v_relation.relname,v_runtime);
        END IF;
        IF v_relation.oid=ANY(v_readable) THEN
            EXECUTE pg_catalog.format('GRANT SELECT ON TABLE %I.%I TO %I',
                v_relation.nspname,v_relation.relname,v_runtime);
        END IF;
        IF pg_catalog.has_table_privilege(v_runtime,v_relation.oid,
                'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR pg_catalog.has_any_column_privilege(v_runtime,v_relation.oid,
                'INSERT,UPDATE,REFERENCES')
           OR (NOT v_relation.oid=ANY(v_readable) AND (
               pg_catalog.has_table_privilege(v_runtime,v_relation.oid,'SELECT')
               OR pg_catalog.has_any_column_privilege(v_runtime,v_relation.oid,'SELECT')
           )) THEN
            RAISE EXCEPTION 'Runtime retains private Workspace access: %.%',
                v_relation.nspname,v_relation.relname;
        END IF;
    END LOOP;
    FOR v_relation IN
        SELECT c.oid,n.nspname,c.relname FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind='S' AND (
            n.nspname ~ '^(aap|yoao)_workspace(_[a-z0-9]+)*_v[0-9]+$'
            OR (n.nspname='public' AND c.relname IN (
                'display_seq_projects','display_seq_project_templates'
            ))
            OR EXISTS (SELECT FROM pg_catalog.pg_depend d
                       WHERE d.classid='pg_catalog.pg_class'::pg_catalog.regclass
                         AND d.objid=c.oid AND d.refclassid=d.classid
                         AND d.refobjid=ANY(v_relations) AND d.deptype IN ('a','i'))
        )
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON SEQUENCE %I.%I FROM %I, PUBLIC',
            v_relation.nspname,v_relation.relname,v_runtime);
        IF pg_catalog.has_sequence_privilege(v_runtime,v_relation.oid,'USAGE,SELECT,UPDATE') THEN
            RAISE EXCEPTION 'Runtime retains private Workspace sequence access: %.%',
                v_relation.nspname,v_relation.relname;
        END IF;
    END LOOP;
    FOR v_schema IN SELECT oid,nspname FROM pg_catalog.pg_namespace
        WHERE nspname ~ '^(aap|yoao)_workspace(_[a-z0-9]+)*_v[0-9]+$'
    LOOP
        EXECUTE pg_catalog.format('REVOKE CREATE ON SCHEMA %I FROM %I, PUBLIC',
            v_schema.nspname,v_runtime);
        IF pg_catalog.has_schema_privilege(v_runtime,v_schema.oid,'CREATE') THEN
            RAISE EXCEPTION 'Runtime retains private Workspace schema creation: %',
                v_schema.nspname;
        END IF;
    END LOOP;
    FOR v_default IN
        SELECT d.defaclobjtype,r.rolname,n.nspname
        FROM pg_catalog.pg_default_acl d
        JOIN pg_catalog.pg_roles r ON r.oid=d.defaclrole
        LEFT JOIN pg_catalog.pg_namespace n ON n.oid=d.defaclnamespace
        WHERE d.defaclrole=ANY(v_owners) AND d.defaclobjtype IN ('r','S')
          AND (d.defaclnamespace=0 OR n.nspname='public'
               OR n.nspname ~ '^(aap|yoao)_workspace(_[a-z0-9]+)*_v[0-9]+$')
          AND EXISTS (SELECT FROM pg_catalog.aclexplode(d.defaclacl) a
                      WHERE a.grantee IN (0,v_runtime_oid))
    LOOP
        EXECUTE pg_catalog.format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE %I%s REVOKE ALL ON %s FROM %I, PUBLIC',
            v_default.rolname,
            CASE WHEN v_default.nspname IS NULL THEN ''
                 ELSE pg_catalog.format(' IN SCHEMA %I',v_default.nspname) END,
            CASE v_default.defaclobjtype WHEN 'r' THEN 'TABLES' ELSE 'SEQUENCES' END,
            v_runtime);
    END LOOP;
END;
$workspace_private_acl$;
DO $$
DECLARE
    relation oid;
    cleanup_functions oid[] := ARRAY[
        'yoao_assets_v1.pending_storage_deletions_v2(text,timestamptz,uuid,uuid,integer)'::regprocedure::oid,
        'yoao_assets_v1.complete_storage_deletion(uuid,uuid)'::regprocedure::oid,
        'yoao_assets_v1.claim_image_garbage(uuid,integer,integer)'::regprocedure::oid,
        'yoao_assets_v1.complete_image_garbage(uuid,uuid)'::regprocedure::oid,
        'yoao_assets_v1.claim_accepted_input_garbage(uuid,integer,integer)'::regprocedure::oid,
        'yoao_assets_v1.complete_accepted_input_garbage(uuid,uuid)'::regprocedure::oid,
        'yoao_assets_v1.claim_accepted_video_input_garbage(uuid,integer,integer)'::regprocedure::oid,
        'yoao_assets_v1.complete_accepted_video_input_garbage(uuid,uuid)'::regprocedure::oid,
        'yoao_assets_v1.claim_native_pyramid_retirement(uuid,integer)'::regprocedure::oid,
        'yoao_assets_v1.prepare_native_pyramid_retirement_page(uuid,uuid,integer)'::regprocedure::oid,
        'yoao_assets_v1.complete_native_pyramid_retirement_page(uuid,uuid,uuid,text,jsonb)'::regprocedure::oid
    ];
BEGIN
    FOR relation IN
        SELECT catalog.oid
        FROM pg_catalog.pg_class AS catalog
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = catalog.relnamespace
        WHERE namespace.nspname = 'public'
          AND catalog.relkind IN ('r', 'p', 'v', 'm', 'f')
    LOOP
        IF has_any_column_privilege(current_setting('yoao.asset_cleanup_role'),
                                    relation, 'SELECT,INSERT,UPDATE,REFERENCES')
           OR has_table_privilege(current_setting('yoao.asset_cleanup_role'),
                                  relation,
                                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') THEN
            RAISE EXCEPTION 'The Assets cleanup role has direct table access';
        END IF;
    END LOOP;

    IF NOT has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                                'yoao_assets_v1', 'USAGE')
       OR has_database_privilege(current_setting('yoao.asset_cleanup_role'),
                                 current_database(), 'CREATE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'public', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_assets_v1', 'CREATE')
       OR NOT has_function_privilege(current_setting('yoao.asset_cleanup_role'),
                                     'yoao_assets_v1.pending_storage_deletions_v2(text,timestamptz,uuid,uuid,integer)',
                                     'EXECUTE')
       OR NOT has_function_privilege(current_setting('yoao.asset_cleanup_role'),
                                     'yoao_assets_v1.complete_storage_deletion(uuid,uuid)',
                                     'EXECUTE')
       OR has_function_privilege(current_setting('yoao.asset_cleanup_role'),
                                 'yoao_assets_v1.pending_storage_deletions(uuid,integer)',
                                 'EXECUTE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_identity_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_workspace_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_labeling_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_jobs_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_learning_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_inference_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_insights_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_diagnostics_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_export_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'),
                               'yoao_audit_v1', 'USAGE') THEN
        RAISE EXCEPTION 'The Assets cleanup role has access outside Assets';
    END IF;

    IF (SELECT count(*) FROM unnest(cleanup_functions) AS function(oid)
        WHERE has_function_privilege(current_setting('yoao.asset_cleanup_role'),function.oid,'EXECUTE')) <> 11 THEN
        RAISE EXCEPTION 'The Assets cleanup role is missing an approved cleanup function';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS proc
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = proc.pronamespace
        WHERE (pg_catalog.left(namespace.nspname, 4) = 'aap_'
               OR pg_catalog.left(namespace.nspname, 5) = 'yoao_')
          AND pg_catalog.right(namespace.nspname, 3) = '_v1'
          AND NOT proc.oid = ANY(cleanup_functions)
          AND pg_catalog.has_function_privilege(
              current_setting('yoao.asset_cleanup_role'), proc.oid, 'EXECUTE'
          )
    ) THEN
        RAISE EXCEPTION 'The Assets cleanup role can execute outside its eleven cleanup functions';
    END IF;
END $$;
-- Preserve embedded command capabilities while denying direct Identity storage.
DO $identity_private_acl$
DECLARE
    v_runtime name := current_setting('yoao.application_role');
    v_relation record;
    v_columns text;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR v_runtime=current_user
       OR pg_catalog.pg_has_role(v_runtime,current_user,'MEMBER')
       OR EXISTS (
           SELECT 1 FROM pg_catalog.pg_auth_members
           WHERE member=(SELECT oid FROM pg_catalog.pg_roles WHERE rolname=v_runtime)
       )
       OR NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=v_runtime AND rolcanlogin
             AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
       ) THEN
        RAISE EXCEPTION 'Invalid embedded Identity runtime ownership';
    END IF;
    FOR v_relation IN
        SELECT c.oid,n.nspname,c.relname
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind IN ('r','p','v','m','f') AND (
            (n.nspname='public' AND (
                c.relname IN ('users','api_keys','groups','organizations',
                    'organization_members','user_invitations',
                    'password_reset_tokens','email_verification_tokens')
                OR pg_catalog.left(c.relname,9)='identity_'
            )) OR (
                n.nspname ~ '^(aap|yoao)_identity(_[a-z0-9]+)*_v[0-9]+$'
                AND c.relkind <> 'v'
            )
        )
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON TABLE %I.%I FROM %I',
            v_relation.nspname,v_relation.relname,v_runtime);
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),',' ORDER BY attnum)
        INTO v_columns FROM pg_catalog.pg_attribute
        WHERE attrelid=v_relation.oid AND attnum>0 AND NOT attisdropped;
        IF v_columns IS NOT NULL THEN
            EXECUTE pg_catalog.format('REVOKE ALL (%s) ON TABLE %I.%I FROM %I',
                v_columns,v_relation.nspname,v_relation.relname,v_runtime);
        END IF;
        IF pg_catalog.has_table_privilege(v_runtime,v_relation.oid,
                'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR pg_catalog.has_any_column_privilege(v_runtime,v_relation.oid,
                'SELECT,INSERT,UPDATE,REFERENCES') THEN
            RAISE EXCEPTION 'Embedded runtime retains private Identity access: %.%',
                v_relation.nspname,v_relation.relname;
        END IF;
    END LOOP;
END;
$identity_private_acl$;
DO $labeling_private_acl$
DECLARE
    v_runtime name := current_setting('yoao.application_role');
    v_runtime_oid oid;
    v_tables text[] := ARRAY[
        'ai_mask_accept_decisions', 'annotation_comments',
        'annotation_conversion_plans', 'annotation_drafts', 'annotation_feedbacks',
        'annotation_input_sources', 'annotation_lineage_edges', 'annotation_operations',
        'annotation_video_input_sources', 'annotation_video_tracker_input_sources',
        'annotations', 'batch_result_bundles', 'batch_result_parts',
        'batch_result_payloads', 'failed_predictions', 'mask_annotation_revisions',
        'mask_format_imports', 'mask_gc_intents', 'mask_gc_key_epochs', 'mask_gc_roots',
        'mask_gc_sweeps', 'mask_gc_write_permit_secrets', 'mask_qc_issues', 'mask_qc_runs',
        'mask_repair_batches', 'mask_review_scopes', 'point_cloud_quality_evaluations',
        'point_cloud_quality_issues', 'point_cloud_quality_runs', 'prediction_metas',
        'prediction_retry_attempts', 'predictions', 'raster_mask_uploads',
        'scene_track_intervals', 'scene_track_operations', 'scene_tracks',
        'task_append_operations', 'task_batches', 'task_creation_chunks',
        'task_creation_operations', 'task_dataset_item_links', 'task_events',
        'task_locks', 'tasks', 'video_segments', 'video_track_quality_issues',
        'video_track_quality_runs', 'video_tracker_input_scopes', 'video_tracker_jobs'
    ];
    v_private oid[];
    v_owners oid[];
    v_name text;
    v_relation record;
    v_default record;
    v_columns text;
BEGIN
    SELECT oid INTO v_runtime_oid FROM pg_catalog.pg_roles
    WHERE rolname=v_runtime AND rolcanlogin
      AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls);
    IF current_database() <> current_setting('yoao.expected_database')
       OR v_runtime_oid IS NULL OR v_runtime=current_user THEN
        RAISE EXCEPTION 'Invalid embedded Labeling runtime ownership';
    END IF;
    FOREACH v_name IN ARRAY v_tables LOOP
        IF pg_catalog.to_regclass(pg_catalog.format('public.%I',v_name)) IS NULL THEN
            RAISE EXCEPTION 'Missing private Labeling relation: %',v_name;
        END IF;
    END LOOP;
    v_private := ARRAY(
        WITH RECURSIVE storage(oid) AS (
            SELECT c.oid FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE (n.nspname='public' AND c.relname=ANY(v_tables))
               OR (n.nspname ~ '^(aap|yoao)_labeling(_[a-z0-9]+)*_v[0-9]+$'
                   AND c.relkind IN ('r','p','m','f'))
            UNION
            SELECT i.inhrelid FROM pg_catalog.pg_inherits i
            JOIN storage s ON s.oid=i.inhparent
        ) SELECT oid FROM storage
    );
    v_owners := ARRAY(
        SELECT relowner FROM pg_catalog.pg_class WHERE oid=ANY(v_private)
        UNION SELECT p.proowner FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname ~ '^(aap|yoao)_labeling(_[a-z0-9]+)*_v[0-9]+$'
        UNION SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_user
    );
    IF EXISTS (SELECT FROM pg_catalog.unnest(v_owners) owner_oid
               WHERE owner_oid=v_runtime_oid
                  OR pg_catalog.pg_has_role(v_runtime_oid,owner_oid,'MEMBER')) THEN
        RAISE EXCEPTION 'Runtime inherits private Labeling ownership';
    END IF;
    FOR v_relation IN
        SELECT c.oid,n.nspname,c.relname FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE c.oid=ANY(v_private)
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON TABLE %I.%I FROM %I, PUBLIC',
            v_relation.nspname,v_relation.relname,v_runtime);
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),',' ORDER BY attnum)
        INTO v_columns FROM pg_catalog.pg_attribute
        WHERE attrelid=v_relation.oid AND attnum>0 AND NOT attisdropped;
        IF v_columns IS NOT NULL THEN
            EXECUTE pg_catalog.format('REVOKE ALL (%s) ON TABLE %I.%I FROM %I, PUBLIC',
                v_columns,v_relation.nspname,v_relation.relname,v_runtime);
        END IF;
        IF pg_catalog.has_table_privilege(v_runtime,v_relation.oid,
                'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR pg_catalog.has_any_column_privilege(v_runtime,v_relation.oid,
                'SELECT,INSERT,UPDATE,REFERENCES') THEN
            RAISE EXCEPTION 'Runtime retains private Labeling access: %.%',
                v_relation.nspname,v_relation.relname;
        END IF;
    END LOOP;
    FOR v_relation IN
        SELECT c.oid,n.nspname,c.relname FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind='S' AND (
            (n.nspname='public' AND c.relname IN ('display_seq_tasks','display_seq_batches'))
            OR n.nspname ~ '^(aap|yoao)_labeling(_[a-z0-9]+)*_v[0-9]+$'
            OR EXISTS (SELECT FROM pg_catalog.pg_depend d
                       WHERE d.classid='pg_catalog.pg_class'::pg_catalog.regclass
                         AND d.objid=c.oid AND d.refclassid=d.classid
                         AND d.refobjid=ANY(v_private) AND d.deptype IN ('a','i'))
        )
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON SEQUENCE %I.%I FROM %I, PUBLIC',
            v_relation.nspname,v_relation.relname,v_runtime);
        IF pg_catalog.has_sequence_privilege(v_runtime,v_relation.oid,'USAGE,SELECT,UPDATE') THEN
            RAISE EXCEPTION 'Runtime retains private Labeling sequence access: %.%',
                v_relation.nspname,v_relation.relname;
        END IF;
    END LOOP;
    FOR v_relation IN
        SELECT c.oid,n.nspname,c.relname FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind='v' AND n.nspname ~ '^(aap|yoao)_labeling(_[a-z0-9]+)*_v[0-9]+$'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER ON TABLE %I.%I FROM %I, PUBLIC',
            v_relation.nspname,v_relation.relname,v_runtime);
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),',' ORDER BY attnum)
        INTO v_columns FROM pg_catalog.pg_attribute
        WHERE attrelid=v_relation.oid AND attnum>0 AND NOT attisdropped;
        IF v_columns IS NOT NULL THEN
            EXECUTE pg_catalog.format(
                'REVOKE INSERT (%1$s),UPDATE (%1$s),REFERENCES (%1$s) ON TABLE %2$I.%3$I FROM %4$I, PUBLIC',
                v_columns,v_relation.nspname,v_relation.relname,v_runtime);
        END IF;
        IF pg_catalog.has_table_privilege(v_runtime,v_relation.oid,
                'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR pg_catalog.has_any_column_privilege(v_runtime,v_relation.oid,
                'INSERT,UPDATE,REFERENCES') THEN
            RAISE EXCEPTION 'Runtime retains Labeling view writes: %.%',
                v_relation.nspname,v_relation.relname;
        END IF;
    END LOOP;
    FOR v_default IN
        SELECT d.defaclobjtype,r.rolname,n.nspname
        FROM pg_catalog.pg_default_acl d
        JOIN pg_catalog.pg_roles r ON r.oid=d.defaclrole
        LEFT JOIN pg_catalog.pg_namespace n ON n.oid=d.defaclnamespace
        WHERE d.defaclrole=ANY(v_owners) AND d.defaclobjtype IN ('r','S')
          AND (d.defaclnamespace=0 OR n.nspname='public'
               OR n.nspname ~ '^(aap|yoao)_labeling(_[a-z0-9]+)*_v[0-9]+$')
          AND EXISTS (SELECT FROM pg_catalog.aclexplode(d.defaclacl) a
                      WHERE a.grantee IN (0,v_runtime_oid))
    LOOP
        EXECUTE pg_catalog.format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE %I%s REVOKE ALL ON %s FROM %I, PUBLIC',
            v_default.rolname,
            CASE WHEN v_default.nspname IS NULL THEN ''
                 ELSE pg_catalog.format(' IN SCHEMA %I',v_default.nspname) END,
            CASE v_default.defaclobjtype WHEN 'r' THEN 'TABLES' ELSE 'SEQUENCES' END,
            v_runtime);
    END LOOP;
END;
$labeling_private_acl$;
DO $inference_private_acl$
DECLARE
    v_runtime name := current_setting('yoao.application_role');
    v_runtime_oid oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=v_runtime);
    v_relation record;
    v_columns text;
    v_function record;
    v_signature text;
    v_public text[] := ARRAY[
        'yoao_inference_topology_v1.topology_policy()',
        'yoao_inference_topology_v1.registry_facts(uuid)',
        'yoao_inference_topology_v1.registry_auth_snapshot(uuid)',
        'yoao_inference_topology_v1.lock_plugin_registry(uuid)',
        'yoao_inference_topology_v1.prepare_plugin_installation(text)',
        'yoao_inference_topology_v1.plugin_installation(uuid)',
        'yoao_inference_topology_v1.plugin_installations()',
        'yoao_inference_topology_v1.plugin_installation_evidence(uuid)',
        'yoao_inference_topology_v1.plugin_asset_approval(uuid)',
        'yoao_inference_topology_v1.set_plugin_asset_approval(uuid,integer,text)',
        'yoao_inference_topology_v1.command_plugin_installation(uuid,integer,text,text)',
        'yoao_inference_topology_v1.authorize_plugin_routing_replacement(uuid,integer,uuid,text,text,text)',
        'yoao_inference_topology_v1.registry_url_owner(text)',
        'yoao_inference_topology_v1.registry_catalog()',
        'yoao_inference_topology_v1.registry_names(uuid[])',
        'yoao_inference_topology_v1.registry_reference(uuid)',
        'yoao_inference_topology_v1.pool_reference(uuid)',
        'yoao_inference_topology_v1.pool_for_registry(uuid)',
        'yoao_inference_topology_v1.pool_catalog()',
        'yoao_inference_topology_v1.pool_members(uuid)',
        'yoao_inference_topology_v1.project_backend_catalog(uuid)',
        'yoao_inference_topology_v1.project_backend_facts(uuid[])',
        'yoao_inference_topology_v1.enabled_project_ids(uuid[])',
        'yoao_inference_topology_v1.project_pool_catalog(uuid)',
        'yoao_inference_topology_v1.project_assoc_for_registry(uuid,uuid)',
        'yoao_inference_topology_v1.project_interactive_backend(uuid)',
        'yoao_inference_topology_v1.create_manual_registry(uuid,text)',
        'yoao_inference_topology_v1.singleton_pool(uuid)',
        'yoao_inference_topology_v1.create_singleton_pool(uuid,uuid,text,text,text)',
        'yoao_inference_topology_v1.create_singleton_member(uuid,uuid,uuid)',
        'yoao_inference_topology_v1.patch_registry_auth_cas(uuid,text,text,bigint)',
        'yoao_inference_topology_v1.validate_registry_delete(uuid)',
        'yoao_inference_topology_v1.delete_registry(uuid,text)',
        'yoao_inference_topology_v1.create_pool(uuid,text)',
        'yoao_inference_topology_v1.patch_pool(uuid,text)',
        'yoao_inference_topology_v1.lock_empty_pool(uuid)',
        'yoao_inference_topology_v1.delete_empty_pool(uuid)',
        'yoao_inference_topology_v1.lock_member_upsert_rows(uuid,uuid)',
        'yoao_inference_topology_v1.lock_repair_model_rows(uuid,uuid,uuid[])',
        'yoao_inference_topology_v1.upsert_member(uuid,uuid,integer,text)',
        'yoao_inference_topology_v1.remove_member(uuid,uuid,text)',
        'yoao_inference_topology_v1.drain_member(uuid,uuid)',
        'yoao_inference_topology_v1.resume_member(uuid,uuid)',
        'yoao_inference_topology_v1.drift_review_rows(uuid,uuid)',
        'yoao_inference_topology_v1.lock_drift_acceptance_rows(uuid,uuid)',
        'yoao_inference_topology_v1.accept_drift(uuid,uuid,text,boolean,text)',
        'yoao_inference_topology_v1.set_project_registry_enablement(uuid,uuid,boolean,text)',
        'yoao_inference_topology_v1.set_project_pool_enablement(uuid,uuid,boolean,text)',
        'yoao_inference_topology_v1.delete_project_associations(uuid)',
        'yoao_inference_topology_v1.seed_registry_by_url(text)',
        'yoao_inference_topology_v1.create_seed_registry(uuid,text,text,text)',
        'yoao_inference_backend_endpoint_v1.backend_endpoint_policy()',
        'yoao_inference_backend_endpoint_v1.capture_backend_endpoint_subject(uuid,uuid,text,text)',
        'yoao_inference_backend_endpoint_v1.controlled_model_credential_policy(uuid)',
        'yoao_inference_backend_endpoint_v1.health_backend_row(uuid,boolean)',
        'yoao_inference_backend_endpoint_v1.catalog_endpoint_rows()',
        'yoao_inference_backend_endpoint_v1.stats_endpoint_rows()',
        'yoao_inference_backend_endpoint_v1.observation_registration_labels(text[])',
        'yoao_inference_gpu_caller_v1.gpu_caller_policy()',
        'yoao_inference_gpu_caller_v1.gpu_caller_step(text,text)'
    ];
BEGIN
    IF current_database()<>current_setting('yoao.expected_database')
       OR v_runtime_oid IS NULL OR v_runtime=current_user
       OR EXISTS (SELECT FROM pg_catalog.pg_roles WHERE oid=v_runtime_oid
                  AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)) THEN
        RAISE EXCEPTION 'Invalid Inference caller runtime';
    END IF;
    FOR v_relation IN
        SELECT c.oid,n.nspname,c.relname,c.relowner FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE (n.nspname='public' AND c.relname IN (
            'ml_backend_registry','ml_backend_service_pools','ml_backend_pool_members',
            'project_ml_backend_pool','gpu_backend_fences','gpu_backend_memberships',
            'gpu_arbiter_rollouts','gpu_backend_cancel_intents'
        )) OR (n.nspname IN ('yoao_inference_topology_v1','yoao_inference_backend_endpoint_v1',
                            'yoao_inference_gpu_v1','yoao_inference_gpu_collector_v1')
               AND c.relkind IN ('r','p','v','m','f'))
    LOOP
        IF pg_catalog.pg_has_role(v_runtime_oid,v_relation.relowner,'MEMBER') THEN
            RAISE EXCEPTION 'Runtime inherits private Inference ownership';
        END IF;
        EXECUTE pg_catalog.format('REVOKE ALL ON TABLE %I.%I FROM %I, PUBLIC',
            v_relation.nspname,v_relation.relname,v_runtime);
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),',' ORDER BY attnum)
        INTO v_columns FROM pg_catalog.pg_attribute
        WHERE attrelid=v_relation.oid AND attnum>0 AND NOT attisdropped;
        IF v_columns IS NOT NULL THEN
            EXECUTE pg_catalog.format('REVOKE ALL (%s) ON TABLE %I.%I FROM %I, PUBLIC',
                v_columns,v_relation.nspname,v_relation.relname,v_runtime);
        END IF;
        IF pg_catalog.has_table_privilege(v_runtime_oid,v_relation.oid,
                'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR pg_catalog.has_any_column_privilege(v_runtime_oid,v_relation.oid,
                'SELECT,INSERT,UPDATE,REFERENCES') THEN
            RAISE EXCEPTION 'Runtime retains private Inference relation access';
        END IF;
    END LOOP;
    FOR v_function IN
        SELECT p.oid::pg_catalog.regprocedure AS signature
        FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname IN ('yoao_inference_topology_v1','yoao_inference_backend_endpoint_v1',
                            'yoao_inference_gpu_v1','yoao_inference_gpu_caller_v1',
                            'yoao_inference_gpu_collector_v1')
           OR (n.nspname='yoao_inference_backend_probe_v1' AND p.proname='open_backend_probe_subject')
           OR (n.nspname='yoao_inference_health_observation_v1' AND p.proname='open_health_observation_subject')
           OR (n.nspname='yoao_inference_health_data_v1' AND p.proname='backend_row')
           OR (n.nspname='yoao_inference_registry_ingress_v1' AND p.proname IN (
               'registry_by_url','registry_candidates','create_registry','apply_observation',
               'disconnect_env_registry','sync_result','record_sync_result'
           ))
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON FUNCTION %s FROM %I, PUBLIC',
            v_function.signature,v_runtime);
    END LOOP;
    FOREACH v_signature IN ARRAY v_public LOOP
        IF pg_catalog.to_regprocedure(v_signature) IS NULL THEN
            RAISE EXCEPTION 'Missing public Inference capability: %',v_signature;
        END IF;
        EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA %I TO %I',
            pg_catalog.split_part(v_signature,'.',1),v_runtime);
        EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION %s TO %I',v_signature,v_runtime);
    END LOOP;
END;
$inference_private_acl$;
REVOKE EXECUTE ON FUNCTION
    yoao_assets_v1.update_storage_connection(uuid,uuid,uuid,jsonb,jsonb,text,text,text,jsonb,jsonb)
    FROM :"application_role", PUBLIC;
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.list_storage_connections(uuid,timestamptz,uuid,integer),
    yoao_assets_v1.read_storage_connection(uuid,uuid),
    yoao_assets_v1.read_storage_connection_policy(uuid,jsonb),
    yoao_assets_v1.capture_storage_connection(uuid,uuid,text),
    yoao_assets_v1.read_storage_connection_receipt(uuid,uuid,text,uuid,jsonb),
    yoao_assets_v1.create_storage_connection(uuid,uuid,jsonb,jsonb,text,text,jsonb,jsonb),
    yoao_assets_v1.delete_storage_connection(uuid,uuid,uuid,jsonb),
    yoao_assets_v1.record_storage_connection_test(uuid,uuid,uuid,text,text,jsonb,jsonb,jsonb),
    yoao_assets_v1.update_storage_connection_authorized(uuid,uuid,uuid,jsonb,jsonb,text,text,text,jsonb,jsonb,uuid,text[],jsonb),
    yoao_assets_v1.begin_connection_probe(uuid,uuid,uuid,text[]),
    yoao_assets_v1.finish_connection_probe(uuid,uuid,uuid,uuid,text[],jsonb,boolean)
    TO :"application_role";
COMMIT;
