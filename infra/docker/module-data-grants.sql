-- Run after migrations as the schema owner. Roles must already exist.
-- psql -v expected_database=annotation_test -v migration_role=owner
--   -v identity_owner=identity_code -v workspace_owner=workspace_code
--   -v assets_owner=assets_code -v labeling_owner=labeling_code
--   -v jobs_owner=jobs_code -v learning_owner=learning_code
--   -v inference_owner=inference_code -v insights_owner=insights_code
--   -v diagnostics_owner=diagnostics_code -v export_owner=export_code
--   -v audit_owner=audit_code -v settings_owner=settings_code
--   -v notifications_owner=notifications_code -v workflow_role=append_client
--   -v asset_cleanup_role=asset_cleanup_client
--   -v mask_gc_role=labeling_mask_gc_client
--   -v inference_registry_ingress_role=inference_registry_ingress_client
--   -v inference_routing_role=inference_routing_client
--   -f infra/docker/module-data-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.identity_owner', :'identity_owner', true);
SELECT set_config('aap.workspace_owner', :'workspace_owner', true);
SELECT set_config('aap.assets_owner', :'assets_owner', true);
SELECT set_config('aap.labeling_owner', :'labeling_owner', true);
SELECT set_config('aap.jobs_owner', :'jobs_owner', true);
SELECT set_config('aap.learning_owner', :'learning_owner', true);
SELECT set_config('aap.inference_owner', :'inference_owner', true);
SELECT set_config('aap.insights_owner', :'insights_owner', true);
SELECT set_config('aap.diagnostics_owner', :'diagnostics_owner', true);
SELECT set_config('aap.export_owner', :'export_owner', true);
SELECT set_config('aap.audit_owner', :'audit_owner', true);
SELECT set_config('aap.settings_owner', :'settings_owner', true);
SELECT set_config('aap.notifications_owner', :'notifications_owner', true);
SELECT set_config('aap.workflow_role', :'workflow_role', true);
SELECT set_config('aap.asset_cleanup_role', :'asset_cleanup_role', true);
SELECT set_config('aap.mask_gc_role', :'mask_gc_role', true);
SELECT set_config('aap.inference_registry_ingress_role', :'inference_registry_ingress_role', true);
SELECT set_config('aap.inference_routing_role', :'inference_routing_role', true);

DO $$
DECLARE
    role_name text;
    role_names text[] := ARRAY[
        current_setting('aap.identity_owner'),
        current_setting('aap.workspace_owner'),
        current_setting('aap.assets_owner'),
        current_setting('aap.labeling_owner'),
        current_setting('aap.jobs_owner'),
        current_setting('aap.learning_owner'),
        current_setting('aap.inference_owner'),
        current_setting('aap.insights_owner'),
        current_setting('aap.diagnostics_owner'),
        current_setting('aap.export_owner'),
        current_setting('aap.audit_owner'),
        current_setting('aap.settings_owner'),
        current_setting('aap.notifications_owner'),
        current_setting('aap.workflow_role'),
        current_setting('aap.asset_cleanup_role'),
        current_setting('aap.mask_gc_role'),
        current_setting('aap.inference_registry_ingress_role'),
        current_setting('aap.inference_routing_role')
    ];
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF (SELECT count(DISTINCT n) FROM unnest(role_names) AS n) <> 18 THEN
        RAISE EXCEPTION 'Module owners and caller must be distinct roles';
    END IF;
    FOREACH role_name IN ARRAY role_names LOOP
        IF NOT EXISTS (
            SELECT FROM pg_roles WHERE rolname = role_name
              AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
              AND rolcanlogin = (role_name IN (
                  current_setting('aap.workflow_role'),
                  current_setting('aap.asset_cleanup_role'),
                  current_setting('aap.mask_gc_role'),
                  current_setting('aap.inference_registry_ingress_role'),
                  current_setting('aap.inference_routing_role')
              ))
        ) OR pg_has_role(role_name, current_user, 'MEMBER') THEN
            RAISE EXCEPTION 'Unsafe or missing module role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT FROM pg_auth_members AS m
            WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = role_name)
               OR (m.roleid = (SELECT oid FROM pg_roles WHERE rolname = role_name)
                   AND m.member <> current_user::pg_catalog.regrole)
        ) THEN
            RAISE EXCEPTION 'Module roles must not have untrusted memberships: %', role_name;
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT FROM pg_auth_members AS membership
        WHERE membership.roleid = (
            SELECT oid FROM pg_roles
            WHERE rolname = current_setting('aap.asset_cleanup_role')
        )
    ) THEN
        RAISE EXCEPTION 'The Assets cleanup role must not have inheriting members';
    END IF;
    IF EXISTS (
        SELECT FROM pg_auth_members AS membership
        WHERE membership.roleid = (
            SELECT oid FROM pg_roles
            WHERE rolname = current_setting('aap.mask_gc_role')
        )
    ) THEN
        RAISE EXCEPTION 'The Labeling Mask GC role must not have inheriting members';
    END IF;
END $$;

REVOKE ALL ON SCHEMA aap_identity_v1, aap_identity_keys_v1, aap_identity_sessions_v1, aap_identity_accounts_v1, aap_identity_recovery_v1, aap_identity_groups_v1, aap_identity_deactivation_v1, aap_identity_invitations_v1, aap_identity_mail_v2, aap_identity_directory_v1, aap_identity_admin_passwords_v1, aap_identity_platform_roles_v1, aap_identity_suspensions_v1, aap_identity_deletions_v1, aap_identity_offboarding_v1, aap_identity_briefs_v1, aap_workspace_owner_transfer_v1, aap_workspace_configuration_v1, aap_workspace_membership_v1, aap_workspace_template_query_v1, aap_workspace_project_query_v1, aap_workspace_directory_query_v1, aap_workspace_v1, aap_assets_v1,
    aap_labeling_class_query_v1, aap_labeling_v1, aap_jobs_v1, aap_learning_v1, aap_inference_v1, aap_inference_registry_ingress_v1, aap_inference_routing_v1,
    aap_insights_v1, aap_diagnostics_v1, aap_export_v1, aap_audit_v1, aap_settings_v1, aap_notifications_v1 FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_identity_v1, aap_workspace_template_query_v1, aap_workspace_project_query_v1, aap_workspace_directory_query_v1, aap_workspace_v1,
    aap_identity_keys_v1, aap_identity_sessions_v1, aap_identity_accounts_v1, aap_identity_recovery_v1, aap_identity_groups_v1, aap_identity_deactivation_v1, aap_identity_invitations_v1, aap_identity_mail_v2, aap_identity_directory_v1, aap_identity_admin_passwords_v1, aap_identity_platform_roles_v1, aap_identity_suspensions_v1, aap_identity_deletions_v1, aap_identity_offboarding_v1, aap_identity_briefs_v1, aap_workspace_owner_transfer_v1, aap_workspace_configuration_v1, aap_workspace_membership_v1, aap_assets_v1, aap_labeling_class_query_v1, aap_labeling_v1, aap_jobs_v1, aap_learning_v1,
    aap_inference_v1, aap_inference_registry_ingress_v1, aap_inference_routing_v1, aap_insights_v1, aap_diagnostics_v1, aap_export_v1,
    aap_audit_v1, aap_settings_v1, aap_notifications_v1 FROM PUBLIC;
REVOKE ALL ON SCHEMA aap_identity_v1, aap_identity_keys_v1, aap_identity_sessions_v1, aap_identity_accounts_v1, aap_identity_recovery_v1, aap_identity_groups_v1, aap_identity_deactivation_v1, aap_identity_invitations_v1, aap_identity_mail_v2, aap_identity_directory_v1, aap_identity_admin_passwords_v1, aap_identity_platform_roles_v1, aap_identity_suspensions_v1, aap_identity_deletions_v1, aap_identity_offboarding_v1, aap_identity_briefs_v1, aap_workspace_template_query_v1, aap_workspace_project_query_v1, aap_workspace_directory_query_v1, aap_workspace_v1, aap_assets_v1,
    aap_labeling_class_query_v1, aap_labeling_v1, aap_jobs_v1, aap_learning_v1, aap_inference_v1, aap_inference_registry_ingress_v1, aap_inference_routing_v1,
    aap_insights_v1, aap_diagnostics_v1, aap_export_v1, aap_audit_v1, aap_settings_v1, aap_notifications_v1
    FROM :"asset_cleanup_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_identity_v1, aap_workspace_template_query_v1, aap_workspace_project_query_v1, aap_workspace_directory_query_v1, aap_workspace_v1,
    aap_identity_keys_v1, aap_identity_sessions_v1, aap_identity_accounts_v1, aap_identity_recovery_v1, aap_identity_groups_v1, aap_identity_deactivation_v1, aap_identity_invitations_v1, aap_identity_mail_v2, aap_identity_directory_v1, aap_identity_admin_passwords_v1, aap_identity_platform_roles_v1, aap_identity_suspensions_v1, aap_identity_deletions_v1, aap_identity_offboarding_v1, aap_identity_briefs_v1, aap_assets_v1, aap_labeling_class_query_v1, aap_labeling_v1, aap_jobs_v1, aap_learning_v1,
    aap_inference_v1, aap_inference_registry_ingress_v1, aap_inference_routing_v1, aap_insights_v1, aap_diagnostics_v1, aap_export_v1,
    aap_audit_v1 FROM :"asset_cleanup_role";
REVOKE ALL ON SCHEMA aap_identity_v1, aap_identity_keys_v1, aap_identity_sessions_v1, aap_identity_accounts_v1, aap_identity_recovery_v1, aap_identity_groups_v1, aap_identity_deactivation_v1, aap_identity_invitations_v1, aap_identity_mail_v2, aap_identity_directory_v1, aap_identity_admin_passwords_v1, aap_identity_platform_roles_v1, aap_identity_suspensions_v1, aap_identity_deletions_v1, aap_identity_offboarding_v1, aap_identity_briefs_v1, aap_workspace_template_query_v1, aap_workspace_project_query_v1, aap_workspace_directory_query_v1, aap_workspace_v1, aap_assets_v1,
    aap_labeling_class_query_v1, aap_labeling_v1, aap_jobs_v1, aap_learning_v1, aap_inference_v1, aap_inference_registry_ingress_v1, aap_inference_routing_v1,
    aap_insights_v1, aap_diagnostics_v1, aap_export_v1, aap_audit_v1,
    aap_settings_v1, aap_notifications_v1 FROM :"mask_gc_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_identity_v1, aap_workspace_template_query_v1, aap_workspace_project_query_v1, aap_workspace_directory_query_v1, aap_workspace_v1,
    aap_identity_keys_v1, aap_identity_sessions_v1, aap_identity_accounts_v1, aap_identity_recovery_v1, aap_identity_groups_v1, aap_identity_deactivation_v1, aap_identity_invitations_v1, aap_identity_mail_v2, aap_identity_directory_v1, aap_identity_admin_passwords_v1, aap_identity_platform_roles_v1, aap_identity_suspensions_v1, aap_identity_deletions_v1, aap_identity_offboarding_v1, aap_identity_briefs_v1, aap_assets_v1, aap_labeling_class_query_v1, aap_labeling_v1, aap_jobs_v1, aap_learning_v1,
    aap_inference_v1, aap_inference_registry_ingress_v1, aap_inference_routing_v1, aap_insights_v1, aap_diagnostics_v1, aap_export_v1,
    aap_audit_v1, aap_settings_v1, aap_notifications_v1 FROM :"mask_gc_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"workflow_role";
REVOKE ALL ON DATABASE :"expected_database" FROM :"asset_cleanup_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"asset_cleanup_role";
REVOKE ALL ON DATABASE :"expected_database" FROM :"mask_gc_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"mask_gc_role";
REVOKE ALL ON SCHEMA public FROM :"asset_cleanup_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"asset_cleanup_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"asset_cleanup_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM :"asset_cleanup_role";
REVOKE ALL ON SCHEMA public FROM :"mask_gc_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"mask_gc_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"mask_gc_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM :"mask_gc_role";
GRANT USAGE ON SCHEMA public TO :"identity_owner", :"workspace_owner",
    :"assets_owner", :"labeling_owner", :"jobs_owner", :"learning_owner",
    :"inference_owner", :"insights_owner", :"diagnostics_owner",
    :"export_owner", :"audit_owner", :"settings_owner", :"notifications_owner";

-- Identity owns current authentication facts and the bounded self-account
-- projection needed while it retains each User row lock through the transaction.
GRANT SELECT (
    id, name, email, role, group_name, group_id, is_active, credential_generation,
    disabled_kind, disabled_at, disabled_by, disabled_reason, email_verified_at,
    last_login_at, last_seen_at, password_admin_reset_at, deactivation_requested_at,
    deactivation_reason,
    deactivation_scheduled_at, preferences, avatar_ref, password_hash, status,
    created_at, weekly_target_default
), UPDATE (
    id, credential_generation, last_login_at, last_seen_at, status, role, name, preferences,
    password_hash, password_admin_reset_at, avatar_ref, email_verified_at,
    group_id, group_name, is_active, disabled_kind, disabled_at, disabled_by,
    disabled_reason, deactivation_requested_at, deactivation_reason,
    deactivation_scheduled_at
)
    ON public.users TO :"identity_owner";
GRANT SELECT (organization_id, user_id, deleted_at)
    ON public.organization_members TO :"identity_owner";
GRANT INSERT (
    id, email, name, password_hash, credential_generation, role, group_name,
    group_id, status, is_active, email_verified_at, preferences
) ON public.users TO :"identity_owner";
GRANT SELECT (
    id, user_id, name, key_prefix, key_hash, scopes, expires_at, last_used_at,
    revoked_at, created_at
), INSERT (id, user_id, name, key_prefix, key_hash, scopes, expires_at),
    UPDATE (name, key_prefix, key_hash, scopes, expires_at, last_used_at, revoked_at)
    ON public.api_keys TO :"identity_owner";
GRANT SELECT (id, user_id, token_hash, issued_email, expires_at, used_at),
    INSERT (id, user_id, token_hash, issued_email, expires_at), UPDATE (used_at)
    ON public.password_reset_tokens TO :"identity_owner";
GRANT SELECT (id, user_id, token_hash, issued_email, expires_at, used_at),
    INSERT (id, user_id, token_hash, issued_email, expires_at), UPDATE (used_at)
    ON public.email_verification_tokens TO :"identity_owner";
GRANT SELECT, INSERT, UPDATE, DELETE
    ON public.identity_revoked_jtis TO :"identity_owner";
GRANT SELECT (
    operation_id, actor_id, operation, payload, derivation_id, result, created_at
), INSERT (operation_id, actor_id, operation, payload, derivation_id, result)
    ON public.identity_key_operation_receipts TO :"identity_owner";
GRANT SELECT (
    operation_id, actor_id, operation, request_scope, result, audit_id, created_at
), INSERT (
    operation_id, actor_id, operation, request_scope, result, audit_id
)
    ON public.identity_session_operation_receipts TO :"identity_owner";
GRANT SELECT (
    operation_id, actor_id, operation, request_scope, result, audit_id, created_at
), INSERT (
    operation_id, actor_id, operation, request_scope, result, audit_id
)
    ON public.identity_account_operation_receipts TO :"identity_owner";
GRANT SELECT (
    operation_id, operation, actor_id, request_fingerprint, derivation_id,
    result, audit_id, created_at
), INSERT (
    operation_id, operation, actor_id, request_fingerprint, derivation_id,
    result, audit_id
)
    ON public.identity_recovery_operation_receipts TO :"identity_owner";
GRANT SELECT (
    operation_id, actor_id, operation, request_fingerprint, derivation_id,
    result, created_at
), INSERT (
    operation_id, actor_id, operation, request_fingerprint, derivation_id, result
)
    ON public.identity_group_operation_receipts TO :"identity_owner";
GRANT SELECT (
    operation_id, actor_id, operation, request_fingerprint, derivation_id,
    result, audit_id, created_at
), INSERT (
    operation_id, actor_id, operation, request_fingerprint, derivation_id,
    result, audit_id
)
    ON public.identity_deactivation_operation_receipts TO :"identity_owner";
GRANT SELECT (
    operation_id, protocol, requested_limit, processed, examined,
    blocked_last_super_admin, has_more, created_at
), INSERT (
    operation_id, protocol, requested_limit, processed, examined,
    blocked_last_super_admin, has_more
)
    ON public.identity_deactivation_due_receipts TO :"identity_owner";
GRANT SELECT (
    request_id, user_id, requested_at, scheduled_at, state, origin,
    created_at, updated_at
), INSERT (
    request_id, user_id, requested_at, scheduled_at, state, origin,
    created_at, updated_at
), UPDATE (state, updated_at)
    ON public.identity_deactivation_requests TO :"identity_owner";
GRANT SELECT, INSERT, UPDATE, DELETE ON public.groups TO :"identity_owner";
GRANT SELECT (
    id, email, role, project_role, group_name, project_id, token, expires_at,
    invited_by, accepted_at, accepted_user_id, revoked_at, created_at
), INSERT (
    id, email, role, project_role, group_name, project_id, token, expires_at, invited_by
), UPDATE (
    role, project_role, group_name, token, expires_at,
    accepted_at, accepted_user_id, revoked_at
) ON public.user_invitations TO :"identity_owner";
GRANT SELECT (
    operation_id, actor_id, operation, request_fingerprint,
    derivation_id, result, audit_id, created_at
), INSERT (
    operation_id, actor_id, operation, request_fingerprint,
    derivation_id, result, audit_id
) ON public.identity_invitation_operation_receipts TO :"identity_owner";
GRANT SELECT (
    operation_id, actor_id, operation, request_fingerprint, derivation_id,
    result, audit_id, created_at
), INSERT (
    operation_id, actor_id, operation, request_fingerprint, derivation_id,
    result, audit_id
) ON public.identity_directory_export_receipts TO :"identity_owner";
GRANT SELECT (
    operation_id, actor_id, target_user_id, operation, request_fingerprint,
    derivation_id, target_email, password_admin_reset_at,
    password_state_fingerprint, audit_id, created_at
), INSERT (
    operation_id, actor_id, target_user_id, operation, request_fingerprint,
    derivation_id, target_email, password_admin_reset_at,
    password_state_fingerprint, audit_id
) ON public.identity_admin_password_reset_receipts TO :"identity_owner";
GRANT SELECT (
    operation_id, actor_id, target_user_id, operation, request_fingerprint,
    derivation_id, result
), INSERT (
    operation_id, actor_id, target_user_id, operation, request_fingerprint,
    derivation_id, result, audit_id
) ON public.identity_platform_role_operation_receipts TO :"identity_owner";
GRANT SELECT (
    operation_id, actor_id, operation, target_user_id, request_fingerprint,
    derivation_id, target_generation, target_is_active, result
), INSERT (
    operation_id, actor_id, operation, target_user_id, request_fingerprint,
    derivation_id, target_generation, target_is_active, result, audit_id
) ON public.identity_suspension_operation_receipts TO :"identity_owner";
GRANT SELECT (
    operation_id, actor_id, operation, target_user_id, transfer_to_user_id,
    request_fingerprint, derivation_id, target_generation, target_is_active, result
), INSERT (
    operation_id, actor_id, operation, target_user_id, transfer_to_user_id,
    request_fingerprint, derivation_id, target_generation, target_is_active,
    result, audit_id
) ON public.identity_deletion_operation_receipts TO :"identity_owner";
GRANT SELECT (
    operation_id, actor_id, operation, target_user_id, request_fingerprint,
    derivation_id, target_generation, target_is_active, result, scope_basis
), INSERT (
    operation_id, actor_id, operation, target_user_id, request_fingerprint,
    derivation_id, target_generation, target_is_active, result, scope_basis, audit_id
) ON public.identity_offboarding_operation_receipts TO :"identity_owner";
GRANT SELECT ON public.identity_credential_mail_outbox TO :"identity_owner";
GRANT INSERT (
    id, actor_id, challenge_id, purpose, email, frontend_base_url,
    token_hash, token_ciphertext, encryption_id, expires_at,
    invitation_id, invitation_context
) ON public.identity_credential_mail_outbox TO :"identity_owner";
GRANT UPDATE (
    status, attempt, available_at, worker_id, lease_token, lease_seconds,
    lease_expires_at, last_reason, updated_at
) ON public.identity_credential_mail_outbox TO :"identity_owner";
GRANT SELECT (id, owner_id, data_type, scene_mode, display_id, name), UPDATE (id),
    DELETE ON public.projects TO :"workspace_owner";
