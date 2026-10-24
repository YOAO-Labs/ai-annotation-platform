-- Run with psql as the migration owner after the application migration head is
-- installed. All changes are confined to the selected database and runtime role.
-- psql -v expected_database=annotation_production -v application_role=anno_prod_app
--       -v migration_role=anno_prod_owner -v asset_cleanup_role=asset_cleanup_client
--       -f lan-production-grants.sql
-- application_role composes embedded owners through their published capabilities.
-- Identity and Notifications private storage is denied; other legacy access remains.
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.application_role', :'application_role', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.asset_cleanup_role', :'asset_cleanup_role', true);

DO $$
BEGIN
    IF current_database() <> current_setting('aap.expected_database') THEN
        RAISE EXCEPTION 'Unexpected database; refusing to change application grants';
    END IF;
    IF current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Run this script as the migration owner';
    END IF;
    IF current_setting('aap.application_role') = current_user
       OR pg_has_role(current_setting('aap.application_role'), current_user, 'MEMBER') THEN
        RAISE EXCEPTION 'The runtime role must not inherit the migration owner';
    END IF;
    IF current_setting('aap.asset_cleanup_role') IN (
           current_setting('aap.application_role'), current_user
       )
       OR pg_has_role(current_setting('aap.asset_cleanup_role'), current_user, 'MEMBER')
       OR EXISTS (
           SELECT 1 FROM pg_catalog.pg_auth_members AS member
           WHERE member.member = (
               SELECT candidate.oid FROM pg_catalog.pg_roles AS candidate
               WHERE candidate.rolname = current_setting('aap.asset_cleanup_role')
           ) OR member.roleid = (
               SELECT candidate.oid FROM pg_catalog.pg_roles AS candidate
               WHERE candidate.rolname = current_setting('aap.asset_cleanup_role')
           )
       ) THEN
        RAISE EXCEPTION 'The Assets cleanup role must be distinct and have no role memberships';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_roles
        WHERE rolname = current_setting('aap.application_role')
          AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'The runtime role must not hold cluster administration privileges';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles
        WHERE rolname = current_setting('aap.asset_cleanup_role')
          AND rolcanlogin
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'The Assets cleanup role must be a non-admin LOGIN role';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM PUBLIC;
-- Concurrent materialized-view refresh uses temporary relations internally.
GRANT CONNECT, TEMPORARY ON DATABASE :"expected_database" TO :"application_role";
REVOKE ALL ON DATABASE :"expected_database" FROM :"asset_cleanup_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"asset_cleanup_role";
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO :"application_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"application_role";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"application_role";
GRANT USAGE ON SCHEMA aap_workspace_v1, aap_workspace_membership_v1,
    aap_workspace_owner_transfer_v1, aap_inference_pipelines_v1,
    aap_assets_v1, aap_labeling_v1, aap_jobs_v1, aap_learning_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION aap_inference_pipelines_v1.pipeline_policy(),
    aap_inference_pipelines_v1.pipeline_step(jsonb, text, jsonb, text[], text[])
    TO :"application_role";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.clear_primary_backend_bindings(uuid)
    TO :"application_role";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.read_import_project(text),
    aap_workspace_v1.create_import_project(uuid,text,text,uuid,jsonb),
    aap_workspace_v1.create_point_cloud_seed_project(uuid,text,text,uuid,jsonb),
    aap_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb),
    aap_workspace_v1.write_seed_members(jsonb),
    aap_workspace_v1.create_media_seed_project(uuid,text,text,uuid,text,jsonb),
    aap_workspace_v1.write_seed_project_projection(uuid,jsonb),
    aap_workspace_v1.pin_seed_member_assignment(uuid,timestamptz),
    aap_workspace_v1.create_scale_seed_project(uuid,text),
    aap_workspace_v1.ensure_scale_seed_member(uuid,uuid,uuid),
    aap_workspace_v1.guard_scale_seed_cleanup(text),
    aap_workspace_v1.delete_scale_seed_projects(text,uuid[]),
    aap_assets_v1.guard_scale_seed_dataset_cleanup(uuid,boolean),
    aap_workspace_v1.write_point_cloud_quality_config(uuid,jsonb)
    TO :"application_role";
GRANT USAGE ON SCHEMA aap_inference_backend_probe_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION aap_inference_backend_probe_v1.backend_probe_policy(),
    aap_inference_backend_probe_v1.open_backend_probe_subject(text,uuid)
    TO :"application_role";
GRANT USAGE ON SCHEMA aap_inference_health_observation_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION aap_inference_health_observation_v1.health_observation_policy(),
    aap_inference_health_observation_v1.capture_health_observation_subject(uuid,uuid,text,text,double precision),
    aap_inference_health_observation_v1.open_health_observation_subject(text,uuid,uuid),
    aap_inference_health_observation_v1.seal_health_observation(text,uuid,uuid,text),
    aap_inference_health_observation_v1.verify_health_observation(text,uuid,uuid,text)
    TO :"application_role";
GRANT USAGE ON SCHEMA aap_inference_health_data_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION aap_inference_health_data_v1.health_data_policy(),
    aap_inference_health_data_v1.backend_row(uuid,boolean),
    aap_inference_health_data_v1.membership_row(uuid,text,boolean),
    aap_inference_health_data_v1.observation_clock(),
    aap_inference_health_data_v1.lock_resource(text),
    aap_inference_health_data_v1.try_promotion(),
    aap_inference_health_data_v1.write_backend(uuid,text,timestamp with time zone,text,boolean,boolean),
    aap_inference_health_data_v1.member_pool(uuid),
    aap_inference_health_data_v1.pool_row(uuid,uuid),
    aap_inference_health_data_v1.member_row(uuid),
    aap_inference_health_data_v1.apply_reconciliation(uuid,uuid,text,text,text,bigint,text,text)
    TO :"application_role";
