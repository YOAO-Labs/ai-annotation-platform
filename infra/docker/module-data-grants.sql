\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config('yoao.assets_owner', :'assets_owner', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.jobs_owner', :'jobs_owner', true);
SELECT set_config('yoao.learning_owner', :'learning_owner', true);
SELECT set_config('yoao.inference_owner', :'inference_owner', true);
SELECT set_config('yoao.insights_owner', :'insights_owner', true);
SELECT set_config('yoao.diagnostics_owner', :'diagnostics_owner', true);
SELECT set_config('yoao.export_owner', :'export_owner', true);
SELECT set_config('yoao.audit_owner', :'audit_owner', true);
SELECT set_config('yoao.settings_owner', :'settings_owner', true);
SELECT set_config('yoao.notifications_owner', :'notifications_owner', true);
SELECT set_config('yoao.workflow_role', :'workflow_role', true);
SELECT set_config('yoao.asset_cleanup_role', :'asset_cleanup_role', true);
SELECT set_config('yoao.mask_gc_role', :'mask_gc_role', true);
SELECT set_config('yoao.inference_registry_ingress_role', :'inference_registry_ingress_role', true);
SELECT set_config('yoao.inference_routing_role', :'inference_routing_role', true);

DO $$
DECLARE
    role_name text;
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.workspace_owner'),
        current_setting('yoao.assets_owner'),
        current_setting('yoao.labeling_owner'),
        current_setting('yoao.jobs_owner'),
        current_setting('yoao.learning_owner'),
        current_setting('yoao.inference_owner'),
        current_setting('yoao.insights_owner'),
        current_setting('yoao.diagnostics_owner'),
        current_setting('yoao.export_owner'),
        current_setting('yoao.audit_owner'),
        current_setting('yoao.settings_owner'),
        current_setting('yoao.notifications_owner'),
        current_setting('yoao.workflow_role'),
        current_setting('yoao.asset_cleanup_role'),
        current_setting('yoao.mask_gc_role'),
        current_setting('yoao.inference_registry_ingress_role'),
        current_setting('yoao.inference_routing_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
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
                  current_setting('yoao.workflow_role'),
                  current_setting('yoao.asset_cleanup_role'),
                  current_setting('yoao.mask_gc_role'),
                  current_setting('yoao.inference_registry_ingress_role'),
                  current_setting('yoao.inference_routing_role')
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
            WHERE rolname = current_setting('yoao.asset_cleanup_role')
        )
    ) THEN
        RAISE EXCEPTION 'The Assets cleanup role must not have inheriting members';
    END IF;
    IF EXISTS (
        SELECT FROM pg_auth_members AS membership
        WHERE membership.roleid = (
            SELECT oid FROM pg_roles
            WHERE rolname = current_setting('yoao.mask_gc_role')
        )
    ) THEN
        RAISE EXCEPTION 'The Labeling Mask GC role must not have inheriting members';
    END IF;
END $$;

DO $private_routines$
DECLARE
    namespace name;
BEGIN
    FOR namespace IN
        SELECT n.nspname FROM pg_catalog.pg_namespace AS n
        WHERE pg_catalog.left(n.nspname, 5) = 'yoao_'
           OR pg_catalog.left(n.nspname, 4) = 'aap_'
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM PUBLIC', namespace);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL ROUTINES IN SCHEMA %I FROM PUBLIC', namespace);
    END LOOP;
END;
$private_routines$;
REVOKE ALL ON SCHEMA yoao_identity_v1, yoao_identity_keys_v1, yoao_identity_sessions_v1, yoao_identity_accounts_v1, yoao_identity_recovery_v1, yoao_identity_groups_v1, yoao_identity_deactivation_v1, yoao_identity_invitations_v1, yoao_identity_mail_v2, yoao_identity_directory_v1, yoao_identity_admin_passwords_v1, yoao_identity_platform_roles_v1, yoao_identity_suspensions_v1, yoao_identity_deletions_v1, yoao_identity_offboarding_v1, yoao_identity_briefs_v1, yoao_workspace_template_query_v1, yoao_workspace_project_query_v1, yoao_workspace_directory_query_v1, yoao_workspace_v1, yoao_assets_v1,
    yoao_labeling_class_query_v1, yoao_labeling_v1, yoao_jobs_v1, yoao_learning_v1, yoao_inference_v1, yoao_inference_registry_ingress_v1, yoao_inference_routing_v1,
    yoao_insights_v1, yoao_diagnostics_v1, yoao_export_v1, yoao_audit_v1, yoao_settings_v1, yoao_notifications_v1
    FROM :"asset_cleanup_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_identity_v1, yoao_workspace_template_query_v1, yoao_workspace_project_query_v1, yoao_workspace_directory_query_v1, yoao_workspace_v1,
    yoao_identity_keys_v1, yoao_identity_sessions_v1, yoao_identity_accounts_v1, yoao_identity_recovery_v1, yoao_identity_groups_v1, yoao_identity_deactivation_v1, yoao_identity_invitations_v1, yoao_identity_mail_v2, yoao_identity_directory_v1, yoao_identity_admin_passwords_v1, yoao_identity_platform_roles_v1, yoao_identity_suspensions_v1, yoao_identity_deletions_v1, yoao_identity_offboarding_v1, yoao_identity_briefs_v1, yoao_assets_v1, yoao_labeling_class_query_v1, yoao_labeling_v1, yoao_jobs_v1, yoao_learning_v1,
    yoao_inference_v1, yoao_inference_registry_ingress_v1, yoao_inference_routing_v1, yoao_insights_v1, yoao_diagnostics_v1, yoao_export_v1,
    yoao_audit_v1 FROM :"asset_cleanup_role";
REVOKE ALL ON SCHEMA yoao_identity_v1, yoao_identity_keys_v1, yoao_identity_sessions_v1, yoao_identity_accounts_v1, yoao_identity_recovery_v1, yoao_identity_groups_v1, yoao_identity_deactivation_v1, yoao_identity_invitations_v1, yoao_identity_mail_v2, yoao_identity_directory_v1, yoao_identity_admin_passwords_v1, yoao_identity_platform_roles_v1, yoao_identity_suspensions_v1, yoao_identity_deletions_v1, yoao_identity_offboarding_v1, yoao_identity_briefs_v1, yoao_workspace_template_query_v1, yoao_workspace_project_query_v1, yoao_workspace_directory_query_v1, yoao_workspace_v1, yoao_assets_v1,
    yoao_labeling_class_query_v1, yoao_labeling_v1, yoao_jobs_v1, yoao_learning_v1, yoao_inference_v1, yoao_inference_registry_ingress_v1, yoao_inference_routing_v1,
    yoao_insights_v1, yoao_diagnostics_v1, yoao_export_v1, yoao_audit_v1,
    yoao_settings_v1, yoao_notifications_v1 FROM :"mask_gc_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_identity_v1, yoao_workspace_template_query_v1, yoao_workspace_project_query_v1, yoao_workspace_directory_query_v1, yoao_workspace_v1,
    yoao_identity_keys_v1, yoao_identity_sessions_v1, yoao_identity_accounts_v1, yoao_identity_recovery_v1, yoao_identity_groups_v1, yoao_identity_deactivation_v1, yoao_identity_invitations_v1, yoao_identity_mail_v2, yoao_identity_directory_v1, yoao_identity_admin_passwords_v1, yoao_identity_platform_roles_v1, yoao_identity_suspensions_v1, yoao_identity_deletions_v1, yoao_identity_offboarding_v1, yoao_identity_briefs_v1, yoao_assets_v1, yoao_labeling_class_query_v1, yoao_labeling_v1, yoao_jobs_v1, yoao_learning_v1,
    yoao_inference_v1, yoao_inference_registry_ingress_v1, yoao_inference_routing_v1, yoao_insights_v1, yoao_diagnostics_v1, yoao_export_v1,
    yoao_audit_v1, yoao_settings_v1, yoao_notifications_v1 FROM :"mask_gc_role";
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

GRANT SELECT (
    id, name, email, role, group_name, group_id, is_active, credential_generation,
    disabled_kind, disabled_at, disabled_by, disabled_reason, email_verified_at,
    last_login_at, password_admin_reset_at, deactivation_requested_at,
    deactivation_reason,
    deactivation_scheduled_at, avatar_ref, password_hash,
    created_at, weekly_target_default
), UPDATE (
    id, credential_generation, last_login_at, role, name,
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
    group_id, is_active, email_verified_at
) ON public.users TO :"identity_owner";
GRANT SELECT, INSERT, UPDATE ON public.identity_account_presence TO :"identity_owner";
GRANT SELECT, INSERT, UPDATE ON public.identity_account_preferences TO :"identity_owner";
GRANT SELECT, INSERT, UPDATE ON public.identity_api_key_usage TO :"identity_owner";
GRANT SELECT (
    id, user_id, name, key_prefix, key_hash, scopes, expires_at,
    revoked_at, created_at
), INSERT (id, user_id, name, key_prefix, key_hash, scopes, expires_at),
    UPDATE (name, key_prefix, key_hash, scopes, expires_at, revoked_at)
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
    ON yoao_identity_v1.account_profiles TO :"audit_owner";

GRANT SELECT (id, display_id, data_type, name, file_count),
    UPDATE (id, file_count, updated_at), DELETE
    ON public.datasets TO :"assets_owner";
GRANT SELECT (storage_scan_prefix, archived_at, archived_by, lifecycle_revision)
    ON public.datasets TO :"assets_owner";
GRANT SELECT (location_id, dataset_id) ON public.dataset_sources TO :"assets_owner";
GRANT SELECT (id, project_id, dataset_id, created_at),
    INSERT (id, project_id, dataset_id),
    UPDATE (id) ON public.project_datasets TO :"assets_owner";
GRANT DELETE ON public.project_datasets TO :"assets_owner";
GRANT SELECT (id, dataset_id, name, source_format, source_metadata, created_by),
    INSERT (id, display_id, dataset_id, name, source_format, source_metadata, created_by),
    UPDATE (id), DELETE
    ON public.scenes TO :"assets_owner";
GRANT SELECT (nuscenes_map_original_id) ON public.scenes TO :"assets_owner";
GRANT USAGE ON SEQUENCE public.display_seq_scenes TO :"assets_owner";
GRANT SELECT, INSERT (
    id, dataset_id, file_name, file_path, file_type, file_size,
    content_hash, width, height, metadata
), UPDATE (id, file_path, file_size, content_hash, width, height, scene_id, frame_index, updated_at), DELETE
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

GRANT USAGE ON SCHEMA yoao_fixture_v1
    TO :"assets_owner", :"jobs_owner", :"labeling_owner", :"identity_owner";
GRANT SELECT ON yoao_fixture_v1.fixture_original_targets,
    yoao_fixture_v1.fixture_runs, yoao_fixture_v1.fixture_run_resources,
    yoao_fixture_v1.fixture_run_admissions, yoao_fixture_v1.fixture_auxiliary_intents
    TO :"assets_owner";
GRANT UPDATE (id) ON yoao_fixture_v1.fixture_original_targets TO :"assets_owner";
GRANT UPDATE (id) ON yoao_fixture_v1.fixture_runs
    TO :"assets_owner", :"jobs_owner", :"labeling_owner", :"identity_owner";
GRANT UPDATE (run_id) ON yoao_fixture_v1.fixture_run_resources TO :"assets_owner";
GRANT SELECT ON yoao_fixture_v1.fixture_runs, yoao_fixture_v1.fixture_run_resources,
    yoao_fixture_v1.fixture_presentation_jobs TO :"jobs_owner";
GRANT INSERT, UPDATE (state) ON yoao_fixture_v1.fixture_presentation_jobs
    TO :"jobs_owner";
GRANT SELECT ON yoao_fixture_v1.fixture_runs, yoao_fixture_v1.fixture_run_resources
    TO :"labeling_owner";
GRANT SELECT ON yoao_fixture_v1.fixture_runs, yoao_fixture_v1.fixture_run_resources,
    yoao_fixture_v1.fixture_run_admissions, yoao_fixture_v1.fixture_invitation_intents
    TO :"identity_owner";
GRANT INSERT (run_id, kind, resource_id, logical_key, state)
    ON yoao_fixture_v1.fixture_run_resources TO :"identity_owner";
GRANT UPDATE (invitation_id, accepted_user_id, state)
    ON yoao_fixture_v1.fixture_invitation_intents TO :"identity_owner";

GRANT SELECT (id, name, metadata, is_temporal) ON public.datasets TO :"assets_owner";
GRANT SELECT (description, created_by, created_at, updated_at),
    INSERT (id, display_id, name, description, data_type, is_temporal,
        file_count, created_by, metadata),
    UPDATE (metadata, is_temporal)
    ON public.datasets TO :"assets_owner";
GRANT SELECT (display_id, created_at, updated_at),
    UPDATE (name, source_format, source_metadata, nuscenes_map_original_id, updated_at)
    ON public.scenes TO :"assets_owner";
GRANT USAGE, SELECT ON SEQUENCE public.display_seq_scenes TO :"assets_owner";
GRANT UPDATE (metadata, nuscenes_raw_original_id) ON public.dataset_items TO :"assets_owner";
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

DO $$
DECLARE
    denied record;
BEGIN
    FOR denied IN
        SELECT * FROM (VALUES
            (current_setting('yoao.assets_owner'), 'public.users'),
            (current_setting('yoao.assets_owner'), 'public.system_settings'),
            (current_setting('yoao.assets_owner'), 'public.notifications'),
            (current_setting('yoao.assets_owner'), 'public.notification_preferences'),
            (current_setting('yoao.jobs_owner'), 'public.users'),
            (current_setting('yoao.jobs_owner'), 'public.notifications'),
            (current_setting('yoao.jobs_owner'), 'public.system_settings'),
            (current_setting('yoao.notifications_owner'), 'public.users'),
            (current_setting('yoao.notifications_owner'), 'public.async_jobs'),
            (current_setting('yoao.notifications_owner'), 'public.datasets'),
            (current_setting('yoao.settings_owner'), 'public.users'),
            (current_setting('yoao.settings_owner'), 'public.async_jobs'),
            (current_setting('yoao.identity_owner'), 'public.projects'),
            (current_setting('yoao.identity_owner'), 'public.project_members'),
            (current_setting('yoao.identity_owner'), 'public.tasks'),
            (current_setting('yoao.identity_owner'), 'public.task_batches'),
            (current_setting('yoao.assets_owner'), 'public.projects'),
            (current_setting('yoao.assets_owner'), 'public.project_members'),
            (current_setting('yoao.assets_owner'), 'public.tasks'),
            (current_setting('yoao.assets_owner'), 'public.task_batches'),
            (current_setting('yoao.assets_owner'), 'public.task_dataset_item_links'),
            (current_setting('yoao.assets_owner'), 'public.annotations'),
            (current_setting('yoao.assets_owner'), 'public.video_tracker_jobs'),
            (current_setting('yoao.assets_owner'), 'public.video_tracker_input_scopes'),
            (current_setting('yoao.assets_owner'), 'public.annotation_video_tracker_input_sources'),
            (current_setting('yoao.labeling_owner'), 'public.users'),
            (current_setting('yoao.labeling_owner'), 'public.project_members'),
            (current_setting('yoao.labeling_owner'), 'public.datasets'),
            (current_setting('yoao.labeling_owner'), 'public.dataset_items'),
            (current_setting('yoao.labeling_owner'), 'public.project_datasets'),
            (current_setting('yoao.workspace_owner'), 'public.users'),
            (current_setting('yoao.workspace_owner'), 'public.datasets'),
            (current_setting('yoao.workspace_owner'), 'public.dataset_items'),
            (current_setting('yoao.workspace_owner'), 'public.project_datasets'),
            (current_setting('yoao.workspace_owner'), 'public.tasks')
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

GRANT DELETE ON public.annotation_conversion_plans TO :"labeling_owner";
GRANT INSERT (actor_id, expires_at, id, plan_json, request_digest, request_json, snapshot_digest, task_id, token_hash) ON public.annotation_conversion_plans TO :"labeling_owner";
GRANT UPDATE (executed_idempotency_key, executed_operation_id) ON public.annotation_conversion_plans TO :"labeling_owner";
GRANT UPDATE (response_json) ON public.annotation_operations TO :"labeling_owner";
GRANT INSERT (calibration_digest, calibration_revision, created_at, sensor_dataset_item_id, sensor_role, sensor_visibility) ON public.annotations TO :"labeling_owner";
GRANT UPDATE (calibration_digest, calibration_revision, sensor_visibility) ON public.annotations TO :"labeling_owner";
GRANT INSERT (adapter_version, format_id, id, manifest_version, mapping_digest, mapping_json, options_digest, options_json, plan_digest, plan_json, project_id, receipt_expires_at, requested_by_id, result_json, staged_object_key, staged_sha256, staging_ref, status, token_hash) ON public.mask_format_imports TO :"labeling_owner";
GRANT UPDATE (async_job_id, completed_at, result_json, status, updated_at) ON public.mask_format_imports TO :"labeling_owner";
GRANT INSERT (annotation_id, annotation_version, code, dedupe_key, frame_end, frame_start, id, last_seen_run_id, metric, project_id, region_bbox, region_digest, region_mask_ref, related_annotation_ids, run_id, severity, severity_rank, source, source_versions, status, suggestion, task_id, threshold, updated_at) ON public.mask_qc_issues TO :"labeling_owner";
GRANT UPDATE (last_seen_run_id, metric, region_bbox, region_digest, region_mask_ref, resolved_at, resolved_by_id, severity, severity_rank, source, status, suggestion, threshold, updated_at) ON public.mask_qc_issues TO :"labeling_owner";
GRANT INSERT (async_job_id, config_digest, config_revision, config_snapshot, id, progress_pct, project_id, requested_by_id, scope_json, singleflight_key, source_snapshot, source_snapshot_digest, status, summary, task_snapshot_digests) ON public.mask_qc_runs TO :"labeling_owner";
GRANT UPDATE (completed_at, error_message, progress_pct, started_at, status, summary, updated_at) ON public.mask_qc_runs TO :"labeling_owner";
GRANT INSERT (id, plan_digest, plan_json, project_id, receipt_expires_at, request_json, requested_by_id, result_json, status, token_hash) ON public.mask_repair_batches TO :"labeling_owner";
GRANT UPDATE (async_job_id, completed_at, requested_by_id, result_json, rollback_async_job_id, rollback_expires_at, rolled_back_at, status, updated_at) ON public.mask_repair_batches TO :"labeling_owner";
GRANT INSERT (annotation_id, candidate_digest, decision, frame_end, frame_start, id, project_id, qc_issue_id, region_digest, region_mask_ref, result_annotation_version, reviewer_id, source_annotation_version, source_job_id, source_job_revision, task_id) ON public.mask_review_scopes TO :"labeling_owner";
GRANT UPDATE (id) ON public.mask_review_scopes TO :"labeling_owner";
GRANT INSERT (baseline_config_digest, baseline_config_revision, baseline_config_snapshot, candidate_config_digest, candidate_config_snapshot, created_by_id, cutoff_at, gate_reasons, gate_status, id, project_id, sample_count, sample_snapshot, summary) ON public.point_cloud_quality_evaluations TO :"labeling_owner";
GRANT UPDATE (promoted_at, promoted_by_id, promoted_config_revision) ON public.point_cloud_quality_evaluations TO :"labeling_owner";
GRANT INSERT (annotation_id, annotation_version, class_name, code, dedupe_key, evidence, frame_end, frame_start, id, last_seen_run_id, locator, metric, project_id, related_annotation_ids, rule_version, run_id, scene_id, scene_track_id, severity, severity_rank, source_versions, status, suggested_command, task_id, threshold, track_revision) ON public.point_cloud_quality_issues TO :"labeling_owner";
GRANT UPDATE (annotation_id, annotation_version, class_name, evidence, last_seen_run_id, locator, metric, related_annotation_ids, resolution_reason, resolved_at, resolved_by_id, review_note, review_verdict, reviewed_at, reviewed_by_id, run_id, scene_track_id, severity, severity_rank, source_versions, status, suggested_command, task_id, threshold, track_revision, updated_at) ON public.point_cloud_quality_issues TO :"labeling_owner";
GRANT INSERT (async_job_id, config_digest, config_revision, config_snapshot, id, progress_pct, project_id, requested_by_id, scope_json, singleflight_key, source_snapshot, source_snapshot_digest, status, summary) ON public.point_cloud_quality_runs TO :"labeling_owner";
GRANT UPDATE (completed_at, error_message, progress_pct, started_at, status, summary, updated_at) ON public.point_cloud_quality_runs TO :"labeling_owner";
GRANT INSERT (sequence_order, created_at, updated_at, assignee_id, assigned_at, reviewer_id, reviewer_claimed_at, submitted_at, reviewed_at, review_round_id, reject_reason, reject_reason_type, skip_reason, skipped_at) ON public.tasks TO :"labeling_owner";
GRANT UPDATE (dataset_item_id, file_name, file_path, file_type, created_at) ON public.tasks TO :"labeling_owner";
GRANT INSERT (total_tasks) ON public.task_batches TO :"labeling_owner";
GRANT INSERT (created_at, updated_at) ON public.annotations TO :"labeling_owner";
GRANT INSERT (staged_result, celery_task_id, started_at, completed_at, error_message, created_at, updated_at) ON public.video_tracker_jobs TO :"labeling_owner";
GRANT INSERT (created_at) ON public.task_dataset_item_links TO :"labeling_owner";
GRANT UPDATE (anchor_position) ON public.annotation_feedbacks TO :"labeling_owner";
GRANT INSERT (dataset_item_id, end_frame, id, segment_index, start_frame, status) ON public.video_segments TO :"labeling_owner";
GRANT UPDATE (assignee_id, lock_expires_at, locked_at, locked_by, status, updated_at) ON public.video_segments TO :"labeling_owner";
GRANT DELETE ON public.video_track_quality_issues TO :"labeling_owner";
GRANT INSERT (code, frame_end, frame_start, id, left_annotation_id, metric, right_annotation_id, run_id) ON public.video_track_quality_issues TO :"labeling_owner";
GRANT INSERT (async_job_id, id, input_digest, input_snapshot, left_segment_id, metrics, pairs, progress_pct, requested_by_id, right_segment_id, sampling_digest, status, task_id) ON public.video_track_quality_runs TO :"labeling_owner";
GRANT UPDATE (accepted_at, accepted_by_id, completed_at, error_message, metrics, pairs, progress_pct, stale_at, started_at, status, updated_at) ON public.video_track_quality_runs TO :"labeling_owner";
GRANT INSERT (annotation_id, correction_frame, created_by, dataset_item_id, direction, event_channel, from_frame, id, job_kind, model_key, prompt, revision, segment_id, status, target_class_name, target_tool_unit_id, task_id, to_frame, track_id_snapshot) ON public.video_tracker_jobs TO :"labeling_owner";
GRANT UPDATE (cancel_requested_at, celery_task_id, event_channel, prompt, revision, started_at) ON public.video_tracker_jobs TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.workspace_project_query_dataset_ids(uuid) TO :"labeling_owner";

GRANT SELECT (
    id, dataset_item_id, segment_index, start_frame, end_frame, assignee_id, status, locked_by, locked_at, lock_expires_at, created_at, updated_at
) ON public.video_segments TO :"labeling_owner";
GRANT SELECT (
    id, task_id, dataset_item_id, annotation_id, segment_id, target_class_name, target_tool_unit_id, created_by, status, job_kind, track_id_snapshot, correction_frame, model_key, direction, from_frame, to_frame, prompt, staged_result, revision, event_channel, celery_task_id, cancel_requested_at, started_at, completed_at, error_message, created_at, updated_at
) ON public.video_tracker_jobs TO :"labeling_owner";
GRANT SELECT (
    id, project_id, task_id, annotation_id, qc_issue_id, source_job_id, reviewer_id, source_annotation_version, result_annotation_version, source_job_revision, frame_start, frame_end, region_mask_ref, region_digest, candidate_digest, decision, created_at
) ON public.mask_review_scopes TO :"labeling_owner";
GRANT SELECT (
    id, token_hash, task_id, actor_id, request_digest, snapshot_digest, request_json, plan_json, executed_operation_id, executed_idempotency_key, created_at, expires_at
) ON public.annotation_conversion_plans TO :"labeling_owner";
GRANT SELECT (
    id, project_id, requested_by_id, async_job_id, format_id, adapter_version, manifest_version, staged_object_key, staged_sha256, staging_ref, mapping_json, options_json, mapping_digest, options_digest, plan_json, plan_digest, token_hash, receipt_expires_at, status, result_json, created_at, updated_at, completed_at
) ON public.mask_format_imports TO :"labeling_owner";
GRANT SELECT (
    id, project_id, async_job_id, requested_by_id, status, progress_pct, scope_json, config_revision, config_digest, config_snapshot, source_snapshot, source_snapshot_digest, task_snapshot_digests, singleflight_key, summary, error_message, started_at, completed_at, created_at, updated_at
) ON public.mask_qc_runs TO :"labeling_owner";
GRANT SELECT (
    id, run_id, last_seen_run_id, project_id, task_id, annotation_id, annotation_version, related_annotation_ids, source_versions, code, severity, severity_rank, status, frame_start, frame_end, metric, threshold, region_bbox, region_mask_ref, region_digest, dedupe_key, source, suggestion, resolved_by_id, resolved_at, created_at, updated_at
) ON public.mask_qc_issues TO :"labeling_owner";
GRANT SELECT (
    id, project_id, requested_by_id, async_job_id, rollback_async_job_id, token_hash, status, plan_digest, request_json, plan_json, result_json, receipt_expires_at, rollback_expires_at, created_at, updated_at, completed_at, rolled_back_at
) ON public.mask_repair_batches TO :"labeling_owner";
GRANT SELECT (
    id, project_id, async_job_id, requested_by_id, status, progress_pct, scope_json, config_revision, config_digest, config_snapshot, source_snapshot, source_snapshot_digest, singleflight_key, summary, error_message, started_at, completed_at, created_at, updated_at
) ON public.point_cloud_quality_runs TO :"labeling_owner";
GRANT SELECT (
    id, run_id, last_seen_run_id, project_id, scene_id, task_id, annotation_id, annotation_version, scene_track_id, track_revision, related_annotation_ids, source_versions, class_name, code, rule_version, severity, severity_rank, status, frame_start, frame_end, metric, threshold, evidence, locator, suggested_command, dedupe_key, resolution_reason, resolved_by_id, resolved_at, review_verdict, review_note, reviewed_by_id, reviewed_at, created_at, updated_at
) ON public.point_cloud_quality_issues TO :"labeling_owner";
GRANT SELECT (
    id, project_id, created_by_id, baseline_config_revision, baseline_config_digest, baseline_config_snapshot, candidate_config_digest, candidate_config_snapshot, cutoff_at, sample_count, sample_snapshot, summary, gate_status, gate_reasons, promoted_by_id, promoted_at, promoted_config_revision, created_at
) ON public.point_cloud_quality_evaluations TO :"labeling_owner";
GRANT SELECT (
    id, task_id, left_segment_id, right_segment_id, async_job_id, requested_by_id, status, progress_pct, input_snapshot, input_digest, sampling_digest, metrics, pairs, error_message, accepted_by_id, accepted_at, stale_at, started_at, completed_at, created_at, updated_at
) ON public.video_track_quality_runs TO :"labeling_owner";
GRANT SELECT (
    id, run_id, left_annotation_id, right_annotation_id, code, frame_start, frame_end, metric, created_at
) ON public.video_track_quality_issues TO :"labeling_owner";
GRANT SELECT (id, sensor_name, created_at) ON public.task_dataset_item_links TO :"labeling_owner";
GRANT SELECT (id, dataset_item_id, title, start_frame, end_frame) ON public.video_chapters TO :"assets_owner";
GRANT SELECT ON yoao_assets_v1.video_chapter_facts,
    yoao_assets_v1.labeling_dataset_item_facts,
    yoao_assets_v1.scale_seed_dataset_item_facts TO :"labeling_owner";
GRANT SELECT ON yoao_labeling_v1.annotation_draft_rows TO :"labeling_owner";

REVOKE SELECT ON public.tasks FROM :"labeling_owner";
GRANT SELECT (total_annotations, is_labeled),
    UPDATE (total_annotations, is_labeled)
    ON public.tasks TO :"labeling_owner";
GRANT SELECT (id, project_id, dataset_item_id, batch_id, status), UPDATE (id), DELETE
    ON public.tasks TO :"labeling_owner";
GRANT SELECT (
    assignee_id, assigned_at, submitted_at, reviewer_id, reviewer_is_override,
    reviewer_claimed_at, reviewed_at, reject_reason, reject_reason_type,
    skip_reason, skipped_at, reopened_count, last_reopened_at, review_round_id,
    annotation_contributor_ids, review_contributor_ids, review_submitter_id,
    first_review_eligible, first_review_contributor_ids, first_reviewed_at,
    first_review_result, updated_at
), UPDATE (
    status, assignee_id, assigned_at, submitted_at, reviewer_id,
    reviewer_is_override, reviewer_claimed_at, reviewed_at, reject_reason,
    reject_reason_type, skip_reason, skipped_at, reopened_count, last_reopened_at,
    review_round_id, annotation_contributor_ids, review_contributor_ids,
    review_submitter_id, first_review_contributor_ids, first_reviewed_at,
    first_review_result, updated_at
) ON public.tasks TO :"labeling_owner";
GRANT SELECT (
        id, project_id, dataset_id, display_id, annotator_id, reviewer_id,
        name, description, status, priority, deadline, assigned_user_ids,
        total_tasks, completed_tasks, review_tasks, approved_tasks, rejected_tasks,
        created_by, created_at, updated_at, review_feedback, reviewed_at,
        reviewed_by, admin_locked, admin_lock_reason, admin_locked_at,
        admin_locked_by
    ),
    INSERT (
        id, project_id, dataset_id, display_id, name, description, status,
        priority, deadline, assigned_user_ids, created_by, annotator_id, reviewer_id
    ),
    UPDATE (
        id, dataset_id, total_tasks, completed_tasks, review_tasks, updated_at,
        annotator_id, reviewer_id, assigned_user_ids, name, description, status,
        priority, deadline, review_feedback,
        reviewed_at, reviewed_by, admin_locked, admin_lock_reason,
        admin_locked_at, admin_locked_by
    ), DELETE
    ON public.task_batches TO :"labeling_owner";
GRANT SELECT (sequence_order),
    UPDATE (batch_id, sequence_order, assignee_is_override, total_predictions)
    ON public.tasks TO :"labeling_owner";
GRANT CREATE ON SCHEMA public TO :"labeling_owner";
DO $labeling_prediction_storage_owner$
DECLARE
    v_partition record;
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.labeling_owner'));
BEGIN
    FOR v_partition IN
        SELECT relid FROM pg_catalog.pg_partition_tree('public.predictions')
    LOOP
        EXECUTE pg_catalog.format('ALTER TABLE %s OWNER TO %I',
            v_partition.relid::pg_catalog.regclass,
            current_setting('yoao.labeling_owner'));
    END LOOP;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_partition_tree('public.predictions') AS partition
        JOIN pg_catalog.pg_class AS relation ON relation.oid=partition.relid
        WHERE relation.relowner<>v_owner
    ) THEN
        RAISE EXCEPTION 'Prediction storage must remain with its Labeling owner';
    END IF;
END;
$labeling_prediction_storage_owner$;
GRANT SELECT (id, project_id, task_id), DELETE
    ON public.predictions TO :"labeling_owner";
GRANT SELECT (id, object_key, task_id, linked_at, created_at),
    INSERT (id, task_id, object_key), UPDATE (created_at, linked_at), DELETE
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
GRANT INSERT (
    id, created_at, task_id, project_id, ml_backend_id, ml_backend_pool_id,
    model_version, score, tool_unit_id, result, source, rejected_shape_indexes
), UPDATE (rejected_shape_indexes) ON public.predictions TO :"labeling_owner";
GRANT INSERT (
    id, prediction_id, prediction_created_at, inference_time_ms,
    prompt_tokens, completion_tokens, total_tokens, prompt_cost,
    completion_cost, total_cost, extra
) ON public.prediction_metas TO :"labeling_owner";
GRANT INSERT (
    id, task_id, project_id, ml_backend_id, ml_backend_pool_id,
    model_version, error_type, message, extra
), UPDATE (dismissed_at) ON public.failed_predictions TO :"labeling_owner";
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
GRANT SELECT (
    video_segment_id, user_id, source, annotation_type, tool_unit_id, class_name,
    geometry, confidence, track_id, scene_track_id, sensor_role, sensor_visibility,
    calibration_revision, calibration_digest, temporal_role, lead_time,
    was_cancelled, ground_truth, is_active, attributes, attributes_meta,
    z_order, is_locked, is_hidden, version, created_at, updated_at
), INSERT (
    id, task_id, project_id, video_segment_id, user_id, source, annotation_type,
    tool_unit_id, class_name, geometry, confidence, parent_prediction_id,
    parent_annotation_id, track_id, scene_track_id, temporal_role, lead_time,
    was_cancelled, ground_truth, is_active, attributes, attributes_meta,
    z_order, is_locked, is_hidden, version
), UPDATE (
    annotation_type, class_name, geometry, confidence, track_id, scene_track_id,
    temporal_role, is_active, attributes, attributes_meta, z_order,
    is_locked, is_hidden, version, updated_at
) ON public.annotations TO :"labeling_owner";
GRANT SELECT (
    id, project_id, scene_id, track_id, class_name, presence_mode, attributes,
    attributes_meta, revision, created_by, retired_at, created_at, updated_at
), INSERT (
    id, project_id, scene_id, track_id, class_name, created_by
), UPDATE (class_name, presence_mode, revision, retired_at, attributes, attributes_meta, updated_at)
    ON public.scene_tracks TO :"labeling_owner";
GRANT UPDATE (source) ON public.annotations TO :"labeling_owner";
GRANT SELECT (id, scene_track_id, start_frame, end_frame, source, version),
    INSERT (id, scene_track_id, start_frame, end_frame, source, created_by),
    UPDATE (start_frame, end_frame, version, updated_at), DELETE
    ON public.scene_track_intervals TO :"labeling_owner";
GRANT INSERT (presence_mode, attributes, attributes_meta, revision)
    ON public.scene_tracks TO :"labeling_owner";
GRANT SELECT (operation_id, created_by, created_at, updated_at),
    INSERT (version, operation_id)
    ON public.scene_track_intervals TO :"labeling_owner";