GRANT SELECT (
    id, organization_id, display_id, name, type_label, type_key, data_type,
    owner_id, status, ai_enabled, tool_bindings, label_config, sampling,
    maximum_annotations, show_overlap_first, iou_dedup_threshold, box_threshold,
    text_threshold, ai_interactive_enabled, raster_mask_native_editing_enabled,
    mask_qc_config, point_cloud_quality_config, rendering_config, video_sampling,
    video_collaboration, scene_mode, prefer_same_scene_continuation,
    scene_continuation_window_min, default_variants, preannotate_pipeline,
    annotation_guide, guide_assets, model_version, task_lock_ttl_seconds, due_date,
    created_at, updated_at
) ON public.projects TO :"workspace_owner";
GRANT UPDATE (owner_id, created_at, updated_at) ON public.projects TO :"workspace_owner";
GRANT UPDATE (guide_assets) ON public.projects TO :"workspace_owner";
GRANT SELECT (ml_backend_pool_id) ON public.projects TO :"workspace_owner";
GRANT INSERT (
    id, display_id, name, type_label, type_key, data_type, owner_id, status,
    ai_enabled, due_date, sampling, maximum_annotations, show_overlap_first,
    iou_dedup_threshold, box_threshold, text_threshold, ai_interactive_enabled,
    raster_mask_native_editing_enabled, mask_qc_config, point_cloud_quality_config,
    rendering_config, annotation_guide, guide_assets, video_sampling,
    video_collaboration, scene_mode, prefer_same_scene_continuation,
    scene_continuation_window_min, default_variants, preannotate_pipeline,
    tool_bindings, label_config
) ON public.projects TO :"workspace_owner";
GRANT UPDATE (
    name, type_label, type_key, data_type, status, ai_enabled, due_date,
    sampling, maximum_annotations, show_overlap_first, iou_dedup_threshold,
    box_threshold, text_threshold, ai_interactive_enabled,
    raster_mask_native_editing_enabled, mask_qc_config, point_cloud_quality_config,
    rendering_config, annotation_guide, guide_assets, video_sampling,
    video_collaboration, scene_mode, prefer_same_scene_continuation,
    scene_continuation_window_min, default_variants, preannotate_pipeline,
    tool_bindings, label_config, ml_backend_pool_id
) ON public.projects TO :"workspace_owner";
GRANT SELECT (
    id, display_id, name, description, type_label, type_key, data_type,
    tool_bindings, label_config, ai_enabled, sampling, maximum_annotations,
    show_overlap_first, iou_dedup_threshold, box_threshold, text_threshold,
    rendering_config, annotation_guide, scope, organization_id, created_by,
    source_project_id, usage_count, created_at, updated_at
), INSERT (
    id, display_id, name, description, type_label, type_key, data_type,
    tool_bindings, label_config, ai_enabled, sampling, maximum_annotations,
    show_overlap_first, iou_dedup_threshold, box_threshold, text_threshold,
    rendering_config, annotation_guide, scope, organization_id, created_by,
    source_project_id
), UPDATE (
    name, description, type_label, type_key, data_type, tool_bindings,
    label_config, ai_enabled, sampling, maximum_annotations, show_overlap_first,
    iou_dedup_threshold, box_threshold, text_threshold, rendering_config,
    annotation_guide, scope, organization_id, usage_count, updated_at
), DELETE ON public.project_templates TO :"workspace_owner";
GRANT USAGE ON SEQUENCE public.display_seq_projects,
    public.display_seq_project_templates TO :"workspace_owner";
GRANT SELECT, INSERT, UPDATE, DELETE
    ON public.workspace_configuration_operation_states,
       public.workspace_configuration_operation_attempts TO :"workspace_owner";
GRANT SELECT (
    operation_id, actor_id, operation, target_id, request_hash, result, selectors,
    frozen_scope, frozen_organization_id, frozen_creator_id, created_at
), INSERT (
    operation_id, actor_id, operation, target_id, request_hash, result, selectors,
    frozen_scope, frozen_organization_id, frozen_creator_id
) ON public.workspace_configuration_operation_receipts TO :"workspace_owner";
GRANT SELECT (id, project_id, user_id, role), INSERT (id, project_id, user_id, role, assigned_by),
    UPDATE (id), DELETE
    ON public.project_members TO :"workspace_owner";
GRANT SELECT (assigned_by, version, assigned_at, updated_at, weekly_target),
    UPDATE (role, assigned_by, version, assigned_at, updated_at)
    ON public.project_members TO :"workspace_owner";
GRANT SELECT (
    operation_id, actor_id, project_id, operation, request_hash, result, audit_id
), INSERT (
    operation_id, actor_id, project_id, operation, request_hash, result, audit_id
) ON public.workspace_membership_operation_receipts TO :"workspace_owner";
GRANT SELECT (
    operation_id, actor_id, project_id, new_owner_id, request_hash, result
), INSERT (
    operation_id, actor_id, project_id, new_owner_id, request_hash, result
) ON public.workspace_owner_transfer_operation_receipts TO :"workspace_owner";
GRANT SELECT (source_project_id), UPDATE (source_project_id)
    ON public.project_templates TO :"workspace_owner";
GRANT SELECT (operation_id, actor_id, project_id, request, result, created_at),
    INSERT (operation_id, actor_id, project_id, request, result), UPDATE (operation_id)
    ON public.project_deletion_operations TO :"workspace_owner";
GRANT SELECT (operation_id, actor_id, dataset_id, project_id, request, result, created_at),
    INSERT (operation_id, actor_id, dataset_id, project_id, request, result),
    UPDATE (operation_id) ON public.dataset_unlink_operations TO :"workspace_owner";
GRANT SELECT (
    id, actor_id, actor_email, actor_role, action, target_type, target_id,
    method, path, status_code, ip, detail_json, request_id, created_at
), INSERT (
    actor_id, actor_email, actor_role, action, target_type, target_id,
    method, path, status_code, ip, request_id, detail_json
), UPDATE (actor_email, actor_role) ON public.audit_logs TO :"audit_owner";
GRANT SELECT (id, email)
    ON aap_identity_v1.account_profiles TO :"audit_owner";
DO $$
BEGIN
    EXECUTE format(
        'GRANT USAGE ON SEQUENCE %s TO %I',
        pg_get_serial_sequence('public.audit_logs', 'id'),
        current_setting('aap.audit_owner')
    );
END $$;

-- Assets owns catalog/source rows and their exact upload, deletion and connection receipts.
GRANT SELECT (id, display_id, data_type, name, file_count),
    UPDATE (id, file_count, updated_at), DELETE
    ON public.datasets TO :"assets_owner";
GRANT SELECT (id, project_id, dataset_id, created_at),
    INSERT (id, project_id, dataset_id),
    UPDATE (id) ON public.project_datasets TO :"assets_owner";
GRANT DELETE ON public.project_datasets TO :"assets_owner";
GRANT SELECT (id, dataset_id, name, source_format, source_metadata, created_by),
    INSERT (id, display_id, dataset_id, name, source_format, source_metadata, created_by),
    UPDATE (id), DELETE
    ON public.scenes TO :"assets_owner";
GRANT USAGE ON SEQUENCE public.display_seq_scenes TO :"assets_owner";
GRANT SELECT, INSERT (
    id, dataset_id, file_name, file_path, file_type, file_size,
    content_hash, width, height, metadata
), UPDATE (id, file_size, content_hash, width, height, scene_id, frame_index, updated_at), DELETE
    ON public.dataset_items TO :"assets_owner";
GRANT SELECT (operation_id, actor_id, dataset_id, item_id, request, result, created_at),
    INSERT (operation_id, actor_id, dataset_id, item_id, request, result)
    ON public.dataset_legacy_upload_operations TO :"assets_owner";
GRANT SELECT ON public.dataset_upload_intents TO :"assets_owner";
GRANT INSERT (
    item_id, dataset_id, actor_id, file_name, file_type, content_type, staging_key
), UPDATE (file_path, publication_id, state, result, updated_at)
    ON public.dataset_upload_intents TO :"assets_owner";
GRANT SELECT (operation_id, actor_id, dataset_id, item_id, request, result, created_at),
    INSERT (operation_id, actor_id, dataset_id, item_id, request, result),
    UPDATE (operation_id) ON public.dataset_source_deletion_operations
    TO :"assets_owner";
GRANT SELECT (project_id), DELETE ON public.storage_connections TO :"assets_owner";
GRANT SELECT (id, name, kind, config, secret_enc, scope, created_by, updated_at, created_at, project_id),
    INSERT (id, name, kind, config, secret_enc, scope, project_id, created_by),
    UPDATE (id, name, config, secret_enc, updated_at)
    ON public.storage_connections TO :"assets_owner";
GRANT SELECT (
    operation_id, actor_id, action, connection_id, intent,
    connection_scope, connection_created_by, result, created_at
), INSERT (
    operation_id, actor_id, action, connection_id, intent,
    connection_scope, connection_created_by, result
) ON public.storage_connection_operations TO :"assets_owner";
GRANT SELECT (is_temporal) ON public.datasets TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE ON public.dataset_import_operations,
    public.dataset_import_entries, public.dataset_import_deliveries TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE ON public.dataset_source_ingest_operations,
    public.dataset_source_ingest_entries, public.dataset_source_ingest_deliveries
    TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE, DELETE ON public.image_pyramid_assets
    TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE ON public.image_pyramid_generations
    TO :"assets_owner";
GRANT SELECT (
    operation_id, asset_id, bucket, prefix, created_at, completed_at, not_before,
    retained
), INSERT (operation_id, asset_id, bucket, prefix, not_before, retained),
    UPDATE (completed_at, not_before, retained)
    ON public.asset_storage_deletions TO :"assets_owner";

-- Spatial state stays private to the Assets function owner.
GRANT SELECT (id, name, metadata, is_temporal) ON public.datasets TO :"assets_owner";
GRANT SELECT (description, created_by, created_at, updated_at),
    INSERT (id, display_id, name, description, data_type, is_temporal,
        file_count, created_by, metadata),
    UPDATE (metadata, is_temporal)
    ON public.datasets TO :"assets_owner";
GRANT SELECT (display_id, created_at, updated_at),
    UPDATE (name, source_format, source_metadata, updated_at)
    ON public.scenes TO :"assets_owner";
GRANT USAGE, SELECT ON SEQUENCE public.display_seq_scenes TO :"assets_owner";
GRANT UPDATE (metadata) ON public.dataset_items TO :"assets_owner";
GRANT UPDATE (
    media_source_generation, media_timetable_revision,
    media_materialized_generation, media_object_identity, media_source_drifted,
    thumbnail_path, blurhash, width, height
) ON public.dataset_items TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE ON public.video_frame_cache, public.video_chunks
    TO :"assets_owner";
GRANT SELECT, INSERT, DELETE ON public.video_frame_indices TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE ON public.asset_media_jobs TO :"assets_owner";
GRANT SELECT, INSERT ON public.asset_media_operations TO :"assets_owner";
GRANT SELECT, INSERT, DELETE ON public.asset_media_timetable_staging TO :"assets_owner";
GRANT SELECT, INSERT ON public.asset_media_garbage TO :"assets_owner";
GRANT UPDATE (
    lease_token, lease_expires_at, attempt_count, completed_at,
    not_before, last_ack_token
)
    ON public.asset_media_garbage TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE ON public.asset_image_jobs TO :"assets_owner";
GRANT SELECT, INSERT ON public.asset_image_operations TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE ON public.asset_image_garbage TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE ON public.asset_accepted_image_inputs,
    public.asset_accepted_image_input_attempts,
    public.asset_accepted_input_garbage TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE ON public.asset_accepted_image_input_manifests,
    public.asset_accepted_image_input_manifest_members TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE ON public.asset_accepted_video_manifests,
    public.asset_accepted_video_members, public.asset_accepted_video_inputs,
    public.asset_accepted_video_input_attempts, public.asset_accepted_video_frames,
    public.asset_accepted_video_frame_attempts TO :"assets_owner";
GRANT SELECT, INSERT ON public.asset_accepted_video_times TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE ON public.video_tracker_input_scopes TO :"labeling_owner";
GRANT SELECT, INSERT ON public.annotation_video_tracker_input_sources TO :"labeling_owner";
GRANT SELECT, INSERT, UPDATE ON public.scene_frame_poses TO :"assets_owner";
GRANT SELECT, INSERT ON public.sensor_calibration_revisions,
    public.dataset_spatial_operations TO :"assets_owner";
GRANT SELECT (operation_id, action, intent, result, created_at),
    INSERT (operation_id, action, intent, result)
    ON public.dataset_operator_operations TO :"assets_owner";

-- Cross-owner reads use versioned projections, never private foreign-table grants.
DO $$
DECLARE
    denied record;
BEGIN
    FOR denied IN
        SELECT * FROM (VALUES
            (current_setting('aap.assets_owner'), 'public.users'),
            (current_setting('aap.assets_owner'), 'public.system_settings'),
            (current_setting('aap.assets_owner'), 'public.notifications'),
            (current_setting('aap.assets_owner'), 'public.notification_preferences'),
            (current_setting('aap.jobs_owner'), 'public.users'),
            (current_setting('aap.jobs_owner'), 'public.notifications'),
            (current_setting('aap.jobs_owner'), 'public.system_settings'),
            (current_setting('aap.notifications_owner'), 'public.users'),
            (current_setting('aap.notifications_owner'), 'public.async_jobs'),
            (current_setting('aap.notifications_owner'), 'public.datasets'),
            (current_setting('aap.settings_owner'), 'public.users'),
            (current_setting('aap.settings_owner'), 'public.async_jobs'),
            (current_setting('aap.identity_owner'), 'public.projects'),
            (current_setting('aap.identity_owner'), 'public.project_members'),
            (current_setting('aap.identity_owner'), 'public.tasks'),
            (current_setting('aap.identity_owner'), 'public.task_batches'),
            (current_setting('aap.assets_owner'), 'public.projects'),
            (current_setting('aap.assets_owner'), 'public.project_members'),
            (current_setting('aap.assets_owner'), 'public.tasks'),
            (current_setting('aap.assets_owner'), 'public.task_batches'),
            (current_setting('aap.assets_owner'), 'public.task_dataset_item_links'),
            (current_setting('aap.assets_owner'), 'public.annotations'),
            (current_setting('aap.assets_owner'), 'public.video_tracker_jobs'),
            (current_setting('aap.assets_owner'), 'public.video_tracker_input_scopes'),
            (current_setting('aap.assets_owner'), 'public.annotation_video_tracker_input_sources'),
            (current_setting('aap.labeling_owner'), 'public.users'),
            (current_setting('aap.labeling_owner'), 'public.project_members'),
            (current_setting('aap.labeling_owner'), 'public.datasets'),
            (current_setting('aap.labeling_owner'), 'public.dataset_items'),
            (current_setting('aap.labeling_owner'), 'public.project_datasets'),
            (current_setting('aap.workspace_owner'), 'public.users'),
            (current_setting('aap.workspace_owner'), 'public.datasets'),
            (current_setting('aap.workspace_owner'), 'public.dataset_items'),
            (current_setting('aap.workspace_owner'), 'public.project_datasets'),
            (current_setting('aap.workspace_owner'), 'public.tasks')
        ) AS relation_scope(role_name, relation_name)
    LOOP
        IF has_table_privilege(
               denied.role_name, denied.relation_name,
               'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
           )
           OR has_any_column_privilege(
               denied.role_name, denied.relation_name, 'SELECT'
           )
           OR has_any_column_privilege(
               denied.role_name, denied.relation_name, 'INSERT'
           )
           OR has_any_column_privilege(
               denied.role_name, denied.relation_name, 'UPDATE'
           )
           OR has_any_column_privilege(
               denied.role_name, denied.relation_name, 'REFERENCES'
           ) THEN
            RAISE EXCEPTION 'Owner % has direct foreign-table access to %',
                denied.role_name, denied.relation_name;
        END IF;
    END LOOP;
END $$;

-- Labeling owns all task creation mutations, counter writes, and receipts.
GRANT SELECT (id, project_id, dataset_item_id, batch_id, status), UPDATE (id), DELETE
    ON public.tasks TO :"labeling_owner";
GRANT SELECT (
        id, project_id, dataset_id, display_id, annotator_id, reviewer_id,
        name, status, admin_locked, assigned_user_ids
    ),
    UPDATE (
        id, dataset_id, total_tasks, completed_tasks, review_tasks, updated_at,
        annotator_id, reviewer_id, assigned_user_ids
    ), DELETE
    ON public.task_batches TO :"labeling_owner";
GRANT SELECT (id, project_id, task_id), DELETE
    ON public.predictions TO :"labeling_owner";
GRANT SELECT (object_key, created_at), DELETE
    ON public.raster_mask_uploads TO :"labeling_owner";
GRANT SELECT, INSERT, UPDATE, DELETE ON
    public.mask_gc_intents,
    public.mask_gc_key_epochs,
    public.mask_gc_sweeps,
    public.mask_gc_roots
    TO :"labeling_owner";
GRANT SELECT ON public.mask_gc_write_permit_secrets TO :"labeling_owner";
GRANT SELECT, INSERT, UPDATE, DELETE ON public.batch_result_bundles,
    public.batch_result_payloads, public.batch_result_parts,
    public.prediction_retry_attempts TO :"labeling_owner";
GRANT SELECT ON public.predictions, public.prediction_metas,
    public.failed_predictions TO :"labeling_owner";