GRANT USAGE ON SCHEMA aap_inference_registry_ingress_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION aap_inference_registry_ingress_v1.registry_ingress_policy(),
    aap_inference_registry_ingress_v1.registry_by_url(text),
    aap_inference_registry_ingress_v1.registry_candidates(text),
    aap_inference_registry_ingress_v1.create_registry(uuid,text,text,text,boolean,text),
    aap_inference_registry_ingress_v1.singleton_pool(uuid),
    aap_inference_registry_ingress_v1.create_singleton_pool(uuid,uuid,uuid,text,text,text),
    aap_inference_registry_ingress_v1.apply_observation(uuid,text,text,text,boolean,text,timestamp with time zone,text,boolean,text),
    aap_inference_registry_ingress_v1.disconnect_env_registry(uuid,text),
    aap_inference_registry_ingress_v1.tag_seed_registry(uuid,text),
    aap_inference_registry_ingress_v1.sync_result(uuid,text),
    aap_inference_registry_ingress_v1.record_sync_result(uuid,text,integer)
    TO :"application_role";
GRANT USAGE ON SCHEMA aap_inference_routing_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION aap_inference_routing_v1.routing_policy(),
    aap_inference_routing_v1.routing_step(text,text),
    aap_inference_routing_v1.scope_retirement_step(text,text),
    aap_inference_routing_v1.video_execution_scope_step(text,text)
    TO :"application_role";
GRANT EXECUTE ON FUNCTION aap_learning_v1.registry_evaluation_reference_exists(uuid),
    aap_jobs_v1.count_running_registry_predictions(uuid),
    aap_jobs_v1.video_execution_step(text,text),
    aap_jobs_v1.video_execution_checkpoint_step(text,text) TO :"application_role";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.video_writer_step(text,text)
    TO :"application_role";
-- Embedded Identity adapters use the same owner capabilities as isolated clients.
GRANT USAGE ON SCHEMA aap_identity_v1, aap_identity_keys_v1,
    aap_identity_sessions_v1, aap_identity_accounts_v1, aap_identity_recovery_v1,
    aap_identity_groups_v1, aap_identity_deactivation_v1,
    aap_identity_invitations_v1, aap_identity_mail_v2,
    aap_identity_directory_v1, aap_identity_admin_passwords_v1,
    aap_identity_platform_roles_v1, aap_identity_suspensions_v1,
    aap_identity_deletions_v1, aap_identity_offboarding_v1,
    aap_identity_briefs_v1,
    aap_settings_v1, aap_notifications_v1 TO :"application_role";
-- Existing API bindings call these owner functions inside the business transaction.
GRANT USAGE ON SCHEMA aap_audit_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION aap_audit_v1.append_events(jsonb),
    aap_audit_v1.query_events(pg_catalog.jsonb),
    aap_notifications_v1.preference_command(jsonb),
    aap_notifications_v1.append_notification(jsonb),
    aap_notifications_v1.append_job_terminal_notification(jsonb),
    aap_notifications_v1.inbox_page(uuid,text,boolean,integer,integer),
    aap_notifications_v1.inbox_total_count(uuid,text),
    aap_notifications_v1.inbox_unread_count(uuid,text),
    aap_notifications_v1.read_notification(uuid),
    aap_notifications_v1.find_terminal_notification(uuid,text,uuid),
    aap_notifications_v1.delivery_job_scopes(uuid[]),
    aap_notifications_v1.delivery_member_pairs(jsonb),
    aap_notifications_v1.delivery_reviewer_pairs(jsonb),
    aap_notifications_v1.delivery_owner_pairs(jsonb),
    aap_notifications_v1.delivery_super_pairs(jsonb),
    aap_notifications_v1.read_state_command(jsonb),
    aap_notifications_v1.delete_command(pg_catalog.jsonb)
    TO :"application_role";
SELECT right(current_database(),5)='_test' OR right(current_database(),4)='_e2e'
    AS identity_fixture_database \gset
\if :identity_fixture_database
GRANT EXECUTE ON FUNCTION aap_identity_v1.replace_fixture_preferences(uuid,jsonb)
    TO :"application_role";
\endif

SELECT right(lower(current_database()),5)='_test' OR right(lower(current_database()),4)='_e2e'
    AS identity_e2e_fixture_database \gset
\if :identity_e2e_fixture_database
GRANT USAGE ON SCHEMA aap_workspace_fixture_v1 TO :"application_role";
GRANT SELECT ON aap_workspace_fixture_v1.templates TO :"application_role";
GRANT EXECUTE ON FUNCTION
    aap_workspace_fixture_v1.create_project(jsonb),
    aap_workspace_fixture_v1.create_templates(jsonb),
    aap_workspace_fixture_v1.guard_project_cleanup(uuid[]),
    aap_workspace_fixture_v1.delete_projects(uuid[]),
    aap_workspace_fixture_v1.delete_templates(boolean,text[])
    TO :"application_role";

GRANT USAGE ON SCHEMA aap_identity_fixture_v1 TO :"application_role";
GRANT SELECT ON aap_identity_fixture_v1.account_profiles TO :"application_role";
GRANT EXECUTE ON FUNCTION
    aap_identity_fixture_v1.create_account(uuid,text,text,text,text),
    aap_identity_fixture_v1.mark_manual_inactive(uuid,timestamptz,uuid),
    aap_identity_fixture_v1.create_invitations(jsonb),
    aap_identity_fixture_v1.delete_accounts(boolean,text[]),
    aap_identity_fixture_v1.delete_reset_tokens(uuid[]),
    aap_identity_fixture_v1.delete_invitations(uuid[]),
    aap_identity_fixture_v1.delete_organization_members(uuid[])
    TO :"application_role";
\endif

GRANT SELECT ON aap_identity_v1.account_profiles,
    aap_identity_v1.invitation_audit_targets TO :"application_role";
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
REVOKE ALL ON TABLE aap_notifications_v1.notification_facts
    FROM :"application_role", PUBLIC;
REVOKE ALL (id, user_id, type, target_type, target_id, payload, read_at, created_at)
    ON TABLE aap_notifications_v1.notification_facts FROM :"application_role", PUBLIC;
