"""Run the module-data LOGIN-role acceptance against owned local resources.

Usage: apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
    --output /tmp/aap-module-data-acceptance.json

The JSON report is paired with a sibling JUnit XML file. Only this invocation's
PostgreSQL container and LocalResources test topology are created or removed.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from uuid import uuid4
import shutil
from urllib.parse import quote, urlsplit
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))

CONTAINER_LABEL = "org.aap.acceptance"
CONTAINER_LABEL_VALUE = "module-data-boundary"
CONTAINER_ID_LABEL = "org.aap.acceptance-id"
POSTGRES_IMAGE = "postgres:16-alpine"
IDENTITY_CLIENT_ROLES = {
    "AUTH": "identity_auth_client",
    "COMMAND": "identity_command_client",
    "FENCE": "identity_fence_client",
    "OPERATOR": "identity_session_operator",
    "KEYS": "identity_key_client",
    "SESSIONS": "identity_session_client",
    "ACCOUNTS": "identity_account_client",
    "GROUP": "identity_group_client",
    "INVITATION": "identity_invitation_client",
    "DEACTIVATION": "identity_deactivation_client",
    "DEACTIVATION_OPERATOR": "identity_deactivation_operator",
    "NOTIFICATION_PUBLISH": "notification_publish_client",
    "PRESENCE": "identity_presence_client",
    "RECOVERY": "identity_recovery_client",
    "MAIL": "identity_mail_client",
    "MAIL_V2": "identity_mail_v2_client",
    "MAIL_SETTINGS": "identity_mail_settings_client",
    "ADMIN_PASSWORD": "identity_admin_password_client",
    "PLATFORM_ROLE": "identity_platform_role_client",
    "SUSPENSION": "identity_suspension_client",
    "DELETION": "identity_deletion_client",
    "OFFBOARDING": "identity_offboarding_client",
    "DIRECTORY": "identity_directory_client",
    "BRIEFS": "identity_briefs_client",
}
MODULE_ROLES = (
    "identity_code",
    "workspace_code",
    "assets_code",
    "labeling_code",
    "jobs_code",
    "learning_code",
    "inference_code",
    "insights_code",
    "diagnostics_code",
    "export_code",
    "audit_code",
    "settings_code",
    "notifications_code",
    "append_client",
    "asset_cleanup_client",
    "asset_catalog_client",
    "asset_source_client",
    "asset_import_client",
    "asset_operator_client",
    "asset_media_control_client",
    "asset_media_worker_client",
    "asset_image_worker_client",
    "labeling_mask_gc_client",
    "preannotation_result_client",
    *IDENTITY_CLIENT_ROLES.values(),
)
SQL_ARTIFACTS = (
    "0190_workspace_data_contract.sql",
    "0191_dataset_task_append_contract.sql",
    "0192_creation_authority.sql",
    "0193_task_creation_jobs.sql",
    "0194_initial_task_creation.sql",
    "0195_task_append_timestamp.sql",
    "0196_project_deletion_retention.sql",
    "0197_project_labeling_delete.sql",
    "0198_project_owned_cleanup_contract.sql",
    "0199_project_deletion.sql",
    "0200_deleted_project_job_scope.sql",
    "0201_dataset_source_retention.sql",
    "0202_dataset_unlink_labeling.sql",
    "0203_dataset_unlink_assets.sql",
    "0204_dataset_unlink_jobs_audit.sql",
    "0205_dataset_unlink.sql",
    "0206_dataset_source_guards.sql",
    "0207_dataset_upload_intents.sql",
    "0208_dataset_source_support.sql",
    "0209_dataset_source_deletion.sql",
    "0210_asset_storage_cleanup.sql",
    "0211_dataset_catalog.sql",
    "0212_asset_association_reads.sql",
    "0213_assets_source_runtime.sql",
    "0214_dataset_import_delivery.sql",
    "0214_dataset_import_support.sql",
    "0214_dataset_import_core.sql",
    "0215_dataset_source_ingest.sql",
    "0215_dataset_source_ingest_delivery.sql",
    "0216_assets_spatial_authorities.sql",
    "0216_assets_spatial.sql",
    "0216_assets_spatial_projections.sql",
    "0217_assets_connections_support.sql",
    "0217_assets_connections.sql",
    "0218_assets_operator.sql",
    "0219_media_authorities.sql",
    "0219_assets_media_state.sql",
    "0219_assets_media_control.sql",
    "0219_assets_media_execution.sql",
    "0220_image_authorities.sql",
    "0220_assets_images_state.sql",
    "0220_assets_images_control.sql",
    "0220_assets_images_execution.sql",
    "0220_assets_images_garbage.sql",
    "0220_image_cleanup.sql",
    "0220_image_sources.sql",
    "0221_accepted_inputs_state.sql",
    "0221_accepted_inputs_authorities.sql",
    "0221_accepted_inputs_control.sql",
    "0221_accepted_inputs_execution.sql",
    "0221_accepted_inputs_retention.sql",
    "0221_accepted_inputs_garbage.sql",
    "0222_accepted_input_manifests_state.sql",
    "0222_accepted_input_manifests_authorities.sql",
    "0222_accepted_input_manifests_control.sql",
    "0223_video_input_authorities.sql",
    "0224_video_input_selection.sql",
    "0225_video_inputs_state.sql",
    "0225_video_inputs_control.sql",
    "0225_video_inputs_garbage.sql",
    "0225_video_inputs_execution.sql",
    "0225_video_inputs_retention.sql",
    "0226_video_input_claim_authority.sql",
    "0227_scene_image_input_manifests.sql",
    "0228_video_tracker_inputs.sql",
    "0229_mask_gc.sql",
    "0230_dataset_source_aux_retention.sql",
    "0231_preannotation_results.sql",
    "0231_preannotation_result_mask_roots.sql",
    "0232_tracker_running_mask_roots.sql",
    "0233_prediction_retry_state.sql",
    "0233_prediction_retry_result_scope.sql",
    "0233_prediction_retry_finalization.sql",
    "0234_mask_gc_preview.sql",
    "0235_identity_admission.sql",
    "0236_identity_api_keys.sql",
    "0237_identity_sessions.sql",
    "0238_identity_accounts.sql",
    "0239_identity_recovery.sql",
    "0240_identity_groups.sql",
    "0241_deactivation_notifications.sql",
    "0241_identity_deactivation.sql",
    "0242_identity_invitations_support.sql",
    "0242_invitation_mail.sql",
    "0242_identity_invitations.sql",
    "0243_identity_directory_support.sql",
    "0243_identity_directory.sql",
    "0244_identity_admin_passwords_support.sql",
    "0244_identity_admin_passwords.sql",
    "0245_identity_platform_roles_support.sql",
    "0245_identity_platform_roles.sql",
    "0246_identity_suspensions_support.sql",
    "0246_identity_suspensions.sql",
    "0247_identity_deletions_support.sql",
    "0247_identity_deletions.sql",
    "0248_identity_offboarding_support.sql",
    "0248_identity_offboarding.sql",
    "0249_identity_briefs_support.sql",
    "0249_identity_briefs.sql",
    "0250_identity_actor_context.sql",
)
SCHEMA_ARTIFACTS = (
    "0191_task_append.schema.json",
    "0192_creation_authority.schema.json",
    "0193_task_creation_jobs.schema.json",
    "0194_task_creation.schema.json",
    "0196_project_deletion_retention.schema.json",
    "0197_project_labeling_delete.schema.json",
    "0198_project_owned_cleanup_contract.schema.json",
    "0199_project_deletion.schema.json",
    "0201_dataset_source_retention.schema.json",
    "0202_dataset_unlink_labeling.schema.json",
    "0203_dataset_unlink_assets.schema.json",
    "0204_dataset_unlink_jobs_audit.schema.json",
    "0205_dataset_unlink.schema.json",
    "0206_dataset_source_guards.schema.json",
    "0207_dataset_upload_intents.schema.json",
    "0208_dataset_source_support.schema.json",
    "0209_dataset_source_deletion.schema.json",
    "0210_asset_storage_cleanup.schema.json",
    "0211_dataset_catalog.schema.json",
    "0212_asset_association_reads.schema.json",
    "0213_assets_source_runtime.schema.json",
    "0214_dataset_import.schema.json",
    "0214_dataset_import_delivery.schema.json",
    "0215_dataset_source_ingest.schema.json",
    "0216_assets_spatial.schema.json",
    "0217_assets_connections.schema.json",
    "0218_assets_operator.schema.json",
    "0219_assets_media.schema.json",
    "0220_assets_images.schema.json",
    "0221_accepted_inputs.schema.json",
    "0222_accepted_input_manifests.schema.json",
    "0223_video_input_authorities.schema.json",
    "0224_video_input_selection.schema.json",
    "0225_video_inputs.schema.json",
    "0225_video_input_evidence.schema.json",
    "0228_video_tracker_inputs.schema.json",
    "0229_mask_gc.schema.json",
    "0230_dataset_source_aux_retention.schema.json",
    "0231_preannotation_results.schema.json",
    "0233_prediction_retries.schema.json",
    "0233_preannotation_result_scope.schema.json",
    "0234_mask_gc.schema.json",
    "0235_identity_admission.schema.json",
    "0236_identity_api_keys.schema.json",
    "0237_identity_sessions.schema.json",
    "0238_identity_accounts.schema.json",
    "0239_identity_recovery.schema.json",
    "0240_identity_groups.schema.json",
    "0241_identity_deactivation.schema.json",
    "0242_identity_invitations.schema.json",
    "0243_identity_directory.schema.json",
    "0244_identity_admin_passwords.schema.json",
    "0245_identity_platform_roles.schema.json",
    "0245_platform_role_workspace_facts.schema.json",
    "0245_platform_role_labeling_facts.schema.json",
    "0246_identity_suspensions.schema.json",
    "0247_identity_deletions.schema.json",
    "0248_identity_offboarding.schema.json",
    "0249_identity_briefs.schema.json",
    "0250_identity_actor_context.schema.json",
)
TEST_FILES = (
    "tests/test_identity_admin_passwords_native.py",
    "tests/test_identity_admin_passwords_roles.py",
    "tests/test_identity_admin_passwords_restore.py",
    "tests/test_identity_platform_roles_native.py",
    "tests/test_identity_platform_roles_roles.py",
    "tests/test_identity_platform_roles_restore.py",
    "tests/test_identity_suspensions_native.py",
    "tests/test_identity_suspensions_roles.py",
    "tests/test_identity_suspensions_restore.py",
    "tests/test_identity_deletions_native.py",
    "tests/test_identity_deletions_roles.py",
    "tests/test_identity_deletions_restore.py",
    "tests/test_identity_offboarding_native.py",
    "tests/test_identity_offboarding_roles.py",
    "tests/test_identity_offboarding_restore.py",
    "tests/test_identity_briefs_native.py",
    "tests/test_identity_briefs_roles.py",
    "tests/test_identity_briefs_restore.py",
    "tests/test_identity_actor_context_native.py",
    "tests/test_users_role_matrix.py::TestRoleMatrix::test_sa_cannot_demote_last_super_admin",
    "tests/test_identity_directory_native.py",
    "tests/test_identity_directory_roles.py",
    "tests/test_identity_invitations_native.py",
    "tests/test_identity_invitations_roles.py",
    "tests/test_identity_invitation_gateway.py",
    "tests/test_identity_mail_v2_gateway.py",
    "tests/test_identity_deactivation_native.py",
    "tests/test_identity_deactivation_roles.py",
    "tests/test_identity_groups_native.py",
    "tests/test_identity_groups_roles.py",
    "tests/test_identity_challenges_native.py",
    "tests/test_identity_challenges_roles.py",
    "tests/test_identity_credential_mail_native.py",
    "tests/test_identity_recovery_gateway.py",
    "tests/test_identity_accounts_native.py",
    "tests/test_identity_accounts_roles.py",
    "tests/test_identity_sessions_native.py",
    "tests/test_identity_sessions_roles.py",
    "tests/test_identity_auth_native.py",
    "tests/test_identity_auth_roles.py",
    "tests/test_workspace_data_contract.py",
    "tests/test_task_append_data_contract.py",
    "tests/test_task_append_consumer.py",
    "tests/test_creation_authority_contract.py",
    "tests/test_task_creation_job_contract.py",
    "tests/test_initial_task_creation_contract.py",
    "tests/test_task_creation_bindings.py",
    "tests/test_task_creation_runtime.py",
    "tests/test_task_creation_handoff.py",
    "tests/test_project_owned_cleanup_contract.py",
    "tests/test_project_labeling_delete_contract.py",
    "tests/test_project_deletion_retention.py",
    "tests/test_project_deletion_bindings.py",
    "tests/test_deleted_project_job_scope.py",
    "tests/test_project_deletion_data.py",
    "tests/test_model_lineage.py",
    "tests/test_project_task_datasets.py",
    "tests/test_sample_evaluations.py",
    "tests/test_async_jobs.py",
    "tests/test_async_job_actor_freshness.py",
    "tests/test_notifications.py",
    "tests/test_notification_delivery_scope.py",
    "tests/test_pointcloud_box3d_annotation.py",
    "tests/test_pointcloud_import.py",
    "tests/test_pointcloud_manifest.py",
    "tests/test_pointcloud_scene_e2e.py",
    "tests/test_module_data_roles.py",
    "tests/test_datasets_upload_zip.py",
    "tests/test_scene_inference.py",
    "tests/test_mask_formats_m6.py",
    "tests/test_assets_source_ingest.py",
    "tests/test_dataset_source_ingest_native.py",
    "tests/test_dataset_source_ingest_runtime.py",
    "tests/test_dataset_source_ingest_delivery.py",
    "tests/test_dataset_import.py",
    "tests/test_dataset_import_native.py",
    "tests/test_dataset_import_migration.py",
    "tests/test_assets_import_bindings.py",
    "tests/test_dataset_import_cancellation.py",
    "tests/test_dataset_import_runtime.py",
    "tests/test_dataset_import_delivery.py",
    "tests/test_dataset_import_delivery_native.py",
    "tests/test_dataset_link.py",
    "tests/test_dataset_link_kind_match.py",
    "tests/test_dataset_source_deletion_bindings.py",
    "tests/test_dataset_source_deletion.py",
    "tests/test_dataset_source_http.py",
    "tests/test_dataset_source_guards.py",
    "tests/test_dataset_source_aux_retention.py",
    "tests/test_dataset_source_support.py",
    "tests/test_dataset_upload_data.py",
    "tests/test_dataset_upload_native.py",
    "tests/test_dataset_upload_storage.py",
    "tests/test_dataset_upload_s3_runtime.py",
    "tests/test_dataset_source_retention.py",
    "tests/test_dataset_unlink_labeling.py",
    "tests/test_dataset_unlink_assets.py",
    "tests/test_dataset_unlink_jobs_audit.py",
    "tests/test_dataset_unlink_coordinator.py",
    "tests/test_dataset_unlink_bindings.py",
    "tests/test_dataset_unlink_http.py",
    "tests/test_asset_storage_cleanup_runtime.py",
    "tests/test_assets_source_native.py",
    "tests/test_assets_source_runtime.py",
    "tests/test_assets_source_gateway.py",
    "tests/test_assets_catalog_native.py",
    "tests/test_assets_spatial_native.py",
    "tests/test_assets_spatial_calibration_native.py",
    "tests/test_assets_spatial_native_peer.py",
    "tests/test_assets_spatial_preparation.py",
    "tests/test_assets_spatial_concurrency.py",
    "tests/test_assets_spatial_receipt_native.py",
    "tests/test_assets_spatial_workflow_native.py",
    "tests/test_assets_spatial_pose_pairs_native.py",
    "tests/test_storage_connections_native.py",
    "tests/test_storage_connections_peer.py",
    "tests/test_assets_operator_native.py",
    "tests/test_assets_operator_peer.py",
    "tests/test_assets_operator_runtime.py",
    "tests/test_assets_media_native.py",
    "tests/test_assets_media_control_native.py",
    "tests/test_image_source_authorities.py",
    "tests/test_assets_images_sources.py",
    "tests/test_assets_images_control.py",
    "tests/test_assets_images_execution.py",
    "tests/test_assets_images_retention.py",
    "tests/test_image_cleanup_native.py",
    "tests/test_image_thumbnail_publication.py",
    "tests/test_image_pyramid.py",
    "tests/test_image_backfill_maintenance.py",
    "tests/test_image_worker_compatibility.py",
    "tests/test_media_celery_loops.py",
    "tests/test_assets_images_api_bridge.py",
    "tests/test_assets_images_live.py",
    "tests/test_assets_images_containers.py",
    "tests/test_assets_images_migration_contract.py",
    "tests/test_assets_images_migration_guards.py",
    "tests/test_assets_accepted_inputs_migration.py",
    "tests/test_assets_input_manifests_migration.py",
    "tests/test_assets_input_manifests_native.py",
    "tests/test_scene_image_input_manifests_migration.py",
    "tests/test_scene_image_input_manifests_native.py",
    "tests/test_video_input_authority_migration.py",
    "tests/test_video_input_authorities_native.py",
    "tests/test_video_input_authority_roles.py",
    "tests/test_video_input_selection_migration.py",
    "tests/test_video_input_selection_native.py",
    "tests/test_video_input_selection_roles.py",
    "tests/test_video_input_ledger_state.py",
    "tests/test_video_input_ledger_migration.py",
    "tests/test_video_input_ledger_control.py",
    "tests/test_video_input_ledger_execution.py",
    "tests/test_video_input_ledger_retention.py",
    "tests/test_video_input_ledger_lifecycle.py",
    "tests/test_video_input_ledger_roles.py",
    "tests/test_video_input_claim_authority.py",
    "tests/test_video_tracker_input_migration.py",
    "tests/test_video_tracker_input_native.py",
    "tests/test_video_tracker_input_lifecycle.py",
    "tests/test_video_tracker_input_roles.py",
    "tests/test_video_input_s3_scope.py",
    "tests/test_native_schema_autogeneration.py",
    "tests/test_accepted_input_preannotation.py",
    "tests/test_preannotation_result_consumer.py",
    "tests/test_preannotation_results_native.py",
    "tests/test_preannotation_results_migration.py",
    "tests/test_preannotation_result_roles.py",
    "tests/test_batch_result_consumers_native.py",
    "tests/test_batch_result_native_peer.py",
    "tests/test_preannotation_result_mask_roots.py",
    "tests/test_batch_result_reset.py",
    "tests/test_batch_result_worker_live.py",
    "tests/test_preannotation_results_runtime_image.py",
    "tests/test_prediction_retry_input_deferral.py",
    "tests/test_prediction_retry_native_migration.py",
    "tests/test_prediction_retry_legacy_migration.py",
    "tests/test_prediction_retries_native.py",
    "tests/test_tracker_running_mask_roots.py",
    "tests/test_tracker_running_mask_roots_native.py",
    "tests/test_migration_0232_tracker_running_mask_roots.py",
    "tests/test_raster_mask_lock_boundary.py",
    "tests/test_mask_gc_native.py",
    "tests/test_mask_gc_protocol_native.py",
    "tests/test_mask_gc_migration.py",
    "tests/test_mask_gc_roles.py",
    "tests/test_mask_gc_bindings.py",
    "tests/test_mask_gc_live.py",
    "tests/test_mask_gc_preview_native.py",
    "tests/test_mask_gc_preview_authority.py",
    "tests/test_mask_gc_cutover_peer.py",
    "tests/test_mask_gc_collection_live.py",
    "tests/test_mask_gc_runtime_image.py",
    "tests/test_assets_accepted_inputs_native.py",
    "tests/test_accepted_inputs_api_bridge.py",
    "tests/test_accepted_inputs_live.py",
    "tests/test_video_input_live.py",
    "tests/test_screenshot_seed_catalog.py",
    "tests/test_task_views.py",
    "tests/test_task_views_filter.py",
    "tests/test_tasks_drill_filters.py",
    "tests/test_tasks_list_sequence_order.py",
    "tests/test_assets_catalog_gateway.py",
    "tests/test_assets_media_peer.py",
    "tests/test_assets_media_live.py",
    "tests/test_assets_media_containers.py",
    "tests/test_assets_spatial_gateway.py",
    "tests/test_assets_spatial_runtime.py",
    "tests/test_assets_catalog_runtime.py",
    "tests/test_asset_association_reads.py",
    "tests/test_asset_association_runtime.py",
    "tests/test_assets_associations_gateway.py",
    "tests/test_dataset_api_scopes.py",
    "tests/test_datasets_list_has_scenes.py",
    "tests/test_dataset_axis_convention.py",
    "tests/test_tool_bindings_helpers.py",
    "tests/test_worker_signals.py",
    "tests/test_security_headers.py",
    "tests/test_create_tasks_worker.py",
    "tests/test_batch_delete_force.py",
    "tests/test_batch_delete_reset.py",
    "tests/test_batch_split_by_scene.py",
    "tests/test_batch_split_semantics.py",
    "tests/test_project_scene_mode.py",
    "tests/test_operational_ingest_boundaries.py",
    "tests/test_storage_connections_api.py",
)


def _test_file(value: str) -> str:
    relative = Path(value.split("::", 1)[0])
    path = (ROOT / "apps/api" / relative).resolve()
    if (
        relative.is_absolute()
        or not path.is_relative_to(ROOT / "apps/api/tests")
        or path.suffix != ".py"
        or not path.is_file()
    ):
        raise argparse.ArgumentTypeError("Select an existing file under apps/api/tests")
    return value


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create one labeled disposable PostgreSQL instance, prepare the owned "
            "test-mode resources, and run the module-data role acceptance."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="evidence JSON path; a sibling .junit.xml report is written by default",
    )
    parser.add_argument(
        "--junit-xml",
        type=Path,
        help="explicit JUnit XML path (defaults beside --output)",
    )
    parser.add_argument(
        "--test-file",
        dest="test_files",
        action="append",
        type=_test_file,
        metavar="TEST_NODEID",
        help="select an apps/api test file or node ID; repeat to replace the default suite",
    )
    return parser.parse_args(argv)


def _write_evidence(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _passed_test_count(junit: Path) -> int:
    return sum(
        all(case.find(state) is None for state in ("skipped", "failure", "error"))
        for case in ET.parse(junit).findall(".//testcase")
    )


def _cleanup_storage_scope(backend) -> dict:
    from worktree_resources import docker

    endpoint = urlsplit(f"http://{backend.settings.minio_endpoint}")
    matches = []
    for candidate in docker(
        "ps",
        "--filter",
        "label=com.docker.compose.service=minio",
        "--format",
        "{{.ID}}",
    ).splitlines():
        observed = json.loads(docker("inspect", candidate))[0]
        bindings = (observed.get("NetworkSettings", {}).get("Ports") or {}).get(
            "9000/tcp"
        ) or []
        if any(
            int(binding.get("HostPort", 0)) == (endpoint.port or 9000)
            for binding in bindings
        ):
            matches.append(observed)
    if len(matches) != 1:
        raise RuntimeError("The local MinIO admin container is not uniquely identified")
    bucket = backend.resources["buckets"]["MINIO_MEDIA_CACHE_BUCKET"]
    tags = backend.s3.get_bucket_tagging(Bucket=bucket)["TagSet"]
    if not any(
        tag["Key"] == "aap-worktree-owner"
        and tag["Value"] == backend.resources["owner"]
        for tag in tags
    ):
        raise RuntimeError("The cleanup bucket is not owned by this acceptance")
    inputs_bucket = backend.resources["buckets"]["MINIO_INPUTS_BUCKET"]
    input_tags = backend.s3.get_bucket_tagging(Bucket=inputs_bucket)["TagSet"]
    if not any(
        tag["Key"] == "aap-worktree-owner"
        and tag["Value"] == backend.resources["owner"]
        for tag in input_tags
    ):
        raise RuntimeError("The accepted-input bucket is not owned by this acceptance")
    versioning = backend.s3.get_bucket_versioning(Bucket=inputs_bucket)
    if versioning.get("Status") != "Enabled":
        raise RuntimeError("The accepted-input bucket is not versioned")
    token = secrets.token_hex(8)
    scheme = "https" if backend.settings.minio_use_ssl else "http"
    return {
        "container": matches[0]["Id"],
        "bucket": bucket,
        "inputs_bucket": inputs_bucket,
        "access_key": f"aapcln{token[:14]}",
        "secret_key": secrets.token_hex(24),
        "policy": f"aap-cleanup-{token}",
        "config": f"/tmp/aap-cleanup-{token}",
        "host": f"{scheme}://{quote(backend.settings.minio_access_key, safe='')}:{quote(backend.settings.minio_secret_key, safe='')}@127.0.0.1:9000",
        "user_created": False,
        "policy_created": False,
    }


def _restricted_s3_client(backend, scope: dict):
    import boto3
    from botocore.config import Config

    scheme = "https" if backend.settings.minio_use_ssl else "http"
    return boto3.client(
        "s3",
        endpoint_url=f"{scheme}://{backend.settings.minio_endpoint}",
        aws_access_key_id=scope["access_key"],
        aws_secret_access_key=scope["secret_key"],
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            connect_timeout=3,
            read_timeout=10,
            retries={"max_attempts": 1},
            s3={"addressing_style": "path"},
        ),
    )


def _expect_s3_denied(label: str, operation) -> None:
    from botocore.exceptions import ClientError

    try:
        operation()
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {
            "AccessDenied",
            "Forbidden",
            "AllAccessDisabled",
        }:
            return
        raise RuntimeError(f"{label} returned an unexpected S3 error") from None
    raise RuntimeError(f"{label} was unexpectedly allowed")


def _verify_accepted_input_s3_scopes(
    backend, control_scope, worker_scope, cleanup_scope
):
    """Exercise version-bound input access with the three restricted S3 identities."""
    from botocore.exceptions import ClientError

    bucket = backend.resources["buckets"]["MINIO_INPUTS_BUCKET"]
    if any(
        scope["inputs_bucket"] != bucket
        for scope in (control_scope, worker_scope, cleanup_scope)
    ):
        raise RuntimeError("The accepted-input S3 identities disagree on the bucket")
    state = backend.s3.get_bucket_versioning(Bucket=bucket)
    if state.get("Status") != "Enabled":
        raise RuntimeError("The owned accepted-input bucket is not versioned")
    media_cache = backend.resources["buckets"]["MINIO_MEDIA_CACHE_BUCKET"]
    cache_versioning_before = backend.s3.get_bucket_versioning(Bucket=media_cache).get(
        "Status"
    )

    control = _restricted_s3_client(backend, control_scope)
    worker = _restricted_s3_client(backend, worker_scope)
    cleanup = _restricted_s3_client(backend, cleanup_scope)
    input_id = str(uuid4())
    prefix = f"accepted-inputs/{input_id}/"
    attempt_a = f"{prefix}g1/a{uuid4()}/source"
    attempt_b = f"{prefix}g2/a{uuid4()}/source"
    multipart_key = f"{prefix}g3/a{uuid4()}/source"
    created_upload_id = None
    try:
        first = backend.s3.put_object(Bucket=bucket, Key=attempt_a, Body=b"accepted-v1")
        version_a = first.get("VersionId")
        if not version_a or version_a == "null":
            raise RuntimeError("The input bucket did not issue an object VersionId")

        if control.get_bucket_versioning(Bucket=bucket).get("Status") != "Enabled":
            raise RuntimeError("The control S3 identity did not read bucket versioning")
        body = control.get_object(Bucket=bucket, Key=attempt_a, VersionId=version_a)[
            "Body"
        ]
        try:
            if body.read() != b"accepted-v1":
                raise RuntimeError(
                    "The control S3 identity read the wrong input version"
                )
        finally:
            body.close()
        _expect_s3_denied(
            "control input write",
            lambda: control.put_object(Bucket=bucket, Key=attempt_b, Body=b"forbidden"),
        )
        _expect_s3_denied(
            "control unversioned input read",
            lambda: control.get_object(Bucket=bucket, Key=attempt_a),
        )
        _expect_s3_denied(
            "control input version listing",
            lambda: control.list_object_versions(Bucket=bucket, Prefix=prefix),
        )

        if worker.get_bucket_versioning(Bucket=bucket).get("Status") != "Enabled":
            raise RuntimeError(
                "The image Worker S3 identity did not read bucket versioning"
            )
        worker_body = worker.get_object(
            Bucket=bucket, Key=attempt_a, VersionId=version_a
        )["Body"]
        try:
            if worker_body.read() != b"accepted-v1":
                raise RuntimeError("The image Worker read the wrong input version")
        finally:
            worker_body.close()
        _expect_s3_denied(
            "image Worker unversioned input read",
            lambda: worker.get_object(Bucket=bucket, Key=attempt_a),
        )
        second = worker.put_object(
            Bucket=bucket, Key=attempt_b, Body=b"accepted-v2", IfNoneMatch="*"
        )
        version_b = second.get("VersionId")
        if not version_b or version_b == "null":
            raise RuntimeError("The image Worker PUT did not receive a VersionId")
        verified = worker.head_object(Bucket=bucket, Key=attempt_b, VersionId=version_b)
        if verified.get("ContentLength") != len(b"accepted-v2"):
            raise RuntimeError(
                "The image Worker could not verify its exact input version"
            )
        try:
            worker.put_object(
                Bucket=bucket, Key=attempt_b, Body=b"overwrite", IfNoneMatch="*"
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {
                "PreconditionFailed",
                "412",
            }:
                raise RuntimeError(
                    "The input conditional PUT returned an unexpected error"
                ) from None
        else:
            raise RuntimeError(
                "The conditional input PUT overwrote an existing attempt key"
            )
        _expect_s3_denied(
            "image Worker input version deletion",
            lambda: worker.delete_object(
                Bucket=bucket, Key=attempt_a, VersionId=version_a
            ),
        )
        _expect_s3_denied(
            "image Worker input version listing",
            lambda: worker.list_object_versions(Bucket=bucket, Prefix=prefix),
        )

        marker = backend.s3.delete_object(Bucket=bucket, Key=attempt_a)
        marker_id = marker.get("VersionId")
        if not marker_id or marker_id == "null":
            raise RuntimeError("The versioned input delete did not create a marker")

        upload = worker.create_multipart_upload(Bucket=bucket, Key=multipart_key)
        created_upload_id = upload["UploadId"]
        worker.upload_part(
            Bucket=bucket,
            Key=multipart_key,
            UploadId=created_upload_id,
            PartNumber=1,
            Body=b"x",
        )
        matching = []
        for _attempt in range(20):
            uploads = cleanup.list_multipart_uploads(Bucket=bucket, Prefix=prefix).get(
                "Uploads", []
            )
            if not uploads:
                uploads = cleanup.list_multipart_uploads(Bucket=bucket).get(
                    "Uploads", []
                )
            matching = [
                item
                for item in uploads
                if item.get("UploadId") == created_upload_id
                and item.get("Key") == multipart_key
            ]
            if matching:
                break
            time.sleep(0.1)
        if len(matching) != 1:
            raise RuntimeError(
                "The cleanup S3 identity could not enumerate input uploads"
            )
        cleanup.abort_multipart_upload(
            Bucket=bucket, Key=multipart_key, UploadId=created_upload_id
        )
        created_upload_id = None

        _expect_s3_denied(
            "cleanup input read",
            lambda: cleanup.get_object(
                Bucket=bucket, Key=attempt_b, VersionId=version_b
            ),
        )
        _expect_s3_denied(
            "cleanup unversioned input deletion",
            lambda: cleanup.delete_object(Bucket=bucket, Key=attempt_b),
        )
        versions_response = cleanup.list_object_versions(Bucket=bucket, Prefix=prefix)
        versions = [
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for item in versions_response.get("Versions", [])
        ]
        markers = [
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for item in versions_response.get("DeleteMarkers", [])
        ]
        if not {version_a, version_b, marker_id}.issubset(
            {item["VersionId"] for item in versions + markers}
        ):
            raise RuntimeError(
                "The cleanup S3 identity did not list all input versions"
            )
        for item in versions + markers:
            cleanup.delete_object(
                Bucket=bucket,
                Key=item["Key"],
                VersionId=item["VersionId"],
            )
        try:
            backend.s3.get_object(Bucket=bucket, Key=attempt_a, VersionId=version_a)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {
                "NoSuchVersion",
                "InvalidArgument",
                "NoSuchKey",
            }:
                raise RuntimeError(
                    "The input version remained after scoped cleanup"
                ) from None
        else:
            raise RuntimeError("The input version remained after scoped cleanup")
    finally:
        if created_upload_id:
            try:
                backend.s3.abort_multipart_upload(
                    Bucket=bucket, Key=multipart_key, UploadId=created_upload_id
                )
            except Exception:
                pass
        paginator = backend.s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            objects = [
                {"Key": item["Key"], "VersionId": item["VersionId"]}
                for item in page.get("Versions", []) + page.get("DeleteMarkers", [])
            ]
            if objects:
                backend.s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
    cache_versioning_after = backend.s3.get_bucket_versioning(Bucket=media_cache).get(
        "Status"
    )
    if cache_versioning_after != cache_versioning_before:
        raise RuntimeError("The accepted-input check changed media-cache versioning")


def _enable_owned_bucket_versioning(backend, bucket: str) -> None:
    tags = backend.s3.get_bucket_tagging(Bucket=bucket)["TagSet"]
    if not any(
        tag["Key"] == "aap-worktree-owner"
        and tag["Value"] == backend.resources["owner"]
        for tag in tags
    ):
        raise RuntimeError("The image source bucket is not owned by this acceptance")
    backend.s3.put_bucket_versioning(
        Bucket=bucket,
        VersioningConfiguration={"Status": "Enabled"},
    )
    state = backend.s3.get_bucket_versioning(Bucket=bucket)
    if state.get("Status") != "Enabled":
        raise RuntimeError("The owned image source bucket is not versioned")


def _cleanup_mc(
    scope: dict, arguments: list[str], *, payload: str | None = None
) -> None:
    from worktree_resources import docker

    observed = json.loads(docker("inspect", scope["container"]))[0]
    if not (
        observed.get("Id") == scope["container"]
        and observed.get("State", {}).get("Running") is True
        and (observed.get("Config", {}).get("Labels") or {}).get(
            "com.docker.compose.service"
        )
        == "minio"
    ):
        raise RuntimeError("The MinIO admin container identity changed")
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "-e",
            f"MC_HOST_scoped={scope['host']}",
            "-e",
            f"MC_CONFIG_DIR={scope['config']}",
            scope["container"],
            "/usr/bin/mc",
            "--json",
            *arguments,
        ],
        input=payload,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("MinIO cleanup identity administration failed")


def _cleanup_storage_policy(scope: dict) -> dict:
    cache_arn = f"arn:aws:s3:::{scope['bucket']}"
    inputs_arn = f"arn:aws:s3:::{scope['inputs_bucket']}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": [cache_arn],
                "Condition": {
                    "StringLike": {"s3:prefix": ["image-assets/*", "image-pyramids/*"]}
                },
            },
            {
                "Effect": "Allow",
                "Action": ["s3:DeleteObject"],
                "Resource": [
                    f"{cache_arn}/image-assets/*",
                    f"{cache_arn}/image-pyramids/*",
                ],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetBucketVersioning",
                    "s3:ListBucketMultipartUploads",
                ],
                "Resource": [inputs_arn],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:ListBucketVersions",
                ],
                "Resource": [inputs_arn],
                "Condition": {
                    "StringLike": {
                        "s3:prefix": ["accepted-inputs/*", "accepted-video-inputs/*"]
                    }
                },
            },
            {
                "Effect": "Allow",
                "Action": ["s3:DeleteObjectVersion", "s3:AbortMultipartUpload"],
                "Resource": [
                    f"{inputs_arn}/accepted-inputs/*",
                    f"{inputs_arn}/accepted-video-inputs/*",
                ],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:DeleteObject"],
                "Resource": [
                    f"{inputs_arn}/accepted-inputs/*",
                    f"{inputs_arn}/accepted-video-inputs/*",
                ],
                "Condition": {
                    "StringLike": {"s3:versionid": "*?*"},
                    "StringNotEquals": {"s3:versionid": "null"},
                },
            },
        ],
    }


def _require_input_version_id(statement: dict) -> dict:
    """Restrict version reads to a nonempty VersionId in the S3 policy context."""
    return {
        **statement,
        "Condition": {
            "StringLike": {"s3:versionid": "*?*"},
            "StringNotEquals": {"s3:versionid": "null"},
        },
    }


def _create_cleanup_storage_scope(scope: dict) -> None:
    _cleanup_mc(
        scope,
        ["admin", "user", "add", "scoped", scope["access_key"], scope["secret_key"]],
    )
    scope["user_created"] = True
    policy = _cleanup_storage_policy(scope)
    _cleanup_mc(
        scope,
        ["admin", "policy", "create", "scoped", scope["policy"], "/dev/stdin"],
        payload=json.dumps(policy),
    )
    scope["policy_created"] = True
    _cleanup_mc(
        scope,
        [
            "admin",
            "policy",
            "attach",
            "scoped",
            scope["policy"],
            "--user",
            scope["access_key"],
        ],
    )


def _destroy_cleanup_storage_scope(scope: dict) -> None:
    from worktree_resources import docker

    if scope["user_created"]:
        _cleanup_mc(scope, ["admin", "user", "remove", "scoped", scope["access_key"]])
        scope["user_created"] = False
    if scope["policy_created"]:
        _cleanup_mc(scope, ["admin", "policy", "remove", "scoped", scope["policy"]])
        scope["policy_created"] = False
    docker("exec", scope["container"], "rm", "-rf", "--", scope["config"])


def _create_api_bootstrap_storage_scope(scope: dict) -> None:
    """Allow legacy API startup metadata checks on only this run's ten buckets."""
    buckets = scope["buckets"]
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                "Resource": [f"arn:aws:s3:::{bucket}" for bucket in buckets.values()],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetBucketVersioning"],
                "Resource": [f"arn:aws:s3:::{buckets['MINIO_INPUTS_BUCKET']}"],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetLifecycleConfiguration",
                    "s3:PutLifecycleConfiguration",
                ],
                "Resource": [
                    f"arn:aws:s3:::{buckets[name]}"
                    for name in (
                        "MINIO_BUCKET",
                        "MINIO_BUG_REPORTS_BUCKET",
                        "MINIO_MEDIA_CACHE_BUCKET",
                        "MINIO_IMPORT_BUCKET",
                        "MINIO_EXPORT_BUCKET",
                    )
                ],
            },
        ],
    }
    _cleanup_mc(
        scope,
        ["admin", "user", "add", "scoped", scope["access_key"], scope["secret_key"]],
    )
    scope["user_created"] = True
    _cleanup_mc(
        scope,
        ["admin", "policy", "create", "scoped", scope["policy"], "/dev/stdin"],
        payload=json.dumps(policy),
    )
    scope["policy_created"] = True
    _cleanup_mc(
        scope,
        [
            "admin",
            "policy",
            "attach",
            "scoped",
            scope["policy"],
            "--user",
            scope["access_key"],
        ],
    )


