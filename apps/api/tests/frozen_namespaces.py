"""Translate current YOAO database names back to frozen AAP artifact names.

Frozen migrations, packaged SQL copies and goldens keep the spelling they were
installed with. Only tests that compare a current contract document or live
catalog against such a frozen artifact use this module; runtime code must never
depend on it, and it is not a compatibility alias for the database.
"""

from __future__ import annotations

import re
from typing import Any

_SCHEMAS = (
    "assets_v1",
    "audit_v1",
    "export_v1",
    "identity_accounts_v1",
    "identity_admin_passwords_v1",
    "identity_deactivation_v1",
    "identity_deletions_v1",
    "identity_directory_v1",
    "identity_fixture_v1",
    "identity_groups_v1",
    "identity_invitations_v1",
    "identity_mail_v2",
    "identity_offboarding_v1",
    "identity_platform_roles_v1",
    "identity_recovery_v1",
    "identity_sessions_v1",
    "identity_suspensions_v1",
    "identity_v1",
    "inference_backend_endpoint_v1",
    "inference_backend_probe_v1",
    "inference_gpu_v1",
    "inference_health_data_v1",
    "inference_health_observation_v1",
    "inference_pipelines_v1",
    "inference_registry_ingress_v1",
    "inference_routing_v1",
    "inference_topology_v1",
    "inference_v1",
    "jobs_v1",
    "labeling_v1",
    "learning_v1",
    "notifications_v1",
    "settings_v1",
    "workspace_configuration_v1",
    "workspace_fixture_v1",
    "workspace_v1",
)

_OPTIONAL_SCHEMAS = (
    "identity_audit_v1",
    "labeling_audit_v1",
    "workspace_audit_v1",
)

_LOGINS = (
    "append_client",
    "asset_catalog_client",
    "asset_cleanup_client",
    "asset_image_worker_client",
    "asset_import_client",
    "asset_media_control_client",
    "asset_media_worker_client",
    "asset_operator_client",
    "asset_source_client",
    "audit_append_client",
    "audit_maintenance_client",
    "audit_query_client",
    "audit_read_client",
    "export_execution_client",
    "identity_account_client",
    "identity_actor_status_client",
    "identity_admin_password_client",
    "identity_auth_client",
    "identity_command_client",
    "identity_deactivation_client",
    "identity_deactivation_operator",
    "identity_deletion_client",
    "identity_directory_client",
    "identity_fence_client",
    "identity_group_client",
    "identity_invitation_client",
    "identity_mail_client",
    "identity_mail_settings_client",
    "identity_mail_v2_client",
    "identity_offboarding_client",
    "identity_platform_role_client",
    "identity_presence_client",
    "identity_provisioning_client",
    "identity_recovery_client",
    "identity_session_client",
    "identity_session_operator",
    "identity_suspension_client",
    "inference_backend_probe_client",
    "inference_execution_client",
    "inference_health_data_client",
    "inference_health_observation_client",
    "inference_health_origin_client",
    "inference_pipelines_client",
    "inference_pool_reference_client",
    "inference_registry_ingress_client",
    "inference_routing_client",
    "jobs_lifecycle_client",
    "jobs_list_query_client",
    "jobs_notification_scope_client",
    "jobs_publication_client",
    "labeling_annotation_maintenance_client",
    "labeling_annotation_transfer_client",
    "labeling_annotation_write_client",
    "labeling_batch_control_client",
    "labeling_core_read_client",
    "labeling_cross_frame_client",
    "labeling_discussion_client",
    "labeling_editing_client",
    "labeling_mask_edit_client",
    "labeling_mask_gc_client",
    "labeling_mask_quality_client",
    "labeling_prediction_client",
    "labeling_scene_read_client",
    "labeling_scene_track_command_client",
    "labeling_spatial_quality_client",
    "labeling_task_control_client",
    "labeling_task_maintenance_client",
    "labeling_video_control_client",
    "labeling_video_writer_client",
    "notification_append_client",
    "notification_delivery_read_client",
    "notification_inbox_query_client",
    "notification_preferences_client",
    "notification_publish_client",
    "preannotation_result_client",
    "video_execution_client",
    "video_restore_operator_client",
    "workspace_configuration_client",
    "workspace_dataset_unlink_client",
    "workspace_project_deletion_client",
    "workspace_project_maintenance_client",
    "workspace_scale_seed_client",
    "audit_daily_refresh_client",
    "inference_backend_stats_client",
    "inference_endpoint_catalog_client",
    "inference_endpoint_execution_client",
    "inference_endpoint_health_client",
    "inference_endpoint_lifecycle_client",
    "inference_endpoint_read_client",
    "inference_endpoint_setup_client",
    "inference_endpoint_stats_client",
    "inference_gpu_authority_client",
    "inference_gpu_control_endpoint_client",
    "inference_gpu_health_endpoint_client",
    "jobs_dispatch_relay_client",
    "notification_read_client",
)

_GUCS = (
    "restore_training_storage",
    "identity_offboarding_prepare",
    "mask_gc_write_permit",
)

_CURRENT_TO_HISTORICAL = {
    **{f"yoao_{name}": f"aap_{name}" for name in _SCHEMAS + _OPTIONAL_SCHEMAS},
    **{f"yoao_{name}": name for name in _LOGINS},
    **{f"yoao.{name}": f"aap.{name}" for name in _GUCS},
}
_ORDERED = sorted(
    _CURRENT_TO_HISTORICAL.items(), key=lambda item: len(item[0]), reverse=True
)
_HISTORICAL_TO_CURRENT = {
    frozen: current for current, frozen in _CURRENT_TO_HISTORICAL.items()
}
_ORDERED_HISTORICAL = sorted(
    _HISTORICAL_TO_CURRENT.items(), key=lambda item: len(item[0]), reverse=True
)


def _compile(ordered: list[tuple[str, str]]) -> tuple[re.Pattern[str], dict[str, str]]:
    pattern = re.compile("|".join(re.escape(source) for source, _ in ordered))
    return pattern, dict(ordered)


_HISTORICAL_PATTERN, _HISTORICAL_MAP = _compile(_ORDERED)
_CURRENT_PATTERN, _CURRENT_MAP = _compile(_ORDERED_HISTORICAL)


def historical(value: Any) -> Any:
    """Return ``value`` with current database names spelled as frozen artifacts do."""
    return _translate(value, _HISTORICAL_PATTERN, _HISTORICAL_MAP)


def current(value: Any) -> Any:
    """Return a frozen artifact name translated to its current database spelling."""
    return _translate(value, _CURRENT_PATTERN, _CURRENT_MAP)


def _translate(value: Any, pattern: re.Pattern[str], mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        return pattern.sub(lambda match: mapping[match.group(0)], value)
    if isinstance(value, list):
        return [_translate(item, pattern, mapping) for item in value]
    if isinstance(value, dict):
        return {
            _translate(key, pattern, mapping): _translate(item, pattern, mapping)
            for key, item in value.items()
        }
    return value