REVOKE EXECUTE ON FUNCTION aap_notifications_v1._visible_inbox_rows(uuid,text)
    FROM :"application_role", PUBLIC;
REVOKE ALL ON TABLE aap_audit_v1.audit_events FROM :"application_role", PUBLIC;
REVOKE ALL (id, actor_id, actor_email, actor_role, action, target_type, target_id,
    method, path, status_code, ip, detail_json, request_id, created_at)
    ON TABLE aap_audit_v1.audit_events FROM :"application_role", PUBLIC;
GRANT SELECT ON aap_audit_v1.audit_events TO :"application_role";
REVOKE ALL ON TABLE public.mv_audit_bi_daily FROM :"application_role", PUBLIC;
REVOKE ALL (day, action, target_type, actor_role, status_family, event_count)
    ON TABLE public.mv_audit_bi_daily FROM :"application_role", PUBLIC;
REVOKE ALL ON TABLE aap_audit_v1.audit_daily_facts FROM :"application_role", PUBLIC;
REVOKE ALL (day, action, target_type, actor_role, status_family, event_count)
    ON TABLE aap_audit_v1.audit_daily_facts FROM :"application_role", PUBLIC;
GRANT SELECT ON aap_audit_v1.audit_daily_facts TO :"application_role";
REVOKE ALL ON TABLE aap_jobs_v1.notification_scope_facts FROM :"application_role", PUBLIC;
REVOKE ALL (id, kind, scope_project_id)
    ON TABLE aap_jobs_v1.notification_scope_facts FROM :"application_role", PUBLIC;
GRANT SELECT ON aap_jobs_v1.notification_scope_facts TO :"application_role";
REVOKE EXECUTE ON FUNCTION aap_audit_v1.refresh_audit_bi_mv()
    FROM :"application_role", PUBLIC;
DO $notification_private_acl$
DECLARE
    v_runtime pg_catalog.name := current_setting('aap.application_role');
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
            'aap_notifications_v1.notification_facts', 'SELECT')
       OR pg_catalog.has_any_column_privilege(v_runtime,
            'aap_notifications_v1.notification_facts', 'SELECT')
       OR pg_catalog.has_table_privilege(v_runtime,
            'aap_notifications_v1.notification_facts',
            'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(v_runtime,
            'aap_notifications_v1.notification_facts', 'INSERT,UPDATE,REFERENCES') THEN
        RAISE EXCEPTION 'The runtime role can read Notifications facts directly';
    END IF;
    IF NOT pg_catalog.has_function_privilege(v_runtime,
            'aap_notifications_v1.read_notification(uuid)', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_runtime,
            'aap_notifications_v1.find_terminal_notification(uuid,text,uuid)', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_runtime,
            'aap_notifications_v1.delivery_job_scopes(uuid[])', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_runtime,
            'aap_notifications_v1.delivery_member_pairs(jsonb)', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_runtime,
            'aap_notifications_v1.delivery_reviewer_pairs(jsonb)', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_runtime,
            'aap_notifications_v1.delivery_owner_pairs(jsonb)', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_runtime,
            'aap_notifications_v1.delivery_super_pairs(jsonb)', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_runtime,
            'aap_notifications_v1.inbox_page(uuid,text,boolean,integer,integer)', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_runtime,
            'aap_notifications_v1.inbox_total_count(uuid,text)', 'EXECUTE')
       OR NOT pg_catalog.has_function_privilege(v_runtime,
            'aap_notifications_v1.inbox_unread_count(uuid,text)', 'EXECUTE')
       OR pg_catalog.has_function_privilege(v_runtime,
            'aap_notifications_v1._visible_inbox_rows(uuid,text)', 'EXECUTE') THEN
        RAISE EXCEPTION 'The runtime role is missing fixed Notifications reads';
    END IF;
END;
$notification_private_acl$;
DO $audit_read_acl$
DECLARE
    v_runtime pg_catalog.name := current_setting('aap.application_role');
BEGIN
    IF NOT pg_catalog.has_table_privilege(v_runtime, 'aap_audit_v1.audit_events', 'SELECT')
       OR pg_catalog.has_table_privilege(v_runtime, 'aap_audit_v1.audit_events',
            'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(v_runtime, 'aap_audit_v1.audit_events',
            'INSERT,UPDATE,REFERENCES')
       OR NOT pg_catalog.has_table_privilege(v_runtime, 'aap_audit_v1.audit_daily_facts', 'SELECT')
       OR pg_catalog.has_table_privilege(v_runtime, 'aap_audit_v1.audit_daily_facts',
            'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(v_runtime, 'aap_audit_v1.audit_daily_facts',
            'INSERT,UPDATE,REFERENCES')
       OR pg_catalog.has_table_privilege(v_runtime, 'public.mv_audit_bi_daily',
            'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(v_runtime, 'public.mv_audit_bi_daily',
            'SELECT,INSERT,UPDATE,REFERENCES')
       OR pg_catalog.has_function_privilege(v_runtime, 'aap_audit_v1.refresh_audit_bi_mv()', 'EXECUTE') THEN
        RAISE EXCEPTION 'The runtime role is missing read-only Audit facts';
    END IF;
END;
$audit_read_acl$;
DO $jobs_scope_acl$
DECLARE
    v_runtime pg_catalog.name := current_setting('aap.application_role');
BEGIN
    IF NOT pg_catalog.has_table_privilege(v_runtime, 'aap_jobs_v1.notification_scope_facts', 'SELECT')
       OR pg_catalog.has_table_privilege(v_runtime, 'aap_jobs_v1.notification_scope_facts',
            'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(v_runtime, 'aap_jobs_v1.notification_scope_facts',
            'INSERT,UPDATE,REFERENCES') THEN
        RAISE EXCEPTION 'The runtime role is missing read-only Jobs notification facts';
    END IF;
END;
$jobs_scope_acl$;
GRANT SELECT ON aap_workspace_v1.project_member_facts TO :"application_role";
GRANT SELECT ON aap_workspace_v1.project_facts,
    aap_labeling_v1.project_catalog_counters TO :"application_role";