def _create_catalog_storage_scope(scope: dict) -> None:
    _cleanup_mc(
        scope,
        ["admin", "user", "add", "scoped", scope["access_key"], scope["secret_key"]],
    )
    scope["user_created"] = True
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [
                    f"arn:aws:s3:::{scope['datasets_bucket']}/*",
                    *(
                        f"arn:aws:s3:::{scope['bucket']}/{prefix}/*"
                        for prefix in (
                            "thumbnails",
                            "videos",
                            "playback",
                            "image-pyramids",
                        )
                    ),
                ],
            }
        ],
    }
    _cleanup_mc(
        scope,
        ["admin", "policy", "create", "scoped", scope["policy"], "/dev/stdin"],
        payload=json.dumps(policy),
    )
    scope["policy_created"] = True
    _cleanup_mc(
        scope,
        [
            "admin",
            "policy",
            "attach",
            "scoped",
            scope["policy"],
            "--user",
            scope["access_key"],
        ],
    )


def _media_storage_policy(scope: dict, *, worker: bool) -> dict:
    cache_bucket_arn = f"arn:aws:s3:::{scope['bucket']}"
    cache_objects = f"{cache_bucket_arn}/media-assets/*"
    source_objects = [f"arn:aws:s3:::{scope['datasets_bucket']}/*"]
    if scope.get("legacy_bucket"):
        source_objects.append(f"arn:aws:s3:::{scope['legacy_bucket']}/*")
    statements = [
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:GetObjectVersion"],
            "Resource": source_objects,
        }
    ]
    inputs_bucket = scope.get("inputs_bucket")
    if inputs_bucket:
        inputs_arn = f"arn:aws:s3:::{inputs_bucket}"
        statements.extend(
            [
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetBucketVersioning"],
                    "Resource": [inputs_arn],
                },
                _require_input_version_id(
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetObjectVersion"],
                        "Resource": [f"{inputs_arn}/accepted-video-inputs/*"]
                        if worker
                        else [
                            f"{inputs_arn}/accepted-inputs/*",
                            f"{inputs_arn}/accepted-video-inputs/*",
                        ],
                    }
                ),
            ]
        )
        if worker:
            statements.append(
                {
                    "Effect": "Allow",
                    "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
                    "Resource": [f"{inputs_arn}/accepted-video-inputs/*"],
                }
            )
    if worker:
        statements.extend(
            [
                {
                    "Effect": "Allow",
                    "Action": ["s3:ListBucket"],
                    "Resource": [cache_bucket_arn],
                    "Condition": {"StringLike": {"s3:prefix": ["media-assets/*"]}},
                },
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                    "Resource": [cache_objects],
                },
                {
                    "Effect": "Allow",
                    "Action": ["s3:DeleteObject"],
                    "Resource": [
                        f"{cache_bucket_arn}/videos/*/frames/*",
                        f"{cache_bucket_arn}/videos/*/chunks/*",
                    ],
                },
            ]
        )
    else:
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [
                    cache_objects,
                    f"{cache_bucket_arn}/videos/*/frames/*",
                    f"{cache_bucket_arn}/videos/*/chunks/*",
                    f"{cache_bucket_arn}/playback/*.mp4",
                    f"{cache_bucket_arn}/image-assets/*",
                    f"{cache_bucket_arn}/image-pyramids/*",
                ],
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


