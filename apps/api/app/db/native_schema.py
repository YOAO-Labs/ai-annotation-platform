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
    }
)


def include_orm_schema_object(object_, name, type_, reflected, compare_to):
    """Native SQL migrations remain the sole schema owner for these relations."""
    return not (
        type_ == "table"
        and getattr(object_, "schema", None) in {None, "public"}
        and name in NATIVE_MANAGED_TABLES
    )