GRANT EXECUTE ON FUNCTION
    aap_identity_v1.authentication_policy(),
    aap_identity_v1.read_provisioning_account(text),
    aap_identity_v1.create_provisioning_account(jsonb),
    aap_identity_v1.create_seed_persona_account(jsonb),
    aap_identity_v1.read_scale_seed_super_admin(),
    aap_identity_v1.read_scale_seed_annotator(uuid[]),
    aap_identity_v1.read_actor_context(uuid,text),
    aap_identity_v1.read_actor_facts(uuid),
    aap_identity_v1.lock_accounts_share(uuid[],boolean),
    aap_identity_v1.check_accepted_api_key(uuid,uuid,text[]),
    aap_identity_v1.lock_accounts_update(uuid[],boolean),
    aap_identity_v1.lock_accounts_key_share(uuid[],boolean),
    aap_identity_v1.lock_super_admin_invariant(),
    aap_identity_v1.super_admin_status(uuid),
    aap_identity_v1.read_jwt_principal(uuid,uuid,bigint,timestamptz,boolean),
    aap_identity_v1.read_api_key_candidates(text,uuid,integer),
    aap_identity_v1.confirm_api_key_principal(uuid,text),
    aap_identity_v1.get_credential_generation(uuid),
    aap_identity_v1.advance_credential_generation(uuid),
    aap_identity_v1.revoke_jti(uuid,timestamptz),
    aap_identity_v1.is_jti_revoked(uuid),
    aap_identity_v1.retire_account_credentials(uuid),
    aap_identity_v1.lock_current_principal(uuid,text,uuid,bigint,timestamptz,text[]),
    aap_identity_keys_v1.key_policy(),
    aap_identity_keys_v1.execute_key_command(jsonb),
    aap_identity_sessions_v1.session_policy(),
    aap_identity_sessions_v1.read_login_candidate(text),
    aap_identity_sessions_v1.execute_session_command(jsonb),
    aap_identity_accounts_v1.account_policy(),
    aap_identity_accounts_v1.prepare_account_command(jsonb),
    aap_identity_accounts_v1.apply_account_command(jsonb),
    aap_identity_accounts_v1.mark_inactive_accounts_offline(timestamptz),
    aap_identity_recovery_v1.recovery_policy(),
    aap_identity_recovery_v1.prepare_recovery_command(jsonb),
    aap_identity_recovery_v1.apply_recovery_command(jsonb),
    aap_identity_recovery_v1.read_registration_status(jsonb),
    aap_identity_recovery_v1.mail_policy(),
    aap_identity_recovery_v1.claim_mail(jsonb),
    aap_identity_recovery_v1.renew_mail(jsonb),
    aap_identity_recovery_v1.finish_mail(jsonb),
    aap_identity_groups_v1.group_policy(),
    aap_identity_groups_v1.execute_group_command(jsonb),
    aap_identity_deactivation_v1.deactivation_policy(),
    aap_identity_deactivation_v1.execute_deactivation_command(jsonb),
    aap_identity_deactivation_v1.due_policy(),
    aap_identity_deactivation_v1.process_due_deactivations(jsonb),
    aap_identity_invitations_v1.invitation_policy(),
    aap_identity_invitations_v1.prepare_invitation_command(jsonb),
    aap_identity_invitations_v1.apply_invitation_command(jsonb),
    aap_identity_directory_v1.directory_policy(),
    aap_identity_directory_v1.execute_directory_command(jsonb),
    aap_identity_admin_passwords_v1.admin_password_policy(),
    aap_identity_admin_passwords_v1.execute_admin_password_command(jsonb),
    aap_identity_platform_roles_v1.platform_role_policy(),
    aap_identity_platform_roles_v1.execute_platform_role_command(jsonb),
    aap_identity_suspensions_v1.suspension_policy(),
    aap_identity_suspensions_v1.execute_suspension_command(jsonb),
    aap_identity_deletions_v1.deletion_policy(),
    aap_identity_deletions_v1.execute_deletion_command(jsonb),
    aap_identity_offboarding_v1.offboarding_policy(),
    aap_identity_offboarding_v1.prepare_offboarding_command(jsonb),
    aap_identity_offboarding_v1.apply_offboarding_command(jsonb),
    aap_identity_briefs_v1.briefs_policy(),
    aap_identity_briefs_v1.execute_briefs_command(jsonb),
    aap_identity_mail_v2.mail_policy(),
    aap_identity_mail_v2.claim_mail(jsonb),
    aap_identity_mail_v2.renew_mail(jsonb),
    aap_identity_mail_v2.finish_mail(jsonb),
    aap_notifications_v1.identity_delivery_policy(),
    aap_notifications_v1.claim_identity_deliveries(jsonb),
    aap_notifications_v1.finish_identity_delivery(jsonb),
    aap_settings_v1.mail_delivery_policy(jsonb)
    TO :"application_role";
GRANT EXECUTE ON FUNCTION
    aap_workspace_membership_v1.membership_policy(),
    aap_workspace_membership_v1.prepare_membership_command(jsonb),
    aap_workspace_membership_v1.apply_membership_command(jsonb)
    TO :"application_role";
GRANT EXECUTE ON FUNCTION
    aap_workspace_owner_transfer_v1.owner_transfer_policy(),
    aap_workspace_owner_transfer_v1.transfer_project_owner(jsonb)
    TO :"application_role";
GRANT USAGE ON SCHEMA aap_workspace_configuration_v1,
    aap_workspace_template_query_v1, aap_workspace_directory_query_v1
    TO :"application_role";
GRANT EXECUTE ON FUNCTION
    aap_workspace_configuration_v1.configuration_policy(),
    aap_workspace_configuration_v1.configuration_step(jsonb,text,jsonb,text[],text[]),
    aap_workspace_template_query_v1.template_query_policy(),
    aap_workspace_template_query_v1.template_query_step(jsonb,text,jsonb,text,text[],text),
    aap_workspace_directory_query_v1.directory_query_policy(),
    aap_workspace_directory_query_v1.directory_query_step(jsonb,text,jsonb)
    TO :"application_role";