def _create_mask_content_storage_scope(scope: dict, *, cleanup: bool) -> None:
    from mask_content_s3_policy import mask_content_policy

    _cleanup_mc(
        scope,
        ["admin", "user", "add", "scoped", scope["access_key"], scope["secret_key"]],
    )
    scope["user_created"] = True
    _cleanup_mc(
        scope,
        ["admin", "policy", "create", "scoped", scope["policy"], "/dev/stdin"],
        payload=json.dumps(mask_content_policy(scope["bucket"], cleanup=cleanup)),
    )
    scope["policy_created"] = True
    _cleanup_mc(
        scope,
        [
            "admin",
            "policy",
            "attach",
            "scoped",
            scope["policy"],
            "--user",
            scope["access_key"],
        ],
    )


def _create_media_storage_scope(scope: dict, *, worker: bool) -> None:
    _cleanup_mc(
        scope,
        ["admin", "user", "add", "scoped", scope["access_key"], scope["secret_key"]],
    )
    scope["user_created"] = True
    policy = _media_storage_policy(scope, worker=worker)
    _cleanup_mc(
        scope,
        ["admin", "policy", "create", "scoped", scope["policy"], "/dev/stdin"],
        payload=json.dumps(policy),
    )
    scope["policy_created"] = True
    _cleanup_mc(
        scope,
        [
            "admin",
            "policy",
            "attach",
            "scoped",
            scope["policy"],
            "--user",
            scope["access_key"],
        ],
    )