GRANT SELECT (
    id, scene_id, actor_id, kind, idempotency_key, request_digest,
    snapshot_token, source_revisions, result_revisions, before_state, after_state,
    inverse_payload, response_json, status, reverted_by_operation_id,
    created_at, completed_at
), INSERT (
    id, scene_id, actor_id, kind, idempotency_key, request_digest,
    snapshot_token, source_revisions, result_revisions, before_state, after_state,
    inverse_payload, response_json, status
), UPDATE (
    result_revisions, after_state, response_json, status, reverted_by_operation_id
) ON public.scene_track_operations TO :"labeling_owner";
GRANT SELECT (
    id, task_id, actor_id, kind, idempotency_key, request_digest,
    scope_fingerprint, source_versions, result_versions, report, status,
    response_json, created_at, completed_at
), INSERT (
    id, task_id, actor_id, kind, idempotency_key, request_digest,
    scope_fingerprint, source_versions, result_versions, report, status, response_json
) ON public.annotation_operations TO :"labeling_owner";
GRANT SELECT (id, annotation_id, annotation_version, created_at, expires_at),
    INSERT (
        id, project_id, task_id, annotation_id, annotation_version, geometry,
        geometry_digest, source_kind, expires_at
    ), UPDATE (expires_at)
    ON public.mask_annotation_revisions TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION public.capture_mask_annotation_revision(),
    public.record_mask_annotation_revision(uuid,uuid,uuid,integer,jsonb,text)
    TO :"labeling_owner";
GRANT SELECT, INSERT ON public.annotation_input_sources TO :"labeling_owner";
GRANT SELECT, INSERT ON public.annotation_video_input_sources TO :"labeling_owner";
GRANT SELECT (id, project_id, task_id, annotation_id, thread_parent_id),
    UPDATE (thread_parent_id), DELETE
    ON public.annotation_feedbacks TO :"labeling_owner";
GRANT SELECT (task_id, annotation_id), DELETE
    ON public.annotation_drafts TO :"labeling_owner";
GRANT SELECT (
    id, project_id, dataset_item_id, display_id, file_name,
    file_path, file_type, media_source_generation, media_object_identity, media_source_drifted,
    tags, status, assignee_id, assignee_is_override, assigned_at,
    is_labeled, overlap, total_annotations, total_predictions, precomputed_agreement,
    sequence_order, thumbnail_path, blurhash, batch_id, version,
    submitted_at, reviewer_id, reviewer_is_override, reviewer_claimed_at, review_round_id,
    first_review_eligible, first_reviewed_at, first_review_result, first_review_contributor_ids, annotation_contributor_ids,
    review_contributor_ids, review_submitter_id, reviewed_at, reject_reason, reject_reason_type,
    skip_reason, skipped_at, reopened_count, last_reopened_at, created_at,
    updated_at, thumbnail_output_id, original_id
) ON public.tasks TO :"labeling_owner";
GRANT UPDATE (original_id) ON public.tasks TO :"labeling_owner";
GRANT SELECT (id, user_id, result, was_postponed, created_at, updated_at),
    INSERT (id, task_id, user_id, result, was_postponed), UPDATE (result, updated_at)
    ON public.annotation_drafts TO :"labeling_owner";
GRANT SELECT (id, unique_id, task_id, user_id, expire_at, created_at),
    INSERT (id, unique_id, task_id, user_id, expire_at),
    UPDATE (id, expire_at), DELETE
    ON public.task_locks TO :"labeling_owner";
GRANT SELECT (project_id, annotation_id), DELETE
    ON public.annotation_comments TO :"labeling_owner";
GRANT SELECT (project_id), DELETE ON public.task_events TO :"labeling_owner";
GRANT SELECT (
    id, annotation_id, project_id, author_id, body,
    is_resolved, is_active, mentions, attachments, canvas_drawing,
    anchor, created_at, updated_at
) ON public.annotation_comments TO :"labeling_owner";
GRANT SELECT (
    id, kind, anchor_type, project_id, task_id,
    annotation_id, anchor_position, status, severity, title,
    body, author_id, attachments, mentions, canvas_drawing,
    thread_parent_id, is_active, resolved_at, resolved_by_id, created_at,
    updated_at
) ON public.annotation_feedbacks TO :"labeling_owner";
GRANT SELECT (
    id, task_id, user_id, project_id, kind,
    started_at, ended_at, duration_ms, annotation_count, was_rejected,
    collector_version, collection_source, collection_coverage, created_at
) ON public.task_events TO :"labeling_owner";
GRANT SELECT (
    id, task_id, actor_id, kind, idempotency_key,
    request_digest, scope_fingerprint, source_versions, result_versions, report,
    status, response_json, created_at, completed_at
) ON public.annotation_operations TO :"labeling_owner";
GRANT SELECT (
    id, operation_id, source_annotation_id, result_annotation_id, relation,
    source_version, result_version, frame_index, created_at
) ON public.annotation_lineage_edges TO :"labeling_owner";
GRANT SELECT (
    id, project_id, task_id, annotation_id, annotation_version,
    geometry, geometry_digest, source_kind, operation_id, actor_id,
    created_at, expires_at
) ON public.mask_annotation_revisions TO :"labeling_owner";
GRANT SELECT (
    id, task_id, idempotency_key, request_digest, candidate_id,
    content_digest, prediction_id, prediction_created_at, annotation_id, source_annotation_id,
    source_version, result_version, actor_id, response_json, created_at,
    expires_at
) ON public.ai_mask_accept_decisions TO :"labeling_owner";
GRANT INSERT (id, annotation_id, project_id, author_id, body, mentions, attachments, canvas_drawing, anchor) ON public.annotation_comments TO :"labeling_owner";
GRANT INSERT (id, kind, anchor_type, project_id, task_id, annotation_id, anchor_position, status, severity, title, body, author_id, attachments, mentions, canvas_drawing, thread_parent_id, is_active) ON public.annotation_feedbacks TO :"labeling_owner";
GRANT INSERT (id, task_id, user_id, project_id, kind, started_at, ended_at, duration_ms, annotation_count, was_rejected, collector_version, collection_source, collection_coverage, created_at) ON public.task_events TO :"labeling_owner";
GRANT INSERT (id, task_id, actor_id, kind, idempotency_key, request_digest, scope_fingerprint, source_versions, result_versions, report, status, response_json, created_at, completed_at) ON public.annotation_operations TO :"labeling_owner";
GRANT INSERT (id, operation_id, source_annotation_id, result_annotation_id, relation, source_version, result_version, frame_index) ON public.annotation_lineage_edges TO :"labeling_owner";
GRANT INSERT (id, task_id, idempotency_key, request_digest, candidate_id, content_digest, prediction_id, prediction_created_at, annotation_id, source_annotation_id, source_version, result_version, actor_id, response_json) ON public.ai_mask_accept_decisions TO :"labeling_owner";
GRANT UPDATE (id) ON public.ai_mask_accept_decisions TO :"labeling_owner";
GRANT UPDATE (body, is_resolved, is_active, attachments, updated_at) ON public.annotation_comments TO :"labeling_owner";
GRANT UPDATE (status, resolved_at, resolved_by_id, severity, title, body, mentions, is_active, updated_at) ON public.annotation_feedbacks TO :"labeling_owner";
GRANT UPDATE (report) ON public.annotation_operations TO :"labeling_owner";
GRANT UPDATE (tool_unit_id, user_id) ON public.annotations TO :"labeling_owner";
GRANT DELETE ON public.mask_annotation_revisions, public.ai_mask_accept_decisions TO :"labeling_owner";
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
    annotation_contributor_ids, reopened_count, batch_id, version, created_at,
    sequence_order
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
GRANT USAGE ON SEQUENCE public.display_seq_tasks, public.display_seq_batches
    TO :"labeling_owner";

GRANT SELECT (id, project_id, display_id, batch_id, status, total_predictions,
    total_annotations, is_labeled, assignee_id, reviewer_id, assigned_at,
    submitted_at, reviewer_claimed_at, reviewed_at, reject_reason,
    reject_reason_type, skip_reason, skipped_at, sequence_order,
    annotation_contributor_ids, review_round_id, review_contributor_ids,
    review_submitter_id, created_at, updated_at)
    ON public.tasks TO :"labeling_owner";
GRANT UPDATE (display_id, sequence_order, status, assignee_id, reviewer_id,
    batch_id, assigned_at, submitted_at, reviewer_claimed_at, reviewed_at,
    reject_reason, reject_reason_type, skip_reason, skipped_at,
    total_annotations, total_predictions, is_labeled,
    annotation_contributor_ids, review_round_id, review_contributor_ids,
    review_submitter_id, created_at, updated_at)
    ON public.tasks TO :"labeling_owner";
GRANT SELECT (id, project_id, dataset_id, display_id, name, description,
    status, priority, assigned_user_ids, total_tasks, completed_tasks,
    review_tasks, approved_tasks, rejected_tasks, created_by, created_at,
    updated_at, annotator_id, reviewer_id)
    ON public.task_batches TO :"labeling_owner";
GRANT INSERT (id, project_id, dataset_id, display_id, name, description,
    status, priority, annotator_id, reviewer_id, assigned_user_ids, created_by,
    created_at, updated_at)
    ON public.task_batches TO :"labeling_owner";
GRANT UPDATE (dataset_id, name, description, status, priority, annotator_id,
    reviewer_id, assigned_user_ids, created_by, created_at, updated_at,
    total_tasks, completed_tasks, review_tasks, approved_tasks, rejected_tasks)
    ON public.task_batches TO :"labeling_owner";
GRANT SELECT (id, task_id, project_id, parent_prediction_id, is_active,
    was_cancelled)
    ON public.annotations TO :"labeling_owner";
GRANT INSERT (id, task_id, project_id, user_id, source, annotation_type,
    tool_unit_id, class_name, geometry, attributes, is_active, was_cancelled,
    ground_truth, version, created_at, updated_at)
    ON public.annotations TO :"labeling_owner";
GRANT UPDATE (task_id, project_id, user_id, source, annotation_type,
    tool_unit_id, class_name, geometry, attributes, is_active, was_cancelled,
    ground_truth, created_at, updated_at)
    ON public.annotations TO :"labeling_owner";
GRANT UPDATE (created_at) ON public.predictions TO :"labeling_owner";
GRANT UPDATE (prediction_created_at, created_at)
    ON public.prediction_metas TO :"labeling_owner";
GRANT DELETE ON public.video_tracker_jobs TO :"labeling_owner";
GRANT SELECT (id, project_id, task_id, kind, author_id, body)
    ON public.annotation_feedbacks TO :"labeling_owner";
GRANT DELETE ON public.annotation_feedbacks TO :"labeling_owner";
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

GRANT SELECT ON public.async_jobs TO :"jobs_owner";
GRANT INSERT (id, kind, project_id, user_id, status, progress_pct, payload, result,
    celery_task_id, created_at, updated_at)
    ON public.async_jobs TO :"jobs_owner";
GRANT UPDATE (
    status, progress_pct, result, error_message, celery_task_id, started_at,
    completed_at, updated_at, project_id, retained_project_id, payload
) ON public.async_jobs TO :"jobs_owner";

GRANT SELECT (key, value_type, value_json) ON public.system_settings TO :"settings_owner";
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

GRANT USAGE ON SCHEMA yoao_settings_v1 TO :"settings_owner", :"assets_owner", :"identity_owner";
GRANT USAGE ON SCHEMA yoao_notifications_v1
    TO :"notifications_owner", :"jobs_owner", :"identity_owner";
GRANT USAGE ON SCHEMA yoao_identity_v1 TO :"jobs_owner", :"notifications_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.lock_active_actor(uuid) TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.read_actor_facts(uuid)
    TO :"notifications_owner";
GRANT SELECT (id, role, is_active) ON yoao_identity_v1.account_profiles
    TO :"notifications_owner";
GRANT USAGE ON SCHEMA yoao_workspace_v1, yoao_workspace_project_query_v1, yoao_jobs_v1
    TO :"notifications_owner";
GRANT EXECUTE ON FUNCTION yoao_workspace_v1.actor_project_memberships(uuid),
    yoao_workspace_project_query_v1.project_search_scope(uuid,text)
    TO :"notifications_owner";
GRANT SELECT (id, kind, scope_project_id) ON yoao_jobs_v1.notification_scope_facts
    TO :"notifications_owner";
GRANT EXECUTE ON FUNCTION yoao_settings_v1.resolve_connector_host_allowlist(jsonb)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_settings_v1.lock_connector_host_allowlist(jsonb)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_notifications_v1.create_job_terminal_notification(
    uuid, uuid, text, jsonb
) TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.create_dataset_import_job(uuid,uuid,jsonb),
    yoao_jobs_v1.lock_dataset_import_job(uuid),
    yoao_jobs_v1.claim_dataset_import_job(uuid),
    yoao_jobs_v1.update_dataset_import_job_progress(uuid,text,integer,integer,bigint),
    yoao_jobs_v1.finish_dataset_import_job(uuid,text,jsonb,text),
    yoao_jobs_v1.cancel_dataset_import_job(uuid,uuid,jsonb)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.record_dataset_import(uuid,uuid,uuid,jsonb,jsonb)
    TO :"assets_owner";

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

REVOKE SELECT (id, ml_backend_pool_id) ON public.projects FROM :"inference_owner";
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
GRANT SELECT (project_id, task_id), UPDATE (project_id, task_id)
    ON public.bug_reports TO :"diagnostics_owner";
GRANT SELECT (project_id), DELETE ON public.export_artifacts TO :"export_owner";

GRANT USAGE ON SCHEMA yoao_identity_v1
    TO :"identity_owner", :"workspace_owner", :"assets_owner", :"audit_owner",
       :"labeling_owner", :"learning_owner", :"inference_owner", :"workflow_role";
GRANT USAGE ON SCHEMA yoao_identity_keys_v1, yoao_identity_sessions_v1,
    yoao_identity_accounts_v1, yoao_identity_recovery_v1, yoao_identity_groups_v1,
    yoao_identity_deactivation_v1, yoao_identity_invitations_v1,
    yoao_identity_mail_v2, yoao_identity_directory_v1,
    yoao_identity_admin_passwords_v1, yoao_identity_platform_roles_v1,
    yoao_identity_suspensions_v1, yoao_identity_deletions_v1,
    yoao_identity_offboarding_v1, yoao_identity_briefs_v1
    TO :"identity_owner";
GRANT USAGE ON SCHEMA yoao_workspace_v1 TO :"workspace_owner", :"assets_owner",
    :"labeling_owner", :"jobs_owner", :"workflow_role", :"identity_owner",
    :"inference_owner";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"assets_owner", :"labeling_owner",
    :"jobs_owner", :"learning_owner", :"workspace_owner", :"workflow_role",
    :"asset_cleanup_role";
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"labeling_owner", :"jobs_owner",
    :"assets_owner", :"learning_owner", :"diagnostics_owner",
    :"workspace_owner", :"workflow_role", :"identity_owner";
GRANT USAGE ON SCHEMA yoao_jobs_v1 TO :"jobs_owner", :"labeling_owner",
    :"workspace_owner", :"assets_owner", :"workflow_role", :"inference_owner",
    :"export_owner";
GRANT USAGE ON SCHEMA yoao_inference_routing_v1 TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION pg_catalog.pg_control_system() TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.video_execution_step(text,text),
    yoao_jobs_v1.video_execution_checkpoint_step(text,text)
    TO :"labeling_owner", :"workflow_role";
GRANT USAGE ON SCHEMA yoao_learning_v1
    TO :"learning_owner", :"workspace_owner", :"assets_owner", :"inference_owner";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.registry_evaluation_reference_exists(uuid),
    yoao_jobs_v1.count_running_registry_predictions(uuid),
    yoao_jobs_v1._video_execution_route_step(text,text),
    yoao_jobs_v1._video_execution_scope_is_current(uuid,boolean)
    TO :"inference_owner";
GRANT EXECUTE ON FUNCTION
    yoao_inference_routing_v1._video_execution_canonical_json(jsonb),
    yoao_inference_routing_v1._video_execution_no_scope_rejection(uuid,uuid,text)
    TO :"jobs_owner";
GRANT USAGE ON SCHEMA yoao_inference_v1 TO :"inference_owner", :"workspace_owner";
GRANT USAGE ON SCHEMA yoao_workspace_configuration_v1 TO :"workspace_owner";
GRANT USAGE ON SCHEMA yoao_workspace_template_query_v1 TO :"workspace_owner";
GRANT USAGE ON SCHEMA yoao_workspace_directory_query_v1 TO :"workspace_owner";
GRANT USAGE ON SCHEMA yoao_labeling_class_query_v1 TO :"labeling_owner";
GRANT USAGE ON SCHEMA yoao_workspace_project_query_v1
    TO :"workspace_owner", :"labeling_owner", :"identity_owner", :"inference_owner";