-- FOR KEY SHARE in the failure proof needs UPDATE on one column.
GRANT UPDATE (id, retry_count, last_retry_at, extra)
    ON public.failed_predictions TO :"labeling_owner";
GRANT SELECT, INSERT, UPDATE, DELETE ON public.batch_result_executions,
    public.batch_result_members, public.prediction_retry_deliveries TO :"jobs_owner";
GRANT SELECT (dataset_item_id) ON public.video_chapters TO :"labeling_owner";
GRANT SELECT (id, project_id, task_id, retry_count, dismissed_at, extra), DELETE
    ON public.failed_predictions TO :"labeling_owner";
GRANT SELECT (prediction_id, failed_prediction_id, extra), DELETE
    ON public.prediction_metas TO :"labeling_owner";
GRANT SELECT (
    id, project_id, task_id, parent_annotation_id, parent_prediction_id,
    sensor_dataset_item_id
), UPDATE (parent_annotation_id, parent_prediction_id), DELETE
    ON public.annotations TO :"labeling_owner";
GRANT SELECT, INSERT ON public.annotation_input_sources TO :"labeling_owner";
GRANT SELECT, INSERT ON public.annotation_video_input_sources TO :"labeling_owner";
GRANT SELECT (id, project_id, task_id, annotation_id, thread_parent_id),
    UPDATE (thread_parent_id), DELETE
    ON public.annotation_feedbacks TO :"labeling_owner";
GRANT SELECT (task_id, annotation_id), DELETE
    ON public.annotation_drafts TO :"labeling_owner";
GRANT SELECT (id, task_id, user_id, expire_at), UPDATE (id), DELETE
    ON public.task_locks TO :"labeling_owner";
GRANT SELECT (project_id, annotation_id), DELETE
    ON public.annotation_comments TO :"labeling_owner";
GRANT SELECT (project_id), DELETE ON public.task_events TO :"labeling_owner";
GRANT SELECT (project_id), DELETE ON public.mask_review_scopes TO :"labeling_owner";
GRANT SELECT (project_id), DELETE ON public.mask_qc_issues TO :"labeling_owner";
GRANT SELECT (project_id), DELETE ON public.mask_qc_runs TO :"labeling_owner";
GRANT SELECT (project_id, scene_id), DELETE
    ON public.point_cloud_quality_issues TO :"labeling_owner";
GRANT SELECT (project_id), DELETE
    ON public.point_cloud_quality_evaluations TO :"labeling_owner";
GRANT SELECT (project_id, scope_json), DELETE
    ON public.point_cloud_quality_runs TO :"labeling_owner";
GRANT SELECT (project_id), DELETE
    ON public.mask_annotation_revisions TO :"labeling_owner";
GRANT SELECT (project_id), DELETE
    ON public.mask_repair_batches TO :"labeling_owner";
GRANT SELECT (project_id), DELETE
    ON public.mask_format_imports TO :"labeling_owner";
GRANT SELECT (project_id, scene_id), DELETE ON public.scene_tracks TO :"labeling_owner";
GRANT SELECT (scene_id) ON public.scene_track_operations TO :"labeling_owner";
GRANT SELECT (dataset_item_id) ON public.video_segments,
    public.video_tracker_jobs TO :"labeling_owner";
GRANT SELECT (
    id, task_id, dataset_item_id, annotation_id, segment_id, target_class_name,
    target_tool_unit_id, created_by, status, job_kind, track_id_snapshot,
    correction_frame, model_key, direction, from_frame, to_frame, prompt,
    staged_result, revision, cancel_requested_at, started_at, created_at, completed_at,
    event_channel, error_message, updated_at
), UPDATE (id, status, staged_result, error_message, completed_at, updated_at)
    ON public.video_tracker_jobs TO :"labeling_owner";
GRANT SELECT (
    id, status, cancel_requested_at, started_at
), UPDATE (id) ON public.video_tracker_jobs TO :"jobs_owner";
GRANT SELECT (tracker_job_id, project_id, prepare_error)
    ON public.video_tracker_input_scopes TO :"jobs_owner";
GRANT SELECT (
    id, task_id, project_id, parent_annotation_id, geometry, track_id, video_segment_id, is_active,
    was_cancelled, version
) ON public.annotations TO :"labeling_owner";
GRANT SELECT (
    id, dataset_item_id, start_frame, end_frame, segment_index, status,
    assignee_id, locked_by, lock_expires_at
), UPDATE (id) ON public.video_segments TO :"labeling_owner";
GRANT INSERT (
    id, project_id, dataset_item_id, display_id, file_name, file_path, file_type,
    tags, status, assignee_is_override, is_labeled, overlap, total_annotations,
    total_predictions, reviewer_is_override, first_review_eligible,
    annotation_contributor_ids, reopened_count, batch_id, version
) ON public.tasks TO :"labeling_owner";
GRANT SELECT (
    id, total_tasks, completed_tasks, review_tasks, in_progress_tasks, batch_summary
), UPDATE (
    total_tasks, completed_tasks, review_tasks, in_progress_tasks,
    batch_summary, updated_at
)
    ON public.projects TO :"labeling_owner";
GRANT SELECT (task_id, dataset_item_id, role),
    INSERT (id, task_id, dataset_item_id, role, sensor_name)
    ON public.task_dataset_item_links TO :"labeling_owner";
GRANT SELECT (
    file_type, file_path, file_name, assignee_id, reviewer_id, created_at,
    media_source_generation, media_object_identity, media_source_drifted,
    thumbnail_path, blurhash
) ON public.tasks TO :"labeling_owner";
GRANT SELECT (version, total_predictions) ON public.tasks TO :"labeling_owner";
-- Project query facts stay with Labeling; Workspace receives only typed reads.
GRANT SELECT (created_at, reviewed_at, updated_at, submitted_at)
    ON public.tasks TO :"labeling_owner";
GRANT SELECT (created_at, parent_prediction_id, is_active, was_cancelled)
    ON public.annotations TO :"labeling_owner";
GRANT SELECT (class_name, attributes) ON public.annotations TO :"labeling_owner";
GRANT UPDATE (class_name) ON public.annotations TO :"labeling_owner";
GRANT SELECT (id, status, annotator_id, reviewer_id)
    ON public.task_batches TO :"labeling_owner";
GRANT UPDATE (
    media_source_generation, media_object_identity, media_source_drifted,
    thumbnail_path, blurhash
) ON public.tasks TO :"labeling_owner";
GRANT UPDATE (updated_at) ON public.tasks TO :"labeling_owner";
GRANT UPDATE (assignee_id, reviewer_id, assigned_at, reviewer_claimed_at)
    ON public.tasks TO :"labeling_owner";
GRANT SELECT (
    display_id, annotation_contributor_ids, review_round_id,
    review_contributor_ids, review_submitter_id, assignee_is_override,
    reviewer_is_override, reviewer_claimed_at
) ON public.tasks TO :"labeling_owner";
GRANT UPDATE (id) ON public.task_dataset_item_links TO :"labeling_owner";
GRANT USAGE ON SEQUENCE public.display_seq_tasks TO :"labeling_owner";
GRANT SELECT, INSERT, UPDATE ON public.task_append_operations TO :"labeling_owner";
GRANT SELECT (
    id, actor_id, dataset_id, project_id, association_id, job_id, mode,
    chunk_size, request, manifest, total, next_chunk, processed, created,
    state, created_at, updated_at
), INSERT (
    id, actor_id, dataset_id, project_id, association_id, job_id, mode,
    chunk_size, request
), UPDATE (
    manifest, total, next_chunk, processed, created, state, updated_at
) ON public.task_creation_operations TO :"labeling_owner";
GRANT SELECT (operation_id, chunk_index, result),
    INSERT (operation_id, chunk_index, result), UPDATE (chunk_index)
    ON public.task_creation_chunks TO :"labeling_owner";

-- Jobs writes its own lifecycle row; no direct table access reaches the caller.
GRANT SELECT ON public.async_jobs TO :"jobs_owner";
GRANT INSERT (id, kind, project_id, user_id, status, progress_pct, payload, result,
    celery_task_id, created_at, updated_at)
    ON public.async_jobs TO :"jobs_owner";
GRANT UPDATE (
    status, progress_pct, result, error_message, celery_task_id, started_at,
    completed_at, updated_at, project_id, retained_project_id, payload
) ON public.async_jobs TO :"jobs_owner";

-- Settings and Notifications retain their own relation permissions.
GRANT SELECT (key, value_type, value_json) ON public.system_settings TO :"settings_owner";
-- SHARE table locking fences an absent connector policy row from a concurrent insert.
GRANT UPDATE ON public.system_settings TO :"settings_owner";
GRANT SELECT, INSERT, DELETE ON public.notifications TO :"notifications_owner";
REVOKE UPDATE ON public.notifications FROM :"notifications_owner";
REVOKE UPDATE (
    id, user_id, type, target_type, target_id, payload, read_at, created_at
) ON public.notifications FROM :"notifications_owner";
GRANT UPDATE (read_at) ON public.notifications TO :"notifications_owner";
GRANT SELECT (user_id, type, channels)
    ON public.notification_preferences TO :"notifications_owner";
GRANT INSERT (user_id, type, channels)
    ON public.notification_preferences TO :"notifications_owner";
GRANT UPDATE (channels, updated_at)
    ON public.notification_preferences TO :"notifications_owner";
GRANT SELECT (
    notification_id, operation_id, recipient_id, notification_type, state,
    attempt, available_at, worker_id, lease_id, lease_expires_at,
    last_reason, created_at, delivered_at
) ON public.notification_delivery_outbox TO :"notifications_owner";
GRANT INSERT (
    notification_id, operation_id, recipient_id, notification_type
) ON public.notification_delivery_outbox TO :"notifications_owner";
GRANT UPDATE (
    state, attempt, available_at, worker_id, lease_id, lease_expires_at,
    last_reason, delivered_at
) ON public.notification_delivery_outbox TO :"notifications_owner";

GRANT USAGE ON SCHEMA aap_settings_v1 TO :"settings_owner", :"assets_owner", :"identity_owner";
GRANT USAGE ON SCHEMA aap_notifications_v1
    TO :"notifications_owner", :"jobs_owner", :"identity_owner";
GRANT USAGE ON SCHEMA aap_identity_v1 TO :"jobs_owner", :"notifications_owner";
GRANT EXECUTE ON FUNCTION aap_identity_v1.lock_active_actor(uuid) TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION aap_identity_v1.read_actor_facts(uuid)
    TO :"notifications_owner";
GRANT SELECT (id, role, is_active) ON aap_identity_v1.account_profiles
    TO :"notifications_owner";
GRANT USAGE ON SCHEMA aap_workspace_v1, aap_workspace_project_query_v1, aap_jobs_v1
    TO :"notifications_owner";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.actor_project_memberships(uuid),
    aap_workspace_project_query_v1.project_search_scope(uuid,text)
    TO :"notifications_owner";
GRANT SELECT (id, kind, scope_project_id) ON aap_jobs_v1.notification_scope_facts
    TO :"notifications_owner";
GRANT EXECUTE ON FUNCTION aap_settings_v1.resolve_connector_host_allowlist(jsonb)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_settings_v1.lock_connector_host_allowlist(jsonb)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_notifications_v1.create_job_terminal_notification(
    uuid, uuid, text, jsonb
) TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.create_dataset_import_job(uuid,uuid,jsonb),
    aap_jobs_v1.lock_dataset_import_job(uuid),
    aap_jobs_v1.claim_dataset_import_job(uuid),
    aap_jobs_v1.update_dataset_import_job_progress(uuid,text,integer,integer,bigint),
    aap_jobs_v1.finish_dataset_import_job(uuid,text,jsonb,text),
    aap_jobs_v1.cancel_dataset_import_job(uuid,uuid,jsonb)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.record_dataset_import(uuid,uuid,uuid,jsonb,jsonb)
    TO :"assets_owner";

-- Learning holds project retention evidence, task-derived streams and recipes.
GRANT SELECT (project_id) ON public.active_learning_rounds TO :"learning_owner";
GRANT SELECT (task_id, dataset_item_id) ON public.active_learning_pool_members,
    public.active_learning_candidates TO :"learning_owner";
GRANT SELECT (project_id) ON public.training_studies,
    public.trained_model_versions, public.project_model_selections,
    public.project_model_releases, public.model_decisions,
    public.sample_evaluation_runs, public.learning_policies,
    public.training_dataset_versions, public.training_runs,
    public.training_previews, public.learning_triggers
    TO :"learning_owner";
GRANT SELECT (project_id), DELETE ON public.project_task_datasets,
    public.training_recipes, public.training_recipe_revisions
    TO :"learning_owner";
GRANT SELECT (deployment_id) ON public.sample_evaluation_runs TO :"learning_owner";
GRANT SELECT (id, registry_id) ON public.trained_model_deployments TO :"learning_owner";

-- Other owners expose only their project cleanup predicates and row mutations.
GRANT SELECT (project_id), DELETE ON public.project_ml_backend_pool,
    public.project_pipelines TO :"inference_owner";
GRANT SELECT (
        id, scope, project_id, organization_id, name, stages, is_default,
        created_by, usage_count, created_at, updated_at
    ) ON public.project_pipelines TO :"inference_owner";
GRANT INSERT (
        id, scope, project_id, organization_id, name, stages, is_default, created_by
    ) ON public.project_pipelines TO :"inference_owner";
GRANT UPDATE (
        scope, project_id, organization_id, name, stages, is_default,
        usage_count, updated_at
    ) ON public.project_pipelines TO :"inference_owner";
-- Native health operations materialize full rows and hold caller-owned locks.
GRANT SELECT ON public.ml_backend_registry, public.ml_backend_service_pools,
    public.ml_backend_pool_members TO :"inference_owner";
GRANT UPDATE (state, last_checked_at, health_meta, is_interactive, updated_at)
    ON public.ml_backend_registry TO :"inference_owner";
GRANT INSERT (
        id, name, url, state, is_interactive, auth_method, auth_token,
        extra_params, gpu_resource_id, vram_budget_mb, eviction_priority,
        health_meta, source, error_message, last_checked_at
    ) ON public.ml_backend_registry TO :"inference_owner";
GRANT UPDATE (name, error_message, extra_params)
    ON public.ml_backend_registry TO :"inference_owner";
GRANT INSERT (
        id, name, enabled, routing_policy, legacy_instance_id,
        routing_generation, capability_fingerprint, capability_snapshot
    ) ON public.ml_backend_service_pools TO :"inference_owner";
GRANT INSERT (id, pool_id, registry_id, traffic_state, weight)
    ON public.ml_backend_pool_members TO :"inference_owner";
GRANT UPDATE (capability_snapshot, capability_fingerprint, enabled,
    routing_generation, updated_at)
    ON public.ml_backend_service_pools TO :"inference_owner";
GRANT UPDATE (traffic_state, updated_at)
    ON public.ml_backend_pool_members TO :"inference_owner";
GRANT SELECT (
        backend_registry_id, gpu_resource_id, membership_epoch, state, max_concurrency
    ),
    UPDATE (state) ON public.gpu_backend_memberships TO :"inference_owner";
-- The existing Registry configuration trigger retains its GPU fence checks.
GRANT SELECT (backend_registry_id, runtime_epoch_high_water,
    generation_high_water, control_epoch_high_water, token_expiry_high_water),
    UPDATE (backend_registry_id)
    ON public.gpu_backend_fences TO :"inference_owner";
GRANT UPDATE (membership_epoch, runtime_epoch_baseline, vram_budget_mb,
    eviction_priority, max_concurrency, updated_at)
    ON public.gpu_backend_memberships TO :"inference_owner";
GRANT SELECT (registry_id, pool_id) ON public.ml_backend_pool_members
    TO :"inference_owner";
GRANT SELECT (id, created_at, updated_at, capability_snapshot)
    ON public.ml_backend_service_pools TO :"inference_owner";
GRANT SELECT (id, enabled, routing_generation, legacy_instance_id),
    UPDATE (enabled, routing_generation, updated_at)
    ON public.ml_backend_service_pools TO :"inference_owner";
GRANT SELECT (
        id, last_checked_at, created_at, updated_at, extra_params, health_meta
    ) ON public.ml_backend_registry TO :"inference_owner";
GRANT SELECT (id, url, auth_method, auth_token, extra_params)
    ON public.ml_backend_registry TO :"inference_owner";
GRANT SELECT (
        id, project_id, pool_id, enabled, created_at, updated_at, default_variants
    ) ON public.project_ml_backend_pool TO :"inference_owner";
GRANT SELECT (id, pool_id, enabled), INSERT (id, project_id, pool_id, enabled),
    UPDATE (enabled, updated_at)
    ON public.project_ml_backend_pool TO :"inference_owner";
GRANT SELECT (project_id), DELETE ON public.project_task_views TO :"insights_owner";
GRANT SELECT (project_id, task_id), UPDATE (project_id, task_id)
    ON public.bug_reports TO :"diagnostics_owner";
GRANT SELECT (project_id), DELETE ON public.export_artifacts TO :"export_owner";

-- Cross-owner function calls share one PostgreSQL transaction and retain the
-- dataset -> current authority -> association -> Jobs/Labeling lock order.
GRANT USAGE ON SCHEMA aap_identity_v1
    TO :"identity_owner", :"workspace_owner", :"assets_owner", :"audit_owner",
       :"labeling_owner", :"learning_owner", :"inference_owner", :"workflow_role";
GRANT USAGE ON SCHEMA aap_identity_keys_v1, aap_identity_sessions_v1,
    aap_identity_accounts_v1, aap_identity_recovery_v1, aap_identity_groups_v1,
    aap_identity_deactivation_v1, aap_identity_invitations_v1,
    aap_identity_mail_v2, aap_identity_directory_v1,
    aap_identity_admin_passwords_v1, aap_identity_platform_roles_v1,
    aap_identity_suspensions_v1, aap_identity_deletions_v1,
    aap_identity_offboarding_v1, aap_identity_briefs_v1
    TO :"identity_owner";
GRANT USAGE ON SCHEMA aap_workspace_v1 TO :"workspace_owner", :"assets_owner",
    :"labeling_owner", :"jobs_owner", :"workflow_role", :"identity_owner",
    :"inference_owner";
GRANT USAGE ON SCHEMA aap_assets_v1 TO :"assets_owner", :"labeling_owner",
    :"jobs_owner", :"learning_owner", :"workspace_owner", :"workflow_role",
    :"asset_cleanup_role";
GRANT USAGE ON SCHEMA aap_labeling_v1 TO :"labeling_owner", :"jobs_owner",
    :"assets_owner", :"learning_owner", :"diagnostics_owner",
    :"workspace_owner", :"workflow_role", :"identity_owner";
GRANT USAGE ON SCHEMA aap_jobs_v1 TO :"jobs_owner", :"labeling_owner",
    :"workspace_owner", :"assets_owner", :"workflow_role", :"inference_owner";