def _image_worker_storage_policy(scope: dict) -> dict:
    cache_bucket_arn = f"arn:aws:s3:::{scope['bucket']}"
    source_objects = [
        f"arn:aws:s3:::{scope['datasets_bucket']}/*",
        f"arn:aws:s3:::{scope['legacy_bucket']}/*",
    ]
    derived_objects = [
        f"{cache_bucket_arn}/image-assets/*",
        f"{cache_bucket_arn}/image-pyramids/*",
    ]
    inputs_arn = f"arn:aws:s3:::{scope['inputs_bucket']}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                "Resource": source_objects,
            },
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": [cache_bucket_arn],
                "Condition": {"StringLike": {"s3:prefix": ["image-pyramids/*"]}},
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": derived_objects,
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetBucketVersioning"],
                "Resource": [inputs_arn],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:PutObject"],
                "Resource": [f"{inputs_arn}/accepted-inputs/*"],
            },
            _require_input_version_id(
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObjectVersion"],
                    "Resource": [f"{inputs_arn}/accepted-inputs/*"],
                }
            ),
        ],
    }


def _create_image_storage_scope(scope: dict) -> None:
    _cleanup_mc(
        scope,
        ["admin", "user", "add", "scoped", scope["access_key"], scope["secret_key"]],
    )
    scope["user_created"] = True
    policy = _image_worker_storage_policy(scope)
    _cleanup_mc(
        scope,
        ["admin", "policy", "create", "scoped", scope["policy"], "/dev/stdin"],
        payload=json.dumps(policy),
    )
    scope["policy_created"] = True
    _cleanup_mc(
        scope,
        [
            "admin",
            "policy",
            "attach",
            "scoped",
            scope["policy"],
            "--user",
            scope["access_key"],
        ],
    )