GRANT USAGE ON SCHEMA aap_workspace_project_query_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION
    aap_workspace_project_query_v1.project_query_policy(),
    aap_workspace_project_query_v1.project_query_step(
        jsonb, text, jsonb, text, text, text[], text[], uuid, text,
        timestamptz, timestamptz, bigint, integer
    )
    TO :"application_role";
-- These cross-owner dependencies are called only by Workspace SECURITY DEFINER code.
REVOKE ALL ON FUNCTION
    aap_identity_v1.workspace_configuration_org_scope(uuid, uuid),
    aap_assets_v1.workspace_configuration_datasets(uuid),
    aap_labeling_v1.workspace_configuration_video_facts(uuid),
    aap_inference_v1.enable_workspace_project_backend(uuid, uuid)
    FROM :"application_role";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.lock_project_reference(uuid),
    aap_workspace_v1.lock_project_reference_nowait(uuid),
    aap_workspace_v1.read_project_row(uuid),
    aap_workspace_v1.actor_project_memberships(uuid),
    aap_workspace_project_query_v1.project_search_scope(uuid, text),
    aap_workspace_project_query_v1.project_search_member_ids(uuid, text),
    aap_workspace_v1.lock_project_references_for_update(uuid[]),
    aap_workspace_v1.lock_project_member_reference(uuid, uuid, boolean),
    aap_workspace_v1.project_member_rows(uuid, uuid[], boolean),
    aap_workspace_v1.lock_project_manager(uuid, uuid),
    aap_workspace_v1.project_access_step(uuid, uuid, text),
    aap_workspace_v1.lock_project_row(uuid, boolean),
    aap_workspace_v1.delete_project(uuid, uuid, uuid, jsonb),
    aap_workspace_v1.preview_dataset_unlink(uuid, uuid, uuid),
    aap_workspace_v1.unlink_dataset(uuid, uuid, uuid, uuid, text, jsonb),
    aap_assets_v1.lock_dataset_reference(uuid),
    aap_assets_v1.lock_current_association(uuid, uuid),
    aap_assets_v1.associate_project(uuid, uuid, uuid),
    aap_assets_v1.lock_scene_source_references(uuid[]),
    aap_assets_v1.delete_dataset(uuid, uuid, uuid, text, jsonb),
    aap_assets_v1.delete_dataset_item(uuid, uuid, uuid, uuid, text, jsonb),
    aap_assets_v1.reserve_dataset_upload(uuid, uuid, uuid, text, text),
    aap_assets_v1.lock_dataset_upload(uuid, uuid, uuid),
    aap_assets_v1.find_dataset_upload_duplicate(uuid, uuid, uuid, text),
    aap_assets_v1.complete_dataset_upload(uuid, uuid, uuid, uuid, bigint, text, integer, integer),
    aap_assets_v1.discard_dataset_upload(uuid, uuid, uuid, text),
    aap_labeling_v1.append_dataset_items(uuid, uuid[], uuid),
    aap_labeling_v1.write_project_counter_projection(uuid, jsonb),
    aap_labeling_v1.decrement_project_review_count(uuid),
    aap_labeling_v1.decrement_project_completed_count(uuid),
    aap_labeling_v1.record_project_review_approval(uuid),
    aap_labeling_v1.read_mask_write_epochs(jsonb),
    aap_labeling_v1.begin_mask_content_write(jsonb),
    aap_labeling_v1.lock_mask_write_epochs(jsonb),
    aap_labeling_v1.register_tracker_running_mask_root(jsonb),
    aap_labeling_v1.capture_video_tracker_input(uuid,uuid,integer),
    aap_labeling_v1.lock_video_tracker_input_for_stage(uuid,uuid,uuid,uuid,text,text),
    aap_labeling_v1.lock_video_tracker_input_for_accept(uuid,uuid,uuid,uuid,text,text),
    aap_labeling_v1.attach_annotation_video_tracker_input_source(uuid,uuid,uuid,jsonb),
    aap_labeling_v1.copy_annotation_video_tracker_input_sources(uuid,uuid,uuid),
    aap_labeling_v1.claim_mask_gc_sweep(jsonb),
    aap_labeling_v1.stage_mask_gc_page(jsonb),
    aap_labeling_v1.claim_mask_gc_deletes(jsonb),
    aap_labeling_v1.finish_mask_gc_delete(jsonb),
    aap_labeling_v1.read_mask_gc_progress(jsonb),
    aap_labeling_v1.inspect_mask_gc_page(jsonb),
    aap_labeling_v1.begin_batch_result(jsonb),
    aap_labeling_v1.put_batch_result_part(jsonb),
    aap_labeling_v1.seal_batch_result(jsonb),
    aap_labeling_v1.read_batch_result(jsonb),
    aap_labeling_v1.lock_batch_result_write(jsonb),
    aap_labeling_v1.finish_batch_result_write(jsonb),
    aap_labeling_v1.finish_batch_result_failure(jsonb),
    aap_jobs_v1.claim_batch_result_execution(jsonb),
    aap_jobs_v1.heartbeat_batch_result_execution(jsonb),
    aap_jobs_v1.release_batch_result_execution(jsonb),
    aap_jobs_v1.stage_batch_result_members(jsonb),
    aap_jobs_v1.start_batch_result_execution(jsonb),
    aap_jobs_v1.read_batch_result_execution(jsonb),
    aap_jobs_v1.mark_batch_result_overwrite(jsonb),
    aap_jobs_v1.skip_batch_result_member(jsonb),
    aap_jobs_v1.finish_batch_result_execution(jsonb),
    aap_jobs_v1.start_batch_result_member(jsonb),
    aap_labeling_v1.admit_prediction_retry(jsonb),
    aap_labeling_v1.read_prediction_retry(jsonb),
    aap_labeling_v1.finish_prediction_retry_write(jsonb),
    aap_labeling_v1.finish_prediction_retry_failure(jsonb),
    aap_jobs_v1.claim_prediction_retry_deliveries(jsonb),
    aap_jobs_v1.finish_prediction_retry_delivery(jsonb),
    aap_labeling_v1.accept_dataset_task_creation(uuid, uuid, uuid, uuid, uuid, uuid, text, integer),
    aap_labeling_v1.prepare_dataset_task_creation(uuid, jsonb),
    aap_labeling_v1.read_dataset_task_creation(uuid),
    aap_labeling_v1.advance_dataset_task_creation(uuid, integer),
    aap_jobs_v1.create_task_creation_job(uuid, uuid, uuid, uuid, uuid),
    aap_jobs_v1.find_task_creation_job(uuid, uuid, uuid, uuid),
    aap_jobs_v1.claim_task_creation_job(uuid, text),
    aap_jobs_v1.finish_task_creation_job(uuid, uuid),
    aap_jobs_v1.fail_task_creation_job(uuid, uuid, text),
    aap_jobs_v1.cancel_task_creation_job(uuid, uuid),
    aap_assets_v1.pending_storage_deletions(uuid, integer),
    aap_assets_v1.complete_storage_deletion(uuid, uuid)
    TO :"application_role";