GRANT EXECUTE ON FUNCTION yoao_workspace_project_query_v1.project_search_scope(uuid, text)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_workspace_project_query_v1.project_search_member_ids(uuid, text)
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION
    yoao_labeling_v1.workspace_project_query_counters(uuid[]),
    yoao_labeling_v1.workspace_project_query_ai_counts(uuid[]),
    yoao_labeling_v1.workspace_project_query_annotation_count(uuid[], boolean),
    yoao_labeling_v1.workspace_project_query_task_series(uuid[], timestamptz[]),
    yoao_labeling_v1.workspace_project_query_annotation_series(uuid[], timestamptz[]),
    yoao_labeling_v1.workspace_project_query_linked_task_count(uuid, uuid[]),
    yoao_labeling_v1.workspace_project_query_task_count(uuid),
    yoao_labeling_v1.workspace_project_query_batches(uuid),
    yoao_labeling_v1.workspace_project_query_batch_task_counts(uuid),
    yoao_assets_v1.workspace_project_query_dataset_ids(uuid),
    yoao_assets_v1.workspace_project_query_dataset_item_count(uuid[]),
    yoao_jobs_v1.workspace_project_query_job_status_counts(uuid),
    yoao_jobs_v1.workspace_project_query_latest_job_status(uuid),
    yoao_inference_v1.workspace_project_query_pool(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.workspace_project_query_dataset_item_ids(uuid[])
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_configuration_v1.configuration_policy(),
    yoao_workspace_configuration_v1.configuration_step(jsonb, text, jsonb, text[], text[])
    TO :"workspace_owner";
GRANT USAGE ON SCHEMA yoao_insights_v1 TO :"insights_owner", :"workspace_owner";
GRANT USAGE ON SCHEMA yoao_labeling_v1, yoao_workspace_v1, yoao_assets_v1 TO :"insights_owner";
GRANT SELECT (user_id, project_id, kind, started_at, duration_ms, annotation_count, was_rejected)
    ON yoao_labeling_v1.task_event_facts TO :"insights_owner";
GRANT EXECUTE ON FUNCTION yoao_workspace_v1.read_project_visibility(uuid,uuid),
    yoao_workspace_v1.lock_media_project_access(uuid,uuid),
    yoao_assets_v1.lock_dataset_reference(uuid) TO :"insights_owner";
GRANT USAGE ON SCHEMA yoao_diagnostics_v1
    TO :"diagnostics_owner", :"workspace_owner";
GRANT USAGE ON SCHEMA yoao_export_v1 TO :"export_owner", :"workspace_owner";
GRANT USAGE ON SCHEMA yoao_audit_v1
    TO :"identity_owner", :"audit_owner", :"workspace_owner", :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.record_identity_session_event(
    text, jsonb, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.record_identity_account_event(
    text, jsonb, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.record_identity_bootstrap_admin(
    uuid, text, text
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.record_identity_recovery_event(
    text, jsonb, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.record_identity_group_event(
    text, jsonb, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.record_identity_deactivation_event(
    text, jsonb, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.record_identity_invitation_event(
    text, jsonb, jsonb, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.valid_request_context(jsonb)
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_settings_v1.identity_recovery_policy(jsonb)
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_settings_v1.invitation_policy(jsonb)
    TO :"identity_owner";
GRANT USAGE ON SCHEMA yoao_workspace_v1 TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_workspace_v1.lock_group_assignment_scope(
    uuid, uuid[], boolean
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_workspace_v1.invitation_projects(uuid[]),
    yoao_workspace_v1.lock_invitation_project(uuid, uuid),
    yoao_workspace_v1.read_invitation_membership(uuid, uuid),
    yoao_workspace_v1.grant_invitation_membership(uuid, uuid, text, uuid, uuid)
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_v1.identity_directory_owned_member_ids(uuid),
    yoao_workspace_v1.identity_directory_project_member_ids(uuid),
    yoao_workspace_v1.identity_directory_lifecycle_blocked(uuid, uuid[])
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_v1.admin_password_target_in_owned_project(uuid, uuid)
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_v1.identity_platform_role_facts(uuid),
    yoao_workspace_v1.lock_identity_platform_role_owned_projects(uuid)
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_workspace_v1.lock_identity_suspension_scope(
    uuid, uuid, text
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_v1.lock_identity_deletion_scope(uuid, uuid),
    yoao_workspace_v1.identity_deletion_transfer_covered(uuid, uuid[])
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_v1.lock_identity_offboarding_scope(uuid, uuid[], uuid[]),
    yoao_workspace_v1.identity_offboarding_replay_scope(uuid, uuid[]),
    yoao_workspace_v1.identity_offboarding_facts(uuid),
    yoao_workspace_v1.transfer_identity_offboarding_owners(uuid, jsonb)
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_v1.identity_brief_project_roles(uuid, uuid[])
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION
    yoao_identity_v1.read_workspace_membership_accounts(uuid[]),
    yoao_identity_v1.workspace_directory_superadmins()
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION
    yoao_audit_v1.valid_request_context(jsonb),
    yoao_audit_v1.record_workspace_membership_event(text, jsonb, uuid, jsonb, jsonb)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.workspace_configuration_datasets(uuid),
    yoao_labeling_v1.workspace_configuration_video_facts(uuid),
    yoao_labeling_v1.rename_project_annotation_class(uuid, text, text),
    yoao_inference_v1.enable_workspace_project_backend(uuid, uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.workspace_configuration_dataset_rows(uuid),
    yoao_assets_v1.workspace_configuration_dataset_scenes(uuid),
    yoao_assets_v1.read_media_summaries(uuid[]),
    yoao_labeling_v1.workspace_configuration_task_count(uuid),
    yoao_labeling_v1.workspace_configuration_annotation_count(uuid),
    yoao_labeling_v1.workspace_configuration_tracker_count(uuid),
    yoao_labeling_v1.workspace_configuration_video_items(uuid),
    yoao_inference_v1.workspace_configuration_source_pool(uuid),
    yoao_inference_v1.workspace_configuration_backend(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.workspace_configuration_existing_item_ids(uuid[])
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    yoao_labeling_v1.read_membership_work(uuid, uuid, uuid, uuid),
    yoao_labeling_v1.lock_membership_work(uuid, uuid, text, boolean),
    yoao_labeling_v1.handoff_membership_annotation(uuid, uuid, uuid),
    yoao_labeling_v1.handoff_membership_reviewer(uuid, uuid, uuid),
    yoao_labeling_v1.release_membership_task_locks(uuid, uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION
    yoao_labeling_v1.identity_platform_role_facts(uuid, uuid[])
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION
    yoao_labeling_v1.identity_deletion_work(uuid),
    yoao_labeling_v1.transfer_identity_deletion_work(uuid, uuid)
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION
    yoao_labeling_v1.lock_identity_offboarding_work(uuid, uuid[]),
    yoao_labeling_v1.identity_offboarding_facts(uuid),
    yoao_labeling_v1.transfer_identity_offboarding_work(uuid, jsonb),
    yoao_labeling_v1.release_identity_offboarding_task_locks(uuid)
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.identity_directory_project_refs(uuid[])
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.record_identity_directory_export(
    jsonb, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.record_identity_admin_password_reset(
    jsonb, uuid, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.record_identity_platform_role_change(
    jsonb, uuid, jsonb, text, text, text
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.record_identity_suspension_event(
    text, jsonb, uuid, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.record_identity_deletion_event(
    jsonb, uuid, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.record_identity_offboarding_event(
    jsonb, uuid, jsonb, jsonb
) TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.invitation_work_summary(uuid, uuid, text)
    TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_notifications_v1.create_identity_deactivation_notifications(
    uuid, text, jsonb, uuid[]
) TO :"identity_owner";

GRANT EXECUTE ON FUNCTION yoao_identity_v1.lock_active_actor(uuid)
    TO :"workspace_owner", :"assets_owner", :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.read_actor_facts(uuid)
    TO :"workspace_owner", :"assets_owner", :"labeling_owner", :"learning_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.lock_current_principal(
    uuid, text, uuid, bigint, timestamptz, text[]
) TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    yoao_identity_v1.workspace_configuration_org_scope(uuid, uuid),
    yoao_identity_v1.workspace_template_query_org_ids(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION
    yoao_identity_v1.lock_accounts_share(uuid[], boolean),
    yoao_identity_v1.lock_accounts_update(uuid[], boolean),
    yoao_identity_v1.lock_accounts_key_share(uuid[], boolean)
    TO :"workspace_owner", :"workflow_role";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.lock_accounts_share(uuid[], boolean)
    TO :"labeling_owner", :"learning_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.check_accepted_api_key(uuid, uuid, text[])
    TO :"learning_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.lock_accounts_update(uuid[], boolean)
    TO :"inference_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.read_actor_facts(uuid)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.lock_audit_actor(uuid)
    TO :"audit_owner";
GRANT EXECUTE ON FUNCTION yoao_workspace_v1.lock_project_reference(uuid)
    TO :"assets_owner", :"inference_owner";
GRANT SELECT ON yoao_workspace_v1.project_catalog_configs TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_workspace_v1.read_project_visibility(uuid, uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_workspace_v1.lock_project_manager(uuid, uuid)
    TO :"assets_owner", :"labeling_owner", :"jobs_owner", :"workflow_role";
GRANT EXECUTE ON FUNCTION yoao_workspace_v1.read_project_row(uuid)
    TO :"labeling_owner";
GRANT SELECT (project_id, user_id, role) ON yoao_workspace_v1.project_member_facts
    TO :"labeling_owner";
GRANT SELECT (id, name) ON yoao_workspace_v1.project_facts
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_workspace_v1.project_access_step(uuid, uuid, text)
    TO :"inference_owner";
GRANT EXECUTE ON FUNCTION yoao_workspace_v1.clear_primary_backend_bindings(uuid)
    TO :"inference_owner";
GRANT EXECUTE ON FUNCTION yoao_workspace_project_query_v1.project_query_step(
    jsonb, text, jsonb, text, text, text[], text[], uuid, text,
    timestamptz, timestamptz, bigint, integer
) TO :"inference_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.workspace_template_query_org_ids(uuid)
    TO :"inference_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.lock_dataset_reference(uuid),
    yoao_assets_v1.lock_current_association(uuid, uuid),
    yoao_assets_v1.lock_append_sources(uuid, uuid[]),
    yoao_assets_v1.lock_task_creation_items(uuid, uuid[])
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.invalidate_task_image_source(uuid)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.lock_current_association(uuid, uuid)
    TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.lock_dataset_reference(uuid)
    TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.lock_current_association(uuid, uuid),
    yoao_assets_v1.lock_dataset_reference(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.lock_scene_source_references(uuid[])
    TO :"labeling_owner", :"workflow_role";
GRANT SELECT ON yoao_assets_v1.project_dataset_item_sources TO :"labeling_owner";
GRANT SELECT ON yoao_labeling_v1.project_catalog_counters TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.project_dataset_task_counts(uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.project_accepted_image_input_job(uuid, uuid, uuid, boolean)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    yoao_labeling_v1.project_video_tracker_input(uuid,uuid,uuid,uuid,text,boolean,boolean,boolean),
    yoao_labeling_v1.fail_video_tracker_input_preparation(uuid,uuid,uuid,uuid,text,text),
    yoao_labeling_v1.video_tracker_input_referenced(uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.read_video_tracker_task_source(uuid,uuid,jsonb),
    yoao_assets_v1._lock_video_tracker_input_plan(uuid,uuid,uuid,uuid,text,text,boolean)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.accepted_image_input_job_consistency(uuid, uuid, uuid, uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    yoao_jobs_v1.project_accepted_image_input_manifest_job(uuid, uuid, uuid, uuid, jsonb, text, boolean),
    yoao_jobs_v1.accepted_image_input_manifest_job_consistency(uuid, uuid, uuid, uuid, jsonb, text)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    yoao_jobs_v1.project_video_input_job(uuid, uuid, uuid, uuid, jsonb, text, boolean),
    yoao_labeling_v1.read_video_input_task_source(uuid, uuid),
    yoao_labeling_v1.lock_video_input_task_source(uuid, uuid, jsonb)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    yoao_labeling_v1.open_video_input_manifest_members(uuid, uuid, jsonb),
    yoao_labeling_v1.lock_video_input_manifest_member(uuid, uuid, uuid, jsonb, jsonb)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    yoao_labeling_v1.project_accepted_image_input_task(uuid, uuid, boolean),
    yoao_labeling_v1.accepted_image_input_referenced(uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.accepted_video_input_referenced(uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.associate_project(uuid, uuid, uuid)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.delete_project_owned_records(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.project_task_references(uuid)
    TO :"assets_owner", :"learning_owner", :"diagnostics_owner";
GRANT EXECUTE ON FUNCTION
    yoao_labeling_v1.read_image_task_source(uuid, uuid, boolean),
    yoao_labeling_v1.read_image_task_source_share(uuid, uuid, boolean),
    yoao_labeling_v1.observe_image_task_source(uuid, text, jsonb),
    yoao_labeling_v1.publish_task_image_thumbnail(uuid, text, bigint, text, text),
    yoao_labeling_v1.image_thumbnail_references(text),
    yoao_labeling_v1.scan_image_task_sources(jsonb, uuid, integer),
    yoao_labeling_v1.invalidate_task_image_thumbnail(uuid, text, text)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    yoao_labeling_v1.open_accepted_image_input_manifest_members(uuid, uuid, jsonb),
    yoao_labeling_v1.lock_accepted_image_input_manifest_member(uuid, uuid, uuid, uuid, uuid, text)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.lock_accepted_image_input(uuid, uuid, uuid)
    TO :"workflow_role", :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1._lock_accepted_image_input_receipt(uuid, uuid, uuid)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1._lock_accepted_image_input_pin(uuid, uuid, text)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.attach_annotation_input_source(uuid, uuid, text)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.copy_annotation_input_sources(uuid, uuid)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.find_accepted_image_input(uuid, uuid, uuid),
    yoao_assets_v1.reserve_accepted_image_input(uuid, uuid, uuid, text, jsonb),
    yoao_assets_v1.read_accepted_image_input(uuid, uuid, uuid),
    yoao_assets_v1.read_accepted_image_input_work(uuid, uuid, uuid)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.capture_accepted_image_input_manifest(
    uuid, uuid, uuid, jsonb
) TO :"workflow_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.capture_accepted_video_manifest(uuid,uuid,uuid,jsonb)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.lock_accepted_video_input(uuid,uuid,uuid,uuid,text),
    yoao_assets_v1.lock_accepted_video_frame(uuid,uuid,uuid,uuid,text,bigint,jsonb,jsonb),
    yoao_assets_v1._lock_accepted_video_input_pin(uuid,uuid,jsonb,uuid)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    yoao_labeling_v1.attach_annotation_video_input_source(uuid,uuid,jsonb),
    yoao_labeling_v1.copy_annotation_video_input_sources(uuid,uuid)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.dataset_item_references(uuid, uuid)
    TO :"labeling_owner", :"learning_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.dataset_item_scene_references(uuid, uuid)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.dataset_scene_references(uuid),
    yoao_assets_v1.dataset_association_references(uuid)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.dataset_task_references(uuid, uuid)
    TO :"assets_owner", :"learning_owner", :"diagnostics_owner";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.delete_project_owned_records(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.append_dataset_items(uuid, uuid[], uuid),
    yoao_labeling_v1.accept_dataset_task_creation(uuid, uuid, uuid, uuid, uuid, uuid, text, integer),
    yoao_labeling_v1.prepare_dataset_task_creation(uuid, jsonb),
    yoao_labeling_v1.read_dataset_task_creation(uuid),
    yoao_labeling_v1.advance_dataset_task_creation(uuid, integer)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.append_dataset_items(uuid, uuid[], uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.read_dataset_task_creation(uuid)
    TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.lock_task_creation_job(uuid, uuid, uuid, uuid, uuid, uuid),
    yoao_jobs_v1.update_task_creation_progress(uuid, uuid, bigint, bigint, bigint)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.create_task_creation_job(uuid, uuid, uuid, uuid, uuid),
    yoao_jobs_v1.find_task_creation_job(uuid, uuid, uuid, uuid),
    yoao_jobs_v1.claim_task_creation_job(uuid, text),
    yoao_jobs_v1.finish_task_creation_job(uuid, uuid),
    yoao_jobs_v1.fail_task_creation_job(uuid, uuid, text),
    yoao_jobs_v1.cancel_task_creation_job(uuid, uuid)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.detach_project_references(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.cancel_dataset_task_creation(uuid, uuid, uuid)
    TO :"workspace_owner", :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.guard_dataset_source_deletion(uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.task_creation_execution_active(uuid, uuid)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.lock_project_deletion(uuid),
    yoao_learning_v1.guard_project_deletion(uuid),
    yoao_learning_v1.delete_project_owned_records(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION yoao_inference_v1.clear_project_ml_backend_pool(uuid),
    yoao_inference_v1.clear_project_pipelines(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION yoao_insights_v1.clear_project_task_views(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION yoao_diagnostics_v1.detach_project_references(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION yoao_export_v1.clear_project_export_artifacts(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.record_project_deletion(
    uuid, uuid, uuid, text, text, bigint, bigint, jsonb
) TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.guard_dataset_unlink(uuid, uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.guard_dataset_source_deletion(uuid, uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.guard_dataset_source_deletion(uuid, uuid),
    yoao_labeling_v1.detach_deleted_dataset_attribution(uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.lock_dataset_unlink_scope(uuid, uuid),
    yoao_labeling_v1.guard_dataset_unlink(uuid, uuid),
    yoao_labeling_v1.preview_dataset_unlink(uuid, uuid),
    yoao_labeling_v1.delete_dataset_tasks(uuid, uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.prepare_dataset_unlink_cleanup(
    uuid, uuid, uuid, text
), yoao_assets_v1.remove_dataset_association(uuid, uuid, uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION yoao_diagnostics_v1.detach_dataset_task_references(
    uuid, uuid
) TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.valid_request_context(jsonb),
    yoao_audit_v1.record_dataset_unlink(
        uuid, uuid, uuid, uuid, bigint, bigint, uuid[], jsonb
    ) TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.valid_request_context(jsonb),
    yoao_audit_v1.record_dataset_source_deletion(
        uuid, uuid, uuid, uuid, bigint, bigint, bigint, bigint, bigint, bigint, jsonb
    ) TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.pending_storage_deletions_v2(
    text, timestamptz, uuid, uuid, integer
),
    yoao_assets_v1.complete_storage_deletion(uuid, uuid),
    yoao_assets_v1.claim_image_garbage(uuid, integer, integer),
    yoao_assets_v1.complete_image_garbage(uuid, uuid),
    yoao_assets_v1.claim_accepted_input_garbage(uuid, integer, integer),
    yoao_assets_v1.complete_accepted_input_garbage(uuid, uuid),
    yoao_assets_v1.claim_accepted_video_input_garbage(uuid,integer,integer),
    yoao_assets_v1.complete_accepted_video_input_garbage(uuid,uuid),
    yoao_assets_v1.claim_native_pyramid_retirement(uuid,integer),
    yoao_assets_v1.prepare_native_pyramid_retirement_page(uuid,uuid,integer),
    yoao_assets_v1.complete_native_pyramid_retirement_page(uuid,uuid,uuid,text,jsonb)
    TO :"asset_cleanup_role";
REVOKE ALL ON FUNCTION yoao_assets_v1.pending_storage_deletions(uuid, integer)
    FROM :"asset_cleanup_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.read_dataset_source_authority(uuid, boolean)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.delete_dataset(
    uuid, uuid, uuid, text, jsonb
), yoao_assets_v1.delete_dataset_item(
    uuid, uuid, uuid, uuid, text, jsonb
), yoao_assets_v1.reserve_dataset_upload(
    uuid, uuid, uuid, text, text
), yoao_assets_v1.lock_dataset_upload(uuid, uuid, uuid),
    yoao_assets_v1.find_dataset_upload_duplicate(uuid, uuid, uuid, text),
    yoao_assets_v1.complete_dataset_upload(
        uuid, uuid, uuid, uuid, bigint, text, integer, integer
    ), yoao_assets_v1.discard_dataset_upload(uuid, uuid, uuid, text)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.begin_dataset_ingest(uuid,uuid,uuid,jsonb),
    yoao_assets_v1.prepare_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb),
    yoao_assets_v1.stage_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb),
    yoao_assets_v1.complete_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb),
    yoao_assets_v1.read_dataset_ingest_layout(uuid,uuid,uuid,uuid,integer),
    yoao_assets_v1.finish_dataset_ingest(uuid,uuid,uuid,jsonb)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.claim_dataset_source_ingest_delivery(uuid,integer),
    yoao_assets_v1.complete_dataset_source_ingest_delivery(uuid,uuid),
    yoao_assets_v1.dataset_import_delivery_target_is_current(text,uuid)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION yoao_workspace_v1.preview_dataset_unlink(uuid, uuid, uuid),
    yoao_workspace_v1.unlink_dataset(uuid, uuid, uuid, uuid, text, jsonb)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION yoao_workspace_v1.delete_project(uuid, uuid, uuid, jsonb)
    TO :"workflow_role";

GRANT CREATE ON SCHEMA yoao_identity_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA yoao_identity_keys_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA yoao_identity_sessions_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA yoao_identity_accounts_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA yoao_identity_recovery_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA yoao_identity_groups_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA yoao_identity_directory_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA yoao_identity_admin_passwords_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA yoao_identity_platform_roles_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA yoao_identity_suspensions_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA yoao_identity_deletions_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA yoao_identity_offboarding_v1 TO :"identity_owner";
GRANT CREATE ON SCHEMA yoao_identity_briefs_v1 TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.lock_active_actor(uuid) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.lock_audit_actor(uuid) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.read_actor_facts(uuid) OWNER TO :"identity_owner";
ALTER VIEW yoao_identity_v1.account_profiles OWNER TO :"identity_owner";
ALTER VIEW yoao_identity_v1.invitation_audit_targets OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.check_accepted_api_key(uuid, uuid, text[])
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.workspace_configuration_org_scope(uuid, uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.workspace_template_query_org_ids(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.read_provisioning_account(text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.create_provisioning_account(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.create_seed_persona_account(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.replace_fixture_preferences(uuid,jsonb)
    OWNER TO :"identity_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_workspace_fixture_v1 TO :"workspace_owner";
ALTER VIEW yoao_workspace_fixture_v1.templates OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_fixture_v1.create_project(jsonb) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_fixture_v1.create_templates(jsonb) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_fixture_v1.guard_project_cleanup(uuid[]) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_fixture_v1.delete_projects(uuid[]) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_fixture_v1.delete_templates(boolean,text[]) OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA yoao_workspace_fixture_v1 FROM :"workspace_owner";

GRANT USAGE ON SCHEMA yoao_identity_fixture_v1 TO :"identity_owner";
GRANT DELETE ON public.users,public.password_reset_tokens,public.user_invitations,public.organization_members
    TO :"identity_owner";
GRANT INSERT (accepted_at,accepted_user_id,revoked_at) ON public.user_invitations
    TO :"identity_owner";
ALTER VIEW yoao_identity_fixture_v1.account_profiles OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_fixture_v1.create_account(uuid,text,text,text,text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_fixture_v1.mark_manual_inactive(uuid,timestamptz,uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_fixture_v1.create_invitations(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_fixture_v1.delete_accounts(boolean,text[])
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_fixture_v1.delete_reset_tokens(uuid[])
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_fixture_v1.delete_invitations(uuid[])
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_fixture_v1.delete_organization_members(uuid[])
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.read_first_seed_account()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.read_scale_seed_super_admin()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.read_scale_seed_annotator(uuid[])
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1._admission_failure(text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.authentication_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.read_actor_context(uuid, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1._initialize_account_presence()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1._initialize_api_key_usage()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1._initialize_account_preferences()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.lock_accounts_share(uuid[], boolean)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.lock_accounts_update(uuid[], boolean)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.lock_accounts_key_share(uuid[], boolean)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.read_jwt_principal(
    uuid, uuid, bigint, timestamptz, boolean
) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.read_api_key_candidates(text, uuid, integer)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.confirm_api_key_principal(uuid, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.get_credential_generation(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.advance_credential_generation(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.revoke_jti(uuid, timestamptz)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.is_jti_revoked(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.retire_account_credentials(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.lock_current_principal(
    uuid, text, uuid, bigint, timestamptz, text[]
) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.purge_expired_revocations(integer)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.lock_super_admin_invariant()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.super_admin_status(uuid)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA yoao_identity_v1 FROM :"identity_owner";
ALTER FUNCTION yoao_identity_keys_v1.key_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_keys_v1.execute_key_command(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA yoao_identity_keys_v1 FROM :"identity_owner";
ALTER FUNCTION yoao_identity_sessions_v1._failure(text, bigint)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_sessions_v1.session_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_sessions_v1.read_login_candidate(text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_sessions_v1.execute_session_command(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA yoao_identity_sessions_v1 FROM :"identity_owner";
ALTER FUNCTION yoao_identity_accounts_v1._failure(text, smallint, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_accounts_v1._account_record(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_accounts_v1._lock_current_actor(jsonb, text, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_accounts_v1.account_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_accounts_v1.prepare_account_command(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_accounts_v1.apply_account_command(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_accounts_v1.mark_inactive_accounts_offline(timestamptz)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA yoao_identity_accounts_v1 FROM :"identity_owner";
ALTER FUNCTION yoao_identity_recovery_v1._failure(text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_recovery_v1._mail_failure(text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_recovery_v1._actor_record(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_recovery_v1._token_record(text, uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_recovery_v1.recovery_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_recovery_v1.prepare_recovery_command(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_recovery_v1.apply_recovery_command(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_recovery_v1.read_registration_status(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_recovery_v1.mail_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_recovery_v1.claim_mail(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_recovery_v1.renew_mail(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_recovery_v1.finish_mail(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA yoao_identity_recovery_v1 FROM :"identity_owner";
ALTER FUNCTION yoao_identity_groups_v1._failure(text, smallint, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_groups_v1._strip_unicode_space(text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_groups_v1._account_record(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_groups_v1._group_record(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_groups_v1._group_scope(uuid, uuid[], boolean)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_groups_v1._group_actor(jsonb, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_groups_v1.group_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_groups_v1.execute_group_command(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_groups_v1.resolve_invitation_group(uuid, text)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA yoao_identity_groups_v1 FROM :"identity_owner";
GRANT CREATE ON SCHEMA yoao_identity_deactivation_v1 TO :"identity_owner";
ALTER FUNCTION yoao_identity_deactivation_v1._failure(text, smallint, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_deactivation_v1._due_failure(text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_deactivation_v1._strip_unicode_space(text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_deactivation_v1._account_record(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_deactivation_v1._actor(jsonb, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_deactivation_v1._reconcile_request(uuid, timestamptz, timestamptz)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_deactivation_v1.deactivation_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_deactivation_v1.due_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_deactivation_v1.execute_deactivation_command(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_deactivation_v1.process_due_deactivations(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA yoao_identity_deactivation_v1 FROM :"identity_owner";

GRANT CREATE ON SCHEMA yoao_identity_invitations_v1, yoao_identity_mail_v2
    TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1.bind_fixture_invitation()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1.bind_fixture_invitation_birth(uuid, uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1.lock_fixture_invitation_run(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1.guard_fixture_invitation_acceptance()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1._failure(text, text, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1._strip_unicode_space(text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1._account_record(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1._invitation_record(uuid, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1._project_map(uuid[])
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1._policy(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1._lock_users(uuid[])
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1._actor(jsonb, text, boolean, boolean)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1.invitation_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1.invitation_quota_reached(
    uuid, timestamptz, integer
) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1._empty_result()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1._incompatible_role_detail(text, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1._args_valid(text, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1._read_result(text, uuid, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1._native_actor(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1._project_record(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1._batch_snapshot(uuid, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1.prepare_invitation_command(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1._batch_effects(
    boolean, uuid, text, text, jsonb, jsonb, jsonb, jsonb, jsonb
) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1._run_invitation_preview(
    uuid, text, text, jsonb, jsonb, jsonb, jsonb
) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1.apply_invitation_command(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_invitations_v1.lock_invitation_mail_scope(uuid, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_mail_v2.mail_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_mail_v2.claim_mail(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_mail_v2.renew_mail(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_mail_v2.finish_mail(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA yoao_identity_invitations_v1, yoao_identity_mail_v2
    FROM :"identity_owner";

ALTER FUNCTION yoao_identity_directory_v1._failure(text, integer, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_directory_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_directory_v1._strip_unicode_space(text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_directory_v1._valid_payload(text, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_directory_v1._directory_user(uuid)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_directory_v1._scope_user_ids(
    uuid, text, text, jsonb, boolean
) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_directory_v1.directory_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_directory_v1.execute_directory_command(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA yoao_identity_directory_v1 FROM :"identity_owner";

ALTER FUNCTION yoao_identity_admin_passwords_v1._failure(text, integer, text)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_admin_passwords_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_admin_passwords_v1.admin_password_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_admin_passwords_v1.execute_admin_password_command(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA yoao_identity_admin_passwords_v1 FROM :"identity_owner";
ALTER FUNCTION yoao_identity_platform_roles_v1._failure(text, integer, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_platform_roles_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_platform_roles_v1.platform_role_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_platform_roles_v1.execute_platform_role_command(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_platform_roles_v1._preview_snapshot(uuid)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA yoao_identity_platform_roles_v1 FROM :"identity_owner";
ALTER FUNCTION yoao_identity_suspensions_v1._failure(text, integer, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_suspensions_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_suspensions_v1.suspension_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_suspensions_v1.execute_suspension_command(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA yoao_identity_suspensions_v1 FROM :"identity_owner";
ALTER FUNCTION yoao_identity_deletions_v1._failure(text, integer, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_deletions_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_deletions_v1.deletion_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_deletions_v1.execute_deletion_command(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA yoao_identity_deletions_v1 FROM :"identity_owner";
ALTER FUNCTION yoao_identity_offboarding_v1._failure(text, integer, jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_offboarding_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_offboarding_v1.offboarding_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_offboarding_v1.prepare_offboarding_command(jsonb)
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_offboarding_v1.apply_offboarding_command(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA yoao_identity_offboarding_v1 FROM :"identity_owner";
ALTER FUNCTION yoao_identity_briefs_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_briefs_v1._failure() OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_briefs_v1.briefs_policy()
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_briefs_v1.execute_briefs_command(jsonb)
    OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA yoao_identity_briefs_v1 FROM :"identity_owner";

GRANT CREATE ON SCHEMA yoao_workspace_v1 TO :"workspace_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_workspace_membership_v1
    TO :"workspace_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_workspace_owner_transfer_v1
    TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.lock_project_reference(uuid) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.lock_project_reference_nowait(uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.read_project_row(uuid) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.actor_project_memberships(uuid) OWNER TO :"workspace_owner";
ALTER VIEW yoao_workspace_v1.project_member_facts OWNER TO :"workspace_owner";
ALTER VIEW yoao_workspace_v1.project_facts OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.lock_project_references_for_update(uuid[])
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.lock_project_member_reference(uuid, uuid, boolean)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.project_member_rows(uuid, uuid[], boolean)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1._project_read_failure(text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.read_project_visibility(uuid, uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.lock_project_manager(uuid, uuid) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.project_access_step(uuid, uuid, text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.lock_project_row(uuid, boolean)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.clear_primary_backend_bindings(uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.read_import_project(text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.create_import_project(uuid,text,text,uuid,jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.create_point_cloud_seed_project(uuid,text,text,uuid,jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.write_seed_members(jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.pin_seed_member_assignment(uuid,timestamptz)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.write_point_cloud_quality_config(uuid,jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.create_media_seed_project(uuid,text,text,uuid,text,jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.write_seed_project_projection(uuid,jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.create_scale_seed_project(uuid,text) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.ensure_scale_seed_member(uuid,uuid,uuid) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.guard_scale_seed_cleanup(text) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.delete_scale_seed_projects(text,uuid[]) OWNER TO :"workspace_owner";

GRANT CREATE ON SCHEMA yoao_inference_backend_probe_v1 TO :"inference_owner";
ALTER SCHEMA yoao_inference_backend_probe_v1 OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_backend_probe_v1.probe_secret OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_backend_probe_v1.backend_probe_policy()
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_backend_probe_v1.open_backend_probe_subject(text, uuid)
    OWNER TO :"inference_owner";
REVOKE CREATE ON SCHEMA yoao_inference_backend_probe_v1 FROM :"inference_owner";

GRANT CREATE ON SCHEMA yoao_inference_health_observation_v1 TO :"inference_owner";
ALTER SCHEMA yoao_inference_health_observation_v1 OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_health_observation_v1.observation_secret OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_health_observation_v1.health_observation_policy() OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_health_observation_v1.capture_health_observation_subject(uuid,uuid,text,text,double precision) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_health_observation_v1.open_health_observation_subject(text,uuid,uuid) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_health_observation_v1.seal_health_observation(text,uuid,uuid,text) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_health_observation_v1.verify_health_observation(text,uuid,uuid,text) OWNER TO :"inference_owner";
REVOKE CREATE ON SCHEMA yoao_inference_health_observation_v1 FROM :"inference_owner";

GRANT CREATE ON SCHEMA yoao_inference_health_data_v1 TO :"inference_owner";
ALTER SCHEMA yoao_inference_health_data_v1 OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_health_data_v1.health_data_policy() OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_health_data_v1.backend_row(uuid,boolean) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_health_data_v1.membership_row(uuid,text,boolean) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_health_data_v1.observation_clock() OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_health_data_v1.lock_resource(text) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_health_data_v1.try_promotion() OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_health_data_v1.write_backend(uuid,text,timestamp with time zone,text,boolean,boolean) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_health_data_v1.member_pool(uuid) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_health_data_v1.pool_row(uuid,uuid) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_health_data_v1.member_row(uuid) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_health_data_v1.apply_reconciliation(uuid,uuid,text,text,text,bigint,text,text) OWNER TO :"inference_owner";
REVOKE CREATE ON SCHEMA yoao_inference_health_data_v1 FROM :"inference_owner";

GRANT CREATE ON SCHEMA yoao_inference_registry_ingress_v1 TO :"inference_owner";
ALTER SCHEMA yoao_inference_registry_ingress_v1 OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_registry_ingress_v1.env_sync_receipts
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_registry_ingress_v1.registry_ingress_policy()
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_registry_ingress_v1.registry_by_url(text)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_registry_ingress_v1.registry_candidates(text)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_registry_ingress_v1.create_registry(
    uuid, text, text, text, boolean, text
) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_registry_ingress_v1.create_registry_plugin_legacy(
    uuid, text, text, text, boolean, text
) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_registry_ingress_v1.singleton_pool(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_registry_ingress_v1.create_singleton_pool(
    uuid, uuid, uuid, text, text, text
) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_registry_ingress_v1.apply_observation(
    uuid, text, text, text, boolean, text, timestamptz, text, boolean, text
) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_registry_ingress_v1.apply_observation_plugin_legacy(
    uuid, text, text, text, boolean, text, timestamptz, text, boolean, text
) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_registry_ingress_v1.disconnect_env_registry(
    uuid, text
) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_registry_ingress_v1.disconnect_env_registry_plugin_legacy(
    uuid, text
) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_registry_ingress_v1.tag_seed_registry(uuid, text)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_registry_ingress_v1.tag_seed_registry_plugin_legacy(
    uuid, text
) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_registry_ingress_v1.sync_result(uuid, text)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_registry_ingress_v1.record_sync_result(
    uuid, text, integer
) OWNER TO :"inference_owner";
REVOKE CREATE ON SCHEMA yoao_inference_registry_ingress_v1 FROM :"inference_owner";

GRANT CREATE ON SCHEMA yoao_inference_routing_v1 TO :"inference_owner";
ALTER SCHEMA yoao_inference_routing_v1 OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_routing_v1.authority_secret OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_routing_v1.registry_state OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_routing_v1.scopes OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_routing_v1.attempts OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_routing_v1.weights OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_routing_v1.controls OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_routing_v1.control_attempts OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_routing_v1.command_receipts OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_routing_v1.plugin_terminal_update_guard OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_routing_v1.plugin_replacement_bindings OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_routing_v1.scope_retirements OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_routing_v1.video_execution_scope_permits
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1.routing_policy()
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1.routing_step(text, text)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_step_0279(text, text)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1.scope_retirement_step(text, text)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1.supersede_unbegun_scope(text)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1.model_dispatch_step(text)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1.guard_model_attempt()
    OWNER TO :"inference_owner";
GRANT USAGE ON SCHEMA yoao_inference_topology_v1 TO :"inference_owner";
GRANT EXECUTE ON FUNCTION yoao_inference_topology_v1.plugin_asset_approval(uuid)
    TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1.capture_scope_boot()
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._scope_retirement_step_0280(text, text)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1.video_execution_scope_step(text, text)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._video_execution_canonical_json(jsonb)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._video_execution_scope_uuid(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._video_execution_result(jsonb,text)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._video_execution_no_scope_rejection(uuid,uuid,text)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._video_execution_permit_scope()
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._video_execution_attempt_gate()
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._video_execution_heartbeat_gate()
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._scope_retirement_result(text, jsonb)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._scope_retirement_rejected(text)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._scope_retirement_view(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._scope_retirement_context(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_scope_retirement_guard_state()
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_scope_retirement_guard_control()
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_exact_keys(jsonb, text[])
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_uuid(text)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_integer(jsonb, bigint, bigint)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_reply(text, jsonb)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_rejected(text)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_receipt_safe(jsonb)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_realm()
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_secret()
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_base64url(bytea)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_unbase64url(text)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_sign_snapshot(jsonb)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_open_snapshot(text)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_scope_capability(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_control_capability(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_scope_view(uuid, boolean)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_control_view(uuid, boolean)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_transport_identity(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_max_concurrency(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_sign_admission(
    uuid, uuid, uuid, text, text, text
) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_lock_admission_pool(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_has_unresolved(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_has_control(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_registry_after_insert()
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_registry_before_change()
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_pool_before_change()
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_pool_after_generation()
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_member_before_change()
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._routing_project_pool_before_change()
    OWNER TO :"inference_owner";
REVOKE CREATE ON SCHEMA yoao_inference_routing_v1 FROM :"inference_owner";
ALTER FUNCTION yoao_workspace_v1.lock_group_assignment_scope(uuid, uuid[], boolean)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.invitation_projects(uuid[])
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.lock_invitation_project(uuid, uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.read_invitation_membership(uuid, uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.grant_invitation_membership(uuid, uuid, text, uuid, uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.identity_directory_owned_member_ids(uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.identity_directory_project_member_ids(uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.identity_directory_lifecycle_blocked(uuid, uuid[])
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.admin_password_target_in_owned_project(uuid, uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.identity_platform_role_facts(uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.lock_identity_platform_role_owned_projects(uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.lock_identity_suspension_scope(uuid, uuid, text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.lock_identity_deletion_scope(uuid, uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.identity_deletion_transfer_covered(uuid, uuid[])
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.lock_identity_offboarding_scope(uuid, uuid[], uuid[])
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.identity_offboarding_replay_scope(uuid, uuid[])
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.identity_offboarding_facts(uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.transfer_identity_offboarding_owners(uuid, jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.identity_brief_project_roles(uuid, uuid[])
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1._project_delete_failure(text) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.delete_project(uuid, uuid, uuid, jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1._dataset_unlink_failure(text, text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.preview_dataset_unlink(uuid, uuid, uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.unlink_dataset(uuid, uuid, uuid, uuid, text, jsonb)
    OWNER TO :"workspace_owner";
ALTER VIEW yoao_workspace_v1.project_catalog_configs OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA yoao_workspace_v1 FROM :"workspace_owner";
ALTER FUNCTION yoao_workspace_membership_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_membership_v1._failure(integer, jsonb, jsonb, boolean)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_membership_v1._request_is_valid(jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_membership_v1._membership_record(uuid, uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_membership_v1.membership_policy()
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_membership_v1.prepare_membership_command(jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_membership_v1.apply_membership_command(jsonb)
    OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA yoao_workspace_membership_v1 FROM :"workspace_owner";
ALTER FUNCTION yoao_workspace_owner_transfer_v1._failure(integer, jsonb, boolean)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_owner_transfer_v1.owner_transfer_policy()
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_owner_transfer_v1.transfer_project_owner(jsonb)
    OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA yoao_workspace_owner_transfer_v1 FROM :"workspace_owner";
GRANT CREATE ON SCHEMA yoao_workspace_configuration_v1 TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1.configuration_policy()
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1.configuration_step(jsonb, text, jsonb, text[], text[])
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1._step_success(jsonb, text, jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1._step_failure(jsonb, text, integer, jsonb, jsonb, boolean)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1._configuration_actor(uuid, boolean, text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1._class_rename_project_row(uuid, text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1._class_rename_member_rows(uuid, uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1._guide_asset_read_project_row(uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1._guide_asset_read_member_rows(uuid, uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1._configuration_project_snapshot(uuid, text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1._configuration_project_access(jsonb, jsonb, boolean)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1._configuration_template_snapshot(uuid, boolean)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1._configuration_template_visible(jsonb, jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1._guard_unfinished_attempt()
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1._cleanup_unwritten_state()
    OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA yoao_workspace_configuration_v1 FROM :"workspace_owner";

GRANT CREATE ON SCHEMA yoao_workspace_template_query_v1 TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_template_query_v1.template_query_policy()
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_template_query_v1.template_query_step(jsonb, text, jsonb, text, text[], text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_template_query_v1._template_row_json(public.project_templates)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_template_query_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA yoao_workspace_template_query_v1 FROM :"workspace_owner";

GRANT CREATE ON SCHEMA yoao_workspace_project_query_v1 TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_project_query_v1.project_query_policy()
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_project_query_v1.project_query_step(
    jsonb, text, jsonb, text, text, text[], text[], uuid, text,
    timestamptz, timestamptz, bigint, integer
) OWNER TO :"workspace_owner";
DO $$
DECLARE
    implementation regprocedure := pg_catalog.to_regprocedure(
        'yoao_workspace_project_query_v1._project_query_step_impl(jsonb, text, jsonb, text, text, text[], text[], uuid, text, timestamptz, timestamptz, bigint, integer)'
    );
BEGIN
    IF implementation IS NOT NULL THEN
        EXECUTE pg_catalog.format(
            'ALTER FUNCTION %s OWNER TO %I',
            implementation,
            current_setting('yoao.workspace_owner')
        );
    END IF;
END $$;
ALTER FUNCTION yoao_workspace_project_query_v1.project_search_scope(uuid, text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_project_query_v1.project_search_member_ids(uuid, text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_project_query_v1._project_row_json(
    jsonb, integer, integer, integer, integer, text
) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_project_query_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA yoao_workspace_project_query_v1 FROM :"workspace_owner";

GRANT CREATE ON SCHEMA yoao_workspace_directory_query_v1 TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_directory_query_v1.directory_query_policy()
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_directory_query_v1.directory_query_step(jsonb, text, jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_directory_query_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA yoao_workspace_directory_query_v1 FROM :"workspace_owner";
ALTER FUNCTION yoao_identity_v1.workspace_directory_superadmins()
    OWNER TO :"identity_owner";

ALTER FUNCTION yoao_identity_v1.read_workspace_membership_accounts(uuid[])
    OWNER TO :"identity_owner";
ALTER FUNCTION yoao_audit_v1.record_workspace_membership_event(
    text, jsonb, uuid, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_labeling_v1.read_membership_work(uuid, uuid, uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_membership_work(uuid, uuid, text, boolean)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.handoff_membership_annotation(uuid, uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.handoff_membership_reviewer(uuid, uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.release_membership_task_locks(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._membership_python_repr(jsonb)
    OWNER TO :"labeling_owner";

ALTER FUNCTION yoao_labeling_v1.invitation_work_summary(uuid, uuid, text)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.identity_directory_project_refs(uuid[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.identity_platform_role_facts(uuid, uuid[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.identity_deletion_work(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.transfer_identity_deletion_work(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_identity_offboarding_work(uuid, uuid[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.identity_offboarding_facts(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.transfer_identity_offboarding_work(uuid, jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.release_identity_offboarding_task_locks(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_audit_v1.record_identity_admin_password_reset(
    jsonb, uuid, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.record_identity_platform_role_change(
    jsonb, uuid, jsonb, text, text, text
) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.record_identity_suspension_event(
    text, jsonb, uuid, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.record_identity_bootstrap_admin(uuid, text, text)
    OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.record_identity_deletion_event(
    jsonb, uuid, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_settings_v1.invitation_policy(jsonb)
    OWNER TO :"settings_owner";
ALTER FUNCTION yoao_audit_v1.record_identity_invitation_event(
    text, jsonb, jsonb, jsonb, jsonb
) OWNER TO :"audit_owner";

GRANT CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._managed_reclaim_json_string(text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._managed_reclaim_lock_identity(text,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.is_fixture_original_source(text,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.classify_fixture_original_reclaim(uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_scale_fixture_originals(uuid,uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_auxiliary_intent() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_tracker_prepare_original(uuid,uuid,uuid,uuid,text,text,text)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_tracker_control_original(uuid,uuid,uuid,uuid,text,text,text,text)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_tracker_control_original(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._acceptance_history_source(uuid) OWNER TO :"assets_owner";
REVOKE ALL ON FUNCTION yoao_assets_v1._acceptance_history_source(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION yoao_assets_v1._acceptance_history_source(uuid) TO :"labeling_owner";
ALTER FUNCTION yoao_assets_v1._legacy_upload_reply(jsonb, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_legacy_dataset_upload(uuid, uuid, uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.complete_legacy_dataset_upload(
    uuid, uuid, uuid, uuid, text, bigint, text, integer, integer
) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._association_read_failure(text)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.list_dataset_projects(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.list_project_datasets(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.workspace_configuration_datasets(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.workspace_configuration_dataset_rows(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.workspace_configuration_dataset_scenes(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.workspace_configuration_existing_item_ids(uuid[])
    OWNER TO :"assets_owner";
ALTER VIEW yoao_assets_v1.video_chapter_facts OWNER TO :"assets_owner";
ALTER VIEW yoao_assets_v1.labeling_dataset_item_facts OWNER TO :"assets_owner";
ALTER VIEW yoao_assets_v1.fixture_screenshot_item_facts OWNER TO :"assets_owner";
GRANT SELECT ON yoao_assets_v1.fixture_screenshot_item_facts TO :"labeling_owner";
ALTER VIEW yoao_assets_v1.scale_seed_dataset_item_facts OWNER TO :"assets_owner";
ALTER VIEW yoao_assets_v1.project_dataset_item_sources OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_dataset_source_authority(uuid, boolean)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_dataset_reference(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_current_association(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_append_sources(uuid, uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.associate_project(uuid, uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_task_creation_items(uuid, uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_scene_source_references(uuid[])
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.delete_project_owned_records(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_item_references(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_item_scene_references(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_dataset_source_auxiliary_retention(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_scale_seed_dataset_cleanup(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_scene_references(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.remove_dataset_association(uuid, uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.prepare_dataset_unlink_cleanup(uuid, uuid, uuid, text)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.pending_storage_deletions(uuid, integer)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.complete_storage_deletion(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_upload_failure(text)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_upload_success(jsonb)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_upload_actor(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_upload_facts(public.dataset_upload_intents)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._accept_dataset_upload(
    uuid, uuid, uuid, uuid, bigint, text, integer, integer
) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._finish_dataset_upload(uuid, uuid, uuid, uuid, bigint)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.reserve_dataset_upload(uuid, uuid, uuid, text, text)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_dataset_upload(uuid, uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.find_dataset_upload_duplicate(uuid, uuid, uuid, text)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.complete_dataset_upload(
    uuid, uuid, uuid, uuid, bigint, text, integer, integer
) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.discard_dataset_upload(uuid, uuid, uuid, text)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._source_delete_failure(text)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_association_references(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._lock_dataset_source_deletion(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._queue_pyramid_deletions(uuid, uuid[], text)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._delete_dataset_source_records(uuid, uuid, uuid, text)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._delete_dataset_source(
    uuid, uuid, uuid, uuid, text, jsonb
) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.delete_dataset(uuid, uuid, uuid, text, jsonb)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.delete_dataset_item(
    uuid, uuid, uuid, uuid, text, jsonb
) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.pending_storage_deletions_v2(
    text, timestamptz, uuid, uuid, integer
) OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.image_pyramid_native_attempts OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.image_pyramid_native_members OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.native_pyramid_retirement_targets OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.native_pyramid_retirements OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.native_pyramid_retirement_pages OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.claim_native_pyramid_retirement(uuid, integer)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.prepare_native_pyramid_retirement_page(
    uuid, uuid, integer
) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.complete_native_pyramid_retirement_page(
    uuid, uuid, uuid, text, jsonb
) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._native_pyramid_retirement_policy_valid(jsonb)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._native_pyramid_retirement_grace(jsonb)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._native_pyramid_retirement_target_valid(jsonb, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._native_pyramid_retirement_page_items(uuid, uuid[])
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._native_pyramid_retirement_claimable(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_native_pyramid_retirement()
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_native_pyramid_retirement_target()
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_native_pyramid_retirement_page()
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.preserve_native_pyramid_retirement_grace()
    OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.native_pyramid_read_permits OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.allocate_native_pyramid_output(
    uuid, bigint, uuid, uuid, integer
) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._native_pyramid_output_fixture_owned(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_pyramid_gc_eligible(text, uuid, integer)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._image_saved_output_target(uuid, integer, uuid, boolean)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_native_pyramid_read_permit()
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_native_pyramid_read(
    uuid, uuid, uuid, integer, text, uuid, uuid
) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.complete_native_pyramid_read(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._native_pyramid_target_destination(
    uuid, text, text, text
) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.capture_native_pyramid_write_io(
    uuid, uuid, bigint, uuid, text
) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.capture_native_pyramid_read_io(uuid, uuid, jsonb)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.capture_native_pyramid_retirement_io(uuid, uuid, uuid)
    OWNER TO :"assets_owner";
DO $$
DECLARE
    target record;
    owner_name text := current_setting('yoao.assets_owner');
BEGIN
    FOR target IN
        SELECT proc.oid::regprocedure AS signature
        FROM pg_catalog.pg_proc AS proc
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = proc.pronamespace
        WHERE namespace.nspname = 'yoao_assets_v1'
          AND proc.prokind IN ('f', 'p')
          AND (
              proc.proname LIKE '%image%'
              OR proc.proname LIKE '%native_pyramid%'
          )
    LOOP
        EXECUTE pg_catalog.format(
            'ALTER FUNCTION %s OWNER TO %I', target.signature, owner_name
        );
    END LOOP;
END $$;
ALTER FUNCTION yoao_assets_v1.claim_accepted_input_garbage(uuid, integer, integer)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.complete_accepted_input_garbage(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._accepted_input_garbage_immutable_scope()
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.workspace_project_query_dataset_ids(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.workspace_project_query_dataset_item_count(uuid[])
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.workspace_project_query_dataset_item_ids(uuid[])
    OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";

GRANT CREATE ON SCHEMA yoao_labeling_class_query_v1 TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_class_query_v1.class_query_policy() OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_class_query_v1.class_counts(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_class_query_v1.attribute_rows(uuid) OWNER TO :"labeling_owner";
REVOKE CREATE ON SCHEMA yoao_labeling_class_query_v1 FROM :"labeling_owner";

GRANT CREATE ON SCHEMA yoao_labeling_v1 TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_task_original_pointer(uuid,text) OWNER TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.lock_task_original_pointer(uuid,text)
    TO :"assets_owner";
ALTER FUNCTION yoao_labeling_v1.append_dataset_items(uuid, uuid[], uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.accept_dataset_task_creation(
    uuid, uuid, uuid, uuid, uuid, uuid, text, integer
) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.prepare_dataset_task_creation(uuid, jsonb) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.read_dataset_task_creation(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.advance_dataset_task_creation(uuid, integer) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.project_task_references(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.delete_project_owned_records(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._dataset_unlink_task_snapshot(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.dataset_task_references(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_dataset_unlink_scope(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.guard_dataset_unlink(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.preview_dataset_unlink(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.delete_dataset_tasks(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.guard_dataset_source_deletion(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.detach_deleted_dataset_attribution(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.project_dataset_task_counts(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.workspace_configuration_video_facts(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.rename_project_annotation_class(uuid, text, text)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.workspace_configuration_task_count(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.workspace_configuration_annotation_count(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.workspace_configuration_tracker_count(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.workspace_configuration_video_items(uuid)
    OWNER TO :"labeling_owner";
ALTER VIEW yoao_labeling_v1.project_catalog_counters OWNER TO :"labeling_owner";
DO $$
DECLARE
    target record;
    owner_name text := current_setting('yoao.labeling_owner');
BEGIN
    FOR target IN
        SELECT proc.oid::regprocedure AS signature
        FROM pg_catalog.pg_proc AS proc
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = proc.pronamespace
        WHERE namespace.nspname = 'yoao_labeling_v1'
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
    owner_name text := current_setting('yoao.labeling_owner');
BEGIN
    FOR target IN
        SELECT proc.oid::regprocedure AS signature
        FROM pg_catalog.pg_proc AS proc
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = proc.pronamespace
        WHERE namespace.nspname = 'yoao_labeling_v1'
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
ALTER FUNCTION yoao_labeling_v1.attach_annotation_input_source(uuid, uuid, text)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._annotation_input_sources_reply(jsonb, text)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.copy_annotation_input_sources(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._annotation_input_source_immutable()
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._inherit_annotation_input_sources()
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._propagate_annotation_input_source()
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._video_input_task_source_reply(jsonb, text)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._video_input_task_binding_valid(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.read_video_input_task_source(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_video_input_task_source(uuid, uuid, jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._video_input_selection_reply(jsonb, text)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._video_input_manifest_selection_valid(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._video_input_captured_task_valid(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._video_input_selection_task_access(uuid, uuid, uuid, uuid, text)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.open_video_input_manifest_members(uuid, uuid, jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_video_input_manifest_member(uuid, uuid, uuid, jsonb, jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.workspace_project_query_counters(uuid[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.workspace_project_query_ai_counts(uuid[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.workspace_project_query_annotation_count(uuid[], boolean)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.workspace_project_query_task_series(uuid[], timestamptz[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.workspace_project_query_annotation_series(uuid[], timestamptz[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.workspace_project_query_linked_task_count(uuid, uuid[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.workspace_project_query_task_count(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.workspace_project_query_batches(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.workspace_project_query_batch_task_counts(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.task_edit_lock_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.task_workflow_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.annotation_statistics_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.scene_track_domain_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.annotation_write_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.annotation_bulk_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.video_bbox_editing_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.batch_control_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.prediction_record_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.prediction_partition_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.scene_track_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.cross_frame_write_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.annotation_prediction_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.annotation_maintenance_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.discussion_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.task_event_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.mask_edit_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._acceptance_history(jsonb)
    OWNER TO :"labeling_owner";
REVOKE ALL ON FUNCTION yoao_labeling_v1._acceptance_history(jsonb) FROM PUBLIC;
ALTER FUNCTION yoao_labeling_v1.mask_revision_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._mask_revision_canonical_json(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.ai_mask_accept_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.scene_read_query(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._scene_read_track_json(uuid,uuid,uuid,text,text,text,jsonb,jsonb,integer,timestamptz)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._scene_read_interval_json(uuid,uuid,integer,integer,text,integer)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._scene_read_annotation_json(uuid,uuid,uuid,text,text,text,jsonb,text,uuid,text,text,boolean,boolean,boolean,boolean,integer,timestamptz,integer)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.video_control_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.video_segment_lifecycle_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.screenshot_operations_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._fixture_screenshot_project(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._fixture_screenshot_path(uuid,uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._fixture_screenshot_task(uuid,uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._fixture_screenshot_user(uuid,text,text)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.annotation_transfer_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._annotation_transfer_exact_fields(jsonb,text[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._annotation_transfer_without_track_ids(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._annotation_transfer_without_suffix(text)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._annotation_transfer_camera_bbox_valid(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_task_for_core_read(uuid,text,boolean,uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_task_dataset_item_links_for_core_read(uuid,boolean,uuid,integer)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_active_learning_feedback_task_rows(uuid,uuid[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_active_learning_feedback_annotation_rows(uuid[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.next_batch_display_id()
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_batch_rows_for_core_read(uuid[],uuid,text,boolean)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_annotation_rows_for_core_read(uuid[],uuid,uuid,text,boolean)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.mask_qc_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.mask_repair_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.raster_mask_retention_query(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._quality_exact_keys(jsonb,text[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._raster_mask_reference_keys()
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.spatial_quality_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.task_maintenance_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.purge_scale_seed_labeling_rows(text,uuid[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.create_scale_seed_batches(text,uuid,uuid,integer,uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.create_scale_seed_tasks(text,uuid,uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.create_scale_seed_annotations(text,uuid,uuid,integer)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.e2e_fixture_command(jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.prepare_filtering_rendering(jsonb)
    OWNER TO :"labeling_owner";
ALTER VIEW yoao_labeling_v1.task_rows OWNER TO :"labeling_owner";
ALTER VIEW yoao_labeling_v1.batch_rows OWNER TO :"labeling_owner";
ALTER VIEW yoao_labeling_v1.annotation_rows OWNER TO :"labeling_owner";
ALTER VIEW yoao_labeling_v1.prediction_rows OWNER TO :"labeling_owner";
ALTER VIEW yoao_labeling_v1.prediction_meta_rows OWNER TO :"labeling_owner";
ALTER VIEW yoao_labeling_v1.failed_prediction_rows OWNER TO :"labeling_owner";
ALTER VIEW yoao_labeling_v1.task_lock_rows OWNER TO :"labeling_owner";
ALTER VIEW yoao_labeling_v1.task_dataset_item_link_rows OWNER TO :"labeling_owner";
ALTER VIEW yoao_labeling_v1.annotation_draft_rows OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_video_segment_for_core_read(uuid,uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_task_rows_for_core_read(uuid[],uuid,uuid) OWNER TO :"labeling_owner";
ALTER VIEW yoao_labeling_v1.scene_track_rows OWNER TO :"labeling_owner";
ALTER VIEW yoao_labeling_v1.scene_track_interval_rows OWNER TO :"labeling_owner";
ALTER VIEW yoao_labeling_v1.video_segment_rows OWNER TO :"labeling_owner";
ALTER VIEW yoao_labeling_v1.video_tracker_job_rows OWNER TO :"labeling_owner";
ALTER VIEW yoao_labeling_v1.video_track_quality_run_rows OWNER TO :"labeling_owner";
ALTER VIEW yoao_labeling_v1.annotation_comment_facts OWNER TO :"labeling_owner";
ALTER VIEW yoao_labeling_v1.annotation_feedback_facts OWNER TO :"labeling_owner";
ALTER VIEW yoao_labeling_v1.task_event_facts OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.retryable_deployment_usage(uuid,uuid,uuid) OWNER TO :"labeling_owner";
REVOKE CREATE ON SCHEMA yoao_labeling_v1 FROM :"labeling_owner";

GRANT CREATE ON SCHEMA yoao_jobs_v1 TO :"jobs_owner";
ALTER VIEW yoao_jobs_v1.notification_scope_facts OWNER TO :"jobs_owner";
ALTER SCHEMA yoao_jobs_v1 OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.count_running_registry_predictions(uuid)
    OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._accepted_image_input_job_reply(jsonb, text)
    OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_input_job_reply(jsonb, text)
    OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_input_selection_valid(jsonb)
    OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.project_video_input_job(uuid, uuid, uuid, uuid, jsonb, text, boolean)
    OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.project_accepted_image_input_job(uuid, uuid, uuid, boolean)
    OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.accepted_image_input_job_consistency(uuid, uuid, uuid, uuid)
    OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._accepted_image_input_manifest_reply(jsonb, text)
    OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.project_accepted_image_input_manifest_job(
    uuid, uuid, uuid, uuid, jsonb, text, boolean
) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.accepted_image_input_manifest_job_consistency(
    uuid, uuid, uuid, uuid, jsonb, text
) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._task_creation_job_failure(text, text) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._task_creation_job_payload_valid(uuid, jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.create_task_creation_job(uuid, uuid, uuid, uuid, uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.find_task_creation_job(uuid, uuid, uuid, uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.claim_task_creation_job(uuid, text) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.lock_task_creation_job(uuid, uuid, uuid, uuid, uuid, uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.update_task_creation_progress(uuid, uuid, bigint, bigint, bigint) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.finish_task_creation_job(uuid, uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.fail_task_creation_job(uuid, uuid, text) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.cancel_task_creation_job(uuid, uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.detach_project_references(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.retain_async_job_project_scope() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.cancel_dataset_task_creation(uuid, uuid, uuid)
    OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_dataset_source_deletion(uuid)
    OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.task_creation_execution_active(uuid, uuid)
    OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.workspace_project_query_job_status_counts(uuid)
    OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.workspace_project_query_latest_job_status(uuid)
    OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.video_execution_authority OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.video_executions OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.video_execution_route_intents OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.video_execution_step(text,text) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_execution_settled(uuid,uuid,text,text,uuid)
    OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_execution_route_step(text,text)
    OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_execution_scope_is_current(uuid,boolean)
    OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_execution_claim_view(uuid,uuid)
    OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_execution_secret() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_execution_realm() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_execution_exact_keys(jsonb,text[])
    OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_execution_uuid(jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_execution_reply(jsonb,text) OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA yoao_jobs_v1 FROM :"jobs_owner";

GRANT CREATE ON SCHEMA yoao_learning_v1 TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.registry_evaluation_reference_exists(uuid)
    OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.lock_project_deletion(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.guard_project_deletion(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.delete_project_owned_records(uuid)
    OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.guard_dataset_item_deletion(uuid, uuid)
    OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.guard_dataset_unlink(uuid, uuid)
    OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.guard_dataset_source_deletion(uuid, uuid)
    OWNER TO :"learning_owner";
REVOKE CREATE ON SCHEMA yoao_learning_v1 FROM :"learning_owner";

GRANT CREATE ON SCHEMA yoao_inference_v1 TO :"inference_owner";
ALTER FUNCTION yoao_inference_v1.ai_mask_pool_reference_row(uuid) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_v1.clear_project_ml_backend_pool(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_v1.clear_project_pipelines(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_v1.enable_workspace_project_backend(uuid, uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_v1.workspace_configuration_source_pool(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_v1.workspace_configuration_backend(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_v1.workspace_project_query_pool(uuid)
    OWNER TO :"inference_owner";
REVOKE CREATE ON SCHEMA yoao_inference_v1 FROM :"inference_owner";

GRANT CREATE ON SCHEMA yoao_inference_pipelines_v1 TO :"inference_owner";
ALTER SCHEMA yoao_inference_pipelines_v1 OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_pipelines_v1.operation_states OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_pipelines_v1.operation_attempts OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_pipelines_v1.operation_receipts OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_pipelines_v1._has_exact_keys(jsonb, text[])
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_pipelines_v1._actor_is_valid(jsonb)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_pipelines_v1._command_is_valid(jsonb)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_pipelines_v1._query_is_valid(jsonb)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_pipelines_v1._pipeline_row_value(public.project_pipelines)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_pipelines_v1._step_success(uuid, text, text, uuid, uuid, jsonb)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_pipelines_v1._step_failure(uuid, text, text, uuid, uuid, integer, jsonb, jsonb, boolean)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_pipelines_v1._guard_unfinished_operation()
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_pipelines_v1._cleanup_unfinished_state()
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_pipelines_v1.pipeline_policy()
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_pipelines_v1.pipeline_step(jsonb, text, jsonb, text[], text[])
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_pipelines_v1.frozen_snapshot_uses_deployment(uuid,uuid)
    OWNER TO :"inference_owner";
REVOKE CREATE ON SCHEMA yoao_inference_pipelines_v1 FROM :"inference_owner";

GRANT TEMPORARY ON DATABASE :"expected_database" TO :"insights_owner";
GRANT CREATE ON SCHEMA public, yoao_insights_v1 TO :"insights_owner";
ALTER MATERIALIZED VIEW public.mv_user_perf_daily OWNER TO :"insights_owner";
ALTER TABLE public.project_task_views OWNER TO :"insights_owner";
ALTER TABLE public.dataset_saved_views OWNER TO :"insights_owner";
ALTER FUNCTION yoao_insights_v1.clear_project_task_views(uuid)
    OWNER TO :"insights_owner";
ALTER FUNCTION yoao_insights_v1.refresh_user_perf_daily() OWNER TO :"insights_owner";
ALTER FUNCTION yoao_insights_v1._task_view_project_scope(uuid,uuid,boolean) OWNER TO :"insights_owner";
ALTER FUNCTION yoao_insights_v1._task_view_snapshot(public.project_task_views) OWNER TO :"insights_owner";
ALTER FUNCTION yoao_insights_v1._lock_writable_task_view(uuid,uuid,uuid,boolean) OWNER TO :"insights_owner";
ALTER FUNCTION yoao_insights_v1.list_saved_task_views(uuid,uuid,text) OWNER TO :"insights_owner";
ALTER FUNCTION yoao_insights_v1.read_saved_task_view(uuid,uuid,uuid) OWNER TO :"insights_owner";
ALTER FUNCTION yoao_insights_v1.create_saved_task_view(uuid,uuid,text,text,text,jsonb,jsonb,jsonb) OWNER TO :"insights_owner";
ALTER FUNCTION yoao_insights_v1.update_saved_task_view(uuid,uuid,uuid,text,text,jsonb,jsonb,jsonb) OWNER TO :"insights_owner";
ALTER FUNCTION yoao_insights_v1.delete_saved_task_view(uuid,uuid,uuid) OWNER TO :"insights_owner";
ALTER FUNCTION yoao_insights_v1._dataset_view_snapshot(public.dataset_saved_views) OWNER TO :"insights_owner";
ALTER FUNCTION yoao_insights_v1.list_personal_dataset_views(uuid,uuid) OWNER TO :"insights_owner";
ALTER FUNCTION yoao_insights_v1.has_personal_dataset_view(uuid,uuid,uuid) OWNER TO :"insights_owner";
ALTER FUNCTION yoao_insights_v1.prepare_personal_dataset_view(uuid,uuid) OWNER TO :"insights_owner";
ALTER FUNCTION yoao_insights_v1.create_personal_dataset_view(uuid,uuid,text,jsonb) OWNER TO :"insights_owner";
ALTER FUNCTION yoao_insights_v1.update_personal_dataset_view(uuid,uuid,uuid,text,jsonb) OWNER TO :"insights_owner";
ALTER FUNCTION yoao_insights_v1.delete_personal_dataset_view(uuid,uuid,uuid) OWNER TO :"insights_owner";
GRANT EXECUTE ON FUNCTION yoao_insights_v1.refresh_user_perf_daily() TO :"migration_role";
REVOKE CREATE ON SCHEMA public, yoao_insights_v1 FROM :"insights_owner";

GRANT CREATE ON SCHEMA yoao_diagnostics_v1 TO :"diagnostics_owner";
ALTER FUNCTION yoao_diagnostics_v1.detach_project_references(uuid)
    OWNER TO :"diagnostics_owner";
ALTER FUNCTION yoao_diagnostics_v1.detach_dataset_task_references(uuid, uuid)
    OWNER TO :"diagnostics_owner";
REVOKE CREATE ON SCHEMA yoao_diagnostics_v1 FROM :"diagnostics_owner";

GRANT CREATE ON SCHEMA yoao_export_v1 TO :"export_owner";
ALTER TABLE yoao_export_v1.executions OWNER TO :"export_owner";
ALTER TABLE yoao_export_v1.execution_deliveries OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.publications_retired(uuid) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.capture_execution_birth() OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.capture_execution_delivery() OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.session_holds_lock(text,uuid,integer) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.begin_execution(uuid,uuid,text,uuid,integer) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.enter_execution_io(uuid,uuid) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.quiesce_execution_io(uuid,uuid) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.finish_execution(uuid,uuid) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.retire_execution(uuid,uuid) OWNER TO :"export_owner";
ALTER FUNCTION yoao_export_v1.clear_project_export_artifacts(uuid)
    OWNER TO :"export_owner";
REVOKE CREATE ON SCHEMA yoao_export_v1 FROM :"export_owner";

GRANT CREATE ON SCHEMA public TO :"audit_owner";
GRANT EXECUTE ON FUNCTION public.deny_audit_log_mutation() TO :"audit_owner";
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
DO $audit_storage_owner$
DECLARE
    v_partition record;
    v_sequence text;
    v_sequence_owner oid;
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.audit_owner'));
BEGIN
    v_sequence := pg_catalog.pg_get_serial_sequence('public.audit_logs', 'id');
    IF v_sequence IS NULL OR v_owner IS NULL THEN
        RAISE EXCEPTION 'Audit log sequence or code owner is missing';
    END IF;
    FOR v_partition IN
        SELECT relid FROM pg_catalog.pg_partition_tree('public.audit_logs')
    LOOP
        EXECUTE pg_catalog.format('ALTER TABLE %s OWNER TO %I',
            v_partition.relid::pg_catalog.regclass,
            current_setting('yoao.audit_owner'));
    END LOOP;
    SELECT relation.relowner INTO v_sequence_owner
    FROM pg_catalog.pg_class AS relation
    WHERE relation.oid=v_sequence::pg_catalog.regclass;
    IF v_sequence_owner IS DISTINCT FROM v_owner THEN
        EXECUTE pg_catalog.format('ALTER SEQUENCE %s OWNER TO %I',
            v_sequence, current_setting('yoao.audit_owner'));
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_partition_tree('public.audit_logs') AS partition
        JOIN pg_catalog.pg_class AS relation ON relation.oid=partition.relid
        WHERE relation.relowner<>v_owner
    ) OR (
        SELECT relation.relowner FROM pg_catalog.pg_class AS relation
        WHERE relation.oid=v_sequence::pg_catalog.regclass
    ) IS DISTINCT FROM v_owner THEN
        RAISE EXCEPTION 'Audit partitions or their sequence have another owner';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_default_acl AS defaults
        CROSS JOIN LATERAL pg_catalog.aclexplode(defaults.defaclacl) AS acl
        WHERE defaults.defaclrole=v_owner
          AND defaults.defaclnamespace IN (0, 'public'::pg_catalog.regnamespace)
          AND defaults.defaclobjtype IN ('r','S')
          AND acl.grantee<>v_owner
    ) THEN
        RAISE EXCEPTION 'Audit owner default privileges expose future private storage';
    END IF;
END;
$audit_storage_owner$;
ALTER MATERIALIZED VIEW public.mv_audit_bi_daily OWNER TO :"audit_owner";
GRANT TEMPORARY ON DATABASE :"expected_database" TO :"audit_owner";
GRANT CREATE ON SCHEMA yoao_audit_v1 TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.record_identity_session_event(
    text, jsonb, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.record_identity_account_event(
    text, jsonb, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.record_identity_recovery_event(
    text, jsonb, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.record_identity_group_event(
    text, jsonb, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.record_identity_deactivation_event(
    text, jsonb, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.record_identity_offboarding_event(
    jsonb, uuid, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.record_identity_directory_export(
    jsonb, jsonb, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.record_project_deletion(
    uuid, uuid, uuid, text, text, bigint, bigint, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.valid_request_context(jsonb)
    OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.record_dataset_unlink(
    uuid, uuid, uuid, uuid, bigint, bigint, uuid[], jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.record_dataset_source_deletion(
    uuid, uuid, uuid, uuid, bigint, bigint, bigint, bigint, bigint, bigint, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.append_events(jsonb) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.query_events(pg_catalog.jsonb) OWNER TO :"audit_owner";
ALTER VIEW yoao_audit_v1.audit_events OWNER TO :"audit_owner";
ALTER VIEW yoao_audit_v1.audit_daily_facts OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.refresh_audit_bi_mv() OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.maintenance_command(jsonb) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1._assert_platform_partition(date,bigint,bigint) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.platform_maintenance(text,date,bigint,bigint,text,uuid,timestamptz)
    OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.archive_output_has_ready(integer,integer) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.archive_output_ready_month(integer,integer) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.archive_output_ready_list() OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.archive_output_pending(uuid) OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.archive_output_history() OWNER TO :"audit_owner";
ALTER FUNCTION yoao_audit_v1.archive_output_save_candidate(
    uuid,uuid,date,bigint,bigint,bigint,bigint,text,bigint,text,jsonb,timestamptz
) OWNER TO :"audit_owner";
GRANT SELECT, INSERT, UPDATE (state, published_at)
    ON public.audit_archive_outputs TO :"audit_owner";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.refresh_audit_bi_mv() TO :"migration_role";
GRANT USAGE ON SCHEMA yoao_fixture_v1 TO :"audit_owner";
GRANT SELECT (id, parent_run_id, scope_key, family, state), UPDATE (id)
    ON yoao_fixture_v1.fixture_runs TO :"audit_owner";
GRANT SELECT (run_id, audit_id, audit_created_at)
    ON yoao_fixture_v1.fixture_audit_events TO :"audit_owner";
GRANT SELECT (run_id, state)
    ON yoao_fixture_v1.fixture_run_admissions TO :"audit_owner";
REVOKE CREATE ON SCHEMA yoao_audit_v1 FROM :"audit_owner";

GRANT CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
GRANT CREATE ON SCHEMA yoao_labeling_v1 TO :"labeling_owner";
GRANT CREATE ON SCHEMA yoao_workspace_v1 TO :"workspace_owner";
GRANT CREATE ON SCHEMA yoao_jobs_v1 TO :"jobs_owner";
GRANT CREATE ON SCHEMA yoao_audit_v1 TO :"audit_owner";
GRANT CREATE ON SCHEMA yoao_settings_v1 TO :"settings_owner";
GRANT CREATE ON SCHEMA yoao_notifications_v1 TO :"notifications_owner";
ALTER VIEW yoao_notifications_v1.notification_facts OWNER TO :"notifications_owner";
DO $$
DECLARE
    capability record;
BEGIN
    FOR capability IN
        SELECT proc.oid, scopes.owner_key
        FROM pg_catalog.pg_proc AS proc
        JOIN pg_catalog.pg_namespace AS ns ON ns.oid=proc.pronamespace
        JOIN (VALUES
            ('yoao_assets_v1','yoao.assets_owner'),
            ('yoao_labeling_v1','yoao.labeling_owner'),
            ('yoao_workspace_v1','yoao.workspace_owner'),
            ('yoao_jobs_v1','yoao.jobs_owner'),
            ('yoao_audit_v1','yoao.audit_owner'),
            ('yoao_settings_v1','yoao.settings_owner'),
            ('yoao_notifications_v1','yoao.notifications_owner')
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
               OR (ns.nspname = 'yoao_assets_v1' AND proc.proname LIKE '%video_%')
               OR (ns.nspname = 'yoao_labeling_v1' AND proc.proname LIKE '%video_input%')
               OR (ns.nspname = 'yoao_labeling_v1' AND proc.proname LIKE '%video_tracker%')
               OR ns.nspname IN ('yoao_settings_v1','yoao_notifications_v1'))
    LOOP
        EXECUTE pg_catalog.format('ALTER FUNCTION %s OWNER TO %I',
            capability.oid::pg_catalog.regprocedure,
            current_setting(capability.owner_key));
    END LOOP;
END $$;
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_labeling_v1 FROM :"labeling_owner";
REVOKE CREATE ON SCHEMA yoao_workspace_v1 FROM :"workspace_owner";
REVOKE CREATE ON SCHEMA yoao_jobs_v1 FROM :"jobs_owner";
REVOKE CREATE ON SCHEMA yoao_audit_v1 FROM :"audit_owner";
REVOKE CREATE ON SCHEMA yoao_settings_v1 FROM :"settings_owner";
REVOKE CREATE ON SCHEMA yoao_notifications_v1 FROM :"notifications_owner";

GRANT CREATE ON SCHEMA yoao_assets_v1 TO :"jobs_owner";
ALTER FUNCTION yoao_assets_v1.prepare_fixture_video_request_delivery() OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"jobs_owner";

GRANT CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
ALTER VIEW yoao_assets_v1.spatial_item_references OWNER TO :"assets_owner";
ALTER VIEW yoao_assets_v1.spatial_scene_references OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"insights_owner";
GRANT SELECT ON yoao_assets_v1.spatial_item_references,
    yoao_assets_v1.spatial_scene_references
    TO :"labeling_owner", :"insights_owner", :"workflow_role";
GRANT EXECUTE ON FUNCTION yoao_workspace_v1.read_spatial_project_access(uuid,uuid)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_v1.read_media_project_access(uuid,uuid),
    yoao_workspace_v1.lock_media_project_access(uuid,uuid),
    yoao_workspace_v1.read_media_video_collaboration_policy(uuid),
    yoao_workspace_v1.read_media_managed_projects(uuid,uuid[])
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.read_media_source(uuid,uuid,uuid)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    yoao_labeling_v1.read_spatial_camera_roles(uuid[]),
    yoao_labeling_v1.read_spatial_camera_target(uuid,uuid,text),
    yoao_labeling_v1.lock_spatial_camera_authority(uuid,uuid,text,uuid),
    yoao_labeling_v1.lock_spatial_preparation_scope(uuid),
    yoao_labeling_v1.read_spatial_preparation_scope(uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    yoao_workspace_v1.read_media_project_access(uuid,uuid),
    yoao_workspace_v1.lock_media_project_access(uuid,uuid),
    yoao_workspace_v1.read_media_managed_projects(uuid,uuid[]),
    yoao_labeling_v1.read_media_task_access(uuid,uuid,uuid),
    yoao_labeling_v1.read_media_visible_video_item(uuid,uuid),
    yoao_labeling_v1.lock_media_task_annotation_write(uuid,uuid,uuid),
    yoao_labeling_v1.read_media_failure_context(uuid,uuid[]),
    yoao_settings_v1.read_video_media_preferences()
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    yoao_audit_v1.record_sensor_calibration_update(uuid,uuid,uuid,uuid,text,integer,text,jsonb),
    yoao_audit_v1.record_storage_connection_change(uuid,uuid,text,uuid,jsonb,jsonb)
    TO :"assets_owner";
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
        'public.storage_fault_states', 'public.storage_observation_buckets',
        'yoao_assets_v1.storage_fault_observation_key',
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
        IF has_any_column_privilege(current_setting('yoao.workflow_role'), target, 'SELECT,INSERT,UPDATE,REFERENCES')
           OR has_table_privilege(current_setting('yoao.workflow_role'), target, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') THEN
            RAISE EXCEPTION 'Caller has direct privileges on %; use a clean dedicated role', target;
        END IF;
        IF has_any_column_privilege(current_setting('yoao.asset_cleanup_role'), target, 'SELECT,INSERT,UPDATE,REFERENCES')
           OR has_table_privilege(current_setting('yoao.asset_cleanup_role'), target, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') THEN
            RAISE EXCEPTION 'Asset cleanup role has direct privileges on %; use a clean dedicated role', target;
        END IF;
    END LOOP;

    IF has_database_privilege(current_setting('yoao.workflow_role'), current_database(), 'CREATE')
       OR has_schema_privilege(current_setting('yoao.workflow_role'), 'public', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.workflow_role'), 'yoao_identity_v1', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.workflow_role'), 'yoao_workspace_v1', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.workflow_role'), 'yoao_assets_v1', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.workflow_role'), 'yoao_labeling_v1', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.workflow_role'), 'yoao_jobs_v1', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.workflow_role'), 'yoao_learning_v1', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.workflow_role'), 'yoao_inference_v1', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.workflow_role'), 'yoao_insights_v1', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.workflow_role'), 'yoao_diagnostics_v1', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.workflow_role'), 'yoao_export_v1', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.workflow_role'), 'yoao_audit_v1', 'CREATE') THEN
        RAISE EXCEPTION 'Caller must not hold schema or database CREATE privileges';
    END IF;

    IF has_database_privilege(current_setting('yoao.asset_cleanup_role'), current_database(), 'CREATE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'public', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_identity_v1', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_workspace_v1', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_assets_v1', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_labeling_v1', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_jobs_v1', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_learning_v1', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_inference_v1', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_insights_v1', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_diagnostics_v1', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_export_v1', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_audit_v1', 'CREATE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_identity_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_workspace_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_labeling_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_jobs_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_learning_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_inference_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_insights_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_diagnostics_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_export_v1', 'USAGE')
       OR has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_audit_v1', 'USAGE') THEN
        RAISE EXCEPTION 'Asset cleanup role must be confined to the Assets schema';
    END IF;

    IF has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_jobs_v1.lock_task_creation_job(uuid,uuid,uuid,uuid,uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_jobs_v1.update_task_creation_progress(uuid,uuid,bigint,bigint,bigint)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_jobs_v1._task_creation_job_failure(text,text)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_jobs_v1._task_creation_job_payload_valid(uuid,jsonb)', 'EXECUTE') THEN
        RAISE EXCEPTION 'Caller must not execute Jobs-internal capabilities';
    END IF;

    IF has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_identity_v1.lock_active_actor(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_workspace_v1.lock_project_reference(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1.lock_dataset_reference(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1.lock_current_association(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1.lock_append_sources(uuid,uuid[])', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1.lock_task_creation_items(uuid,uuid[])', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_identity_v1.lock_audit_actor(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_workspace_v1._project_delete_failure(text)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1.delete_project_owned_records(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_labeling_v1.project_task_references(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_labeling_v1.delete_project_owned_records(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_jobs_v1.detach_project_references(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_jobs_v1.retain_async_job_project_scope()', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_learning_v1.lock_project_deletion(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_learning_v1.guard_project_deletion(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_learning_v1.delete_project_owned_records(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_inference_v1.clear_project_ml_backend_pool(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_inference_v1.clear_project_pipelines(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_insights_v1.clear_project_task_views(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_diagnostics_v1.detach_project_references(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_export_v1.clear_project_export_artifacts(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_audit_v1.record_project_deletion(uuid,uuid,uuid,text,text,bigint,bigint,jsonb)', 'EXECUTE') THEN
        RAISE EXCEPTION 'Caller must use only the public project deletion capability';
    END IF;

    IF has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1.pending_storage_deletions(uuid,integer)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1.pending_storage_deletions_v2(text,timestamptz,uuid,uuid,integer)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1.complete_storage_deletion(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_jobs_v1.cancel_dataset_task_creation(uuid,uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_audit_v1.valid_request_context(jsonb)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_audit_v1.record_dataset_unlink(uuid,uuid,uuid,uuid,bigint,bigint,uuid[],jsonb)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_workspace_v1._dataset_unlink_failure(text,text)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1.dataset_item_references(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1.dataset_scene_references(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1.prepare_dataset_unlink_cleanup(uuid,uuid,uuid,text)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1.remove_dataset_association(uuid,uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_diagnostics_v1.detach_dataset_task_references(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_learning_v1.guard_dataset_item_deletion(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_learning_v1.guard_dataset_unlink(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_labeling_v1._dataset_unlink_task_snapshot(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_labeling_v1.dataset_task_references(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_labeling_v1.lock_dataset_unlink_scope(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_labeling_v1.guard_dataset_unlink(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_labeling_v1.preview_dataset_unlink(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_labeling_v1.delete_dataset_tasks(uuid,uuid)', 'EXECUTE') THEN
        RAISE EXCEPTION 'Caller must use the Workspace dataset-unlink capability';
    END IF;

    IF has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1._accept_dataset_upload(uuid,uuid,uuid,uuid,bigint,text,integer,integer)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1._finish_dataset_upload(uuid,uuid,uuid,uuid,bigint)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1._dataset_upload_failure(text)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1._dataset_upload_success(jsonb)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1._dataset_upload_actor(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1._dataset_upload_facts(public.dataset_upload_intents)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1._delete_dataset_source(uuid,uuid,uuid,uuid,text,jsonb)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1._delete_dataset_source_records(uuid,uuid,uuid,text)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1._source_delete_failure(text)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1._lock_dataset_source_deletion(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1._queue_pyramid_deletions(uuid,uuid[],text)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1.dataset_association_references(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_jobs_v1.guard_dataset_source_deletion(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_jobs_v1.task_creation_execution_active(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_learning_v1.guard_dataset_source_deletion(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_labeling_v1.guard_dataset_source_deletion(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_labeling_v1.detach_deleted_dataset_attribution(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_audit_v1.record_dataset_source_deletion(uuid,uuid,uuid,uuid,bigint,bigint,bigint,bigint,bigint,bigint,jsonb)', 'EXECUTE') THEN
        RAISE EXCEPTION 'Workflow caller must use the public Assets source/upload capabilities';
    END IF;

    IF NOT has_schema_privilege(current_setting('yoao.asset_cleanup_role'), 'yoao_assets_v1', 'USAGE')
       OR NOT has_function_privilege(current_setting('yoao.asset_cleanup_role'),
        'yoao_assets_v1.pending_storage_deletions_v2(text,timestamptz,uuid,uuid,integer)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('yoao.asset_cleanup_role'),
        'yoao_assets_v1.complete_storage_deletion(uuid,uuid)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('yoao.asset_cleanup_role'),
        'yoao_assets_v1.claim_image_garbage(uuid,integer,integer)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('yoao.asset_cleanup_role'),
        'yoao_assets_v1.complete_image_garbage(uuid,uuid)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('yoao.asset_cleanup_role'),
        'yoao_assets_v1.claim_accepted_input_garbage(uuid,integer,integer)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('yoao.asset_cleanup_role'),
        'yoao_assets_v1.complete_accepted_input_garbage(uuid,uuid)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('yoao.asset_cleanup_role'),
        'yoao_assets_v1.claim_accepted_video_input_garbage(uuid,integer,integer)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('yoao.asset_cleanup_role'),
        'yoao_assets_v1.complete_accepted_video_input_garbage(uuid,uuid)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('yoao.asset_cleanup_role'),
        'yoao_assets_v1.claim_native_pyramid_retirement(uuid,integer)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('yoao.asset_cleanup_role'),
        'yoao_assets_v1.prepare_native_pyramid_retirement_page(uuid,uuid,integer)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('yoao.asset_cleanup_role'),
        'yoao_assets_v1.complete_native_pyramid_retirement_page(uuid,uuid,uuid,text,jsonb)', 'EXECUTE')
       OR has_function_privilege(current_setting('yoao.asset_cleanup_role'),
        'yoao_assets_v1.pending_storage_deletions(uuid,integer)', 'EXECUTE')
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.pg_proc AS proc
           WHERE proc.pronamespace = 'yoao_assets_v1'::pg_catalog.regnamespace
             AND pg_catalog.has_function_privilege(
                 current_setting('yoao.asset_cleanup_role'), proc.oid, 'EXECUTE'
             )
             AND proc.oid NOT IN (
                 'yoao_assets_v1.pending_storage_deletions_v2(text,timestamptz,uuid,uuid,integer)'::pg_catalog.regprocedure,
                 'yoao_assets_v1.complete_storage_deletion(uuid,uuid)'::pg_catalog.regprocedure,
                 'yoao_assets_v1.claim_image_garbage(uuid,integer,integer)'::pg_catalog.regprocedure,
                 'yoao_assets_v1.complete_image_garbage(uuid,uuid)'::pg_catalog.regprocedure,
                 'yoao_assets_v1.claim_accepted_input_garbage(uuid,integer,integer)'::pg_catalog.regprocedure,
                 'yoao_assets_v1.complete_accepted_input_garbage(uuid,uuid)'::pg_catalog.regprocedure,
                 'yoao_assets_v1.claim_accepted_video_input_garbage(uuid,integer,integer)'::pg_catalog.regprocedure,
                 'yoao_assets_v1.complete_accepted_video_input_garbage(uuid,uuid)'::pg_catalog.regprocedure,
                 'yoao_assets_v1.claim_native_pyramid_retirement(uuid,integer)'::pg_catalog.regprocedure,
                 'yoao_assets_v1.prepare_native_pyramid_retirement_page(uuid,uuid,integer)'::pg_catalog.regprocedure,
                 'yoao_assets_v1.complete_native_pyramid_retirement_page(uuid,uuid,uuid,text,jsonb)'::pg_catalog.regprocedure
             )
       ) OR EXISTS (
           SELECT 1
           FROM pg_catalog.pg_proc AS proc
           JOIN pg_catalog.pg_namespace AS namespace
             ON namespace.oid = proc.pronamespace
           WHERE namespace.nspname <> 'yoao_assets_v1'
             AND (pg_catalog.left(namespace.nspname, 4) = 'aap_'
                  OR pg_catalog.left(namespace.nspname, 5) = 'yoao_')
             AND pg_catalog.right(namespace.nspname, 3) = '_v1'
             AND pg_catalog.has_function_privilege(
                 current_setting('yoao.asset_cleanup_role'), proc.oid, 'EXECUTE'
             )
       ) THEN
        RAISE EXCEPTION 'Asset cleanup role must execute only its outbox functions';
    END IF;

    IF NOT has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_workspace_v1.delete_project(uuid,uuid,uuid,jsonb)', 'EXECUTE') THEN
        RAISE EXCEPTION 'Caller is missing the public project deletion capability';
    END IF;
    IF NOT has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_workspace_v1.preview_dataset_unlink(uuid,uuid,uuid)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_workspace_v1.unlink_dataset(uuid,uuid,uuid,uuid,text,jsonb)', 'EXECUTE') THEN
        RAISE EXCEPTION 'Caller is missing the public dataset-unlink capabilities';
    END IF;
    IF NOT has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1.delete_dataset(uuid,uuid,uuid,text,jsonb)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1.delete_dataset_item(uuid,uuid,uuid,uuid,text,jsonb)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1.reserve_dataset_upload(uuid,uuid,uuid,text,text)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1.lock_dataset_upload(uuid,uuid,uuid)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1.find_dataset_upload_duplicate(uuid,uuid,uuid,text)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1.complete_dataset_upload(uuid,uuid,uuid,uuid,bigint,text,integer,integer)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1.discard_dataset_upload(uuid,uuid,uuid,text)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('yoao.workflow_role'),
        'yoao_assets_v1.lock_scene_source_references(uuid[])', 'EXECUTE') THEN
        RAISE EXCEPTION 'Caller is missing a public Assets source or upload capability';
    END IF;
END $$;

GRANT CREATE ON SCHEMA yoao_labeling_v1 TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.write_project_counter_projection(uuid, jsonb)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.decrement_project_review_count(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.decrement_project_completed_count(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.record_project_review_approval(uuid)
    OWNER TO :"labeling_owner";
REVOKE CREATE ON SCHEMA yoao_labeling_v1 FROM :"labeling_owner";

GRANT CREATE ON SCHEMA yoao_jobs_v1 TO :"jobs_owner";
GRANT CREATE ON SCHEMA yoao_labeling_v1 TO :"labeling_owner";
DO $$
DECLARE
    target record;
    owner_name text;
BEGIN
    FOR target IN
        SELECT proc.oid::regprocedure AS signature, namespace.nspname
        FROM pg_catalog.pg_proc AS proc
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = proc.pronamespace
        WHERE namespace.nspname IN ('yoao_jobs_v1', 'yoao_labeling_v1')
          AND proc.prokind = 'f' AND (proc.proname LIKE '%batch_result%'
              OR proc.proname LIKE '%prediction_retry%')
    LOOP
        owner_name := CASE target.nspname
            WHEN 'yoao_jobs_v1' THEN current_setting('yoao.jobs_owner')
            ELSE current_setting('yoao.labeling_owner') END;
        EXECUTE pg_catalog.format('ALTER FUNCTION %s OWNER TO %I', target.signature, owner_name);
    END LOOP;
END $$;
REVOKE CREATE ON SCHEMA yoao_jobs_v1 FROM :"jobs_owner";
REVOKE CREATE ON SCHEMA yoao_labeling_v1 FROM :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    yoao_jobs_v1._batch_result_scope_valid(jsonb, boolean),
    yoao_jobs_v1.read_batch_result_execution(jsonb),
    yoao_jobs_v1.lock_batch_result_execution(jsonb),
    yoao_jobs_v1.record_batch_result_member(jsonb),
    yoao_jobs_v1.advance_batch_result_member(jsonb),
    yoao_jobs_v1.batch_result_execution_referenced(jsonb),
    yoao_jobs_v1._create_prediction_retry_job(jsonb),
    yoao_jobs_v1._finish_prediction_retry_job(jsonb),
    yoao_jobs_v1.lock_prediction_retry_finish_scope(jsonb)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    yoao_labeling_v1.batch_result_commit_proof(jsonb),
    yoao_labeling_v1.batch_result_referenced(jsonb),
    yoao_labeling_v1.retire_batch_results(jsonb),
    yoao_labeling_v1._prediction_retry_no_raw_data(jsonb),
    yoao_labeling_v1._prediction_retry_attempt_scope_valid(jsonb, boolean),
    yoao_labeling_v1._prediction_retry_attempt_exists(uuid),
    yoao_labeling_v1._prediction_retry_member_scope_matches(uuid, uuid, jsonb),
    yoao_labeling_v1._prediction_retry_terminal_matches(uuid, text, jsonb),
    yoao_labeling_v1._abandon_prediction_retry_from_job(uuid, text, text),
    yoao_labeling_v1._retire_prediction_retry_results(uuid),
    yoao_labeling_v1._prediction_retry_job_delete_allowed(uuid)
    TO :"jobs_owner";
GRANT CREATE ON SCHEMA yoao_jobs_v1 TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.video_execution_checkpoint_restore OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.video_execution_journals OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.video_execution_window_attempts OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.video_execution_captures OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.video_execution_capture_parts OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.video_execution_windows OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.video_execution_window_parts OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.video_execution_span_closures OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.video_execution_checkpoint_step(text,text) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_checkpoint_results_complete(uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.video_execution_running_mask_roots_live(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.video_execution_checkpoint_mask_retained(text) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.video_execution_input_retained(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.video_execution_source_item_retained(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.video_tracker_input_retention_facts(uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_checkpoint_reply(jsonb,text) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_checkpoint_canonical_json(jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_checkpoint_digest(jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_checkpoint_integer(jsonb,bigint,bigint) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_checkpoint_digest_valid(text) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_checkpoint_plan_identities_complete(jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_checkpoint_manifest_valid(jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_checkpoint_plan_valid(jsonb,jsonb,uuid,jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_checkpoint_input_facts(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_checkpoint_all_routes_retired(uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._video_execution_checkpoint_terminal() OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA yoao_jobs_v1 FROM :"jobs_owner";
GRANT CREATE ON SCHEMA yoao_labeling_v1 TO :"labeling_owner";
ALTER TABLE yoao_labeling_v1.video_execution_checkpoint_mask_sets OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.video_execution_request_facts(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.retain_video_execution_checkpoint_masks(uuid,text,jsonb) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._mask_gc_root_entry_live(text,text,timestamptz,boolean) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._mask_gc_release_tracker_running_outputs() OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.video_tracker_input_referenced(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.guard_dataset_source_deletion(uuid,uuid) OWNER TO :"labeling_owner";
REVOKE CREATE ON SCHEMA yoao_labeling_v1 FROM :"labeling_owner";
GRANT CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.video_execution_input_facts(uuid) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
GRANT CREATE ON SCHEMA yoao_inference_routing_v1 TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1.video_execution_scope_retired(uuid) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1.video_execution_scope_facts(uuid) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._tracker_scopes_physically_retired(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1._guard_disabled_plugin_tracker_scope()
    OWNER TO :"inference_owner";
REVOKE CREATE ON SCHEMA yoao_inference_routing_v1 FROM :"inference_owner";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.video_execution_request_facts(uuid) TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.video_execution_input_facts(uuid) TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.retain_video_execution_checkpoint_masks(uuid,text,jsonb) TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_inference_routing_v1.video_execution_scope_retired(uuid) TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_inference_routing_v1.video_execution_scope_facts(uuid) TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.video_execution_running_mask_roots_live(uuid) TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.video_execution_checkpoint_mask_retained(text) TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.video_execution_input_retained(uuid) TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.video_execution_source_item_retained(uuid) TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.video_tracker_input_retention_facts(uuid,uuid)
    TO :"labeling_owner";

GRANT CREATE ON SCHEMA yoao_labeling_v1 TO :"labeling_owner";
ALTER TABLE yoao_labeling_v1.video_writer_receipts OWNER TO :"labeling_owner";
DO $video_writer_owners$
DECLARE
    capability regprocedure;
BEGIN
    FOR capability IN
        SELECT function.oid::regprocedure
        FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = function.pronamespace
        WHERE namespace.nspname = 'yoao_labeling_v1'
          AND (function.proname = 'video_writer_step'
               OR pg_catalog.left(function.proname,14) = '_video_writer_')
    LOOP
        EXECUTE pg_catalog.format('ALTER FUNCTION %s OWNER TO %I',
            capability, current_setting('yoao.labeling_owner'));
    END LOOP;
END
$video_writer_owners$;
REVOKE CREATE ON SCHEMA yoao_labeling_v1 FROM :"labeling_owner";
GRANT CREATE ON SCHEMA yoao_jobs_v1 TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.video_writer_authority(text,jsonb) OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA yoao_jobs_v1 FROM :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.video_writer_authority(text,jsonb)
    TO :"labeling_owner";

GRANT CREATE ON SCHEMA yoao_jobs_v1 TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.video_restore_fence OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.video_restore_authority(text,jsonb) OWNER TO :"jobs_owner";
DO $video_restore_jobs_owners$
DECLARE
    capability regprocedure;
BEGIN
    FOR capability IN
        SELECT function.oid::regprocedure
        FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = function.pronamespace
        WHERE namespace.nspname = 'yoao_jobs_v1'
          AND pg_catalog.left(function.proname,15) = '_video_restore_'
    LOOP
        EXECUTE pg_catalog.format('ALTER FUNCTION %s OWNER TO %I',
            capability, current_setting('yoao.jobs_owner'));
    END LOOP;
END
$video_restore_jobs_owners$;
REVOKE CREATE ON SCHEMA yoao_jobs_v1 FROM :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.video_restore_authority(text,jsonb)
    TO :"inference_owner";
GRANT CREATE ON SCHEMA yoao_inference_routing_v1 TO :"inference_owner";
ALTER TABLE yoao_inference_routing_v1.video_restore_records OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_routing_v1.video_restore_contexts OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_routing_v1.video_restore_scopes OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_routing_v1.video_restore_attempts OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_routing_v1.video_restore_scope_retirements OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_routing_v1.video_restore_no_scope_receipts OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_routing_v1.video_restore_function_restore OWNER TO :"inference_owner";
DO $video_restore_owners$
DECLARE
    capability regprocedure;
BEGIN
    FOR capability IN
        SELECT function.oid::regprocedure
        FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = function.pronamespace
        WHERE namespace.nspname = 'yoao_inference_routing_v1'
          AND (function.proname = 'video_restore_step'
               OR function.proname = '_video_execution_scope_step_pre_restore'
               OR pg_catalog.left(function.proname,15) = '_video_restore_')
    LOOP
        EXECUTE pg_catalog.format('ALTER FUNCTION %s OWNER TO %I',
            capability, current_setting('yoao.inference_owner'));
    END LOOP;
END
$video_restore_owners$;
REVOKE CREATE ON SCHEMA yoao_inference_routing_v1 FROM :"inference_owner";
GRANT CREATE ON SCHEMA yoao_jobs_v1 TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.video_deletion_clearances OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_video_execution_deletion(uuid[],uuid[],uuid)
    OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA yoao_jobs_v1 FROM :"jobs_owner";
GRANT EXECUTE ON FUNCTION
    yoao_jobs_v1.guard_video_execution_deletion(uuid[],uuid[],uuid)
    TO :"labeling_owner";
GRANT CREATE ON SCHEMA yoao_labeling_v1 TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.guard_project_video_deletion(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.guard_dataset_video_deletion(uuid,uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.delete_fixture_project_rows(uuid[])
    OWNER TO :"labeling_owner";
REVOKE CREATE ON SCHEMA yoao_labeling_v1 FROM :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    yoao_labeling_v1.guard_project_video_deletion(uuid),
    yoao_labeling_v1.guard_dataset_video_deletion(uuid,uuid)
    TO :"workspace_owner";
GRANT CREATE ON SCHEMA yoao_jobs_v1 TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._async_job_list_rows(uuid,text,text[],text[],uuid,text)
    OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.async_job_list_page(uuid,text,text[],text[],uuid,text,integer,integer)
    OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.async_job_list_total(uuid,text,text[],text[],uuid,text)
    OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA yoao_jobs_v1 FROM :"jobs_owner";
GRANT USAGE ON SCHEMA yoao_identity_v1, yoao_workspace_v1,
    yoao_workspace_project_query_v1 TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.read_actor_facts(uuid),
    yoao_workspace_v1.actor_project_memberships(uuid),
    yoao_workspace_project_query_v1.project_search_scope(uuid,text)
    TO :"jobs_owner";
GRANT USAGE ON SCHEMA yoao_workspace_fixture_v1 TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_workspace_fixture_v1.guard_project_cleanup(uuid[])
    TO :"jobs_owner";
GRANT USAGE ON SCHEMA yoao_workspace_fixture_v1 TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_workspace_fixture_v1.guard_project_cleanup(uuid[])
    TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.guard_fixture_presentation_failures(uuid,uuid,uuid[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.guard_fixture_project_retry_anchors(uuid[])
    OWNER TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    yoao_labeling_v1.guard_fixture_presentation_failures(uuid,uuid,uuid[]),
    yoao_labeling_v1.guard_fixture_project_retry_anchors(uuid[])
    TO :"jobs_owner";
GRANT SELECT, INSERT, UPDATE, DELETE ON public.async_jobs TO :"jobs_owner";
GRANT CREATE ON SCHEMA yoao_jobs_v1 TO :"jobs_owner";
ALTER VIEW yoao_jobs_v1.async_job_creation OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.delete_fixture_project_jobs(uuid[]) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.delete_fixture_presentation_jobs(uuid,uuid,uuid[])
    OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.set_filter_fixture_job_outcomes(uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_fixture_project_jobs(uuid[]) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.fixture_dataset_job_scope(uuid,text,jsonb,uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.fixture_job_dataset_scope(uuid,text,jsonb,uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_fixture_dataset_jobs(uuid[]) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.create_filter_fixture_jobs(uuid,uuid,uuid,uuid,uuid,uuid,text,text)
    OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.lock_action_idempotency(text,uuid,uuid,text) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.find_action_idempotent_jobs(text,uuid,uuid,text) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.find_video_retry_job(uuid,uuid,text) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.lock_video_retry_jobs(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.read_job(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.read_job_for_update(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.read_job_for_share_nowait(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.read_job_for_update_nowait(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.lock_cross_frame_singleflight(uuid,text) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.active_cross_frame_job(uuid,text) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.cross_frame_job_page(uuid,text,integer) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.cross_frame_job_count(uuid,text) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.latest_batch_prediction_times(uuid[]) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.frozen_deployment_usage(uuid,uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.jobs_by_execution_id(text) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.insert_video_summary(uuid,text,uuid,uuid,jsonb,text) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.mark_dispatch_failed(uuid,timestamptz,text) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.cancel_batch_predict_jobs(uuid,timestamptz) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.delete_batch_predict_jobs(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.delete_terminal_jobs_before(timestamptz,text[]) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._jobs_lifecycle_has_keys(jsonb,text[]) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._jobs_lifecycle_is_truthy(jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._jobs_lifecycle_integer(jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.apply_lifecycle(uuid,jsonb) OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.dispatch_intents OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.prepare_dispatch(jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.claim_dispatches(jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.finish_dispatch(jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.dispatch_status(uuid) OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.dispatch_publication_attempts OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.dispatch_retry_publications OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.dispatch_publication_retirements OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.publication_job_births OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.publication_intent_births OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.fixture_media_deliveries OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.fixture_media_publication_attempts OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.fixture_media_publication_retirements OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_publication_attempt() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_publication_retirement() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_publication_birth() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.capture_publication_job_birth() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.capture_publication_intent_birth() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.job_publications_retired(uuid,text[]) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.job_publications_not_sent(uuid,text[]) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.publication_session_owns(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.begin_dispatch_publication(uuid,uuid,text,bigint,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.finish_dispatch_publication(uuid,uuid,text,bigint,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.refuse_dispatch_publication(uuid,uuid,text,bigint,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.retry_publication_intent(uuid,text,text,jsonb,jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.begin_retry_publication(uuid,uuid,text,text,jsonb,jsonb,integer,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.finish_retry_publication(uuid,uuid,text,integer,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_retired_dispatch() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.retire_job_publications(uuid,text[]) OWNER TO :"jobs_owner";
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"jobs_owner";
GRANT SELECT (id, status, job_kind)
    ON yoao_labeling_v1.video_tracker_job_rows TO :"jobs_owner";
REVOKE CREATE ON SCHEMA yoao_jobs_v1 FROM :"jobs_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_inference_topology_v1,
    yoao_inference_backend_endpoint_v1, yoao_inference_gpu_v1,
    yoao_inference_gpu_caller_v1, yoao_inference_gpu_collector_v1 TO :"inference_owner";
GRANT SELECT, INSERT, UPDATE, DELETE ON public.ml_backend_registry,
    public.ml_backend_service_pools, public.ml_backend_pool_members,
    public.project_ml_backend_pool, public.gpu_backend_fences,
    public.gpu_backend_memberships, public.gpu_arbiter_rollouts,
    public.gpu_backend_cancel_intents TO :"inference_owner";
DO $inference_owner$
DECLARE
    v_function record;
    v_owner name := current_setting('yoao.inference_owner');
BEGIN
    FOR v_function IN
        SELECT p.oid::pg_catalog.regprocedure AS signature
        FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname IN (
            'yoao_inference_topology_v1','yoao_inference_backend_endpoint_v1',
            'yoao_inference_gpu_v1','yoao_inference_gpu_caller_v1',
            'yoao_inference_gpu_collector_v1'
        )
    LOOP
        EXECUTE pg_catalog.format('ALTER FUNCTION %s OWNER TO %I',v_function.signature,v_owner);
    END LOOP;
END;
$inference_owner$;
ALTER TABLE yoao_inference_topology_v1.plugin_asset_approvals OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_backend_endpoint_v1.endpoint_secret OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_gpu_v1.installation_control_intents OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_gpu_v1.installation_terminal_intents OWNER TO :"inference_owner";
REVOKE CREATE ON SCHEMA yoao_inference_topology_v1,
    yoao_inference_backend_endpoint_v1, yoao_inference_gpu_v1,
    yoao_inference_gpu_caller_v1, yoao_inference_gpu_collector_v1 FROM :"inference_owner";
GRANT SELECT (avatar_ref), UPDATE (avatar_ref) ON public.users TO :"identity_owner";
GRANT SELECT, UPDATE ON public.avatar_uploads TO :"identity_owner";
GRANT SELECT (guide_assets_revision), UPDATE (guide_assets_revision)
    ON public.projects TO :"workspace_owner";
GRANT SELECT, INSERT, DELETE
    ON public.project_guide_assets TO :"workspace_owner";
GRANT SELECT, UPDATE (id, state, retired_at)
    ON public.guide_asset_uploads TO :"workspace_owner";
GRANT SELECT (id, bound_comment_id, state, created_at), UPDATE (id, state, retired_at)
    ON public.comment_attachment_uploads TO :"labeling_owner";
GRANT SELECT (execution_snapshot_id), INSERT (execution_snapshot_id)
    ON public.failed_predictions TO :"labeling_owner";
GRANT SELECT (id, project_id) ON public.prediction_execution_snapshots TO :"jobs_owner";
GRANT SELECT (id, payload) ON public.prediction_execution_snapshots TO :"inference_owner";
GRANT USAGE ON SCHEMA yoao_inference_pipelines_v1 TO :"jobs_owner", :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_inference_pipelines_v1.frozen_snapshot_uses_deployment(uuid,uuid)
    TO :"jobs_owner", :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.retryable_deployment_usage(uuid,uuid,uuid)
    TO :"jobs_owner";
GRANT SELECT (execution_snapshot_id), UPDATE (execution_snapshot_id)
    ON public.async_jobs TO :"jobs_owner";

GRANT SELECT, INSERT, UPDATE ON public.storage_locations,
    public.managed_original_creations, public.dataset_assets,
    public.dataset_asset_locations, public.storage_operations TO :"assets_owner";
GRANT SELECT (bucket, object_key, state)
    ON public.managed_original_reclaim_claims TO :"assets_owner";
GRANT DELETE ON public.storage_locations TO :"assets_owner";
GRANT SELECT ON public.storage_bindings TO :"assets_owner";
GRANT UPDATE (purpose) ON public.storage_bindings TO :"assets_owner";
GRANT SELECT (revision), INSERT (revision), UPDATE (revision)
    ON public.storage_connections TO :"assets_owner";
GRANT SELECT, INSERT ON public.storage_maintenance_gates TO :"assets_owner";
GRANT UPDATE (location_id, state, revision, changed_at)
    ON public.storage_maintenance_gates TO :"assets_owner";
GRANT SELECT (location_id) ON public.storage_location_probes TO :"assets_owner";
GRANT SELECT (node_id, result) ON public.storage_location_probes TO :"assets_owner";
GRANT SELECT (id, dataset_id, location_id, name, enabled, unregistered_at, config,
              config_revision, policy_revision, cache_policy)
    ON public.dataset_sources TO :"assets_owner";
GRANT SELECT (source_id, path_digest, content_sha256, asset_id)
    ON public.dataset_source_items TO :"assets_owner";
GRANT SELECT (id, source_id, async_job_id)
    ON public.dataset_scan_runs, public.dataset_registration_runs TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE ON public.storage_fault_states, public.storage_observation_buckets
    TO :"assets_owner";
GRANT USAGE ON SEQUENCE public.storage_fault_observation_seq TO :"assets_owner";
GRANT SELECT ON public.image_pyramid_assets, public.image_pyramid_generations,
    public.image_pyramid_outputs TO :"assets_owner";
GRANT UPDATE (id) ON public.image_pyramid_assets TO :"assets_owner";
GRANT UPDATE (asset_id) ON public.image_pyramid_generations TO :"assets_owner";
GRANT UPDATE (id) ON public.image_pyramid_outputs TO :"assets_owner";
GRANT INSERT (id, asset_id, generation, lease_token, storage_ref, state)
    ON public.image_pyramid_outputs TO :"assets_owner";
GRANT SELECT (id,state,storage_ref,sha256,size_bytes,etag,version_id,uncertain_write,error_code)
    ON public.thumbnail_outputs TO :"assets_owner";
GRANT SELECT (id,state,storage_ref,inventory,lease_token,dataset_item_id)
    ON public.video_derivative_outputs TO :"assets_owner";
GRANT UPDATE (id) ON public.thumbnail_outputs, public.video_derivative_outputs
    TO :"assets_owner";

GRANT SELECT (location_id) ON public.original_cache_entries,
    public.managed_original_retirements, public.dataset_auxiliary_originals,
    public.task_originals TO :"assets_owner";
GRANT SELECT (storage_identity, bucket, object_key, etag, version_id)
    ON public.task_originals TO :"assets_owner";
GRANT SELECT (id, state, source_location_id, target_location_id)
    ON public.storage_migration_read_publications TO :"assets_owner";
GRANT SELECT (publication_id, bucket, object_key, sha256, size_bytes, source_etag, source_version_id)
    ON public.storage_migration_read_routes TO :"assets_owner";
GRANT SELECT (storage_ref) ON public.thumbnail_outputs,
    public.video_derivative_outputs, public.prediction_input_leases,
    public.mask_format_uploads, public.comment_attachment_uploads,
    public.avatar_uploads, public.guide_asset_uploads, public.raster_mask_objects,
    public.bug_attachment_uploads, public.audit_archive_outputs,
    public.export_outputs, public.export_media_bundles,
    public.training_dataset_versions, public.training_runs,
    public.training_previews, public.sample_evaluation_runs,
    public.active_learning_rounds, public.training_storage_deletions TO :"assets_owner";
GRANT SELECT (staging_ref) ON public.mask_format_imports TO :"assets_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.storage_connection_revision_bump() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._connector_location_id(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._storage_connection_location_referenced(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._managed_original_endpoint(text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._managed_original_prefix(text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._managed_original_identity(text,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._managed_original_gate_revision(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._managed_original_request_valid(jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._managed_original_reply(jsonb,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._managed_original_facts(public.managed_original_creations) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._managed_original_proof_valid(jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.publish_verified_dataset_original() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._managed_original_source_gate_active(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_publication_storage_gate(uuid,bigint) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_video_derivative_links(uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.publish_image_pyramid_thumbnail(uuid,integer,uuid) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"identity_owner", :"workspace_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.lock_publication_storage_gate(uuid,bigint)
    TO :"identity_owner", :"workspace_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_labeling_v1 TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.retire_project_comment_uploads(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.publish_task_pyramid_thumbnail(uuid,text) OWNER TO :"labeling_owner";
REVOKE CREATE ON SCHEMA yoao_labeling_v1 FROM :"labeling_owner";
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"assets_owner", :"workspace_owner";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.retire_project_comment_uploads(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.publish_task_pyramid_thumbnail(uuid,text)
    TO :"assets_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_workspace_v1 TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.retire_project_uploads(uuid) OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA yoao_workspace_v1 FROM :"workspace_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_jobs_v1 TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.bind_prediction_execution_snapshot(uuid,uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.read_job_execution_snapshot(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.read_job_progress(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.read_job_progress_for_update(uuid,text,text) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.record_job_progress(uuid,text,text,timestamptz,jsonb) OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA yoao_jobs_v1 FROM :"jobs_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_notifications_v1 TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1._notification_is_migration_inventory(uuid) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1._notification_unknown_job_scope(uuid) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1._visible_inbox_rows_scoped(uuid,text) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.read_state_command_scoped(jsonb) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.delete_command_scoped(jsonb) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.inbox_page_scoped(uuid,text,boolean,integer,integer) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.inbox_total_count_scoped(uuid,text) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.inbox_unread_count_scoped(uuid,text) OWNER TO :"notifications_owner";
REVOKE CREATE ON SCHEMA yoao_notifications_v1 FROM :"notifications_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_workspace_configuration_v1 TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1._guide_entry_matches(jsonb,uuid,uuid,text,text,integer,timestamptz)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1._guide_lock_project(uuid,text) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1._guide_authority(uuid,uuid) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1._guide_storage_gate(uuid,bigint) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1._guide_failure(jsonb,text,integer,text) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_configuration_v1.guide_assets_step(jsonb,text,jsonb) OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA yoao_workspace_configuration_v1 FROM :"workspace_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_identity_v1 TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.read_accepted_api_key_facts(uuid,uuid) OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA yoao_identity_v1 FROM :"identity_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_labeling_v1 TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_task_rows_for_core_read_share(uuid[],uuid,uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_task_dataset_item_link_rows_for_core_read_share(uuid[],integer)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_prediction_for_core_read_share(uuid,uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_prediction_rows_for_core_read_share(uuid,uuid[],integer)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_scene_track_rows_for_core_read(uuid[],integer)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_scene_track_interval_rows_for_core_read_share(uuid[],integer)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_prediction_meta_for_core_read_share(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_video_segment_for_core_read_share(uuid,uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_video_segment_ids_for_export_share(uuid[],integer)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_video_quality_run_ids_for_export_share(uuid[],integer)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.publish_task_original(uuid,uuid,uuid,uuid,uuid,text)
    OWNER TO :"labeling_owner";
REVOKE CREATE ON SCHEMA yoao_labeling_v1 FROM :"labeling_owner";
GRANT SELECT (id, task_id, project_id, creation_id, sha256, size_bytes)
    ON public.task_originals TO :"assets_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_task_original_binding(uuid) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.read_task_original_binding(uuid)
    TO :"labeling_owner";
GRANT DELETE ON public.storage_operations TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.read_accepted_api_key_facts(uuid,uuid)
    TO :"assets_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_ingest_publication_authority(uuid,uuid,jsonb,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.stage_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._connection_probe_reply(jsonb,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._connection_authority_proof(uuid,uuid,text[],jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_connection_probe(uuid,uuid,uuid,text[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_connection_probe(uuid,uuid,uuid,uuid,text[],jsonb,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.update_storage_connection_authorized(uuid,uuid,uuid,jsonb,jsonb,text,text,text,jsonb,jsonb,uuid,text[],jsonb)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.publish_operator_item_original(jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.operator_item_original_facts(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.operator_auxiliary_pointer(uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._managed_original_import_prefix(text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._managed_original_scope_key(text,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._operator_read_permit_purpose(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_operator_auxiliary(jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.publish_operator_auxiliary(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.operator_auxiliary_owner(uuid,text,jsonb,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.operator_auxiliary_material(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.operator_item_original_material(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.bind_operator_auxiliary_item(uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.bind_operator_auxiliary_scene(uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.operator_dataset_state(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.operator_scene_state(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.open_operator_storage_operation(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_operator_original_prepared(uuid,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.mark_operator_original_writing(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_operator_original_written(uuid,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_operator_original_verified(uuid,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fail_operator_original(uuid,text,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.close_operator_storage_operation(uuid,uuid,boolean) OWNER TO :"assets_owner";
GRANT SELECT, INSERT ON public.dataset_auxiliary_originals TO :"assets_owner";
GRANT DELETE ON public.dataset_asset_locations TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE ON public.storage_usage_mutations TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";

GRANT CREATE ON SCHEMA yoao_jobs_v1 TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.prediction_mask_producers OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.prediction_mask_producer_objects OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.fixture_prediction_job_births OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.fixture_prediction_job_datasets OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.fixture_worker_job_datasets OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.unsettled_worker_job_deletions OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.retain_unsettled_worker_deletion() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.cross_frame_job_birth(uuid,uuid,uuid,jsonb) OWNER TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.cross_frame_job_birth(uuid,uuid,uuid,jsonb) TO :"assets_owner";
ALTER FUNCTION yoao_jobs_v1.cross_frame_birth_identity_matches(uuid,uuid,uuid,jsonb) OWNER TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.cross_frame_birth_identity_matches(uuid,uuid,uuid,jsonb) TO :"assets_owner";
ALTER FUNCTION yoao_jobs_v1.worker_job_scope_digest(text,jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_worker_job_dataset_receipt() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.record_worker_job_dataset_scope() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_worker_job_scope_change() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.retry_born_dataset_scope(uuid,uuid,uuid,jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_retry_born_dataset_scope() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.bind_prediction_retry_attempt_birth() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_prediction_retry_attempt_scope() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_retry_born_authority() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.retry_authority_snapshot_matches(uuid,jsonb,uuid) OWNER TO :"jobs_owner";
GRANT SELECT (digest) ON public.prediction_execution_snapshots TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.fixture_prediction_dispatch_tombstones OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.begin_prediction_mask_producer(jsonb,bigint,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.bind_prediction_mask_object(uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.finish_prediction_mask_producer(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.prediction_mask_object_retained(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.record_fixture_prediction_job_birth() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_fixture_prediction_job_datasets() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.prediction_fixture_open(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.preserve_fixture_prediction_dispatches() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.fixture_prediction_retired(uuid,uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_fixture_prediction_ledgers(uuid,uuid) OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA yoao_jobs_v1 FROM :"jobs_owner";
GRANT SELECT (id) ON public.raster_mask_objects TO :"jobs_owner";
GRANT SELECT (job_id,ordinal,raw_complete,prepared_complete)
    ON public.batch_result_bundles TO :"jobs_owner";
GRANT SELECT, UPDATE (job_id) ON public.prediction_retry_attempts TO :"jobs_owner";
GRANT SELECT (id,project_id), UPDATE (id) ON public.failed_predictions TO :"jobs_owner";
GRANT SELECT (id,project_id,dataset_item_id,file_path,file_type)
    ON yoao_labeling_v1.task_rows TO :"assets_owner", :"jobs_owner", :"inference_owner";
GRANT SELECT (thumbnail_output_id) ON yoao_labeling_v1.task_rows TO :"assets_owner";
GRANT SELECT (thumbnail_output_id) ON public.dataset_source_previews TO :"assets_owner";
GRANT CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_operator_source_receipt() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.require_fixture_operator_manifest() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_auxiliary_intent() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_prediction_inputs_retired(uuid,uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_task_dataset_scope(uuid,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.cross_frame_born_dataset_scope(uuid,uuid,uuid,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.retry_task_dataset_scope(uuid,uuid,uuid,text,text,uuid,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.retry_authority_binding_matches(uuid,uuid,uuid,text,text,text,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fence_cross_frame_job_dataset_scope(uuid,uuid,uuid,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._cross_frame_dataset_scope(uuid,uuid,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._cross_frame_scope_uuid(jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._cross_frame_scope_int(jsonb) OWNER TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.cross_frame_born_dataset_scope(uuid,uuid,uuid,jsonb),
    yoao_assets_v1.retry_authority_binding_matches(uuid,uuid,uuid,text,text,text,text,jsonb),
    yoao_assets_v1.retry_task_dataset_scope(uuid,uuid,uuid,text,text,uuid,jsonb)
    TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.fixture_task_dataset_scope(uuid,jsonb)
    TO :"jobs_owner";
ALTER FUNCTION yoao_assets_v1.fixture_original_readers_empty(uuid) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.fixture_prediction_inputs_retired(uuid,uuid[])
    TO :"jobs_owner", :"inference_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.fixture_original_readers_empty(uuid)
    TO :"migration_role";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.prediction_mask_object_retained(uuid)
    TO :"labeling_owner";

GRANT USAGE ON SCHEMA yoao_fixture_v1 TO :"inference_owner";
GRANT SELECT ON yoao_fixture_v1.fixture_runs, yoao_fixture_v1.fixture_run_resources
    TO :"inference_owner";
GRANT UPDATE (id) ON yoao_fixture_v1.fixture_runs TO :"inference_owner";
GRANT USAGE ON SCHEMA yoao_jobs_v1, yoao_labeling_v1, yoao_assets_v1 TO :"inference_owner";
GRANT EXECUTE ON FUNCTION
    yoao_jobs_v1._batch_result_scope_valid(jsonb,boolean),
    yoao_jobs_v1._lock_batch_result_scope(jsonb,boolean,boolean),
    yoao_jobs_v1.prediction_fixture_open(uuid)
    TO :"inference_owner";
GRANT SELECT, UPDATE (id) ON public.async_jobs TO :"inference_owner";
GRANT SELECT, UPDATE (job_id) ON public.batch_result_executions TO :"inference_owner";
GRANT CREATE ON SCHEMA yoao_inference_routing_v1 TO :"inference_owner";
ALTER TABLE yoao_inference_routing_v1.prediction_execution_scopes OWNER TO :"inference_owner";
ALTER TABLE yoao_inference_routing_v1.fixture_prediction_test_scopes OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1.prediction_scope_reserve(text) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1.prediction_test_scope_reserve(text) OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1.guard_prediction_scope_insert() OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1.guard_prediction_dispatch_start() OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1.guard_prediction_managed_attempt() OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1.fixture_prediction_scope_inventory(uuid,uuid[],uuid[])
    OWNER TO :"inference_owner";
ALTER FUNCTION yoao_inference_routing_v1.fixture_prediction_tests_retired(uuid,uuid[],uuid[])
    OWNER TO :"inference_owner";
REVOKE CREATE ON SCHEMA yoao_inference_routing_v1 FROM :"inference_owner";
GRANT SELECT (scope_id,job_id,actor_id,project_id,request_digest,generation,uses_gpu)
    ON yoao_inference_routing_v1.prediction_execution_scopes TO :"jobs_owner";
GRANT SELECT (scope_id,phase) ON yoao_inference_routing_v1.scope_retirements TO :"jobs_owner";
GRANT SELECT (id,project_id,operation) ON yoao_inference_routing_v1.scopes TO :"jobs_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_fixture_v1 TO :"export_owner";
GRANT CREATE ON SCHEMA public TO :"export_owner";
ALTER TABLE yoao_fixture_v1.fixture_native_exports OWNER TO :"export_owner";
ALTER TABLE yoao_fixture_v1.fixture_native_export_outputs OWNER TO :"export_owner";
ALTER TABLE public.fixture_export_zip_retirements OWNER TO :"export_owner";
ALTER TABLE yoao_fixture_v1.fixture_export_zip_versions OWNER TO :"export_owner";
ALTER TABLE public.image_export_scopes OWNER TO :"export_owner";
ALTER FUNCTION public.image_export_snapshot_datasets(jsonb) OWNER TO :"export_owner";
ALTER FUNCTION public.prepare_image_export_scope(uuid,uuid,uuid,uuid,uuid[]) OWNER TO :"export_owner";
ALTER FUNCTION public.image_export_scope_matches(uuid,uuid) OWNER TO :"export_owner";
ALTER FUNCTION public.guard_image_export_scope() OWNER TO :"export_owner";
ALTER FUNCTION public.guard_image_export_scope_transition() OWNER TO :"export_owner";
ALTER FUNCTION public.guard_image_export_job_birth() OWNER TO :"export_owner";
ALTER FUNCTION public.guard_image_export_fixture_dataset_birth() OWNER TO :"export_owner";
ALTER FUNCTION public.check_image_export_scope_committed() OWNER TO :"export_owner";
ALTER FUNCTION public.guard_fixture_export_retention() OWNER TO :"export_owner";
ALTER FUNCTION public.guard_fixture_export_zip_state() OWNER TO :"export_owner";
ALTER FUNCTION public.record_fixture_export_zip_creation() OWNER TO :"export_owner";
ALTER FUNCTION public.guard_fixture_export_zip_receipt() OWNER TO :"export_owner";
ALTER FUNCTION public.fixture_export_retired(uuid,uuid,uuid) OWNER TO :"export_owner";
ALTER FUNCTION public.guard_fixture_export_execution() OWNER TO :"export_owner";
ALTER FUNCTION public.record_fixture_export_execution() OWNER TO :"export_owner";
ALTER FUNCTION public.guard_fixture_export_job_identity() OWNER TO :"export_owner";
ALTER FUNCTION public.fixture_export_session_owns_job(uuid) OWNER TO :"export_owner";
ALTER FUNCTION public.retire_fixture_export_execution(uuid,uuid,uuid) OWNER TO :"export_owner";
ALTER FUNCTION public.fixture_export_execution_retired(uuid,uuid,uuid) OWNER TO :"export_owner";
ALTER FUNCTION public.guard_fixture_native_export_evidence() OWNER TO :"export_owner";
ALTER FUNCTION public.capture_fixture_native_export() OWNER TO :"export_owner";
ALTER FUNCTION public.fixture_native_export_lifetime(uuid,uuid) OWNER TO :"export_owner";
ALTER FUNCTION public.capture_fixture_native_export_output() OWNER TO :"export_owner";
ALTER FUNCTION public.fixture_native_export_cleanup_eligible(uuid) OWNER TO :"export_owner";
ALTER FUNCTION public.retire_fixture_native_export_output(uuid) OWNER TO :"export_owner";
ALTER FUNCTION public.fixture_export_owner_session() OWNER TO :"export_owner";
ALTER FUNCTION public.guard_fixture_export_private_retirement() OWNER TO :"export_owner";
ALTER FUNCTION public.guard_export_zip_version_proof() OWNER TO :"export_owner";
ALTER FUNCTION public.capture_fixture_export_zip_version() OWNER TO :"export_owner";
ALTER FUNCTION public.guard_fixture_export_zip_version() OWNER TO :"export_owner";
ALTER FUNCTION public.confirm_fixture_export_zip_absent(uuid) OWNER TO :"export_owner";
ALTER FUNCTION public.guard_fixture_zip_retirement_proof() OWNER TO :"export_owner";
REVOKE CREATE ON SCHEMA public, yoao_fixture_v1 FROM :"export_owner";
GRANT SELECT ON yoao_fixture_v1.fixture_runs, yoao_fixture_v1.fixture_run_resources,
    yoao_fixture_v1.fixture_original_targets TO :"export_owner";
GRANT UPDATE (id) ON yoao_fixture_v1.fixture_runs,
    yoao_fixture_v1.fixture_original_targets TO :"export_owner";
GRANT UPDATE (run_id) ON yoao_fixture_v1.fixture_run_resources TO :"export_owner";
GRANT SELECT (id,is_active), UPDATE (id) ON public.users TO :"export_owner";
GRANT SELECT (id,data_type) ON public.projects TO :"export_owner";
GRANT SELECT (id,user_id,project_id,kind,status) ON public.async_jobs TO :"export_owner";
GRANT SELECT (payload) ON public.async_jobs TO :"export_owner";
GRANT SELECT ON public.image_export_scopes TO :"jobs_owner";
GRANT SELECT, UPDATE (id) ON public.export_media_bundles TO :"export_owner";
GRANT SELECT ON public.export_outputs TO :"export_owner";
GRANT SELECT (purpose) ON public.storage_operations TO :"export_owner";
GRANT EXECUTE ON FUNCTION
    public.fixture_export_retired(uuid,uuid,uuid),
    public.fixture_export_execution_retired(uuid,uuid,uuid)
    TO :"jobs_owner";
GRANT SELECT (job_id, run_id, project_id, actor_id, kind, started_at, completed_at, result)
    ON yoao_jobs_v1.fixture_annotation_imports TO :"jobs_owner";
GRANT CREATE ON SCHEMA yoao_jobs_v1, yoao_fixture_v1 TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.fixture_export_dispatch_receipts OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.fixture_real_job_retired(uuid,uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.preserve_fixture_export_dispatch() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_fixture_v1.guard_jobs_anchor() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_fixture_v1.guard_dataset_job_anchor() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_fixture_v1.guard_presentation_job_update() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_fixture_v1.guard_presentation_job_dispatch() OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA yoao_jobs_v1, yoao_fixture_v1 FROM :"jobs_owner";
GRANT EXECUTE ON FUNCTION
    yoao_inference_routing_v1.fixture_prediction_tests_retired(uuid,uuid[],uuid[])
    TO :"jobs_owner";
GRANT CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
ALTER TABLE yoao_assets_v1.import_original_verifications OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_import_original_verification() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._import_verification_scope(jsonb,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.reserve_import_original_read(jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.cancel_unstarted_import_original_read(jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_import_original_verification(jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_import_original_read(jsonb,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_import_original_verification(jsonb,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.recover_import_original_verification(jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.status_import_original_verification(jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.resume_import_original_verification(jsonb) OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_media_births OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_native_media_jobs OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_native_media_job_evidence() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.capture_fixture_native_media_job() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_native_admission_current(text,uuid,uuid) OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_native_media_admissions OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_native_media_admission() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_native_source_lock_held(integer,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_fixture_native_source_admission(text,uuid,bigint,uuid,integer,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_fixture_native_source_admission(uuid) OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_native_media_executions OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_native_media_claims OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_native_media_execution() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_native_media_claim() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_native_execution_lock_held(integer,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_fixture_native_execution(text,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_fixture_native_claim(uuid,integer,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_native_claim_allowed(text,uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.consume_fixture_native_claim(text,uuid,bigint,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_fixture_native_claim(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_fixture_native_execution(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_native_job_candidates(text) OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_native_scan_admissions OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_native_scan_admission() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_fixture_native_scan(uuid,uuid,bigint,uuid,integer,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_fixture_native_scan(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_native_scan_visible(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_legacy_task_scan_visible(uuid) OWNER TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.fixture_legacy_task_scan_visible(uuid) TO :"labeling_owner";
ALTER TABLE yoao_assets_v1.fixture_native_media_sources OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_native_cache_targets OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_native_cache_writes OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_native_media_source() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.capture_fixture_native_media_source(text,uuid,bigint,uuid,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.settle_fixture_native_media_source() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_native_cache_target() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.register_fixture_native_cache_target(uuid,text,text,text,text,text,uuid,uuid,bigint) OWNER TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.register_fixture_native_cache_target(uuid,text,text,text,text,text,uuid,uuid,bigint)
    TO :"migration_role";
ALTER FUNCTION yoao_assets_v1.guard_fixture_native_cache_write() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_fixture_native_cache_write(text,uuid,bigint,uuid,uuid,uuid,text,text,text,text,text,bigint,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.advance_fixture_native_cache_write(uuid,uuid,text,text,text) OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_native_read_principals OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_native_cache_reads OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_native_read_principal() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.register_fixture_native_reader(uuid,name,text) OWNER TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.register_fixture_native_reader(uuid,name,text) TO :"migration_role";
ALTER FUNCTION yoao_assets_v1.guard_fixture_native_cache_read() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_native_read_lock_held(integer,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_fixture_native_output_read(uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_fixture_native_output_read(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.retire_fixture_native_reader(uuid) OWNER TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.retire_fixture_native_reader(uuid) TO :"migration_role";
ALTER FUNCTION yoao_assets_v1.fixture_native_reader_identity(uuid) OWNER TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.fixture_native_reader_identity(uuid) TO :"migration_role";
ALTER TABLE yoao_assets_v1.fixture_native_resolver_principals OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_native_resolver_principal() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.register_fixture_native_resolver(uuid,name) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.resolve_fixture_native_cache(text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.retire_fixture_native_resolver(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_native_resolver_identity(uuid) OWNER TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.fixture_native_resolver_identity(uuid) TO :"migration_role";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.register_fixture_native_resolver(uuid,name),
    yoao_assets_v1.retire_fixture_native_resolver(uuid)
    TO :"migration_role";
ALTER TABLE yoao_assets_v1.fixture_native_cache_retirements OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_native_scope_retirements OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_native_scope_retirement() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_native_cache_retirement() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_native_retirement_ready(uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_native_retirement_scopes(uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_native_retirement_inventory(uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.prepare_fixture_native_scope(uuid,uuid,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_fixture_native_scope(uuid,uuid,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.prepare_fixture_native_retirement(uuid,uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.advance_fixture_native_retirement(uuid,uuid,uuid,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_native_outputs_retired(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_native_cache_scope_owned(text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_media_birth_evidence() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_fixture_media_birth() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_media_reference() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_pyramid_generation_owned(uuid,uuid,uuid,uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_pyramid_attempt_matches(uuid,uuid,uuid,uuid,integer,uuid,uuid) OWNER TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.fixture_pyramid_attempt_matches(uuid,uuid,uuid,uuid,integer,uuid,uuid)
    TO :"jobs_owner";
ALTER FUNCTION yoao_assets_v1.fixture_media_child_source_matches(uuid,uuid,uuid,timestamptz,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_media_owner_exists(text,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_media_item_in_cutoff(uuid,uuid,uuid,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_video_attempt_scope(uuid,uuid) OWNER TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.fixture_video_attempt_scope(uuid,uuid) TO :"jobs_owner";
ALTER FUNCTION yoao_assets_v1.fixture_media_ordinary_gc(text,uuid,integer) OWNER TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.fixture_media_item_in_cutoff(uuid,uuid,uuid,timestamptz)
    TO :"jobs_owner";
ALTER TABLE yoao_assets_v1.media_native_stages OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_media_native_stage_tombstone() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.track_media_native_stage() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_media_output_permit() OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_media_versions OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_media_version() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_fixture_media_version(text,uuid,uuid,text,uuid,text,text,text,text,bigint,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_fixture_media_version(text,uuid,uuid,uuid,text,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.verify_fixture_media_version(text,uuid,uuid,uuid,text) OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_media_retirement_permits OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_media_version_retirements OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_media_retirement_permit() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.acquire_fixture_media_retirement_permit(uuid,uuid,uuid,text,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.close_fixture_media_retirement_permit() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_media_retirement_session_clean(uuid,uuid,uuid) OWNER TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.fixture_media_retirement_session_clean(uuid,uuid,uuid)
    TO :"jobs_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_media_version_retirement() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.prepare_fixture_media_version_retirement(uuid,uuid,uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.advance_fixture_media_version_retirement(uuid,uuid,uuid,uuid,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_media_retirement_inventory(uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_media_output_inventory(uuid,uuid,uuid,text,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_media_output_retirement_done(uuid,uuid,uuid,text,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_dataset_media_retired(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_media_retirement_complete(uuid,uuid,uuid) OWNER TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.fixture_dataset_media_retired(uuid,uuid)
    TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.fixture_media_child_source_matches(uuid,uuid,uuid,timestamptz,jsonb)
    TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.fixture_pyramid_generation_owned(uuid,uuid,uuid,uuid,integer)
    TO :"jobs_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
GRANT CREATE ON SCHEMA yoao_jobs_v1 TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.fixture_media_backfills OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.fixture_media_dispatches OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.fixture_media_executor_principals OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.fixture_media_principal_matches(uuid) OWNER TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.fixture_media_principal_matches(uuid) TO :"assets_owner";
ALTER TABLE yoao_jobs_v1.fixture_media_children OWNER TO :"jobs_owner";
ALTER TABLE yoao_jobs_v1.fixture_media_pyramid_attempts OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_fixture_media_pyramid_attempt() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.record_fixture_pyramid_attempt(uuid,uuid,uuid,integer,uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.fixture_pyramid_attempts_retired(uuid,uuid,uuid,integer,integer) OWNER TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.fixture_pyramid_attempts_retired(uuid,uuid,uuid,integer,integer)
    TO :"assets_owner";
ALTER TABLE yoao_jobs_v1.fixture_media_video_attempts OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_fixture_media_video_attempt() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.record_fixture_video_attempt(uuid,uuid,uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.fixture_video_attempt_retired(uuid,uuid,uuid,uuid) OWNER TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.fixture_video_attempt_retired(uuid,uuid,uuid,uuid)
    TO :"assets_owner";
ALTER TABLE yoao_jobs_v1.fixture_media_retirement_sessions OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_fixture_media_retirement_session() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.begin_fixture_media_retirement(uuid,uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.fixture_media_retirement_authorized(uuid,uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.finish_fixture_media_retirement(uuid,uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.fixture_media_retirement_current(uuid,uuid) OWNER TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.fixture_media_retirement_authorized(uuid,uuid,uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.fixture_media_retirement_current(uuid,uuid)
    TO :"assets_owner";
ALTER FUNCTION yoao_jobs_v1.fixture_media_backfill_retired(uuid,uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.delete_fixture_media_backfills(uuid,uuid) OWNER TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.delete_fixture_media_backfills(uuid,uuid)
    TO :"migration_role";
ALTER FUNCTION yoao_jobs_v1.retire_fixture_media_executor(uuid) OWNER TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.retire_fixture_media_executor(uuid) TO :"migration_role";
ALTER FUNCTION yoao_jobs_v1.fixture_media_executor_identity(uuid) OWNER TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.fixture_media_executor_identity(uuid) TO :"migration_role";
ALTER FUNCTION yoao_jobs_v1.guard_fixture_media_backfill_evidence() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.capture_fixture_media_backfill() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.fixture_media_backfill_route(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_fixture_media_backfill_job() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_fixture_media_job_reuse() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_fixture_media_dispatch_evidence() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_fixture_media_dispatch() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.capture_fixture_media_dispatch() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_fixture_media_executor_principal() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.register_fixture_media_executor(uuid,name) OWNER TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.register_fixture_media_executor(uuid,name) TO :"migration_role";
ALTER FUNCTION yoao_jobs_v1.fixture_media_session_owns(text,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.fixture_media_session_authorized(text,uuid,uuid,uuid,uuid,uuid) OWNER TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.fixture_media_session_authorized(text,uuid,uuid,uuid,uuid,uuid)
    TO :"assets_owner";
ALTER FUNCTION yoao_jobs_v1.begin_fixture_media_backfill(uuid,text,text,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.finish_fixture_media_backfill(uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.guard_fixture_media_child_evidence() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.prepare_fixture_media_child(uuid,uuid,uuid,uuid,jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.begin_fixture_media_child(uuid,text,uuid,jsonb,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.bind_fixture_media_child_generation(uuid,uuid,uuid,integer) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.finish_fixture_media_child(uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.fixture_media_work_exists(text,text) OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA yoao_jobs_v1 FROM :"jobs_owner";
GRANT EXECUTE ON FUNCTION
    yoao_jobs_v1.job_publications_retired(uuid,text[])
    TO :"export_owner";
GRANT EXECUTE ON FUNCTION
    yoao_jobs_v1.job_publications_retired(uuid,text[]),
    yoao_jobs_v1.job_publications_not_sent(uuid,text[]),
    yoao_jobs_v1.fixture_media_delivery(uuid),
    yoao_jobs_v1.fixture_media_publication_accepted(uuid,uuid,text),
    yoao_jobs_v1.fixture_media_publication_retirement_outcome(uuid,uuid,text)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.media_producer_binding(text,uuid,text,uuid,jsonb),
    yoao_assets_v1.fixture_independent_source_matches(uuid,uuid,uuid,text,uuid,text,jsonb,jsonb),
    yoao_assets_v1.prepare_fixture_independent_execution(uuid,uuid,uuid,text,uuid,text,uuid,text,jsonb,jsonb)
    TO :"jobs_owner";

GRANT CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_independent_media_births OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_external_media_jobs OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_external_media_sources OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_native_pyramid_generations OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_independent_media_executions OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_independent_media_children OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_legacy_birth_admissions OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_screenshot_producer_admissions OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_screenshot_producer_candidates OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_independent_pyramid_bindings OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_independent_pyramid_attempts OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_independent_video_attempts OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_independent_no_start OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_native_job_retirements OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_video_output_anchors OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_run_no_media_birth_certificates OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_native_target_retirements OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_native_target_physical_acknowledgements OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.fixture_external_pyramid_reuses OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_fixture_external_pyramid_reuse(text,uuid,uuid,uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_external_image_attempt_sources_proven(uuid,uuid,uuid,uuid,bigint,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_original_read_permit() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_dataset_legacy_original_readers_retired(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_task_legacy_original_readers_retired(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.notify_fixture_legacy_original_reader() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_independent_source_matches(uuid,uuid,uuid,text,uuid,text,jsonb,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.prepare_fixture_independent_execution(uuid,uuid,uuid,text,uuid,text,uuid,text,jsonb,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_native_target_location() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_native_cache_target_binding(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_native_target_original() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_native_target_binding() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_native_target_output() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_native_target_connection_alias() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_native_target_references_retired(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_native_producer_ready(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_native_reader_ready(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_native_pyramid_evidence() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.capture_fixture_native_pyramid() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_native_pyramid_generation_retired(uuid,uuid,uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_independent_execution() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_independent_session_owns(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_independent_delivery_envelope(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_fixture_independent_execution(uuid,uuid,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_independent_session_authorized(uuid,uuid,uuid,uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_fixture_independent_execution(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_legacy_execution_target(text,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.bind_fixture_independent_child(uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_legacy_birth_admission() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_fixture_legacy_birth_admission(text,uuid,uuid,uuid,uuid,uuid,uuid,integer,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_fixture_legacy_birth_admission(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_legacy_birth_admission_current(uuid,uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_screenshot_producer_admission() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_fixture_screenshot_producer(uuid,uuid,uuid,uuid,uuid,integer,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_screenshot_producer_current(uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_screenshot_producer_target(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_fixture_screenshot_producer(uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.reuse_fixture_screenshot_producer(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.bind_fixture_independent_pyramid(uuid,uuid,uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_fixture_independent_pyramid_attempt(uuid,uuid,uuid,integer,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_independent_pyramid_bindings_retired(uuid,uuid,uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_independent_pyramid_generation_retired(uuid,uuid,uuid,integer,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_screenshot_output_receipt(uuid,uuid,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_independent_delivery_matches(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.retire_fixture_independent_publications(uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_independent_deliveries_retired(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_native_job_retirement() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_native_job_inventory(uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.retire_fixture_native_job(uuid,uuid,uuid,text,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.guard_fixture_native_target_gate() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_native_target_receipt(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.retire_fixture_native_target(uuid,text,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.acknowledge_fixture_native_target_physical_retirement(text,text) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
GRANT CREATE ON SCHEMA yoao_jobs_v1 TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.fixture_pyramid_children_retired(uuid,uuid,uuid,integer) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.fixture_pyramid_settled_attempt_count(uuid,uuid,uuid,integer) OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA yoao_jobs_v1 FROM :"jobs_owner";

GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.fixture_native_cache_target_binding(uuid),
    yoao_assets_v1.fixture_screenshot_output_receipt(uuid,uuid,uuid,text),
    yoao_assets_v1.seal_fixture_run_without_media(uuid),
    yoao_assets_v1.retire_fixture_video_output_anchor(uuid,uuid,uuid),
    yoao_assets_v1.fixture_video_output_disposition(uuid,uuid,uuid),
    yoao_assets_v1.acknowledge_fixture_native_target_physical_retirement(text,text)
    TO :"migration_role";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.record_fixture_external_pyramid_reuse(text,uuid,uuid,uuid,integer)
    TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.fixture_external_image_attempt_sources_proven(uuid,uuid,uuid,uuid,bigint,uuid)
    TO :"migration_role";
GRANT EXECUTE ON FUNCTION
    yoao_jobs_v1.fixture_media_execution_run(text,uuid,uuid),
    yoao_jobs_v1.fixture_media_retirement_started(uuid,uuid),
    yoao_jobs_v1.fixture_media_run_retired(uuid),
    yoao_jobs_v1.fixture_pyramid_children_retired(uuid,uuid,uuid,integer),
    yoao_jobs_v1.fixture_pyramid_settled_attempt_count(uuid,uuid,uuid,integer),
    yoao_jobs_v1.retire_fixture_media_publication(uuid,uuid,text)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.fixture_dataset_media_producers_quiescent(uuid,uuid)
    TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION
    yoao_labeling_v1.lock_task_for_core_read(uuid,text,boolean,uuid),
    yoao_labeling_v1.lock_task_dataset_item_links_for_core_read(uuid,boolean,uuid,integer)
    TO :"assets_owner";
GRANT SELECT (family,run_id,admission_id,operation_id,dataset_id,state,job_id,generation)
    ON yoao_fixture_v1.fixture_native_media_work_intents TO :"assets_owner";
GRANT SELECT (task_id,dataset_item_id,role)
    ON yoao_labeling_v1.task_dataset_item_link_rows TO :"assets_owner";
GRANT SELECT (run_id) ON yoao_jobs_v1.fixture_media_backfills,
    yoao_jobs_v1.fixture_media_children TO :"assets_owner";
GRANT SELECT (child_id,asset_id,generation,dataset_id,item_id,delegation)
    ON yoao_jobs_v1.fixture_media_children TO :"assets_owner";
GRANT SELECT (run_id,state) ON yoao_jobs_v1.fixture_media_executor_principals TO :"assets_owner";
GRANT UPDATE (id) ON yoao_fixture_v1.fixture_run_admissions TO :"assets_owner";
GRANT UPDATE (run_id) ON yoao_fixture_v1.fixture_run_resources TO :"jobs_owner";
GRANT DELETE ON public.asset_image_jobs, public.asset_media_jobs TO :"assets_owner";
GRANT SELECT, DELETE, UPDATE (state) ON public.video_derivative_outputs TO :"assets_owner";
GRANT UPDATE (video_output_id,video_building_output_id) ON public.dataset_items TO :"assets_owner";
GRANT SELECT (retired_at), UPDATE (state,retired_at)
    ON public.thumbnail_outputs TO :"assets_owner";
GRANT UPDATE (state, inventory, updated_at)
    ON public.image_pyramid_outputs TO :"assets_owner";
GRANT SELECT (original_id) ON yoao_labeling_v1.task_rows TO :"assets_owner";
DO $fixture_owner$
BEGIN
    IF NOT COALESCE((
        SELECT pg_catalog.pg_has_role(SESSION_USER, relation.relowner, 'MEMBER')
        FROM pg_catalog.pg_class AS relation
        WHERE relation.oid=pg_catalog.to_regclass('public.fixture_export_zip_retirements')
          AND relation.relowner=current_setting('yoao.export_owner')::pg_catalog.regrole
    ), false) THEN
        RAISE EXCEPTION 'Migration session is not a member of the actual fixture owner';
    END IF;
END;
$fixture_owner$;
GRANT CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
ALTER TABLE yoao_assets_v1.storage_fault_observation_key OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._storage_fault_seal(bigint,uuid,uuid,text,text,jsonb,jsonb)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._storage_fault_location_visible(uuid,text,uuid,boolean)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._storage_fault_job_visible(uuid,text,uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.reserve_storage_fault_observation(uuid,uuid,text,text,jsonb,jsonb)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_storage_fault_observation(bigint,bytea,uuid,uuid,text,text,jsonb,jsonb,text)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_storage_fault_facts(uuid,uuid,text,boolean,boolean)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.storage_fault_location_visible(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.storage_fault_page(uuid,uuid,text,integer,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_storage_observation_rate(uuid,uuid,text,boolean)
    OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_storage_observation_rates(uuid,uuid) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
GRANT CREATE ON SCHEMA yoao_jobs_v1 TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.read_storage_fault_job(uuid) OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA yoao_jobs_v1 FROM :"jobs_owner";
GRANT USAGE ON SCHEMA yoao_jobs_v1 TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.read_storage_fault_job(uuid) TO :"assets_owner";
REVOKE ALL ON TABLE yoao_assets_v1.storage_fault_observation_key FROM PUBLIC;
REVOKE ALL ON SEQUENCE public.storage_fault_observation_seq
    FROM PUBLIC, :"workflow_role", :"asset_cleanup_role", :"mask_gc_role";

GRANT USAGE ON SCHEMA yoao_settings_v1 TO :"settings_owner";
REVOKE UPDATE ON TABLE public.system_settings FROM :"settings_owner";
GRANT SELECT (key, value_type, value_json, updated_at, updated_by, runtime_revision),
    INSERT (key, value_type, value_json, updated_at, updated_by),
    UPDATE (value_type, value_json, updated_at, updated_by), DELETE
    ON public.system_settings TO :"settings_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_settings_v1 TO :"settings_owner";
ALTER TABLE yoao_settings_v1.smtp_test_attempts OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1._rekey_override() OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1._setting_type(text) OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1._override_stamp() OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1._super_admin(uuid,boolean) OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1.runtime_knob(text) OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1.connector_override() OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1.import_limits() OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1.management_snapshot(uuid,boolean) OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1.apply_settings_edit(uuid,text,jsonb,text[],text)
    OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1.queue_smtp_test(uuid) OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1.read_smtp_test(uuid,uuid) OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1.claim_smtp_test() OWNER TO :"settings_owner";
ALTER FUNCTION yoao_settings_v1.finish_smtp_test(uuid,uuid,text,text,text,integer)
    OWNER TO :"settings_owner";
REVOKE CREATE ON SCHEMA yoao_settings_v1 FROM :"settings_owner";

GRANT USAGE ON SCHEMA yoao_identity_v1 TO :"settings_owner", :"diagnostics_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.read_actor_facts(uuid),
    yoao_identity_v1.lock_accounts_share(uuid[],boolean) TO :"settings_owner", :"diagnostics_owner";
GRANT SELECT (id, name, role) ON yoao_identity_v1.account_profiles TO :"diagnostics_owner";

GRANT USAGE ON SCHEMA public TO :"diagnostics_owner", :"settings_owner";
GRANT SELECT, DELETE ON public.bug_reports, public.bug_comments, public.bug_attachment_uploads
    TO :"diagnostics_owner";
GRANT INSERT (id, display_id, reporter_id, user_role, route, project_id, task_id, title, description,
    severity, status, browser_ua, viewport, recent_api_calls, recent_console_errors, screenshot_url, attachments),
    UPDATE (project_id, task_id, title, description, severity, status, duplicate_of_id, assigned_to_id,
        resolution, fixed_in_version, triaged_at, fixed_at, reopen_count, last_reopened_at)
    ON public.bug_reports TO :"diagnostics_owner";
GRANT INSERT (id, bug_report_id, author_id, body) ON public.bug_comments TO :"diagnostics_owner";
GRANT INSERT (id, owner_id, sha256, size_bytes, mime_type, file_name, created_at, expires_at, storage_ref, state),
    UPDATE (bound_report_id, object_version_id, state, last_attempted_at, retired_at)
    ON public.bug_attachment_uploads TO :"diagnostics_owner";
GRANT USAGE ON SEQUENCE public.display_seq_bug_reports TO :"diagnostics_owner";
GRANT SELECT (purpose) ON public.storage_operations TO :"diagnostics_owner";
GRANT USAGE ON SCHEMA yoao_assets_v1, yoao_labeling_v1 TO :"diagnostics_owner";
GRANT SELECT (id, project_id, dataset_item_id) ON yoao_labeling_v1.task_rows TO :"diagnostics_owner";
GRANT SELECT (project_id, item_id) ON yoao_assets_v1.project_dataset_item_sources TO :"diagnostics_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_diagnostics_v1 TO :"diagnostics_owner";
DO $ownership$
DECLARE
    v_signature text;
    v_function regprocedure;
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_diagnostics_v1._actor_role(uuid,boolean)',
        'yoao_diagnostics_v1._bug_projection(public.bug_reports)',
        'yoao_diagnostics_v1._comment_projection(public.bug_comments,text,text)',
        'yoao_diagnostics_v1._bug_attachment_key(uuid,uuid,text)',
        'yoao_diagnostics_v1._bug_attachment_public(public.bug_attachment_uploads)',
        'yoao_diagnostics_v1._bug_attachment_claim(jsonb)',
        'yoao_diagnostics_v1._bug_attachment_id(text)',
        'yoao_diagnostics_v1._bug_attachment_receipt(public.bug_attachment_uploads)',
        'yoao_diagnostics_v1._comment_context(public.bug_reports)',
        'yoao_diagnostics_v1._bug_attachment_cleanup_blocked(public.bug_attachment_uploads)',
        'yoao_diagnostics_v1.create_bug_report(uuid,jsonb)',
        'yoao_diagnostics_v1.confirm_bug_attachment_binding(uuid,uuid)',
        'yoao_diagnostics_v1.list_bug_reports(uuid,boolean,text,text,text,integer,integer)',
        'yoao_diagnostics_v1.read_bug_report(uuid,uuid)',
        'yoao_diagnostics_v1.patch_bug_report(uuid,uuid,jsonb)',
        'yoao_diagnostics_v1.prepare_bug_comment(uuid,uuid)',
        'yoao_diagnostics_v1.add_bug_comment(uuid,uuid,text)',
        'yoao_diagnostics_v1.bug_cluster_candidates(uuid,uuid)',
        'yoao_diagnostics_v1.bug_report_markdown_comments(uuid,uuid[])',
        'yoao_diagnostics_v1.delete_bug_report(uuid,uuid)',
        'yoao_diagnostics_v1.detach_orphan_bug_tasks(uuid,uuid[])',
        'yoao_diagnostics_v1.reserve_bug_attachment(uuid,uuid,text,integer,text,text,timestamptz,timestamptz,jsonb)',
        'yoao_diagnostics_v1.lock_pending_bug_attachment(uuid,uuid)',
        'yoao_diagnostics_v1.record_bug_attachment_version(uuid,uuid,jsonb,text)',
        'yoao_diagnostics_v1.publish_bug_attachment(uuid,uuid,jsonb,text)',
        'yoao_diagnostics_v1.lock_report_attachment(uuid,uuid,text)',
        'yoao_diagnostics_v1.bug_attachment_cleanup_candidates(integer)',
        'yoao_diagnostics_v1.prepare_bug_attachment_cleanup(uuid)',
        'yoao_diagnostics_v1.lock_retired_bug_attachment(uuid)',
        'yoao_diagnostics_v1.complete_bug_attachment_cleanup(uuid,jsonb,text)',
        'yoao_diagnostics_v1.bug_attachment_history(uuid)',
        'yoao_diagnostics_v1.bug_attachment_location_referenced(uuid)',
        'yoao_diagnostics_v1.bug_attachment_maintenance_facts()'
    ] LOOP
        v_function := v_signature::regprocedure;
        EXECUTE pg_catalog.format('ALTER FUNCTION %s OWNER TO %I', v_function,
                                  pg_catalog.current_setting('yoao.diagnostics_owner'));
    END LOOP;
END
$ownership$;
REVOKE CREATE ON SCHEMA yoao_diagnostics_v1 FROM :"diagnostics_owner";
GRANT USAGE ON SCHEMA yoao_diagnostics_v1 TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_diagnostics_v1.bug_attachment_location_referenced(uuid),
    yoao_diagnostics_v1.bug_attachment_maintenance_facts(),
    yoao_diagnostics_v1.lock_pending_bug_attachment(uuid,uuid),
    yoao_diagnostics_v1.lock_report_attachment(uuid,uuid,text),
    yoao_diagnostics_v1.lock_retired_bug_attachment(uuid)
    TO :"assets_owner";
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
GRANT USAGE ON SCHEMA yoao_learning_v1 TO :"learning_owner";
GRANT SELECT, INSERT, UPDATE, DELETE ON public.training_runs, public.training_previews
TO :"learning_owner";
GRANT SELECT, INSERT, UPDATE ON public.training_attempts, public.training_resource_leases,
    public.training_preview_resource_leases TO :"learning_owner";
GRANT SELECT, INSERT ON public.training_events TO :"learning_owner";
GRANT SELECT, INSERT, DELETE ON public.training_telemetry_samples, public.training_log_chunks
TO :"learning_owner";
GRANT CREATE ON SCHEMA yoao_learning_v1 TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_get_run(uuid, uuid, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_get_attempt(uuid, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_get_preview(uuid, uuid, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_executor_run(uuid, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_executor_preview(uuid, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_attempt_proof(uuid, text, uuid, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_preview_proof(uuid, text, uuid, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_run_request(uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_gpu_queue() OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_study_children(uuid, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_study_attempts(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_run_attempts(uuid, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_adoption_attempts(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_trigger_run(uuid, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_active_auto_run(uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_trigger_run_ids(uuid, uuid[]) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_run_dependencies(uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_run_has_lease(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_create_run(uuid, uuid, uuid, uuid, uuid, uuid, uuid, uuid, text, text, jsonb, text, jsonb, jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_pin_run_storage(uuid, jsonb, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_enqueue_attempt(uuid, uuid, uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_reserve_study_attempt(uuid, uuid, jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_manual_resume(uuid, uuid, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_delete_run(uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_gpu_lease(text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_lock_gpu_lease(text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_observe_gpu(text, text, uuid, jsonb, timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_cpu_lease(text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_lock_cpu_lease(text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_observe_cpu(text, text, uuid, jsonb, timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_claim_attempt(uuid, uuid, text, text, uuid, uuid, text, jsonb, timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_lose_attempt(uuid, uuid, timestamptz, text, jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_cancel_attempt(uuid, uuid, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_finish_queued_attempt(uuid, uuid, text, timestamptz, text, jsonb, jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_finish_executor_attempt(uuid, uuid, text, uuid, text, boolean, text, timestamptz, text, jsonb, jsonb, text, jsonb, uuid, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_heartbeat_attempt(uuid, uuid, text, uuid, text, timestamptz, text, text, jsonb, jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_attempt_uploads(uuid, uuid, text, uuid, text, jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_record_event(uuid, text, uuid, text, integer, text, jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_event_page(uuid, integer, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_last_epoch(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_set_observation_receipt(uuid, text, uuid, text, jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_telemetry_receipt(uuid, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_append_telemetry(uuid, text, uuid, text, uuid, jsonb)
    OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_trim_telemetry(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_telemetry_earliest(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_telemetry_page(uuid, integer, integer, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_cleanup_telemetry(timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_logs_receipt(uuid, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_append_logs(uuid, text, uuid, text, uuid, jsonb)
    OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_trim_logs(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_logs_earliest(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_logs_page(uuid, integer, integer, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_cleanup_logs(timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_latest_telemetry(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_preview_request(uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_active_preview_count(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_create_preview(uuid, uuid, uuid, uuid, uuid, text, jsonb, text, jsonb, jsonb, text, timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_preview_has_lease(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_pin_preview_storage(uuid, jsonb, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_attach_preview_job(uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_cpu_queue(text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_claim_preview(uuid, text, text, uuid, uuid, text, jsonb, timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_heartbeat_preview(uuid, text, uuid, text, timestamptz, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_preview_uploads(uuid, text, uuid, text, jsonb, timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_finish_queued_preview(uuid, text, timestamptz, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_finish_executor_preview(uuid, text, uuid, text, boolean, text, timestamptz, text, text, jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_cancel_preview(uuid, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_lose_preview(uuid, timestamptz, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_expire_preview(uuid, timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_preview_cleanup_queue(timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_preview_cleanup_expire(uuid, timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_delete_expired_preview(uuid, jsonb, timestamptz) OWNER TO :"learning_owner";
ALTER VIEW yoao_learning_v1._training_run_facts OWNER TO :"learning_owner";
ALTER VIEW yoao_learning_v1._training_attempt_facts OWNER TO :"learning_owner";
ALTER VIEW yoao_learning_v1._training_preview_facts OWNER TO :"learning_owner";
ALTER VIEW yoao_learning_v1.training_run_summary OWNER TO :"learning_owner";
ALTER VIEW yoao_learning_v1.training_run_storage_owner OWNER TO :"learning_owner";
ALTER VIEW yoao_learning_v1.training_preview_storage_owner OWNER TO :"learning_owner";
ALTER VIEW yoao_learning_v1.training_run_lineage OWNER TO :"learning_owner";
ALTER VIEW yoao_learning_v1.training_attempt_lineage OWNER TO :"learning_owner";
REVOKE CREATE ON SCHEMA yoao_learning_v1 FROM :"learning_owner";
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    public.training_dataset_versions, public.training_dataset_analyses,
    public.training_dataset_analyzed_samples, public.training_storage_deletions
    TO :"learning_owner";
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.project_task_datasets TO :"learning_owner";
GRANT SELECT, INSERT ON TABLE public.task_dataset_version_memberships TO :"learning_owner";
GRANT SELECT (id, storage_ref) ON public.training_runs TO :"learning_owner";
GRANT SELECT (id) ON public.training_previews, public.sample_evaluation_runs,
    public.active_learning_rounds TO :"learning_owner";
GRANT USAGE ON SCHEMA yoao_assets_v1, yoao_labeling_v1 TO :"learning_owner";
GRANT SELECT (item_id, dataset_id) ON yoao_assets_v1.spatial_item_references TO :"learning_owner";
GRANT SELECT (id, project_id, dataset_item_id) ON yoao_labeling_v1.task_rows TO :"learning_owner";
GRANT USAGE ON SCHEMA yoao_learning_v1 TO :"assets_owner", :"learning_owner";
GRANT CREATE ON SCHEMA yoao_learning_v1 TO :"learning_owner";
ALTER VIEW yoao_learning_v1.training_dataset_metadata OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_task(jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_facts(uuid,uuid,boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_profile(jsonb,text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_read(uuid,uuid,boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_hold(uuid,uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_request(uuid,uuid,boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_job_lock(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_create(uuid,uuid,uuid,uuid,uuid,text,uuid,text,jsonb,text,jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_stream(uuid,uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_associate(uuid,uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_set_storage(uuid,uuid,uuid,jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_queue(uuid,uuid,uuid,uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_start(uuid,uuid,uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_progress(uuid,uuid,uuid,integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_publish(uuid,uuid,uuid,text,jsonb,jsonb,text,text,bigint,timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_fail(uuid,uuid,uuid,text,boolean,text,text,timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_delete(uuid,uuid,uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_manifest(uuid,uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_sample_evidence(uuid,uuid,uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_original_history() OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_binding_history() OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_asset_versions(uuid,boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_location_versions(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_analysis_read(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_analysis_start(uuid,uuid,text,text,text,jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_analysis_add_members(uuid,text,jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_analysis_sample_page(uuid,text,integer,bigint,text,integer,text,text,text[]) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_analysis_member(uuid,uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_analysis_observations(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_analysis_media_counts(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_analysis_observe_media(uuid,uuid,text,bigint,text,timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_storage_stage_deletion(uuid,uuid,text,jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_storage_claim_deletion(uuid,timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_storage_complete_deletion(uuid,jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_storage_deletion_batch() OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.training_dataset_high_water_violations(uuid) OWNER TO :"learning_owner";
REVOKE CREATE ON SCHEMA yoao_learning_v1 FROM :"learning_owner";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.training_dataset_asset_versions(uuid,boolean) TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.training_original_history() TO :"assets_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_learning_v1 TO :"learning_owner";
GRANT USAGE ON SCHEMA yoao_jobs_v1 TO :"learning_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.frozen_deployment_usage(uuid,uuid,uuid),
    yoao_jobs_v1.read_job(uuid) TO :"learning_owner";
GRANT SELECT (id, project_id) ON public.training_runs TO :"learning_owner";
GRANT UPDATE (id) ON public.training_runs TO :"learning_owner";
GRANT SELECT (id, run_id, status, artifacts) ON public.training_attempts TO :"learning_owner";
GRANT SELECT (id, project_id, status, snapshot) ON public.training_dataset_versions TO :"learning_owner";
GRANT SELECT (evaluation_id) ON public.active_learning_references TO :"learning_owner";
GRANT SELECT, INSERT ON public.trained_model_versions, public.trained_model_deployments,
    public.project_model_releases, public.project_model_selections, public.model_decisions,
    public.sample_evaluation_runs, public.sample_evaluation_attempts, public.sample_evaluation_samples
    TO :"learning_owner";
GRANT UPDATE (id) ON public.trained_model_versions TO :"learning_owner";
GRANT UPDATE (check_id,status,receipt,trial,error_message,checked_at)
    ON public.trained_model_deployments TO :"learning_owner";
GRANT UPDATE (id) ON public.project_model_releases TO :"learning_owner";
GRANT UPDATE (release_id,generation,updated_at) ON public.project_model_selections TO :"learning_owner";
GRANT UPDATE (status,storage_ref,generation,current_attempt_id,async_job_id,error_code,error_message,
              completed_at,deletion_requested_at) ON public.sample_evaluation_runs TO :"learning_owner";
GRANT UPDATE (status,claim_id,report,error_code,error_message,heartbeat_at,dispatched_at,
              maintenance_at,started_at,completed_at) ON public.sample_evaluation_attempts TO :"learning_owner";
GRANT UPDATE (status,request,evidence,error_code,error_message,produced_attempt_id)
    ON public.sample_evaluation_samples TO :"learning_owner";
GRANT DELETE ON public.sample_evaluation_runs TO :"learning_owner";

SELECT set_config('yoao.learning_owner', :'learning_owner', true);
DO $learning_models$
DECLARE
    v_name text;
    v_owner text := current_setting('yoao.learning_owner');
BEGIN
    FOREACH v_name IN ARRAY ARRAY[
        '_model_version_rows', '_model_deployment_rows', '_model_release_rows', '_model_decision_rows',
        'model_version_lineage', 'model_deployment_lineage', 'model_release_lineage',
        '_evaluation_run_rows', '_evaluation_attempt_rows', '_evaluation_sample_rows',
        'sample_evaluation_lineage', 'sample_evaluation_storage_owners'
    ] LOOP
        EXECUTE format('ALTER VIEW yoao_learning_v1.%I OWNER TO %I', v_name, v_owner);
    END LOOP;
    FOREACH v_name IN ARRAY ARRAY[
        'model_version_detail', 'model_registration_replay',
        'register_model_candidate', 'run_model_candidates', 'run_has_model_candidate',
        'catalog_model_candidates', 'model_candidate_visibility', 'model_candidate_metadata',
        '_filtered_model_candidates', 'list_model_candidates', 'count_model_candidates',
        'model_deployments_for_versions', 'model_deployment_detail', 'hold_model_deployment_for_registry',
        'hold_registry_model_deployments', 'count_active_model_releases', 'hold_model_candidate',
        'hold_prediction_model_reference', 'start_model_deployment_check', 'finish_model_deployment_check',
        'advance_model_deployment_retirement', 'model_release_detail', 'model_release_replay',
        'catalog_model_releases', 'project_model_selection', 'list_project_model_releases',
        'count_project_model_releases', 'publish_project_model_release', 'append_model_decision',
        'list_model_decisions', 'trial_mask_keys', 'trial_mask_referenced',
        'pending_dataset_mask_keys', 'pending_dataset_mask_referenced',
        'evaluation_detail', 'evaluation_for_attempt', 'hold_evaluation_for_current_job',
        'evaluation_replay', 'list_project_evaluations', 'count_project_evaluations',
        'dataset_has_sample_evaluation', 'held_out_evaluation_budget', 'held_out_evaluation_specs',
        'evaluation_attempt_detail', 'evaluation_attempt_history', 'evaluation_sample_detail',
        'evaluation_sample_evidence', 'hold_evaluation_adoption_attempts', 'hold_evaluation_adoption_samples',
        'pin_retained_evaluation_storage', 'create_sample_evaluation', 'queue_evaluation_attempt',
        '_hold_evaluation_attempt', 'claim_evaluation_attempt', 'heartbeat_evaluation_attempt',
        'record_evaluation_dispatch', 'evaluation_maintenance_candidates', 'record_evaluation_maintenance',
        'start_evaluation_sample', 'checkpoint_evaluation_sample', 'fail_evaluation_sample_input',
        'request_evaluation_cancellation', 'lose_evaluation_attempt', 'finish_evaluation_attempt',
        'request_evaluation_deletion', 'purge_deleted_evaluation'
    ] LOOP
        EXECUTE format('ALTER FUNCTION yoao_learning_v1.%I OWNER TO %I', v_name, v_owner);
    END LOOP;
END
$learning_models$;
REVOKE CREATE ON SCHEMA yoao_learning_v1 FROM :"learning_owner";
GRANT USAGE ON SCHEMA yoao_learning_v1 TO :"learning_owner", :"assets_owner";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"learning_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.dataset_item_references(uuid, uuid) TO :"learning_owner";
GRANT SELECT, INSERT ON TABLE public.active_learning_rounds,
    public.active_learning_attempts,
    public.active_learning_pool_members,
    public.active_learning_candidates,
    public.active_learning_decisions,
    public.active_learning_references,
    public.active_learning_feedback,
    public.learning_policies,
    public.learning_triggers,
    public.training_studies,
    public.training_trials,
    public.training_recipes,
    public.training_recipe_revisions
    TO :"learning_owner";
GRANT UPDATE ON TABLE public.active_learning_rounds,
    public.active_learning_attempts,
    public.active_learning_candidates,
    public.learning_policies,
    public.learning_triggers,
    public.training_studies,
    public.training_trials,
    public.training_recipes
    TO :"learning_owner";
GRANT DELETE ON TABLE public.learning_policies TO :"learning_owner";
GRANT SELECT (id, project_id, snapshot, snapshot_digest, manifest), UPDATE (status)
    ON TABLE public.training_dataset_versions TO :"learning_owner";
GRANT SELECT (id, project_id, auto_trigger_id, study_id, dataset_id, request_id, config_digest,
    status, "authorization") ON TABLE public.training_runs TO :"learning_owner";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.active_learning_original_history(),
    yoao_learning_v1.active_learning_original_decisions(),
    yoao_learning_v1.active_learning_pool_item_count(uuid[]),
    yoao_learning_v1.active_learning_candidate_item_count(uuid[])
    TO :"assets_owner";
GRANT CREATE ON SCHEMA yoao_learning_v1 TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_create_round(uuid, uuid, uuid, uuid, uuid, uuid, text, text, jsonb, text, jsonb, jsonb, uuid[]) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_round(uuid, uuid, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_round_request(uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_round_for_job(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_rounds(uuid, text, integer, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_round_count(uuid, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_attempt(uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_recent_attempts(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_attempt_ids(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_candidates(uuid, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_due_rounds(timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_open_attempt(uuid, integer, uuid, uuid, uuid, timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_claim_attempt(uuid, integer, uuid, timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_touch_attempt(uuid, integer, uuid, text, timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_debit_round(uuid, integer, uuid, timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_candidate_request(uuid, integer, uuid, integer, jsonb, jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_candidate_evidence(uuid, integer, uuid, integer, jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_settle_attempt(uuid, integer, uuid, text, text, text, jsonb, timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_release_cancelled_claim(uuid, integer, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_decision_request(uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_decisions(uuid, integer, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_accepted_decisions(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_record_decision(uuid, uuid, uuid, uuid, text, text, uuid, integer[], jsonb, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_feedback_request(uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_feedback(uuid, integer, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_receipt_count(uuid, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_record_feedback(uuid, uuid, uuid, text, jsonb, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_retain_history(uuid, jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_history_reference(uuid, uuid, uuid[], uuid[], uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_dataset_history_budget(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_dataset_history(uuid, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.active_learning_original_history() OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.active_learning_original_decisions() OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.active_learning_pool_item_count(uuid[]) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.active_learning_candidate_item_count(uuid[]) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_adoption_attempts(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_adoption_candidates(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_pin_storage(uuid, uuid, integer, jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_policy(uuid, text, uuid, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_save_policy(uuid, text, integer, uuid, boolean, uuid, jsonb, jsonb, timestamptz, timestamptz, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_disable_policy(uuid, integer, timestamptz, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_delete_policy(uuid, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_due_policies(timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_policy_check_state(uuid, integer, boolean, text, text, timestamptz, timestamptz, timestamptz, text, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_advance_baseline(uuid, integer, uuid, uuid, jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_trigger(uuid, uuid, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_other_active_trigger(uuid, uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_trigger_duplicate(uuid, integer, uuid, text[]) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_trigger_history(uuid, text, integer, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_trigger_count(uuid, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_build_trigger(uuid, integer, uuid, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_trigger_progress(uuid, integer, text, text, timestamptz, text, text, text, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_requeue_trigger(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_policy_claim(uuid, uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_dataset_references(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_study(uuid, uuid, uuid, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_studies(uuid, integer, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_study_count(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_due_studies() OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_study_claim(uuid, uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_create_study(uuid, uuid, uuid, uuid, uuid, uuid, text, text, jsonb, text, jsonb, jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_trials(uuid, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_bind_trial(uuid, uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_study_progress(uuid, boolean, text, text, timestamptz, timestamptz) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_resume_study(uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_recipe(uuid, uuid, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_recipes(uuid, boolean, integer, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_recipe_count(uuid, boolean) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_recipe_revision(uuid, uuid, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_recipe_request(uuid, uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_recipe_revisions(uuid, integer, integer) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_recipe_revision_count(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_append_recipe(uuid, uuid, integer, uuid, text, text, jsonb, text, jsonb, uuid, uuid, text) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_learning_v1.selection_archive_recipe(uuid, uuid, integer) OWNER TO :"learning_owner";
ALTER VIEW yoao_learning_v1.selection_storage_owners OWNER TO :"learning_owner";
ALTER VIEW yoao_learning_v1.recipe_lineage OWNER TO :"learning_owner";
ALTER VIEW yoao_learning_v1.recipe_revision_lineage OWNER TO :"learning_owner";
REVOKE CREATE ON SCHEMA yoao_learning_v1 FROM :"learning_owner";

SELECT yoao_assets_storage_v1._adopt_installed_owners();
REVOKE ALL ON SCHEMA yoao_assets_storage_v1 FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_assets_storage_v1 FROM PUBLIC;
GRANT SELECT, INSERT ON public.task_originals, public.dataset_auxiliary_originals TO :"assets_owner";
GRANT UPDATE (id) ON public.task_originals, public.dataset_auxiliary_originals TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE ON public.managed_original_creations TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE ON public.dataset_assets, public.dataset_asset_locations TO :"assets_owner";
GRANT DELETE ON public.dataset_asset_locations TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE, DELETE ON public.original_cache_entries, public.original_cache_read_leases TO :"assets_owner";
GRANT SELECT, INSERT ON public.managed_original_retirements, public.managed_original_retention_holds TO :"assets_owner";
GRANT UPDATE (retain_until, policy_revision) ON public.managed_original_retirements TO :"assets_owner";
GRANT UPDATE (revision, released_at, released_by, release_reason) ON public.managed_original_retention_holds TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE ON public.managed_original_verifications,
    public.managed_original_reclaim_attempts, public.managed_original_reclaim_claims,
    public.managed_original_reference_reconciliation TO :"assets_owner";
GRANT SELECT (id, dataset_id, location_id, enabled, unregistered_at, ownership_mode,
    cache_policy, config_revision, policy_revision), UPDATE (id) ON public.dataset_sources TO :"assets_owner";
GRANT SELECT (dataset_item_id, source_id, run_id, path_digest) ON public.dataset_references TO :"assets_owner";
GRANT SELECT (run_id, path_digest, observed, verified_content, relative_path) ON public.dataset_scan_entries TO :"assets_owner";
GRANT SELECT (id, source_id, snapshot) ON public.dataset_scan_runs TO :"assets_owner";
GRANT SELECT (async_job_id, status, source, operation_id) ON public.storage_migration_inventories TO :"assets_owner";
GRANT SELECT (id, source, target, summary, delta_sequence) ON public.storage_migration_copies TO :"assets_owner";
GRANT SELECT (copy_id, status, source_operation_id, target_operation_id)
    ON public.storage_migration_copy_attempts TO :"assets_owner";
GRANT SELECT, INSERT ON public.storage_operations TO :"assets_owner";
GRANT SELECT, UPDATE (location_id) ON public.storage_maintenance_gates TO :"assets_owner";
GRANT SELECT (id, point_cloud_quality_config), UPDATE (id) ON public.projects TO :"workspace_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_original_item(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_asset_for_item(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.managed_original_locations(uuid,boolean,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.task_original_source(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.auxiliary_original_source(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.auxiliary_original_item(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.auxiliary_original_scene(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.publish_task_original_source(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.publish_auxiliary_original_source(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.managed_original_location_ref(uuid,uuid,text,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_binding_history() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.local_original_receipt(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.local_original_for_owner_key(text,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.abandon_unstarted_fixture_original(uuid,uuid,uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._managed_original_locator_facts(public.managed_original_creations) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_auxiliary_original_material(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fixture_original_target_matches(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.allocate_local_original(jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_local_original_step(uuid,text,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.protect_original_group(text,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.attach_verified_original(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_registry_snapshot(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.observe_original_reclamation(text,uuid,text,text,text,jsonb,uuid,uuid,integer,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_cache_scope(uuid,uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_cache_usage(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_cache_charge(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_cache_current_state(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_cache_entry_facts(uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.reserve_original_cache_generation(jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._original_cache_object_key(public.original_cache_entries) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_cache_has_readers(uuid,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.retire_original_cache_generation(uuid,timestamptz,timestamptz,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.retire_original_cache_page(uuid,timestamptz,timestamptz,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.publish_original_cache_generation(uuid,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_original_cache_read(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_cache_locator_current(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_original_cache_read(uuid,uuid,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_cache_lease_current(uuid,uuid,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.end_original_cache_read(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.expire_original_cache_reads(uuid,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.evict_original_cache_generation(uuid,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.reschedule_original_cache_check(uuid,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_original_cache_cleanup_error(uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_original_cache_cleanup(uuid,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.due_original_cache_checks(timestamptz,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.reclaim_attempt_facts(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.reclaim_attempt_for_job(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.reclaim_history_page(uuid,integer,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.reclaim_history_count(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.reclaim_claim_facts(text,text,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.reclaim_creation_active(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.admit_original_reclaim(jsonb,uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.claim_original_reclaim_attempt(uuid,text,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.request_original_reclaim_cancel(uuid,text,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_original_reclaim_attempt(uuid,text,text,text,text,text,timestamptz,timestamptz,boolean,jsonb,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.acquire_original_reclaim_operation(uuid,text,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.reclaim_version_may_be_deleted(uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.checkpoint_original_reclaim(uuid,text,text,jsonb,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_reclaim_gate_blocked(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_reclaim_source_blocked(text[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_reconciliation_facts(boolean,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.queue_original_reconciliation(uuid,text,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.advance_original_reconciliation(uuid,text,text,text,text,jsonb,timestamptz,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.observe_original_retention(uuid,uuid,text,timestamptz,uuid,text,text,integer,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_retirement_anchor(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_original_retention_hold(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.apply_original_retention(uuid,integer,text,timestamptz,uuid,integer,text,text,text,uuid,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.observe_original_verification(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_verification_facts(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_verification_for_job(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.active_original_verification(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_verification_unsettled(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_verification_history(uuid,integer,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_verification_count(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.admit_original_verification(jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.claim_original_verification(uuid,text,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.request_original_verification_cancel(uuid,text,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.bind_original_verification_permit(uuid,text,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_original_verification(uuid,text,text,text,text,text,timestamptz,timestamptz,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_retirement_candidates(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.append_original_retirement_assertions(uuid,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_original_binding_history() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.original_cache_binding_history() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_original_registry_snapshot() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_point_cloud_original_scenes(uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_point_cloud_original_poses(uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_point_cloud_original_items(uuid[]) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_labeling_v1 TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.original_reclaim_task_evidence(text) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_point_cloud_original_run(uuid) OWNER TO :"labeling_owner";
REVOKE CREATE ON SCHEMA yoao_labeling_v1 FROM :"labeling_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_jobs_v1 TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.sync_original_maintenance_job(uuid,text,text,text,text,timestamptz,text,jsonb,boolean) OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA yoao_jobs_v1 FROM :"jobs_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_assets_storage_v1 TO :"assets_owner";
ALTER FUNCTION yoao_assets_storage_v1.begin_stopped_original_reclaim(uuid,uuid,integer,text) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_storage_v1 FROM :"assets_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_workspace_v1 TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.lock_point_cloud_original_configuration(uuid) OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA yoao_workspace_v1 FROM :"workspace_owner";

GRANT USAGE ON SCHEMA yoao_jobs_v1, yoao_labeling_v1 TO :"assets_owner";
GRANT SELECT ON yoao_labeling_v1.task_dataset_item_link_rows TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.original_reclaim_task_evidence(text) TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.read_job(uuid),
    yoao_jobs_v1.sync_original_maintenance_job(uuid,text,text,text,text,timestamptz,text,jsonb,boolean)
TO :"assets_owner";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"assets_owner", :"jobs_owner";
GRANT USAGE ON SCHEMA yoao_identity_v1, yoao_jobs_v1, yoao_learning_v1,
    yoao_labeling_v1, yoao_workspace_v1, yoao_assets_storage_v1 TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.read_actor_facts(uuid) TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.read_job(uuid), yoao_jobs_v1.read_job_for_update(uuid),
    yoao_jobs_v1.dataset_active_job_facts(uuid),
    yoao_jobs_v1.settle_dataset_scan_job(uuid, text, text, timestamptz, text, jsonb),
    yoao_jobs_v1.transition_directory_registration_job(uuid, uuid, text, text, text, timestamptz, text, jsonb)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.training_dataset_asset_versions(uuid, boolean) TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1._catalog_dataset_summary(uuid) TO :"assets_owner";
GRANT SELECT (id, dataset_item_id, project_id) ON yoao_labeling_v1.task_rows TO :"assets_owner";
GRANT SELECT (id, task_id, dataset_item_id, role) ON yoao_labeling_v1.task_dataset_item_link_rows TO :"assets_owner";
GRANT SELECT (id, task_id) ON yoao_labeling_v1.annotation_rows TO :"assets_owner";
GRANT SELECT (id, data_type, scene_mode) ON yoao_workspace_v1.project_facts TO :"assets_owner";
GRANT SELECT ON yoao_assets_storage_v1.agent_location_status TO :"assets_owner";

GRANT SELECT (id, display_id, name, description, data_type, storage_scan_prefix, is_temporal,
    file_count, created_by, archived_at, archived_by, lifecycle_revision, metadata, created_at, updated_at),
    UPDATE (id, file_count, updated_at, storage_scan_prefix, archived_at, archived_by, lifecycle_revision)
    ON public.datasets TO :"assets_owner";
GRANT UPDATE (metadata) ON public.dataset_items TO :"assets_owner";
GRANT SELECT (display_id) ON public.scenes TO :"assets_owner";
GRANT SELECT (id, scope, created_by, revision), UPDATE (id)
    ON public.storage_connections TO :"assets_owner";
GRANT SELECT ON public.original_cache_entries TO :"assets_owner";
GRANT SELECT (id, owner_kind, owner_id, path_digest, state, storage_ref, input_ref, sha256, size_bytes, uncertain_write)
    ON public.thumbnail_outputs TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE ON public.dataset_sources, public.dataset_scan_runs,
    public.dataset_scan_entries, public.dataset_registration_runs, public.dataset_registration_entries,
    public.dataset_source_sync_schedules, public.dataset_source_changes, public.dataset_source_previews,
    public.dataset_reference_replicas, public.dataset_quality_runs, public.dataset_quality_results,
    public.dataset_quality_review_batches, public.dataset_quality_review_members TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE, DELETE ON public.dataset_source_items TO :"assets_owner";
GRANT SELECT, INSERT ON public.dataset_source_revisions, public.dataset_references TO :"assets_owner";
GRANT UPDATE (dataset_item_id) ON public.dataset_references TO :"assets_owner";
GRANT DELETE ON public.dataset_quality_review_batches TO :"assets_owner";
GRANT USAGE ON SEQUENCE public.dataset_source_acceptance_order_seq TO :"assets_owner";

GRANT CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_quality_duplicate_groups(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_quality_evidence(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_quality_job_matches(uuid, text, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_quality_manage(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_quality_result_matches(uuid, text, text, text, uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_quality_review_receipt(uuid, boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_quality_review_summary(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_quality_review_visible(uuid, uuid, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_quality_run_receipt(uuid, boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_quality_run_visible(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_quality_sources_visible(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_quality_summary(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_quality_writable(uuid, integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_relationship_digest(text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_workbench_cache_evidence(uuid, uuid, uuid[], uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_workbench_observations(uuid, uuid, uuid[], uuid[], uuid, boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._dataset_workbench_preferred_mapping(uuid, uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._directory_registration_fenced(uuid, uuid, text, integer, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._lock_dataset_directory_source(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._lock_dataset_scan(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._running_dataset_scan(uuid, uuid, text, integer, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._transition_directory_registration_entry(uuid, text, text, text, text, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.active_dataset_scan(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.active_directory_registration(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.append_directory_source_revision(uuid, uuid, uuid, text, uuid, uuid, text, boolean, uuid, uuid, uuid, integer, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.apply_directory_source_selection(uuid, uuid, integer, integer, jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.arm_directory_registration_dispatch(uuid, uuid, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.bind_dataset_scan_attempt(uuid, uuid, integer, uuid, uuid, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.capture_dataset_scan_directory(uuid, uuid, text, integer, uuid, text, text, jsonb, jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.capture_dataset_scan_s3_page(uuid, uuid, text, integer, uuid, text, integer, text, integer, jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.change_dataset_archive_state(uuid, uuid, integer, boolean, timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.change_directory_reference_replica(uuid, uuid, uuid, integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.change_directory_source_lifecycle(uuid, uuid, integer, integer, text, timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.check_directory_revision_selection(uuid, text, integer, uuid, text, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.claim_dataset_scan(uuid, uuid, text, text, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.claim_directory_registration(uuid, uuid, text, text, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.claim_directory_sync_wakeup(uuid, uuid, timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.configure_directory_sync(uuid, integer, uuid, uuid, text, boolean, integer, text, text, integer, integer, jsonb, timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.confirm_directory_registration(uuid, uuid, integer, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.consume_directory_relocation(uuid, text, uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.continue_directory_sync(uuid, integer, uuid, timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.create_dataset_directory_source(uuid, uuid, uuid, text, text, text, jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.create_directory_preflight(uuid, uuid, uuid, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.create_directory_reference_replica(uuid, uuid, uuid, integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_archive_impact(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_cover_facts(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_display_item_page(uuid, uuid[], integer, bigint) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_item_at_object_key(uuid, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_item_count(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_item_path_facts(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_location_visible(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_managed_duplicate_candidates(uuid, uuid, text, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_metadata(uuid, boolean, boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_owns_directory_media(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_project_ids(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_apply_review(uuid, uuid, uuid, integer, text, text, timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_attach_job(uuid, integer, uuid, uuid, uuid, uuid, text, integer, jsonb, timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_begin(uuid, uuid, jsonb, integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_expired_runs(timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_job_visible(uuid, text, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_key_matches(uuid, integer, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_lock_locations(uuid, uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_lock_result(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_maintenance_members(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_next_result(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_other_active(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_reconcile(uuid, boolean, timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_record_result(uuid, uuid, integer, text, uuid, timestamptz, text, text, boolean, jsonb, integer, integer, timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_result_page(uuid, uuid, integer, integer, text, text, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_review_attach_job(uuid, integer, uuid, uuid, uuid, uuid, text, integer, timestamptz, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_review_attempt(uuid, uuid, text, integer, text, timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_review_batch(uuid, uuid, uuid, boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_review_batch_page(uuid, uuid, integer, integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_review_checkpoint(uuid, uuid, integer, text, uuid, integer, timestamptz, timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_review_create_preview(uuid, uuid, integer, jsonb, text, text, text, timestamptz, timestamptz, jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_review_expired_batches(timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_review_key_matches(uuid, integer, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_review_member_page(uuid, uuid, uuid, integer, integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_review_next_member(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_review_prepare_preview(uuid, uuid, timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_review_preview_rows(uuid, uuid, text, text, text, uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_review_reconcile(uuid, boolean, timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_review_transition(uuid, uuid, integer, text, text, timestamptz, timestamptz, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_run(uuid, uuid, uuid, boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_run_attempt(uuid, uuid, text, integer, text, timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_run_page(uuid, uuid, integer, integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_quality_run_transition(uuid, uuid, integer, text, text, timestamptz, timestamptz, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_scan_dispatch_route(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_scan_for_job(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_scan_snapshot(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_scene_choices(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_source_visible(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_workbench_cache_candidates(uuid, uuid, uuid[], uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_workbench_item_matches(uuid, uuid, uuid, text, text, uuid, text, text, text, integer, integer, integer, integer, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_workbench_item_states(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_workbench_page_observations(uuid, uuid, uuid, uuid[], uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_workbench_preview_mapping(uuid, uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_workbench_source_reference_evidence(uuid, uuid[], uuid[], uuid[], text[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.dataset_workbench_source_runs(uuid, uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.declare_directory_source_protection(uuid, uuid, integer, jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_confirmation_sample(uuid, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_dataset_names(uuid, uuid, uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_job_visible(uuid, text, uuid, text, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_location_job_runs(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_preflight_exclude_unseen(uuid, text[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_preflight_unseen(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_preview_is_current(uuid, text, uuid, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_preview_slot(uuid, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_reference_binding(uuid, text, boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_reference_content(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_reference_lineage(uuid, uuid, uuid, text, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_reference_reclaim_facts(uuid, uuid, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_reference_replica_available(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_reference_replica_edge(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_reference_replica_page(uuid, uuid, integer, integer, boolean, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_reference_route_order(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_registration_binding(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_registration_change_kinds(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_registration_changes(uuid, text, integer, integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_registration_entries(uuid, text, integer, integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_registration_for_job(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_registration_for_scan(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_registration_probe_paths(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_relocation_candidates(uuid, text, integer, integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_revision_for_observation(uuid, uuid, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_scan_entry_page(uuid, uuid, integer, bigint) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_source_active_attempts(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_source_display_locations(uuid, uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_source_impact(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_source_revision_history(uuid, text, integer, integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_source_selection_heads(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_source_usage(uuid, uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.due_directory_registrations(timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.due_directory_sync_sources(timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.edit_dataset_directory_source(uuid, uuid, integer, text, boolean, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fail_directory_confirmation_sample(uuid, uuid, integer, text, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fail_directory_sync_wakeup(uuid, integer, uuid, text, boolean, timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.filtered_dataset_catalog(uuid, text, text[], boolean, text, uuid, integer, bigint) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_directory_registration(uuid, uuid, text, text, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.list_dataset_scans(uuid, uuid, boolean, boolean, integer, integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_dataset_directory_source(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_directory_reference_bindings(uuid[], uuid[], uuid[], uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_directory_reference_location(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_directory_reference_replica_primary(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_managed_item_asset_slot(uuid, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.next_dataset_scan_directory(uuid, uuid, text, integer, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.next_directory_registration_entry(uuid, uuid, text, integer, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.note_directory_registration_dispatch_failure(uuid, uuid, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.open_dataset_scan_s3_page(uuid, uuid, text, integer, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.open_directory_registration_attempt(uuid, uuid, uuid, uuid, uuid, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.pin_legacy_dataset_prefix(uuid, integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.project_has_dataset(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.publish_directory_preview(uuid, text, uuid, text, integer, integer, integer, integer, timestamptz, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.publish_directory_reference(uuid, uuid, uuid, text, text, bigint) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.publish_directory_reference_head(uuid, uuid, uuid, uuid, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.publish_managed_dataset_item(uuid, integer, text, text, text, bigint, text, integer, integer, text, boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_dataset_scan(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_dataset_scan_directory(uuid, uuid, text, integer, uuid, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_dataset_scan_execution(uuid, boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_directory_registration(uuid, boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_directory_source_head(uuid, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_directory_source_revision(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_directory_sync_schedule(uuid, boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_observed_dataset_scan_file(uuid, uuid, text, boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_directory_registration_entry(uuid, uuid, text, integer, uuid, text, text, text, uuid, bigint, text, bigint, bigint, bigint, boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.register_managed_item_asset(uuid, uuid, text, uuid, text, jsonb, text, bigint, text, timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.requeue_directory_registration(uuid, uuid, text, integer, uuid, text, integer, text, boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.retry_directory_registration_entry(uuid, integer, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.search_dataset_names(uuid, text, integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.settle_dataset_scan(uuid, uuid, text, integer, uuid, text, text, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.settle_directory_sync_wakeup(uuid, integer, uuid, text, uuid, text, timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.start_dataset_scan(uuid, uuid, jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.validate_dataset_scan_directory(uuid, uuid, text, integer, uuid, text, text, text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.verify_observed_dataset_scan_file(uuid, uuid, text, jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.visible_dataset_sources(uuid, uuid, uuid, uuid) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";

GRANT CREATE ON SCHEMA yoao_labeling_v1 TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_point_quality_annotation_geometry(uuid) OWNER TO :"labeling_owner";
REVOKE CREATE ON SCHEMA yoao_labeling_v1 FROM :"labeling_owner";

GRANT EXECUTE ON FUNCTION yoao_assets_v1.directory_job_visible(uuid, text, uuid, text, uuid),
    yoao_assets_v1.directory_location_job_runs(uuid, uuid),
    yoao_assets_v1.dataset_quality_job_visible(uuid, text, text) TO :"jobs_owner";
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
GRANT USAGE, CREATE ON SCHEMA yoao_labeling_v1 TO :"labeling_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_workspace_v1 TO :"workspace_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";

GRANT SELECT (id, owner_id, annotation_id, task_id, project_id, storage_ref, sha256,
    size_bytes, mime_type, file_name, created_at, expires_at, state, bound_comment_id,
    retired_at, last_attempted_at),
    INSERT (id, owner_id, annotation_id, task_id, project_id, storage_ref, sha256,
    size_bytes, mime_type, file_name, created_at, expires_at, state),
    UPDATE (state, bound_comment_id, retired_at, last_attempted_at), DELETE
    ON public.comment_attachment_uploads TO :"labeling_owner";
GRANT SELECT (id, project_id, uploader_id, logical_object_key, content_type, storage_ref,
    created_at, upload_expires_at),
    INSERT (id, project_id, uploader_id, logical_object_key, content_type, storage_ref,
    created_at, upload_expires_at), UPDATE (id)
    ON public.mask_format_uploads TO :"labeling_owner";
GRANT SELECT (id, logical_key, canonical_sha256, canonical_size_bytes, height, width, runs,
    storage_encoding, stored_sha256, stored_size_bytes, storage_ref, state,
    created_at, published_at, last_used_at, last_attempted_at),
    INSERT (id, logical_key, canonical_sha256, canonical_size_bytes, height, width, runs,
    storage_encoding, stored_sha256, stored_size_bytes, storage_ref, state, created_at),
    UPDATE (state, published_at, last_used_at, retired_at, last_attempted_at), DELETE
    ON public.raster_mask_objects TO :"labeling_owner";
GRANT SELECT (id, object_id, gate_revision),
    INSERT (id, object_id, gate_revision, created_at), UPDATE (id), DELETE
    ON public.raster_mask_publications TO :"labeling_owner";
GRANT INSERT (id, owner_id, project_id, storage_ref, sha256, size_bytes, mime_type,
    file_name, state, created_at), UPDATE (last_attempted_at), DELETE
    ON public.guide_asset_uploads TO :"workspace_owner";
GRANT UPDATE (upload_id) ON public.project_guide_assets TO :"workspace_owner";

ALTER TYPE yoao_workspace_v1.guide_physical_receipt OWNER TO :"workspace_owner";
ALTER TYPE yoao_labeling_v1.raster_physical_receipt OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.create_uploading_task(uuid,uuid,text,text,text,uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.mask_ai_inventory() OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_workspace_v1.guide_clone_snapshot(uuid) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.stage_guide_upload(uuid,uuid,uuid,jsonb,text,integer,text,text,timestamptz)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.lock_guide_upload(uuid,boolean) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.mount_guide_upload(uuid,uuid) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.guide_upload_has_reference(uuid) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.guide_retirement_candidates(integer) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.attempt_guide_retirement(uuid,timestamptz) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.settle_guide_retirement(uuid,jsonb) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.guide_binding_history() OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_labeling_v1.stage_mask_format_upload(uuid,uuid,uuid,text,text,jsonb,timestamptz)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.mask_format_upload_source(text,boolean) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.mask_format_binding_history() OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.stage_comment_attachment(uuid,uuid,uuid,uuid,uuid,jsonb,text,integer,text,text,timestamptz)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_comment_attachment(uuid,boolean) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.comment_attachment_binding(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.publish_comment_attachment(uuid,jsonb) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.bind_comment_attachment(uuid,uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.retire_comment_attachments(uuid[],timestamptz) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.comment_attachment_retirement_candidates(timestamptz,integer)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.attempt_comment_attachment_retirement(uuid,timestamptz)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.settle_comment_attachment_retirement(uuid,jsonb,timestamptz)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.annotation_binding_history() OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.stage_raster_object(uuid,text,text,integer,integer,integer,integer,text,text,integer,jsonb,timestamptz)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.ready_raster_owner(text,boolean) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_raster_owners(uuid[],boolean) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_raster_ready_for_publish(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.publish_raster_object(uuid,jsonb,timestamptz) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.touch_ready_raster_object(uuid,jsonb,timestamptz) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.retire_raster_object(uuid,text,jsonb,timestamptz,timestamptz)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.raster_physical_gc_candidates(timestamptz,integer,boolean)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.note_raster_gc_attempts(uuid[],timestamptz[]) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.settle_raster_gc(uuid,jsonb) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.open_raster_publication(uuid,uuid,integer,timestamptz) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.raster_publication_matches(uuid,uuid,integer) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.release_raster_publication(uuid,uuid,integer) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_raster_publications(uuid[]) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.consume_raster_publications(uuid[],uuid[],integer[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.raster_object_has_publication(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.expire_unlinked_raster_uploads(timestamptz) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.raster_physical_reference_keys(text) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_assets_v1.lock_video_segment_source(uuid,uuid) OWNER TO :"assets_owner";

REVOKE CREATE ON SCHEMA yoao_labeling_v1 FROM :"labeling_owner";
REVOKE CREATE ON SCHEMA yoao_workspace_v1 FROM :"workspace_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"assets_owner";

GRANT SELECT, INSERT, UPDATE ON public.image_pyramid_assets, public.image_pyramid_outputs,
    public.video_chunks, public.video_frame_cache, public.video_derivative_outputs,
    public.thumbnail_outputs TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE, DELETE ON public.image_pyramid_generations, public.video_chapters
    TO :"assets_owner";
GRANT SELECT, INSERT, DELETE ON public.video_frame_indices TO :"assets_owner";
GRANT SELECT (id, dataset_id, file_type, file_path, created_at, width, height, metadata,
    thumbnail_output_id, thumbnail_path, blurhash, video_output_id, video_building_output_id)
    ON public.dataset_items TO :"assets_owner";
GRANT UPDATE (video_building_output_id, video_output_id, width, height, thumbnail_path,
    thumbnail_output_id, blurhash, metadata, updated_at) ON public.dataset_items TO :"assets_owner";
GRANT SELECT (id, location_id, purpose, revision) ON public.storage_operations TO :"assets_owner";
GRANT SELECT (location_id, state, revision), UPDATE (location_id)
    ON public.storage_maintenance_gates TO :"assets_owner";
GRANT SELECT (id, kind, enabled, config_revision, config, connection_id)
    ON public.storage_locations TO :"assets_owner";
GRANT SELECT (purpose, location_id, bucket, generation) ON public.storage_bindings TO :"assets_owner";
GRANT SELECT (id, kind, scope, project_id, config) ON public.storage_connections TO :"assets_owner";
GRANT SELECT (source_id, path_digest, thumbnail_output_id, state, source_sha256, spec, location_id,
    bucket, object_key, sha256, size_bytes) ON public.dataset_source_previews TO :"assets_owner";
GRANT SELECT (id, location_id) ON public.dataset_sources TO :"assets_owner";
GRANT SELECT (location_id, endpoint, bucket, binding_generation, storage_identity)
    ON yoao_assets_v1.fixture_native_cache_targets TO :"assets_owner";
GRANT SELECT (thumbnail_path, thumbnail_output_id) ON yoao_labeling_v1.task_rows TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.read_media_task_access(uuid,uuid,uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.legacy_pyramid_gc_eligible(text,uuid,integer),
    yoao_assets_v1.fixture_media_ordinary_gc(text,uuid,integer),
    yoao_assets_v1.dataset_owns_directory_media(uuid),
    yoao_assets_v1.publish_image_pyramid_thumbnail(uuid,integer,uuid) TO :"assets_owner";

ALTER FUNCTION yoao_assets_v1.read_legacy_pyramid_asset(text,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_legacy_pyramid_generation(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_legacy_pyramid_summaries(uuid[],uuid[],text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.prepare_legacy_pyramid_generation(uuid,text,uuid,text,text,jsonb,jsonb,text,boolean,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_legacy_pyramid_claim(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.claim_legacy_pyramid_generation(uuid,integer,uuid,uuid,integer,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_legacy_pyramid_publication(uuid,integer,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_legacy_pyramid_context(uuid,integer,uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_legacy_pyramid_output(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_legacy_pyramid_output_for_publish(uuid,integer,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.publish_legacy_pyramid_generation(uuid,integer,uuid,uuid,jsonb,jsonb,bigint) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fail_legacy_pyramid_generation(uuid,integer,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.invalidate_legacy_pyramid_generation(uuid,integer,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.touch_legacy_pyramid_generation(uuid,integer,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.scan_expired_legacy_pyramid_generations() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.scan_stale_legacy_pyramid_generations(timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.remove_stale_legacy_pyramid_generation(uuid,integer,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_pyramid_output_unreferenced(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.scan_retirable_legacy_pyramid_outputs(timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_legacy_pyramid_retirement(uuid,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.check_legacy_pyramid_retirement(uuid,jsonb,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_legacy_pyramid_retirement(uuid,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_legacy_pyramid_retained_bytes() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_video_chapter_scope(uuid,uuid,uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_legacy_video_chapters(uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_legacy_video_chapter_source(uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_legacy_video_chapter(uuid,uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.create_legacy_video_chapter(uuid,uuid,uuid,uuid,integer,integer,text,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.update_legacy_video_chapter(uuid,uuid,uuid,uuid,integer,integer,text,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.delete_legacy_video_chapter(uuid,uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_media_cache_binding_history() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_thumbnail_facts(public.thumbnail_outputs) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_media_cache_target_current(jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_thumbnail_gc_clear(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_thumbnail_gc_recheck(uuid,jsonb,uuid,timestamptz,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_thumbnail_output(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_item_thumbnail_pointer(uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_thumbnail_evidence(uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.allocate_legacy_thumbnail(uuid,text,uuid,text,jsonb,jsonb,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_legacy_thumbnail_result(uuid,jsonb,text,bigint,text,text,boolean,boolean,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_directory_thumbnail_candidate(uuid,uuid,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.retire_legacy_directory_thumbnail(uuid,timestamptz,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_thumbnail_business(text,uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.set_legacy_item_dimensions(uuid,integer,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_legacy_item_thumbnail_error(uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.publish_legacy_thumbnail(text,uuid,uuid,jsonb,jsonb,text,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_thumbnail_gc_candidates(integer,timestamptz,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_thumbnail_gc_start(uuid,timestamptz,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_thumbnail_gc_admit(uuid,jsonb,uuid,timestamptz,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_thumbnail_gc_finish(uuid,jsonb,timestamptz,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_item(uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_slot(uuid,text,jsonb,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_create_slot(uuid,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_attempt(uuid,text,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_allocate_output(uuid,uuid,text,jsonb,jsonb,jsonb,uuid,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_attach_output(uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_cache_output(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_dispatch_sources(uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_pts(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_failed_frames(uuid,integer,text,integer[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_touch_slot(text,uuid,uuid,uuid,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_rebuild_items(uuid[],uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_locate_pending(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_claim_attempt(uuid,uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_fail_attempt(uuid,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_publish_attempt(uuid,uuid,jsonb,jsonb,timestamptz,integer,integer,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_ready_bytes() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_expired_slots(text,timestamptz,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_expire_slot(text,uuid,uuid,uuid,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_abandoned_attempts(timestamptz,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_expire_attempt(uuid,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_retirement_candidates(timestamptz,timestamptz,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_begin_sweep(uuid,timestamptz,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_sweep_attempt(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_sweep_permit(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_finish_sweep(uuid,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_video_building_link(public.video_derivative_outputs) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_video_idle(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_video_unreferenced(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_video_expired(public.video_derivative_outputs,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_video_gc_lock(uuid) OWNER TO :"assets_owner";

REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";

REVOKE ALL ON FUNCTION
    yoao_assets_v1.read_legacy_pyramid_asset,
    yoao_assets_v1.read_legacy_pyramid_generation,
    yoao_assets_v1.read_legacy_pyramid_summaries,
    yoao_assets_v1.prepare_legacy_pyramid_generation,
    yoao_assets_v1.lock_legacy_pyramid_claim,
    yoao_assets_v1.claim_legacy_pyramid_generation,
    yoao_assets_v1.lock_legacy_pyramid_publication,
    yoao_assets_v1.read_legacy_pyramid_context,
    yoao_assets_v1.read_legacy_pyramid_output,
    yoao_assets_v1.lock_legacy_pyramid_output_for_publish,
    yoao_assets_v1.publish_legacy_pyramid_generation,
    yoao_assets_v1.fail_legacy_pyramid_generation,
    yoao_assets_v1.invalidate_legacy_pyramid_generation,
    yoao_assets_v1.touch_legacy_pyramid_generation,
    yoao_assets_v1.scan_expired_legacy_pyramid_generations,
    yoao_assets_v1.scan_stale_legacy_pyramid_generations,
    yoao_assets_v1.remove_stale_legacy_pyramid_generation,
    yoao_assets_v1._legacy_pyramid_output_unreferenced,
    yoao_assets_v1.scan_retirable_legacy_pyramid_outputs,
    yoao_assets_v1.begin_legacy_pyramid_retirement,
    yoao_assets_v1.check_legacy_pyramid_retirement,
    yoao_assets_v1.finish_legacy_pyramid_retirement,
    yoao_assets_v1.read_legacy_pyramid_retained_bytes,
    yoao_assets_v1._legacy_video_chapter_scope,
    yoao_assets_v1.read_legacy_video_chapters,
    yoao_assets_v1.lock_legacy_video_chapter_source,
    yoao_assets_v1.lock_legacy_video_chapter,
    yoao_assets_v1.create_legacy_video_chapter,
    yoao_assets_v1.update_legacy_video_chapter,
    yoao_assets_v1.delete_legacy_video_chapter,
    yoao_assets_v1.read_media_cache_binding_history,
    yoao_assets_v1._legacy_thumbnail_facts,
    yoao_assets_v1._legacy_media_cache_target_current,
    yoao_assets_v1._legacy_thumbnail_gc_clear,
    yoao_assets_v1._legacy_thumbnail_gc_recheck,
    yoao_assets_v1.legacy_thumbnail_output,
    yoao_assets_v1.legacy_item_thumbnail_pointer,
    yoao_assets_v1.directory_thumbnail_evidence,
    yoao_assets_v1.allocate_legacy_thumbnail,
    yoao_assets_v1.record_legacy_thumbnail_result,
    yoao_assets_v1.legacy_directory_thumbnail_candidate,
    yoao_assets_v1.retire_legacy_directory_thumbnail,
    yoao_assets_v1.legacy_thumbnail_business,
    yoao_assets_v1.set_legacy_item_dimensions,
    yoao_assets_v1.record_legacy_item_thumbnail_error,
    yoao_assets_v1.publish_legacy_thumbnail,
    yoao_assets_v1.legacy_thumbnail_gc_candidates,
    yoao_assets_v1.legacy_thumbnail_gc_start,
    yoao_assets_v1.legacy_thumbnail_gc_admit,
    yoao_assets_v1.legacy_thumbnail_gc_finish,
    yoao_assets_v1.legacy_video_item,
    yoao_assets_v1.legacy_video_slot,
    yoao_assets_v1.legacy_video_create_slot,
    yoao_assets_v1.legacy_video_attempt,
    yoao_assets_v1.legacy_video_allocate_output,
    yoao_assets_v1.legacy_video_attach_output,
    yoao_assets_v1.legacy_video_cache_output,
    yoao_assets_v1.legacy_video_dispatch_sources,
    yoao_assets_v1.legacy_video_pts,
    yoao_assets_v1.legacy_video_failed_frames,
    yoao_assets_v1.legacy_video_touch_slot,
    yoao_assets_v1.legacy_video_rebuild_items,
    yoao_assets_v1.legacy_video_locate_pending,
    yoao_assets_v1.legacy_video_claim_attempt,
    yoao_assets_v1.legacy_video_fail_attempt,
    yoao_assets_v1.legacy_video_publish_attempt,
    yoao_assets_v1.legacy_video_ready_bytes,
    yoao_assets_v1.legacy_video_expired_slots,
    yoao_assets_v1.legacy_video_expire_slot,
    yoao_assets_v1.legacy_video_abandoned_attempts,
    yoao_assets_v1.legacy_video_expire_attempt,
    yoao_assets_v1.legacy_video_retirement_candidates,
    yoao_assets_v1.legacy_video_begin_sweep,
    yoao_assets_v1.legacy_video_sweep_attempt,
    yoao_assets_v1.legacy_video_sweep_permit,
    yoao_assets_v1.legacy_video_finish_sweep,
    yoao_assets_v1._legacy_video_building_link,
    yoao_assets_v1._legacy_video_idle,
    yoao_assets_v1._legacy_video_unreferenced,
    yoao_assets_v1._legacy_video_expired,
    yoao_assets_v1._legacy_video_gc_lock
FROM PUBLIC;

GRANT USAGE, CREATE ON SCHEMA yoao_labeling_v1 TO :"labeling_owner";
GRANT SELECT (id, owner_kind, owner_id, input_ref, storage_ref, state, uncertain_write)
    ON public.thumbnail_outputs TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._legacy_task_thumbnail_business(uuid,boolean) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._legacy_task_thumbnail_referenced(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._publish_legacy_task_thumbnail(uuid,uuid,text) OWNER TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    yoao_labeling_v1._legacy_task_thumbnail_business(uuid,boolean),
    yoao_labeling_v1._legacy_task_thumbnail_referenced(uuid),
    yoao_labeling_v1._publish_legacy_task_thumbnail(uuid,uuid,text) TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_labeling_v1 FROM :"labeling_owner";
REVOKE ALL ON FUNCTION
    yoao_labeling_v1._legacy_task_thumbnail_business(uuid,boolean),
    yoao_labeling_v1._legacy_task_thumbnail_referenced(uuid),
    yoao_labeling_v1._publish_legacy_task_thumbnail(uuid,uuid,text) FROM PUBLIC;
GRANT CREATE ON SCHEMA yoao_identity_v1 TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.reserve_avatar_upload(uuid,text,jsonb,text,integer) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.confirm_avatar_upload_version(uuid,text,jsonb,text,integer,text) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.avatar_upload_pending_matches(uuid,text,jsonb,text,integer,text) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.read_ready_avatar_upload(text) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.list_avatar_retirements(integer) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.begin_avatar_retirement(text) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.finish_avatar_retirement(text,jsonb,text,integer,text) OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.read_avatar_binding_history() OWNER TO :"identity_owner";
ALTER FUNCTION yoao_identity_v1.avatar_saved_target_referenced(uuid) OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA yoao_identity_v1 FROM :"identity_owner";

GRANT SELECT, INSERT, UPDATE, DELETE ON public.avatar_uploads TO :"identity_owner";
GRANT CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.matches_avatar_reservation(text,jsonb) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"identity_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.matches_avatar_reservation(text,jsonb) TO :"identity_owner";
GRANT CREATE ON SCHEMA yoao_jobs_v1 TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.dataset_job_visible(uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1._directory_job_ids(uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.directory_job_page(uuid,uuid,integer,integer) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.directory_job_total(uuid,uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.dataset_active_job_facts(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.active_storage_writer_job_counts() OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.settle_dataset_scan_job(uuid,text,text,timestamptz,text,jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION yoao_jobs_v1.transition_directory_registration_job(uuid,uuid,text,text,text,timestamptz,text,jsonb) OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA yoao_jobs_v1 FROM :"jobs_owner";

GRANT USAGE ON SCHEMA yoao_assets_v1 TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION
    yoao_assets_v1.directory_job_visible(uuid,text,uuid,text,uuid),
    yoao_assets_v1.directory_location_job_runs(uuid,uuid),
    yoao_assets_v1.dataset_quality_job_visible(uuid,text,text)
    TO :"jobs_owner";
GRANT USAGE ON SCHEMA yoao_jobs_v1 TO :"assets_owner";
GRANT EXECUTE ON FUNCTION
    yoao_jobs_v1.dataset_active_job_facts(uuid),
    yoao_jobs_v1.settle_dataset_scan_job(uuid,text,text,timestamptz,text,jsonb),
    yoao_jobs_v1.transition_directory_registration_job(uuid,uuid,text,text,text,timestamptz,text,jsonb)
    TO :"assets_owner";

\set ON_ERROR_STOP on

GRANT USAGE ON SCHEMA yoao_identity_v1, yoao_settings_v1 TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.read_actor_facts(uuid),
    yoao_identity_v1.read_accepted_api_key_facts(uuid,uuid),
    yoao_settings_v1.resolve_connector_host_allowlist(jsonb) TO :"assets_owner";
GRANT SELECT (id, kind, scope, project_id, revision, config, secret_enc)
    ON public.storage_connections TO :"assets_owner";
GRANT SELECT (id, enabled, kind, config_revision, connection_id)
    ON public.storage_locations TO :"assets_owner";
GRANT SELECT (purpose, location_id, generation) ON public.storage_bindings TO :"assets_owner";
GRANT SELECT (location_id, state, revision) ON public.storage_maintenance_gates TO :"assets_owner";
GRANT SELECT (id, purpose, location_id, revision) ON public.storage_operations TO :"assets_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
ALTER TABLE yoao_assets_v1.saved_binding_probe OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.saved_io_allowlist(jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.saved_io_connector(uuid,integer,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.saved_io_target(uuid,integer,integer,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._saved_binding_probe_authority(uuid,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._saved_binding_probe_facts(uuid,text,uuid,integer,integer,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_saved_binding_probe(uuid,uuid,uuid,text,text,uuid,integer,integer,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.capture_saved_binding_probe(uuid,uuid,boolean) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
GRANT USAGE ON SCHEMA yoao_assets_storage_v1, yoao_identity_v1, yoao_jobs_v1,
    yoao_labeling_v1 TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_storage_v1.physical_io_receipt(uuid),
    yoao_assets_storage_v1.gate_facts(uuid,text),
    yoao_assets_storage_v1.location_probe_facts(uuid,text),
    yoao_identity_v1.read_actor_facts(uuid),
    yoao_identity_v1.read_accepted_api_key_facts(uuid,uuid),
    yoao_jobs_v1.read_job(uuid),
    yoao_labeling_v1.read_media_task_access(uuid,uuid,uuid) TO :"assets_owner";
GRANT SELECT ON yoao_assets_storage_v1.location_metadata,
    yoao_assets_storage_v1.connector_origins TO :"assets_owner";
GRANT SELECT (id, file_path) ON yoao_labeling_v1.task_rows TO :"assets_owner";
GRANT CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
ALTER TABLE yoao_assets_v1.reference_io_requests OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._reference_io_scope(uuid,text,uuid,uuid,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.prepare_reference_io(uuid,text,uuid,uuid,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.capture_reference_io(uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.recheck_reference_io(uuid,text) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_audit_v1 TO :"audit_owner";

ALTER TABLE yoao_assets_v1.saved_output_admissions OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.saved_output_writes OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._saved_output_gate(uuid,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._saved_output_origin(uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.saved_output_origin(jsonb,text,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.admit_saved_output_read(uuid,text,uuid,uuid,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.saved_avatar_subject(text,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.saved_bug_subject(uuid,uuid,text,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.saved_archive_subject(uuid,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_saved_output_write(uuid,text,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_saved_output_write(uuid,text,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.saved_output_write_status(uuid,text,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._guard_saved_output_write() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_audit_v1.saved_archive_output_facts(uuid,text,uuid) OWNER TO :"audit_owner";

GRANT SELECT (id, location_id, purpose, revision) ON public.storage_operations TO :"assets_owner";
GRANT SELECT (location_id, state, revision) ON public.storage_maintenance_gates TO :"assets_owner";
GRANT SELECT (id, connection_id, kind, enabled, config_revision) ON public.storage_locations TO :"assets_owner";
GRANT SELECT (id, kind, scope, project_id, config, revision) ON public.storage_connections TO :"assets_owner";
GRANT SELECT (token, owner_id, storage_ref, sha256, size_bytes, object_version_id, state)
ON public.avatar_uploads TO :"assets_owner";
GRANT USAGE ON SCHEMA yoao_identity_v1 TO :"assets_owner", :"audit_owner";
GRANT SELECT (avatar_ref) ON yoao_identity_v1.account_profiles TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.read_actor_facts(uuid) TO :"assets_owner", :"audit_owner";
GRANT USAGE ON SCHEMA yoao_audit_v1 TO :"assets_owner";
GRANT SELECT (id, job_id, year, month, storage_ref, sha256, size_bytes, state, published_at)
ON public.audit_archive_outputs TO :"audit_owner";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.saved_archive_output_facts(uuid,text,uuid) TO :"assets_owner";
GRANT USAGE ON SCHEMA yoao_jobs_v1 TO :"audit_owner";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.read_job(uuid) TO :"audit_owner";

REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_audit_v1 FROM :"audit_owner";
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
GRANT USAGE ON SCHEMA yoao_learning_v1 TO :"assets_owner", :"learning_owner";
GRANT CREATE ON SCHEMA yoao_learning_v1 TO :"learning_owner";
GRANT CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
ALTER TABLE public.training_saved_objects OWNER TO :"assets_owner";
ALTER FUNCTION yoao_learning_v1.training_saved_owner_scope(text,uuid,uuid,text,text,text,jsonb) OWNER TO :"learning_owner";
ALTER FUNCTION yoao_assets_v1.capture_training_saved_io(uuid,text,uuid,uuid,text,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_training_saved_io(uuid,text,uuid,uuid,text,text,jsonb,jsonb) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_learning_v1 FROM :"learning_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_learning_v1.training_saved_owner_scope(text,uuid,uuid,text,text,text,jsonb) TO :"assets_owner";
ALTER TABLE public.storage_migration_io_reads OWNER TO :"assets_owner";
ALTER TABLE public.storage_migration_io_writes OWNER TO :"assets_owner";
ALTER TABLE public.storage_migration_io_probes OWNER TO :"assets_owner";
ALTER TABLE public.storage_migration_io_flights OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._migration_io_target(uuid,jsonb,integer,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_io_copy(uuid,uuid,text,uuid,text,integer,integer,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_io_baseline(uuid,uuid,text,uuid,text,integer,integer,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_io_claim_write(uuid,uuid,text,uuid,text,integer,integer,text,integer,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_io_bind_read(uuid,uuid,integer,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_io_read(uuid,uuid,integer,text,text,integer,integer,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_io_cutover(uuid,integer,integer,integer,uuid,text,integer,uuid,integer,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_io_open_flight(uuid,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_io_close_flight(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.migration_io_reconcile_flights(uuid) OWNER TO :"assets_owner";
GRANT USAGE ON SCHEMA yoao_identity_v1, yoao_jobs_v1 TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.read_accepted_api_key_facts(uuid,uuid),
    yoao_identity_v1.read_actor_facts(uuid), yoao_jobs_v1.read_job(uuid) TO :"assets_owner";
GRANT SELECT (target) ON public.storage_migration_finalizations TO :"assets_owner";
GRANT SELECT (window_id, finalization_revision, window_revision)
    ON public.storage_migration_read_publications TO :"assets_owner";
GRANT SELECT (id) ON public.storage_migration_cutovers TO :"assets_owner";
ALTER TABLE yoao_assets_v1.import_io_manifests OWNER TO :"assets_owner";
ALTER TABLE yoao_assets_v1.import_io_attempts OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._import_io_job(uuid,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._import_io_transport(uuid,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.import_saved_io_list(uuid,uuid,jsonb,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.save_observed_dataset_import_manifest(uuid,uuid,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._import_io_entry(uuid,bigint,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._import_io_authority(public.dataset_import_entries) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_dataset_import_original(uuid,uuid,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._import_io_target(public.managed_original_creations) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.open_dataset_import_original(uuid,uuid,bigint,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._import_io_attempt(jsonb,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.import_saved_io_action(jsonb,text,jsonb,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._import_builtin_original(jsonb,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_import_original_prepared(jsonb,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.mark_import_original_writing(jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_import_original_written(jsonb,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fail_import_original(jsonb,text,text,jsonb) OWNER TO :"assets_owner";

GRANT USAGE ON SCHEMA yoao_identity_v1 TO :"notifications_owner", :"labeling_owner", :"diagnostics_owner";
GRANT SELECT (id, role, is_active) ON yoao_identity_v1.account_profiles TO :"notifications_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.read_actor_facts(uuid) TO :"labeling_owner", :"diagnostics_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_notifications_v1 TO :"notifications_owner";
ALTER TABLE yoao_notifications_v1.notification_channels OWNER TO :"notifications_owner";
ALTER TABLE yoao_notifications_v1.user_notification_channel_bindings OWNER TO :"notifications_owner";
ALTER TABLE yoao_notifications_v1.notification_channel_deliveries OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1._preference_policy_v2() OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.preference_command(jsonb) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1._channel_public_projection(
    yoao_notifications_v1.notification_channels
) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.list_notification_channels(boolean) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.get_notification_channel(text) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.register_notification_channel(jsonb) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.transition_notification_channel(jsonb) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.open_notification_channel_secret(text) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.list_user_channel_bindings(uuid) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.upsert_user_channel_binding(jsonb) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.mark_user_channel_binding_verified(uuid,text,timestamptz)
    OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.delete_user_channel_binding(uuid,text) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.enqueue_channel_deliveries(jsonb) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.has_terminal_channel_delivery(uuid,text,uuid)
    OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.channel_delivery_policy() OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.claim_channel_deliveries(jsonb) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.finish_channel_delivery(jsonb) OWNER TO :"notifications_owner";
REVOKE CREATE ON SCHEMA yoao_notifications_v1 FROM :"notifications_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_labeling_v1 TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._notification_task_visible(uuid,text,uuid,uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.notification_target_visible_to_actor(uuid,text,uuid,uuid,uuid)
    OWNER TO :"labeling_owner";
REVOKE CREATE ON SCHEMA yoao_labeling_v1 FROM :"labeling_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_diagnostics_v1 TO :"diagnostics_owner";
ALTER FUNCTION yoao_diagnostics_v1.bug_report_visible_to_actor(uuid,uuid)
    OWNER TO :"diagnostics_owner";
REVOKE CREATE ON SCHEMA yoao_diagnostics_v1 FROM :"diagnostics_owner";
COMMIT;
