"""Keep native-owned state out of ORM migration autogeneration."""

NATIVE_MANAGED_TABLES = frozenset(
    {
        "asset_accepted_image_inputs",
        "asset_accepted_image_input_attempts",
        "asset_accepted_input_garbage",
        "asset_accepted_image_input_manifests",
        "asset_accepted_image_input_manifest_members",
        "annotation_input_sources",
        "asset_accepted_video_manifests",
        "asset_accepted_video_members",
        "asset_accepted_video_inputs",
        "asset_accepted_video_input_attempts",
        "asset_accepted_video_times",
        "asset_accepted_video_frames",
        "asset_accepted_video_frame_attempts",
        "annotation_video_input_sources",
        "video_tracker_input_scopes",
        "annotation_video_tracker_input_sources",
        "mask_gc_intents",
        "mask_gc_key_epochs",
        "mask_gc_sweeps",
        "mask_gc_roots",
        "mask_gc_write_permit_secrets",
        "batch_result_executions",
        "batch_result_members",
        "batch_result_bundles",
        "batch_result_payloads",
        "batch_result_parts",
        "prediction_retry_attempts",
        "prediction_retry_deliveries",
        "identity_revoked_jtis",
        "identity_key_operation_receipts",
        "identity_session_operation_receipts",
        "identity_account_operation_receipts",
        "identity_recovery_operation_receipts",
        "identity_credential_mail_outbox",
        "identity_group_operation_receipts",
        "identity_deactivation_operation_receipts",
        "identity_deactivation_due_receipts",
        "identity_deactivation_requests",
        "notification_delivery_outbox",
        "identity_invitation_operation_receipts",
        "identity_directory_export_receipts",
        "identity_admin_password_reset_receipts",
        "identity_platform_role_operation_receipts",
        "identity_suspension_operation_receipts",
        "identity_deletion_operation_receipts",
        "identity_offboarding_operation_receipts",
        "workspace_membership_operation_receipts",
        "workspace_owner_transfer_operation_receipts",
        "workspace_configuration_operation_attempts",
        "workspace_configuration_operation_states",
        "workspace_configuration_operation_receipts",
    }
)

NATIVE_MANAGED_RELATIONS = frozenset(
    {("public", name) for name in NATIVE_MANAGED_TABLES}
    | {
        ("aap_jobs_v1", name)
        for name in (
            "video_execution_authority",
            "video_executions",
            "video_execution_route_intents",
            "video_execution_checkpoint_restore",
            "video_execution_journals",
            "video_execution_window_attempts",
            "video_execution_captures",
            "video_execution_capture_parts",
            "video_execution_windows",
            "video_execution_window_parts",
            "video_execution_span_closures",
            "video_restore_fence",
        )
    }
    | {
        ("aap_labeling_v1", "video_execution_checkpoint_mask_sets"),
        ("aap_labeling_v1", "video_writer_receipts"),
        ("aap_inference_routing_v1", "video_execution_scope_permits"),
        ("aap_inference_backend_endpoint_v1", "endpoint_secret"),
    }
    | {
        ("aap_inference_routing_v1", name)
        for name in (
            "video_restore_records",
            "video_restore_contexts",
            "video_restore_scopes",
            "video_restore_attempts",
            "video_restore_scope_retirements",
            "video_restore_no_scope_receipts",
            "video_restore_function_restore",
        )
    }
)


def include_orm_schema_object(object_, name, type_, reflected, compare_to):
    """Native SQL migrations remain the sole schema owner for these relations."""
    return not (
        type_ == "table"
        and (getattr(object_, "schema", None) or "public", name)
        in NATIVE_MANAGED_RELATIONS
    )