-- Compose the existing Spatial native contract for default API/worker calls.
GRANT SELECT ON aap_assets_v1.spatial_item_references,
    aap_assets_v1.spatial_scene_references TO :"application_role";
GRANT EXECUTE ON FUNCTION
    aap_assets_v1.guard_spatial_actor(uuid,text),
    aap_assets_v1.list_spatial_scenes(uuid,uuid,timestamp with time zone,uuid,integer),
    aap_assets_v1.get_spatial_scene(uuid,uuid),
    aap_assets_v1.read_spatial_layout(uuid,uuid,uuid,uuid,integer),
    aap_assets_v1.read_spatial_items(uuid,uuid[],uuid,integer),
    aap_assets_v1.read_spatial_poses(uuid,uuid,integer,integer),
    aap_assets_v1.read_spatial_pose_pairs(jsonb),
    aap_assets_v1.read_spatial_calibrations(uuid[],boolean,uuid,integer,integer),
    aap_assets_v1.lock_spatial_calibration_facts(uuid,uuid,text,uuid,jsonb),
    aap_assets_v1.mutate_spatial_scene(uuid,uuid,uuid,jsonb,jsonb),
    aap_assets_v1.apply_spatial_layout(uuid,uuid,uuid,text,boolean,jsonb,jsonb),
    aap_assets_v1.read_spatial_layout_receipt(uuid,uuid,uuid,text,boolean),
    aap_assets_v1.upsert_spatial_poses(uuid,uuid,jsonb),
    aap_assets_v1.create_spatial_scene(uuid,uuid,uuid,text,text,jsonb,uuid),
    aap_assets_v1.assign_spatial_items(uuid,uuid,jsonb),
    aap_assets_v1.set_spatial_scene_source(uuid,uuid,text,jsonb),
    aap_assets_v1.update_spatial_calibration(uuid,uuid,uuid,text,integer,text,jsonb,text,text,jsonb,jsonb),
    aap_assets_v1.read_pointcloud_preparation(uuid,uuid,integer),
    aap_assets_v1.apply_pointcloud_preparation(uuid,jsonb,jsonb,jsonb),
    aap_assets_v1.read_spatial_camera_target(uuid,uuid,text),
    aap_assets_v1.record_spatial_calibration_provenance(uuid,uuid,jsonb),
    aap_assets_v1.set_spatial_dataset_axis(uuid,uuid,text)
    TO :"application_role";

-- The monolithic API/Celery runtime composes retained video input capabilities
-- locally. Keep this bounded to workflow, control, worker and labeling calls;
-- system sweep and video-input garbage operations stay with their owners.
GRANT EXECUTE ON FUNCTION
    aap_assets_v1.capture_accepted_video_manifest(uuid, uuid, uuid, jsonb),
    aap_assets_v1.read_accepted_video_manifest(uuid, uuid, uuid),
    aap_assets_v1.read_accepted_video_manifest_page(uuid, uuid, uuid, text, text, bigint, integer),
    aap_assets_v1.prepare_accepted_video_member(uuid, uuid, uuid, text, bigint, uuid, text, bigint, jsonb, text),
    aap_assets_v1.seal_accepted_video_manifest(uuid, uuid, uuid, text),
    aap_assets_v1.read_accepted_video_input(uuid, uuid, uuid, uuid),
    aap_assets_v1.read_accepted_video_input_work(uuid, uuid, uuid, uuid),
    aap_assets_v1.read_accepted_video_times(uuid, uuid, uuid, uuid, text, bigint, integer),
    aap_assets_v1.require_accepted_video_frame(uuid, uuid, uuid, uuid, text, bigint),
    aap_assets_v1.read_accepted_video_frame(uuid, uuid, uuid, uuid, text, bigint),
    aap_assets_v1.claim_accepted_video_input(uuid, uuid, integer),
    aap_assets_v1.heartbeat_accepted_video_input(uuid, bigint, uuid, integer),
    aap_assets_v1.stage_accepted_video_times(uuid, bigint, uuid, bigint, jsonb),
    aap_assets_v1.finish_accepted_video_input(uuid, bigint, uuid, jsonb),
    aap_assets_v1.fail_accepted_video_input(uuid, bigint, uuid, text),
    aap_assets_v1.claim_accepted_video_frame(uuid, uuid, integer),
    aap_assets_v1.heartbeat_accepted_video_frame(uuid, bigint, uuid, integer),
    aap_assets_v1.finish_accepted_video_frame(uuid, bigint, uuid, jsonb),
    aap_assets_v1.fail_accepted_video_frame(uuid, bigint, uuid, text),
    aap_assets_v1.lock_accepted_video_frame(uuid, uuid, uuid, uuid, text, bigint, jsonb, jsonb),
    aap_assets_v1.lock_accepted_video_input(uuid, uuid, uuid, uuid, text),
    aap_assets_v1._lock_accepted_video_input_pin(uuid, uuid, jsonb, uuid),
    aap_labeling_v1.attach_annotation_video_input_source(uuid, uuid, jsonb),
    aap_labeling_v1.copy_annotation_video_input_sources(uuid, uuid),
    aap_labeling_v1.accepted_video_input_referenced(uuid)
    TO :"application_role";