GRANT USAGE ON SCHEMA aap_inference_routing_v1 TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION pg_catalog.pg_control_system() TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.video_execution_step(text,text),
    aap_jobs_v1.video_execution_checkpoint_step(text,text)
    TO :"labeling_owner", :"workflow_role";
GRANT USAGE ON SCHEMA aap_learning_v1
    TO :"learning_owner", :"workspace_owner", :"assets_owner", :"inference_owner";
GRANT EXECUTE ON FUNCTION aap_learning_v1.registry_evaluation_reference_exists(uuid),
    aap_jobs_v1.count_running_registry_predictions(uuid),
    aap_jobs_v1._video_execution_route_step(text,text),
    aap_jobs_v1._video_execution_scope_is_current(uuid,boolean)
    TO :"inference_owner";
GRANT EXECUTE ON FUNCTION
    aap_inference_routing_v1._video_execution_canonical_json(jsonb),
    aap_inference_routing_v1._video_execution_no_scope_rejection(uuid,uuid,text)
    TO :"jobs_owner";
GRANT USAGE ON SCHEMA aap_inference_v1 TO :"inference_owner", :"workspace_owner";
GRANT USAGE ON SCHEMA aap_workspace_configuration_v1 TO :"workspace_owner";
GRANT USAGE ON SCHEMA aap_workspace_template_query_v1 TO :"workspace_owner";
GRANT USAGE ON SCHEMA aap_workspace_directory_query_v1 TO :"workspace_owner";
GRANT USAGE ON SCHEMA aap_labeling_class_query_v1 TO :"labeling_owner";
GRANT USAGE ON SCHEMA aap_workspace_project_query_v1
    TO :"workspace_owner", :"labeling_owner", :"identity_owner", :"inference_owner";