def _create_source_storage_scope(scope: dict) -> None:
    _cleanup_mc(
        scope,
        ["admin", "user", "add", "scoped", scope["access_key"], scope["secret_key"]],
    )
    scope["user_created"] = True
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": [
                    f"arn:aws:s3:::{scope['import_bucket']}/dataset-uploads/*"
                ],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{scope['datasets_bucket']}/*"],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{scope['datasets_bucket']}"],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
                "Resource": [
                    f"arn:aws:s3:::{scope['datasets_bucket']}/dataset-sources/*"
                ],
            },
        ],
    }
    _cleanup_mc(
        scope,
        ["admin", "policy", "create", "scoped", scope["policy"], "/dev/stdin"],
        payload=json.dumps(policy),
    )
    scope["policy_created"] = True
    _cleanup_mc(
        scope,
        [
            "admin",
            "policy",
            "attach",
            "scoped",
            scope["policy"],
            "--user",
            scope["access_key"],
        ],
    )


def _create_import_storage_scope(scope: dict, *, source: bool) -> None:
    _cleanup_mc(
        scope,
        ["admin", "user", "add", "scoped", scope["access_key"], scope["secret_key"]],
    )
    scope["user_created"] = True
    if source:
        statements = [
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{scope['source_bucket']}"],
                "Condition": {
                    "StringLike": {"s3:prefix": ["connector-import-source/*"]}
                },
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [
                    f"arn:aws:s3:::{scope['source_bucket']}/connector-import-source/*"
                ],
            },
        ]
    else:
        statements = [
            {
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
                "Resource": [
                    f"arn:aws:s3:::{scope['datasets_bucket']}/dataset-sources/*"
                ],
            }
        ]
    _cleanup_mc(
        scope,
        ["admin", "policy", "create", "scoped", scope["policy"], "/dev/stdin"],
        payload=json.dumps({"Version": "2012-10-17", "Statement": statements}),
    )
    scope["policy_created"] = True
    _cleanup_mc(
        scope,
        [
            "admin",
            "policy",
            "attach",
            "scoped",
            scope["policy"],
            "--user",
            scope["access_key"],
        ],
    )


def _container_details(
    container_id: str, name: str, identity: str, *, require_running: bool
):
    from worktree_resources import docker

    observed = json.loads(docker("inspect", container_id))[0]
    labels = observed.get("Config", {}).get("Labels") or {}
    if (
        observed.get("Id") != container_id
        or observed.get("Name") != f"/{name}"
        or observed.get("Config", {}).get("Image") != POSTGRES_IMAGE
        or labels.get(CONTAINER_LABEL) != CONTAINER_LABEL_VALUE
        or labels.get(CONTAINER_ID_LABEL) != identity
    ):
        raise RuntimeError("PostgreSQL container ownership verification failed")
    if require_running and not observed.get("State", {}).get("Running"):
        raise RuntimeError("owned PostgreSQL container is not running")
    bindings = (observed.get("NetworkSettings", {}).get("Ports") or {}).get(
        "5432/tcp"
    ) or []
    if require_running and (
        len(bindings) != 1 or bindings[0].get("HostIp") != "127.0.0.1"
    ):
        raise RuntimeError("PostgreSQL must have one loopback-only published port")
    port = int(bindings[0]["HostPort"]) if bindings else None
    return port


def _psql(
    container_id: str,
    container_name: str,
    container_identity: str,
    *,
    username: str,
    password: str,
    database: str,
    sql: str,
    variables: dict[str, str] | None = None,
) -> str:
    _container_details(
        container_id, container_name, container_identity, require_running=True
    )
    command = [
        "docker",
        "exec",
        "-i",
        "-e",
        f"PGPASSWORD={password}",
        container_id,
        "psql",
        "-X",
        "-q",
        "-A",
        "-t",
        "-h",
        "127.0.0.1",
        "-p",
        "5432",
        "-U",
        username,
        "-d",
        database,
        "-v",
        "ON_ERROR_STOP=1",
    ]
    for name, value in (variables or {}).items():
        command.extend(("-v", f"{name}={value}"))
    command.extend(("-f", "-"))
    result = subprocess.run(
        command,
        input=sql,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"psql acceptance phase failed with exit {result.returncode}"
        )
    return result.stdout


def _wait_for_postgres(container_id: str, name: str, identity: str) -> None:
    _container_details(container_id, name, identity, require_running=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "docker",
                "exec",
                container_id,
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-p",
                "5432",
                "-U",
                "owner",
                "-d",
                "postgres",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(0.2)
    raise RuntimeError("disposable PostgreSQL did not become ready")


def _create_runtime_role(
    container_id: str,
    container_name: str,
    container_identity: str,
    password: str,
    owner_password: str,
) -> None:
    _psql(
        container_id,
        container_name,
        container_identity,
        username="owner",
        password=owner_password,
        database="postgres",
        sql=(
            "CREATE ROLE legacy_runtime LOGIN "
            f"PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
        ),
    )


def _create_module_roles(
    container_id: str,
    container_name: str,
    container_identity: str,
    database: str,
    owner_password: str,
    append_password: str,
    cleanup_password: str,
    catalog_password: str,
    source_password: str,
    import_password: str,
    operator_password: str,
    media_control_password: str,
    media_worker_password: str,
    image_worker_password: str,
    mask_gc_password: str,
    result_password: str,
) -> None:
    _psql(
        container_id,
        container_name,
        container_identity,
        username="owner",
        password=owner_password,
        database=database,
        sql=(
            "CREATE ROLE identity_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE workspace_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE assets_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE labeling_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE jobs_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE learning_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE inference_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE insights_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE diagnostics_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE export_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE audit_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE settings_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE notifications_code NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE append_client LOGIN "
            f"PASSWORD '{append_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE asset_cleanup_client LOGIN "
            f"PASSWORD '{cleanup_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE asset_catalog_client LOGIN "
            f"PASSWORD '{catalog_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE asset_source_client LOGIN "
            f"PASSWORD '{source_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE asset_import_client LOGIN "
            f"PASSWORD '{import_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE asset_operator_client LOGIN "
            f"PASSWORD '{operator_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE asset_media_control_client LOGIN "
            f"PASSWORD '{media_control_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE asset_media_worker_client LOGIN "
            f"PASSWORD '{media_worker_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE asset_image_worker_client LOGIN "
            f"PASSWORD '{image_worker_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE labeling_mask_gc_client LOGIN "
            f"PASSWORD '{mask_gc_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            "CREATE ROLE preannotation_result_client LOGIN "
            f"PASSWORD '{result_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            'REVOKE TEMPORARY ON DATABASE :"expected_database" FROM PUBLIC;\n'
            'GRANT TEMPORARY ON DATABASE :"expected_database" TO '
            + ", ".join(
                ("legacy_runtime",)
                + tuple(
                    role
                    for role in MODULE_ROLES
                    if role
                    not in {
                        "asset_operator_client",
                        "asset_media_control_client",
                        "asset_media_worker_client",
                        "asset_image_worker_client",
                        "labeling_mask_gc_client",
                        "preannotation_result_client",
                        *IDENTITY_CLIENT_ROLES.values(),
                    }
                )
            )
            + ";\n"
        ),
        variables={"expected_database": database},
    )


def _run_alembic(backend, action: str, revision: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", action, revision],
        cwd=ROOT / "apps/api",
        env=backend.environment(),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"disposable Alembic {action} failed with exit {result.returncode}"
        )


def _install_and_rollback_frozen_sql(
    container_id: str,
    container_name: str,
    container_identity: str,
    database: str,
    owner_password: str,
    artifacts: tuple[Path, ...],
) -> None:
    install_sql = "\n".join(path.read_text(encoding="utf-8") for path in artifacts)
    sql = (
        "\\set ON_ERROR_STOP on\n"
        "BEGIN;\n"
        "SELECT 1 / (current_database() = :'expected_database')::integer;\n"
        f"{install_sql}\n"
        "SELECT aap_identity_accounts_v1.account_policy();\n"
        "SELECT aap_identity_accounts_v1.prepare_account_command(NULL::jsonb);\n"
        "SELECT aap_identity_accounts_v1.apply_account_command(NULL::jsonb);\n"
        "SELECT aap_identity_accounts_v1.mark_inactive_accounts_offline('2000-01-01T00:00:00Z'::timestamptz);\n"
        "SELECT aap_identity_recovery_v1.recovery_policy();\n"
        "SELECT aap_identity_recovery_v1.prepare_recovery_command(NULL::jsonb);\n"
        "SELECT aap_identity_recovery_v1.apply_recovery_command(NULL::jsonb);\n"
        "SELECT aap_identity_recovery_v1.mail_policy();\n"
        "SELECT aap_identity_invitations_v1.prepare_invitation_command('{}'::jsonb);\n"
        "SELECT aap_identity_directory_v1.directory_policy();\n"
        "SELECT aap_identity_directory_v1.execute_directory_command('{}'::jsonb);\n"
        "SELECT aap_identity_admin_passwords_v1.admin_password_policy();\n"
        "SELECT aap_identity_admin_passwords_v1.execute_admin_password_command('{}'::jsonb);\n"
        "SELECT aap_identity_platform_roles_v1.platform_role_policy();\n"
        "SELECT aap_identity_platform_roles_v1.execute_platform_role_command('{}'::jsonb);\n"
        "SELECT aap_identity_suspensions_v1.suspension_policy();\n"
        "SELECT aap_identity_suspensions_v1.execute_suspension_command('{}'::jsonb);\n"
        "SELECT aap_identity_deletions_v1.deletion_policy();\n"
        "SELECT aap_identity_deletions_v1.execute_deletion_command('{}'::jsonb);\n"
        "SELECT aap_identity_offboarding_v1.offboarding_policy();\n"
        "SELECT aap_identity_offboarding_v1.prepare_offboarding_command('{}'::jsonb);\n"
        "SELECT aap_identity_offboarding_v1.apply_offboarding_command('{}'::jsonb);\n"
        "SELECT aap_identity_v1.read_actor_context(NULL::uuid,NULL::text);\n"
        "SELECT aap_identity_briefs_v1.briefs_policy();\n"
        "SELECT aap_identity_briefs_v1.execute_briefs_command('{}'::jsonb);\n"
        "SELECT aap_identity_deactivation_v1.deactivation_policy();\n"
        "SELECT aap_identity_deactivation_v1.execute_deactivation_command(NULL::jsonb);\n"
        "SELECT aap_identity_deactivation_v1.due_policy();\n"
        "SELECT aap_identity_deactivation_v1.process_due_deactivations(NULL::jsonb);\n"
        "SELECT aap_notifications_v1.identity_delivery_policy();\n"
        "SELECT aap_identity_groups_v1.group_policy();\n"
        "SELECT aap_identity_groups_v1.execute_group_command(NULL::jsonb);\n"
        "SELECT aap_identity_v1.lock_active_actor(NULL::uuid);\n"
        "SELECT aap_workspace_v1.lock_project_manager(NULL::uuid, NULL::uuid);\n"
        "SELECT aap_assets_v1.associate_project(NULL::uuid, NULL::uuid, NULL::uuid);\n"
        "SELECT aap_assets_v1.lock_task_creation_items(NULL::uuid, NULL::uuid[]);\n"
        "SELECT aap_jobs_v1.create_task_creation_job(NULL::uuid, NULL::uuid, NULL::uuid, NULL::uuid, NULL::uuid);\n"
        "SELECT aap_jobs_v1.find_task_creation_job(NULL::uuid, NULL::uuid, NULL::uuid, NULL::uuid);\n"
        "SELECT aap_jobs_v1.claim_task_creation_job(NULL::uuid, NULL::text);\n"
        "SELECT aap_jobs_v1.lock_task_creation_job(NULL::uuid, NULL::uuid, NULL::uuid, NULL::uuid, NULL::uuid, NULL::uuid);\n"
        "SELECT aap_jobs_v1.finish_task_creation_job(NULL::uuid, NULL::uuid);\n"
        "SELECT aap_jobs_v1.fail_task_creation_job(NULL::uuid, NULL::uuid, NULL::text);\n"
        "SELECT aap_jobs_v1.cancel_task_creation_job(NULL::uuid, NULL::uuid);\n"
        "SELECT aap_labeling_v1.accept_dataset_task_creation(NULL::uuid, NULL::uuid, NULL::uuid, NULL::uuid, NULL::uuid, NULL::uuid, NULL::text, NULL::integer);\n"
        "SELECT aap_labeling_v1.prepare_dataset_task_creation(NULL::uuid, NULL::jsonb);\n"
        "SELECT aap_labeling_v1.read_dataset_task_creation(NULL::uuid);\n"
        "SELECT aap_labeling_v1.advance_dataset_task_creation(NULL::uuid, NULL::integer);\n"
        "SELECT aap_identity_v1.lock_audit_actor(NULL::uuid);\n"
        "SELECT aap_learning_v1.guard_project_deletion(NULL::uuid);\n"
        "SELECT aap_workspace_v1.delete_project(NULL::uuid, NULL::uuid, NULL::uuid, NULL::jsonb);\n"
        "SELECT aap_learning_v1.guard_dataset_item_deletion(NULL::uuid, NULL::uuid);\n"
        "SELECT aap_learning_v1.guard_dataset_unlink(NULL::uuid, NULL::uuid);\n"
        "SELECT aap_workspace_v1.preview_dataset_unlink(NULL::uuid, NULL::uuid, NULL::uuid);\n"
        "SELECT aap_workspace_v1.unlink_dataset(NULL::uuid, NULL::uuid, NULL::uuid, NULL::uuid, NULL::text, NULL::jsonb);\n"
        "SELECT aap_assets_v1.reserve_dataset_upload(NULL::uuid,NULL::uuid,NULL::uuid,NULL::text,NULL::text);\n"
        "SELECT aap_assets_v1.delete_dataset(NULL::uuid,NULL::uuid,NULL::uuid,NULL::text,NULL::jsonb);\n"
        "SELECT count(*) FROM aap_assets_v1.pending_storage_deletions_v2('cache-bucket',NULL::timestamptz,NULL::uuid,NULL::uuid,100);\n"
        "SELECT aap_assets_v1.list_catalog_datasets(NULL::uuid,NULL::text,NULL::text,NULL::boolean,50,0::bigint);\n"
        "SELECT aap_assets_v1.get_catalog_dataset(NULL::uuid,NULL::uuid);\n"
        "SELECT aap_assets_v1.list_catalog_items(NULL::uuid,NULL::uuid,50,0::bigint);\n"
        "SELECT aap_assets_v1.create_catalog_dataset(NULL::uuid,NULL::uuid,NULL::jsonb);\n"
        "SELECT aap_assets_v1.update_catalog_dataset(NULL::uuid,NULL::uuid,NULL::uuid,NULL::jsonb);\n"
        "SELECT aap_assets_v1.list_dataset_projects(NULL::uuid,NULL::uuid);\n"
        "SELECT aap_assets_v1.list_project_datasets(NULL::uuid,NULL::uuid);\n"
        "SELECT aap_assets_v1.lock_legacy_dataset_upload(NULL::uuid,NULL::uuid,NULL::uuid,NULL::uuid);\n"
        "SELECT aap_assets_v1.complete_legacy_dataset_upload(NULL::uuid,NULL::uuid,NULL::uuid,NULL::uuid,NULL::text,NULL::bigint,NULL::text,NULL::integer,NULL::integer);\n"
        "SELECT aap_assets_v1.begin_dataset_ingest(NULL::uuid,NULL::uuid,NULL::uuid,NULL::jsonb);\n"
        "SELECT aap_assets_v1.claim_dataset_source_ingest_delivery(NULL::uuid,NULL::integer);\n"
        "SELECT aap_assets_v1.guard_spatial_actor(NULL::uuid,NULL::text);\n"
        "SELECT aap_assets_v1.list_spatial_scenes(NULL::uuid,NULL::uuid,NULL::timestamp with time zone,NULL::uuid,NULL::integer);\n"
        "SELECT aap_assets_v1.get_spatial_scene(NULL::uuid,NULL::uuid);\n"
        "SELECT aap_assets_v1.read_spatial_layout(NULL::uuid,NULL::uuid,NULL::uuid,NULL::uuid,NULL::integer);\n"
        "SELECT aap_assets_v1.read_spatial_items(NULL::uuid,NULL::uuid[],NULL::uuid,NULL::integer);\n"
        "SELECT aap_assets_v1.read_spatial_poses(NULL::uuid,NULL::uuid,NULL::integer,NULL::integer);\n"
        "SELECT aap_assets_v1.read_spatial_pose_pairs(NULL::jsonb);\n"
        "SELECT aap_assets_v1.read_spatial_calibrations(NULL::uuid[],NULL::boolean,NULL::uuid,NULL::integer,NULL::integer);\n"
        "SELECT aap_assets_v1.lock_spatial_calibration_facts(NULL::uuid,NULL::uuid,NULL::text,NULL::uuid,NULL::jsonb);\n"
        "SELECT aap_assets_v1.mutate_spatial_scene(NULL::uuid,NULL::uuid,NULL::uuid,NULL::jsonb,NULL::jsonb);\n"
        "SELECT aap_assets_v1.apply_spatial_layout(NULL::uuid,NULL::uuid,NULL::uuid,NULL::text,NULL::boolean,NULL::jsonb,NULL::jsonb);\n"
        "SELECT aap_assets_v1.read_spatial_layout_receipt(NULL::uuid,NULL::uuid,NULL::uuid,NULL::text,NULL::boolean);\n"
        "SELECT aap_assets_v1.upsert_spatial_poses(NULL::uuid,NULL::uuid,NULL::jsonb);\n"
        "SELECT aap_assets_v1.create_spatial_scene(NULL::uuid,NULL::uuid,NULL::uuid,NULL::text,NULL::text,NULL::jsonb,NULL::uuid);\n"
        "SELECT aap_assets_v1.assign_spatial_items(NULL::uuid,NULL::uuid,NULL::jsonb);\n"
        "SELECT aap_assets_v1.set_spatial_scene_source(NULL::uuid,NULL::uuid,NULL::text,NULL::jsonb);\n"
        "SELECT aap_assets_v1.update_spatial_calibration(NULL::uuid,NULL::uuid,NULL::uuid,NULL::text,NULL::integer,NULL::text,NULL::jsonb,NULL::text,NULL::text,NULL::jsonb,NULL::jsonb);\n"
        "SELECT aap_assets_v1.read_pointcloud_preparation(NULL::uuid,NULL::uuid,NULL::integer);\n"
        "SELECT aap_assets_v1.apply_pointcloud_preparation(NULL::uuid,NULL::jsonb,NULL::jsonb,NULL::jsonb);\n"
        "SELECT aap_assets_v1.read_spatial_camera_target(NULL::uuid,NULL::uuid,NULL::text);\n"
        "SELECT aap_assets_v1.record_spatial_calibration_provenance(NULL::uuid,NULL::uuid,NULL::jsonb);\n"
        "SELECT aap_assets_v1.set_spatial_dataset_axis(NULL::uuid,NULL::uuid,NULL::text);\n"
        "SELECT aap_learning_v1.lock_project_deletion(:'project_id'::uuid);\n"
        "SELECT aap_learning_v1.guard_project_deletion(:'project_id'::uuid);\n"
        "SELECT aap_assets_v1.delete_project_owned_records(:'project_id'::uuid);\n"
        "SELECT aap_inference_v1.clear_project_ml_backend_pool(:'project_id'::uuid);\n"
        "SELECT aap_inference_v1.clear_project_pipelines(:'project_id'::uuid);\n"
        "SELECT aap_insights_v1.clear_project_task_views(:'project_id'::uuid);\n"
        "SELECT aap_diagnostics_v1.detach_project_references(:'project_id'::uuid);\n"
        "SELECT aap_export_v1.clear_project_export_artifacts(:'project_id'::uuid);\n"
        "SELECT aap_jobs_v1.detach_project_references(:'project_id'::uuid);\n"
        "SELECT aap_learning_v1.delete_project_owned_records(:'project_id'::uuid);\n"
        "SELECT aap_labeling_v1.delete_project_owned_records(:'project_id'::uuid);\n"
        "SELECT aap_labeling_v1.append_dataset_items("
        ":'dataset_id'::uuid, ARRAY[]::uuid[], :'operation_id'::uuid);\n"
        "ROLLBACK;\n"
    )
    output = _psql(
        container_id,
        container_name,
        container_identity,
        username="owner",
        password=owner_password,
        database=database,
        sql=sql,
        variables={
            "expected_database": database,
            "dataset_id": str(uuid4()),
            "operation_id": str(uuid4()),
            "project_id": str(uuid4()),
        },
    )
    responses: dict[str, list[dict]] = {}
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("schema"), str):
            responses.setdefault(value["schema"], []).append(value)
    append_receipts = responses.get("aap-task-append/v1", [])
    if len(append_receipts) != 1 or append_receipts[0].get("created") != 0:
        raise RuntimeError(
            "native frozen-SQL append probe returned an unexpected receipt"
        )

    expected_errors = {
        "aap-active-actor/v1": "account_not_active",
        "aap-audit-actor/v1": "account_not_active",
        "aap-project-manager/v1": "account_not_active",
        "aap-dataset-association/v1": "account_not_active",
        "aap-task-creation-items/v1": "invalid_request",
        "aap-task-creation-job-command/v1": "invalid_request",
        "aap-task-creation-job-discovery/v1": "invalid_request",
        "aap-task-creation-job-claim/v1": "invalid_request",
        "aap-task-creation-job/v1": "invalid_request",
        "aap-task-creation-job-finish/v1": "invalid_request",
        "aap-task-creation/v1": "invalid_request",
        "aap-project-delete-retention/v1": "invalid_request",
        "aap-project-delete/v1": "invalid_request",
        "aap-source-retention/v1": "invalid_request",
        "aap-dataset-unlink-preview/v1": "invalid_request",
        "aap-dataset-unlink/v1": "invalid_request",
        "aap-dataset-source-delete/v1": "invalid_request",
        "aap-dataset-upload/v1": "invalid_request",
        "aap-assets-catalog/v1": "invalid_request",
        "aap-assets-associations/v1": "invalid_request",
        "aap-legacy-dataset-upload/v1": "invalid_request",
        "aap-dataset-source-ingest/v1": "invalid_request",
        "aap-dataset-source-ingest-delivery/v1": "invalid_request",
        "aap-assets-spatial/v1": "invalid_request",
        "aap-identity-accounts-native/v1": "invalid_request",
        "aap-identity-deactivation-native/v1": "invalid_request",
        "aap-identity-deactivation-due-native/v1": "invalid_request",
        "aap-identity-invitations-native/v1": "invalid_request",
        "aap-identity-directory-native/v1": "invalid_request",
        "aap-identity-admin-passwords-native/v1": "invalid_request",
        "aap-identity-platform-roles-native/v1": "invalid_request",
        "aap-identity-suspensions-native/v1": "invalid_request",
        "aap-identity-deletions-native/v1": "invalid_request",
        "aap-identity-offboarding-native/v1": "invalid_request",
        "aap-identity-briefs-native/v1": "invalid_request",
        "aap-identity-actor/v1": "invalid_request",
    }
    if any(
        not any(
            response.get("ok") is False
            and response.get("error", {}).get("code") == code
            for response in responses.get(schema, [])
        )
        for schema, code in expected_errors.items()
    ):
        raise RuntimeError(
            "native frozen-SQL contract probes returned unexpected errors"
        )
    if not any(
        response.get("ok") is True and response.get("data") == {}
        for response in responses.get("aap-project-delete-retention/v1", [])
    ):
        raise RuntimeError("native Learning retention guard probe did not succeed")
    if not any(
        response.get("ok") is True
        and response.get("data") == {"deleted_tasks": 0, "deleted_annotations": 0}
        for response in responses.get("aap-project-labeling-delete/v1", [])
    ):
        raise RuntimeError("native Labeling project-delete probe did not succeed")
    if not any(
        response.get("schema") == "aap-identity-directory-policy/v1"
        and response.get("policy") == "aap-identity-directory/v1"
        for response in responses.get("aap-identity-directory-policy/v1", [])
    ):
        raise RuntimeError("native Identity directory policy probe did not succeed")
    if not any(
        response.get("schema") == "aap-identity-admin-passwords-policy/v1"
        and response.get("policy") == "aap-identity-admin-passwords/v1"
        for response in responses.get("aap-identity-admin-passwords-policy/v1", [])
    ):
        raise RuntimeError(
            "native Identity admin-password policy probe did not succeed"
        )
    if not any(
        response.get("schema") == "aap-identity-platform-roles-policy/v1"
        and response.get("policy") == "aap-identity-platform-roles/v1"
        for response in responses.get("aap-identity-platform-roles-policy/v1", [])
    ):
        raise RuntimeError("native Identity platform-role policy probe did not succeed")
    if not any(
        response.get("schema") == "aap-identity-suspensions-policy/v1"
        and response.get("policy") == "aap-identity-suspensions/v1"
        for response in responses.get("aap-identity-suspensions-policy/v1", [])
    ):
        raise RuntimeError("native Identity suspension policy probe did not succeed")
    if not any(
        response.get("schema") == "aap-identity-deletions-policy/v1"
        and response.get("policy") == "aap-identity-deletions/v1"
        for response in responses.get("aap-identity-deletions-policy/v1", [])
    ):
        raise RuntimeError("native Identity deletion policy probe did not succeed")
    if not any(
        response.get("schema") == "aap-identity-offboarding-policy/v1"
        and response.get("policy") == "aap-identity-offboarding/v1"
        for response in responses.get("aap-identity-offboarding-policy/v1", [])
    ):
        raise RuntimeError("native Identity offboarding policy probe did not succeed")
    if not any(
        response.get("schema") == "aap-identity-briefs-policy/v1"
        and response.get("policy") == "aap-identity-briefs/v1"
        for response in responses.get("aap-identity-briefs-policy/v1", [])
    ):
        raise RuntimeError("native Identity briefs policy probe did not succeed")

    rollback_state = _psql(
        container_id,
        container_name,
        container_identity,
        username="owner",
        password=owner_password,
        database=database,
        sql=(
            "SELECT current_database() = :'expected_database' "
            "AND to_regnamespace('aap_identity_v1') IS NULL "
            "AND to_regnamespace('aap_identity_accounts_v1') IS NULL "
            "AND to_regnamespace('aap_identity_recovery_v1') IS NULL "
            "AND to_regnamespace('aap_identity_groups_v1') IS NULL "
            "AND to_regnamespace('aap_identity_deactivation_v1') IS NULL "
            "AND to_regnamespace('aap_identity_invitations_v1') IS NULL "
            "AND to_regnamespace('aap_identity_directory_v1') IS NULL "
            "AND to_regnamespace('aap_identity_admin_passwords_v1') IS NULL "
            "AND to_regnamespace('aap_identity_platform_roles_v1') IS NULL "
            "AND to_regnamespace('aap_identity_suspensions_v1') IS NULL "
            "AND to_regnamespace('aap_identity_deletions_v1') IS NULL "
            "AND to_regnamespace('aap_identity_offboarding_v1') IS NULL "
            "AND to_regnamespace('aap_identity_briefs_v1') IS NULL "
            "AND to_regprocedure('aap_workspace_v1.identity_brief_project_roles(uuid,uuid[])') IS NULL "
            "AND to_regprocedure('aap_identity_v1.read_actor_context(uuid,text)') IS NULL "
            "AND to_regnamespace('aap_identity_mail_v2') IS NULL "
            "AND to_regnamespace('aap_workspace_v1') IS NULL "
            "AND to_regnamespace('aap_assets_v1') IS NULL "
            "AND to_regnamespace('aap_labeling_v1') IS NULL "
            "AND to_regnamespace('aap_jobs_v1') IS NULL "
            "AND to_regnamespace('aap_learning_v1') IS NULL "
            "AND to_regnamespace('aap_inference_v1') IS NULL "
            "AND to_regnamespace('aap_insights_v1') IS NULL "
            "AND to_regnamespace('aap_diagnostics_v1') IS NULL "
            "AND to_regnamespace('aap_export_v1') IS NULL "
            "AND to_regnamespace('aap_audit_v1') IS NULL "
            "AND to_regnamespace('aap_settings_v1') IS NULL "
            "AND to_regnamespace('aap_notifications_v1') IS NULL "
            "AND to_regclass('public.task_append_operations') IS NULL "
            "AND to_regclass('public.identity_account_operation_receipts') IS NULL "
            "AND to_regclass('public.identity_recovery_operation_receipts') IS NULL "
            "AND to_regclass('public.identity_group_operation_receipts') IS NULL "
            "AND to_regclass('public.identity_deactivation_operation_receipts') IS NULL "
            "AND to_regclass('public.identity_deactivation_due_receipts') IS NULL "
            "AND to_regclass('public.identity_invitation_operation_receipts') IS NULL "
            "AND to_regclass('public.identity_directory_export_receipts') IS NULL "
            "AND to_regclass('public.identity_admin_password_reset_receipts') IS NULL "
            "AND to_regclass('public.identity_platform_role_operation_receipts') IS NULL "
            "AND to_regclass('public.identity_suspension_operation_receipts') IS NULL "
            "AND to_regclass('public.identity_deletion_operation_receipts') IS NULL "
            "AND to_regclass('public.identity_offboarding_operation_receipts') IS NULL "
            "AND to_regclass('public.identity_deactivation_requests') IS NULL "
            "AND to_regclass('public.notification_delivery_outbox') IS NULL "
            "AND to_regclass('public.identity_credential_mail_outbox') IS NULL "
            "AND to_regclass('public.task_creation_operations') IS NULL "
            "AND to_regclass('public.task_creation_chunks') IS NULL "
            "AND to_regclass('public.project_deletion_operations') IS NULL "
            "AND to_regclass('public.dataset_unlink_operations') IS NULL "
            "AND to_regclass('public.asset_storage_deletions') IS NULL "
            "AND to_regclass('public.dataset_upload_intents') IS NULL "
            "AND to_regclass('public.dataset_source_deletion_operations') IS NULL "
            "AND to_regclass('public.dataset_catalog_operations') IS NULL "
            "AND to_regclass('public.dataset_legacy_upload_operations') IS NULL "
            "AND to_regclass('public.dataset_import_operations') IS NULL "
            "AND to_regclass('public.dataset_import_entries') IS NULL "
            "AND to_regclass('public.dataset_import_deliveries') IS NULL "
            "AND to_regclass('public.dataset_source_ingest_operations') IS NULL "
            "AND to_regclass('public.dataset_source_ingest_entries') IS NULL "
            "AND to_regclass('public.dataset_source_ingest_deliveries') IS NULL "
            "AND to_regclass('public.dataset_spatial_operations') IS NULL "
            "AND to_regclass('public.dataset_operator_operations') IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute "
            "WHERE attrelid = 'public.async_jobs'::pg_catalog.regclass "
            "AND attname = 'retained_project_id' AND NOT attisdropped) "
            "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger "
            "WHERE tgrelid = 'public.async_jobs'::pg_catalog.regclass "
            "AND tgname = 'async_jobs_retain_project_scope' AND NOT tgisinternal);\n"
        ),
        variables={"expected_database": database},
    )
    if rollback_state.strip() != "t":
        raise RuntimeError("native frozen-SQL transaction did not roll back cleanly")


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    test_files = tuple(dict.fromkeys(args.test_files or TEST_FILES))
    output = args.output.resolve()
    junit = (
        args.junit_xml.resolve()
        if args.junit_xml
        else output.with_name(f"{output.stem}.junit.xml")
    )
    if output == junit:
        raise SystemExit("--output and --junit-xml must be different paths")
    output.parent.mkdir(parents=True, exist_ok=True)
    junit.parent.mkdir(parents=True, exist_ok=True)

    evidence = {
        "schema": 1,
        "acceptance": "module-data-boundary",
        "status": "failed",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "code_root": str(ROOT),
        "container_image": POSTGRES_IMAGE,
        "container_labels": {
            CONTAINER_LABEL: CONTAINER_LABEL_VALUE,
        },
        "contract_artifacts": {
            "sql": list(SQL_ARTIFACTS),
            "schemas": list(SCHEMA_ARTIFACTS),
        },
        "database": None,
        "roles": list(MODULE_ROLES),
        "test_files": list(test_files),
        "junit_xml": str(junit),
        "test_exit_code": None,
        "migrations": {
            "head_before_downgrade": None,
            "downgraded_to_0189": False,
            "frozen_sql_installed_and_rolled_back": False,
            "upgraded_to_head": False,
            "head_after_upgrade": None,
            "runtime_grants_reapplied": False,
        },
        "cleanup": {
            "storage_identity_removed": False,
            "catalog_storage_identity_removed": False,
            "source_storage_identity_removed": False,
            "import_storage_identity_removed": False,
            "import_source_storage_identity_removed": False,
            "media_control_storage_identity_removed": False,
            "media_worker_storage_identity_removed": False,
            "image_worker_storage_identity_removed": False,
            "api_bootstrap_storage_identity_removed": None,
            "mask_content_storage_identity_removed": None,
            "mask_cleanup_storage_identity_removed": None,
            "local_resources_destroyed": False,
            "postgres_container_removed": False,
            "resource_root_retained": None,
            "errors": [],
        },
    }
    phase = "preflight"
    exit_code = 1
    container_id = None
    container_name = f"aap-module-data-{secrets.token_hex(6)}"
    container_identity = secrets.token_hex(12)
    backend = None
    cleanup_storage = None
    catalog_storage = None
    source_storage = None
    import_storage = None
    import_source_storage = None
    media_control_storage = None
    media_worker_storage = None
    image_worker_storage = None
    api_bootstrap_storage = None
    mask_content_storage = None
    mask_cleanup_storage = None
    resource_root = Path(
        tempfile.mkdtemp(prefix="aap-module-data-acceptance-")
    ).resolve()

    try:
        selected_test_paths = {name.split("::", 1)[0] for name in test_files}
        missing_tests = [
            name
            for name in selected_test_paths
            if not (ROOT / "apps/api" / name).is_file()
        ]
        if missing_tests:
            raise RuntimeError(f"Missing module-data acceptance cases: {missing_tests}")
        grants_path = ROOT / "infra/docker/module-data-grants.sql"
        catalog_grants_path = ROOT / "infra/docker/assets-catalog-grants.sql"
        source_grants_path = ROOT / "infra/docker/assets-source-grants.sql"
        import_grants_path = ROOT / "infra/docker/assets-import-grants.sql"
        operator_grants_path = ROOT / "infra/docker/assets-operator-grants.sql"
        media_grants_path = ROOT / "infra/docker/assets-media-grants.sql"
        image_grants_path = ROOT / "infra/docker/assets-image-grants.sql"
        mask_gc_grants_path = ROOT / "infra/docker/labeling-mask-gc-data-grants.sql"
        result_grants_path = ROOT / "infra/docker/preannotation-result-data-grants.sql"
        identity_grants_path = ROOT / "infra/docker/identity-auth-grants.sql"
        identity_key_grants_path = ROOT / "infra/docker/identity-keys-grants.sql"
        identity_session_grants_path = (
            ROOT / "infra/docker/identity-sessions-grants.sql"
        )
        identity_account_grants_path = (
            ROOT / "infra/docker/identity-accounts-grants.sql"
        )
        identity_group_grants_path = ROOT / "infra/docker/identity-groups-grants.sql"
        identity_invitation_grants_path = (
            ROOT / "infra/docker/identity-invitations-grants.sql"
        )
        identity_directory_grants_path = (
            ROOT / "infra/docker/identity-directory-grants.sql"
        )
        identity_admin_password_grants_path = (
            ROOT / "infra/docker/identity-admin-passwords-grants.sql"
        )
        identity_platform_role_grants_path = (
            ROOT / "infra/docker/identity-platform-roles-grants.sql"
        )
        identity_suspension_grants_path = (
            ROOT / "infra/docker/identity-suspensions-grants.sql"
        )
        identity_deletion_grants_path = (
            ROOT / "infra/docker/identity-deletions-grants.sql"
        )
        identity_offboarding_grants_path = (
            ROOT / "infra/docker/identity-offboarding-grants.sql"
        )
        identity_briefs_grants_path = ROOT / "infra/docker/identity-briefs-grants.sql"
        identity_deactivation_grants_path = (
            ROOT / "infra/docker/identity-deactivation-grants.sql"
        )
        identity_recovery_grants_path = (
            ROOT / "infra/docker/identity-recovery-grants.sql"
        )
        artifacts = tuple(
            ROOT / "apps/api/alembic/sql" / name for name in SQL_ARTIFACTS
        )
        schema_paths = tuple(
            ROOT / "apps/api/alembic/sql" / name for name in SCHEMA_ARTIFACTS
        )
        if (
            not grants_path.is_file()
            or not catalog_grants_path.is_file()
            or not source_grants_path.is_file()
            or not import_grants_path.is_file()
            or not operator_grants_path.is_file()
            or not media_grants_path.is_file()
            or not image_grants_path.is_file()
            or not mask_gc_grants_path.is_file()
            or not result_grants_path.is_file()
            or not identity_grants_path.is_file()
            or not identity_key_grants_path.is_file()
            or not identity_session_grants_path.is_file()
            or not identity_deactivation_grants_path.is_file()
            or not identity_group_grants_path.is_file()
            or not identity_invitation_grants_path.is_file()
            or not identity_directory_grants_path.is_file()
            or not identity_admin_password_grants_path.is_file()
            or not identity_platform_role_grants_path.is_file()
            or not identity_suspension_grants_path.is_file()
            or not identity_deletion_grants_path.is_file()
            or not identity_offboarding_grants_path.is_file()
            or not identity_briefs_grants_path.is_file()
            or not identity_account_grants_path.is_file()
            or not identity_recovery_grants_path.is_file()
            or any(not path.is_file() for path in artifacts)
            or any(not path.is_file() for path in schema_paths)
        ):
            raise RuntimeError(
                "module-data SQL/schema artifacts are missing from this checkout"
            )
        for path in schema_paths:
            contract = json.loads(path.read_text(encoding="utf-8"))
            if (
                contract.get("$schema")
                != "https://json-schema.org/draft/2020-12/schema"
                or not isinstance(contract.get("$id"), str)
                or not isinstance(contract.get("$defs"), dict)
            ):
                raise RuntimeError(f"invalid frozen module-data schema: {path.name}")
        grant_sql = grants_path.read_text(encoding="utf-8")
        catalog_grant_sql = catalog_grants_path.read_text(encoding="utf-8")
        source_grant_sql = source_grants_path.read_text(encoding="utf-8")
        import_grant_sql = import_grants_path.read_text(encoding="utf-8")
        operator_grant_sql = operator_grants_path.read_text(encoding="utf-8")
        media_grant_sql = media_grants_path.read_text(encoding="utf-8")
        image_grant_sql = image_grants_path.read_text(encoding="utf-8")
        mask_gc_grant_sql = mask_gc_grants_path.read_text(encoding="utf-8")
        result_grant_sql = result_grants_path.read_text(encoding="utf-8")
        identity_grant_sql = identity_grants_path.read_text(encoding="utf-8")
        identity_key_grant_sql = identity_key_grants_path.read_text(encoding="utf-8")
        identity_session_grant_sql = identity_session_grants_path.read_text(
            encoding="utf-8"
        )
        identity_invitation_grant_sql = identity_invitation_grants_path.read_text(
            encoding="utf-8"
        )
        identity_directory_grant_sql = identity_directory_grants_path.read_text(
            encoding="utf-8"
        )
        identity_admin_password_grant_sql = (
            identity_admin_password_grants_path.read_text(encoding="utf-8")
        )
        identity_platform_role_grant_sql = identity_platform_role_grants_path.read_text(
            encoding="utf-8"
        )
        identity_suspension_grant_sql = identity_suspension_grants_path.read_text(
            encoding="utf-8"
        )
        identity_deletion_grant_sql = identity_deletion_grants_path.read_text(
            encoding="utf-8"
        )
        identity_offboarding_grant_sql = identity_offboarding_grants_path.read_text(
            encoding="utf-8"
        )
        identity_briefs_grant_sql = identity_briefs_grants_path.read_text(
            encoding="utf-8"
        )
        identity_group_grant_sql = identity_group_grants_path.read_text(
            encoding="utf-8"
        )
        identity_deactivation_grant_sql = identity_deactivation_grants_path.read_text(
            encoding="utf-8"
        )
        identity_account_grant_sql = identity_account_grants_path.read_text(
            encoding="utf-8"
        )
        identity_recovery_grant_sql = identity_recovery_grants_path.read_text(
            encoding="utf-8"
        )

        from sqlalchemy.engine import URL

        from app.config import settings
        from worktree_env import load_identity, topology
        from worktree_resources import LocalResources, docker, migration_graph
        from worktree_runtime import parse_arguments, run_processes

        if settings.environment != "development":
            raise RuntimeError("module-data acceptance requires development settings")
        owner_password = f"owner-{secrets.token_hex(16)}"
        runtime_password = f"runtime-{secrets.token_hex(16)}"
        append_password = f"append-{secrets.token_hex(16)}"
        cleanup_password = f"cleanup-{secrets.token_hex(16)}"
        catalog_password = f"catalog-{secrets.token_hex(16)}"
        source_password = f"source-{secrets.token_hex(16)}"
        import_password = f"import-{secrets.token_hex(16)}"
        operator_password = f"operator-{secrets.token_hex(16)}"
        media_control_password = f"media-control-{secrets.token_hex(16)}"
        media_worker_password = f"media-worker-{secrets.token_hex(16)}"
        image_worker_password = f"image-worker-{secrets.token_hex(16)}"
        mask_gc_password = f"mask-gc-{secrets.token_hex(16)}"
        result_password = f"batch-result-{secrets.token_hex(16)}"
        identity_passwords = {
            role: secrets.token_hex(24) for role in IDENTITY_CLIENT_ROLES.values()
        }
        container_id = docker(
            "run",
            "-d",
            "--name",
            container_name,
            "--label",
            f"{CONTAINER_LABEL}={CONTAINER_LABEL_VALUE}",
            "--label",
            f"{CONTAINER_ID_LABEL}={container_identity}",
            "--publish",
            "127.0.0.1::5432",
            "--env",
            "POSTGRES_USER=owner",
            "--env",
            f"POSTGRES_PASSWORD={owner_password}",
            "--env",
            "POSTGRES_DB=postgres",
            POSTGRES_IMAGE,
        )
        evidence["container_labels"][CONTAINER_ID_LABEL] = container_identity
        port = _container_details(
            container_id, container_name, container_identity, require_running=True
        )
        evidence["postgres_container"] = {
            "id": container_id,
            "name": container_name,
            "host": "127.0.0.1",
            "port": port,
        }
        _wait_for_postgres(container_id, container_name, container_identity)
        owner_url = URL.create(
            "postgresql+asyncpg",
            username="owner",
            password=owner_password,
            host="127.0.0.1",
            port=port,
            database="postgres",
        ).render_as_string(hide_password=False)
        legacy_url = URL.create(
            "postgresql+asyncpg",
            username="legacy_runtime",
            password=runtime_password,
            host="127.0.0.1",
            port=port,
            database="postgres",
        ).render_as_string(hide_password=False)
        phase = "legacy-runtime-role"
        _create_runtime_role(
            container_id,
            container_name,
            container_identity,
            runtime_password,
            owner_password,
        )

        resources = topology(
            resource_root, load_identity(resource_root, create=True), "test"
        )
        backend = LocalResources(
            resources,
            settings.model_copy(
                update={
                    "database_url": legacy_url,
                    "migration_database_url": owner_url,
                }
            ),
            code_root=ROOT,
        )
        _container_details(
            container_id, container_name, container_identity, require_running=True
        )
        phase = "prepare-owned-test-resources"
        backend.prepare()
        database = resources["database"]
        evidence["database"] = database
        _, heads = migration_graph(ROOT)
        if heads != ["0250"]:
            raise RuntimeError("module-data acceptance requires migration head 0250")
        evidence["migrations"]["head_before_downgrade"] = heads[0]
        phase = "downgrade-to-0189"
        _container_details(
            container_id, container_name, container_identity, require_running=True
        )
        _run_alembic(backend, "downgrade", "0189")
        if asyncio.run(backend.revisions()) != ["0189"]:
            raise RuntimeError("disposable database did not reach revision 0189")
        evidence["migrations"]["downgraded_to_0189"] = True
        phase = "native-frozen-sql-roundtrip"
        _container_details(
            container_id, container_name, container_identity, require_running=True
        )
        _install_and_rollback_frozen_sql(
            container_id,
            container_name,
            container_identity,
            database,
            owner_password,
            artifacts,
        )
        evidence["migrations"]["frozen_sql_installed_and_rolled_back"] = True
        phase = "upgrade-to-head"
        _container_details(
            container_id, container_name, container_identity, require_running=True
        )
        _run_alembic(backend, "upgrade", "head")
        if set(asyncio.run(backend.revisions())) != set(heads):
            raise RuntimeError("disposable database did not return to migration head")
        evidence["migrations"]["upgraded_to_head"] = True
        evidence["migrations"]["head_after_upgrade"] = heads[0]
        _container_details(
            container_id, container_name, container_identity, require_running=True
        )
        asyncio.run(backend.grant_runtime())
        evidence["migrations"]["runtime_grants_reapplied"] = True
        append_url = URL.create(
            "postgresql+asyncpg",
            username="append_client",
            password=append_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        cleanup_url = URL.create(
            "postgresql+asyncpg",
            username="asset_cleanup_client",
            password=cleanup_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        catalog_url = URL.create(
            "postgresql+asyncpg",
            username="asset_catalog_client",
            password=catalog_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        source_url = URL.create(
            "postgresql+asyncpg",
            username="asset_source_client",
            password=source_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        import_url = URL.create(
            "postgresql+asyncpg",
            username="asset_import_client",
            password=import_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        operator_url = URL.create(
            "postgresql+asyncpg",
            username="asset_operator_client",
            password=operator_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        media_control_url = URL.create(
            "postgresql+asyncpg",
            username="asset_media_control_client",
            password=media_control_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        media_worker_url = URL.create(
            "postgresql+asyncpg",
            username="asset_media_worker_client",
            password=media_worker_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        image_worker_url = URL.create(
            "postgresql+asyncpg",
            username="asset_image_worker_client",
            password=image_worker_password,
            host="127.0.0.1",
            port=port,
            database=database,
        ).render_as_string(hide_password=False)
        role_create_password = append_password.replace("'", "''")
        phase = "create-scoped-roles"
        _create_module_roles(
            container_id,
            container_name,
            container_identity,
            database,
            owner_password,
            role_create_password,
            cleanup_password,
            catalog_password,
            source_password,
            import_password,
            operator_password,
            media_control_password,
            media_worker_password,
            image_worker_password,
            mask_gc_password,
            result_password,
        )
        phase = "create-identity-client-roles"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql="\n".join(
                f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER "
                "NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;"
                for role, password in identity_passwords.items()
            ),
        )
        phase = "install-module-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "workspace_owner": "workspace_code",
                "assets_owner": "assets_code",
                "labeling_owner": "labeling_code",
                "jobs_owner": "jobs_code",
                "learning_owner": "learning_code",
                "inference_owner": "inference_code",
                "insights_owner": "insights_code",
                "diagnostics_owner": "diagnostics_code",
                "export_owner": "export_code",
                "audit_owner": "audit_code",
                "settings_owner": "settings_code",
                "notifications_owner": "notifications_code",
                "workflow_role": "append_client",
                "asset_cleanup_role": "asset_cleanup_client",
                "mask_gc_role": "labeling_mask_gc_client",
            },
        )

        phase = "install-mask-gc-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=mask_gc_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "labeling_owner": "labeling_code",
                "mask_gc_role": "labeling_mask_gc_client",
            },
        )

        phase = "install-batch-result-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=result_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "jobs_owner": "jobs_code",
                "labeling_owner": "labeling_code",
                "result_role": "preannotation_result_client",
            },
        )

        phase = "install-identity-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=identity_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                **{
                    f"identity_{kind.lower()}_role": role
                    for kind, role in IDENTITY_CLIENT_ROLES.items()
                },
            },
        )
        phase = "install-identity-key-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=identity_key_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "identity_key_role": "identity_key_client",
            },
        )
        phase = "install-identity-session-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=identity_session_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "identity_session_role": "identity_session_client",
            },
        )
        phase = "install-identity-account-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=identity_account_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "identity_account_role": "identity_account_client",
                "identity_presence_role": "identity_presence_client",
            },
        )
        phase = "install-identity-group-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=identity_group_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "workspace_owner": "workspace_code",
                "audit_owner": "audit_code",
                "identity_group_role": "identity_group_client",
            },
        )
        phase = "install-identity-deactivation-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=identity_deactivation_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "audit_owner": "audit_code",
                "notifications_owner": "notifications_code",
                "identity_deactivation_role": "identity_deactivation_client",
                "identity_deactivation_operator_role": "identity_deactivation_operator",
                "notification_publish_role": "notification_publish_client",
            },
        )
        phase = "install-identity-recovery-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=identity_recovery_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "audit_owner": "audit_code",
                "settings_owner": "settings_code",
                "identity_recovery_role": "identity_recovery_client",
                "identity_mail_role": "identity_mail_client",
                "identity_mail_settings_role": "identity_mail_settings_client",
            },
        )
        phase = "install-identity-invitation-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=identity_invitation_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "workspace_owner": "workspace_code",
                "labeling_owner": "labeling_code",
                "settings_owner": "settings_code",
                "audit_owner": "audit_code",
                "identity_invitation_role": "identity_invitation_client",
                "identity_mail_v2_role": "identity_mail_v2_client",
            },
        )
        phase = "install-identity-directory-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=identity_directory_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "workspace_owner": "workspace_code",
                "labeling_owner": "labeling_code",
                "audit_owner": "audit_code",
                "identity_directory_role": "identity_directory_client",
            },
        )
        phase = "install-identity-admin-password-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=identity_admin_password_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "workspace_owner": "workspace_code",
                "audit_owner": "audit_code",
                "identity_admin_password_role": "identity_admin_password_client",
            },
        )
        phase = "install-identity-platform-role-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=identity_platform_role_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "workspace_owner": "workspace_code",
                "labeling_owner": "labeling_code",
                "audit_owner": "audit_code",
                "identity_platform_role_role": "identity_platform_role_client",
            },
        )
        phase = "install-identity-suspension-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=identity_suspension_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "workspace_owner": "workspace_code",
                "audit_owner": "audit_code",
                "identity_suspension_role": "identity_suspension_client",
            },
        )
        phase = "install-identity-deletion-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=identity_deletion_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "workspace_owner": "workspace_code",
                "labeling_owner": "labeling_code",
                "audit_owner": "audit_code",
                "identity_deletion_role": "identity_deletion_client",
            },
        )
        phase = "install-identity-offboarding-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=identity_offboarding_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "workspace_owner": "workspace_code",
                "labeling_owner": "labeling_code",
                "audit_owner": "audit_code",
                "identity_offboarding_role": "identity_offboarding_client",
            },
        )
        phase = "install-identity-briefs-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=identity_briefs_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "identity_owner": "identity_code",
                "workspace_owner": "workspace_code",
                "identity_briefs_role": "identity_briefs_client",
            },
        )
        resource_environment = backend.environment()
        resource_environment.update(
            {
                f"AAP_MODULE_DATA_IDENTITY_{kind}_URL": URL.create(
                    "postgresql+asyncpg",
                    username=role,
                    password=identity_passwords[role],
                    host="127.0.0.1",
                    port=port,
                    database=database,
                ).render_as_string(hide_password=False)
                for kind, role in IDENTITY_CLIENT_ROLES.items()
            }
        )
        phase = "install-catalog-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=catalog_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "assets_owner": "assets_code",
                "asset_catalog_role": "asset_catalog_client",
            },
        )
        phase = "install-source-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=source_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "assets_owner": "assets_code",
                "asset_source_role": "asset_source_client",
            },
        )
        phase = "install-import-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=import_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "assets_owner": "assets_code",
                "asset_import_role": "asset_import_client",
            },
        )
        phase = "install-operator-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=operator_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "assets_owner": "assets_code",
                "asset_operator_role": "asset_operator_client",
            },
        )
        phase = "install-media-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=media_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "assets_owner": "assets_code",
                "asset_media_control_role": "asset_media_control_client",
                "asset_media_worker_role": "asset_media_worker_client",
            },
        )
        phase = "install-image-worker-grants"
        _psql(
            container_id,
            container_name,
            container_identity,
            username="owner",
            password=owner_password,
            database=database,
            sql=image_grant_sql,
            variables={
                "expected_database": database,
                "migration_role": "owner",
                "assets_owner": "assets_code",
                "asset_image_worker_role": "asset_image_worker_client",
            },
        )
        phase = "create-scoped-storage-identity"
        for bucket in {
            resources["buckets"]["MINIO_DATASETS_BUCKET"],
            resources["buckets"]["MINIO_BUCKET"],
        }:
            _enable_owned_bucket_versioning(backend, bucket)
        cleanup_storage = _cleanup_storage_scope(backend)
        _create_cleanup_storage_scope(cleanup_storage)
        catalog_storage = _cleanup_storage_scope(backend)
        catalog_storage["datasets_bucket"] = resources["buckets"][
            "MINIO_DATASETS_BUCKET"
        ]
        _create_catalog_storage_scope(catalog_storage)
        source_storage = _cleanup_storage_scope(backend)
        source_storage["datasets_bucket"] = resources["buckets"][
            "MINIO_DATASETS_BUCKET"
        ]
        source_storage["import_bucket"] = resources["buckets"]["MINIO_IMPORT_BUCKET"]
        _create_source_storage_scope(source_storage)
        import_storage = _cleanup_storage_scope(backend)
        import_storage["datasets_bucket"] = resources["buckets"][
            "MINIO_DATASETS_BUCKET"
        ]
        _create_import_storage_scope(import_storage, source=False)
        import_source_storage = _cleanup_storage_scope(backend)
        import_source_storage["source_bucket"] = resources["buckets"][
            "MINIO_IMPORT_BUCKET"
        ]
        _create_import_storage_scope(import_source_storage, source=True)
        media_control_storage = _cleanup_storage_scope(backend)
        media_control_storage["datasets_bucket"] = resources["buckets"][
            "MINIO_DATASETS_BUCKET"
        ]
        media_control_storage["inputs_bucket"] = resources["buckets"][
            "MINIO_INPUTS_BUCKET"
        ]
        media_control_storage["legacy_bucket"] = resources["buckets"]["MINIO_BUCKET"]
        media_control_storage["policy"] = "aap-media-control-" + secrets.token_hex(8)
        _create_media_storage_scope(media_control_storage, worker=False)
        media_worker_storage = _cleanup_storage_scope(backend)
        media_worker_storage["datasets_bucket"] = resources["buckets"][
            "MINIO_DATASETS_BUCKET"
        ]
        media_worker_storage["inputs_bucket"] = resources["buckets"][
            "MINIO_INPUTS_BUCKET"
        ]
        media_worker_storage["policy"] = "aap-media-worker-" + secrets.token_hex(8)
        _create_media_storage_scope(media_worker_storage, worker=True)
        image_worker_storage = _cleanup_storage_scope(backend)
        image_worker_storage["datasets_bucket"] = resources["buckets"][
            "MINIO_DATASETS_BUCKET"
        ]
        image_worker_storage["legacy_bucket"] = resources["buckets"]["MINIO_BUCKET"]
        image_worker_storage["inputs_bucket"] = resources["buckets"][
            "MINIO_INPUTS_BUCKET"
        ]
        image_worker_storage["policy"] = "aap-images-worker-" + secrets.token_hex(8)
        _create_image_storage_scope(image_worker_storage)
        if selected_test_paths & {
            "tests/test_mask_content_live.py",
            "tests/test_mask_content_containers.py",
            "tests/test_mask_gc_live.py",
            "tests/test_mask_gc_cutover_peer.py",
            "tests/test_mask_gc_collection_live.py",
            "tests/test_mask_gc_runtime_image.py",
        }:
            mask_content_storage = _cleanup_storage_scope(backend)
            mask_content_storage["bucket"] = resources["buckets"]["MINIO_BUCKET"]
            mask_content_storage["policy"] = "aap-mask-content-" + secrets.token_hex(8)
            _create_mask_content_storage_scope(mask_content_storage, cleanup=False)
            mask_cleanup_storage = _cleanup_storage_scope(backend)
            mask_cleanup_storage["bucket"] = resources["buckets"]["MINIO_BUCKET"]
            mask_cleanup_storage["policy"] = "aap-mask-cleanup-" + secrets.token_hex(8)
            _create_mask_content_storage_scope(mask_cleanup_storage, cleanup=True)
            evidence["mask_content_s3"] = {
                "bucket": resources["buckets"]["MINIO_BUCKET"],
                "prefix": "raster-masks/sha256/",
                "content_and_cleanup_identities_created": True,
            }
        if "tests/test_video_consumers_containers.py" in selected_test_paths:
            api_bootstrap_storage = _cleanup_storage_scope(backend)
            api_bootstrap_storage["buckets"] = resources["buckets"]
            api_bootstrap_storage["policy"] = "aap-api-bootstrap-" + secrets.token_hex(
                8
            )
            _create_api_bootstrap_storage_scope(api_bootstrap_storage)
            api_storage = _restricted_s3_client(backend, api_bootstrap_storage)
            probe_bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
            probe_key = f"api-bootstrap-probe/{uuid4()}"
            probe_version = backend.s3.put_object(
                Bucket=probe_bucket, Key=probe_key, Body=b"private-source"
            )["VersionId"]
            try:
                for bucket in resources["buckets"].values():
                    api_storage.head_bucket(Bucket=bucket)
                _expect_s3_denied(
                    "API direct source read",
                    lambda: api_storage.get_object(
                        Bucket=probe_bucket, Key=probe_key, VersionId=probe_version
                    ),
                )
                _expect_s3_denied(
                    "API direct source write",
                    lambda: api_storage.put_object(
                        Bucket=probe_bucket, Key=probe_key, Body=b"forbidden"
                    ),
                )
            finally:
                api_storage.close()
                backend.s3.delete_object(
                    Bucket=probe_bucket, Key=probe_key, VersionId=probe_version
                )
        phase = "verify-accepted-input-storage-scopes"
        _verify_accepted_input_s3_scopes(
            backend, media_control_storage, image_worker_storage, cleanup_storage
        )
        evidence["accepted_inputs_s3"] = {
            "bucket": resources["buckets"]["MINIO_INPUTS_BUCKET"],
            "versioning": "Enabled",
            "restricted_control_read": True,
            "restricted_image_worker_versioned_write": True,
            "restricted_cleanup_versions_and_multipart": True,
        }
        from cryptography.fernet import Fernet

        media_control_access_key = media_control_storage["access_key"]
        media_control_secret_key = media_control_storage["secret_key"]
        media_worker_access_key = media_worker_storage["access_key"]
        media_worker_secret_key = media_worker_storage["secret_key"]

        resource_environment.update(
            {
                "AAP_MODULE_DATA_ROLE_ACCEPTANCE": "1",
                "AAP_IDENTITY_BRIEFS_LIVE_ACCEPTANCE": "1",
                "RUN_NATIVE_IMPORT_DELIVERY_LEASE_TEST": "1",
                "AAP_MODULE_DATA_APPEND_URL": append_url,
                "AAP_MODULE_DATA_ASSET_CLEANUP_URL": cleanup_url,
                "AAP_MODULE_DATA_ASSET_CATALOG_URL": catalog_url,
                "AAP_MODULE_DATA_ASSET_SOURCE_URL": source_url,
                "AAP_MODULE_DATA_ASSET_IMPORT_URL": import_url,
                "AAP_MODULE_DATA_ASSET_OPERATOR_URL": operator_url,
                "AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_URL": media_control_url,
                "AAP_MODULE_DATA_ASSET_MEDIA_WORKER_URL": media_worker_url,
                "AAP_MODULE_DATA_ASSET_IMAGE_WORKER_URL": image_worker_url,
                "AAP_MODULE_DATA_PREANNOTATION_RESULT_URL": URL.create(
                    "postgresql+asyncpg",
                    username="preannotation_result_client",
                    password=result_password,
                    host="127.0.0.1",
                    port=port,
                    database=database,
                ).render_as_string(hide_password=False),
                "AAP_MODULE_DATA_LABELING_MASK_GC_URL": URL.create(
                    "postgresql+asyncpg",
                    username="labeling_mask_gc_client",
                    password=mask_gc_password,
                    host="127.0.0.1",
                    port=port,
                    database=database,
                ).render_as_string(hide_password=False),
                "ASSETS_MEDIA_INPUTS_BUCKET": resources["buckets"][
                    "MINIO_INPUTS_BUCKET"
                ],
                "AAP_MODULE_DATA_ASSET_IMPORT_S3_ACCESS_KEY": import_storage[
                    "access_key"
                ],
                "AAP_MODULE_DATA_ASSET_IMPORT_S3_SECRET_KEY": import_storage[
                    "secret_key"
                ],
                "AAP_MODULE_DATA_IMPORT_SOURCE_S3_ACCESS_KEY": import_source_storage[
                    "access_key"
                ],
                "AAP_MODULE_DATA_IMPORT_SOURCE_S3_SECRET_KEY": import_source_storage[
                    "secret_key"
                ],
                "CONNECTOR_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                "AAP_MODULE_DATA_ASSET_SOURCE_S3_ACCESS_KEY": source_storage[
                    "access_key"
                ],
                "AAP_MODULE_DATA_ASSET_SOURCE_S3_SECRET_KEY": source_storage[
                    "secret_key"
                ],
                "AAP_MODULE_DATA_ASSET_CATALOG_S3_ACCESS_KEY": catalog_storage[
                    "access_key"
                ],
                "AAP_MODULE_DATA_ASSET_CATALOG_S3_SECRET_KEY": catalog_storage[
                    "secret_key"
                ],
                "AAP_MODULE_DATA_ASSET_CLEANUP_S3_ACCESS_KEY": cleanup_storage[
                    "access_key"
                ],
                "AAP_MODULE_DATA_ASSET_CLEANUP_S3_SECRET_KEY": cleanup_storage[
                    "secret_key"
                ],
                "AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_S3_ACCESS_KEY": media_control_access_key,
                "AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_S3_SECRET_KEY": media_control_secret_key,
                "AAP_MODULE_DATA_ASSET_MEDIA_WORKER_S3_ACCESS_KEY": media_worker_access_key,
                "AAP_MODULE_DATA_ASSET_MEDIA_WORKER_S3_SECRET_KEY": media_worker_secret_key,
                "AAP_MODULE_DATA_ASSET_IMAGE_WORKER_S3_ACCESS_KEY": image_worker_storage[
                    "access_key"
                ],
                "AAP_MODULE_DATA_ASSET_IMAGE_WORKER_S3_SECRET_KEY": image_worker_storage[
                    "secret_key"
                ],
                "AAP_MODULE_DATA_LEGACY_RUNTIME_URL": URL.create(
                    "postgresql+asyncpg",
                    username="legacy_runtime",
                    password=runtime_password,
                    host="127.0.0.1",
                    port=port,
                    database=database,
                ).render_as_string(hide_password=False),
                "AAP_MODULE_DATA_PG_CONTAINER_ID": container_id,
                "AAP_MODULE_DATA_REDIS_CONTAINER_NAME": resources["redis_name"],
                "AAP_MODULE_DATA_REDIS_OWNER": resources["owner"],
                "AAP_MODULE_DATA_PG_CONTAINER_NAME": container_name,
                "AAP_MODULE_DATA_PG_CONTAINER_IDENTITY": container_identity,
            }
        )
        if api_bootstrap_storage is not None:
            resource_environment.update(
                {
                    "AAP_MODULE_DATA_API_BOOTSTRAP_S3_ACCESS_KEY": api_bootstrap_storage[
                        "access_key"
                    ],
                    "AAP_MODULE_DATA_API_BOOTSTRAP_S3_SECRET_KEY": api_bootstrap_storage[
                        "secret_key"
                    ],
                }
            )
        for label, scope in (
            ("CONTENT", mask_content_storage),
            ("CLEANUP", mask_cleanup_storage),
        ):
            if scope is not None:
                resource_environment.update(
                    {
                        "AAP_MASK_CONTENT_ACCEPTANCE": "1",
                        f"AAP_MODULE_DATA_MASK_{label}_S3_ACCESS_KEY": scope[
                            "access_key"
                        ],
                        f"AAP_MODULE_DATA_MASK_{label}_S3_SECRET_KEY": scope[
                            "secret_key"
                        ],
                    }
                )
        command = [
            sys.executable,
            "-c",
            "import os,sys; args=sys.argv[1:]; os.chdir(args.pop(0)); "
            "sys.path.insert(0, os.getcwd()); sys.argv=['pytest', *args]; "
            "from pytest import console_main; raise SystemExit(console_main())",
            str(ROOT / "apps/api"),
            "-q",
            "--tb=short",
            "--maxfail=3",
            f"--junitxml={junit}",
            *test_files,
        ]
        options = parse_arguments(["exec", "--mode", "test", "--", *command])
        phase = "pytest"
        _container_details(
            container_id, container_name, container_identity, require_running=True
        )
        with patch.dict(os.environ, resource_environment, clear=False):
            exit_code = run_processes(backend, options)
        evidence["test_exit_code"] = exit_code
        evidence["passed_test_count"] = _passed_test_count(junit)
        if exit_code == 0 and evidence["passed_test_count"] == 0:
            raise RuntimeError("Acceptance ran no passing test cases")
        evidence["status"] = "passed" if exit_code == 0 else "failed"
    except Exception as error:
        evidence["status"] = "failed"
        evidence["failure"] = {"phase": phase, "error_type": type(error).__name__}
        exit_code = 1
    finally:
        for label, scope in (
            ("mask_content", mask_content_storage),
            ("mask_cleanup", mask_cleanup_storage),
            ("api_bootstrap", api_bootstrap_storage),
            ("image_worker", image_worker_storage),
            ("media_worker", media_worker_storage),
            ("media_control", media_control_storage),
            ("import", import_storage),
            ("import_source", import_source_storage),
        ):
            if scope is not None:
                try:
                    _destroy_cleanup_storage_scope(scope)
                    evidence["cleanup"][label + "_storage_identity_removed"] = True
                except Exception as error:
                    evidence["cleanup"]["errors"].append(
                        {label + "_storage_identity": type(error).__name__}
                    )
                    exit_code = 1
        if source_storage is not None:
            try:
                _destroy_cleanup_storage_scope(source_storage)
                evidence["cleanup"]["source_storage_identity_removed"] = True
            except Exception as error:
                evidence["cleanup"]["errors"].append(
                    {"source_storage_identity": type(error).__name__}
                )
                exit_code = 1
        if catalog_storage is not None:
            try:
                _destroy_cleanup_storage_scope(catalog_storage)
                evidence["cleanup"]["catalog_storage_identity_removed"] = True
            except Exception as error:
                evidence["cleanup"]["errors"].append(
                    {"catalog_storage_identity": type(error).__name__}
                )
                exit_code = 1
        if cleanup_storage is not None:
            try:
                _destroy_cleanup_storage_scope(cleanup_storage)
                evidence["cleanup"]["storage_identity_removed"] = True
            except Exception as error:
                evidence["cleanup"]["errors"].append(
                    {"storage_identity": type(error).__name__}
                )
                exit_code = 1
        local_resources_destroyed = backend is None or not backend.touched
        if backend is not None and backend.touched:
            try:
                if container_id:
                    _container_details(
                        container_id,
                        container_name,
                        container_identity,
                        require_running=True,
                    )
                backend.destroy(backend.resources["confirmation"])
                evidence["cleanup"]["local_resources_destroyed"] = True
                local_resources_destroyed = True
            except Exception as error:
                evidence["cleanup"]["errors"].append(
                    {"resource": "local-resources", "error_type": type(error).__name__}
                )
                exit_code = 1
        # Keep the verified database reachable when cleanup needs another pass.
        if container_id and local_resources_destroyed:
            try:
                _container_details(
                    container_id,
                    container_name,
                    container_identity,
                    require_running=False,
                )
                from worktree_resources import docker

                docker("rm", "-f", "-v", container_id)
                evidence["cleanup"]["postgres_container_removed"] = True
            except Exception as error:
                evidence["cleanup"]["errors"].append(
                    {
                        "resource": "postgres-container",
                        "error_type": type(error).__name__,
                    }
                )
                exit_code = 1
        if local_resources_destroyed:
            try:
                shutil.rmtree(resource_root)
            except FileNotFoundError:
                pass
            except Exception as error:
                evidence["cleanup"]["errors"].append(
                    {"resource": "resource-root", "error_type": type(error).__name__}
                )
                evidence["cleanup"]["resource_root_retained"] = str(resource_root)
                exit_code = 1
        else:
            evidence["cleanup"]["resource_root_retained"] = str(resource_root)
        if exit_code != 0:
            evidence["status"] = "failed"
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_evidence(output, evidence)
        print(
            f"{'PASS' if exit_code == 0 else 'FAIL'}: module-data acceptance; "
            f"evidence={output} junit={junit}",
            flush=True,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