-- The monolithic API/Celery runtime still composes the image media owner
-- locally. Preserve its published image runtime surface alongside video.
GRANT EXECUTE ON FUNCTION
    aap_assets_v1.read_image_source(uuid,uuid,uuid),
    aap_assets_v1.observe_image_source(uuid,uuid,uuid,text,jsonb),
    aap_assets_v1.read_image_summaries(uuid,jsonb,text),
    aap_assets_v1.prepare_image_media(uuid,uuid,uuid,uuid,text,text,jsonb,boolean),
    aap_assets_v1.read_image_generation(uuid,uuid,uuid,integer,text),
    aap_assets_v1.read_image_work(uuid,uuid),
    aap_assets_v1.retry_image_media(uuid,uuid,uuid,uuid,text,text,jsonb,boolean),
    aap_assets_v1.invalidate_image_media(uuid,uuid,uuid,jsonb),
    aap_assets_v1.cancel_image_media(uuid,uuid,uuid),
    aap_assets_v1.resolve_internal_image_source(text,uuid,text,jsonb),
    aap_assets_v1.schedule_image_media(uuid,text,uuid,jsonb,boolean),
    aap_assets_v1.read_internal_image_work(uuid),
    aap_assets_v1.scan_image_backfill(jsonb,text,uuid,integer),
    aap_assets_v1.read_image_usage(),
    aap_assets_v1.claim_image_media(uuid,uuid,integer,text[]),
    aap_assets_v1.heartbeat_image_media(uuid,bigint,uuid,integer),
    aap_assets_v1.finish_image_media(uuid,bigint,uuid,jsonb),
    aap_assets_v1.fail_image_media(uuid,bigint,uuid,text,text),
    aap_assets_v1.advance_image_backfill(uuid,bigint,uuid,integer,jsonb),
    aap_assets_v1.reconcile_image_media(jsonb,integer),
    aap_assets_v1.claim_image_garbage(uuid,integer,integer),
    aap_assets_v1.complete_image_garbage(uuid,uuid)
    TO :"application_role";

-- Retained accepted-image inputs and manifests are used by the legacy
-- API/Celery adapter. Keep their public workflow/control/worker/Labeling calls
-- here; input sweep, input garbage and private receipt/pin helpers stay owner-only.
GRANT EXECUTE ON FUNCTION
    aap_assets_v1.find_accepted_image_input(uuid,uuid,uuid),
    aap_assets_v1.reserve_accepted_image_input(uuid,uuid,uuid,text,jsonb),
    aap_assets_v1.read_accepted_image_input(uuid,uuid,uuid),
    aap_assets_v1.read_accepted_image_input_work(uuid,uuid,uuid),
    aap_assets_v1.claim_accepted_image_input(uuid,uuid,integer),
    aap_assets_v1.heartbeat_accepted_image_input(uuid,bigint,uuid,integer),
    aap_assets_v1.finish_accepted_image_input(uuid,bigint,uuid,jsonb),
    aap_assets_v1.fail_accepted_image_input(uuid,bigint,uuid,text),
    aap_assets_v1.lock_accepted_image_input(uuid,uuid,uuid),
    aap_assets_v1.capture_accepted_image_input_manifest(uuid,uuid,uuid,jsonb),
    aap_assets_v1.read_accepted_image_input_manifest(uuid,uuid,uuid),
    aap_assets_v1.read_accepted_image_input_manifest_page(uuid,uuid,uuid,text,text,bigint,integer),
    aap_assets_v1.prepare_accepted_image_input_manifest_member(uuid,uuid,uuid,text,bigint,uuid,text,jsonb,text),
    aap_assets_v1.seal_accepted_image_input_manifest(uuid,uuid,uuid,text),
    aap_labeling_v1.attach_annotation_input_source(uuid,uuid,text),
    aap_labeling_v1.copy_annotation_input_sources(uuid,uuid)
    TO :"application_role";

REVOKE ALL ON SCHEMA aap_assets_v1 FROM :"asset_cleanup_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_assets_v1 FROM :"asset_cleanup_role";
REVOKE ALL ON TABLE public.asset_storage_deletions FROM :"asset_cleanup_role";
GRANT USAGE ON SCHEMA aap_assets_v1 TO :"asset_cleanup_role";
REVOKE ALL ON FUNCTION aap_assets_v1.pending_storage_deletions(uuid, integer)
    FROM :"asset_cleanup_role";
GRANT EXECUTE ON FUNCTION aap_assets_v1.pending_storage_deletions_v2(
    text, timestamptz, uuid, uuid, integer
),
    aap_assets_v1.complete_storage_deletion(uuid, uuid)
    TO :"asset_cleanup_role";
REVOKE ALL ON SCHEMA public, aap_identity_v1, aap_identity_directory_v1,
    aap_workspace_v1,
    aap_labeling_v1, aap_jobs_v1, aap_learning_v1, aap_inference_v1,
    aap_insights_v1, aap_diagnostics_v1, aap_export_v1, aap_audit_v1
    FROM :"asset_cleanup_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_identity_v1, aap_identity_directory_v1,
    aap_workspace_v1,
    aap_labeling_v1, aap_jobs_v1, aap_learning_v1, aap_inference_v1,
    aap_insights_v1, aap_diagnostics_v1, aap_export_v1, aap_audit_v1
    FROM :"asset_cleanup_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"asset_cleanup_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"asset_cleanup_role";