GRANT EXECUTE ON FUNCTION aap_workspace_project_query_v1.project_search_scope(uuid, text)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION aap_workspace_project_query_v1.project_search_member_ids(uuid, text)
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION
    aap_labeling_v1.workspace_project_query_counters(uuid[]),
    aap_labeling_v1.workspace_project_query_ai_counts(uuid[]),
    aap_labeling_v1.workspace_project_query_annotation_count(uuid[], boolean),
    aap_labeling_v1.workspace_project_query_task_series(uuid[], timestamptz[]),
    aap_labeling_v1.workspace_project_query_annotation_series(uuid[], timestamptz[]),
    aap_labeling_v1.workspace_project_query_linked_task_count(uuid, uuid[]),
    aap_labeling_v1.workspace_project_query_task_count(uuid),
    aap_labeling_v1.workspace_project_query_batches(uuid),
    aap_labeling_v1.workspace_project_query_batch_task_counts(uuid),
    aap_assets_v1.workspace_project_query_dataset_ids(uuid),
    aap_assets_v1.workspace_project_query_dataset_item_count(uuid[]),
    aap_jobs_v1.workspace_project_query_job_status_counts(uuid),
    aap_jobs_v1.workspace_project_query_latest_job_status(uuid),
    aap_inference_v1.workspace_project_query_pool(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION
    aap_assets_v1.workspace_project_query_dataset_item_ids(uuid[])
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    aap_workspace_configuration_v1.configuration_policy(),
    aap_workspace_configuration_v1.configuration_step(jsonb, text, jsonb, text[], text[])
    TO :"workspace_owner";
GRANT USAGE ON SCHEMA aap_insights_v1 TO :"insights_owner", :"workspace_owner";
GRANT USAGE ON SCHEMA aap_diagnostics_v1
    TO :"diagnostics_owner", :"workspace_owner";
GRANT USAGE ON SCHEMA aap_export_v1 TO :"export_owner", :"workspace_owner";
GRANT USAGE ON SCHEMA aap_audit_v1
    TO :"identity_owner", :"audit_owner", :"workspace_owner", :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.record_identity_session_event(
    text, jsonb, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.record_identity_account_event(
    text, jsonb, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.record_identity_bootstrap_admin(
    uuid, text, text
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.record_identity_recovery_event(
    text, jsonb, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.record_identity_group_event(
    text, jsonb, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.record_identity_deactivation_event(
    text, jsonb, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.record_identity_invitation_event(
    text, jsonb, jsonb, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.valid_request_context(jsonb)
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION aap_settings_v1.identity_recovery_policy(jsonb)
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION aap_settings_v1.invitation_policy(jsonb)
    TO :"identity_owner";
GRANT USAGE ON SCHEMA aap_workspace_v1 TO :"identity_owner";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.lock_group_assignment_scope(
    uuid, uuid[], boolean
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.invitation_projects(uuid[]),
    aap_workspace_v1.lock_invitation_project(uuid, uuid),
    aap_workspace_v1.read_invitation_membership(uuid, uuid),
    aap_workspace_v1.grant_invitation_membership(uuid, uuid, text, uuid, uuid)
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION
    aap_workspace_v1.identity_directory_owned_member_ids(uuid),
    aap_workspace_v1.identity_directory_project_member_ids(uuid),
    aap_workspace_v1.identity_directory_lifecycle_blocked(uuid, uuid[])
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION
    aap_workspace_v1.admin_password_target_in_owned_project(uuid, uuid)
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION
    aap_workspace_v1.identity_platform_role_facts(uuid),
    aap_workspace_v1.lock_identity_platform_role_owned_projects(uuid)
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.lock_identity_suspension_scope(
    uuid, uuid, text
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION
    aap_workspace_v1.lock_identity_deletion_scope(uuid, uuid),
    aap_workspace_v1.identity_deletion_transfer_covered(uuid, uuid[])
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION
    aap_workspace_v1.lock_identity_offboarding_scope(uuid, uuid[], uuid[]),
    aap_workspace_v1.identity_offboarding_replay_scope(uuid, uuid[]),
    aap_workspace_v1.identity_offboarding_facts(uuid),
    aap_workspace_v1.transfer_identity_offboarding_owners(uuid, jsonb)
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION
    aap_workspace_v1.identity_brief_project_roles(uuid, uuid[])
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION
    aap_identity_v1.read_workspace_membership_accounts(uuid[]),
    aap_identity_v1.workspace_directory_superadmins()
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION
    aap_audit_v1.valid_request_context(jsonb),
    aap_audit_v1.record_workspace_membership_event(text, jsonb, uuid, jsonb, jsonb)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION
    aap_assets_v1.workspace_configuration_datasets(uuid),
    aap_labeling_v1.workspace_configuration_video_facts(uuid),
    aap_labeling_v1.rename_project_annotation_class(uuid, text, text),
    aap_inference_v1.enable_workspace_project_backend(uuid, uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION
    aap_assets_v1.workspace_configuration_dataset_rows(uuid),
    aap_assets_v1.workspace_configuration_dataset_scenes(uuid),
    aap_assets_v1.read_media_summaries(uuid[]),
    aap_labeling_v1.workspace_configuration_task_count(uuid),
    aap_labeling_v1.workspace_configuration_annotation_count(uuid),
    aap_labeling_v1.workspace_configuration_tracker_count(uuid),
    aap_labeling_v1.workspace_configuration_video_items(uuid),
    aap_inference_v1.workspace_configuration_source_pool(uuid),
    aap_inference_v1.workspace_configuration_backend(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION
    aap_assets_v1.workspace_configuration_existing_item_ids(uuid[])
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    aap_labeling_v1.read_membership_work(uuid, uuid, uuid, uuid),
    aap_labeling_v1.lock_membership_work(uuid, uuid, text, boolean),
    aap_labeling_v1.handoff_membership_annotation(uuid, uuid, uuid),
    aap_labeling_v1.handoff_membership_reviewer(uuid, uuid, uuid),
    aap_labeling_v1.release_membership_task_locks(uuid, uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION
    aap_labeling_v1.identity_platform_role_facts(uuid, uuid[])
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION
    aap_labeling_v1.identity_deletion_work(uuid),
    aap_labeling_v1.transfer_identity_deletion_work(uuid, uuid)
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION
    aap_labeling_v1.lock_identity_offboarding_work(uuid, uuid[]),
    aap_labeling_v1.identity_offboarding_facts(uuid),
    aap_labeling_v1.transfer_identity_offboarding_work(uuid, jsonb),
    aap_labeling_v1.release_identity_offboarding_task_locks(uuid)
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.identity_directory_project_refs(uuid[])
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.record_identity_directory_export(
    jsonb, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.record_identity_admin_password_reset(
    jsonb, uuid, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.record_identity_platform_role_change(
    jsonb, uuid, jsonb, text, text, text
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.record_identity_suspension_event(
    text, jsonb, uuid, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.record_identity_deletion_event(
    jsonb, uuid, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.record_identity_offboarding_event(
    jsonb, uuid, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.invitation_work_summary(uuid, uuid, text)
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION aap_notifications_v1.create_identity_deactivation_notifications(
    uuid, text, jsonb, uuid[]
) TO :"identity_owner";

GRANT EXECUTE ON FUNCTION aap_identity_v1.lock_active_actor(uuid)
    TO :"workspace_owner", :"assets_owner", :"labeling_owner";
GRANT EXECUTE ON FUNCTION aap_identity_v1.read_actor_facts(uuid)
    TO :"workspace_owner", :"assets_owner", :"labeling_owner", :"learning_owner";
GRANT EXECUTE ON FUNCTION
    aap_identity_v1.workspace_configuration_org_scope(uuid, uuid),
    aap_identity_v1.workspace_template_query_org_ids(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION
    aap_identity_v1.lock_accounts_share(uuid[], boolean),
    aap_identity_v1.lock_accounts_update(uuid[], boolean),
    aap_identity_v1.lock_accounts_key_share(uuid[], boolean)
    TO :"workspace_owner", :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_identity_v1.lock_accounts_share(uuid[], boolean)
    TO :"labeling_owner", :"learning_owner";
GRANT EXECUTE ON FUNCTION aap_identity_v1.check_accepted_api_key(uuid, uuid, text[])
    TO :"learning_owner";
GRANT EXECUTE ON FUNCTION aap_identity_v1.lock_accounts_update(uuid[], boolean)
    TO :"inference_owner";
GRANT EXECUTE ON FUNCTION aap_identity_v1.read_actor_facts(uuid)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_identity_v1.lock_audit_actor(uuid)
    TO :"audit_owner";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.lock_project_reference(uuid)
    TO :"assets_owner", :"inference_owner";
GRANT SELECT ON aap_workspace_v1.project_catalog_configs TO :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.read_project_visibility(uuid, uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.lock_project_manager(uuid, uuid)
    TO :"assets_owner", :"labeling_owner", :"jobs_owner", :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.project_access_step(uuid, uuid, text)
    TO :"inference_owner";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.clear_primary_backend_bindings(uuid)
    TO :"inference_owner";
GRANT EXECUTE ON FUNCTION aap_workspace_project_query_v1.project_query_step(
    jsonb, text, jsonb, text, text, text[], text[], uuid, text,
    timestamptz, timestamptz, bigint, integer
) TO :"inference_owner";
GRANT EXECUTE ON FUNCTION aap_identity_v1.workspace_template_query_org_ids(uuid)
    TO :"inference_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.lock_dataset_reference(uuid),
    aap_assets_v1.lock_current_association(uuid, uuid),
    aap_assets_v1.lock_append_sources(uuid, uuid[]),
    aap_assets_v1.lock_task_creation_items(uuid, uuid[])
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.invalidate_task_image_source(uuid)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.lock_current_association(uuid, uuid)
    TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.lock_dataset_reference(uuid)
    TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.lock_current_association(uuid, uuid),
    aap_assets_v1.lock_dataset_reference(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.lock_scene_source_references(uuid[])
    TO :"labeling_owner", :"workflow_role";
GRANT SELECT ON aap_assets_v1.project_dataset_item_sources TO :"labeling_owner";
GRANT SELECT ON aap_labeling_v1.project_catalog_counters TO :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.project_dataset_task_counts(uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.project_accepted_image_input_job(uuid, uuid, uuid, boolean)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    aap_labeling_v1.project_video_tracker_input(uuid,uuid,uuid,uuid,text,boolean,boolean,boolean),
    aap_labeling_v1.fail_video_tracker_input_preparation(uuid,uuid,uuid,uuid,text,text),
    aap_labeling_v1.video_tracker_input_referenced(uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    aap_assets_v1.read_video_tracker_task_source(uuid,uuid,jsonb),
    aap_assets_v1._lock_video_tracker_input_plan(uuid,uuid,uuid,uuid,text,text,boolean)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.accepted_image_input_job_consistency(uuid, uuid, uuid, uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    aap_jobs_v1.project_accepted_image_input_manifest_job(uuid, uuid, uuid, uuid, jsonb, text, boolean),
    aap_jobs_v1.accepted_image_input_manifest_job_consistency(uuid, uuid, uuid, uuid, jsonb, text)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    aap_jobs_v1.project_video_input_job(uuid, uuid, uuid, uuid, jsonb, text, boolean),
    aap_labeling_v1.read_video_input_task_source(uuid, uuid),
    aap_labeling_v1.lock_video_input_task_source(uuid, uuid, jsonb)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    aap_labeling_v1.open_video_input_manifest_members(uuid, uuid, jsonb),
    aap_labeling_v1.lock_video_input_manifest_member(uuid, uuid, uuid, jsonb, jsonb)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    aap_labeling_v1.project_accepted_image_input_task(uuid, uuid, boolean),
    aap_labeling_v1.accepted_image_input_referenced(uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.accepted_video_input_referenced(uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.associate_project(uuid, uuid, uuid)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_assets_v1.delete_project_owned_records(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.project_task_references(uuid)
    TO :"assets_owner", :"learning_owner", :"diagnostics_owner";
GRANT EXECUTE ON FUNCTION
    aap_labeling_v1.read_image_task_source(uuid, uuid, boolean),
    aap_labeling_v1.observe_image_task_source(uuid, text, jsonb),
    aap_labeling_v1.publish_task_image_thumbnail(uuid, text, bigint, text, text),
    aap_labeling_v1.image_thumbnail_references(text),
    aap_labeling_v1.scan_image_task_sources(jsonb, uuid, integer),
    aap_labeling_v1.invalidate_task_image_thumbnail(uuid, text, text)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    aap_labeling_v1.open_accepted_image_input_manifest_members(uuid, uuid, jsonb),
    aap_labeling_v1.lock_accepted_image_input_manifest_member(uuid, uuid, uuid, uuid, uuid, text)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.lock_accepted_image_input(uuid, uuid, uuid)
    TO :"workflow_role", :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    aap_assets_v1._lock_accepted_image_input_receipt(uuid, uuid, uuid)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    aap_assets_v1._lock_accepted_image_input_pin(uuid, uuid, text)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.attach_annotation_input_source(uuid, uuid, text)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.copy_annotation_input_sources(uuid, uuid)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION
    aap_assets_v1.find_accepted_image_input(uuid, uuid, uuid),
    aap_assets_v1.reserve_accepted_image_input(uuid, uuid, uuid, text, jsonb),
    aap_assets_v1.read_accepted_image_input(uuid, uuid, uuid),
    aap_assets_v1.read_accepted_image_input_work(uuid, uuid, uuid)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_assets_v1.capture_accepted_image_input_manifest(
    uuid, uuid, uuid, jsonb
) TO :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_assets_v1.capture_accepted_video_manifest(uuid,uuid,uuid,jsonb)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION
    aap_assets_v1.lock_accepted_video_input(uuid,uuid,uuid,uuid,text),
    aap_assets_v1.lock_accepted_video_frame(uuid,uuid,uuid,uuid,text,bigint,jsonb,jsonb),
    aap_assets_v1._lock_accepted_video_input_pin(uuid,uuid,jsonb,uuid)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    aap_labeling_v1.attach_annotation_video_input_source(uuid,uuid,jsonb),
    aap_labeling_v1.copy_annotation_video_input_sources(uuid,uuid)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_assets_v1.dataset_item_references(uuid, uuid)
    TO :"labeling_owner", :"learning_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.dataset_item_scene_references(uuid, uuid)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.dataset_scene_references(uuid),
    aap_assets_v1.dataset_association_references(uuid)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.dataset_task_references(uuid, uuid)
    TO :"assets_owner", :"learning_owner", :"diagnostics_owner";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.delete_project_owned_records(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.append_dataset_items(uuid, uuid[], uuid),
    aap_labeling_v1.accept_dataset_task_creation(uuid, uuid, uuid, uuid, uuid, uuid, text, integer),
    aap_labeling_v1.prepare_dataset_task_creation(uuid, jsonb),
    aap_labeling_v1.read_dataset_task_creation(uuid),
    aap_labeling_v1.advance_dataset_task_creation(uuid, integer)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.append_dataset_items(uuid, uuid[], uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.read_dataset_task_creation(uuid)
    TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.lock_task_creation_job(uuid, uuid, uuid, uuid, uuid, uuid),
    aap_jobs_v1.update_task_creation_progress(uuid, uuid, bigint, bigint, bigint)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.create_task_creation_job(uuid, uuid, uuid, uuid, uuid),
    aap_jobs_v1.find_task_creation_job(uuid, uuid, uuid, uuid),
    aap_jobs_v1.claim_task_creation_job(uuid, text),
    aap_jobs_v1.finish_task_creation_job(uuid, uuid),
    aap_jobs_v1.fail_task_creation_job(uuid, uuid, text),
    aap_jobs_v1.cancel_task_creation_job(uuid, uuid)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.detach_project_references(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.cancel_dataset_task_creation(uuid, uuid, uuid)
    TO :"workspace_owner", :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.guard_dataset_source_deletion(uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.task_creation_execution_active(uuid, uuid)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION aap_learning_v1.lock_project_deletion(uuid),
    aap_learning_v1.guard_project_deletion(uuid),
    aap_learning_v1.delete_project_owned_records(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_inference_v1.clear_project_ml_backend_pool(uuid),
    aap_inference_v1.clear_project_pipelines(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_insights_v1.clear_project_task_views(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_diagnostics_v1.detach_project_references(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_export_v1.clear_project_export_artifacts(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.record_project_deletion(
    uuid, uuid, uuid, text, text, bigint, bigint, jsonb
) TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_learning_v1.guard_dataset_unlink(uuid, uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_learning_v1.guard_dataset_source_deletion(uuid, uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.guard_dataset_source_deletion(uuid, uuid),
    aap_labeling_v1.detach_deleted_dataset_attribution(uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.lock_dataset_unlink_scope(uuid, uuid),
    aap_labeling_v1.guard_dataset_unlink(uuid, uuid),
    aap_labeling_v1.preview_dataset_unlink(uuid, uuid),
    aap_labeling_v1.delete_dataset_tasks(uuid, uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.prepare_dataset_unlink_cleanup(
    uuid, uuid, uuid, text
), aap_assets_v1.remove_dataset_association(uuid, uuid, uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_diagnostics_v1.detach_dataset_task_references(
    uuid, uuid
) TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.valid_request_context(jsonb),
    aap_audit_v1.record_dataset_unlink(
        uuid, uuid, uuid, uuid, bigint, bigint, uuid[], jsonb
    ) TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.valid_request_context(jsonb),
    aap_audit_v1.record_dataset_source_deletion(
        uuid, uuid, uuid, uuid, bigint, bigint, bigint, bigint, bigint, bigint, jsonb
    ) TO :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.pending_storage_deletions_v2(
    text, timestamptz, uuid, uuid, integer
),
    aap_assets_v1.complete_storage_deletion(uuid, uuid),
    aap_assets_v1.claim_image_garbage(uuid, integer, integer),
    aap_assets_v1.complete_image_garbage(uuid, uuid),
    aap_assets_v1.claim_accepted_input_garbage(uuid, integer, integer),
    aap_assets_v1.complete_accepted_input_garbage(uuid, uuid),
    aap_assets_v1.claim_accepted_video_input_garbage(uuid,integer,integer),
    aap_assets_v1.complete_accepted_video_input_garbage(uuid,uuid)
    TO :"asset_cleanup_role";
REVOKE ALL ON FUNCTION aap_assets_v1.pending_storage_deletions(uuid, integer)
    FROM :"asset_cleanup_role";
GRANT EXECUTE ON FUNCTION aap_assets_v1.delete_dataset(
    uuid, uuid, uuid, text, jsonb
), aap_assets_v1.delete_dataset_item(
    uuid, uuid, uuid, uuid, text, jsonb
), aap_assets_v1.reserve_dataset_upload(
    uuid, uuid, uuid, text, text
), aap_assets_v1.lock_dataset_upload(uuid, uuid, uuid),
    aap_assets_v1.find_dataset_upload_duplicate(uuid, uuid, uuid, text),
    aap_assets_v1.complete_dataset_upload(
        uuid, uuid, uuid, uuid, bigint, text, integer, integer
    ), aap_assets_v1.discard_dataset_upload(uuid, uuid, uuid, text)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_assets_v1.begin_dataset_ingest(uuid,uuid,uuid,jsonb),
    aap_assets_v1.prepare_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb),
    aap_assets_v1.complete_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb),
    aap_assets_v1.read_dataset_ingest_layout(uuid,uuid,uuid,uuid,integer),
    aap_assets_v1.finish_dataset_ingest(uuid,uuid,uuid,jsonb)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION
    aap_assets_v1.claim_dataset_source_ingest_delivery(uuid,integer),
    aap_assets_v1.complete_dataset_source_ingest_delivery(uuid,uuid),
    aap_assets_v1.dataset_import_delivery_target_is_current(text,uuid)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.preview_dataset_unlink(uuid, uuid, uuid),
    aap_workspace_v1.unlink_dataset(uuid, uuid, uuid, uuid, text, jsonb)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.delete_project(uuid, uuid, uuid, jsonb)
    TO :"workflow_role";

-- Keep schemas owned by the migrator; NOLOGIN code owners cannot replace code.
GRANT CREATE ON SCHEMA aap_identity_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA aap_identity_keys_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA aap_identity_sessions_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA aap_identity_accounts_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA aap_identity_recovery_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA aap_identity_groups_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA aap_identity_directory_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA aap_identity_admin_passwords_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA aap_identity_platform_roles_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA aap_identity_suspensions_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA aap_identity_deletions_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA aap_identity_offboarding_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA aap_identity_briefs_v1 TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.lock_active_actor(uuid) OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.lock_audit_actor(uuid) OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.read_actor_facts(uuid) OWNER TO :"identity_owner";
ALTER VIEW aap_identity_v1.account_profiles OWNER TO :"identity_owner";
ALTER VIEW aap_identity_v1.invitation_audit_targets OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.check_accepted_api_key(uuid, uuid, text[])
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.workspace_configuration_org_scope(uuid, uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.workspace_template_query_org_ids(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.read_provisioning_account(text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.create_provisioning_account(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.create_seed_persona_account(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.replace_fixture_preferences(uuid,jsonb)
    OWNER TO :"identity_owner";
GRANT USAGE, CREATE ON SCHEMA aap_workspace_fixture_v1 TO :"workspace_owner";
ALTER VIEW aap_workspace_fixture_v1.templates OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_fixture_v1.create_project(jsonb) OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_fixture_v1.create_templates(jsonb) OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_fixture_v1.guard_project_cleanup(uuid[]) OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_fixture_v1.delete_projects(uuid[]) OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_fixture_v1.delete_templates(boolean,text[]) OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA aap_workspace_fixture_v1 FROM :"workspace_owner";

GRANT USAGE ON SCHEMA aap_identity_fixture_v1 TO :"identity_owner";
GRANT DELETE ON public.users,public.password_reset_tokens,public.user_invitations,public.organization_members
    TO :"identity_owner";
GRANT INSERT (accepted_at,accepted_user_id,revoked_at) ON public.user_invitations
    TO :"identity_owner";
ALTER VIEW aap_identity_fixture_v1.account_profiles OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_fixture_v1.create_account(uuid,text,text,text,text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_fixture_v1.mark_manual_inactive(uuid,timestamptz,uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_fixture_v1.create_invitations(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_fixture_v1.delete_accounts(boolean,text[])
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_fixture_v1.delete_reset_tokens(uuid[])
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_fixture_v1.delete_invitations(uuid[])
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_fixture_v1.delete_organization_members(uuid[])
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.read_scale_seed_super_admin()
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.read_scale_seed_annotator(uuid[])
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1._admission_failure(text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.authentication_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.read_actor_context(uuid, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.lock_accounts_share(uuid[], boolean)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.lock_accounts_update(uuid[], boolean)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.lock_accounts_key_share(uuid[], boolean)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.read_jwt_principal(
    uuid, uuid, bigint, timestamptz, boolean
) OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.read_api_key_candidates(text, uuid, integer)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.confirm_api_key_principal(uuid, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.get_credential_generation(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.advance_credential_generation(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.revoke_jti(uuid, timestamptz)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.is_jti_revoked(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.retire_account_credentials(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.lock_current_principal(
    uuid, text, uuid, bigint, timestamptz, text[]
) OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.purge_expired_revocations(integer)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.lock_super_admin_invariant()
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.super_admin_status(uuid)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA aap_identity_v1 FROM :"identity_owner";
ALTER FUNCTION aap_identity_keys_v1.key_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_keys_v1.execute_key_command(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA aap_identity_keys_v1 FROM :"identity_owner";
ALTER FUNCTION aap_identity_sessions_v1._failure(text, bigint)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_sessions_v1.session_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_sessions_v1.read_login_candidate(text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_sessions_v1.execute_session_command(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA aap_identity_sessions_v1 FROM :"identity_owner";
ALTER FUNCTION aap_identity_accounts_v1._failure(text, smallint, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_accounts_v1._account_record(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_accounts_v1._lock_current_actor(jsonb, text, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_accounts_v1.account_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_accounts_v1.prepare_account_command(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_accounts_v1.apply_account_command(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_accounts_v1.mark_inactive_accounts_offline(timestamptz)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA aap_identity_accounts_v1 FROM :"identity_owner";
ALTER FUNCTION aap_identity_recovery_v1._failure(text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_recovery_v1._mail_failure(text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_recovery_v1._actor_record(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_recovery_v1._token_record(text, uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_recovery_v1.recovery_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_recovery_v1.prepare_recovery_command(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_recovery_v1.apply_recovery_command(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_recovery_v1.read_registration_status(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_recovery_v1.mail_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_recovery_v1.claim_mail(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_recovery_v1.renew_mail(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_recovery_v1.finish_mail(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA aap_identity_recovery_v1 FROM :"identity_owner";
ALTER FUNCTION aap_identity_groups_v1._failure(text, smallint, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_groups_v1._strip_unicode_space(text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_groups_v1._account_record(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_groups_v1._group_record(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_groups_v1._group_scope(uuid, uuid[], boolean)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_groups_v1._group_actor(jsonb, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_groups_v1.group_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_groups_v1.execute_group_command(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_groups_v1.resolve_invitation_group(uuid, text)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA aap_identity_groups_v1 FROM :"identity_owner";
GRANT CREATE ON SCHEMA aap_identity_deactivation_v1 TO :"identity_owner";
ALTER FUNCTION aap_identity_deactivation_v1._failure(text, smallint, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_deactivation_v1._due_failure(text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_deactivation_v1._strip_unicode_space(text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_deactivation_v1._account_record(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_deactivation_v1._actor(jsonb, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_deactivation_v1._reconcile_request(uuid, timestamptz, timestamptz)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_deactivation_v1.deactivation_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_deactivation_v1.due_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_deactivation_v1.execute_deactivation_command(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_deactivation_v1.process_due_deactivations(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA aap_identity_deactivation_v1 FROM :"identity_owner";

GRANT CREATE ON SCHEMA aap_identity_invitations_v1, aap_identity_mail_v2
    TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1._failure(text, text, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1._strip_unicode_space(text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1._account_record(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1._invitation_record(uuid, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1._project_map(uuid[])
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1._policy(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1._lock_users(uuid[])
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1._actor(jsonb, text, boolean, boolean)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1.invitation_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1.invitation_quota_reached(
    uuid, timestamptz, integer
) OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1._empty_result()
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1._incompatible_role_detail(text, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1._args_valid(text, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1._read_result(text, uuid, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1._native_actor(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1._project_record(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1._batch_snapshot(uuid, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1.prepare_invitation_command(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1._batch_effects(
    boolean, uuid, text, text, jsonb, jsonb, jsonb, jsonb, jsonb
) OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1._run_invitation_preview(
    uuid, text, text, jsonb, jsonb, jsonb, jsonb
) OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1.apply_invitation_command(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_invitations_v1.lock_invitation_mail_scope(uuid, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_mail_v2.mail_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_mail_v2.claim_mail(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_mail_v2.renew_mail(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_mail_v2.finish_mail(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA aap_identity_invitations_v1, aap_identity_mail_v2
    FROM :"identity_owner";

ALTER FUNCTION aap_identity_directory_v1._failure(text, integer, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_directory_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_directory_v1._strip_unicode_space(text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_directory_v1._valid_payload(text, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_directory_v1._directory_user(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_directory_v1._scope_user_ids(
    uuid, text, text, jsonb, boolean
) OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_directory_v1.directory_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_directory_v1.execute_directory_command(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA aap_identity_directory_v1 FROM :"identity_owner";

ALTER FUNCTION aap_identity_admin_passwords_v1._failure(text, integer, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_admin_passwords_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_admin_passwords_v1.admin_password_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_admin_passwords_v1.execute_admin_password_command(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA aap_identity_admin_passwords_v1 FROM :"identity_owner";
ALTER FUNCTION aap_identity_platform_roles_v1._failure(text, integer, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_platform_roles_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_platform_roles_v1.platform_role_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_platform_roles_v1.execute_platform_role_command(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_platform_roles_v1._preview_snapshot(uuid)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA aap_identity_platform_roles_v1 FROM :"identity_owner";
ALTER FUNCTION aap_identity_suspensions_v1._failure(text, integer, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_suspensions_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_suspensions_v1.suspension_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_suspensions_v1.execute_suspension_command(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA aap_identity_suspensions_v1 FROM :"identity_owner";
ALTER FUNCTION aap_identity_deletions_v1._failure(text, integer, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_deletions_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_deletions_v1.deletion_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_deletions_v1.execute_deletion_command(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA aap_identity_deletions_v1 FROM :"identity_owner";
ALTER FUNCTION aap_identity_offboarding_v1._failure(text, integer, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_offboarding_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_offboarding_v1.offboarding_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_offboarding_v1.prepare_offboarding_command(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_offboarding_v1.apply_offboarding_command(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA aap_identity_offboarding_v1 FROM :"identity_owner";
ALTER FUNCTION aap_identity_briefs_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_briefs_v1._failure() OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_briefs_v1.briefs_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_briefs_v1.execute_briefs_command(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA aap_identity_briefs_v1 FROM :"identity_owner";

GRANT CREATE ON SCHEMA aap_workspace_v1 TO :"workspace_owner";
GRANT USAGE, CREATE ON SCHEMA aap_workspace_membership_v1
    TO :"workspace_owner";
GRANT USAGE, CREATE ON SCHEMA aap_workspace_owner_transfer_v1
    TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.lock_project_reference(uuid) OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.lock_project_reference_nowait(uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.read_project_row(uuid) OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.actor_project_memberships(uuid) OWNER TO :"workspace_owner";
ALTER VIEW aap_workspace_v1.project_member_facts OWNER TO :"workspace_owner";
ALTER VIEW aap_workspace_v1.project_facts OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.lock_project_references_for_update(uuid[])
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.lock_project_member_reference(uuid, uuid, boolean)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.project_member_rows(uuid, uuid[], boolean)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1._project_read_failure(text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.read_project_visibility(uuid, uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.lock_project_manager(uuid, uuid) OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.project_access_step(uuid, uuid, text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.lock_project_row(uuid, boolean)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.clear_primary_backend_bindings(uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.read_import_project(text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.create_import_project(uuid,text,text,uuid,jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.create_point_cloud_seed_project(uuid,text,text,uuid,jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.write_seed_members(jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.pin_seed_member_assignment(uuid,timestamptz)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.write_point_cloud_quality_config(uuid,jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.create_media_seed_project(uuid,text,text,uuid,text,jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.write_seed_project_projection(uuid,jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.create_scale_seed_project(uuid,text) OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.ensure_scale_seed_member(uuid,uuid,uuid) OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.guard_scale_seed_cleanup(text) OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.delete_scale_seed_projects(text,uuid[]) OWNER TO :"workspace_owner";

GRANT CREATE ON SCHEMA aap_inference_backend_probe_v1 TO :"inference_owner";
ALTER SCHEMA aap_inference_backend_probe_v1 OWNER TO :"inference_owner";
ALTER TABLE aap_inference_backend_probe_v1.probe_secret OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_backend_probe_v1.backend_probe_policy()
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_backend_probe_v1.open_backend_probe_subject(text, uuid)
    OWNER TO :"inference_owner";
REVOKE CREATE ON SCHEMA aap_inference_backend_probe_v1 FROM :"inference_owner";

GRANT CREATE ON SCHEMA aap_inference_health_observation_v1 TO :"inference_owner";
ALTER SCHEMA aap_inference_health_observation_v1 OWNER TO :"inference_owner";
ALTER TABLE aap_inference_health_observation_v1.observation_secret OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_health_observation_v1.health_observation_policy() OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_health_observation_v1.capture_health_observation_subject(uuid,uuid,text,text,double precision) OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_health_observation_v1.open_health_observation_subject(text,uuid,uuid) OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_health_observation_v1.seal_health_observation(text,uuid,uuid,text) OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_health_observation_v1.verify_health_observation(text,uuid,uuid,text) OWNER TO :"inference_owner";
REVOKE CREATE ON SCHEMA aap_inference_health_observation_v1 FROM :"inference_owner";

GRANT CREATE ON SCHEMA aap_inference_health_data_v1 TO :"inference_owner";
ALTER SCHEMA aap_inference_health_data_v1 OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_health_data_v1.health_data_policy() OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_health_data_v1.backend_row(uuid,boolean) OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_health_data_v1.membership_row(uuid,text,boolean) OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_health_data_v1.observation_clock() OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_health_data_v1.lock_resource(text) OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_health_data_v1.try_promotion() OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_health_data_v1.write_backend(uuid,text,timestamp with time zone,text,boolean,boolean) OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_health_data_v1.member_pool(uuid) OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_health_data_v1.pool_row(uuid,uuid) OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_health_data_v1.member_row(uuid) OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_health_data_v1.apply_reconciliation(uuid,uuid,text,text,text,bigint,text,text) OWNER TO :"inference_owner";
REVOKE CREATE ON SCHEMA aap_inference_health_data_v1 FROM :"inference_owner";

GRANT CREATE ON SCHEMA aap_inference_registry_ingress_v1 TO :"inference_owner";
ALTER SCHEMA aap_inference_registry_ingress_v1 OWNER TO :"inference_owner";
ALTER TABLE aap_inference_registry_ingress_v1.env_sync_receipts
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_registry_ingress_v1.registry_ingress_policy()
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_registry_ingress_v1.registry_by_url(text)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_registry_ingress_v1.registry_candidates(text)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_registry_ingress_v1.create_registry(
    uuid, text, text, text, boolean, text
) OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_registry_ingress_v1.singleton_pool(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_registry_ingress_v1.create_singleton_pool(
    uuid, uuid, uuid, text, text, text
) OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_registry_ingress_v1.apply_observation(
    uuid, text, text, text, boolean, text, timestamptz, text, boolean, text
) OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_registry_ingress_v1.disconnect_env_registry(
    uuid, text
) OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_registry_ingress_v1.tag_seed_registry(uuid, text)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_registry_ingress_v1.sync_result(uuid, text)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_registry_ingress_v1.record_sync_result(
    uuid, text, integer
) OWNER TO :"inference_owner";
REVOKE CREATE ON SCHEMA aap_inference_registry_ingress_v1 FROM :"inference_owner";

GRANT CREATE ON SCHEMA aap_inference_routing_v1 TO :"inference_owner";
ALTER SCHEMA aap_inference_routing_v1 OWNER TO :"inference_owner";
ALTER TABLE aap_inference_routing_v1.authority_secret OWNER TO :"inference_owner";
ALTER TABLE aap_inference_routing_v1.registry_state OWNER TO :"inference_owner";
ALTER TABLE aap_inference_routing_v1.scopes OWNER TO :"inference_owner";
ALTER TABLE aap_inference_routing_v1.attempts OWNER TO :"inference_owner";
ALTER TABLE aap_inference_routing_v1.weights OWNER TO :"inference_owner";
ALTER TABLE aap_inference_routing_v1.controls OWNER TO :"inference_owner";
ALTER TABLE aap_inference_routing_v1.control_attempts OWNER TO :"inference_owner";
ALTER TABLE aap_inference_routing_v1.command_receipts OWNER TO :"inference_owner";
ALTER TABLE aap_inference_routing_v1.scope_retirements OWNER TO :"inference_owner";
ALTER TABLE aap_inference_routing_v1.video_execution_scope_permits
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1.routing_policy()
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1.routing_step(text, text)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_step_0279(text, text)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1.scope_retirement_step(text, text)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._scope_retirement_step_0280(text, text)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1.video_execution_scope_step(text, text)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._video_execution_canonical_json(jsonb)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._video_execution_scope_uuid(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._video_execution_result(jsonb,text)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._video_execution_no_scope_rejection(uuid,uuid,text)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._video_execution_permit_scope()
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._video_execution_attempt_gate()
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._video_execution_heartbeat_gate()
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._scope_retirement_result(text, jsonb)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._scope_retirement_rejected(text)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._scope_retirement_view(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._scope_retirement_context(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_scope_retirement_guard_state()
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_scope_retirement_guard_control()
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_exact_keys(jsonb, text[])
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_uuid(text)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_integer(jsonb, bigint, bigint)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_reply(text, jsonb)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_rejected(text)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_receipt_safe(jsonb)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_realm()
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_secret()
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_base64url(bytea)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_unbase64url(text)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_sign_snapshot(jsonb)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_open_snapshot(text)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_scope_capability(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_control_capability(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_scope_view(uuid, boolean)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_control_view(uuid, boolean)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_transport_identity(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_max_concurrency(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_sign_admission(
    uuid, uuid, uuid, text, text, text
) OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_lock_admission_pool(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_has_unresolved(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_has_control(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_registry_after_insert()
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_registry_before_change()
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_pool_before_change()
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_pool_after_generation()
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_member_before_change()
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1._routing_project_pool_before_change()
    OWNER TO :"inference_owner";
REVOKE CREATE ON SCHEMA aap_inference_routing_v1 FROM :"inference_owner";
ALTER FUNCTION aap_workspace_v1.lock_group_assignment_scope(uuid, uuid[], boolean)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.invitation_projects(uuid[])
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.lock_invitation_project(uuid, uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.read_invitation_membership(uuid, uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.grant_invitation_membership(uuid, uuid, text, uuid, uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.identity_directory_owned_member_ids(uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.identity_directory_project_member_ids(uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.identity_directory_lifecycle_blocked(uuid, uuid[])
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.admin_password_target_in_owned_project(uuid, uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.identity_platform_role_facts(uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.lock_identity_platform_role_owned_projects(uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.lock_identity_suspension_scope(uuid, uuid, text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.lock_identity_deletion_scope(uuid, uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.identity_deletion_transfer_covered(uuid, uuid[])
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.lock_identity_offboarding_scope(uuid, uuid[], uuid[])
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.identity_offboarding_replay_scope(uuid, uuid[])
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.identity_offboarding_facts(uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.transfer_identity_offboarding_owners(uuid, jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.identity_brief_project_roles(uuid, uuid[])
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1._project_delete_failure(text) OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.delete_project(uuid, uuid, uuid, jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1._dataset_unlink_failure(text, text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.preview_dataset_unlink(uuid, uuid, uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.unlink_dataset(uuid, uuid, uuid, uuid, text, jsonb)
    OWNER TO :"workspace_owner";
ALTER VIEW aap_workspace_v1.project_catalog_configs OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA aap_workspace_v1 FROM :"workspace_owner";
ALTER FUNCTION aap_workspace_membership_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_membership_v1._failure(integer, jsonb, jsonb, boolean)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_membership_v1._request_is_valid(jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_membership_v1._membership_record(uuid, uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_membership_v1.membership_policy()
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_membership_v1.prepare_membership_command(jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_membership_v1.apply_membership_command(jsonb)
    OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA aap_workspace_membership_v1 FROM :"workspace_owner";
ALTER FUNCTION aap_workspace_owner_transfer_v1._failure(integer, jsonb, boolean)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_owner_transfer_v1.owner_transfer_policy()
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_owner_transfer_v1.transfer_project_owner(jsonb)
    OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA aap_workspace_owner_transfer_v1 FROM :"workspace_owner";
GRANT CREATE ON SCHEMA aap_workspace_configuration_v1 TO :"workspace_owner";
ALTER FUNCTION aap_workspace_configuration_v1.configuration_policy()
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_configuration_v1.configuration_step(jsonb, text, jsonb, text[], text[])
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_configuration_v1._step_success(jsonb, text, jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_configuration_v1._step_failure(jsonb, text, integer, jsonb, jsonb, boolean)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_configuration_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_configuration_v1._configuration_actor(uuid, boolean, text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_configuration_v1._class_rename_project_row(uuid, text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_configuration_v1._class_rename_member_rows(uuid, uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_configuration_v1._guide_asset_read_project_row(uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_configuration_v1._guide_asset_read_member_rows(uuid, uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_configuration_v1._configuration_project_snapshot(uuid, text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_configuration_v1._configuration_project_access(jsonb, jsonb, boolean)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_configuration_v1._configuration_template_snapshot(uuid, boolean)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_configuration_v1._configuration_template_visible(jsonb, jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_configuration_v1._guard_unfinished_attempt()
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_configuration_v1._cleanup_unwritten_state()
    OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA aap_workspace_configuration_v1 FROM :"workspace_owner";

GRANT CREATE ON SCHEMA aap_workspace_template_query_v1 TO :"workspace_owner";
ALTER FUNCTION aap_workspace_template_query_v1.template_query_policy()
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_template_query_v1.template_query_step(jsonb, text, jsonb, text, text[], text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_template_query_v1._template_row_json(public.project_templates)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_template_query_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA aap_workspace_template_query_v1 FROM :"workspace_owner";

GRANT CREATE ON SCHEMA aap_workspace_project_query_v1 TO :"workspace_owner";
ALTER FUNCTION aap_workspace_project_query_v1.project_query_policy()
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_project_query_v1.project_query_step(
    jsonb, text, jsonb, text, text, text[], text[], uuid, text,
    timestamptz, timestamptz, bigint, integer
) OWNER TO :"workspace_owner";
DO $$
DECLARE
    implementation regprocedure := pg_catalog.to_regprocedure(
        'aap_workspace_project_query_v1._project_query_step_impl(jsonb, text, jsonb, text, text, text[], text[], uuid, text, timestamptz, timestamptz, bigint, integer)'
    );
BEGIN
    IF implementation IS NOT NULL THEN
        EXECUTE pg_catalog.format(
            'ALTER FUNCTION %s OWNER TO %I',
            implementation,
            current_setting('aap.workspace_owner')
        );
    END IF;
END $$;
ALTER FUNCTION aap_workspace_project_query_v1.project_search_scope(uuid, text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_project_query_v1.project_search_member_ids(uuid, text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_project_query_v1._project_row_json(
    jsonb, integer, integer, integer, integer, text
) OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_project_query_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA aap_workspace_project_query_v1 FROM :"workspace_owner";

GRANT CREATE ON SCHEMA aap_workspace_directory_query_v1 TO :"workspace_owner";
ALTER FUNCTION aap_workspace_directory_query_v1.directory_query_policy()
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_directory_query_v1.directory_query_step(jsonb, text, jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_directory_query_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA aap_workspace_directory_query_v1 FROM :"workspace_owner";
ALTER FUNCTION aap_identity_v1.workspace_directory_superadmins()
    OWNER TO :"identity_owner";

ALTER FUNCTION aap_identity_v1.read_workspace_membership_accounts(uuid[])
    OWNER TO :"identity_owner";
ALTER FUNCTION aap_audit_v1.record_workspace_membership_event(
    text, jsonb, uuid, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION aap_labeling_v1.read_membership_work(uuid, uuid, uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.lock_membership_work(uuid, uuid, text, boolean)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.handoff_membership_annotation(uuid, uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.handoff_membership_reviewer(uuid, uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.release_membership_task_locks(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1._membership_python_repr(jsonb)
    OWNER TO :"labeling_owner";

ALTER FUNCTION aap_labeling_v1.invitation_work_summary(uuid, uuid, text)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.identity_directory_project_refs(uuid[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.identity_platform_role_facts(uuid, uuid[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.identity_deletion_work(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.transfer_identity_deletion_work(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.lock_identity_offboarding_work(uuid, uuid[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.identity_offboarding_facts(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.transfer_identity_offboarding_work(uuid, jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.release_identity_offboarding_task_locks(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_audit_v1.record_identity_admin_password_reset(
    jsonb, uuid, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.record_identity_platform_role_change(
    jsonb, uuid, jsonb, text, text, text
) OWNER TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.record_identity_suspension_event(
    text, jsonb, uuid, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.record_identity_bootstrap_admin(uuid, text, text)
    OWNER TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.record_identity_deletion_event(
    jsonb, uuid, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION aap_settings_v1.invitation_policy(jsonb)
    OWNER TO :"settings_owner";
ALTER FUNCTION aap_audit_v1.record_identity_invitation_event(
    text, jsonb, jsonb, jsonb, jsonb
) OWNER TO :"audit_owner";

GRANT CREATE ON SCHEMA aap_assets_v1 TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._legacy_upload_reply(jsonb, text) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.lock_legacy_dataset_upload(uuid, uuid, uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.complete_legacy_dataset_upload(
    uuid, uuid, uuid, uuid, text, bigint, text, integer, integer
) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._association_read_failure(text)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.list_dataset_projects(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.list_project_datasets(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.workspace_configuration_datasets(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.workspace_configuration_dataset_rows(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.workspace_configuration_dataset_scenes(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.workspace_configuration_existing_item_ids(uuid[])
    OWNER TO :"assets_owner";
ALTER VIEW aap_assets_v1.project_dataset_item_sources OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.lock_dataset_reference(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.lock_current_association(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.lock_append_sources(uuid, uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.associate_project(uuid, uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.lock_task_creation_items(uuid, uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.lock_scene_source_references(uuid[])
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.delete_project_owned_records(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.dataset_item_references(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.dataset_item_scene_references(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.guard_dataset_source_auxiliary_retention(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.guard_scale_seed_dataset_cleanup(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.dataset_scene_references(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.remove_dataset_association(uuid, uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.prepare_dataset_unlink_cleanup(uuid, uuid, uuid, text)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.pending_storage_deletions(uuid, integer)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.complete_storage_deletion(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._dataset_upload_failure(text)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._dataset_upload_success(jsonb)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._dataset_upload_actor(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._dataset_upload_facts(public.dataset_upload_intents)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._accept_dataset_upload(
    uuid, uuid, uuid, uuid, bigint, text, integer, integer
) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._finish_dataset_upload(uuid, uuid, uuid, uuid, bigint)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.reserve_dataset_upload(uuid, uuid, uuid, text, text)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.lock_dataset_upload(uuid, uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.find_dataset_upload_duplicate(uuid, uuid, uuid, text)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.complete_dataset_upload(
    uuid, uuid, uuid, uuid, bigint, text, integer, integer
) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.discard_dataset_upload(uuid, uuid, uuid, text)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._source_delete_failure(text)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.dataset_association_references(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._lock_dataset_source_deletion(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._queue_pyramid_deletions(uuid, uuid[], text)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._delete_dataset_source_records(uuid, uuid, uuid, text)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._delete_dataset_source(
    uuid, uuid, uuid, uuid, text, jsonb
) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.delete_dataset(uuid, uuid, uuid, text, jsonb)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.delete_dataset_item(
    uuid, uuid, uuid, uuid, text, jsonb
) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.pending_storage_deletions_v2(
    text, timestamptz, uuid, uuid, integer
) OWNER TO :"assets_owner";
DO $$
DECLARE
    target record;
    owner_name text := current_setting('aap.assets_owner');
BEGIN
    FOR target IN
        SELECT proc.oid::regprocedure AS signature
        FROM pg_catalog.pg_proc AS proc
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = proc.pronamespace
        WHERE namespace.nspname = 'aap_assets_v1'
          AND proc.prokind IN ('f', 'p')
          AND proc.proname LIKE '%image%'
    LOOP
        EXECUTE pg_catalog.format(
            'ALTER FUNCTION %s OWNER TO %I', target.signature, owner_name
        );
    END LOOP;
END $$;
ALTER FUNCTION aap_assets_v1.claim_accepted_input_garbage(uuid, integer, integer)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.complete_accepted_input_garbage(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._accepted_input_garbage_immutable_scope()
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.workspace_project_query_dataset_ids(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.workspace_project_query_dataset_item_count(uuid[])
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.workspace_project_query_dataset_item_ids(uuid[])
    OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA aap_assets_v1 FROM :"assets_owner";

GRANT CREATE ON SCHEMA aap_labeling_class_query_v1 TO :"labeling_owner";
ALTER FUNCTION aap_labeling_class_query_v1.class_query_policy() OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_class_query_v1.class_counts(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_class_query_v1.attribute_rows(uuid) OWNER TO :"labeling_owner";
REVOKE CREATE ON SCHEMA aap_labeling_class_query_v1 FROM :"labeling_owner";

GRANT CREATE ON SCHEMA aap_labeling_v1 TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.append_dataset_items(uuid, uuid[], uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.accept_dataset_task_creation(
    uuid, uuid, uuid, uuid, uuid, uuid, text, integer
) OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.prepare_dataset_task_creation(uuid, jsonb) OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.read_dataset_task_creation(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.advance_dataset_task_creation(uuid, integer) OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.project_task_references(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.delete_project_owned_records(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1._dataset_unlink_task_snapshot(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.dataset_task_references(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.lock_dataset_unlink_scope(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.guard_dataset_unlink(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.preview_dataset_unlink(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.delete_dataset_tasks(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.guard_dataset_source_deletion(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.detach_deleted_dataset_attribution(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.project_dataset_task_counts(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.workspace_configuration_video_facts(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.rename_project_annotation_class(uuid, text, text)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.workspace_configuration_task_count(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.workspace_configuration_annotation_count(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.workspace_configuration_tracker_count(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.workspace_configuration_video_items(uuid)
    OWNER TO :"labeling_owner";
ALTER VIEW aap_labeling_v1.project_catalog_counters OWNER TO :"labeling_owner";
DO $$
DECLARE
    target record;
    owner_name text := current_setting('aap.labeling_owner');
BEGIN
    FOR target IN
        SELECT proc.oid::regprocedure AS signature
        FROM pg_catalog.pg_proc AS proc
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = proc.pronamespace
        WHERE namespace.nspname = 'aap_labeling_v1'
          AND proc.prokind IN ('f', 'p')
          AND proc.proname LIKE '%image%'
    LOOP
        EXECUTE pg_catalog.format(
            'ALTER FUNCTION %s OWNER TO %I', target.signature, owner_name
        );
    END LOOP;
END $$;
DO $$
DECLARE
    target record;
    owner_name text := current_setting('aap.labeling_owner');
BEGIN
    FOR target IN
        SELECT proc.oid::regprocedure AS signature
        FROM pg_catalog.pg_proc AS proc
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = proc.pronamespace
        WHERE namespace.nspname = 'aap_labeling_v1'
          AND proc.prokind = 'f'
          AND (
              proc.proname LIKE '%mask_gc%'
              OR proc.proname IN (
                  'read_mask_write_epochs',
                  'begin_mask_content_write',
                  'lock_mask_write_epochs',
                  'register_tracker_running_mask_root'
              )
          )
    LOOP
        EXECUTE pg_catalog.format(
            'ALTER FUNCTION %s OWNER TO %I', target.signature, owner_name
        );
    END LOOP;
END $$;
ALTER FUNCTION aap_labeling_v1.attach_annotation_input_source(uuid, uuid, text)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1._annotation_input_sources_reply(jsonb, text)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.copy_annotation_input_sources(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1._annotation_input_source_immutable()
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1._inherit_annotation_input_sources()
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1._propagate_annotation_input_source()
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1._video_input_task_source_reply(jsonb, text)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1._video_input_task_binding_valid(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.read_video_input_task_source(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.lock_video_input_task_source(uuid, uuid, jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1._video_input_selection_reply(jsonb, text)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1._video_input_manifest_selection_valid(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1._video_input_captured_task_valid(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1._video_input_selection_task_access(uuid, uuid, uuid, uuid, text)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.open_video_input_manifest_members(uuid, uuid, jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.lock_video_input_manifest_member(uuid, uuid, uuid, jsonb, jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.workspace_project_query_counters(uuid[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.workspace_project_query_ai_counts(uuid[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.workspace_project_query_annotation_count(uuid[], boolean)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.workspace_project_query_task_series(uuid[], timestamptz[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.workspace_project_query_annotation_series(uuid[], timestamptz[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.workspace_project_query_linked_task_count(uuid, uuid[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.workspace_project_query_task_count(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.workspace_project_query_batches(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.workspace_project_query_batch_task_counts(uuid)
    OWNER TO :"labeling_owner";
REVOKE CREATE ON SCHEMA aap_labeling_v1 FROM :"labeling_owner";

GRANT CREATE ON SCHEMA aap_jobs_v1 TO :"jobs_owner";
ALTER VIEW aap_jobs_v1.notification_scope_facts OWNER TO :"jobs_owner";
ALTER SCHEMA aap_jobs_v1 OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.count_running_registry_predictions(uuid)
    OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._accepted_image_input_job_reply(jsonb, text)
    OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_input_job_reply(jsonb, text)
    OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_input_selection_valid(jsonb)
    OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.project_video_input_job(uuid, uuid, uuid, uuid, jsonb, text, boolean)
    OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.project_accepted_image_input_job(uuid, uuid, uuid, boolean)
    OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.accepted_image_input_job_consistency(uuid, uuid, uuid, uuid)
    OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._accepted_image_input_manifest_reply(jsonb, text)
    OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.project_accepted_image_input_manifest_job(
    uuid, uuid, uuid, uuid, jsonb, text, boolean
) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.accepted_image_input_manifest_job_consistency(
    uuid, uuid, uuid, uuid, jsonb, text
) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._task_creation_job_failure(text, text) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._task_creation_job_payload_valid(uuid, jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.create_task_creation_job(uuid, uuid, uuid, uuid, uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.find_task_creation_job(uuid, uuid, uuid, uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.claim_task_creation_job(uuid, text) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.lock_task_creation_job(uuid, uuid, uuid, uuid, uuid, uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.update_task_creation_progress(uuid, uuid, bigint, bigint, bigint) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.finish_task_creation_job(uuid, uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.fail_task_creation_job(uuid, uuid, text) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.cancel_task_creation_job(uuid, uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.detach_project_references(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.retain_async_job_project_scope() OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.cancel_dataset_task_creation(uuid, uuid, uuid)
    OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.guard_dataset_source_deletion(uuid)
    OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.task_creation_execution_active(uuid, uuid)
    OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.workspace_project_query_job_status_counts(uuid)
    OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.workspace_project_query_latest_job_status(uuid)
    OWNER TO :"jobs_owner";
ALTER TABLE aap_jobs_v1.video_execution_authority OWNER TO :"jobs_owner";
ALTER TABLE aap_jobs_v1.video_executions OWNER TO :"jobs_owner";
ALTER TABLE aap_jobs_v1.video_execution_route_intents OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.video_execution_step(text,text) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_execution_route_step(text,text)
    OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_execution_scope_is_current(uuid,boolean)
    OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_execution_claim_view(uuid,uuid)
    OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_execution_secret() OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_execution_realm() OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_execution_exact_keys(jsonb,text[])
    OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_execution_uuid(jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_execution_reply(jsonb,text) OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA aap_jobs_v1 FROM :"jobs_owner";

GRANT CREATE ON SCHEMA aap_learning_v1 TO :"learning_owner";
ALTER FUNCTION aap_learning_v1.registry_evaluation_reference_exists(uuid)
    OWNER TO :"learning_owner";
ALTER FUNCTION aap_learning_v1.lock_project_deletion(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION aap_learning_v1.guard_project_deletion(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION aap_learning_v1.delete_project_owned_records(uuid)
    OWNER TO :"learning_owner";
ALTER FUNCTION aap_learning_v1.guard_dataset_item_deletion(uuid, uuid)
    OWNER TO :"learning_owner";
ALTER FUNCTION aap_learning_v1.guard_dataset_unlink(uuid, uuid)
    OWNER TO :"learning_owner";
ALTER FUNCTION aap_learning_v1.guard_dataset_source_deletion(uuid, uuid)
    OWNER TO :"learning_owner";
REVOKE CREATE ON SCHEMA aap_learning_v1 FROM :"learning_owner";

GRANT CREATE ON SCHEMA aap_inference_v1 TO :"inference_owner";
ALTER FUNCTION aap_inference_v1.clear_project_ml_backend_pool(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_v1.clear_project_pipelines(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_v1.enable_workspace_project_backend(uuid, uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_v1.workspace_configuration_source_pool(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_v1.workspace_configuration_backend(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_v1.workspace_project_query_pool(uuid)
    OWNER TO :"inference_owner";
REVOKE CREATE ON SCHEMA aap_inference_v1 FROM :"inference_owner";

GRANT CREATE ON SCHEMA aap_inference_pipelines_v1 TO :"inference_owner";
ALTER SCHEMA aap_inference_pipelines_v1 OWNER TO :"inference_owner";
ALTER TABLE aap_inference_pipelines_v1.operation_states OWNER TO :"inference_owner";
ALTER TABLE aap_inference_pipelines_v1.operation_attempts OWNER TO :"inference_owner";
ALTER TABLE aap_inference_pipelines_v1.operation_receipts OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_pipelines_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_pipelines_v1._actor_is_valid(jsonb)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_pipelines_v1._command_is_valid(jsonb)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_pipelines_v1._query_is_valid(jsonb)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_pipelines_v1._pipeline_row_value(public.project_pipelines)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_pipelines_v1._step_success(uuid, text, text, uuid, uuid, jsonb)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_pipelines_v1._step_failure(uuid, text, text, uuid, uuid, integer, jsonb, jsonb, boolean)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_pipelines_v1._guard_unfinished_operation()
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_pipelines_v1._cleanup_unfinished_state()
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_pipelines_v1.pipeline_policy()
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_pipelines_v1.pipeline_step(jsonb, text, jsonb, text[], text[])
    OWNER TO :"inference_owner";
REVOKE CREATE ON SCHEMA aap_inference_pipelines_v1 FROM :"inference_owner";

GRANT CREATE ON SCHEMA aap_insights_v1 TO :"insights_owner";
ALTER FUNCTION aap_insights_v1.clear_project_task_views(uuid)
    OWNER TO :"insights_owner";
REVOKE CREATE ON SCHEMA aap_insights_v1 FROM :"insights_owner";

GRANT CREATE ON SCHEMA aap_diagnostics_v1 TO :"diagnostics_owner";
ALTER FUNCTION aap_diagnostics_v1.detach_project_references(uuid)
    OWNER TO :"diagnostics_owner";
ALTER FUNCTION aap_diagnostics_v1.detach_dataset_task_references(uuid, uuid)
    OWNER TO :"diagnostics_owner";
REVOKE CREATE ON SCHEMA aap_diagnostics_v1 FROM :"diagnostics_owner";

GRANT CREATE ON SCHEMA aap_export_v1 TO :"export_owner";
ALTER FUNCTION aap_export_v1.clear_project_export_artifacts(uuid)
    OWNER TO :"export_owner";
REVOKE CREATE ON SCHEMA aap_export_v1 FROM :"export_owner";

GRANT CREATE ON SCHEMA public TO :"audit_owner";
ALTER MATERIALIZED VIEW public.mv_audit_bi_daily OWNER TO :"audit_owner";
REVOKE CREATE ON SCHEMA public FROM :"audit_owner";
-- Concurrent refresh creates transient relations as the fixed MV owner.
GRANT TEMPORARY ON DATABASE :"expected_database" TO :"audit_owner";
GRANT CREATE ON SCHEMA aap_audit_v1 TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.record_identity_session_event(
    text, jsonb, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.record_identity_account_event(
    text, jsonb, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.record_identity_recovery_event(
    text, jsonb, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.record_identity_group_event(
    text, jsonb, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.record_identity_deactivation_event(
    text, jsonb, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.record_identity_offboarding_event(
    jsonb, uuid, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.record_identity_directory_export(
    jsonb, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.record_project_deletion(
    uuid, uuid, uuid, text, text, bigint, bigint, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.valid_request_context(jsonb)
    OWNER TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.record_dataset_unlink(
    uuid, uuid, uuid, uuid, bigint, bigint, uuid[], jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.record_dataset_source_deletion(
    uuid, uuid, uuid, uuid, bigint, bigint, bigint, bigint, bigint, bigint, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.append_events(jsonb) OWNER TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.query_events(pg_catalog.jsonb) OWNER TO :"audit_owner";
ALTER VIEW aap_audit_v1.audit_events OWNER TO :"audit_owner";
ALTER VIEW aap_audit_v1.audit_daily_facts OWNER TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.refresh_audit_bi_mv() OWNER TO :"audit_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.refresh_audit_bi_mv() TO :"migration_role";
REVOKE CREATE ON SCHEMA aap_audit_v1 FROM :"audit_owner";

-- Import and spatial helpers stay with their schema owner; runtime roles gain no DDL.
GRANT CREATE ON SCHEMA aap_assets_v1 TO :"assets_owner";
GRANT CREATE ON SCHEMA aap_labeling_v1 TO :"labeling_owner";
GRANT CREATE ON SCHEMA aap_workspace_v1 TO :"workspace_owner";
GRANT CREATE ON SCHEMA aap_jobs_v1 TO :"jobs_owner";
GRANT CREATE ON SCHEMA aap_audit_v1 TO :"audit_owner";
GRANT CREATE ON SCHEMA aap_settings_v1 TO :"settings_owner";
GRANT CREATE ON SCHEMA aap_notifications_v1 TO :"notifications_owner";
ALTER VIEW aap_notifications_v1.notification_facts OWNER TO :"notifications_owner";
DO $$
DECLARE
    capability record;
BEGIN
    FOR capability IN
        SELECT proc.oid, scopes.owner_key
        FROM pg_catalog.pg_proc AS proc
        JOIN pg_catalog.pg_namespace AS ns ON ns.oid=proc.pronamespace
        JOIN (VALUES
            ('aap_assets_v1','aap.assets_owner'),
            ('aap_labeling_v1','aap.labeling_owner'),
            ('aap_workspace_v1','aap.workspace_owner'),
            ('aap_jobs_v1','aap.jobs_owner'),
            ('aap_audit_v1','aap.audit_owner'),
            ('aap_settings_v1','aap.settings_owner'),
            ('aap_notifications_v1','aap.notifications_owner')
        ) AS scopes(schema_name,owner_key) ON scopes.schema_name=ns.nspname
        WHERE proc.prokind='f'
          AND (proc.proname LIKE '%dataset_import%'
               OR proc.proname LIKE '%dataset_ingest%'
               OR proc.proname LIKE '%source_ingest%'
               OR proc.proname LIKE '%spatial%'
               OR proc.proname LIKE '%pointcloud_preparation%'
               OR proc.proname LIKE '%sensor_calibration%'
               OR proc.proname LIKE '%storage_connection%'
               OR proc.proname LIKE '%operator%'
               OR proc.proname LIKE '%media%'
               OR (ns.nspname = 'aap_assets_v1' AND proc.proname LIKE '%video_%')
               OR (ns.nspname = 'aap_labeling_v1' AND proc.proname LIKE '%video_input%')
               OR (ns.nspname = 'aap_labeling_v1' AND proc.proname LIKE '%video_tracker%')
               OR ns.nspname IN ('aap_settings_v1','aap_notifications_v1'))
    LOOP
        EXECUTE pg_catalog.format('ALTER FUNCTION %s OWNER TO %I',
            capability.oid::pg_catalog.regprocedure,
            current_setting(capability.owner_key));
    END LOOP;
END $$;
REVOKE CREATE ON SCHEMA aap_assets_v1 FROM :"assets_owner";
REVOKE CREATE ON SCHEMA aap_labeling_v1 FROM :"labeling_owner";
REVOKE CREATE ON SCHEMA aap_workspace_v1 FROM :"workspace_owner";
REVOKE CREATE ON SCHEMA aap_jobs_v1 FROM :"jobs_owner";
REVOKE CREATE ON SCHEMA aap_audit_v1 FROM :"audit_owner";
REVOKE CREATE ON SCHEMA aap_settings_v1 FROM :"settings_owner";
REVOKE CREATE ON SCHEMA aap_notifications_v1 FROM :"notifications_owner";

-- Cross-owner spatial authority and audit stay behind their owner capabilities.
GRANT CREATE ON SCHEMA aap_assets_v1 TO :"assets_owner";
ALTER VIEW aap_assets_v1.spatial_item_references OWNER TO :"assets_owner";
ALTER VIEW aap_assets_v1.spatial_scene_references OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA aap_assets_v1 FROM :"assets_owner";
GRANT USAGE ON SCHEMA aap_assets_v1 TO :"insights_owner";
GRANT SELECT ON aap_assets_v1.spatial_item_references,
    aap_assets_v1.spatial_scene_references
    TO :"labeling_owner", :"insights_owner", :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.read_spatial_project_access(uuid,uuid)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    aap_workspace_v1.read_media_project_access(uuid,uuid),
    aap_workspace_v1.lock_media_project_access(uuid,uuid),
    aap_workspace_v1.read_media_video_collaboration_policy(uuid),
    aap_workspace_v1.read_media_managed_projects(uuid,uuid[])
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    aap_labeling_v1.read_spatial_camera_roles(uuid[]),
    aap_labeling_v1.read_spatial_camera_target(uuid,uuid,text),
    aap_labeling_v1.lock_spatial_camera_authority(uuid,uuid,text,uuid),
    aap_labeling_v1.lock_spatial_preparation_scope(uuid),
    aap_labeling_v1.read_spatial_preparation_scope(uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    aap_workspace_v1.read_media_project_access(uuid,uuid),
    aap_workspace_v1.lock_media_project_access(uuid,uuid),
    aap_workspace_v1.read_media_managed_projects(uuid,uuid[]),
    aap_labeling_v1.read_media_task_access(uuid,uuid,uuid),
    aap_labeling_v1.read_media_visible_video_item(uuid,uuid),
    aap_labeling_v1.lock_media_task_annotation_write(uuid,uuid,uuid),
    aap_labeling_v1.read_media_failure_context(uuid,uuid[]),
    aap_settings_v1.read_video_media_preferences()
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    aap_audit_v1.record_sensor_calibration_update(uuid,uuid,uuid,uuid,text,integer,text,jsonb),
    aap_audit_v1.record_storage_connection_change(uuid,uuid,text,uuid,jsonb,jsonb)
    TO :"assets_owner";
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
    TO :"workflow_role";

DO $$
DECLARE
    target text;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'public.users', 'public.projects', 'public.project_members',
        'public.project_templates', 'public.project_deletion_operations',
        'public.dataset_unlink_operations', 'public.dataset_source_deletion_operations',
        'public.dataset_upload_intents', 'public.asset_storage_deletions',
        'public.audit_logs', 'public.datasets', 'public.scenes',
        'public.dataset_import_operations','public.dataset_import_entries',
        'public.dataset_import_deliveries','public.notifications',
        'public.dataset_source_ingest_operations',
        'public.dataset_source_ingest_entries', 'public.dataset_source_ingest_deliveries',
        'public.dataset_spatial_operations', 'public.sensor_calibration_revisions',
        'public.dataset_operator_operations',
        'public.scene_frame_poses',
        'public.notification_preferences','public.system_settings',
        'public.dataset_items', 'public.project_datasets',
        'public.storage_connections', 'public.storage_connection_operations',
        'public.image_pyramid_assets',
        'public.asset_accepted_image_inputs',
        'public.asset_accepted_image_input_attempts',
        'public.asset_accepted_input_garbage',
        'public.asset_accepted_image_input_manifests',
        'public.asset_accepted_image_input_manifest_members',
        'public.annotation_input_sources',
        'public.asset_accepted_video_manifests',
        'public.asset_accepted_video_members',
        'public.asset_accepted_video_inputs',
        'public.asset_accepted_video_input_attempts',
        'public.asset_accepted_video_times',
        'public.asset_accepted_video_frames',
        'public.asset_accepted_video_frame_attempts',
        'public.annotation_video_input_sources',
        'public.video_tracker_input_scopes',
        'public.annotation_video_tracker_input_sources',
        'public.tasks', 'public.task_batches', 'public.task_locks',
        'public.predictions', 'public.prediction_metas', 'public.failed_predictions',
        'public.annotations', 'public.annotation_feedbacks', 'public.annotation_drafts',
        'public.annotation_comments', 'public.task_events',
        'public.scene_tracks', 'public.mask_review_scopes',
        'public.mask_qc_runs', 'public.mask_qc_issues',
        'public.mask_annotation_revisions', 'public.mask_repair_batches',
        'public.mask_format_imports', 'public.point_cloud_quality_runs',
        'public.point_cloud_quality_issues', 'public.point_cloud_quality_evaluations',
        'public.task_append_operations',
        'public.task_creation_operations', 'public.task_creation_chunks',
        'public.active_learning_rounds', 'public.active_learning_pool_members',
        'public.active_learning_candidates', 'public.training_studies',
        'public.trained_model_versions', 'public.project_model_selections',
        'public.project_model_releases', 'public.model_decisions',
        'public.sample_evaluation_runs', 'public.learning_policies',
        'public.training_dataset_versions', 'public.training_runs',
        'public.training_previews', 'public.learning_triggers',
        'public.project_task_datasets', 'public.training_recipes',
        'public.training_recipe_revisions', 'public.project_ml_backend_pool',
        'public.project_pipelines', 'public.project_task_views',
        'public.bug_reports', 'public.export_artifacts', 'public.async_jobs'
    ] LOOP
        IF has_any_column_privilege(current_setting('aap.workflow_role'), target, 'SELECT,INSERT,UPDATE,REFERENCES')
           OR has_table_privilege(current_setting('aap.workflow_role'), target, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') THEN
            RAISE EXCEPTION 'Caller has direct privileges on %; use a clean dedicated role', target;
        END IF;
        IF has_any_column_privilege(current_setting('aap.asset_cleanup_role'), target, 'SELECT,INSERT,UPDATE,REFERENCES')
           OR has_table_privilege(current_setting('aap.asset_cleanup_role'), target, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') THEN
            RAISE EXCEPTION 'Asset cleanup role has direct privileges on %; use a clean dedicated role', target;
        END IF;
    END LOOP;

    IF has_database_privilege(current_setting('aap.workflow_role'), current_database(), 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'public', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_identity_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_workspace_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_assets_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_labeling_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_jobs_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_learning_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_inference_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_insights_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_diagnostics_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_export_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_audit_v1', 'CREATE') THEN
        RAISE EXCEPTION 'Caller must not hold schema or database CREATE privileges';
    END IF;

    IF has_database_privilege(current_setting('aap.asset_cleanup_role'), current_database(), 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'public', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_identity_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_workspace_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_assets_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_labeling_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_jobs_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_learning_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_inference_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_insights_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_diagnostics_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_export_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_audit_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_identity_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_workspace_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_labeling_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_jobs_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_learning_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_inference_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_insights_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_diagnostics_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_export_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_audit_v1', 'USAGE') THEN
        RAISE EXCEPTION 'Asset cleanup role must be confined to the Assets schema';
    END IF;

    IF has_function_privilege(current_setting('aap.workflow_role'),
        'aap_jobs_v1.lock_task_creation_job(uuid,uuid,uuid,uuid,uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_jobs_v1.update_task_creation_progress(uuid,uuid,bigint,bigint,bigint)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_jobs_v1._task_creation_job_failure(text,text)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_jobs_v1._task_creation_job_payload_valid(uuid,jsonb)', 'EXECUTE') THEN
        RAISE EXCEPTION 'Caller must not execute Jobs-internal capabilities';
    END IF;

    IF has_function_privilege(current_setting('aap.workflow_role'),
        'aap_identity_v1.lock_active_actor(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_workspace_v1.lock_project_reference(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.lock_dataset_reference(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.lock_current_association(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.lock_append_sources(uuid,uuid[])', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.lock_task_creation_items(uuid,uuid[])', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_identity_v1.lock_audit_actor(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_workspace_v1._project_delete_failure(text)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.delete_project_owned_records(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_labeling_v1.project_task_references(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_labeling_v1.delete_project_owned_records(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_jobs_v1.detach_project_references(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_jobs_v1.retain_async_job_project_scope()', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_learning_v1.lock_project_deletion(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_learning_v1.guard_project_deletion(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_learning_v1.delete_project_owned_records(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_inference_v1.clear_project_ml_backend_pool(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_inference_v1.clear_project_pipelines(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_insights_v1.clear_project_task_views(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_diagnostics_v1.detach_project_references(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_export_v1.clear_project_export_artifacts(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_audit_v1.record_project_deletion(uuid,uuid,uuid,text,text,bigint,bigint,jsonb)', 'EXECUTE') THEN
        RAISE EXCEPTION 'Caller must use only the public project deletion capability';
    END IF;

    IF has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.pending_storage_deletions(uuid,integer)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.pending_storage_deletions_v2(text,timestamptz,uuid,uuid,integer)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.complete_storage_deletion(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_jobs_v1.cancel_dataset_task_creation(uuid,uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_audit_v1.valid_request_context(jsonb)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_audit_v1.record_dataset_unlink(uuid,uuid,uuid,uuid,bigint,bigint,uuid[],jsonb)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_workspace_v1._dataset_unlink_failure(text,text)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.dataset_item_references(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.dataset_scene_references(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.prepare_dataset_unlink_cleanup(uuid,uuid,uuid,text)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.remove_dataset_association(uuid,uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_diagnostics_v1.detach_dataset_task_references(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_learning_v1.guard_dataset_item_deletion(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_learning_v1.guard_dataset_unlink(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_labeling_v1._dataset_unlink_task_snapshot(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_labeling_v1.dataset_task_references(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_labeling_v1.lock_dataset_unlink_scope(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_labeling_v1.guard_dataset_unlink(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_labeling_v1.preview_dataset_unlink(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_labeling_v1.delete_dataset_tasks(uuid,uuid)', 'EXECUTE') THEN
        RAISE EXCEPTION 'Caller must use the Workspace dataset-unlink capability';
    END IF;

    IF has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._accept_dataset_upload(uuid,uuid,uuid,uuid,bigint,text,integer,integer)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._finish_dataset_upload(uuid,uuid,uuid,uuid,bigint)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._dataset_upload_failure(text)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._dataset_upload_success(jsonb)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._dataset_upload_actor(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._dataset_upload_facts(public.dataset_upload_intents)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._delete_dataset_source(uuid,uuid,uuid,uuid,text,jsonb)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._delete_dataset_source_records(uuid,uuid,uuid,text)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._source_delete_failure(text)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._lock_dataset_source_deletion(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._queue_pyramid_deletions(uuid,uuid[],text)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.dataset_association_references(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_jobs_v1.guard_dataset_source_deletion(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_jobs_v1.task_creation_execution_active(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_learning_v1.guard_dataset_source_deletion(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_labeling_v1.guard_dataset_source_deletion(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_labeling_v1.detach_deleted_dataset_attribution(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_audit_v1.record_dataset_source_deletion(uuid,uuid,uuid,uuid,bigint,bigint,bigint,bigint,bigint,bigint,jsonb)', 'EXECUTE') THEN
        RAISE EXCEPTION 'Workflow caller must use the public Assets source/upload capabilities';
    END IF;

    IF NOT has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_assets_v1', 'USAGE')
       OR NOT has_function_privilege(current_setting('aap.asset_cleanup_role'),
        'aap_assets_v1.pending_storage_deletions_v2(text,timestamptz,uuid,uuid,integer)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.asset_cleanup_role'),
        'aap_assets_v1.complete_storage_deletion(uuid,uuid)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.asset_cleanup_role'),
        'aap_assets_v1.claim_image_garbage(uuid,integer,integer)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.asset_cleanup_role'),
        'aap_assets_v1.complete_image_garbage(uuid,uuid)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.asset_cleanup_role'),
        'aap_assets_v1.claim_accepted_input_garbage(uuid,integer,integer)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.asset_cleanup_role'),
        'aap_assets_v1.complete_accepted_input_garbage(uuid,uuid)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.asset_cleanup_role'),
        'aap_assets_v1.claim_accepted_video_input_garbage(uuid,integer,integer)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.asset_cleanup_role'),
        'aap_assets_v1.complete_accepted_video_input_garbage(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.asset_cleanup_role'),
        'aap_assets_v1.pending_storage_deletions(uuid,integer)', 'EXECUTE')
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.pg_proc AS proc
           WHERE proc.pronamespace = 'aap_assets_v1'::pg_catalog.regnamespace
             AND pg_catalog.has_function_privilege(
                 current_setting('aap.asset_cleanup_role'), proc.oid, 'EXECUTE'
             )
             AND proc.oid NOT IN (
                 'aap_assets_v1.pending_storage_deletions_v2(text,timestamptz,uuid,uuid,integer)'::pg_catalog.regprocedure,
                 'aap_assets_v1.complete_storage_deletion(uuid,uuid)'::pg_catalog.regprocedure,
                 'aap_assets_v1.claim_image_garbage(uuid,integer,integer)'::pg_catalog.regprocedure,
                 'aap_assets_v1.complete_image_garbage(uuid,uuid)'::pg_catalog.regprocedure,
                 'aap_assets_v1.claim_accepted_input_garbage(uuid,integer,integer)'::pg_catalog.regprocedure,
                 'aap_assets_v1.complete_accepted_input_garbage(uuid,uuid)'::pg_catalog.regprocedure,
                 'aap_assets_v1.claim_accepted_video_input_garbage(uuid,integer,integer)'::pg_catalog.regprocedure,
                 'aap_assets_v1.complete_accepted_video_input_garbage(uuid,uuid)'::pg_catalog.regprocedure
             )
       ) OR EXISTS (
           SELECT 1
           FROM pg_catalog.pg_proc AS proc
           JOIN pg_catalog.pg_namespace AS namespace
             ON namespace.oid = proc.pronamespace
           WHERE namespace.nspname <> 'aap_assets_v1'
             AND pg_catalog.left(namespace.nspname, 4) = 'aap_'
             AND pg_catalog.right(namespace.nspname, 3) = '_v1'
             AND pg_catalog.has_function_privilege(
                 current_setting('aap.asset_cleanup_role'), proc.oid, 'EXECUTE'
             )
       ) THEN
        RAISE EXCEPTION 'Asset cleanup role must execute only its outbox functions';
    END IF;

    IF NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_workspace_v1.delete_project(uuid,uuid,uuid,jsonb)', 'EXECUTE') THEN
        RAISE EXCEPTION 'Caller is missing the public project deletion capability';
    END IF;
    IF NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_workspace_v1.preview_dataset_unlink(uuid,uuid,uuid)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_workspace_v1.unlink_dataset(uuid,uuid,uuid,uuid,text,jsonb)', 'EXECUTE') THEN
        RAISE EXCEPTION 'Caller is missing the public dataset-unlink capabilities';
    END IF;
    IF NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.delete_dataset(uuid,uuid,uuid,text,jsonb)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.delete_dataset_item(uuid,uuid,uuid,uuid,text,jsonb)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.reserve_dataset_upload(uuid,uuid,uuid,text,text)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.lock_dataset_upload(uuid,uuid,uuid)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.find_dataset_upload_duplicate(uuid,uuid,uuid,text)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.complete_dataset_upload(uuid,uuid,uuid,uuid,bigint,text,integer,integer)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.discard_dataset_upload(uuid,uuid,uuid,text)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.lock_scene_source_references(uuid[])', 'EXECUTE') THEN
        RAISE EXCEPTION 'Caller is missing a public Assets source or upload capability';
    END IF;
END $$;

GRANT CREATE ON SCHEMA aap_labeling_v1 TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.write_project_counter_projection(uuid, jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.decrement_project_review_count(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.decrement_project_completed_count(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.record_project_review_approval(uuid)
    OWNER TO :"labeling_owner";
REVOKE CREATE ON SCHEMA aap_labeling_v1 FROM :"labeling_owner";

-- Durable result data stays with its Jobs/Labeling owner. Cross-owner calls
-- expose only scoped capabilities; the inference executor receives no tables.
GRANT CREATE ON SCHEMA aap_jobs_v1 TO :"jobs_owner";
GRANT CREATE ON SCHEMA aap_labeling_v1 TO :"labeling_owner";
DO $$
DECLARE
    target record;
    owner_name text;
BEGIN
    FOR target IN
        SELECT proc.oid::regprocedure AS signature, namespace.nspname
        FROM pg_catalog.pg_proc AS proc
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = proc.pronamespace
        WHERE namespace.nspname IN ('aap_jobs_v1', 'aap_labeling_v1')
          AND proc.prokind = 'f' AND (proc.proname LIKE '%batch_result%'
              OR proc.proname LIKE '%prediction_retry%')
    LOOP
        owner_name := CASE target.nspname
            WHEN 'aap_jobs_v1' THEN current_setting('aap.jobs_owner')
            ELSE current_setting('aap.labeling_owner') END;
        EXECUTE pg_catalog.format('ALTER FUNCTION %s OWNER TO %I', target.signature, owner_name);
    END LOOP;
END $$;
REVOKE CREATE ON SCHEMA aap_jobs_v1 FROM :"jobs_owner";
REVOKE CREATE ON SCHEMA aap_labeling_v1 FROM :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    aap_jobs_v1._batch_result_scope_valid(jsonb, boolean),
    aap_jobs_v1.read_batch_result_execution(jsonb),
    aap_jobs_v1.lock_batch_result_execution(jsonb),
    aap_jobs_v1.record_batch_result_member(jsonb),
    aap_jobs_v1.advance_batch_result_member(jsonb),
    aap_jobs_v1.batch_result_execution_referenced(jsonb),
    aap_jobs_v1._create_prediction_retry_job(jsonb),
    aap_jobs_v1._finish_prediction_retry_job(jsonb),
    aap_jobs_v1.lock_prediction_retry_finish_scope(jsonb)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    aap_labeling_v1.batch_result_commit_proof(jsonb),
    aap_labeling_v1.batch_result_referenced(jsonb),
    aap_labeling_v1.retire_batch_results(jsonb),
    aap_labeling_v1._prediction_retry_no_raw_data(jsonb),
    aap_labeling_v1._prediction_retry_attempt_scope_valid(jsonb, boolean),
    aap_labeling_v1._prediction_retry_attempt_exists(uuid),
    aap_labeling_v1._prediction_retry_member_scope_matches(uuid, uuid, jsonb),
    aap_labeling_v1._prediction_retry_terminal_matches(uuid, text, jsonb),
    aap_labeling_v1._abandon_prediction_retry_from_job(uuid, text, text),
    aap_labeling_v1._retire_prediction_retry_results(uuid),
    aap_labeling_v1._prediction_retry_job_delete_allowed(uuid)
    TO :"jobs_owner";
-- Video window journals keep input and Mask retention behind owner capabilities.
GRANT CREATE ON SCHEMA aap_jobs_v1 TO :"jobs_owner";
ALTER TABLE aap_jobs_v1.video_execution_checkpoint_restore OWNER TO :"jobs_owner";
ALTER TABLE aap_jobs_v1.video_execution_journals OWNER TO :"jobs_owner";
ALTER TABLE aap_jobs_v1.video_execution_window_attempts OWNER TO :"jobs_owner";
ALTER TABLE aap_jobs_v1.video_execution_captures OWNER TO :"jobs_owner";
ALTER TABLE aap_jobs_v1.video_execution_capture_parts OWNER TO :"jobs_owner";
ALTER TABLE aap_jobs_v1.video_execution_windows OWNER TO :"jobs_owner";
ALTER TABLE aap_jobs_v1.video_execution_window_parts OWNER TO :"jobs_owner";
ALTER TABLE aap_jobs_v1.video_execution_span_closures OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.video_execution_checkpoint_step(text,text) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_checkpoint_results_complete(uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.video_execution_running_mask_roots_live(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.video_execution_checkpoint_mask_retained(text) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.video_execution_input_retained(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.video_execution_source_item_retained(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.video_tracker_input_retention_facts(uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_checkpoint_reply(jsonb,text) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_checkpoint_canonical_json(jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_checkpoint_digest(jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_checkpoint_integer(jsonb,bigint,bigint) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_checkpoint_digest_valid(text) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_checkpoint_plan_identities_complete(jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_checkpoint_manifest_valid(jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_checkpoint_plan_valid(jsonb,jsonb,uuid,jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_checkpoint_input_facts(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_checkpoint_all_routes_retired(uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._video_execution_checkpoint_terminal() OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA aap_jobs_v1 FROM :"jobs_owner";
GRANT CREATE ON SCHEMA aap_labeling_v1 TO :"labeling_owner";
ALTER TABLE aap_labeling_v1.video_execution_checkpoint_mask_sets OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.video_execution_request_facts(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.retain_video_execution_checkpoint_masks(uuid,text,jsonb) OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1._mask_gc_root_entry_live(text,text,timestamptz,boolean) OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1._mask_gc_release_tracker_running_outputs() OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.video_tracker_input_referenced(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.guard_dataset_source_deletion(uuid,uuid) OWNER TO :"labeling_owner";
REVOKE CREATE ON SCHEMA aap_labeling_v1 FROM :"labeling_owner";
GRANT CREATE ON SCHEMA aap_assets_v1 TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.video_execution_input_facts(uuid) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA aap_assets_v1 FROM :"assets_owner";
GRANT CREATE ON SCHEMA aap_inference_routing_v1 TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1.video_execution_scope_retired(uuid) OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_routing_v1.video_execution_scope_facts(uuid) OWNER TO :"inference_owner";
REVOKE CREATE ON SCHEMA aap_inference_routing_v1 FROM :"inference_owner";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.video_execution_request_facts(uuid) TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.video_execution_input_facts(uuid) TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.retain_video_execution_checkpoint_masks(uuid,text,jsonb) TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION aap_inference_routing_v1.video_execution_scope_retired(uuid) TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION aap_inference_routing_v1.video_execution_scope_facts(uuid) TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.video_execution_running_mask_roots_live(uuid) TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.video_execution_checkpoint_mask_retained(text) TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.video_execution_input_retained(uuid) TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.video_execution_source_item_retained(uuid) TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.video_tracker_input_retention_facts(uuid,uuid)
    TO :"labeling_owner";

-- Final video publication stays inside the Labeling business transaction.
GRANT CREATE ON SCHEMA aap_labeling_v1 TO :"labeling_owner";
ALTER TABLE aap_labeling_v1.video_writer_receipts OWNER TO :"labeling_owner";
DO $video_writer_owners$
DECLARE
    capability regprocedure;
BEGIN
    FOR capability IN
        SELECT function.oid::regprocedure
        FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = function.pronamespace
        WHERE namespace.nspname = 'aap_labeling_v1'
          AND (function.proname = 'video_writer_step'
               OR pg_catalog.left(function.proname,14) = '_video_writer_')
    LOOP
        EXECUTE pg_catalog.format('ALTER FUNCTION %s OWNER TO %I',
            capability, current_setting('aap.labeling_owner'));
    END LOOP;
END
$video_writer_owners$;
REVOKE CREATE ON SCHEMA aap_labeling_v1 FROM :"labeling_owner";
GRANT CREATE ON SCHEMA aap_jobs_v1 TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.video_writer_authority(text,jsonb) OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA aap_jobs_v1 FROM :"jobs_owner";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.video_writer_authority(text,jsonb)
    TO :"labeling_owner";

-- Offline restore coordinates existing owners through one fixed Jobs capability.
GRANT CREATE ON SCHEMA aap_jobs_v1 TO :"jobs_owner";
ALTER TABLE aap_jobs_v1.video_restore_fence OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.video_restore_authority(text,jsonb) OWNER TO :"jobs_owner";
DO $video_restore_jobs_owners$
DECLARE
    capability regprocedure;
BEGIN
    FOR capability IN
        SELECT function.oid::regprocedure
        FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = function.pronamespace
        WHERE namespace.nspname = 'aap_jobs_v1'
          AND pg_catalog.left(function.proname,15) = '_video_restore_'
    LOOP
        EXECUTE pg_catalog.format('ALTER FUNCTION %s OWNER TO %I',
            capability, current_setting('aap.jobs_owner'));
    END LOOP;
END
$video_restore_jobs_owners$;
REVOKE CREATE ON SCHEMA aap_jobs_v1 FROM :"jobs_owner";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.video_restore_authority(text,jsonb)
    TO :"inference_owner";
GRANT CREATE ON SCHEMA aap_inference_routing_v1 TO :"inference_owner";
ALTER TABLE aap_inference_routing_v1.video_restore_records OWNER TO :"inference_owner";
ALTER TABLE aap_inference_routing_v1.video_restore_contexts OWNER TO :"inference_owner";
ALTER TABLE aap_inference_routing_v1.video_restore_scopes OWNER TO :"inference_owner";
ALTER TABLE aap_inference_routing_v1.video_restore_attempts OWNER TO :"inference_owner";
ALTER TABLE aap_inference_routing_v1.video_restore_scope_retirements OWNER TO :"inference_owner";
ALTER TABLE aap_inference_routing_v1.video_restore_no_scope_receipts OWNER TO :"inference_owner";
ALTER TABLE aap_inference_routing_v1.video_restore_function_restore OWNER TO :"inference_owner";
DO $video_restore_owners$
DECLARE
    capability regprocedure;
BEGIN
    FOR capability IN
        SELECT function.oid::regprocedure
        FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = function.pronamespace
        WHERE namespace.nspname = 'aap_inference_routing_v1'
          AND (function.proname = 'video_restore_step'
               OR function.proname = '_video_execution_scope_step_pre_restore'
               OR pg_catalog.left(function.proname,15) = '_video_restore_')
    LOOP
        EXECUTE pg_catalog.format('ALTER FUNCTION %s OWNER TO %I',
            capability, current_setting('aap.inference_owner'));
    END LOOP;
END
$video_restore_owners$;
REVOKE CREATE ON SCHEMA aap_inference_routing_v1 FROM :"inference_owner";
-- Deletion uses current-run retirement proof without exposing Jobs journals.
GRANT CREATE ON SCHEMA aap_jobs_v1 TO :"jobs_owner";
ALTER TABLE aap_jobs_v1.video_deletion_clearances OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.guard_video_execution_deletion(uuid[],uuid[],uuid)
    OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA aap_jobs_v1 FROM :"jobs_owner";
GRANT EXECUTE ON FUNCTION
    aap_jobs_v1.guard_video_execution_deletion(uuid[],uuid[],uuid)
    TO :"labeling_owner";
GRANT CREATE ON SCHEMA aap_labeling_v1 TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.guard_project_video_deletion(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.guard_dataset_video_deletion(uuid,uuid)
    OWNER TO :"labeling_owner";
REVOKE CREATE ON SCHEMA aap_labeling_v1 FROM :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    aap_labeling_v1.guard_project_video_deletion(uuid),
    aap_labeling_v1.guard_dataset_video_deletion(uuid,uuid)
    TO :"workspace_owner";
-- Jobs owns page and total; cross-owner facts stay fixed capabilities.
GRANT CREATE ON SCHEMA aap_jobs_v1 TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._async_job_list_rows(uuid,text,text[],text[],uuid,text)
    OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.async_job_list_page(uuid,text,text[],text[],uuid,text,integer,integer)
    OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.async_job_list_total(uuid,text,text[],text[],uuid,text)
    OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA aap_jobs_v1 FROM :"jobs_owner";
GRANT USAGE ON SCHEMA aap_identity_v1, aap_workspace_v1,
    aap_workspace_project_query_v1 TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION aap_identity_v1.read_actor_facts(uuid),
    aap_workspace_v1.actor_project_memberships(uuid),
    aap_workspace_project_query_v1.project_search_scope(uuid,text)
    TO :"jobs_owner";
GRANT USAGE ON SCHEMA aap_workspace_fixture_v1 TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION aap_workspace_fixture_v1.guard_project_cleanup(uuid[])
    TO :"jobs_owner";
-- Jobs owns lifecycle writes; existing producers retain their current ACLs.
GRANT SELECT, INSERT, UPDATE, DELETE ON public.async_jobs TO :"jobs_owner";
GRANT CREATE ON SCHEMA aap_jobs_v1 TO :"jobs_owner";
ALTER VIEW aap_jobs_v1.async_job_creation OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.delete_fixture_project_jobs(uuid[]) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.set_filter_fixture_job_outcomes(uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.lock_action_idempotency(text,uuid,uuid,text) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.find_action_idempotent_jobs(text,uuid,uuid,text) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.read_job(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.read_job_for_update(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.read_job_for_share_nowait(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.lock_cross_frame_singleflight(uuid,text) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.active_cross_frame_job(uuid,text) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.cross_frame_job_page(uuid,text,integer) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.cross_frame_job_count(uuid,text) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.latest_batch_prediction_times(uuid[]) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.jobs_by_execution_id(text) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.insert_video_summary(uuid,text,uuid,uuid,jsonb,text) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.mark_dispatch_failed(uuid,timestamptz,text) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.cancel_batch_predict_jobs(uuid,timestamptz) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.delete_batch_predict_jobs(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.delete_terminal_jobs_before(timestamptz,text[]) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._jobs_lifecycle_has_keys(jsonb,text[]) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._jobs_lifecycle_is_truthy(jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._jobs_lifecycle_integer(jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.apply_lifecycle(uuid,jsonb) OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA aap_jobs_v1 FROM :"jobs_owner";
COMMIT;