GRANT USAGE ON SCHEMA aap_assets_v1 TO :"asset_cleanup_role";
GRANT EXECUTE ON FUNCTION aap_assets_v1.pending_storage_deletions_v2(
    text, timestamptz, uuid, uuid, integer
),
    aap_assets_v1.complete_storage_deletion(uuid, uuid),
    aap_assets_v1.claim_image_garbage(uuid,integer,integer),
    aap_assets_v1.complete_image_garbage(uuid,uuid),
    aap_assets_v1.claim_accepted_input_garbage(uuid,integer,integer),
    aap_assets_v1.complete_accepted_input_garbage(uuid,uuid),
    aap_assets_v1.claim_accepted_video_input_garbage(uuid,integer,integer),
    aap_assets_v1.complete_accepted_video_input_garbage(uuid,uuid)
    TO :"asset_cleanup_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_role" IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"application_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_role" IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO :"application_role";

-- Existing maintenance tasks create/drop monthly partitions and refresh these
-- two materialized views via the normal runtime connection. Ownership is limited
-- to these objects; the migration role inherits the runtime role, never vice versa.
DO $$
DECLARE
    obj record;
BEGIN
    FOR obj IN
        SELECT relid FROM pg_partition_tree('public.audit_logs')
        UNION
        SELECT relid FROM pg_partition_tree('public.predictions')
    LOOP
        EXECUTE format('ALTER TABLE %s OWNER TO %I', obj.relid::regclass,
                       current_setting('aap.application_role'));
    END LOOP;
    EXECUTE format('ALTER MATERIALIZED VIEW public.mv_user_perf_daily OWNER TO %I',
                   current_setting('aap.application_role'));
END $$;

REVOKE INSERT, UPDATE, DELETE ON TABLE public.alembic_version FROM :"application_role";
REVOKE DELETE ON TABLE public.gpu_backend_memberships, public.gpu_backend_fences
    FROM :"application_role";

DO $$
BEGIN
    IF has_table_privilege(current_setting('aap.application_role'),
                           'public.gpu_backend_memberships', 'DELETE')
       OR has_table_privilege(current_setting('aap.application_role'),
                              'public.gpu_backend_fences', 'DELETE') THEN
        RAISE EXCEPTION 'The runtime role still has effective GPU tombstone DELETE privileges';
    END IF;
END $$;

DO $$
DECLARE
    relation oid;
    cleanup_functions oid[] := ARRAY[
        'aap_assets_v1.pending_storage_deletions_v2(text,timestamptz,uuid,uuid,integer)'::regprocedure::oid,
        'aap_assets_v1.complete_storage_deletion(uuid,uuid)'::regprocedure::oid,
        'aap_assets_v1.claim_image_garbage(uuid,integer,integer)'::regprocedure::oid,
        'aap_assets_v1.complete_image_garbage(uuid,uuid)'::regprocedure::oid,
        'aap_assets_v1.claim_accepted_input_garbage(uuid,integer,integer)'::regprocedure::oid,
        'aap_assets_v1.complete_accepted_input_garbage(uuid,uuid)'::regprocedure::oid,
        'aap_assets_v1.claim_accepted_video_input_garbage(uuid,integer,integer)'::regprocedure::oid,
        'aap_assets_v1.complete_accepted_video_input_garbage(uuid,uuid)'::regprocedure::oid
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
        IF has_any_column_privilege(current_setting('aap.asset_cleanup_role'),
                                    relation, 'SELECT,INSERT,UPDATE,REFERENCES')
           OR has_table_privilege(current_setting('aap.asset_cleanup_role'),
                                  relation,
                                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') THEN
            RAISE EXCEPTION 'The Assets cleanup role has direct table access';
        END IF;
    END LOOP;

    IF NOT has_schema_privilege(current_setting('aap.asset_cleanup_role'),
                                'aap_assets_v1', 'USAGE')
       OR has_database_privilege(current_setting('aap.asset_cleanup_role'),
                                 current_database(), 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'),
                               'public', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'),
                               'aap_assets_v1', 'CREATE')
       OR NOT has_function_privilege(current_setting('aap.asset_cleanup_role'),
                                     'aap_assets_v1.pending_storage_deletions_v2(text,timestamptz,uuid,uuid,integer)',
                                     'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.asset_cleanup_role'),
                                     'aap_assets_v1.complete_storage_deletion(uuid,uuid)',
                                     'EXECUTE')
       OR has_function_privilege(current_setting('aap.asset_cleanup_role'),
                                 'aap_assets_v1.pending_storage_deletions(uuid,integer)',
                                 'EXECUTE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'),
                               'aap_identity_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'),
                               'aap_workspace_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'),
                               'aap_labeling_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'),
                               'aap_jobs_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'),
                               'aap_learning_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'),
                               'aap_inference_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'),
                               'aap_insights_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'),
                               'aap_diagnostics_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'),
                               'aap_export_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'),
                               'aap_audit_v1', 'USAGE') THEN
        RAISE EXCEPTION 'The Assets cleanup role has access outside Assets';
    END IF;

    IF (SELECT count(*) FROM unnest(cleanup_functions) AS function(oid)
        WHERE has_function_privilege(current_setting('aap.asset_cleanup_role'),function.oid,'EXECUTE')) <> 8 THEN
        RAISE EXCEPTION 'The Assets cleanup role is missing an approved cleanup function';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS proc
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = proc.pronamespace
        WHERE pg_catalog.left(namespace.nspname, 4) = 'aap_'
          AND pg_catalog.right(namespace.nspname, 3) = '_v1'
          AND NOT proc.oid = ANY(cleanup_functions)
          AND pg_catalog.has_function_privilege(
              current_setting('aap.asset_cleanup_role'), proc.oid, 'EXECUTE'
          )
    ) THEN
        RAISE EXCEPTION 'The Assets cleanup role can execute outside its eight cleanup functions';
    END IF;
END $$;
-- Preserve embedded command capabilities while denying direct Identity storage.
DO $identity_private_acl$
DECLARE
    v_runtime name := current_setting('aap.application_role');
    v_relation record;
    v_columns text;
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
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
                n.nspname ~ '^aap_identity(_[a-z0-9]+)*_v[0-9]+$'
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
COMMIT;
